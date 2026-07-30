"""Resolve one parsed citation entry to a fetchable document URL.

Priority order (per product decision -- most essays on this platform cite
sources that already carry a URL, so that is the primary path, not a
fallback):
  1. The citation's own URL, if it has one.
  2. A DOI (explicit, or parsed out of the citation's own URL) resolved via
     Crossref's free `/works/{doi}` endpoint.
  3. A Crossref bibliographic search by author/title/year, for citations
     that give neither a URL nor a DOI (typical of some APA journal entries).
  4. Otherwise: unresolved. The caller marks the citation "unverifiable" and
     moves on -- this module never raises, so one bad citation can never
     abort a fact-check run.

Uses stdlib urllib (no requests/httpx dependency), matching core.llm's
existing outbound-HTTP convention.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ... import config

CROSSREF_BASE = "https://api.crossref.org/works"
DOI_RE = re.compile(r'10\.\d{4,9}/[^\s"\'<>]+', re.IGNORECASE)
TITLE_SIMILARITY_THRESHOLD = 0.6

_USER_AGENT = "assessment-platform-citations/0.1 (+https://github.com/ohburks/essay-grading)"

_throttle_lock = threading.Lock()
_last_call_ts = 0.0


def extract_doi(text: str) -> str | None:
    match = DOI_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip('.,);')


def _tokenize(s: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _title_similarity(a: str, b: str) -> float:
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _crossref_get(path: str, params: dict) -> dict | None:
    """Single chokepoint for every Crossref HTTP call. Never raises -- any
    network/timeout/non-2xx/parse failure returns None. Tests monkeypatch
    this one function rather than mocking urllib directly."""
    global _last_call_ts
    with _throttle_lock:
        wait = _last_call_ts + config.CROSSREF_MIN_INTERVAL_SECS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.monotonic()

    base = f"{CROSSREF_BASE}/{urllib.parse.quote(path, safe='/')}" if path else CROSSREF_BASE
    params = dict(params or {})
    if config.CROSSREF_MAILTO:
        params["mailto"] = config.CROSSREF_MAILTO
    url = f"{base}?{urllib.parse.urlencode(params)}" if params else base
    headers = {"User-Agent": _USER_AGENT}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=config.CITATIONS_FETCH_TIMEOUT_SECS) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def crossref_lookup_by_doi(doi: str) -> str | None:
    data = _crossref_get(doi, {})
    if not data:
        return None
    msg = data.get("message") or {}
    # Prefer TDM/full-text links (where open-access full text surfaces) over
    # the primary landing-page URL, which is often just a paywall.
    for link in (msg.get("link") or []):
        url = link.get("URL")
        if url:
            return url
    primary = (msg.get("resource") or {}).get("primary") or {}
    return primary.get("URL") or None


def crossref_search_by_biblio(author: str, title: str, year: str) -> tuple[str | None, str | None]:
    query = " ".join(x for x in (author, title, year) if x).strip()
    if not query:
        return None, None
    data = _crossref_get("", {"query.bibliographic": query, "rows": 3})
    if not data:
        return None, None
    items = ((data.get("message") or {}).get("items")) or []
    if not items:
        return None, None
    top = items[0]
    top_titles = top.get("title") or []
    top_title = top_titles[0] if top_titles else ""
    if _title_similarity(title, top_title) < TITLE_SIMILARITY_THRESHOLD:
        return None, None
    url = None
    for link in (top.get("link") or []):
        u = link.get("URL")
        if u:
            url = u
            break
    if not url:
        url = ((top.get("resource") or {}).get("primary") or {}).get("URL")
    return url, top.get("DOI")


def resolve_citation(entry: dict) -> dict:
    """Never raises. Returns {"resolved_url", "method": "direct_url"|"doi"|
    "crossref_search"|"unresolved", "doi", "error"}."""
    try:
        url = (entry.get("url") or "").strip()
        if url.lower().startswith(("http://", "https://")):
            return {"resolved_url": url, "method": "direct_url",
                    "doi": (entry.get("doi") or "").strip() or extract_doi(url),
                    "error": ""}

        doi = (entry.get("doi") or "").strip() or extract_doi(url)
        if doi:
            resolved = crossref_lookup_by_doi(doi)
            if resolved:
                return {"resolved_url": resolved, "method": "doi", "doi": doi, "error": ""}

        title = (entry.get("title") or "").strip()
        if title:
            resolved_url, found_doi = crossref_search_by_biblio(
                entry.get("author") or "", title, entry.get("year") or "")
            if resolved_url:
                return {"resolved_url": resolved_url, "method": "crossref_search",
                        "doi": found_doi or doi, "error": ""}

        return {"resolved_url": None, "method": "unresolved", "doi": doi, "error": ""}
    except Exception as e:
        return {"resolved_url": None, "method": "unresolved", "doi": None, "error": str(e)[:300]}
