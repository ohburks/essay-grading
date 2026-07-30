"""
SQLite database layer for the unified assessment platform.

Design lineage: Performative_Assessment_V5 database.py (assessmentRework branch).
Carries forward its two load-bearing conventions:

  * CREATE TABLE IF NOT EXISTS + an idempotent PRAGMA table_info -> ALTER TABLE
    ADD COLUMN loop, so existing databases widen safely as new report-facing
    evidence becomes exportable (no migration framework needed).
  * export_schema_version stamped on every assessment row, so longitudinal
    research exports can always tell which dictionary a row was written under.

New in the consolidated platform:

  * an `assessments` spine for essay+trace grading, with raw inputs in
    artifacts_json,
  * `score_records` — per-criterion x channel rows (the TGFWA ScoreRecord
    model, one claim per row),
  * `assessment_runs` — DB-backed live session state (replaces the V5
    in-memory _state dict, so runs survive restarts),
  * `auth_sessions` — opaque-token auth (replaces Flask signed cookies),
  * versioned `content_items` for rubrics.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash

DATA_DIR = Path(os.environ.get("ASSESSMENT_DATA_DIR",
                               Path(__file__).resolve().parents[2] / "data"))
DB_FILE = Path(os.environ.get("ASSESSMENT_DB_PATH", DATA_DIR / "assessments.db"))

VALID_ROLES = ("admin", "instructor", "student")
VALID_MODES = ("essay_trace",)
VALID_CONTENT_KINDS = ("rubric",)

# pbkdf2 relies only on hashlib.pbkdf2_hmac (present in every Python build).
# werkzeug's default of scrypt needs OpenSSL-with-scrypt, which the macOS
# system Python (linked against LibreSSL) lacks — so pin pbkdf2 for portability.
_HASH_METHOD = "pbkdf2:sha256"

EXPORT_SCHEMA_VERSION = "3"

_SEED_USERS = [
    # (username, password, role, display_name)
    ("admin",      "admin123",   "admin",      "Administrator"),
    ("instructor", "Teach@2024", "instructor", "Instructor Demo"),
    ("emma",  "Learn@2024", "student", "Emma Clarke"),
    ("liam",  "Learn@2024", "student", "Liam Patel"),
    ("sofia", "Learn@2024", "student", "Sofia Nguyen"),
    ("james", "Learn@2024", "student", "James Okafor"),
    ("priya", "Learn@2024", "student", "Priya Singh"),
    ("tyler", "Learn@2024", "student", "Tyler Brooke"),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return uuid.uuid4().hex


def _conn():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_FILE))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _widen(c, table: str, columns: dict):
    """Idempotent column additions: the V5 pattern for schema evolution."""
    existing = {row["name"] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, decl in columns.items():
        if name not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username           TEXT PRIMARY KEY,
                password_hash      TEXT NOT NULL,
                role               TEXT NOT NULL CHECK(role IN ('admin','instructor','student')),
                display_name       TEXT NOT NULL,
                theme              TEXT NOT NULL DEFAULT 'light',
                preferred_provider TEXT NOT NULL DEFAULT '',
                preferred_model    TEXT NOT NULL DEFAULT '',
                created_at         TEXT NOT NULL DEFAULT ''
            )
        """)
        _widen(c, "users", {"grading_style": "TEXT NOT NULL DEFAULT ''",
                            "style_intensity": "TEXT NOT NULL DEFAULT 'moderate'"})
        c.execute("""
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                username   TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS content_items (
                kind       TEXT NOT NULL CHECK(kind IN ('rubric')),
                content_id TEXT NOT NULL,
                version    TEXT NOT NULL,
                payload    TEXT NOT NULL,
                active     INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (kind, content_id, version)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS assessments (
                id              TEXT PRIMARY KEY,
                username        TEXT NOT NULL,
                mode            TEXT NOT NULL CHECK(mode IN ('essay_trace')),
                status          TEXT NOT NULL DEFAULT 'draft'
                                CHECK(status IN ('draft','in_progress','grading','graded','error')),
                name            TEXT NOT NULL DEFAULT '',
                description     TEXT NOT NULL DEFAULT '',
                content_id      TEXT NOT NULL DEFAULT '',
                content_version TEXT NOT NULL DEFAULT '',
                artifacts       TEXT NOT NULL DEFAULT '{}',
                is_exemplar     INTEGER NOT NULL DEFAULT 0,
                graded_live     INTEGER NOT NULL DEFAULT 0,
                export_schema_version TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                completed_at    TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_assessments_user ON assessments(username)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_assessments_mode ON assessments(mode)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS score_records (
                assessment_id      TEXT NOT NULL,
                criterion_id       TEXT NOT NULL,
                channel            TEXT NOT NULL CHECK(channel IN ('trace','product')),
                passes             TEXT NOT NULL DEFAULT '[]',
                median             REAL,
                spread             REAL,
                no_evidence        INTEGER NOT NULL DEFAULT 0,
                confidence         TEXT NOT NULL DEFAULT 'low',
                evidence           TEXT NOT NULL DEFAULT '[]',
                anchor_matched     TEXT NOT NULL DEFAULT '',
                rubric_version     TEXT NOT NULL DEFAULT '',
                graded_at          TEXT NOT NULL DEFAULT '',
                needs_review       INTEGER NOT NULL DEFAULT 0,
                review_reasons     TEXT NOT NULL DEFAULT '[]',
                override_score     REAL,
                override_rationale TEXT NOT NULL DEFAULT '',
                override_ts        TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (assessment_id, criterion_id, channel)
            )
        """)
        _widen(c, "score_records", {"style_applied": "TEXT NOT NULL DEFAULT ''",
                                    "style_hash": "TEXT NOT NULL DEFAULT ''"})
        c.execute("""
            CREATE TABLE IF NOT EXISTS layer_b_results (
                assessment_id         TEXT PRIMARY KEY,
                result                TEXT NOT NULL,
                dominant_help_seeking TEXT NOT NULL DEFAULT '',
                dominant_response_use TEXT NOT NULL DEFAULT '',
                interpretive_label    TEXT NOT NULL DEFAULT '',
                verification_rate     REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS assessment_runs (
                assessment_id TEXT PRIMARY KEY,
                state         TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                expires_at    TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                assessment_id TEXT NOT NULL,
                kind          TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'running'
                              CHECK(status IN ('running','done','error')),
                done          INTEGER NOT NULL DEFAULT 0,
                total         INTEGER NOT NULL DEFAULT 0,
                label         TEXT NOT NULL DEFAULT '',
                error         TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS llm_eval_cache (
                key        TEXT PRIMARY KEY,
                response   TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS style_molds (
                cache_key  TEXT PRIMARY KEY,
                content_id TEXT NOT NULL,
                version    TEXT NOT NULL,
                style_hash TEXT NOT NULL,
                notes_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS cited_sources (
                source_key        TEXT PRIMARY KEY,
                style              TEXT NOT NULL DEFAULT '',
                raw_citation       TEXT NOT NULL DEFAULT '',
                author             TEXT NOT NULL DEFAULT '',
                title              TEXT NOT NULL DEFAULT '',
                year               TEXT NOT NULL DEFAULT '',
                citation_url       TEXT NOT NULL DEFAULT '',
                doi                TEXT NOT NULL DEFAULT '',
                resolved_url       TEXT NOT NULL DEFAULT '',
                resolution_method  TEXT NOT NULL DEFAULT '',
                status             TEXT NOT NULL DEFAULT 'pending'
                                   CHECK(status IN ('pending','fetched','indexed','unverifiable','error')),
                error              TEXT NOT NULL DEFAULT '',
                chunk_count        INTEGER NOT NULL DEFAULT 0,
                fetched_at         TEXT NOT NULL DEFAULT '',
                indexed_at         TEXT NOT NULL DEFAULT '',
                created_at         TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS assessment_citations (
                assessment_id TEXT NOT NULL,
                ordinal       INTEGER NOT NULL,
                source_key    TEXT NOT NULL,
                raw_citation  TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (assessment_id, ordinal)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_assessment_citations_source "
                  "ON assessment_citations(source_key)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS fact_check_results (
                assessment_id     TEXT NOT NULL,
                statement_index   INTEGER NOT NULL,
                statement_text    TEXT NOT NULL DEFAULT '',
                citation_marker   TEXT NOT NULL DEFAULT '',
                source_key        TEXT NOT NULL DEFAULT '',
                match_confidence  TEXT NOT NULL DEFAULT '',
                verdict           TEXT NOT NULL DEFAULT 'unchecked'
                                  CHECK(verdict IN ('supported','contradicted','not_addressed',
                                                    'unverifiable_source','unchecked')),
                evidence_quote    TEXT NOT NULL DEFAULT '',
                evidence_chunk_id TEXT NOT NULL DEFAULT '',
                reasoning         TEXT NOT NULL DEFAULT '',
                checked_at        TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (assessment_id, statement_index)
            )
        """)
        c.commit()


