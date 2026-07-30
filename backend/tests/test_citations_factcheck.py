"""Unit tests for citations.factcheck (statement matching + the
evidence-provenance guard) and citations.pipeline.get_or_fetch_source (the
cache-checked entry point mirroring molding.get_or_mold_notes's shape,
except a genuine fetch failure IS cached here, since a paywalled URL doesn't
un-paywall itself). resolution/fetch/vectorstore are monkeypatched
throughout so nothing here touches the network or a real Chroma instance.

normalize_verdict is the most important test in the feature: it's what
stops a hallucinated "supporting" quote from ever reaching the UI."""

from app.db import database as db
from app.services.citations import factcheck, parsing, pipeline
from app.services.citations import fetch as fetch_mod
from app.services.citations import resolution as resolution_mod
from app.services.citations import vectorstore as vectorstore_mod

db.init_db()

CITATIONS = [
    {"raw": "Diaz, Maria. Title A. 2021.", "author": "diaz", "year": "2021"},
    {"raw": "Smith, John. Title B. 2019.", "author": "smith", "year": "2019"},
    {"raw": "Smith, Alice. Title C. 2020.", "author": "smith", "year": "2020"},
]

CHUNKS = [
    {"chunk_id": "src:0", "text": "The study found a 30% increase in observed rainfall."},
    {"chunk_id": "src:1", "text": "No conclusions were drawn about temperature trends."},
]


# ---- match_statement_to_source ----

def test_match_with_year_requires_surname_and_year_match():
    match = factcheck.match_statement_to_source("(Diaz, 2021)", CITATIONS)
    assert match is not None
    assert match["citation"]["raw"] == "Diaz, Maria. Title A. 2021."
    assert match["confidence"] == "high"


def test_match_with_year_no_candidate_returns_none():
    assert factcheck.match_statement_to_source("(Diaz, 1999)", CITATIONS) is None


def test_match_without_year_unambiguous_surname_matches():
    match = factcheck.match_statement_to_source("(Diaz 12)", CITATIONS)
    assert match is not None
    assert match["citation"]["raw"] == "Diaz, Maria. Title A. 2021."


def test_match_without_year_ambiguous_surname_returns_none():
    # Two "Smith" entries with different years -> never guess which one.
    assert factcheck.match_statement_to_source("(Smith 12)", CITATIONS) is None


def test_match_with_no_surname_returns_none():
    assert factcheck.match_statement_to_source("()", CITATIONS) is None


# ---- normalize_verdict: the evidence-provenance guard ----

def test_normalize_verdict_accepts_quote_verbatim_in_claimed_chunk():
    raw = {"verdict": "supported", "quote": "30% increase in observed rainfall",
          "chunkId": "src:0", "reasoning": "Matches directly."}
    result = factcheck.normalize_verdict(raw, CHUNKS)
    assert result["verdict"] == "supported"
    assert result["evidence_quote"] == "30% increase in observed rainfall"
    assert result["evidence_chunk_id"] == "src:0"


def test_normalize_verdict_demotes_when_quote_not_in_claimed_chunk_at_all():
    raw = {"verdict": "supported", "quote": "a quote that appears nowhere",
          "chunkId": "src:0", "reasoning": "..."}
    result = factcheck.normalize_verdict(raw, CHUNKS)
    assert result["verdict"] == "not_addressed"
    assert result["evidence_quote"] == ""


def test_normalize_verdict_demotes_wrong_attribution_quote_real_but_different_chunk():
    """The quote IS real (appears verbatim in src:1), but the model claimed it
    came from src:0 — this must be demoted, not accepted just because the
    quote exists somewhere in the retrieved set."""
    raw = {"verdict": "contradicted", "quote": "No conclusions were drawn about temperature trends",
          "chunkId": "src:0", "reasoning": "..."}
    result = factcheck.normalize_verdict(raw, CHUNKS)
    assert result["verdict"] == "not_addressed"
    assert result["evidence_quote"] == ""


def test_normalize_verdict_invalid_verdict_value_falls_back_to_not_addressed():
    raw = {"verdict": "definitely true", "quote": "", "chunkId": "", "reasoning": ""}
    result = factcheck.normalize_verdict(raw, CHUNKS)
    assert result["verdict"] == "not_addressed"


def test_normalize_verdict_not_addressed_never_requires_a_quote():
    raw = {"verdict": "not_addressed", "quote": "", "chunkId": "", "reasoning": "irrelevant passages"}
    result = factcheck.normalize_verdict(raw, CHUNKS)
    assert result["verdict"] == "not_addressed"
    assert result["evidence_quote"] == ""


def test_normalize_verdict_supported_with_missing_quote_is_demoted():
    raw = {"verdict": "supported", "quote": "", "chunkId": "src:0", "reasoning": "..."}
    result = factcheck.normalize_verdict(raw, CHUNKS)
    assert result["verdict"] == "not_addressed"


def test_normalize_verdict_truncates_long_reasoning():
    raw = {"verdict": "not_addressed", "quote": "", "chunkId": "", "reasoning": "x" * 1000}
    result = factcheck.normalize_verdict(raw, CHUNKS)
    assert len(result["reasoning"]) == 500


# ---- pipeline.get_or_fetch_source: the cache-checked entry point ----

def _entry(raw="Diaz, Maria. Title. 2021.", url="https://example.org/diaz", doi=""):
    return {"raw": raw, "style": "mla", "author": "Diaz", "title": "Title",
           "year": "2021", "url": url, "doi": doi}


