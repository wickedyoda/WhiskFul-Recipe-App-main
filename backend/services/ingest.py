import json
import logging
import os
import re
import shutil
import subprocess  # noqa: S404  # nosec B603 - required for ffmpeg, whisper, pytesseract on admin/user-uploaded media
import uuid
from datetime import datetime
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import quote as url_quote
from urllib.parse import urlparse

import requests
import yt_dlp
from backend.database import SessionLocal
from backend.models import Cookbook, Recipe, Store
from backend.services.media_text import extract_text_from_file
from sqlalchemy.orm import Session

MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/media"))
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
(MEDIA_ROOT / "audio").mkdir(exist_ok=True)
(MEDIA_ROOT / "subtitles").mkdir(exist_ok=True)
(MEDIA_ROOT / "raw").mkdir(exist_ok=True)

_VALID_SCHEMES = {"http", "https"}
_MAX_URL_LENGTH = 2048
_URL_RE = re.compile(r"^https?://[^\s]+$")

# SSRF protection — block private/internal/reserved addresses
_PRIVATE_NETWORKS = [
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),
    ip_network("0.0.0.0/8"),
    ip_network("224.0.0.0/4"),
    ip_network("240.0.0.0/4"),
]
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}


def _is_private_host(hostname: str) -> bool:
    """Check if a hostname resolves to or is a private/internal address (SSRF protection)."""
    hostname = hostname.lower().strip()
    if hostname in _BLOCKED_HOSTS:
        return True
    # Check if it's an IP address
    try:
        ip = ip_address(hostname)
        for net in _PRIVATE_NETWORKS:
            if ip in net:
                return True
    except ValueError:
        pass  # Not an IP, it's a domain name — let yt-dlp handle it
    return False


def _log_safe(value: object, max_len: int = 200) -> str:
    """Strip control characters and bound length so untrusted values cannot
    forge log records (CodeQL: py/log-injection)."""
    text = str(value)
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:max_len]


def _sanitize_media_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if len(url) > _MAX_URL_LENGTH:
        raise ValueError("url exceeds maximum allowed length")
    parsed = urlparse(url)
    if parsed.scheme not in _VALID_SCHEMES:
        raise ValueError("unsupported url scheme")
    if not parsed.netloc:
        raise ValueError("url must include a host")
    if not _URL_RE.match(url):
        raise ValueError("invalid url format")
    # SSRF protection — block internal/private hosts
    hostname = parsed.hostname or ""
    if _is_private_host(hostname):
        raise ValueError("url resolves to a private or internal host")
    return url.strip()


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # nosec B603,B607 - subprocess on trusted internal commands from PATH
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:] if proc.stderr else "external command failed")


def ensure_local_cookbook(db: Session, user_id: int) -> Cookbook:
    cb = db.query(Cookbook).filter(Cookbook.owner_id==user_id, Cookbook.name=="Imported Recipes", Cookbook.store==Store.local).first()
    if not cb:
        cb = Cookbook(name="Imported Recipes", description="Auto-imported social recipes", store=Store.local, owner_id=user_id)
        db.add(cb)
        db.commit()
        db.refresh(cb)
    return cb


def _extract_metadata(url: str, workdir: Path) -> dict:
    """Extract metadata from the source URL using yt-dlp (without downloading).

    Returns title, description, uploader, and thumbnail list.
    """
    sanitized_url = _sanitize_media_url(url)
    opts = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(sanitized_url, download=False)
        return {
            "title": info.get("fulltitle") or info.get("title"),
            "description": info.get("description"),
            "uploader": info.get("uploader") or info.get("creator"),
            "thumbnails": info.get("thumbnails", []),
            "duration": info.get("duration"),
        }
    except Exception as exc:
        logging.warning("Metadata extraction failed for %s: %s", url, exc)
        return {"title": None, "description": None, "uploader": None, "thumbnails": [], "duration": None}


