"""Match an in-text-cited statement to its works-cited entry, retrieve
relevant passages, and get a verdict -- guarded so a claimed supporting
quote is never trusted unless it is actually found in the specific passage
claimed. This guard is the fact-check analog of
services/grading/engine.normalize_pass's evidence-provenance check.
"""

import re

from ..textmatch import normalize_text

VALID_VERDICTS = frozenset({"supported", "contradicted", "not_addressed"})

_YEAR_RE = re.compile(r"(19|20)\d{2}")
_SURNAME_RE = re.compile(r"[A-Za-z\-']+")


def _marker_surname(marker: str) -> str:
    inner = (marker or "").strip().strip("()")
    m = _SURNAME_RE.match(inner)
    return m.group(0).lower() if m else ""


def _marker_year(marker: str) -> str | None:
    m = _YEAR_RE.search(marker or "")
    return m.group(0) if m else None


def match_statement_to_source(marker: str, citations: list[dict]) -> dict | None:
    """Returns {"citation": <entry>, "confidence": "high"} or None (never
    guesses). If the marker has a year, requires author-surname substring
    AND year equality, matching exactly one citation. If the marker has no
    year (typical MLA page-only cite), matches by surname alone -- only if
    exactly one citation shares that surname. Zero or 2+ candidates in
    either case -> None."""
    surname = _marker_surname(marker)
    if not surname:
        return None
    year = _marker_year(marker)

    if year:
        candidates = [c for c in citations
                     if surname in (c.get("author") or "").lower()
                     and year == (c.get("year") or "").strip()]
    else:
        candidates = [c for c in citations if surname in (c.get("author") or "").lower()]

    if len(candidates) == 1:
        return {"citation": candidates[0], "confidence": "high"}
    return None


def build_verdict_system() -> str:
    return (
        "You fact-check a single sentence from a student essay against passages "
        "retrieved from the source it cites.\n\n"
        "Rules (non-negotiable):\n"
        "1. Judge ONLY using the passages given -- do not use outside knowledge.\n"
        "2. verdict must be exactly one of: \"supported\" (the passages back up "
        "the statement), \"contradicted\" (the passages say something that "
        "conflicts with the statement), or \"not_addressed\" (the passages don't "
        "speak to the statement one way or the other).\n"
        "3. If verdict is \"supported\" or \"contradicted\", quote the exact "
        "sentence or phrase from the passage that justifies it -- copy it "
        "verbatim -- and name which passage id it came from.\n"
        "4. Output only the JSON object.\n\n"
        "OUTPUT -- a single JSON object, exactly this shape:\n"
        '{"verdict": "supported"|"contradicted"|"not_addressed", '
        '"quote": "<verbatim quote from a passage, or empty>", '
        '"chunkId": "<id of the passage the quote came from, or empty>", '
        '"reasoning": "<one sentence>"}'
    )


def build_verdict_prompt(statement: str, chunks: list[dict]) -> str:
    passages_block = "\n\n".join(
        f"PASSAGE (id={c['chunk_id']}):\n{c['text']}" for c in chunks
    )
    return f"""STATEMENT FROM THE ESSAY:
<<<
{statement.strip()}
>>>

RETRIEVED PASSAGES FROM THE CITED SOURCE:
{passages_block}

Judge whether the statement is supported, contradicted, or not addressed by these passages."""


def verdict_for_statement(llm_json, statement: str, chunks: list[dict]) -> dict:
    """One LLM call (one retry). Returns the RAW, not-yet-trusted claim."""
    system = build_verdict_system()
    prompt = build_verdict_prompt(statement, chunks)
    try:
        raw = llm_json(system, prompt)
    except Exception:
        raw = llm_json(system, prompt)
    return raw if isinstance(raw, dict) else {}


def normalize_verdict(raw: dict, chunks: list[dict]) -> dict:
    """The evidence-provenance guard: verdict must be one of VALID_VERDICTS;
    any claimed quote must appear verbatim in the SPECIFIC chunk claimed --
    a quote that's real but attributed to a different chunk, or not
    locatable at all, demotes the verdict to 'not_addressed'."""
    verdict = raw.get("verdict")
    if verdict not in VALID_VERDICTS:
        verdict = "not_addressed"

    reasoning = raw.get("reasoning")
    reasoning = reasoning.strip()[:500] if isinstance(reasoning, str) else ""

    evidence_quote, evidence_chunk_id = "", ""
    if verdict in ("supported", "contradicted"):
        quote = raw.get("quote")
        chunk_id = raw.get("chunkId")
        claimed_chunk = next((c for c in chunks if c.get("chunk_id") == chunk_id), None)
        if (isinstance(quote, str) and quote.strip() and claimed_chunk
                and normalize_text(quote) in normalize_text(claimed_chunk.get("text", ""))):
            evidence_quote = quote.strip()
            evidence_chunk_id = chunk_id
        else:
            verdict = "not_addressed"

    return {"verdict": verdict, "evidence_quote": evidence_quote,
           "evidence_chunk_id": evidence_chunk_id, "reasoning": reasoning}
