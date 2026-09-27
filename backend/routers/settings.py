import gzip
import logging
import os
import shutil
import subprocess  # noqa: S404  # nosec B404 - required for DB backup (mysqldump), admin-only
import tempfile
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, field_validator

from backend.config import settings
from backend.models import Role, User
from backend.services.auth import require_role
from backend.services.email import send_email


def _redact_db_url(db_url: str) -> str:
    """Return a DSN safe to expose: credentials replaced, host/db retained.

    Keeps enough for an operator to tell which database is in use without
    disclosing the username or password embedded in the connection string.
    """
    try:
        parsed = urlparse(db_url)
    except ValueError:
        return "<unparseable>"
    if not parsed.password:
        return db_url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    user = parsed.username or ""
    return f"{parsed.scheme}://{user}:***@{host}{parsed.path}"


router = APIRouter(prefix="/settings", tags=["settings"])


class TestEmailRequest(BaseModel):
    email: str


class SettingsOut(BaseModel):
    media_root: str
    frontend_port: int
    backend_port: int
    smtp_host: Optional[str] = None
    smtp_port: int
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_email: Optional[str] = None
    smtp_use_tls: bool
    public_url: Optional[str]
    disk_usage_gb: Optional[float] = None
    disk_total_gb: Optional[str] = None
    disk_used_gb: Optional[str] = None
    disk_free_gb: Optional[str] = None
    guest_login_enabled: bool = True
    guest_email: Optional[str] = None


@router.get("/", response_model=SettingsOut)
def get_settings(_: User = Depends(require_role(Role.admin))):
    media_root = settings.MEDIA_ROOT
    disk_gb = None
    try:
        if os.path.isdir(media_root):
            total = sum(
                f.stat().st_size for f in os.scandir(media_root) if f.is_file()
            )
            disk_gb = round(total / (1024**3), 2)
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not compute disk usage: %s", exc)
    return SettingsOut(
        media_root=media_root,
        frontend_port=settings.FRONTEND_PORT if hasattr(settings, "FRONTEND_PORT") else 3000,
        backend_port=settings.BACKEND_PORT,
        smtp_host=settings.SMTP_HOST or None,
        smtp_port=settings.SMTP_PORT,
        smtp_username=settings.SMTP_USERNAME or None,
        smtp_password=None,  # Never return password in API response (write-only)
        smtp_from_email=settings.SMTP_FROM_EMAIL or None,
        smtp_use_tls=settings.SMTP_USE_TLS,
        public_url=settings.PUBLIC_URL or None,
        disk_usage_gb=disk_gb,
        guest_login_enabled=settings.GUEST_LOGIN_ENABLED,
        guest_email=settings.DEFAULT_GUEST_EMAIL,
    )


class GuestLoginToggle(BaseModel):
    enabled: bool = True


class SmtpSettings(BaseModel):
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_email: Optional[str] = None
    smtp_use_tls: Optional[bool] = True

    @field_validator("smtp_port")
    @classmethod
    def validate_port(cls, v):
        if v is not None and (v < 1 or v > 65535):
            raise ValueError("Port must be between 1 and 65535")
        return v


@router.post("/guest-login", response_model=dict)
def toggle_guest_login(
    payload: GuestLoginToggle,
    _: User = Depends(require_role(Role.admin)),
):
    enabled = payload.enabled
    _update_env("GUEST_LOGIN_ENABLED", "true" if enabled else "false")
    object.__setattr__(settings, "GUEST_LOGIN_ENABLED", enabled)
    return {"status": "ok", "guest_login_enabled": enabled, "message": "Guest login " + ("enabled" if enabled else "disabled")}


@router.get("/guest-login-enabled", response_model=dict)
def get_guest_login_enabled():
    return {"enabled": settings.GUEST_LOGIN_ENABLED}