def _download_thumbnail(meta: dict, workdir: Path) -> Path | None:
    """Download the best thumbnail image for OCR from yt-dlp metadata."""
    thumbnails = meta.get("thumbnails", [])
    if not thumbnails:
        return None

    # Try thumbnails in order of preference (prefer higher resolution)
    thumbs = sorted(thumbnails, key=lambda t: t.get("width", 0), reverse=True)
    for thumb in thumbs:
        thumb_url = thumb.get("url")
        if not thumb_url:
            continue
        try:
            resp = requests.get(thumb_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if resp.status_code == 200:
                thumb_path = workdir / "thumbnail.jpg"
                thumb_path.write_bytes(resp.content)
                return thumb_path
        except Exception as exc:
            logging.debug("Thumbnail download failed: %s", exc)
            continue
    return None


def _download_media(url: str, workdir: Path) -> dict:
    sanitized_url = _sanitize_media_url(url)
    # Stage 1: Extract metadata (works even when video download is blocked)
    meta_opts = {"quiet": True, "noplaylist": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(meta_opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(sanitized_url, download=False)
    except Exception as exc:
        logging.warning("Metadata extraction failed for %s: %s", _log_safe(url), _log_safe(exc))
        return {"ok": False, "error": "Unable to extract metadata from URL (possible 404 or private content)"}

    # Store metadata for use by _extract_recipe_text_from_metadata
    meta = {
        "title": info.get("fulltitle") or info.get("title"),
        "description": info.get("description"),
        "uploader": info.get("uploader") or info.get("creator"),
        "thumbnails": info.get("thumbnails", []),
        "duration": info.get("duration"),
    }

    # Stage 2: Attempt to download the video file + subtitles
    # If download fails (e.g. TikTok blocks), we still return metadata
    # so text-based extraction can proceed.
    video = None
    audio = None
    subtitle_path = None
    download_opts = {
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "convertsubtitles": "srt",
        "quiet": True,
    }
    try:
        with yt_dlp.YoutubeDL(download_opts) as ydl:  # type: ignore[arg-type]
            ydl.extract_info(sanitized_url, download=True)
        files = list(workdir.iterdir())
        video = next((p for p in files if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}), None)
        audio = next((p for p in files if p.suffix.lower() in {".m4a", ".mp3", ".wav", ".aac"}), None)
        subs = sorted([p for p in files if p.suffix.lower() == ".srt"])
        subtitle_path = subs[0] if subs else None
    except Exception as exc:
        logging.warning("Video download failed for %s, falling back to metadata: %s", url, exc)

    if audio is None and video is not None:
        audio = workdir / "audio.wav"
        try:
            _run([
                "ffmpeg", "-y", "-i", str(video),
                "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(audio)
            ])
        except Exception as exc:
            logging.debug("Audio extraction failed (video may have no audio): %s", exc)
            audio = None

    if not subtitle_path and audio is not None:
        whisper_available = shutil.which("whisper")
        if whisper_available:
            try:
                _run([
                    whisper_available, str(audio),
                    "--model", os.getenv("WHISPER_MODEL", "base"),
                    "--language", "en",
                    "--output_format", "srt",
                    "--output_dir", str(workdir)
                ])
                subs = sorted([p for p in workdir.iterdir() if p.suffix.lower() == ".srt"])
                subtitle_path = subs[0] if subs else None
            except Exception as exc:
                logging.warning("whisper subtitle extraction failed: %s", exc)
    return {
        "video": video,
        "audio": audio,
        "subtitle": subtitle_path,
        "workdir": workdir,
        "metadata": meta,
    }


def _clean_srt_text(text: str) -> str:
    text = re.sub(r"\d+\n", "", text)
    text = re.sub(r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def _normalize_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r'^\s*[-•*]\s*', '', line)
    line = re.sub(r'^\d+\.\s*', '', line)
    return line.strip()


def _is_ingredient_like(line: str) -> bool:
    if not line or len(line) > 220:
        return False
    lower = line.lower()
    if any(lower.startswith(k) for k in ["step ", "instruction", "first", "next", "then", "now", "after"]):
        return False
    # Must contain at least some alphabetic characters (not just numbers/symbols)
    if not re.search(r'[a-z]', lower):
        return False
    # Check for numbers (quantities like "1", "1/2", "2.5")
    if re.search(r'\b\d+(\.\d+)?\b', line):
        # An ingredient line with a number should also have recognizable cooking words
        # to avoid matching OCR noise like "ial 4" or "3 lay So"
        words = set(re.findall(r'[a-zA-Z]+', line.lower()))
        if words & _INGREDIENT_WORD_BANK:
            return True
        # Or at least have multiple alphabetic words (likely real text with a quantity)
        alpha_words = [w for w in re.findall(r'[a-zA-Z]{3,}', lower)]
        if len(alpha_words) >= 2:
            return True
        return False
    # Check for unit keywords with word boundaries (prevents 'l' matching 'all')
    units = ['cup', 'tbsp', 'tsp', 'oz', 'gram', 'kg', 'ml', 'pound', 'lb',
             'pinch', 'dash', 'clove', 'slice', 'piece', 'can', 'bunch',
             'sprig', 'tablespoon', 'teaspoon', 'gallon', 'quart', 'pint',
             'package', 'bottle', 'jar', 'stick', 'sheet', 'scoop']
    for unit in units:
        if re.search(r'\b' + re.escape(unit) + r'\b', lower):
            return True
    # Also check for cooking ingredient words without explicit units
    words = set(re.findall(r'[a-zA-Z]+', lower))
    if words & _INGREDIENT_WORD_BANK:
        # Has at least one recognizable cooking word
        return True
    return False


_INGREDIENT_WORD_BANK = {
    "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon",
    "teaspoons", "oz", "ounce", "ounces", "lb", "pound", "pounds", "gram",
    "grams", "kg", "mg", "g", "ml", "l", "liter", "liters", "quart",
    "pint", "gallon", "pinch", "dash", "clove", "cloves", "slice", "slices",
    "piece", "pieces", "can", "cans", "bunch", "bunches", "sprig", "sprigs",
    "package", "bottle", "jar", "stick", "sticks", "sheet", "scoop",
    "salt", "pepper", "garlic", "onion", "butter", "oil", "water", "milk",
    "egg", "eggs", "flour", "sugar", "brown", "baking", "soda", "powder",
    "vanilla", "mix", "corn", "cheese", "cheddar", "mozzarella",
    "casserole", "muffin", "bread", "bun", "buns", "sauce", "ketchup",
    "mustard", "mayo", "mayonnaise", "honey", "syrup", "melted", "shredded",
    "grated", "diced", "chopped", "minced", "sliced", "crushed", "ground",
    "whole", "large", "small", "medium", "fresh", "frozen", "canned",
    "sweet", "hot", "spicy", "mild", "seasoned", "salted", "unsalted",
    "heavy", "whipping", "half", "half-and-half", "buttermilk", "yogurt",
    "sour", "cream", "ricotta", "parmesan", "feta", "swiss", "provolone",
    "jalapeno", "jalapeño", "serrano", "habanero", "cilantro", "parsley",
    "basil", "oregano", "thyme", "rosemary", "paprika", "cumin", "chili",
    "cinnamon", "nutmeg", "ginger", "cardamom",
}

CookING_VERBS = {
    "add", "mix", "stir", "chop", "dice", "slice", "heat", "cook", "boil",
    "simmer", "fry", "bake", "roast", "grill", "whisk", "pour", "spread",
    "layer", "top", "sprinkle", "season", "salt", "pepper", "serve", "let",
    "cool", "wait", "press", "knead", "roll", "fold", "beat", "melt",
    "warm", "combine", "dip", "coat", "drizzle", "garnish", "remove",
    "transfer", "place", "into", "over", "until", "then", "next",
    "first", "step", "pan", "skillet", "saucepan", "bowl", "oven", "microwave", "air",
    "fryer", "dish", "plate", "tray", "rack", "pot",
    "minutes", "minute", "hours", "hour",
    "golden", "brown", "bubbling", "soft", "firm", "crispy",
    "preheat", "shred", "dump", "cover", "uncover", "stirring",
    "cooking", "ready", "eat",
    "taste", "adjust", "optional", "recommended", "use",
    "cut", "crush", "mince", "grate", "peel", "blanch", "steam", "broil",
    "toast", "sift", "measure", "grease", "line", "brush", "rub", "marinate",
    "bubble", "reduce", "wilt", "tenderize", "deglaze",
}

_UI_NOISE_FILTERS = [
    "for you", "foryou", "fyp", "tiktok",
    "likes", "comments", "share", "save", "follow", "following", "followers", "views",
    "subscribe", "hit the bell", "bell icon",
    "swipe up", "link in bio", "check the description",
    "comment below", "let me know", "enjoy", "happy cooking",
    "full recipe", "recipe card",
    "cook with me", "no bake", "cooking tips", "kitchen hack",
    "step by step",
    "creator", "post", "report", "not interested",
    "show this thread", "embed", "copy link",
    "duet", "stitch", "react",
    "verified", "pro", "badge",
]


def _is_ui_noise(line: str) -> bool:
    """Filter out common TikTok UI overlay text and OCR artifacts."""
    lower = line.lower().strip()
    if not lower or len(lower) < 3:
        return True
    # Filter out single characters and very short fragments
    if len(lower.replace(" ", "")) < 3:
        return True
    # Filter out common TikTok UI noise
    if any(noise in lower for noise in _UI_NOISE_FILTERS):
        return True
    # Filter out strings that are mostly symbols/special chars
    alphanumeric_ratio = sum(c.isalnum() for c in lower) / max(len(lower), 1)
    if alphanumeric_ratio < 0.5:
        return True
    # Filter out lines that are just numbers or very short fragments
    if re.match(r'^[\d\s\-\.\,\']+$', lower) and len(lower.replace(" ", "")) < 5:
        return True
    # Filter out common OCR artifacts (strings with weird capitalization patterns)
    # e.g., "ial 4", "3 lay So", "nA," — lines that are mostly lowercase with
    # random uppercase letters scattered in, or very short non-dictionary words
    words = lower.split()
    real_words = 0
    for orig_word in words:
        word = re.sub(r'[^a-z]', '', orig_word)
        if len(word) >= 3 and word in _INGREDIENT_WORD_BANK:
            real_words += 1
    # If no recognizable cooking words and the line looks like garbage, filter it
    if real_words == 0 and len(words) <= 3 and any(len(re.sub(r'[^a-z]', '', w)) <= 2 for w in words):
        return True
    return False


def _preprocess_image_for_ocr(img, upscale: int = 2) -> object:
    """Apply preprocessing to improve OCR accuracy on video frames/thumbnails.

    - Upscales the image 2-3x for better OCR on small text
    - Converts to grayscale
    - Increases contrast
    - Applies sharpening
    """
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    # Upscale for better OCR on small text
    if upscale > 1:
        img = img.resize((img.width * upscale, img.height * upscale), Image.Resampling.LANCZOS)
    # Convert to grayscale
    img = img.convert('L')
    # Increase contrast
    img = ImageOps.autocontrast(img)
    # Boost contrast further
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.5)
    # Apply sharpening
    img = img.filter(ImageFilter.UnsharpMask(radius=1, percent=150, threshold=3))
    return img


def _extract_text_from_video_frames(video_path: Path, workdir: Path, max_frames: int = 30) -> str:
    """Extract text from video frames using ffmpeg + pytesseract OCR.

    Samples up to ``max_frames`` evenly-spaced frames from the video and
    runs OCR on each.  Best combined with ``_extract_recipe_from_text``
    since OCR output is noisy for structured parsing.
    """
    frame_dir = workdir / "frames"
    frame_dir.mkdir(exist_ok=True)

    # Get video duration
    try:
        dur_proc = subprocess.run(  # nosec B603,B607 - ffprobe from PATH, trusted command on admin-only endpoint
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(dur_proc.stdout.strip()) if dur_proc.returncode == 0 else 0
    except Exception:
        duration = 0

    if duration <= 0:
        # Single frame fallback
        frame_times = [0]
    # For short videos (<=60s), sample densely in the first ~20s
    # where ingredients/instructions text is typically shown
    elif duration <= 30:
        max_frames = min(max_frames, 12)
        frame_times = [duration * i / max_frames for i in range(max_frames)]
    elif duration <= 60:
        # Dense sampling in first 15s, sparse after
        first_chunk = 8
        second_chunk = max(max_frames - first_chunk, 4)
        frame_times = [15 * i / first_chunk for i in range(first_chunk)]
        frame_times += [15 + (duration - 15) * i / second_chunk for i in range(second_chunk)]
    else:
        # For long videos (>60s), focus on first 20 seconds (ingredient lists)
        max_frames = min(max_frames, 15)
        frame_times = [20 * i / max_frames for i in range(max_frames)]

    extracted_lines: list[str] = []
    seen_lines: set[str] = set()
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return ""

    for i, t in enumerate(frame_times):
        frame_path = frame_dir / f"frame_{i:03d}.png"
        try:
            _run([
                "ffmpeg", "-y", "-ss", str(t), "-i", str(video_path),
                "-frames:v", "1", "-q:v", "2",
                "-vf", "scale=iw*2:ih*2:flags=lanczos",
                str(frame_path),
            ])
        except Exception as exc:
            logging.debug("Frame extraction failed at t=%.1f: %s", t, exc)
            continue
        if not frame_path.exists():
            continue
        try:
            img = Image.open(frame_path)
            # Crop to center area where text is typically overlaid on TikTok
            w, h = img.size
            img = img.crop((int(w * 0.05), int(h * 0.1), int(w * 0.95), int(h * 0.9)))
            # Don't upscale video frames (1080x1920 already has large enough text)
            # Only upscale thumbnails which are small
            img = _preprocess_image_for_ocr(img, upscale=1)
            # Try PSM 11 (sparse text) which works best for scattered overlays
            text = pytesseract.image_to_string(img, lang="eng", config="--psm 11")
            all_text = text
            for orig_line in all_text.splitlines():
                line = orig_line.strip()
                if not line:
                    continue
                # Deduplicate: skip near-identical lines already seen
                normalized = re.sub(r'\s+', ' ', line).lower()
                if normalized in seen_lines:
                    continue
                # Filter out UI noise
                if _is_ui_noise(line):
                    seen_lines.add(normalized)
                    continue
                seen_lines.add(normalized)
                extracted_lines.append(line)
        except Exception as exc:
            logging.warning("OCR failed for frame %d: %s", i, exc)

    return "\n".join(extracted_lines)


def _extract_from_web_page(url: str) -> dict:
    """Attempt to fetch the source webpage and extract recipe text.

    Many TikTok cooking videos link to a blog post with the full recipe.
    This fetches the page HTML and looks for recipe schema markup
    (JSON-LD ``application/ld+json``) or structured recipe content.
    """
    sanitized_url = _sanitize_media_url(url)
    try:
        resp = requests.get(sanitized_url, headers={"User-Agent": "Mozilla/5.0 (compatible; RecipeBot/1.0)"}, timeout=15, allow_redirects=True)
        html = resp.text[:200000]
    except Exception as exc:
        logging.warning("Web page fetch failed for %s: %s", url, exc)
        return {"title": None, "ingredients": None, "instructions": None}

    # Try JSON-LD recipe schema
    schema_pattern = re.compile(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )
    for match in schema_pattern.finditer(html):
        try:
            data = json.loads(match.group(1))
            # Handle both single dict and list of dicts
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "Recipe":
                    title = item.get("name") or item.get("headline")
                    ingredients = item.get("recipeIngredient")
                    instructions_raw = item.get("recipeInstructions")
                    if isinstance(instructions_raw, list):
                        instructions = "\n".join(
                            instr.get("text") or instr.get("instruction", "") or str(instr)
                            for instr in instructions_raw
                        )
                    else:
                        instructions = instructions_raw
                    if title or ingredients or instructions:
                        return {
                            "title": title,
                            "ingredients": "\n".join(ingredients) if ingredients else None,
                            "instructions": instructions if instructions else None,
                        }
        except Exception as exc:
            logging.debug("Web page extraction step failed: %s", exc)
            continue

    # Fallback: look for meta description and recipe-like text
    title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    title = title_match.group(1).strip() if title_match else None

    return {"title": title, "ingredients": None, "instructions": None}


def _fetch_html(url: str) -> str | None:
    """Fetch HTML content from a URL with proper error handling."""
    try:
        sanitized = _sanitize_media_url(url)
        resp = requests.get(sanitized, headers={"User-Agent": "Mozilla/5.0 (compatible; RecipeBot/1.0)"}, timeout=15)
        resp.raise_for_status()
        return resp.text[:200000]
    except Exception:
        return None


def _find_recipe_in_rss(rss_html: str, blog_url: str, meta: dict) -> str | None:
    """Parse RSS feed and find the most likely matching recipe link.

    Looks for recipe links in the RSS feed that match the TikTok title keywords.
    Returns the first matching recipe permalink, or None if no match found.
    """
    if not rss_html:
        return None

    # Extract title keywords from TikTok metadata
    raw_title = meta.get("title", "") or meta.get("description", "") or ""
    title_clean = re.sub(r"[^a-z0-9\s]", " ", raw_title).strip().lower()
    title_words = set(re.findall(r"[a-z]{3,}", title_clean))

    # Parse RSS feed for recipe links
    link_pattern = re.compile(r'<link[^>]*>(.*?)</link>', re.IGNORECASE | re.DOTALL)
    title_pattern = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
    item_pattern = re.compile(r'<item>(.*?)</item>', re.IGNORECASE | re.DOTALL)

    best_match = None
    best_score = 0

    for item in item_pattern.finditer(rss_html):
        item_html = item.group(1)
        link_match = link_pattern.search(item_html)
        title_match = title_pattern.search(item_html)

        if not link_match or not title_match:
            continue

        link = link_match.group(1).strip()
        title = title_match.group(1).strip()

        # Must be on the same blog domain
        if not link.startswith(blog_url):
            continue

        # Skip non-recipe pages
        if any(skip in link.lower() for skip in ["feed", "rss", "xml", "author", "tag", "category", "page"]):
            continue

        # Score this link by title similarity
        item_words = set(re.findall(r"[a-z]{3,}", title.lower()))
        overlap = len(title_words & item_words)
        if overlap > best_score:
            best_score = overlap
            best_match = link

    return best_match if best_score >= 2 else None


def _find_blog_recipe_link(blog_url: str, search_terms: str, search_page_url: str) -> str | None:
    """Find the actual recipe page URL from a blog search results page.

    Fetches the search results page and looks for links to individual recipe
    posts (excluding the search page itself, RSS feeds, and non-recipe pages).
    """
    sanitized = _sanitize_media_url(search_page_url)
    try:
        resp = requests.get(sanitized, headers={"User-Agent": "Mozilla/5.0 (compatible; RecipeBot/1.0)"}, timeout=15)
        html = resp.text[:100000]
    except Exception:
        return None

    # Find all links that look like recipe pages
    link_pattern = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
    for match in link_pattern.finditer(html):
        link = match.group(1)
        if link.startswith("http"):
            parsed_url = urlparse(link)
            # Must be on the same blog domain
            if parsed_url.netloc != urlparse(blog_url).netloc:
                continue
            # Skip RSS feeds, search pages, etc.
            if "feed" in link or "/search/" in link or "?s=" in link:
                continue
            # Must look like a recipe post (has recipe-like URL)
            path = parsed_url.path.lower()
            if any(kw in path for kw in ["recipe", "casserole", "chicken", "pasta"]):
                return link
    return None


def _clean_ocr_text(text: str) -> str:
    """Clean up OCR output for better parsing.

    Fixes common OCR errors:
    - Replaces ' with space (e.g., \"bit'of\" -> \"bit of\")
    - Replaces ; with , (common OCR misread)
    - Removes excessive punctuation/spacing
    - Normalizes whitespace
    """
    lines = []
    for orig_line in text.splitlines():
        line = orig_line.strip()
        if not line:
            continue
        # Fix common OCR artifacts
        # ' at word boundaries -> space
        line = re.sub(r"(?<=\w)'(?=\w)", " ", line)
        # ; -> ,
        line = line.replace(";", ",")
        # Fix common OCR letter/number confusions
        line = re.sub(r'\b0\b', 'o', line)  # 0 -> o (when standalone)
        # Normalize whitespace
        line = re.sub(r'\s+', ' ', line)
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _is_recipe_step(s: str) -> bool:
    """Check if a sentence looks like a real cooking instruction step."""
    lower = s.lower()
    words = set(re.findall(r'[a-z]+', lower))
    has_cooking_word = bool(words & CookING_VERBS)
    has_enough_length = len(s) >= 8 and len(re.findall(r'[a-z]', s)) >= 5
    return has_cooking_word and has_enough_length


def _add_recipe_steps(instructions: list, steps: list[str]) -> None:
    """Add recipe steps that pass the cooking-verb filter."""
    for s in steps:
        if _is_recipe_step(s):
            instructions.append(s)


def _split_recipe_paragraph(text: str) -> list[str]:
    """Split a long recipe paragraph into logical steps by sentence boundaries."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    steps = []
    for orig_s in sentences:
        s = orig_s.strip()
        if len(s) >= 8:
            steps.append(s)
    return steps if steps else [text]


def _extract_recipe_from_text(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {"title": None, "ingredients": None, "instructions": None}

    title = lines[0]
    normalized = [_normalize_line(line) for line in lines[1:] if _normalize_line(line)]
    ingredients: list[str] = []
    instructions: list[str] = []
    section = "ingredients"

    INSTRUCTION_HEADERS = ("instruction", "step", "how to", "directions", "method", "recipe:")
    INGREDIENT_HEADERS = ("what you need", "you'll need", "ingredients:", "ingredient:")

    for line in normalized:
        lower = line.lower()
        if lower.startswith(INSTRUCTION_HEADERS):
            section = "instructions"
            continue
        if lower.startswith(INGREDIENT_HEADERS):
            section = "ingredients"
            continue
        if section == "ingredients":
            if _is_ingredient_like(line):
                ingredients.append(line)
            elif len(ingredients) >= 2 and not lower.startswith(INGREDIENT_HEADERS):
                section = "instructions"
                if len(line) > 4:
                    instructions.append(line)
            elif len(line) > 50 and section == "ingredients":
                # Long line with no ingredient markers — likely a paragraph
                # of instructions. Switch to instruction section and split.
                section = "instructions"
                _add_recipe_steps(instructions, _split_recipe_paragraph(line))
        elif section == "instructions":
            # Only treat as instruction if it looks like a real cooking instruction
            # (skip OCR noise fragments that don't contain cooking verbs or recipe terms)
            lower = line.lower()
            words = set(re.findall(r'[a-z]+', lower))
            has_cooking_word = bool(words & CookING_VERBS)
            has_enough_length = len(line) >= 8 and len(re.findall(r'[a-z]', line)) >= 5
            if has_cooking_word and has_enough_length:
                # Split long instruction lines into individual steps
                if len(line) > 50:
                    _add_recipe_steps(instructions, _split_recipe_paragraph(line))
                else:
                    instructions.append(line)

            # If we have instructions but no ingredients, try to extract
            # ingredient mentions from the instruction text (e.g. "Add 1 jar of
            # pasta sauce, 1 jar of Alfredo sauce, and 3 chicken breasts")
            if instructions and not ingredients:
                all_text = " ".join(instructions)
                # Look for quantity + measurement patterns
                ingredient_phrase_re = re.compile(
                    r'(\d+[\u2044/\u2044.\u00bc\u00bd\u00be]?(?:\s*(?:cup|tbsp|tsp|tablespoon|teaspoon|oz|lb|pound|kg|ml|l|jar|can|bunch|stick|package|pkg|head|clove|slice|pieces?|whole))\b[^\n.,;]*(?:,|\u2026|and|$))',
                    re.IGNORECASE
                )
                for match in ingredient_phrase_re.finditer(all_text):
                    phrase = match.group(1).strip().rstrip(',.')
                    if phrase and len(phrase) > 5:
                        ingredients.append(phrase)

    return {
        "title": title,
        "ingredients": "\n".join(ingredients) if ingredients else None,
        "instructions": "\n".join(instructions) if instructions else None,
    }


def _extract_from_transcript(transcript_path: Path | None, fallback_audio: Path | None, workdir: Path, video_path: Path | None = None) -> dict:
    if transcript_path and transcript_path.exists():
        raw_text = _clean_srt_text(transcript_path.read_text(errors="ignore") or "")
        parsed = _extract_recipe_from_text(raw_text)
        if parsed.get("ingredients") or parsed.get("instructions"):
            return parsed

    if fallback_audio is not None and fallback_audio.exists():
        whisper_available = shutil.which("whisper")
        if whisper_available:
            try:
                _run([
                    whisper_available, str(fallback_audio),
                    "--model", os.getenv("WHISPER_MODEL", "base"),
                    "--language", "en",
                    "--output_format", "srt",
                    "--output_dir", str(workdir),
                ])
                subs = sorted([p for p in workdir.iterdir() if p.suffix.lower() == ".srt"])
                if subs:
                    raw_text = _clean_srt_text(subs[0].read_text(errors="ignore") or "")
                    parsed = _extract_recipe_from_text(raw_text)
                    if parsed.get("ingredients") or parsed.get("instructions"):
                        return parsed
            except Exception as exc:
                logging.debug("Subtitle-based extraction failed: %s", exc)

    # Fallback: OCR on video frames
    if video_path and video_path.exists():
        logging.info("Falling back to OCR on video frames for %s", video_path)
        ocr_text = _extract_text_from_video_frames(video_path, workdir, max_frames=30)
        if ocr_text:
            ocr_text = _clean_ocr_text(ocr_text)
            parsed = _extract_recipe_from_text(ocr_text)
            if parsed.get("ingredients") or parsed.get("instructions"):
                return parsed

    return {"title": None, "ingredients": None, "instructions": None}


def download_media(url: str, user_id: int) -> dict:
    workdir = MEDIA_ROOT / "raw" / f"{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    result = _download_media(url, workdir)
    if not result.get("ok") and "error" in result:
        return result

    subtitle_path = result.get("subtitle")
    description = str(subtitle_path) if subtitle_path else None
    meta = result.get("metadata", {})
    title = meta.get("title") or workdir.name
    db = SessionLocal()
    db.expire_on_commit = False
    try:
        cookbook = ensure_local_cookbook(db, user_id)
        recipe = Recipe(
            title=title,
            source_url=url,
            source_path=str(result.get("video") or result.get("audio") or ""),
            store=Store.local,
            owner_id=user_id,
            cookbook_id=cookbook.id,
            description=description,
        )
        db.add(recipe)
        db.commit()
        db.refresh(recipe)
    finally:
        db.close()
    return {
        "ok": True,
        "recipe_id": recipe.id,
        "video": str(result.get("video")) if result.get("video") else None,
        "audio": str(result.get("audio")) if result.get("audio") else None,
        "subtitles": str(subtitle_path) if subtitle_path else None,
        "cookbook_id": cookbook.id,
    }


def _clean_facebook_title(title: str, description: str = "") -> str:
    """Extract a clean recipe title from a Facebook Reel title/description.

    Facebook titles look like:
    "124K views · 80K reactions | Onion Crunch Chicken 🧅🤎🍯 My new favorite..."
    We strip view/reaction prefixes and hashtag/emoji suffixes to get just the recipe name.
    """
    if not title:
        return title
    # Try to find a clean title after the pipe separator
    if "|" in title:
        parts = title.split("|", 1)
        candidate = parts[1].strip()
        if candidate:
            title = candidate
    # Strip view/reaction prefixes like "124K views · 80K reactions · "
    title = re.sub(r"^\d+[KM]?\s*(views?|reactions?|likes?|shares?)\s*[·|]\s*", "", title, flags=re.IGNORECASE)
    # Strip trailing hashtags and ellipsis
    title = re.sub(r"\s*#.*$", "", title)
    title = re.sub(r"\s*\.{2,}.*$", "", title)
    # Stop at doubled exclamation/question marks (common in Facebook posts)
    title = re.split(r"!!|\?\?|!{3,}|\?{3,}", title)[0]
    # Strip trailing punctuation/whitespace
    title = title.rstrip("!?. \t")
    # Truncate at first emoji sequence + text (keep the emoji title part)
    # Find where the recipe name ends (before description text)
    # Look for pattern like "Recipe Name!! So much flavor" - cut at double exclamation
    return title[:200].strip()


def _extract_recipe_text_from_metadata(url: str, workdir: Path, result: dict) -> dict:
    """Multi-stage recipe extraction from a video URL.

    Tries these sources in order:
    1. Thumbnail OCR (TikTok thumbnails often have recipe text)
    2. Subtitles (auto-generated or uploaded)
    3. Whisper speech-to-text on audio track
    4. OCR on video frames
    5. Web page JSON-LD recipe schema

    The yt-dlp metadata title is always preferred as the recipe title
    since it is clean and reliable (unlike OCR which produces garbled text
    as the first line).
    """
    # Extract yt-dlp metadata (title, description, thumbnails)
    meta = result.get("metadata", {})
    if not meta:
        meta = _extract_metadata(url, workdir)

    # Download and OCR the thumbnail image — often has recipe text overlay
    thumbnail_ocr_text = ""
    thumbnail_path = _download_thumbnail(meta, workdir)
    if thumbnail_path and thumbnail_path.exists():
        try:
            from PIL import Image  # noqa: PLC0415
            img = _preprocess_image_for_ocr(Image.open(thumbnail_path))
            thumbnail_ocr_text = _ocr_image(img)
            logging.info("Thumbnail OCR: %d chars extracted", len(thumbnail_ocr_text))
        except Exception as exc:
            logging.warning("Thumbnail OCR failed: %s", exc)

    if thumbnail_ocr_text:
        thumbnail_ocr_text = _clean_ocr_text(thumbnail_ocr_text)
        parsed = _extract_recipe_from_text(thumbnail_ocr_text)
        if parsed.get("ingredients") or parsed.get("instructions"):
            title = _clean_facebook_title(meta.get("title", ""), meta.get("description", "")) or parsed.get("title")
            return {
                "title": title,
                "ingredients": parsed.get("ingredients"),
                "instructions": parsed.get("instructions"),
            }

    # Stage 1b: Try extracting from yt-dlp metadata description
    # (TikTok descriptions often contain the full recipe as text)
    meta_desc = meta.get("description", "")
    if meta_desc and meta_desc.strip():
        logging.info("Trying metadata description for %s (%d chars)", url, len(meta_desc))
        desc_parsed = _extract_recipe_from_text(meta_desc)
        if desc_parsed.get("ingredients") or desc_parsed.get("instructions"):
            title = _clean_facebook_title(meta.get("title", ""), meta_desc) or desc_parsed.get("title")
            return {
                "title": title,
                "ingredients": desc_parsed.get("ingredients"),
                "instructions": desc_parsed.get("instructions"),
            }

    video_path = result.get("video")
    # Stage 2-4: subtitles -> Whisper -> video frame OCR
    parsed = _extract_from_transcript(result.get("subtitle"), result.get("audio"), workdir, video_path)
    ocr_ingredients = parsed.get("ingredients")
    ocr_instructions = parsed.get("instructions")
    if ocr_ingredients or ocr_instructions:
        ocr_title = _clean_facebook_title(meta.get("title", ""), meta.get("description", "")) or parsed.get("title")

    # Stage 5: Try to find the recipe on the creator's blog (for TikTok)
    # The creator's blog often has the full recipe as text, which is
    # more reliable than OCR on stylized TikTok text overlays.
    uploader = meta.get("uploader", "")
    if uploader:
        blog_candidates = [
            f"https://{uploader}.com",
            f"https://www.{uploader}.com",
        ]
        for blog_url in blog_candidates:
            try:
                # Try RSS feed first — most reliable way to find recipe posts
                rss_url = f"{blog_url}/feed/rss2/"
                logging.info("Checking RSS feed for %s", rss_url)
                rss_html = _fetch_html(rss_url)
                if rss_html:
                    recipe_link = _find_recipe_in_rss(rss_html, blog_url, meta)
                    if recipe_link:
                        logging.info("Found matching recipe in RSS: %s", recipe_link)
                        recipe_parsed = _extract_from_web_page(recipe_link)
                        if recipe_parsed.get("ingredients") and recipe_parsed.get("instructions"):
                            title = recipe_parsed.get("title") or _clean_facebook_title(meta.get("title", ""), meta.get("description", ""))
                            return {
                                "title": title,
                                "ingredients": recipe_parsed.get("ingredients"),
                                "instructions": recipe_parsed.get("instructions"),
                            }
                # Fallback: search URL
                search_terms = re.sub(r'[^a-z0-9\s]', ' ', (meta.get("title") or "")).strip().lower()
                search_terms = ' '.join(search_terms.split()[:5])
                if search_terms:
                    search_url = f"{blog_url}/?s={url_quote(search_terms)}"
                    logging.info("Searching blog %s for '%s'", blog_url, search_terms)
                    blog_parsed = _extract_from_web_page(search_url)
                    if blog_parsed.get("ingredients") and blog_parsed.get("instructions"):
                        recipe_link = _find_blog_recipe_link(blog_url, search_terms, search_url)
                        if recipe_link:
                            logging.info("Found recipe link: %s", recipe_link)
                            recipe_parsed = _extract_from_web_page(recipe_link)
                            if recipe_parsed.get("ingredients") and recipe_parsed.get("instructions"):
                                title = _clean_facebook_title(meta.get("title", ""), meta.get("description", "")) or recipe_parsed.get("title")
                                return {
                                    "title": title,
                                    "ingredients": recipe_parsed.get("ingredients"),
                                    "instructions": recipe_parsed.get("instructions"),
                                }
                # Also try the blog homepage directly
                blog_parsed = _extract_from_web_page(blog_url)
                if blog_parsed.get("ingredients") and blog_parsed.get("instructions"):
                    title = _clean_facebook_title(meta.get("title", ""), meta.get("description", "")) or blog_parsed.get("title")
                    return {
                        "title": title,
                        "ingredients": blog_parsed.get("ingredients"),
                        "instructions": blog_parsed.get("instructions"),
                    }
            except Exception as exc:
                logging.debug("Blog search failed for %s: %s", blog_url, exc)
                continue

    # If OCR produced results, use them (even if imperfect)
    if ocr_ingredients or ocr_instructions:
        return {
            "title": ocr_title,
            "ingredients": ocr_ingredients,
            "instructions": ocr_instructions,
        }

    # Stage 6: Web page JSON-LD schema (fallback from original source URL)
    logging.info("Video extraction yielded no recipe data, trying web page for %s", url)
    web_parsed = _extract_from_web_page(url)
    if web_parsed.get("ingredients") or web_parsed.get("instructions"):
        title = _clean_facebook_title(meta.get("title", ""), meta.get("description", "")) or web_parsed.get("title")
        return {
            "title": title,
            "ingredients": web_parsed.get("ingredients"),
            "instructions": web_parsed.get("instructions"),
        }

    # Last resort: use metadata title only
    if meta.get("title"):
        return {
            "title": _clean_facebook_title(meta.get("title", ""), meta.get("description", "")),
            "ingredients": None,
            "instructions": None,
        }

    return {"title": None, "ingredients": None, "instructions": None}


def _ocr_image(img) -> str:
    """Run pytesseract OCR on a preprocessed PIL image."""
    import pytesseract  # noqa: PLC0415  # nosec B410 - pytesseract is safe, no eval
    text = pytesseract.image_to_string(img, lang="eng")
    lines = []
    for orig_line in text.splitlines():
        line = orig_line.strip()
        if not line:
            continue
        if _is_ui_noise(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def extract_recipe_from_url(url: str, user_id: int) -> dict:
    workdir = MEDIA_ROOT / "raw" / f"{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    result = _download_media(url, workdir)
    if not result.get("ok") and "error" in result:
        return result

    parsed = _extract_recipe_text_from_metadata(url, workdir, result)

    db = SessionLocal()
    db.expire_on_commit = False
    try:
        cookbook = ensure_local_cookbook(db, user_id)
        recipe = Recipe(
            title=(parsed.get("title") or workdir.name)[:255],
            description=None,
            ingredients=parsed.get("ingredients"),
            instructions=parsed.get("instructions"),
            source_url=url,
            source_path=str(result.get("video") or result.get("audio") or ""),
            store=Store.local,
            owner_id=user_id,
            cookbook_id=cookbook.id,
        )
        db.add(recipe)
        db.commit()
        db.refresh(recipe)
    finally:
        db.close()
    return {
        "ok": True,
        "recipe_id": recipe.id,
        "title": recipe.title,
        "ingredients": recipe.ingredients,
        "instructions": recipe.instructions,
        "source_url": url,
        "cookbook_id": cookbook.id,
    }


def ingest_upload(file, user_id: int) -> dict:
    suffix = Path(file.filename or "upload").suffix.lower()
    workdir = MEDIA_ROOT / "raw" / f"{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / f"upload{suffix}"
    with dest.open("wb") as f:
        f.write(file.file.read())

    video = dest if suffix in {".mp4", ".mkv", ".webm", ".mov"} else None
    audio = dest if suffix in {".m4a", ".mp3", ".wav", ".aac"} else None
    if video:
        audio = workdir / "audio.wav"
        _run([
            "ffmpeg", "-y", "-i", str(video),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(audio)
        ])
    if not video and not audio:
        return {"ok": False, "error": "Unsupported file type"}

    subs = []
    if audio is not None:
        whisper_available = shutil.which("whisper")
        if whisper_available:
            _run([
                whisper_available, str(audio),
                "--model", os.getenv("WHISPER_MODEL", "tiny"),
                "--language", "en",
                "--output_format", "srt",
                "--output_dir", str(workdir)
            ])
            subs = sorted([p for p in workdir.iterdir() if p.suffix.lower() == ".srt"])
        else:
            subs = []  # whisper not installed, skip transcription

    subtitle_path = subs[0] if subs else None
    db = SessionLocal()
    try:
        cookbook = ensure_local_cookbook(db, user_id)
        recipe = Recipe(
            title=workdir.name,
            source_path=str(video or audio),
            store=Store.local,
            owner_id=user_id,
            cookbook_id=cookbook.id,
            description=str(subtitle_path) if subtitle_path else None,
        )
        db.add(recipe)
        db.commit()
        db.refresh(recipe)
    finally:
        db.close()
    return {
        "ok": True,
        "recipe_id": recipe.id,
        "video": str(video) if video else None,
        "audio": str(audio) if audio else None,
        "subtitles": str(subtitle_path) if subtitle_path else None,
        "cookbook_id": cookbook.id,
    }


def extract_recipe_from_upload(file, user_id: int) -> dict:
    suffix = Path(file.filename or "upload").suffix.lower()
    workdir = MEDIA_ROOT / "raw" / f"{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / f"upload{suffix}"
    with dest.open("wb") as f:
        f.write(file.file.read())

    video = dest if suffix in {".mp4", ".mkv", ".webm", ".mov"} else None
    audio = dest if suffix in {".m4a", ".mp3", ".wav", ".aac"} else None
    # Handle document/image files: extract text and create recipe from content
    doc_suffixes = {".txt", ".md", ".pdf", ".doc", ".docx", ".csv"}
    image_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
    if suffix in doc_suffixes or suffix in image_suffixes:
        try:
            extracted = extract_text_from_file(str(dest), file.filename or "upload")
        except Exception:
            extracted = ""
        parsed = _extract_recipe_from_text(extracted)
        db = SessionLocal()
        try:
            cookbook = ensure_local_cookbook(db, user_id)
            cookbook_id = cookbook.id
            recipe = Recipe(
                title=parsed.get("title") or Path(file.filename).stem,
                description=parsed.get("description") or "",
                ingredients=parsed.get("ingredients"),
                instructions=parsed.get("instructions"),
                source_path=str(dest),
                source_filename=file.filename or "",
                store=Store.local,
                owner_id=user_id,
            )
            db.add(recipe)
            db.commit()
            db.refresh(recipe)
        finally:
            db.close()
        return {
            "ok": True,
            "recipe_id": recipe.id,
            "title": recipe.title,
            "ingredients": recipe.ingredients,
            "instructions": recipe.instructions,
            "source_path": str(dest),
            "cookbook_id": cookbook_id,
        }
    if video:
        audio = workdir / "audio.wav"
        _run([
            "ffmpeg", "-y", "-i", str(video),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(audio)
        ])
    if not video and not audio:
        return {"ok": False, "error": "Unsupported file type"}

    parsed = _extract_from_transcript(None, audio, workdir, video)
    db = SessionLocal()
    try:
        cookbook = ensure_local_cookbook(db, user_id)
        recipe = Recipe(
            title=(parsed.get("title") or workdir.name)[:255],
            description=None,
            ingredients=parsed.get("ingredients"),
            instructions=parsed.get("instructions"),
            source_path=str(video or audio),
            store=Store.local,
            owner_id=user_id,
        )
        db.add(recipe)
        db.commit()
        db.refresh(recipe)
    finally:
        db.close()
    return {
        "ok": True,
        "recipe_id": recipe.id,
        "title": recipe.title,
        "ingredients": recipe.ingredients,
        "instructions": recipe.instructions,
        "source_path": str(video or audio),
        "cookbook_id": cookbook.id,
    }
