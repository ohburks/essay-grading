"""Integration tests for citations.vectorstore against a real ChromaDB
instance (downloads the small default-embedding ONNX model on first run) --
excluded from the default test run (see pyproject.toml addopts) so CI
without network/the citations extra installed stays green. Run explicitly
with `pytest -m integration`."""

import pytest

pytest.importorskip("chromadb")

pytestmark = pytest.mark.integration

from app.services.citations import vectorstore


def test_is_available_true_when_chromadb_installed():
    assert vectorstore.is_available() is True


def test_chunk_text_overlaps_and_covers_whole_text():
    text = "word " * 1000
    chunks = vectorstore.chunk_text(text)
    assert len(chunks) > 1
    assert "".join(chunks).count("word") >= text.count("word")


def test_chunk_text_empty_input():
    assert vectorstore.chunk_text("") == []
    assert vectorstore.chunk_text("   ") == []


def test_index_and_query_source_is_idempotent_and_filters_by_source_key():
    key_a = "test-source-a"
    key_b = "test-source-b"
    vectorstore.index_source(key_a, "The sky is blue and the ocean is deep.")
    vectorstore.index_source(key_b, "Paris is the capital of France and a major city.")

    # Re-indexing the same key must not duplicate chunks.
    count_first = vectorstore.index_source(key_a, "The sky is blue and the ocean is deep.")
    count_second = vectorstore.index_source(key_a, "The sky is blue and the ocean is deep.")
    assert count_first == count_second

    results_a = vectorstore.query_source(key_a, "What color is the sky?")
    assert results_a
    assert all("blue" in r["text"] or "ocean" in r["text"] for r in results_a)

    results_b = vectorstore.query_source(key_b, "What color is the sky?", k=5)
    # Metadata filter must keep A's chunks out of B's results even though B
    # was queried with an A-shaped question.
    assert all("France" in r["text"] or "Paris" in r["text"] for r in results_b)


def test_query_source_empty_for_unindexed_key():
    assert vectorstore.query_source("never-indexed-key", "anything") == []