@router.get("/storage", response_model=dict)
def get_storage_info(_: User = Depends(require_role(Role.admin))):
    media_root = settings.MEDIA_ROOT
    usage: dict = {"media_root": media_root, "files": 0, "total_bytes": 0, "total_gb": 0}
    try:
        if os.path.isdir(media_root):
            for root, _, files in os.walk(media_root):
                for f in files:
                    fp = os.path.join(root, f)
                    usage["files"] += 1
                    usage["total_bytes"] += os.path.getsize(fp)
            usage["total_gb"] = round(usage["total_bytes"] / (1024**3), 2)
    except Exception:
        usage["error"] = "Unable to read media storage"
    disk = shutil.disk_usage(media_root if os.path.isdir(media_root) else "/")
    usage["disk_total_gb"] = round(disk.total / (1024**3), 2)
    usage["disk_used_gb"] = round(disk.used / (1024**3), 2)
    usage["disk_free_gb"] = round(disk.free / (1024**3), 2)
    return usage


@router.post("/backup", response_model=dict)
def backup_database(_: User = Depends(require_role(Role.admin))):
    """Create a compressed database backup (MySQL dump or SQLite copy), then return a download URL."""
    db_url = settings.DATABASE_URL
    try:
        fd, raw_path = tempfile.mkstemp(suffix=".sql", prefix="recipe_app_backup_")
        os.close(fd)
        backup_path = raw_path + ".gz"
        if db_url.startswith("sqlite"):
            db_path = db_url.replace("sqlite:///", "")
            if not os.path.isfile(db_path):
                raise FileNotFoundError("SQLite database not found")
            shutil.copy2(db_path, raw_path)
        elif "mysql" in db_url:
            parsed = urlparse(db_url)
            user = parsed.username or ""
            password = parsed.password or ""
            host = parsed.hostname or "localhost"
            port = parsed.port or 3306
            db = parsed.path.lstrip("/")
            env = os.environ.copy()
            env["MYSQL_PWD"] = password
            mysqldump = shutil.which("mysqldump") or "mysqldump"
            result = subprocess.run(  # nosec B607,B603 - mysqldump on admin-only backup endpoint
                [mysqldump, f"-h{host}", f"-P{port}", f"-u{user}", db],
                capture_output=True, text=True, env=env
            )
            if result.returncode != 0:
                raise RuntimeError("Database backup failed")
            with open(raw_path, "w") as f:
                f.write(result.stdout)
        else:
            raise ValueError("Unsupported database type")
        # Compress the backup
        with open(raw_path, "rb") as f_in:
            with gzip.open(backup_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        os.unlink(raw_path)
        file_size = os.path.getsize(backup_path)
        return {
            "status": "ok",
            "backup_path": backup_path,
            "download_url": f"/settings/backup/download?path={backup_path}",
            "size_mb": round(file_size / (1024**2), 2),
        }
    except RuntimeError:
        raise HTTPException(status_code=500, detail="Database backup failed")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Database file not found")
    except Exception:
        raise HTTPException(status_code=500, detail="Unexpected error during backup")


@router.get("/backup/download", response_class=Response)
def download_backup(
    path: str,
    _: User = Depends(require_role(Role.admin)),
):
    """Download a compressed database backup file."""
    # Security: only allow downloading files from the temp directory
    real_path = os.path.realpath(path)
    tmp_dir = os.path.realpath(tempfile.gettempdir())
    if not real_path.startswith(tmp_dir) or not path.endswith(".sql.gz"):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.isfile(real_path):
        raise HTTPException(status_code=404, detail="Backup file not found")
    filename = os.path.basename(real_path)
    with open(real_path, "rb") as f:
        file_data = f.read()
    return Response(
        content=file_data,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _update_env(key: str, value: str):
    """Update or add a key=value pair in the .env file.

    The file is rewritten with owner-only permissions (0600) because it holds
    SMTP credentials in plaintext by design: the backend reads SMTP_PASSWORD
    back from this file at startup, so the value cannot be stored hashed the
    way a user password can. Restricting the mode is what keeps that plaintext
    from being world-readable.
    """
    env_path = ".env"
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith(key + "="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
    except Exception:
        os.close(fd)
        raise
    # os.open only applies the mode when creating; enforce it for existing files
    # too, which may have been created by docker compose with 0644.
    os.chmod(env_path, 0o600)


@router.post("/smtp", response_model=dict)
def update_smtp_settings(
    payload: SmtpSettings,
    _: User = Depends(require_role(Role.admin)),
):
    """Update SMTP/email configuration and write to .env file."""
    updates = {}
    if payload.smtp_host is not None:
        updates["SMTP_HOST"] = payload.smtp_host.strip()
        object.__setattr__(settings, "SMTP_HOST", payload.smtp_host.strip())
    if payload.smtp_port is not None:
        updates["SMTP_PORT"] = str(payload.smtp_port)
        object.__setattr__(settings, "SMTP_PORT", payload.smtp_port)
    if payload.smtp_username is not None:
        updates["SMTP_USERNAME"] = payload.smtp_username.strip()
        object.__setattr__(settings, "SMTP_USERNAME", payload.smtp_username.strip())
    if payload.smtp_password is not None:
        updates["SMTP_PASSWORD"] = payload.smtp_password
        object.__setattr__(settings, "SMTP_PASSWORD", payload.smtp_password)
    if payload.smtp_from_email is not None:
        updates["SMTP_FROM_EMAIL"] = payload.smtp_from_email.strip()
        object.__setattr__(settings, "SMTP_FROM_EMAIL", payload.smtp_from_email.strip())
    if payload.smtp_use_tls is not None:
        updates["SMTP_USE_TLS"] = "true" if payload.smtp_use_tls else "false"
        object.__setattr__(settings, "SMTP_USE_TLS", payload.smtp_use_tls)
    for key, value in updates.items():
        _update_env(key, value)
    return {
        "status": "ok",
        "message": "SMTP settings updated. Restart the backend container to apply changes.",
        "smtp_host": settings.SMTP_HOST,
        "smtp_port": settings.SMTP_PORT,
        "smtp_from_email": settings.SMTP_FROM_EMAIL,
        "smtp_use_tls": settings.SMTP_USE_TLS,
    }


@router.post("/smtp/test", response_model=dict)
def test_smtp_settings(
    payload: TestEmailRequest,
    _: User = Depends(require_role(Role.admin)),
):
    """Send a test email to verify SMTP settings are working."""
    if not settings.SMTP_HOST or not settings.SMTP_FROM_EMAIL:
        raise HTTPException(status_code=400, detail="SMTP settings are not configured")
    sent = send_email(
        to=payload.email,
        subject="WhiskFul SMTP Test",
        body="<h2>✅ SMTP is working!</h2><p>If you received this email, your SMTP configuration is correct.</p><p> — WhiskFul</p>",
    )
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send test email — check SMTP settings and credentials")
    return {"status": "ok", "message": "Test email sent to " + payload.email}


@router.get("/db-health", response_model=dict)
def db_health(_: User = Depends(require_role(Role.admin))):
    """Check database connectivity and basic health."""
    from sqlalchemy import inspect, text

    from backend.database import SessionLocal, engine

    db = SessionLocal()
    try:
        # Basic connectivity check
        db.execute(text("SELECT 1"))
        connected = True
    except Exception as exc:
        connected = False
        # Log the detail server-side; return only a status. The raw exception
        # can carry driver text, connection details, or credentials.
        logging.warning("Database connectivity check failed: %s", type(exc).__name__, exc_info=True)
        return {
            "status": "unhealthy",
            "connected": False,
            "error": "Database connectivity check failed",
        }
    finally:
        db.close()

    # Get table info
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    db_url = settings.DATABASE_URL
    db_type = "mysql" if "mysql" in db_url else ("sqlite" if db_url.startswith("sqlite") else "unknown")

    return {
        "status": "healthy",
        "connected": connected,
        "database_type": db_type,
        # Redacted: the DSN embeds the DB user and password, so returning it
        # verbatim would hand credentials to any caller of this endpoint.
        "database_url": _redact_db_url(db_url),
        "table_count": len(tables),
        "tables": sorted(tables),
    }


@router.get("/db-diag", response_model=dict)
def db_diagnose(_: User = Depends(require_role(Role.admin))):
    """Run detailed database diagnostics: row counts, schema issues, orphaned records."""
    from sqlalchemy import MetaData, Table, func, inspect, select, text

    from backend.database import SessionLocal
    from backend.models import Cookbook, GroceryList, Household, MealPlan, Note, Recipe, RecipeMedia, Tag, User

    db = SessionLocal()
    results: dict = {"tables": {}}

    # Define models to check with their relationship fields
    model_checks = [
        ("recipes", Recipe, {"cooking_steps": "cookbook_id", "notes": "owner_id"}),
        ("recipe_media", RecipeMedia, {}),
        ("cookbooks", Cookbook, {}),
        ("households", Household, {}),
        ("users", User, {}),
        ("tags", Tag, {}),
        ("meal_plans", MealPlan, {}),
        ("grocery_lists", GroceryList, {}),
        ("notes", Note, {}),
    ]

    try:
        inspector = inspect(db.get_bind())

        for table_name, _, _ in model_checks:
            info: dict = {}
            try:
                # Row count - use SQLAlchemy table reflection to avoid SQL injection
                table_obj = Table(table_name, MetaData(), autoload_with=db.get_bind())
                count_result = db.execute(select(func.count()).select_from(table_obj)).scalar()
                info["row_count"] = count_result

                # Check for nullable columns that shouldn't be null
                columns = inspector.get_columns(table_name)
                info["columns"] = [
                    {"name": col["name"], "nullable": col["nullable"], "type": str(col["type"])}
                    for col in columns
                ]
            except Exception as exc:
                info["error"] = str(exc)
            results["tables"][table_name] = info

        # Check for orphaned recipes (cookbook_id pointing to non-existent cookbook)
        try:
            orphaned = db.execute(text(
                "SELECT r.id, r.title, r.cookbook_id FROM recipes r "
                "LEFT JOIN cookbooks c ON r.cookbook_id = c.id "
                "WHERE c.id IS NULL"
            )).fetchall()
            results["orphaned_recipes"] = [{"id": row[0], "title": row[1], "cookbook_id": row[2]} for row in orphaned]
        except Exception:
            results["orphaned_recipes"] = "Check failed (table may not exist)"

        # Check for duplicate recipe URLs
        try:
            dupes = db.execute(text(
                "SELECT source_url, COUNT(*) as cnt FROM recipes "
                "WHERE source_url IS NOT NULL GROUP BY source_url HAVING cnt > 1"
            )).fetchall()
            results["duplicate_recipes"] = [{"source_url": row[0], "count": row[1]} for row in dupes]
        except Exception:
            results["duplicate_recipes"] = "Check failed"

        # Check for recipes with null ingredients/instructions
        try:
            null_ingredients = db.execute(text(
                "SELECT id, title, source_url FROM recipes WHERE ingredients IS NULL ORDER BY id DESC LIMIT 20"
            )).fetchall()
            results["recipes_missing_ingredients"] = [
                {"id": row[0], "title": row[1], "source_url": row[2]} for row in null_ingredients
            ]
        except Exception:
            results["recipes_missing_ingredients"] = "Check failed"

        # Check for recipes with null instructions
        try:
            null_instructions = db.execute(text(
                "SELECT id, title, source_url FROM recipes WHERE instructions IS NULL ORDER BY id DESC LIMIT 20"
            )).fetchall()
            results["recipes_missing_instructions"] = [
                {"id": row[0], "title": row[1], "source_url": row[2]} for row in null_instructions
            ]
        except Exception:
            results["recipes_missing_instructions"] = "Check failed"

        results["status"] = "completed"
    except Exception as exc:
        results["status"] = "error"
        # Detail logged, not returned: these are admin-only endpoints, but an
        # exception string can disclose schema and connection internals.
        logging.warning("Database diagnostics failed: %s", type(exc).__name__, exc_info=True)
        results["error"] = "Database diagnostics failed; see server logs"
    finally:
        db.close()

    return results


@router.post("/db-repair", response_model=dict)
def db_repair(_: User = Depends(require_role(Role.admin))):
    """Attempt to repair common database issues.

    - Reassigns orphaned recipes to a local cookbook
    - Cleans up null titles
    - Returns a summary of repairs made.
    """
    from sqlalchemy import inspect, text

    from backend.database import SessionLocal, engine
    from backend.models import Cookbook, Store

    db = SessionLocal()
    repairs: list[str] = []
    try:
        from backend.models import User as UserModel
        # Find or create local cookbook
        local_cb = db.query(Cookbook).filter(Cookbook.name == "Local Recipes").first()
        if local_cb:
            repairs.append("Local 'Local Recipes' cookbook found: id=" + str(local_cb.id))
        else:
            # Find the first admin user to assign as owner
            admin_user = db.query(UserModel).filter(UserModel.role == "admin").first()
            if not admin_user:
                admin_user = db.query(UserModel).first()
            if admin_user:
                local_cb = Cookbook(name="Local Recipes", store=Store.local, owner_id=admin_user.id)
                db.add(local_cb)
                db.commit()
                db.refresh(local_cb)
                repairs.append("Created 'Local Recipes' cookbook (owner: " + str(admin_user.id) + ")")
            else:
                # Can't create cookbook — no users exist
                local_cb = None

        # Fix orphaned recipes (cookbook_id pointing to non-existent cookbook)
        orphaned = db.execute(text(
            "SELECT r.id FROM recipes r "
            "LEFT JOIN cookbooks c ON r.cookbook_id = c.id "
            "WHERE c.id IS NULL"
        )).fetchall()
        if orphaned and local_cb:
            db.execute(text(
                "UPDATE recipes SET cookbook_id = :cb_id "
                "WHERE cookbook_id IS NULL OR cookbook_id NOT IN (SELECT id FROM cookbooks)"
            ), {"cb_id": local_cb.id})
            db.commit()
            repairs.append(f"Fixed {len(orphaned)} orphaned recipes -> Local Recipes (id={local_cb.id})")
        elif orphaned:
            repairs.append(f"WARNING: {len(orphaned)} orphaned recipes found but no valid cookbook to reassign them to")

        # Fix null titles (use source_url or filename as fallback)
        null_titles = db.execute(text("SELECT id, source_url FROM recipes WHERE title IS NULL OR title = ''")).fetchall()
        if null_titles:
            for row in null_titles:
                new_title = f"Recipe #{row[0]}"
                if row[1]:
                    new_title = row[1].split("/")[-1][:80]
                db.execute(text("UPDATE recipes SET title = :title WHERE id = :rid"), {"title": new_title, "rid": row[0]})
            db.commit()
            repairs.append(f"Fixed {len(null_titles)} recipes with null titles")

        # Check for schema issues (sqlite-specific table_info)
        db_type = "sqlite" if str(engine.url).startswith("sqlite") else "mysql"
        if db_type == "sqlite":
            # Check for column existence issues
            inspector = inspect(db.get_bind())
            for table_name in ["recipes", "cookbooks", "users", "notes", "tags"]:
                try:
                    columns = inspector.get_columns(table_name)
                    col_names = [c["name"] for c in columns]
                    # Check for expected columns
                    expected = {
                        "recipes": ["id", "title", "ingredients", "instructions", "source_url", "cookbook_id"],
                        "users": ["id", "email", "hashed_password", "role"],
                        "cookbooks": ["id", "name"],
                    }
                    if table_name in expected:
                        missing = [c for c in expected[table_name] if c not in col_names]
                        if missing:
                            repairs.append(f"WARNING: Table '{table_name}' missing columns: {missing}")
                except Exception as exc:
                    logging.debug("Table inspection failed for %s: %s", table_name, type(exc).__name__)
                    repairs.append(f"Could not inspect table {table_name}")

        results = {
            "status": "ok",
            "repairs_performed": repairs if repairs else ["No issues found — database is healthy"],
        }
    except Exception as exc:
        logging.warning("Database repair failed: %s", type(exc).__name__, exc_info=True)
        results = {
            "status": "error",
            "error": "Database repair failed; see server logs",
            "repairs_performed": repairs,
        }
    finally:
        db.close()

    return results
