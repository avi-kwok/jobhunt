"""Seen-store: dedupe, record, notify state, matches_since."""

from jobhunt.models import Job, MatchResult
from jobhunt.store import Store


def _job(ext_id, title="Software Engineer Intern"):
    return Job(source="greenhouse", company="Stripe", external_id=ext_id, title=title, url="u")


def _match():
    return MatchResult(is_match=True, score=88, reason="great", location_tier=1, company_priority=1)


def test_filter_new_and_dedupe(tmp_path):
    store = Store(tmp_path / "t.db")
    a, b = _job("1"), _job("2")
    assert store.filter_new([a, b]) == [a, b]
    store.record(a, _match())
    # second time, a is known; only b is new
    fresh = store.filter_new([a, b])
    assert [j.external_id for j in fresh] == ["2"]
    store.close()


def test_two_scans_no_duplicate_matches(tmp_path):
    store = Store(tmp_path / "t.db")
    jobs = [_job("1"), _job("2")]
    first = store.filter_new(jobs)
    for j in first:
        store.record(j, _match())
    # simulate a second scan with the same postings
    second = store.filter_new(jobs)
    assert second == []
    store.close()


def test_mark_notified_and_matches_since(tmp_path):
    store = Store(tmp_path / "t.db")
    j = _job("1")
    store.record(j, _match())
    rows = store.matches_since()
    assert len(rows) == 1 and rows[0]["notified"] == 0
    store.mark_notified([j.uid])
    rows = store.matches_since()
    assert rows[0]["notified"] == 1
    store.close()


def test_non_match_not_returned(tmp_path):
    store = Store(tmp_path / "t.db")
    j = _job("1")
    store.record(j, MatchResult(is_match=False, score=0, reason="no", location_tier=5, company_priority=4))
    assert store.matches_since() == []
    store.close()
