"""Lever adapter — public postings API, no auth."""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

BASE = "https://api.lever.co/v0/postings/{slug}?mode=json"


def _parse_epoch_ms(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


@register
class LeverAdapter(Adapter):
    ats = "lever"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: lever adapter needs a slug")
        url = BASE.format(slug=company.slug)
        resp = request_with_retry(self.client, "GET", url)
        resp.raise_for_status()
        payload = resp.json()  # JSON array

        jobs: list[Job] = []
        for j in payload:
            categories = j.get("categories") or {}
            # descriptionPlain is already plaintext; still cap length
            description = html_to_text(j.get("descriptionPlain") or j.get("description"))
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=str(j.get("id")),
                    title=j.get("text", ""),
                    location=categories.get("location"),
                    department=categories.get("team"),
                    employment_type=categories.get("commitment"),
                    url=j.get("hostedUrl", ""),
                    description=description,
                    posted_at=_parse_epoch_ms(j.get("createdAt")),
                )
            )
        return jobs
