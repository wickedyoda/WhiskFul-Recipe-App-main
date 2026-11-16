"""Log file reading helpers for diagnostics.

Reads recent log lines from the application log file.
Uvicorn writes to stdout/stderr; in Docker we also capture to a file.
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

LOG_PATH = os.environ.get("LOG_PATH", "/media/app.log")
ALERT_EMAIL = "alerts@tyates.one"


def _log_file_path() -> str:
    return os.environ.get("LOG_PATH", "/media/app.log")


def read_recent_logs(hours: int = 2) -> str:
    """Read log lines from the last `hours` from the application log file."""
    log_path = Path(_log_file_path())
    if not log_path.exists():
        return "(No log file found at " + str(log_path) + ")"

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    lines: list[str] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                lines.append(line.rstrip())
    except Exception as exc:
        logging.getLogger(__name__).warning("Failed to read logs: %s", exc)
        return f"(Failed to read logs: {exc})"

    # Best-effort time filter on timestamps that look like ISO
    filtered: list[str] = []
    for line in lines:
        ts_part = line[:23] if len(line) > 23 else ""
        try:
            ts = datetime.strptime(
                ts_part.replace("T", " "), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=UTC)
            if ts >= cutoff:
                filtered.append(line)
        except ValueError:
            filtered.append(line)

    return "\n".join(filtered[-2000:]) if filtered else "(No recent log entries)"


def submit_logs_to_email() -> bool:
    """Email the last 2 hours of logs to alerts@tyates.one. Returns True on success."""
    from backend.services.email import send_email

    log_content = read_recent_logs(hours=2)
    now = datetime.now(UTC)
    subject = f"WhiskFul Diag Logs {now.strftime('%Y-%m-%d')}-{now.strftime('%H:%M')}"
    body = (
        f"<p>Recent application logs (last 2 hours):</p>"
        f"<pre style='font-family:monospace;font-size:12px;background:#f5f5f5;padding:12px;border-radius:4px;'>{log_content}</pre>"
    )
    return send_email(ALERT_EMAIL, subject, body)
