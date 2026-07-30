"""Citation fact-check routes: extract an essay's works-cited list, resolve
and cache each source, index it into Chroma, and fact-check in-text-cited
statements against it. Mirrors the job-kickoff + SSE pattern used by
POST /assessments/{id}/grade (api/grading.py) -- services.jobs is fully
generic and needs no changes to support this second job kind.
"""

from fastapi import APIRouter, Depends, HTTPException

from ..core import security
from ..db import database as db
from ..services import jobs, llm_bridge
from ..services.citations import parsing, pipeline, vectorstore

router = APIRouter(prefix="/api", tags=["citations"])


def _get_owned_assessment(assessment_id: str, user: dict) -> dict:
    a = db.get_assessment(assessment_id)
    if not a or (user["role"] not in ("admin", "instructor")
                and a["username"] != user["username"]):
        raise HTTPException(status_code=404, detail="Assessment not found.")
    return a


def _citation_out(row: dict) -> dict:
    return {
        "ordinal": row["ordinal"],
        "sourceKey": row["source_key"],
        "rawCitation": row["raw_citation"],
        "resolvedUrl": row.get("resolved_url") or "",
        "resolutionMethod": row.get("resolution_method") or "",
        "status": row.get("status") or "pending",
        "error": row.get("error") or "",
        "chunkCount": row.get("chunk_count") or 0,
    }


def _result_out(row: dict) -> dict:
    return {
        "statementIndex": row["statement_index"],
        "statementText": row["statement_text"],
        "citationMarker": row["citation_marker"],
        "sourceKey": row["source_key"],
        "matchConfidence": row["match_confidence"],
        "verdict": row["verdict"],
        "evidenceQuote": row["evidence_quote"],
        "reasoning": row["reasoning"],
        "checkedAt": row["checked_at"],
    }


@router.post("/assessments/{assessment_id}/fact-check")
def fact_check(assessment_id: str, force: bool = False,
              user: dict = Depends(security.require_user),
              override: dict | None = Depends(llm_bridge.llm_override)):
    a = _get_owned_assessment(assessment_id, user)
    essay = a["artifacts"].get("essay", "")
    if not essay:
        raise HTTPException(status_code=422, detail="Assessment has no essay text.")

    try:
        llm_json = llm_bridge.make_llm_json(user, override)
    except llm_bridge.UnknownProvider as e:
        raise HTTPException(status_code=422, detail=str(e))
    except llm_bridge.LLMNotConfigured as e:
        raise HTTPException(status_code=409, detail=str(e))

    if not vectorstore.is_available():
        raise HTTPException(
            status_code=409,
            detail="Fact-checking requires the server's optional 'citations' extra "
                  "(chromadb, pypdf, beautifulsoup4, httpx).")

    citations = parsing.extract_citation_list(llm_json, essay)
    if not citations:
        raise HTTPException(status_code=422,
                            detail="No APA/MLA works-cited list could be found in this essay.")
    statements = parsing.extract_cited_statements(llm_json, essay)
    total = len(citations) + max(1, len(statements))

    prior = db.get_assessment_citations(assessment_id)
    db.delete_fact_check_results(assessment_id)
    db.delete_assessment_citations(assessment_id)
    if force:
        db.reset_cited_sources_for_retry([c["source_key"] for c in prior])

    job_id = jobs.start_job(
        assessment_id, "fact_check", total,
        lambda report: pipeline.run_fact_check(
            llm_json, assessment_id, essay, citations, statements, report))
    return {"jobId": job_id, "total": total}


@router.get("/assessments/{assessment_id}/fact-check")
def get_fact_check(assessment_id: str, user: dict = Depends(security.require_user)):
    _get_owned_assessment(assessment_id, user)
    return {
        "citations": [_citation_out(c) for c in db.get_assessment_citations(assessment_id)],
        "statements": [_result_out(r) for r in db.get_fact_check_results(assessment_id)],
    }
