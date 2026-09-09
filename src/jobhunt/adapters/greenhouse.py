"""Greenhouse adapter — public board API, no auth."""

from __future__ import annotations

from datetime import datetime

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@register
class GreenhouseAdapter(Adapter):
    ats = "greenhouse"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: greenhouse adapter needs a slug")
        url = BASE.format(slug=company.slug)
        resp = request_with_retry(self.client, "GET", url)
        resp.raise_for_status()
        payload = resp.json()

        jobs: list[Job] = []
        for j in payload.get("jobs", []):
            location = (j.get("location") or {}).get("name")
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=str(j.get("id")),
                    title=j.get("title", ""),
                    location=location,
                    url=j.get("absolute_url", ""),
                    description=html_to_text(j.get("content")),
                    posted_at=_parse_dt(j.get("updated_at")),
                )
            )
        return jobs
