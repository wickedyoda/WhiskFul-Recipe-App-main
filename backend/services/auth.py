import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt as _jwt
from backend.config import settings
from backend.database import SessionLocal
from backend.models import PasswordHistory, PasswordResetToken, Role, User
from backend.services.email import send_email
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError

SECRET_KEY = settings.SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE = timedelta(days=15)
PASSWORD_RESET_EXPIRE = timedelta(hours=1)
PASSWORD_HISTORY_LIMIT = 5


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


def create_access_token(email: str, expires_delta: timedelta | None = None):
    payload = {"sub": email, "type": "access"}
    expire = datetime.now(timezone.utc) + (expires_delta or ACCESS_TOKEN_EXPIRE)
    payload["exp"] = int(expire.timestamp())
    return _jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_password_reset_token(user_id: int) -> str:
    db = SessionLocal()
    try:
        raw = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + PASSWORD_RESET_EXPIRE
        token = PasswordResetToken(
            user_id=user_id,
            token=raw,
            expires_at=expires,
        )
        db.add(token)
        db.commit()
        return raw
    finally:
        db.close()


def consume_password_reset_token(token: str, new_password: str) -> bool:
    db = SessionLocal()
    try:
        entry = db.query(PasswordResetToken).filter(PasswordResetToken.token == token).first()
        if not entry or entry.used or entry.expires_at < datetime.now(timezone.utc):
            return False
        user = db.query(User).filter(User.id == entry.user_id).first()
        if not user:
            return False
        if is_password_reused(new_password, user.id, db):
            raise HTTPException(status_code=400, detail="New password must not match the last 5 passwords")
        record_password_history(user.id, user.hashed_password, db)
        user.hashed_password = hash_password(new_password)
        user.must_change_password = 0
        user.password_changed_at = datetime.now(timezone.utc)
        entry.used = 1
        db.add(user)
        db.add(entry)
        db.commit()
        return True
    finally:
        db.close()


def send_password_reset_email(to_email: str, token: str) -> bool:
    reset_url = f"{settings.PUBLIC_URL or 'http://localhost:3000'}/reset-password?token={token}"
    body = f"""
    <html><body>
    <p>Click the link below to reset your password. This link expires in 1 hour.</p>
    <p><a href="{reset_url}">Reset Password</a></p>
    <p>If you did not request a reset, ignore this email.</p>
    </body></html>
    """
    return send_email(to_email, "Password Reset", body)


def request_password_reset(email: str) -> bool:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return False
        token = create_password_reset_token(user.id)
        return send_password_reset_email(user.email, token)
    finally:
        db.close()


def is_password_reused(new_password: str, user_id: int, db) -> bool:
    """Check if the new password matches any of the last PASSWORD_HISTORY_LIMIT passwords."""
    history = db.query(PasswordHistory).filter(PasswordHistory.user_id == user_id).order_by(PasswordHistory.id.desc()).limit(PASSWORD_HISTORY_LIMIT).all()
    for entry in history:
        if verify_password(new_password, entry.hashed_password):
            return True
    return False


def record_password_history(user_id: int, hashed_password: str, db) -> None:
    """Record a password hash into history."""
    entry = PasswordHistory(
        user_id=user_id,
        hashed_password=hashed_password,
    )
    db.add(entry)
    db.commit()


def change_password(user: User, current_password: str, new_password: str, db) -> None:
    if not verify_password(current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if verify_password(new_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="New password must be different from current password")
    if is_password_reused(new_password, user.id, db):
        raise HTTPException(
            status_code=400,
            detail=f"New password must not match the last {PASSWORD_HISTORY_LIMIT} passwords",
        )
    new_hash = hash_password(new_password)
    record_password_history(user.id, user.hashed_password, db)
    user.hashed_password = new_hash
    user.must_change_password = 0
    user.password_changed_at = datetime.now(timezone.utc)
    db.add(user)
    db.commit()


def change_password_admin(target: User, new_password: str, db) -> None:
    """Admin-forced password change — no current password required."""
    if verify_password(new_password, target.hashed_password):
        raise HTTPException(status_code=400, detail="New password must be different from current password")
    if is_password_reused(new_password, target.id, db):
        raise HTTPException(
            status_code=400,
            detail=f"New password must not match the last {PASSWORD_HISTORY_LIMIT} passwords",
        )
    new_hash = hash_password(new_password)
    record_password_history(target.id, target.hashed_password, db)
    target.hashed_password = new_hash
    target.must_change_password = 0
    target.password_changed_at = datetime.now(timezone.utc)
    db.add(target)
    db.commit()


def change_email(user: User, new_email: str, db) -> None:
    existing = db.query(User).filter(User.email == new_email).first()
    if existing and existing.id != user.id:
        raise HTTPException(status_code=400, detail="Email already in use")
    user.email = new_email
    db.add(user)
    db.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    authorization: str | None = Header(default=None),
):
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
    elif credentials:
        token = credentials.credentials

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        payload = _jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        if not email:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()
    db.close()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_approved:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account pending approval")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is inactive")
    return user


def require_password_change(current_user: User = Depends(get_current_user)) -> User:
    if current_user.must_change_password:
        raise HTTPException(status_code=403, detail="Password change required")
    return current_user


def require_role(*allowed: Role):
    def checker(current_user: User = Depends(require_password_change)):
        if current_user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return current_user
    return checker