def seed_default_users():
    """Populate the DB with demo accounts on first run (idempotent)."""
    init_db()
    with _conn() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            return False
        c.executemany(
            "INSERT OR IGNORE INTO users "
            "(username,password_hash,role,display_name,theme,preferred_provider,"
            " preferred_model,created_at) VALUES (?,?,?,?,?,?,?,?)",
            [(u, generate_password_hash(p, method=_HASH_METHOD), r, n, "light", "", "", utcnow())
             for u, p, r, n in _SEED_USERS],
        )
        c.commit()
    return True


# ── User CRUD ─────────────────────────────────────────────────────────────────

def get_user(username: str):
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def all_users():
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY role, display_name").fetchall()
        return [dict(r) for r in rows]


def create_user(username: str, password: str, role: str, display_name: str):
    if role not in VALID_ROLES:
        return False, "Invalid role."
    with _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return False, "That username is already taken."
        c.execute(
            "INSERT INTO users (username,password_hash,role,display_name,created_at) "
            "VALUES (?,?,?,?,?)",
            (username, generate_password_hash(password, method=_HASH_METHOD),
             role, display_name, utcnow()),
        )
        c.commit()
    return True, None


def set_password(username: str, new_password: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE users SET password_hash=? WHERE username=?",
            (generate_password_hash(new_password, method=_HASH_METHOD), username),
        )
        c.commit()
        return cur.rowcount > 0


