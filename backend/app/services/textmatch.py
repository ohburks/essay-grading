"""Shared text-normalization for verbatim-quote verification.

Used by both the grading engine's evidence-provenance guard and the
citations fact-check guard, so "does this quote actually appear in the
source" means exactly the same thing everywhere it's checked.
"""

import re


def normalize_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\"'‘’“”]", "'", s)
    return s.lower()
