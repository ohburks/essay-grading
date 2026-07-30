"""Download a resolved source URL and extract plain text from it (HTML or PDF).

Never raises -- any network failure, oversize download, or unparseable body
returns None (or empty text), and the caller (pipeline.get_or_fetch_source)
records that as the source's own status rather than aborting the whole run.

Uses stdlib urllib (no requests/httpx dependency), matching core.llm's
existing outbound-HTTP convention. HTML parsing uses BeautifulSoup with the
stdlib "html.parser" backend, so lxml isn't a dependency either.
"""

import io
import re
import urllib.error
import urllib.request

from ... import config

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MIN_INDEXABLE_CHARS = 300  # below this, treat as an abstract-only/paywall page

_USER_AGENT = "assessment-platform-citations/0.1 (+https://github.com/ohburks/essay-grading)"
_READ_CHUNK = 65536


def _is_pdf(url: str, content_type: str, first_bytes: bytes) -> bool:
    if content_type and "pdf" in content_type.lower():
        return True
    if url.lower().split("?")[0].endswith(".pdf"):
        return True
    return first_bytes[:5] == b"%PDF-"


def extract_html_text(html_bytes: bytes) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""
    try:
        soup = BeautifulSoup(html_bytes, "html.parser")
    except Exception:
        return ""
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return ""
    parts = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text:
            parts.append(text)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def fetch_and_extract(url: str) -> dict | None:
    """Streams the URL (redirects followed by urllib's default opener,
    size-capped at MAX_DOWNLOAD_BYTES). Returns {"text", "content_type":
    "html"|"pdf", "final_url"} or None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=config.CITATIONS_FETCH_TIMEOUT_SECS) as resp:
            if resp.status != 200:
                return None
            content_type = resp.headers.get("Content-Type", "") or ""
            final_url = resp.geturl()
            chunks, total = [], 0
            while True:
                chunk = resp.read(_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    return None
                chunks.append(chunk)
            body = b"".join(chunks)
    except Exception:
        return None

    if not body:
        return None

    if _is_pdf(final_url, content_type, body):
        text, content_kind = extract_pdf_text(body), "pdf"
    else:
        text, content_kind = extract_html_text(body), "html"

    text = text.strip()
    if not text:
        return None
    return {"text": text, "content_type": content_kind, "final_url": final_url}