def update_user(old_username: str, new_username: str, display_name: str, role: str):
    """Update a user's username (PK), display name, and role.

    Returns (True, None) on success or (False, error_message) on failure.
    """
    if role not in VALID_ROLES:
        return False, "Invalid role."
    with _conn() as c:
        existing = c.execute(
            "SELECT role FROM users WHERE username=?", (old_username,)
        ).fetchone()
        if not existing:
            return False, "User not found."
        # Block removing the final admin (demotion or rename both count).
        if existing["role"] == "admin" and role != "admin":
            others = c.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND username!=?",
                (old_username,),
            ).fetchone()[0]
            if others == 0:
                return False, "Cannot demote the last remaining admin."
        if new_username != old_username:
            if c.execute("SELECT 1 FROM users WHERE username=?", (new_username,)).fetchone():
                return False, "That username is already taken."
        try:
            c.execute(
                "UPDATE users SET username=?, display_name=?, role=? WHERE username=?",
                (new_username, display_name, role, old_username),
            )
            c.execute("UPDATE assessments SET username=? WHERE username=?",
                      (new_username, old_username))
            c.execute("UPDATE auth_sessions SET username=? WHERE username=?",
                      (new_username, old_username))
        except sqlite3.IntegrityError:
            return False, "Could not update user (constraint violation)."
        c.commit()
        return True, None


def set_theme(username: str, theme: str):
    if theme not in ("light", "dark"):
        return
    with _conn() as c:
        c.execute("UPDATE users SET theme=? WHERE username=?", (theme, username))
        c.commit()


