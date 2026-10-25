"""Wraps extract_invoice.extract + check_totals: upload validation, temp files and a per-IP rate limit."""
import logging
import os
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

log = logging.getLogger("crossbill.extraction")


class ExtractionError(Exception):
    """An error with an HTTP status and a message that is safe to show to the user."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def max_upload_bytes() -> int:
    return int(float(os.getenv("MAX_UPLOAD_MB", "5")) * 1024 * 1024)


def validate_pdf(data: bytes) -> None:
    """PDF only (checked by content, not by file name), not empty, within the size limit."""
    if not data:
        raise ExtractionError(400, "The file is empty.")
    if len(data) > max_upload_bytes():
        raise ExtractionError(413, f"The file is larger than {os.getenv('MAX_UPLOAD_MB', '5')} MB.")
    if not data.lstrip()[:5] == b"%PDF-":
        raise ExtractionError(415, "Only PDF invoices are supported.")


# ---------- rate limit (live AI calls only; protects API credits on the public demo) ----------
class RateLimiter:
    """Sliding one-hour window per client IP, kept in memory (one web process)."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, ip: str) -> None:
        limit = int(os.getenv("EXTRACT_RATE_LIMIT_PER_HOUR", "30"))
        now = time.monotonic()
        with self._lock:
            hits = self._hits[ip]
            while hits and now - hits[0] > 3600:
                hits.popleft()
            if len(hits) >= limit:
                wait_min = int((3600 - (now - hits[0])) // 60) + 1
                raise ExtractionError(429, f"Live AI extraction limit reached ({limit} per hour). "
                                           f"Try again in about {wait_min} min, or load a sample instead.")
            hits.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(headers, peer: str | None) -> str:
    """The proxy in front of the app (Render) appends the real client address as the LAST
    X-Forwarded-For entry; earlier entries are client-controlled and are ignored."""
    forwarded = headers.get("x-forwarded-for", "")
    last = forwarded.split(",")[-1].strip()
    return last or peer or "unknown"


# ---------- extraction ----------
def extract_pdf(data: bytes, ip: str) -> dict:
    """Send the PDF to Claude (facts only) and add arithmetic check warnings.
    Only requests that will really reach Claude count towards the rate limit."""
    validate_pdf(data)
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise ExtractionError(503, "Live AI extraction is not configured on this server (no API key). "
                                   "Load a sample to see the pre-extracted result.")
    limiter.check(ip)
    import extract_invoice  # imported lazily so the JSON-only demo needs no API key

    extract_invoice.MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        try:
            invoice = extract_invoice.extract(Path(tmp.name))
        except Exception as e:  # API/network errors: log the type only, never request details or keys
            log.warning("AI extraction failed: %s", type(e).__name__)
            raise ExtractionError(502, f"The AI could not read this invoice ({type(e).__name__}). Please try again.")
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    try:
        invoice["check_warnings"] = extract_invoice.check_totals(invoice)
    except (KeyError, TypeError) as e:
        raise ExtractionError(502, f"The AI returned an incomplete invoice (missing {e}). Please try again.")
    return invoice