def test_get_or_fetch_source_success_path_reaches_indexed(monkeypatch):
    monkeypatch.setattr(resolution_mod, "resolve_citation",
                        lambda entry: {"resolved_url": entry["url"], "method": "direct_url",
                                      "doi": None, "error": ""})
    monkeypatch.setattr(fetch_mod, "fetch_and_extract",
                        lambda url: {"text": "x" * 500, "content_type": "html", "final_url": url})
    monkeypatch.setattr(vectorstore_mod, "index_source", lambda key, text: 4)

    entry = _entry(raw="cache-test-success")
    key = parsing.source_key_for(entry["raw"])
    row = pipeline.get_or_fetch_source(key, entry)
    assert row["status"] == "indexed"
    assert row["chunk_count"] == 4
    assert row["resolved_url"] == entry["url"]


def test_get_or_fetch_source_caches_terminal_state_no_refetch(monkeypatch):
    calls = {"resolve": 0, "fetch": 0}

    def counting_resolve(entry):
        calls["resolve"] += 1
        return {"resolved_url": entry["url"], "method": "direct_url", "doi": None, "error": ""}

    def counting_fetch(url):
        calls["fetch"] += 1
        return {"text": "x" * 500, "content_type": "html", "final_url": url}

    monkeypatch.setattr(resolution_mod, "resolve_citation", counting_resolve)
    monkeypatch.setattr(fetch_mod, "fetch_and_extract", counting_fetch)
    monkeypatch.setattr(vectorstore_mod, "index_source", lambda key, text: 2)

    entry = _entry(raw="cache-test-no-refetch")
    key = parsing.source_key_for(entry["raw"])
    pipeline.get_or_fetch_source(key, entry)
    assert calls["resolve"] == 1 and calls["fetch"] == 1

    # Second call on an already-'indexed' source must not touch resolve/fetch again.
    row = pipeline.get_or_fetch_source(key, entry)
    assert row["status"] == "indexed"
    assert calls["resolve"] == 1 and calls["fetch"] == 1


def test_get_or_fetch_source_unresolved_citation_marked_unverifiable(monkeypatch):
    monkeypatch.setattr(resolution_mod, "resolve_citation",
                        lambda entry: {"resolved_url": None, "method": "unresolved",
                                      "doi": None, "error": ""})
    entry = _entry(raw="cache-test-unresolved", url="")
    key = parsing.source_key_for(entry["raw"])
    row = pipeline.get_or_fetch_source(key, entry)
    assert row["status"] == "unverifiable"


def test_get_or_fetch_source_short_text_marked_unverifiable_not_indexed(monkeypatch):
    monkeypatch.setattr(resolution_mod, "resolve_citation",
                        lambda entry: {"resolved_url": entry["url"], "method": "direct_url",
                                      "doi": None, "error": ""})
    monkeypatch.setattr(fetch_mod, "fetch_and_extract",
                        lambda url: {"text": "too short", "content_type": "html", "final_url": url})
    entry = _entry(raw="cache-test-short-text")
    key = parsing.source_key_for(entry["raw"])
    row = pipeline.get_or_fetch_source(key, entry)
    assert row["status"] == "unverifiable"


def test_get_or_fetch_source_failure_is_not_retried_without_force(monkeypatch):
    calls = {"n": 0}

    def counting_resolve(entry):
        calls["n"] += 1
        return {"resolved_url": None, "method": "unresolved", "doi": None, "error": "boom"}

    monkeypatch.setattr(resolution_mod, "resolve_citation", counting_resolve)
    entry = _entry(raw="cache-test-error-not-retried", url="")
    key = parsing.source_key_for(entry["raw"])
    pipeline.get_or_fetch_source(key, entry)
    pipeline.get_or_fetch_source(key, entry)
    assert calls["n"] == 1


def test_reset_cited_sources_for_retry_allows_a_fresh_attempt(monkeypatch):
    calls = {"n": 0}

    def counting_resolve(entry):
        calls["n"] += 1
        return {"resolved_url": None, "method": "unresolved", "doi": None, "error": "boom"}

    monkeypatch.setattr(resolution_mod, "resolve_citation", counting_resolve)
    entry = _entry(raw="cache-test-force-retry", url="")
    key = parsing.source_key_for(entry["raw"])
    pipeline.get_or_fetch_source(key, entry)
    assert calls["n"] == 1

    db.reset_cited_sources_for_retry([key])
    pipeline.get_or_fetch_source(key, entry)
    assert calls["n"] == 2


def test_differently_worded_citations_of_same_source_get_different_keys():
    """Accepted v1 scope: dedup is by citation-TEXT hash, not resolved
    identity, so two students citing the same real source with different
    wording are treated as different cache entries."""
    key_a = parsing.source_key_for("Diaz, M. (2021). Title. Journal.")
    key_b = parsing.source_key_for("Diaz, Maria. \"Title.\" Journal, 2021.")
    assert key_a != key_b


def test_get_or_fetch_source_zero_chunks_marked_error(monkeypatch):
    monkeypatch.setattr(resolution_mod, "resolve_citation",
                        lambda entry: {"resolved_url": entry["url"], "method": "direct_url",
                                      "doi": None, "error": ""})
    monkeypatch.setattr(fetch_mod, "fetch_and_extract",
                        lambda url: {"text": "x" * 500, "content_type": "html", "final_url": url})
    monkeypatch.setattr(vectorstore_mod, "index_source", lambda key, text: 0)

    entry = _entry(raw="cache-test-zero-chunks")
    key = parsing.source_key_for(entry["raw"])
    row = pipeline.get_or_fetch_source(key, entry)
    assert row["status"] == "error"