def set_model_pref(username: str, provider: str, model: str):
    with _conn() as c:
        c.execute(
            "UPDATE users SET preferred_provider=?, preferred_model=? WHERE username=?",
            (provider or "", model or "", username),
        )
        c.commit()


def set_grading_style(username: str, style: str):
    with _conn() as c:
        c.execute("UPDATE users SET grading_style=? WHERE username=?", (style or "", username))
        c.commit()


def set_style_intensity(username: str, intensity: str):
    with _conn() as c:
        c.execute("UPDATE users SET style_intensity=? WHERE username=?", (intensity, username))
        c.commit()


# ── Auth sessions (opaque token, hash at rest) ────────────────────────────────

SESSION_TTL_DAYS = 14


def create_auth_session(token_hash: str, username: str):
    with _conn() as c:
        expires = (datetime.now(timezone.utc)
                   + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        c.execute(
            "INSERT INTO auth_sessions (token_hash, username, created_at, expires_at) "
            "VALUES (?,?,?,?)",
            (token_hash, username, utcnow(), expires),
        )
        c.commit()


def get_auth_session(token_hash: str):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM auth_sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < utcnow():
            c.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))
            c.commit()
            return None
        return dict(row)


def delete_auth_session(token_hash: str):
    with _conn() as c:
        c.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))
        c.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (utcnow(),))
        c.commit()


# ── Content items (versioned rubrics) ──────────────────────────────────────────

def upsert_content(kind: str, content_id: str, version: str, payload: dict,
                   created_by: str = "", active: bool = True):
    if kind not in VALID_CONTENT_KINDS:
        raise ValueError(f"invalid content kind: {kind}")
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO content_items "
            "(kind, content_id, version, payload, active, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (kind, content_id, version, json.dumps(payload), 1 if active else 0,
             created_by, utcnow()),
        )
        c.commit()


