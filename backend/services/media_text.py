"""Text extraction from uploaded media files (images, PDFs, documents)."""

import logging
import shutil
import subprocess  # noqa: S404  # nosec B404 - required for textract on user-uploaded documents
from pathlib import Path


def extract_text_from_file(file_path: str, filename: str) -> str:
    """Extract text content from uploaded media files.

    Supports: PDF, DOCX, images (PNG/JPG), and text files.
    Returns extracted text or empty string on failure.
    """
    suffix = Path(filename).suffix.lower()
    extracted = ""

    if suffix == ".pdf":
        extracted = _extract_pdf_text(file_path)
    elif suffix in (".docx",):
        extracted = _extract_docx_text(file_path)
    elif suffix in (".doc",):
        extracted = _extract_doc_text(file_path)
    elif suffix in (".txt", ".md", ".csv"):
        try:
            with open(file_path, errors="ignore") as f:
                extracted = f.read(10000)  # Cap at 10k chars
        except OSError:
            pass
    elif suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        extracted = _extract_image_text(file_path)

    return extracted.strip()[:50000]  # Cap at 50k chars


def _extract_pdf_text(file_path: str) -> str:
    """Extract text from PDF using pdftotext (poppler-utils) or pypdf."""
    # Try pdftotext first (fast, accurate)
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        try:
            result = subprocess.run(  # nosec B603 - pdftotext on trusted file path
                [pdftotext, "-layout", file_path, "-"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Fallback: try pypdf
    try:
        import pypdf  # noqa: PLC0415
        with open(file_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            pages = []
            for page in reader.pages:
                text = page.extract_text() or ""
                pages.append(text)
            return "\n".join(pages)[:50000]
    except Exception as exc:
        logging.warning("PDF text extraction failed: %s", exc)
    return ""


def _extract_docx_text(file_path: str) -> str:
    """Extract text from DOCX using python-docx."""
    try:
        import docx  # noqa: PLC0415
        doc = docx.Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs)[:50000]
    except Exception as exc:
        logging.warning("DOCX text extraction failed: %s", exc)
    return ""


def _extract_doc_text(file_path: str) -> str:
    """Extract text from legacy DOC using antiword or catdoc."""
    for binary in ("antiword", "catdoc"):
        path = shutil.which(binary)
        if path:
            try:
                result = subprocess.run(  # nosec B603 - textract on trusted file path
                    [path, file_path],
                    capture_output=True, text=True, timeout=30
                )
                if result.stdout.strip():
                    return result.stdout[:50000]
            except (subprocess.TimeoutExpired, OSError):
                pass
    return ""


def _extract_image_text(file_path: str) -> str:
    """Extract text from images using OCR (pytesseract)."""
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
        img = Image.open(file_path)
        text = pytesseract.image_to_string(img, lang="eng")
        return text.strip()[:50000]
    except Exception as exc:
        logging.warning("OCR text extraction failed: %s", exc)
    return ""
