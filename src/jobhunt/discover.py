"""Resolve companies (by name or careers URL) to concrete ATS endpoints."""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from .adapters.base import USER_AGENT
from .models import CompanyConfig

UNRESOLVED_PATH = Path("unresolved.txt")
RESOLVED_PATH = Path("companies.resolved.yaml")

_GH_HOST = "greenhouse.io"
_LEVER_HOST = "lever.co"
_ASHBY_HOST = "ashbyhq.com"
_WORKDAY_HOST = "myworkdayjobs.com"


# ---- URL parsing ----------------------------------------------------------

def from_url(name: str, url: str) -> CompanyConfig | None:
    """Detect ATS + slug (or Workday tenant/wd/site) directly from a careers URL."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.netloc.lower()
    path = parsed.path

    if _GH_HOST in host or "greenhouse.io/embed" in url:
        # boards.greenhouse.io/<slug> or job-boards.greenhouse.io/<slug>
        m = re.search(r"greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", url) or \
            re.search(r"/([\w-]+)", path)
        if m:
            return CompanyConfig(name=name, ats="greenhouse", slug=m.group(1))

    if _LEVER_HOST in host:
        # jobs.lever.co/<slug>
        parts = [p for p in path.split("/") if p]
        if parts:
            return CompanyConfig(name=name, ats="lever", slug=parts[0])

    if _ASHBY_HOST in host:
        # jobs.ashbyhq.com/<slug>
        parts = [p for p in path.split("/") if p]
        if parts:
            return CompanyConfig(name=name, ats="ashby", slug=parts[0])

    if _WORKDAY_HOST in host:
        # <tenant>.<wd>.myworkdayjobs.com/<...>/<site>
        m = re.match(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com", host)
        parts = [p for p in path.split("/") if p]
        # site is the last path segment that isn't a locale like "en-US"
        site = None
        for seg in parts:
            if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", seg):
                site = seg
        if m and site:
            return CompanyConfig(
                name=name, ats="workday", tenant=m.group(1), wd=m.group(2), site=site
            )

    return None


# ---- name probing ---------------------------------------------------------

def slug_candidates(name: str) -> list[str]:
    """Generate plausible board slugs from a company name."""
    lower = name.lower()
    stripped = re.sub(r"[^a-z0-9]+", "", lower)  # "d. e. shaw" -> "deshaw"
    hyphen = re.sub(r"[^a-z0-9]+", "-", lower).strip("-")  # "capital one" -> "capital-one"
    nospace = lower.replace(" ", "")
    first = lower.split()[0] if lower.split() else lower
    seen: list[str] = []
    for cand in (stripped, hyphen, nospace, first):
        if cand and cand not in seen:
            seen.append(cand)
    return seen


def _probe_greenhouse(client: httpx.Client, slug: str) -> bool:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = client.get(url, timeout=10.0)
        return r.status_code == 200 and bool(r.json().get("jobs"))
    except Exception:  # noqa: BLE001
        return False


def _probe_lever(client: httpx.Client, slug: str) -> bool:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = client.get(url, timeout=10.0)
        return r.status_code == 200 and isinstance(r.json(), list) and bool(r.json())
    except Exception:  # noqa: BLE001
        return False


def _probe_ashby(client: httpx.Client, slug: str) -> bool:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = client.get(url, timeout=10.0)
        return r.status_code == 200 and bool(r.json().get("jobs"))
    except Exception:  # noqa: BLE001
        return False


def from_name(
    name: str, client: httpx.Client | None = None, backoff: float = 0.0
) -> CompanyConfig | None:
    """Probe Greenhouse/Lever/Ashby for each slug candidate; first hit wins."""
    owns = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        for slug in slug_candidates(name):
            # Greenhouse/Lever/Ashby are three *different* hosts, so there's no
            # per-host politeness reason to pause between them — probe straight through.
            if _probe_greenhouse(client, slug):
                return CompanyConfig(name=name, ats="greenhouse", slug=slug)
            if _probe_lever(client, slug):
                return CompanyConfig(name=name, ats="lever", slug=slug)
            if _probe_ashby(client, slug):
                return CompanyConfig(name=name, ats="ashby", slug=slug)
            if backoff:
                time.sleep(backoff)  # optional pause between slug candidates
        return None
    finally:
        if owns:
            client.close()


def resolve(
    name: str, url: str | None = None, client: httpx.Client | None = None
) -> CompanyConfig | None:
    """Resolve a single company: URL if given, else probe by name."""
    if url:
        return from_url(name, url)
    return from_name(name, client=client)


# ---- batch ----------------------------------------------------------------

def discover_all(
    companies: list[CompanyConfig],
    resolved_path: Path = RESOLVED_PATH,
    unresolved_path: Path = UNRESOLVED_PATH,
    client: httpx.Client | None = None,
    progress=None,
    concurrency: int = 8,
) -> tuple[list[CompanyConfig], list[CompanyConfig]]:
    """Resolve every company by name, preserving priority. Writes both output files.

    Companies are resolved concurrently (a shared, thread-safe httpx client). Each
    company touches a few different ATS hosts, so modest concurrency stays polite
    per-host while cutting a ~400-company run from ~10 minutes to about a minute.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    owns = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    resolved: list[CompanyConfig] = []
    unresolved: list[CompanyConfig] = []
    try:
        ordered = sorted(companies, key=lambda c: c.priority)
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {pool.submit(from_name, c.name, client=client): c for c in ordered}
            for fut in as_completed(futures):
                company = futures[fut]
                try:
                    hit = fut.result()
                except Exception:  # noqa: BLE001 - a probe error just means unresolved
                    hit = None
                if hit:
                    hit.priority = company.priority
                    resolved.append(hit)
                else:
                    unresolved.append(company)
                if progress:  # runs in the main thread (as_completed) — no interleave
                    progress(company, hit)
    finally:
        if owns:
            client.close()

    # as_completed is unordered; tidy the output files by priority.
    resolved.sort(key=lambda c: c.priority)
    unresolved.sort(key=lambda c: c.priority)

    resolved_path.write_text(
        yaml.safe_dump(
            {"companies": [c.model_dump(exclude_none=True) for c in resolved]},
            sort_keys=False,
        )
    )
    unresolved_path.write_text(
        "# Could not auto-resolve — supply a careers URL:\n"
        + "\n".join(f"{c.name}  # jobhunt discover \"{c.name}\" --url <URL>" for c in unresolved)
        + "\n"
    )
    return resolved, unresolved