def get_content(kind: str, content_id: str, version: str = None):
    """Fetch one content item; latest version (by created_at, then version) if unspecified."""
    with _conn() as c:
        if version:
            row = c.execute(
                "SELECT * FROM content_items WHERE kind=? AND content_id=? AND version=?",
                (kind, content_id, version),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM content_items WHERE kind=? AND content_id=? AND active=1 "
                "ORDER BY created_at DESC, version DESC LIMIT 1",
                (kind, content_id),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d


def list_content(kind: str):
    """Latest active version of every content item of a kind."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM content_items WHERE kind=? AND active=1 "
            "ORDER BY content_id, created_at DESC, version DESC",
            (kind,),
        ).fetchall()
        latest = {}
        for r in rows:
            if r["content_id"] not in latest:
                d = dict(r)
                d["payload"] = json.loads(d["payload"])
                latest[r["content_id"]] = d
        return list(latest.values())


def list_content_versions(kind: str, content_id: str):
    with _conn() as c:
        rows = c.execute(
            "SELECT kind, content_id, version, created_by, created_at FROM content_items "
            "WHERE kind=? AND content_id=? ORDER BY created_at DESC",
            (kind, content_id),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Assessments spine ─────────────────────────────────────────────────────────

def create_assessment(username: str, mode: str, name: str = "", description: str = "",
                      content_id: str = "", content_version: str = "",
                      artifacts: dict = None, is_exemplar: bool = False,
                      status: str = "draft", assessment_id: str = None):
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode: {mode}")
    aid = assessment_id or new_id()
    with _conn() as c:
        c.execute(
            "INSERT INTO assessments (id, username, mode, status, name, description, "
            "content_id, content_version, artifacts, is_exemplar, export_schema_version, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, username, mode, status, name, description, content_id, content_version,
             json.dumps(artifacts or {}), 1 if is_exemplar else 0,
             EXPORT_SCHEMA_VERSION, utcnow()),
        )
        c.commit()
    return aid


def _assessment_from_row(row):
    d = dict(row)
    d["artifacts"] = json.loads(d["artifacts"] or "{}")
    d["is_exemplar"] = bool(d["is_exemplar"])
    d["graded_live"] = bool(d["graded_live"])
    return d


def get_assessment(assessment_id: str):
    with _conn() as c:
        row = c.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
        return _assessment_from_row(row) if row else None


def list_assessments(username: str = None, mode: str = None):
    q, params = "SELECT * FROM assessments", []
    clauses = []
    if username:
        clauses.append("username=?")
        params.append(username)
    if mode:
        clauses.append("mode=?")
        params.append(mode)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC"
    with _conn() as c:
        return [_assessment_from_row(r) for r in c.execute(q, params).fetchall()]


def update_assessment(assessment_id: str, **fields):
    allowed = {"status", "name", "description", "artifacts", "completed_at",
               "graded_live", "content_version"}
    updates, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"cannot update field: {k}")
        if k == "artifacts":
            v = json.dumps(v)
        if k == "graded_live":
            v = 1 if v else 0
        updates.append(f"{k}=?")
        params.append(v)
    if not updates:
        return
    params.append(assessment_id)
    with _conn() as c:
        c.execute(f"UPDATE assessments SET {', '.join(updates)} WHERE id=?", params)
        c.commit()


def delete_assessment(assessment_id: str):
    with _conn() as c:
        # Note: cited_sources is NOT cleared here -- it's a global, dedup'd
        # cache other assessments may still reference.
        for table in ("score_records", "layer_b_results", "assessment_runs", "jobs",
                     "assessment_citations", "fact_check_results"):
            c.execute(f"DELETE FROM {table} WHERE assessment_id=?", (assessment_id,))
        c.execute("DELETE FROM assessments WHERE id=?", (assessment_id,))
        c.commit()


# ── Score records (Mode A: one claim per row) ─────────────────────────────────

def upsert_score_record(assessment_id: str, rec: dict):
    """rec follows the TGFWA ScoreRecord shape (snake_case keys)."""
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO score_records "
            "(assessment_id, criterion_id, channel, passes, median, spread, no_evidence, "
            " confidence, evidence, anchor_matched, rubric_version, graded_at, "
            " needs_review, review_reasons, override_score, override_rationale, override_ts, "
            " style_applied, style_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (assessment_id, rec["criterion_id"], rec["channel"],
             json.dumps(rec.get("passes", [])), rec.get("median"), rec.get("spread"),
             1 if rec.get("no_evidence") else 0, rec.get("confidence", "low"),
             json.dumps(rec.get("evidence", [])), rec.get("anchor_matched", "") or "",
             rec.get("rubric_version", ""), rec.get("graded_at", utcnow()),
             1 if rec.get("needs_review") else 0,
             json.dumps(rec.get("review_reasons", [])),
             rec.get("override_score"), rec.get("override_rationale", "") or "",
             rec.get("override_ts", "") or "",
             rec.get("style_applied", "") or "", rec.get("style_hash", "") or ""),
        )
        c.commit()


def _score_record_from_row(row):
    d = dict(row)
    d["passes"] = json.loads(d["passes"])
    d["evidence"] = json.loads(d["evidence"])
    d["review_reasons"] = json.loads(d["review_reasons"])
    d["no_evidence"] = bool(d["no_evidence"])
    d["needs_review"] = bool(d["needs_review"])
    return d


def get_score_records(assessment_id: str):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM score_records WHERE assessment_id=? "
            "ORDER BY criterion_id, channel",
            (assessment_id,),
        ).fetchall()
        return [_score_record_from_row(r) for r in rows]


def delete_score_records(assessment_id: str):
    with _conn() as c:
        c.execute("DELETE FROM score_records WHERE assessment_id=?", (assessment_id,))
        c.commit()


def set_score_override(assessment_id: str, criterion_id: str, channel: str,
                       score: float, rationale: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE score_records SET override_score=?, override_rationale=?, override_ts=? "
            "WHERE assessment_id=? AND criterion_id=? AND channel=?",
            (score, rationale, utcnow(), assessment_id, criterion_id, channel),
        )
        c.commit()
        return cur.rowcount > 0


def clear_score_override(assessment_id: str, criterion_id: str, channel: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE score_records SET override_score=NULL, override_rationale='', override_ts='' "
            "WHERE assessment_id=? AND criterion_id=? AND channel=?",
            (assessment_id, criterion_id, channel),
        )
        c.commit()
        return cur.rowcount > 0


def review_queue():
    """All score records routed to instructor judgment, unresolved first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT sr.*, a.username, a.name AS assessment_name FROM score_records sr "
            "JOIN assessments a ON a.id = sr.assessment_id "
            "WHERE sr.needs_review=1 ORDER BY (sr.override_ts != '') ASC, sr.graded_at DESC"
        ).fetchall()
        return [_score_record_from_row(r) for r in rows]


