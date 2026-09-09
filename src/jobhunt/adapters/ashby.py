"""Ashby adapter — public job-board API, no auth."""

from __future__ import annotations

from datetime import datetime

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

BASE = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@register
class AshbyAdapter(Adapter):
    ats = "ashby"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: ashby adapter needs a slug")
        url = BASE.format(slug=company.slug)
        resp = request_with_retry(self.client, "GET", url)
        resp.raise_for_status()
        payload = resp.json()

        jobs: list[Job] = []
        for j in payload.get("jobs", []):
            description = html_to_text(
                j.get("descriptionPlain") or j.get("descriptionHtml")
            )
            # Ashby has no stable numeric id in the public feed; fall back to url.
            external_id = str(j.get("id") or j.get("jobUrl") or j.get("title"))
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=external_id,
                    title=j.get("title", ""),
                    location=j.get("location"),
                    department=j.get("departmentName"),
                    employment_type=j.get("employmentType"),
                    url=j.get("jobUrl", ""),
                    description=description,
                    posted_at=_parse_dt(j.get("publishedAt")),
                )
            )
        return jobs
