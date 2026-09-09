"""Priority-dict inversion + composite sort order."""

from jobhunt.config import invert_priorities
from jobhunt.matcher import composite_key
from jobhunt.models import MatchResult


def test_invert_priorities_basic():
    grouped = {1: ["Google", "Stripe"], 2: ["Brex"], 3: ["RBC"]}
    mapping = invert_priorities(grouped)
    assert mapping == {"Google": 1, "Stripe": 1, "Brex": 2, "RBC": 3}


def test_invert_priorities_lowest_tier_wins_on_duplicate():
    grouped = {2: ["Stripe"], 1: ["Stripe"]}
    mapping = invert_priorities(grouped)
    assert mapping["Stripe"] == 1


def _match(prio, tier, score):
    return MatchResult(
        is_match=True, score=score, reason="", location_tier=tier, company_priority=prio
    )


def test_composite_sort_priority_then_tier_then_score():
    matches = [
        _match(2, 1, 99),  # strong company, local, high score
        _match(1, 5, 10),  # top company, no location, low score
        _match(1, 1, 50),  # top company, local, mid score
        _match(1, 1, 80),  # top company, local, higher score
    ]
    ordered = sorted(matches, key=composite_key)
    # priority 1 dominates; within it, tier 1 before tier 5; within tier, higher score first
    assert [(m.company_priority, m.location_tier, m.score) for m in ordered] == [
        (1, 1, 80),
        (1, 1, 50),
        (1, 5, 10),
        (2, 1, 99),
    ]


def test_default_priority_for_unlisted():
    from pathlib import Path

    from jobhunt.config import load_config

    # Load config.yaml alone (ignore companies.resolved.yaml, which can inject
    # resolved-only companies carrying their own priority).
    cfg = load_config(Path("config.yaml"), resolved_path=Path("/nonexistent.yaml"))
    # Google is tier 1 in the real config; a company not in any tier defaults to 4.
    assert cfg.company_priority["Google"] == 1
    assert all(c.priority == 4 for c in cfg.companies if c.name not in cfg.company_priority)
