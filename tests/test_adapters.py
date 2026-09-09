"""Adapter parsing against mocked ATS responses (no network)."""

import httpx
import pytest
import respx

from jobhunt.adapters.base import html_to_text
from jobhunt.adapters.greenhouse import GreenhouseAdapter
from jobhunt.adapters.lever import LeverAdapter
from jobhunt.adapters.ashby import AshbyAdapter
from jobhunt.adapters.workday import SEARCH_TERMS, WorkdayAdapter
from jobhunt.adapters.amazon import BASE_QUERIES, AmazonAdapter
from jobhunt.models import CompanyConfig, Job


def test_html_to_text_strips_and_truncates():
    out = html_to_text("<p>Hello <b>world</b></p><ul><li>a</li><li>b</li></ul>")
    assert "Hello world" in out
    assert "<" not in out
    long = html_to_text("<p>" + "x" * 5000 + "</p>", max_chars=100)
    assert len(long) <= 101  # 100 + ellipsis


@respx.mock
def test_greenhouse_parses():
    respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 42,
                        "title": "Software Engineer Intern",
                        "updated_at": "2026-06-01T00:00:00Z",
                        "absolute_url": "https://stripe.com/jobs/42",
                        "location": {"name": "Vancouver, BC"},
                        "content": "<p>Build things</p>",
                    }
                ]
            },
        )
    )
    jobs = GreenhouseAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Stripe", ats="greenhouse", slug="stripe")
    )
    assert len(jobs) == 1
    j = jobs[0]
    assert j.external_id == "42"
    assert j.location == "Vancouver, BC"
    assert j.uid == "greenhouse:Stripe:42"
    assert "Build things" in j.description


@respx.mock
def test_lever_parses():
    respx.get("https://api.lever.co/v0/postings/brex?mode=json").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "abc",
                    "text": "Backend Engineering Intern",
                    "hostedUrl": "https://jobs.lever.co/brex/abc",
                    "categories": {"location": "Toronto", "team": "Infra", "commitment": "Intern"},
                    "descriptionPlain": "Do backend work",
                    "createdAt": 1717200000000,
                }
            ],
        )
    )
    jobs = LeverAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Brex", ats="lever", slug="brex")
    )
    assert jobs[0].department == "Infra"
    assert jobs[0].employment_type == "Intern"
    assert jobs[0].posted_at is not None


@respx.mock
def test_ashby_parses():
    respx.get(
        "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "x1",
                        "title": "SWE Intern",
                        "location": "Remote",
                        "jobUrl": "https://jobs.ashbyhq.com/ramp/x1",
                        "departmentName": "Eng",
                        "employmentType": "Intern",
                        "publishedAt": "2026-05-01T00:00:00Z",
                        "descriptionPlain": "Ship code",
                    }
                ]
            },
        )
    )
    jobs = AshbyAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Ramp", ats="ashby", slug="ramp")
    )
    assert jobs[0].location == "Remote"
    assert jobs[0].url.endswith("/ramp/x1")


@respx.mock
def test_workday_paginates():
    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/jobs"

    # total=25: a full page of 20, then a partial page of 5 -> pagination triggers.
    def responder(request):
        import json

        offset = json.loads(request.content)["offset"]
        count = 20 if offset == 0 else 5
        postings = [
            {
                "title": "SWE Intern",
                "externalPath": f"/job/SWE-Intern_R{offset + i}",
                "locationsText": "Seattle",
                "bulletFields": [f"R{offset + i}"],
            }
            for i in range(count)
        ]
        return httpx.Response(200, json={"total": 25, "jobPostings": postings})

    respx.post(url).mock(side_effect=responder)
    jobs = WorkdayAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Acme", ats="workday", tenant="acme", wd="wd5", site="careers")
    )
    assert len(jobs) == 25
    assert jobs[0].url == "https://acme.wd5.myworkdayjobs.com/careers/job/SWE-Intern_R0"