def override_corpus():
    """Every overridden score record — the labeled calibration dataset
    (TGFWA exportOverrideCorpus, now cross-user and durable)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT sr.*, a.username FROM score_records sr "
            "JOIN assessments a ON a.id = sr.assessment_id "
            "WHERE sr.override_ts != '' ORDER BY sr.override_ts"
        ).fetchall()
        return [_score_record_from_row(r) for r in rows]


# ── Layer B results ───────────────────────────────────────────────────────────

def upsert_layer_b(assessment_id: str, result: dict):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO layer_b_results "
            "(assessment_id, result, dominant_help_seeking, dominant_response_use, "
            " interpretive_label, verification_rate) VALUES (?,?,?,?,?,?)",
            (assessment_id, json.dumps(result),
             result.get("dominantHelpSeeking", ""), result.get("dominantResponseUse", ""),
             result.get("interpretiveLabel", ""), result.get("verificationRate")),
        )
        c.commit()


def get_layer_b(assessment_id: str):
    with _conn() as c:
        row = c.execute(
            "SELECT result FROM layer_b_results WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        return json.loads(row["result"]) if row else None


# ── Grading reliability (Mode A: LLM vs instructor overrides) ─────────────────

def mode_a_reliability_stats():
    """LLM-vs-instructor calibration for essay/trace grading, derived entirely
    from score_records — no separate annotation step. An instructor override
    is itself the verdict: how often routed-for-judgment criteria get resolved,
    and by how much the teacher's score differs from the LLM's median once they
    do (a high average delta means the LLM is miscalibrated on that criterion).
    """
    with _conn() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM score_records WHERE graded_at != ''"
        ).fetchone()[0]
        needs_review = c.execute(
            "SELECT COUNT(*) FROM score_records WHERE needs_review=1"
        ).fetchone()[0]
        overridden = c.execute(
            "SELECT COUNT(*) FROM score_records WHERE override_ts != ''"
        ).fetchone()[0]
        avg_delta = c.execute(
            "SELECT AVG(ABS(override_score - median)) FROM score_records "
            "WHERE override_ts != '' AND median IS NOT NULL"
        ).fetchone()[0]

        criterion_rows = c.execute(
            "SELECT criterion_id, COUNT(*) AS total, "
            "  SUM(needs_review) AS needs_review, "
            "  SUM(override_ts != '') AS overridden, "
            "  AVG(CASE WHEN override_ts != '' AND median IS NOT NULL "
            "       THEN ABS(override_score - median) END) AS avg_delta "
            "FROM score_records GROUP BY criterion_id"
        ).fetchall()
        by_criterion = []
        for r in criterion_rows:
            d = dict(r)
            d["resolution_rate"] = (d["overridden"] / d["needs_review"]) if d["needs_review"] else None
            d["avg_delta"] = round(d["avg_delta"], 2) if d["avg_delta"] is not None else None
            by_criterion.append(d)
        # worst-miscalibration first; criteria with no delta data sink to the bottom
        by_criterion.sort(key=lambda d: (d["avg_delta"] is None, -(d["avg_delta"] or 0)))

        recent = [dict(r) for r in c.execute(
            "SELECT sr.criterion_id, sr.channel, sr.median, sr.override_score, "
            "       sr.override_rationale, sr.override_ts, a.username, "
            "       a.name AS assessment_name "
            "FROM score_records sr JOIN assessments a ON a.id = sr.assessment_id "
            "WHERE sr.override_ts != '' ORDER BY sr.override_ts DESC LIMIT 20"
        ).fetchall()]

    return {
        "total": total,
        "needs_review": needs_review,
        "overridden": overridden,
        "resolution_rate": (overridden / needs_review) if needs_review else None,
        "avg_override_delta": round(avg_delta, 2) if avg_delta is not None else None,
        "by_criterion": by_criterion,
        "recent": recent,
    }


# ── Live run state (replaces V5 in-memory _state/_fr_state dicts) ─────────────

RUN_TTL_HOURS = 12


def save_run_state(assessment_id: str, state: dict):
    with _conn() as c:
        expires = (datetime.now(timezone.utc)
                   + timedelta(hours=RUN_TTL_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        c.execute(
            "INSERT OR REPLACE INTO assessment_runs (assessment_id, state, updated_at, expires_at) "
            "VALUES (?,?,?,?)",
            (assessment_id, json.dumps(state), utcnow(), expires),
        )
        c.commit()


def load_run_state(assessment_id: str):
    with _conn() as c:
        c.execute("DELETE FROM assessment_runs WHERE expires_at < ?", (utcnow(),))
        row = c.execute(
            "SELECT state FROM assessment_runs WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        c.commit()
        return json.loads(row["state"]) if row else None


def delete_run_state(assessment_id: str):
    with _conn() as c:
        c.execute("DELETE FROM assessment_runs WHERE assessment_id=?", (assessment_id,))
        c.commit()


# ── Jobs (grading progress; SSE + polling fallback) ───────────────────────────

def create_job(assessment_id: str, kind: str, total: int = 0):
    jid = new_id()
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, assessment_id, kind, status, done, total, label, "
            "created_at, updated_at) VALUES (?,?,?,'running',0,?,'',?,?)",
            (jid, assessment_id, kind, total, utcnow(), utcnow()),
        )
        c.commit()
    return jid


def update_job(job_id: str, done: int = None, total: int = None, label: str = None,
               status: str = None, error: str = None):
    updates, params = ["updated_at=?"], [utcnow()]
    for col, val in (("done", done), ("total", total), ("label", label),
                     ("status", status), ("error", error)):
        if val is not None:
            updates.append(f"{col}=?")
            params.append(val)
    params.append(job_id)
    with _conn() as c:
        c.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id=?", params)
        c.commit()


def get_job(job_id: str):
    with _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


# ── LLM evaluative-call cache ─────────────────────────────────────────────────
# Determinism/testing aid only (see core.llm.cached_evaluative_call) -- not a
# cost-saving cache. Identical (model, base_url, prompt_version, prompt) input
# always returns the same stored response, so repeated test runs stay reproducible.

_EVAL_CACHE_MAX_ROWS = 2000


def eval_cache_get(key: str):
    with _conn() as c:
        row = c.execute("SELECT response FROM llm_eval_cache WHERE key=?", (key,)).fetchone()
        return row["response"] if row else None


def eval_cache_set(key: str, response: str):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO llm_eval_cache (key, response, created_at) "
            "VALUES (?, ?, datetime('now'))",
            (key, response),
        )
        excess = c.execute("SELECT COUNT(*) FROM llm_eval_cache").fetchone()[0] - _EVAL_CACHE_MAX_ROWS
        if excess > 0:
            c.execute(
                "DELETE FROM llm_eval_cache WHERE key IN ("
                "  SELECT key FROM llm_eval_cache ORDER BY created_at ASC, key ASC LIMIT ?)",
                (excess,),
            )
        c.commit()


# ── Style-mold cache (attempt 5: per-criterion grading-style reconciliation
# notes, molded once per rubric-version × style-text pair from the pristine
# rubric baseline, never from a prior mold — see molding.get_or_mold_notes) ──

def get_style_mold(key: str):
    with _conn() as c:
        row = c.execute("SELECT notes_json FROM style_molds WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row["notes_json"]) if row else None


def set_style_mold(key: str, content_id: str, version: str, style_hash: str, notes: dict):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO style_molds "
            "(cache_key, content_id, version, style_hash, notes_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (key, content_id, version, style_hash, json.dumps(notes)),
        )
        c.commit()


# ── Cited-sources cache (global, dedup'd document cache for citation
# fact-checking -- see services.citations.pipeline.get_or_fetch_source) ──────

def upsert_cited_source(source_key: str, *, style: str = "", raw_citation: str = "",
                        author: str = "", title: str = "", year: str = "",
                        citation_url: str = "", doi: str = ""):
    """Insert-if-absent only: does not overwrite an existing row's fetch/index
    state. Use update_cited_source for status transitions."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO cited_sources "
            "(source_key, style, raw_citation, author, title, year, citation_url, "
            " doi, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (source_key, style, raw_citation, author, title, year, citation_url,
             doi, utcnow()),
        )
        c.commit()


