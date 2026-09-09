"""Discover: URL parsing + name-probe resolution."""

import httpx
import respx

from jobhunt.discover import from_name, from_url, slug_candidates


def test_from_url_greenhouse():
    cc = from_url("Stripe", "https://boards.greenhouse.io/stripe")
    assert cc.ats == "greenhouse" and cc.slug == "stripe"


def test_from_url_lever():
    cc = from_url("Brex", "https://jobs.lever.co/brex")
    assert cc.ats == "lever" and cc.slug == "brex"


def test_from_url_ashby():
    cc = from_url("Ramp", "https://jobs.ashbyhq.com/ramp")
    assert cc.ats == "ashby" and cc.slug == "ramp"


def test_from_url_workday():
    cc = from_url(
        "Acme", "https://acme.wd5.myworkdayjobs.com/en-US/careers"
    )
    assert cc.ats == "workday"
    assert cc.tenant == "acme" and cc.wd == "wd5" and cc.site == "careers"


def test_from_url_unknown_host():
    assert from_url("X", "https://example.com/jobs") is None


def test_slug_candidates():
    cands = slug_candidates("D. E. Shaw")
    assert "deshaw" in cands
    cands2 = slug_candidates("Capital One")
    assert "capital-one" in cands2 and "capitalone" in cands2


@respx.mock
def test_from_name_probes_greenhouse_first():
    respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [{"id": 1}]})
    )
    client = httpx.Client()
    cc = from_name("Stripe", client=client, backoff=0)
    assert cc is not None and cc.ats == "greenhouse" and cc.slug == "stripe"


@respx.mock
def test_from_name_falls_through_to_lever():
    respx.get(url__regex=r"greenhouse\.io.*").mock(return_value=httpx.Response(404))
    respx.get("https://api.lever.co/v0/postings/brex?mode=json").mock(
        return_value=httpx.Response(200, json=[{"id": "a"}])
    )
    respx.get(url__regex=r"ashbyhq\.com.*").mock(return_value=httpx.Response(404))
    client = httpx.Client()
    cc = from_name("Brex", client=client, backoff=0)
    assert cc is not None and cc.ats == "lever"


@respx.mock
def test_from_name_unresolved_returns_none():
    respx.route().mock(return_value=httpx.Response(404))
    client = httpx.Client()
    assert from_name("Nonexistent Co", client=client, backoff=0) is None
