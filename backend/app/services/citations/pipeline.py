"""Orchestration for the citation fact-check job: resolve+fetch+index every
works-cited entry, then match+retrieve+verdict every in-text-cited statement.

Per the product decision (skip-and-flag, never fail the whole run): a
failure on any single citation or statement is caught and recorded as that
row's own outcome. Only a systemic failure (e.g. the DB itself unreachable)
propagates -- which services.jobs already turns into an 'error' job status,
matching engine.grade_session's contract.
"""

from ...db import database as db
from . import factcheck, fetch, parsing, resolution, vectorstore


def get_or_fetch_source(source_key: str, entry: dict) -> dict:
    """Cache-checked entry point (mirrors molding.get_or_mold_notes's shape).
    If cited_sources already has this key in a terminal state, returns it
    unchanged -- no re-fetch, no re-embed, no re-hit on Crossref. Unlike
    style-mold caching, a genuine fetch FAILURE is deliberately cached here:
    a paywalled URL doesn't un-paywall itself, so 'error'/'unverifiable' are
    stable facts, not invalid results to discard. Every exception is caught
    and recorded as this source's own error status -- never propagates."""
    row = db.get_cited_source(source_key)
    if row is None:
        db.upsert_cited_source(
            source_key, style=entry.get("style", ""), raw_citation=entry.get("raw", ""),
            author=entry.get("author", ""), title=entry.get("title", ""),
            year=entry.get("year", ""), citation_url=entry.get("url", ""),
            doi=entry.get("doi", ""))
        row = db.get_cited_source(source_key)

    if row["status"] in ("indexed", "unverifiable", "error"):
        return row

    try:
        resolved = resolution.resolve_citation(entry)
        resolved_url = resolved.get("resolved_url")
        method = resolved.get("method", "unresolved")
        if not resolved_url:
            db.update_cited_source(
                source_key, status="unverifiable", resolution_method=method,
                error=resolved.get("error") or "No fetchable URL could be resolved.")
            return db.get_cited_source(source_key)

        fetched = fetch.fetch_and_extract(resolved_url)
        if not fetched or len(fetched["text"]) < fetch.MIN_INDEXABLE_CHARS:
            db.update_cited_source(
                source_key, status="unverifiable", resolved_url=resolved_url,
                resolution_method=method, fetched_at=db.utcnow(),
                error="" if fetched else "Could not download or extract readable text from the resolved URL.")
            return db.get_cited_source(source_key)

        chunk_count = vectorstore.index_source(source_key, fetched["text"])
        if chunk_count == 0:
            db.update_cited_source(
                source_key, status="error", resolved_url=resolved_url,
                resolution_method=method, fetched_at=db.utcnow(),
                error="Vector indexing is unavailable or produced no chunks.")
            return db.get_cited_source(source_key)

        db.update_cited_source(
            source_key, status="indexed", resolved_url=resolved_url,
            resolution_method=method, chunk_count=chunk_count,
            fetched_at=db.utcnow(), indexed_at=db.utcnow(), error="")
        return db.get_cited_source(source_key)
    except Exception as e:
        db.update_cited_source(source_key, status="error", error=str(e)[:300])
        return db.get_cited_source(source_key)


def run_fact_check(llm_json, assessment_id: str, essay: str,
                   citations: list[dict], statements: list[dict], report) -> None:
    total = len(citations) + max(1, len(statements))
    done = 0

    enriched = []
    for i, entry in enumerate(citations):
        source_key = parsing.source_key_for(entry["raw"])
        db.upsert_cited_source(
            source_key, style=entry.get("style", ""), raw_citation=entry["raw"],
            author=entry.get("author", ""), title=entry.get("title", ""),
            year=entry.get("year", ""), citation_url=entry.get("url", ""),
            doi=entry.get("doi", ""))
        db.link_assessment_citation(assessment_id, i, source_key, entry["raw"])
        get_or_fetch_source(source_key, entry)
        enriched.append((source_key, entry))
        done += 1
        report(done, total, f"resolving citation {i + 1}/{len(citations)}")

    citation_entries = [entry for _, entry in enriched]
    source_key_by_raw = {entry["raw"]: source_key for source_key, entry in enriched}

    if not statements:
        report(total, total, "no cited statements found")
        return

    for i, stmt in enumerate(statements):
        try:
            rec = _check_one_statement(llm_json, stmt, citation_entries, source_key_by_raw)
        except Exception as e:
            rec = {
                "statement_text": stmt.get("sentence", ""),
                "citation_marker": stmt.get("marker", ""),
                "source_key": "", "match_confidence": "", "verdict": "unchecked",
                "evidence_quote": "", "evidence_chunk_id": "",
                "reasoning": f"Internal error: {str(e)[:200]}",
            }
        db.upsert_fact_check_result(assessment_id, i, rec)
        done += 1
        report(done, total, f"checking statement {i + 1}/{len(statements)}")


def _check_one_statement(llm_json, stmt: dict, citation_entries: list[dict],
                         source_key_by_raw: dict) -> dict:
    rec = {
        "statement_text": stmt["sentence"], "citation_marker": stmt["marker"],
        "source_key": "", "match_confidence": "", "verdict": "unchecked",
        "evidence_quote": "", "evidence_chunk_id": "", "reasoning": "",
    }

    match = factcheck.match_statement_to_source(stmt["marker"], citation_entries)
    if match is None:
        rec["reasoning"] = "No unambiguous works-cited match for this citation marker."
        return rec

    matched_entry = match["citation"]
    source_key = source_key_by_raw.get(matched_entry["raw"], "")
    rec["source_key"] = source_key
    rec["match_confidence"] = match["confidence"]

    source_row = db.get_cited_source(source_key) if source_key else None
    if not source_row or source_row["status"] != "indexed":
        rec["verdict"] = "unverifiable_source"
        rec["reasoning"] = "Matched source could not be fetched or indexed."
        return rec

    chunks = vectorstore.query_source(source_key, stmt["sentence"])
    if not chunks:
        rec["verdict"] = "not_addressed"
        rec["reasoning"] = "No relevant passages retrieved from the source."
        return rec

    raw_verdict = factcheck.verdict_for_statement(llm_json, stmt["sentence"], chunks)
    rec.update(factcheck.normalize_verdict(raw_verdict, chunks))
    return rec
