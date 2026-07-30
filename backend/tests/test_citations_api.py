"""End-to-end fact-check API: job kickoff, SSE-independent polling, result
shape, ownership, and the 409 feature-unavailable path. Everything that
would touch the network or a real vector store (resolution/fetch/
vectorstore) is monkeypatched so this suite never leaves the process."""

import time

from app.services import llm_bridge
from app.services.citations import fetch, resolution, vectorstore

ESSAY = (
    "Renewable energy adoption has grown sharply in the last decade (Lopez, 2022). "
    "Battery storage costs remain a limiting factor for grid-scale deployment.\n\n"
    "Works Cited\n"
    "Lopez, Ana. \"Trends in Renewable Adoption.\" Energy Policy Review, 2022, "
    "https://example.org/lopez-2022."
)


class FakeLLM:
    """Answers citation-list extraction, statement extraction, and verdict
    calls with canned, verbatim-matched responses."""

    def __call__(self, system, prompt):
        if "works-cited / references list" in system:
            return {"citations": [{
                "raw": "Lopez, Ana. \"Trends in Renewable Adoption.\" Energy Policy Review, 2022, "
                      "https://example.org/lopez-2022.",
                "style": "mla", "author": "Lopez", "title": "Trends in Renewable Adoption",
                "year": "2022", "url": "https://example.org/lopez-2022", "doi": "",
            }]}
        if "carry an inline citation marker" in system:
            return {"statements": [{
                "sentence": "Renewable energy adoption has grown sharply in the last decade (Lopez, 2022).",
                "marker": "(Lopez, 2022)",
            }]}
        # fact-check verdict call
        return {"verdict": "supported", "quote": "adoption grew sharply",
               "chunkId": "fake-chunk-0", "reasoning": "Directly supports the claim."}


def _create_essay_assessment(client, essay=ESSAY):
    r = client.post("/api/assessments", json={"mode": "essay_trace", "name": "fact-check test",
                                              "artifacts": {"essay": essay}},
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _run_and_wait(client, assessment_id, force=False, timeout=30):
    path = f"/api/assessments/{assessment_id}/fact-check"
    if force:
        path += "?force=true"
    r = client.post(path, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200, r.text
    job_id = r.json()["jobId"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish in time")


def test_fact_check_end_to_end(admin_client, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm_bridge, "make_llm_json", lambda user, override=None: fake)
    monkeypatch.setattr(resolution, "resolve_citation",
                        lambda entry: {"resolved_url": entry["url"], "method": "direct_url",
                                      "doi": None, "error": ""})
    monkeypatch.setattr(fetch, "fetch_and_extract",
                        lambda url: {"text": "Renewable adoption grew sharply this decade. " * 20,
                                    "content_type": "html", "final_url": url})
    monkeypatch.setattr(vectorstore, "index_source", lambda key, text: 3)
    monkeypatch.setattr(vectorstore, "query_source",
                        lambda key, query, k=5: [{"chunk_id": "fake-chunk-0",
                                                 "text": "Renewable adoption grew sharply this decade."}])

    aid = _create_essay_assessment(admin_client)
    job = _run_and_wait(admin_client, aid)
    assert job["status"] == "done", job

    result = admin_client.get(f"/api/assessments/{aid}/fact-check").json()
    assert len(result["citations"]) == 1
    assert result["citations"][0]["status"] == "indexed"
    assert result["citations"][0]["resolvedUrl"] == "https://example.org/lopez-2022"

    assert len(result["statements"]) == 1
    assert result["statements"][0]["verdict"] == "supported"
    assert result["statements"][0]["evidenceQuote"] == "adoption grew sharply"


def test_fact_check_no_essay_is_422(admin_client):
    aid = _create_essay_assessment(admin_client, essay="")
    r = admin_client.post(f"/api/assessments/{aid}/fact-check",
                          headers={"X-Requested-With": "fetch"})
    assert r.status_code == 422


def test_fact_check_without_provider_is_409(admin_client, monkeypatch):
    def raise_unconfigured(user, override=None):
        raise llm_bridge.LLMNotConfigured("No LLM provider is configured on the server.")
    monkeypatch.setattr(llm_bridge, "make_llm_json", raise_unconfigured)
    aid = _create_essay_assessment(admin_client)
    r = admin_client.post(f"/api/assessments/{aid}/fact-check",
                          headers={"X-Requested-With": "fetch"})
    assert r.status_code == 409


def test_fact_check_vectorstore_unavailable_is_409(admin_client, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm_bridge, "make_llm_json", lambda user, override=None: fake)
    monkeypatch.setattr(vectorstore, "is_available", lambda: False)
    aid = _create_essay_assessment(admin_client)
    r = admin_client.post(f"/api/assessments/{aid}/fact-check",
                          headers={"X-Requested-With": "fetch"})
    assert r.status_code == 409


def test_fact_check_no_citations_found_is_422(admin_client, monkeypatch):
    class NoCitationsLLM:
        def __call__(self, system, prompt):
            return {"citations": []} if "works-cited" in system else {"statements": []}

    monkeypatch.setattr(llm_bridge, "make_llm_json", lambda user, override=None: NoCitationsLLM())
    aid = _create_essay_assessment(admin_client, essay="An essay with no citations at all.")
    r = admin_client.post(f"/api/assessments/{aid}/fact-check",
                          headers={"X-Requested-With": "fetch"})
    assert r.status_code == 422


def test_fact_check_other_students_assessment_is_404(admin_client, student_client, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm_bridge, "make_llm_json", lambda user, override=None: fake)
    aid = _create_essay_assessment(admin_client)
    # admin_client owns it; a different, unrelated student should get 404, not
    # a 200/403 that would leak the assessment's existence.
    r = student_client.post(f"/api/assessments/{aid}/fact-check",
                            headers={"X-Requested-With": "fetch"})
    assert r.status_code == 404
    r = student_client.get(f"/api/assessments/{aid}/fact-check")
    assert r.status_code == 404