@respx.mock
def test_workday_searches_internship_terms_and_unions():
    """Adapter must query each internship term and dedupe overlapping results.

    Each searchText returns the SAME posting (a huge board would return it under
    several terms); the union must yield it exactly once. Also asserts pagination
    stops on a short page even though `total` is an unreliable 0 (as real Workday
    multi-word queries return), which the old `offset >= total` logic couldn't do.
    """
    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/jobs"
    queries = []

    def responder(request):
        import json

        body = json.loads(request.content)
        queries.append(body["searchText"])
        # One posting, total=0 (unreliable) -> must still stop after the short page.
        postings = [
            {
                "title": "Software Engineer Intern",
                "externalPath": "/job/SWE-Intern_R1",
                "locationsText": "Seattle",
                "bulletFields": ["R1"],
            }
        ]
        return httpx.Response(200, json={"total": 0, "jobPostings": postings})

    respx.post(url).mock(side_effect=responder)
    jobs = WorkdayAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Acme", ats="workday", tenant="acme", wd="wd5", site="careers")
    )
    # Same posting under every term -> deduped to one.
    assert len(jobs) == 1
    assert jobs[0].external_id == "R1"
    # Every configured internship term was actually issued as a query. Under
    # concurrency their arrival order is nondeterministic, so compare as sets.
    assert sorted(queries) == sorted(SEARCH_TERMS)


@respx.mock
def test_amazon_unions_internship_queries():
    """Amazon adapter narrows with internship base_query terms and unions them."""
    seen_queries = []

    def responder(request):
        seen_queries.append(dict(request.url.params).get("base_query"))
        return httpx.Response(
            200,
            json={
                "hits": 1,
                "jobs": [
                    {
                        "id_icims": "999",
                        "title": "Software Dev Engineer Intern",
                        "location": "Seattle",
                        "job_path": "/en/jobs/999/x",
                    }
                ],
            },
        )

    respx.get("https://www.amazon.jobs/en/search.json").mock(side_effect=responder)
    jobs = AmazonAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Amazon", ats="amazon")
    )
    assert len(jobs) == 1  # same posting across queries -> deduped
    assert seen_queries == list(BASE_QUERIES)


def test_workday_falls_back_to_sequential_on_429(monkeypatch):
    """A persistent 429 on the concurrent attempt triggers a sequential retry.

    request_with_retry raises HTTPStatusError(429) after exhausting retries; the
    adapter must cool off (no real sleep here) and re-run every term sequentially,
    returning the jobs the sequential pass finds.
    """
    from jobhunt.adapters import workday as workday_mod

    monkeypatch.setattr(workday_mod.time, "sleep", lambda *a, **k: None)

    company = CompanyConfig(
        name="Acme", ats="workday", tenant="acme", wd="wd5", site="careers"
    )
    job = Job(
        source="workday",
        company="Acme",
        external_id="R1",
        title="Software Engineer Intern",
        url="https://acme.wd5.myworkdayjobs.com/careers/job/SWE-Intern_R1",
    )

    calls = {"n": 0}

    def flaky_search(self, url, job_base, company, term):
        calls["n"] += 1
        # Fail the very first (concurrent) search with a persistent-429 error,
        # exactly as request_with_retry surfaces it; sequential retry succeeds.
        if calls["n"] == 1:
            raise httpx.HTTPStatusError(
                "429", request=httpx.Request("POST", url),
                response=httpx.Response(429),
            )
        return [job]

    monkeypatch.setattr(WorkdayAdapter, "_search", flaky_search)

    jobs = WorkdayAdapter(client=httpx.Client()).fetch(company)
    assert [j.external_id for j in jobs] == ["R1"]  # deduped across all 8 terms


def test_missing_slug_raises():
    with pytest.raises(ValueError):
        GreenhouseAdapter(client=httpx.Client()).fetch(CompanyConfig(name="X", ats="greenhouse"))
