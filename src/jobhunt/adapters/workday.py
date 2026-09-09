"""Workday adapter — public CXS search API, POST + offset pagination.

Large Workday boards (banks, big tech) carry *thousands* of postings. A blind
``searchText=""`` fetch is capped by ``MAX_PAGES`` and, on boards past that cap,
silently drops everything below the cap — including SWE internships that happen
to sort low in Workday's default order. That is exactly how a live TD "Software
Engineer Intern/Co-op" posting was never seen.

Instead we run a small set of internship-oriented searches and union the results
by uid. Workday's ``searchText`` uses AND semantics over title+description, so a
query like ``"software intern"`` returns only postings mentioning *both* — a few
hundred at most, comfortably under the page cap. Because the title-prefilter
downstream *requires* a role term (software/engineer/developer/…) AND an
internship term in the title, these queries form a superset of anything that
could pass the filter: no eligible internship can be hidden behind the cap,
regardless of how huge the board is. The trade-off — a posting whose title and
description contain none of these role words is not fetched — is negligible for
SWE/dev roles and far safer than dropping jobs below a blind cap.
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

JOBS_URL = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
JOB_BASE = "https://{tenant}.{wd}.myworkdayjobs.com/{site}"

PAGE_LIMIT = 20
MAX_PAGES = 50  # per-term safety valve (=1000 postings) so a term can't loop forever

# Role × internship phrase searches. AND semantics keep each result set small and
# under the page cap while together covering the role terms that real SWE/dev
# internship titles use. "data" was dropped: its role-word isn't a strong role_term
# for the downstream title-prefilter, so it mostly pulled non-eligible postings.
# "technology" is kept to catch bank "Technology Analyst" co-op/intern roles without
# pulling whole boards the way a bare "intern" would.
SEARCH_TERMS = (
    "software intern",
    "software co-op",
    "engineer intern",
    "engineer co-op",
    "developer intern",
    "developer co-op",
    "technology intern",
    "technology co-op",
)

SEARCH_CONCURRENCY = 2  # run this many per-board searches at once; low by design so we stay a polite, low-burst client and never trip a host's rate limit

_RATE_LIMIT_COOLDOWN = 5.0


@register
class WorkdayAdapter(Adapter):
    ats = "workday"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        missing = [f for f in ("tenant", "wd", "site") if not getattr(company, f)]
        if missing:
            raise ValueError(
                f"{company.name}: workday adapter needs {', '.join(missing)}"
            )

        url = JOBS_URL.format(tenant=company.tenant, wd=company.wd, site=company.site)
        job_base = JOB_BASE.format(
            tenant=company.tenant, wd=company.wd, site=company.site
        )

        try:
            return self._fetch_terms(
                url, job_base, company, SEARCH_TERMS, workers=SEARCH_CONCURRENCY
            )
        except httpx.HTTPStatusError as exc:
            if exc.response is None or exc.response.status_code != 429:
                raise
            # request_with_retry exhausted its 429 retries: the board is rate-limiting.
            # Cool off, then retry the whole board sequentially (gentler burst).
            time.sleep(_RATE_LIMIT_COOLDOWN)
            print(
                f"[workday] {company.name}: rate-limited, retrying sequentially",
                file=sys.stderr,
            )
            return self._fetch_terms(url, job_base, company, SEARCH_TERMS, workers=1)

    def _fetch_terms(
        self,
        url: str,
        job_base: str,
        company: CompanyConfig,
        terms: tuple[str, ...],
        workers: int,
    ) -> list[Job]:
        """Run each term's search and union results, deduped by uid.

        Results are unioned in the original ``terms`` order regardless of thread
        finish order, so output ordering is deterministic. ``workers=1`` runs the
        searches sequentially; higher values run up to ``workers`` at once.
        """
        if workers <= 1:
            per_term = [self._search(url, job_base, company, t) for t in terms]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(self._search, url, job_base, company, t) for t in terms
                ]
                # .result() re-raises worker exceptions (e.g. the 429) here.
                per_term = [f.result() for f in futures]

        seen_uids: set[str] = set()
        jobs: list[Job] = []
        for term_jobs in per_term:
            for job in term_jobs:
                if job.uid not in seen_uids:
                    seen_uids.add(job.uid)
                    jobs.append(job)
        return jobs

    def _search(
        self, url: str, job_base: str, company: CompanyConfig, term: str
    ) -> list[Job]:
        """Paginate one searchText query to exhaustion.

        Multi-word queries return an unreliable ``total`` (often 0), so we page
        until a short/empty page rather than trusting the count.
        """
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        jobs: list[Job] = []
        offset = 0
        for _ in range(MAX_PAGES):
            body = {
                "appliedFacets": {},
                "limit": PAGE_LIMIT,
                "offset": offset,
                "searchText": term,
            }
            resp = request_with_retry(
                self.client, "POST", url, json=body, headers=headers
            )
            resp.raise_for_status()
            payload = resp.json()

            postings = payload.get("jobPostings", [])
            if not postings:
                break

            for p in postings:
                jobs.append(self._to_job(p, job_base, company))

            offset += PAGE_LIMIT
            if len(postings) < PAGE_LIMIT:  # last (short) page — stop
                break

        return jobs

    @staticmethod
    def _to_job(p: dict, job_base: str, company: CompanyConfig) -> Job:
        external_path = p.get("externalPath", "")
        full_url = job_base + external_path if external_path else job_base
        bullets = p.get("bulletFields") or []
        # bulletFields often carries the req id; use it as the stable id.
        external_id = str(bullets[0]) if bullets else external_path
        description = " | ".join(str(b) for b in bullets) or None
        return Job(
            source="workday",
            company=company.name,
            external_id=external_id,
            title=p.get("title", ""),
            location=p.get("locationsText"),
            url=full_url,
            description=description,
            posted_at=None,  # the list endpoint only gives a relative string…
            posted_note=p.get("postedOn"),  # …so keep it for display ("Posted Today")
        )