def update_cited_source(source_key: str, **fields):
    allowed = {"resolved_url", "resolution_method", "status", "error",
              "chunk_count", "fetched_at", "indexed_at"}
    updates, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"cannot update field: {k}")
        updates.append(f"{k}=?")
        params.append(v)
    if not updates:
        return
    params.append(source_key)
    with _conn() as c:
        c.execute(f"UPDATE cited_sources SET {', '.join(updates)} WHERE source_key=?", params)
        c.commit()


def get_cited_source(source_key: str):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM cited_sources WHERE source_key=?", (source_key,)
        ).fetchone()
        return dict(row) if row else None


def reset_cited_sources_for_retry(source_keys: list):
    if not source_keys:
        return
    with _conn() as c:
        c.executemany(
            "UPDATE cited_sources SET status='pending', error='' WHERE source_key=?",
            [(k,) for k in source_keys],
        )
        c.commit()


def link_assessment_citation(assessment_id: str, ordinal: int, source_key: str,
                             raw_citation: str):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO assessment_citations "
            "(assessment_id, ordinal, source_key, raw_citation, created_at) "
            "VALUES (?,?,?,?,?)",
            (assessment_id, ordinal, source_key, raw_citation, utcnow()),
        )
        c.commit()


def get_assessment_citations(assessment_id: str):
    with _conn() as c:
        rows = c.execute(
            "SELECT ac.assessment_id, ac.ordinal, ac.source_key, ac.raw_citation, "
            "       cs.resolved_url, cs.resolution_method, cs.status, cs.error, "
            "       cs.chunk_count "
            "FROM assessment_citations ac LEFT JOIN cited_sources cs "
            "  ON cs.source_key = ac.source_key "
            "WHERE ac.assessment_id=? ORDER BY ac.ordinal",
            (assessment_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_assessment_citations(assessment_id: str):
    with _conn() as c:
        c.execute("DELETE FROM assessment_citations WHERE assessment_id=?", (assessment_id,))
        c.commit()


def upsert_fact_check_result(assessment_id: str, statement_index: int, rec: dict):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO fact_check_results "
            "(assessment_id, statement_index, statement_text, citation_marker, "
            " source_key, match_confidence, verdict, evidence_quote, "
            " evidence_chunk_id, reasoning, checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (assessment_id, statement_index, rec.get("statement_text", ""),
             rec.get("citation_marker", ""), rec.get("source_key", ""),
             rec.get("match_confidence", ""), rec.get("verdict", "unchecked"),
             rec.get("evidence_quote", ""), rec.get("evidence_chunk_id", ""),
             rec.get("reasoning", ""), utcnow()),
        )
        c.commit()


def get_fact_check_results(assessment_id: str):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM fact_check_results WHERE assessment_id=? "
            "ORDER BY statement_index",
            (assessment_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_fact_check_results(assessment_id: str):
    with _conn() as c:
        c.execute("DELETE FROM fact_check_results WHERE assessment_id=?", (assessment_id,))
        c.commit()


