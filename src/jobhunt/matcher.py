"""Title/location prefilter plus an optional Haiku relevance check."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .models import Criteria, Job, MatchResult

HAIKU_MODEL = "claude-haiku-4-5-20251001"

NO_LOCATION_TIER = 5
REMOTE_KEYWORDS = ("remote",)
# Opaque location rollups we can't pin to a city — Workday collapses a posting
# open in several places into "3 Locations" / "Multiple Locations". We must not
# silently drop these on the location gate (one of those places may be a good
# city); let the LLM judge instead.
_UNRESOLVED_LOCATION_RE = re.compile(
    r"^\s*(multiple locations|\d+\s+locations?)\s*$", re.IGNORECASE
)


def _contains_any(haystack: str, needles: list[str]) -> bool:
    return any(n.lower() in haystack for n in needles)


def location_tier(location: str | None, location_tiers: dict[str, list[str]]) -> int:
    """1-based index of the first tier whose terms appear in `location`; 5 if none.

    Tiers are evaluated in config order, so `remote` gets whatever tier the config
    assigns it (its own tier) rather than being lumped with "no location".
    """
    if not location:
        return NO_LOCATION_TIER
    loc = location.lower()
    for idx, terms in enumerate(location_tiers.values(), start=1):
        if _contains_any(loc, terms):
            return idx
    return NO_LOCATION_TIER


def _is_remote(location: str | None) -> bool:
    if not location:
        return False
    loc = location.lower()
    return any(k in loc for k in REMOTE_KEYWORDS)


def _is_unresolved_location(location: str | None) -> bool:
    """True when we can't pin the posting to a specific city.

    Covers a missing location and opaque multi-location rollups ("3 Locations").
    A *readable* location that simply isn't in our tiers (e.g. "China, Shanghai")
    is NOT unresolved — that one we can and do reject.
    """
    if not location or not location.strip():
        return True
    return bool(_UNRESOLVED_LOCATION_RE.match(location.strip()))


def dedupe_by_uid(jobs: list[Job]) -> list[Job]:
    """Drop duplicate postings within a single fetch, keeping first occurrence.

    Guards against pagination overlap (Workday) or an adapter returning the same
    posting twice, which would otherwise notify once each before the DB catches it.
    """
    seen: set[str] = set()
    unique: list[Job] = []
    for job in jobs:
        if job.uid not in seen:
            seen.add(job.uid)
            unique.append(job)
    return unique


def is_fresh(
    job: Job, freshness_minutes: float | None, now: datetime | None = None
) -> bool:
    """Whether a posting is recent enough to notify on.

    - Disabled (None/0)          -> always fresh (rely on the seen-store alone).
    - No posting timestamp        -> fresh (undated sources trust the seen-store).
    - Has a timestamp             -> fresh iff its age <= the window.

    Naive timestamps are treated as UTC. Future timestamps (clock skew) count
    as fresh.
    """
    if not freshness_minutes:
        return True
    posted = job.posted_at
    if posted is None:
        return True
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age = now - posted
    return age <= timedelta(minutes=freshness_minutes)


def prefilter(
    job: Job, criteria: Criteria, location_tiers: dict[str, list[str]]
) -> tuple[bool, int]:
    """Cheap, deterministic title/location gate.

    Returns (passes, location_tier). Title (lowercased) must contain >=1
    internship term AND >=1 role term AND none of the exclude terms. Location
    tier is computed regardless; if location_match_required and the tier is 5
    (and it isn't remote), the job is rejected.
    """
    title = (job.title or "").lower()

    tier = location_tier(job.location, location_tiers)

    if not _contains_any(title, criteria.internship_terms):
        return False, tier
    if not _contains_any(title, criteria.role_terms):
        return False, tier
    if _contains_any(title, criteria.exclude_terms):
        return False, tier

    if criteria.location_match_required and tier == NO_LOCATION_TIER:
        # Reject only readable-but-non-preferred locations; keep remote and
        # unresolved/opaque ones for the LLM to weigh in on.
        if not _is_remote(job.location) and not _is_unresolved_location(job.location):
            return False, tier

    return True, tier


# ---- LLM relevance check --------------------------------------------------

_SYSTEM = (
    "You are a strict relevance filter for software-engineering INTERNSHIP/co-op "
    "postings. Given a job and a short candidate summary, decide if it is a genuine "
    "SWE/dev/infra/security internship or co-op the candidate should apply to. "
    "Reject full-time/senior/new-grad roles and non-engineering roles. "
    "Respond with ONLY a JSON object: "
    '{"is_match": bool, "score": int 0-100, "reason": "one short sentence"}.'
)


def _build_prompt(job: Job, profile_summary: str) -> str:
    return (
        f"Candidate summary:\n{profile_summary}\n\n"
        f"Job title: {job.title}\n"
        f"Location: {job.location or 'unspecified'}\n"
        f"Description (truncated):\n{job.description or '(none)'}\n\n"
        "Return the JSON verdict now."
    )


def _parse_verdict(text: str) -> dict:
    """Defensively pull a JSON object out of the model's reply."""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in model reply: {text!r}")
    return json.loads(text[start : end + 1])


def llm_match(
    job: Job,
    profile_summary: str,
    location_tier_value: int,
    company_priority: int,
    *,
    client=None,
    model: str = HAIKU_MODEL,
    guard=None,
    max_tokens: int = 256,
) -> MatchResult:
    """One Haiku call classifying a prefiltered survivor.

    Spend/rate caps are enforced by `guard` *before* the call — a `BudgetExceeded`
    from the guard propagates to the caller (which must stop making calls). Any
    other error fails safe as a non-match so a single bad reply can't crash the run.
    """
    # Budget/rate gate runs first and is allowed to propagate (stops the run).
    if guard is not None:
        guard.before_call("match")

    if client is None:
        from anthropic import Anthropic

        from .security import get_api_key

        client = Anthropic(api_key=get_api_key())

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            messages=[{"role": "user", "content": _build_prompt(job, profile_summary)}],
        )
        if guard is not None:
            usage = getattr(resp, "usage", None)
            guard.record(
                model,
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
                kind="match",
            )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        verdict = _parse_verdict(text)
        return MatchResult(
            is_match=bool(verdict.get("is_match", False)),
            score=int(verdict.get("score", 0)),
            reason=str(verdict.get("reason", "")).strip() or "no reason given",
            location_tier=location_tier_value,
            company_priority=company_priority,
        )
    except Exception as exc:  # noqa: BLE001 - never let one bad reply crash the scan
        if guard is not None:
            guard.record_error()
        # Fail safe: treat as a non-fatal non-match so the run continues.
        return MatchResult(
            is_match=False,
            score=0,
            reason=f"llm_match error: {exc}",
            location_tier=location_tier_value,
            company_priority=company_priority,
        )


def composite_key(match: MatchResult) -> tuple[int, int, int]:
    """Batch sort key: company priority, then location tier, then higher score first."""
    return (match.company_priority, match.location_tier, -match.score)
