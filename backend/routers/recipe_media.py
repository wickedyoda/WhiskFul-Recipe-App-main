"""API endpoints for managing recipe media (images, documents, PDFs) linked to recipes."""

import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Recipe, RecipeMedia, User
from backend.schemas import RecipeMediaItem
from backend.services.auth import get_current_user
from backend.services.media_text import extract_text_from_file

router = APIRouter(prefix="/recipes/{recipe_id}/media", tags=["recipe-media"])

# Base directory for uploaded recipe media, matching the layout this router has
# always used (backend/media/recipes/<recipe_id>/media/...). The stored
# file_path is a relpath against the backend directory, so changing the layout
# would orphan every existing RecipeMedia row. MEDIA_ROOT is deliberately not
# consulted here: ingest.py owns that setting for its own working directories.
MEDIA_BASE = Path("backend/media/recipes").resolve()


def _safe_join(*parts: str) -> str:
    """Join path segments under MEDIA_BASE, rejecting anything that escapes it.

    Uses Path.resolve() so '..' segments and symlinks are collapsed before the
    containment check, then compares with is_relative_to() rather than a string
    prefix (which would wrongly accept a sibling like /media-evil).
    """
    candidate = MEDIA_BASE.joinpath(*parts).resolve()
    if not candidate.is_relative_to(MEDIA_BASE):
        raise HTTPException(status_code=400, detail="Invalid media path")
    return str(candidate)

# Allowed file types for recipe media
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
PDF_EXTS = {".pdf"}
DOC_EXTS = {".docx", ".doc"}
TEXT_EXTS = {".txt", ".md", ".csv"}
ALLOWED_EXTS = IMAGE_EXTS | PDF_EXTS | DOC_EXTS | TEXT_EXTS

# Max upload size: 10MB
MAX_FILE_SIZE = 10 * 1024 * 1024


@router.post("", response_model=RecipeMediaItem)
def upload_recipe_media(
    recipe_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a media file (image, PDF, document) to a recipe."""
    recipe = db.query(Recipe).filter(
        Recipe.id == recipe_id, Recipe.owner_id == current_user.id
    ).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    # Validate extension
    filename = file.filename or "upload"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Allowed: images (PNG/JPG/WebP), PDFs, DOC/DOCX, TXT/MD/CSV"
        )

    # Save file. The destination is built from the (server-generated) filename
    # under a per-recipe directory; _safe_join confirms it stays inside the
    # media root regardless of the recipe_id path segment.
    dest_dir = _safe_join(str(recipe_id), "media")
    os.makedirs(dest_dir, exist_ok=True)
    name = f"{int(time.time())}_{uuid.uuid4().hex}{ext}"
    file_path = _safe_join(str(recipe_id), "media", name)

    # Read file content
    content = file.file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 10MB)")

    with open(file_path, "wb") as f:
        f.write(content)

    # Determine media type
    if ext in IMAGE_EXTS:
        media_type = "image"
        thumbnail = _generate_thumbnail(file_path, ext)
    elif ext in PDF_EXTS:
        media_type = "pdf"
        thumbnail = None
    elif ext in DOC_EXTS:
        media_type = "document"
        thumbnail = None
    else:
        media_type = "text"
        thumbnail = None

    # Extract text content for search indexing
    extracted_text = extract_text_from_file(file_path, filename)

    rel_path = os.path.relpath(file_path, "backend")

    db_media = RecipeMedia(
        recipe_id=recipe_id,
        owner_id=current_user.id,
        file_path=rel_path,
        thumbnail_path=thumbnail,
        original_filename=filename,
        media_type=media_type,
        file_size=len(content),
        extracted_text=extracted_text if extracted_text else None,
    )
    db.add(db_media)
    db.commit()
    db.refresh(db_media)

    return RecipeMediaItem.model_validate(db_media)


@router.get("", response_model=list[RecipeMediaItem])
def list_recipe_media(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all media files attached to a recipe."""
    recipe = db.query(Recipe).filter(
        Recipe.id == recipe_id, Recipe.owner_id == current_user.id
    ).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    items = db.query(RecipeMedia).filter(
        RecipeMedia.recipe_id == recipe_id
    ).order_by(RecipeMedia.created_at.desc()).all()
    return [RecipeMediaItem.model_validate(m) for m in items]


@router.delete("/{media_id}")
def delete_recipe_media(
    recipe_id: int,
    media_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a media file from a recipe."""
    item = db.query(RecipeMedia).filter(
        RecipeMedia.id == media_id,
        RecipeMedia.recipe_id == recipe_id,
        RecipeMedia.owner_id == current_user.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Media not found")

    # Delete file from disk
    full_path = os.path.join("backend", item.file_path)
    if os.path.exists(full_path):
        os.remove(full_path)
    if item.thumbnail_path:
        thumb_path = os.path.join("backend", item.thumbnail_path)
        if os.path.exists(thumb_path):
            os.remove(thumb_path)

    db.delete(item)
    db.commit()
    return {"deleted": True}


def _generate_thumbnail(file_path: str, ext: str) -> str | None:
    """Generate a thumbnail for an image file."""
    try:
        from PIL import Image  # noqa: PLC0415
        thumb_dir = _safe_join(os.path.relpath(os.path.dirname(file_path), MEDIA_BASE), "thumbs")
        os.makedirs(thumb_dir, exist_ok=True)
        thumb_name = f"thumb_{os.path.basename(file_path)}"
        thumb_path = os.path.join(thumb_dir, thumb_name)
        with Image.open(file_path) as img:
            img.thumbnail((150, 150))
            img.save(thumb_path, "PNG")
        return os.path.relpath(thumb_path, "backend")
    except Exception:
        return None
