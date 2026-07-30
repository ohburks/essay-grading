"""Unit tests for citations.resolution and citations.fetch -- together, "how
do we turn a parsed citation into indexable text": priority order (direct
URL > DOI > Crossref bibliographic search > unresolved), the safeguards
around Crossref's response shape, and HTML/PDF extraction plus
fetch_and_extract's never-raises contract. All network access is
monkeypatched (resolution._crossref_get as the single chokepoint; urllib for
fetch) so nothing here touches the real network."""

import urllib.error
import urllib.request

from app.services.citations import fetch, resolution

# ---- resolution.py ----


def test_extract_doi_finds_bare_doi_and_strips_trailing_punctuation():
    assert resolution.extract_doi("See 10.1145/1234567.") == "10.1145/1234567"
    assert resolution.extract_doi("https://doi.org/10.1000/xyz123)") == "10.1000/xyz123"
    assert resolution.extract_doi("no doi here") is None


def test_title_similarity_identical_and_disjoint():
    assert resolution._title_similarity("Feedback Loops in Climate Models",
                                        "Feedback Loops in Climate Models") == 1.0
    assert resolution._title_similarity("Feedback Loops in Climate Models",
                                        "Unrelated Topic Entirely") == 0.0


def test_resolve_citation_prefers_direct_url():
    entry = {"url": "https://example.org/paper", "doi": "", "author": "x", "title": "x", "year": "x"}
    result = resolution.resolve_citation(entry)
    assert result == {"resolved_url": "https://example.org/paper", "method": "direct_url",
                      "doi": None, "error": ""}


def test_resolve_citation_uses_doi_when_no_url(monkeypatch):
    def fake_get(path, params):
        assert path == "10.1145/999"
        return {"message": {"link": [{"URL": "https://oa.example.org/fulltext.pdf"}]}}

    monkeypatch.setattr(resolution, "_crossref_get", fake_get)
    entry = {"url": "", "doi": "10.1145/999", "author": "x", "title": "x", "year": "x"}
    result = resolution.resolve_citation(entry)
    assert result["method"] == "doi"
    assert result["resolved_url"] == "https://oa.example.org/fulltext.pdf"


def test_crossref_lookup_by_doi_prefers_link_over_primary_resource(monkeypatch):
    def fake_get(path, params):
        return {"message": {
            "link": [{"URL": "https://oa.example.org/fulltext.pdf"}],
            "resource": {"primary": {"URL": "https://paywalled.example.org/landing"}},
        }}

    monkeypatch.setattr(resolution, "_crossref_get", fake_get)
    assert resolution.crossref_lookup_by_doi("10.1/x") == "https://oa.example.org/fulltext.pdf"


def test_crossref_lookup_by_doi_falls_back_to_primary_resource_when_no_link(monkeypatch):
    def fake_get(path, params):
        return {"message": {"link": [], "resource": {"primary": {"URL": "https://paywalled.example.org/landing"}}}}

    monkeypatch.setattr(resolution, "_crossref_get", fake_get)
    assert resolution.crossref_lookup_by_doi("10.1/x") == "https://paywalled.example.org/landing"


def test_resolve_citation_falls_back_to_crossref_search_when_no_url_or_doi(monkeypatch):
    def fake_get(path, params):
        assert path == ""
        assert "query.bibliographic" in params
        return {"message": {"items": [{
            "title": ["Feedback Loops in Climate Models"],
            "DOI": "10.1/found",
            "link": [{"URL": "https://oa.example.org/found.pdf"}],
        }]}}

    monkeypatch.setattr(resolution, "_crossref_get", fake_get)
    entry = {"url": "", "doi": "", "author": "Diaz", "title": "Feedback Loops in Climate Models",
             "year": "2021"}
    result = resolution.resolve_citation(entry)
    assert result == {"resolved_url": "https://oa.example.org/found.pdf",
                      "method": "crossref_search", "doi": "10.1/found", "error": ""}


def test_crossref_search_rejects_low_similarity_top_hit(monkeypatch):
    def fake_get(path, params):
        return {"message": {"items": [{
            "title": ["Completely Unrelated Topic"], "DOI": "10.1/wrong",
            "link": [{"URL": "https://oa.example.org/wrong.pdf"}],
        }]}}

    monkeypatch.setattr(resolution, "_crossref_get", fake_get)
    url, doi = resolution.crossref_search_by_biblio("Diaz", "Feedback Loops in Climate Models", "2021")
    assert url is None and doi is None


