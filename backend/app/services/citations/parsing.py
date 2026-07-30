"""Essay -> (works-cited list, in-text-cited statements).

Both extraction steps are LLM-assisted but never trusted at face value: every
entry the model returns must appear verbatim (via textmatch.normalize_text)
in the essay it was supposedly lifted from, or it is dropped. This is the
same "no evidence, no score" posture as the grading engine's evidence guard
(services/grading/engine.py) applied to extraction instead of scoring --
a citation or sentence the model invented rather than found is worthless
input to everything downstream.
"""

import hashlib

from ..textmatch import normalize_text

MAX_CITATIONS = 40
MAX_STATEMENTS = 60


def normalize_citation_text(raw: str) -> str:
    return normalize_text(raw or "").strip()


def source_key_for(raw_citation: str) -> str:
    """Global dedup key: sha256 of the normalized citation text. Two
    assessments citing the same source with identical wording share one
    fetch/index; differently-worded citations of the same real source get
    different keys (accepted v1 scope -- see plan's "Known scope limits")."""
    return hashlib.sha256(normalize_citation_text(raw_citation).encode()).hexdigest()[:32]


def build_citation_extraction_system() -> str:
    return (
        "You extract the works-cited / references list from a student essay. "
        "The essay may cite sources in APA or MLA style.\n\n"
        "Rules (non-negotiable):\n"
        "1. Copy each citation's 'raw' text EXACTLY as it appears in the essay -- "
        "same characters, same punctuation, same whitespace. Do not paraphrase, "
        "reformat, or fix typos.\n"
        "2. Only include entries that are actually part of a works-cited/references "
        "list. Do not invent a citation that isn't in the essay.\n"
        "3. For each entry, also extract (best effort, empty string if absent): "
        "the first author's surname, the title, the publication year, a URL if "
        "one literally appears in the citation text, and a DOI if one literally "
        "appears (bare '10.xxxx/...' form, no 'https://doi.org/' prefix).\n"
        "4. Output only the JSON object.\n\n"
        "OUTPUT -- a single JSON object, exactly this shape:\n"
        '{"citations": [{"raw": "<verbatim citation text>", '
        '"style": "apa"|"mla"|"unknown", "author": "<surname>", "title": "<title>", '
        '"year": "<year>", "url": "<url or empty>", "doi": "<doi or empty>"}]}'
    )


def build_citation_extraction_prompt(essay: str) -> str:
    return f"""ESSAY:
<<<
{essay.strip()}
>>>

Extract the works-cited / references list from this essay."""


def extract_citation_list(llm_json, essay: str) -> list[dict]:
    """One LLM call (one retry on transient failure, mirrors molding.mold_notes).
    Returns a list of citation dicts whose 'raw' field is verbatim-verified
    against `essay`; caps at MAX_CITATIONS, keeping essay order."""
    essay = (essay or "").strip()
    if not essay:
        return []

    system = build_citation_extraction_system()
    prompt = build_citation_extraction_prompt(essay)
    try:
        raw = llm_json(system, prompt)
    except Exception:
        raw = llm_json(system, prompt)

    raw = raw if isinstance(raw, dict) else {}
    entries = raw.get("citations")
    if not isinstance(entries, list):
        return []

    norm_essay = normalize_text(essay)
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_citation = entry.get("raw")
        if not isinstance(raw_citation, str) or not raw_citation.strip():
            continue
        if normalize_text(raw_citation) not in norm_essay:
            continue
        style = entry.get("style")
        if style not in ("apa", "mla"):
            style = "unknown"
        out.append({
            "raw": raw_citation.strip(),
            "style": style,
            "author": (entry.get("author") or "").strip(),
            "title": (entry.get("title") or "").strip(),
            "year": (entry.get("year") or "").strip(),
            "url": (entry.get("url") or "").strip(),
            "doi": (entry.get("doi") or "").strip(),
        })
        if len(out) >= MAX_CITATIONS:
            break
    return out


def build_statement_extraction_system() -> str:
    return (
        "You find sentences in a student essay's BODY (not the works-cited list "
        "itself) that carry an inline citation marker, e.g. \"(Smith, 2020)\" "
        "(APA) or \"(Smith 12)\" (MLA, author + page number).\n\n"
        "Rules (non-negotiable):\n"
        "1. Copy 'sentence' and 'marker' EXACTLY as they appear in the essay -- "
        "verbatim substrings, not paraphrases.\n"
        "2. 'marker' is just the parenthetical citation itself, e.g. \"(Smith, 2020)\".\n"
        "3. Only include sentences that make a factual claim attributed to a "
        "source -- skip purely transitional or opinion sentences even if they "
        "happen to sit near a citation.\n"
        "4. Output only the JSON object.\n\n"
        "OUTPUT -- a single JSON object, exactly this shape:\n"
        '{"statements": [{"sentence": "<verbatim sentence>", "marker": "<verbatim marker>"}]}'
    )


def build_statement_extraction_prompt(essay: str) -> str:
    return f"""ESSAY:
<<<
{essay.strip()}
>>>

Find sentences in the essay body carrying an inline citation marker."""


def extract_cited_statements(llm_json, essay: str) -> list[dict]:
    """One LLM call (one retry). Returns [{"sentence", "marker"}] verbatim-
    verified against `essay`; caps at MAX_STATEMENTS, keeping essay order."""
    essay = (essay or "").strip()
    if not essay:
        return []

    system = build_statement_extraction_system()
    prompt = build_statement_extraction_prompt(essay)
    try:
        raw = llm_json(system, prompt)
    except Exception:
        raw = llm_json(system, prompt)

    raw = raw if isinstance(raw, dict) else {}
    entries = raw.get("statements")
    if not isinstance(entries, list):
        return []

    norm_essay = normalize_text(essay)
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sentence = entry.get("sentence")
        marker = entry.get("marker")
        if not isinstance(sentence, str) or not sentence.strip():
            continue
        if not isinstance(marker, str) or not marker.strip():
            continue
        if normalize_text(sentence) not in norm_essay or normalize_text(marker) not in norm_essay:
            continue
        out.append({"sentence": sentence.strip(), "marker": marker.strip()})
        if len(out) >= MAX_STATEMENTS:
            break
    return out
