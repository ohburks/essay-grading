"""Unit tests for citations.parsing — essay -> (citation list, cited
statements). The load-bearing property tested throughout: an LLM-claimed
entry that isn't verbatim in the essay must be dropped, mirroring the
grading engine's evidence-provenance posture applied to extraction."""

from app.services.citations import parsing

ESSAY = (
    "Climate change is accelerating faster than expected (Diaz, 2021). "
    "Some models remain uncertain about long-term feedback loops.\n\n"
    "Works Cited\n"
    "Diaz, Maria. \"Feedback Loops in Climate Models.\" Journal of Climate Science, 2021, "
    "https://example.org/diaz-2021."
)


def test_source_key_for_is_deterministic_and_case_whitespace_insensitive():
    a = parsing.source_key_for("Diaz, Maria. Title. 2021.")
    b = parsing.source_key_for("  diaz, maria.   title.   2021.  ")
    assert a == b
    assert len(a) == 32


def test_source_key_for_differs_for_different_citations():
    a = parsing.source_key_for("Diaz, Maria. Title. 2021.")
    b = parsing.source_key_for("Smith, John. Other Title. 2020.")
    assert a != b


def test_extract_citation_list_drops_entry_not_verbatim_in_essay():
    def fake_llm(system, prompt):
        return {"citations": [
            {"raw": "Diaz, Maria. \"Feedback Loops in Climate Models.\" Journal of Climate Science, "
                   "2021, https://example.org/diaz-2021.",
             "style": "mla", "author": "Diaz", "title": "Feedback Loops in Climate Models",
             "year": "2021", "url": "https://example.org/diaz-2021", "doi": ""},
            {"raw": "A citation the model invented that never appears in the essay.",
             "style": "mla", "author": "Fake", "title": "Invented", "year": "1999",
             "url": "", "doi": ""},
        ]}

    citations = parsing.extract_citation_list(fake_llm, ESSAY)
    assert len(citations) == 1
    assert citations[0]["author"] == "Diaz"
    assert citations[0]["url"] == "https://example.org/diaz-2021"


def test_extract_citation_list_caps_at_max_citations():
    def fake_llm(system, prompt):
        return {"citations": [
            {"raw": ESSAY, "style": "mla", "author": "x", "title": "x", "year": "x",
             "url": "", "doi": ""}
        ] * (parsing.MAX_CITATIONS + 10)}

    citations = parsing.extract_citation_list(fake_llm, ESSAY)
    assert len(citations) == parsing.MAX_CITATIONS


def test_extract_citation_list_retries_once_then_succeeds():
    calls = {"n": 0}

    def flaky_llm(system, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"citations": [{"raw": "Diaz, Maria. \"Feedback Loops in Climate Models.\" "
                                     "Journal of Climate Science, 2021, https://example.org/diaz-2021.",
                              "style": "mla", "author": "Diaz", "title": "t", "year": "2021",
                              "url": "https://example.org/diaz-2021", "doi": ""}]}

    citations = parsing.extract_citation_list(flaky_llm, ESSAY)
    assert calls["n"] == 2
    assert len(citations) == 1


def test_extract_citation_list_empty_essay_short_circuits():
    calls = {"n": 0}

    def counting_llm(system, prompt):
        calls["n"] += 1
        return {"citations": []}

    assert parsing.extract_citation_list(counting_llm, "") == []
    assert calls["n"] == 0


def test_extract_cited_statements_drops_sentence_or_marker_not_verbatim():
    def fake_llm(system, prompt):
        return {"statements": [
            {"sentence": "Climate change is accelerating faster than expected (Diaz, 2021).",
             "marker": "(Diaz, 2021)"},
            {"sentence": "A sentence the model invented.", "marker": "(Nobody, 1900)"},
        ]}

    statements = parsing.extract_cited_statements(fake_llm, ESSAY)
    assert len(statements) == 1
    assert statements[0]["marker"] == "(Diaz, 2021)"


def test_extract_cited_statements_caps_at_max_statements():
    real_sentence = "Climate change is accelerating faster than expected (Diaz, 2021)."

    def fake_llm(system, prompt):
        return {"statements": [{"sentence": real_sentence, "marker": "(Diaz, 2021)"}]
               * (parsing.MAX_STATEMENTS + 5)}

    statements = parsing.extract_cited_statements(fake_llm, ESSAY)
    assert len(statements) == parsing.MAX_STATEMENTS