def test_resolve_citation_unresolved_when_crossref_finds_nothing(monkeypatch):
    monkeypatch.setattr(resolution, "_crossref_get", lambda path, params: None)
    entry = {"url": "", "doi": "", "author": "Nobody", "title": "Nothing Findable", "year": "1900"}
    result = resolution.resolve_citation(entry)
    assert result["method"] == "unresolved"
    assert result["resolved_url"] is None


def test_resolve_citation_never_raises_on_internal_error(monkeypatch):
    def boom(path, params):
        raise RuntimeError("should never surface")

    monkeypatch.setattr(resolution, "_crossref_get", boom)
    entry = {"url": "", "doi": "", "author": "x", "title": "x", "year": "x"}
    result = resolution.resolve_citation(entry)
    assert result["method"] == "unresolved"


def test_crossref_get_never_raises_on_bad_status(monkeypatch):
    class FakeResp:
        status = 500

        def read(self):
            raise AssertionError("should not be called")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: FakeResp())
    assert resolution._crossref_get("", {}) is None


# ---- fetch.py ----


def test_extract_html_text_strips_script_style_nav_footer_header():
    html = b"""
    <html><body>
      <header>Site Header</header>
      <nav>Nav Links</nav>
      <script>var x = 1;</script>
      <style>.a { color: red; }</style>
      <main>The actual article content lives here.</main>
      <footer>Site Footer</footer>
    </body></html>
    """
    text = fetch.extract_html_text(html)
    assert "The actual article content lives here." in text
    assert "Site Header" not in text
    assert "Nav Links" not in text
    assert "var x = 1" not in text
    assert "color: red" not in text
    assert "Site Footer" not in text


def test_extract_pdf_text_tolerates_one_broken_page(monkeypatch):
    class FakePage:
        def __init__(self, text=None, raises=False):
            self._text = text
            self._raises = raises

        def extract_text(self):
            if self._raises:
                raise RuntimeError("corrupt page")
            return self._text

    class FakeReader:
        def __init__(self, _stream):
            self.pages = [FakePage(text="Good page one."),
                         FakePage(raises=True),
                         FakePage(text="Good page three.")]

    monkeypatch.setattr("pypdf.PdfReader", FakeReader)
    text = fetch.extract_pdf_text(b"%PDF-1.4 fake")
    assert "Good page one." in text
    assert "Good page three." in text


def test_extract_pdf_text_returns_empty_on_unparseable_bytes():
    assert fetch.extract_pdf_text(b"not a real pdf") == ""


class _FakeResp:
    status = 200
    headers = {}

    def __init__(self, chunks, final_url="https://example.org/x"):
        self._chunks = list(chunks)
        self._final_url = final_url

    def geturl(self):
        return self._final_url

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_and_extract_returns_none_on_non_200(monkeypatch):
    resp = _FakeResp([])
    resp.status = 404
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: resp)
    assert fetch.fetch_and_extract("https://example.org/missing") is None


def test_fetch_and_extract_returns_none_on_oversize(monkeypatch):
    big_chunk = b"x" * (fetch.MAX_DOWNLOAD_BYTES // 2 + 1)
    resp = _FakeResp([big_chunk, big_chunk], final_url="https://example.org/big")
    resp.headers = {"Content-Type": "text/html"}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: resp)
    assert fetch.fetch_and_extract("https://example.org/big") is None


def test_fetch_and_extract_returns_none_on_timeout(monkeypatch):
    def raise_timeout(*a, **kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", raise_timeout)
    assert fetch.fetch_and_extract("https://example.org/slow") is None


def test_fetch_and_extract_dispatches_pdf_by_magic_bytes(monkeypatch):
    resp = _FakeResp([b"%PDF-1.4 ..."], final_url="https://example.org/report")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: resp)
    monkeypatch.setattr(fetch, "extract_pdf_text", lambda b: "extracted pdf text")
    result = fetch.fetch_and_extract("https://example.org/report")
    assert result == {"text": "extracted pdf text", "content_type": "pdf",
                      "final_url": "https://example.org/report"}
