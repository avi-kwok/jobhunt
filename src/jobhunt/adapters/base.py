"""Adapter ABC, shared httpx client, HTML->text helpers, and a registry."""

from __future__ import annotations

import os
import random
import re
import time
from abc import ABC, abstractmethod
from html import unescape
from html.parser import HTMLParser
from typing import Callable, Type

import httpx

from ..models import CompanyConfig, Job

# Polite crawling convention: identify the tool and give sites a contact.
# Set JOBHUNT_CONTACT in .env to your own address — without it we send no
# contact at all, so a fork never scrapes career sites identifying as someone
# else. Keep your address out of the committed source.
_CONTACT = os.environ.get("JOBHUNT_CONTACT", "").strip()
USER_AGENT = (
    f"jobhunt/0.1 (personal internship scanner; contact {_CONTACT})"
    if _CONTACT
    else "jobhunt/0.1 (personal internship scanner)"
)
DEFAULT_TIMEOUT = 20.0
DESCRIPTION_MAX_CHARS = 2000


def make_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """A shared-style httpx client with a descriptive UA and sane timeout."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
        follow_redirects=True,
    )


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff: float = 1.0,
    **kwargs,
) -> httpx.Response:
    """Issue a request, retrying transient failures with exponential backoff.

    On HTTP 429 the server's `Retry-After` header is honored (capped) instead of
    the exponential schedule; a little random jitter is added to every wait so
    retries don't hammer in lockstep.
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == retries - 1:
                break
            time.sleep(backoff * (2**attempt) + random.uniform(0, 0.5))
            continue

        # retry on transient server / rate-limit statuses
        if resp.status_code in (429, 500, 502, 503, 504):
            last_exc = httpx.HTTPStatusError(
                f"transient {resp.status_code}", request=resp.request, response=resp
            )
            if attempt == retries - 1:
                break
            wait = backoff * (2**attempt)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = min(float(retry_after), 60.0)  # respect, but cap at 60s
            time.sleep(wait + random.uniform(0, 0.5))
            continue

        return resp

    assert last_exc is not None
    raise last_exc


class _TextExtractor(HTMLParser):
    """Collapse HTML into readable plaintext, keeping paragraph breaks."""

    _BLOCK = {"p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str | None, max_chars: int = DESCRIPTION_MAX_CHARS) -> str | None:
    """Strip HTML to plaintext, collapse whitespace, and truncate."""
    if not html:
        return None
    parser = _TextExtractor()
    parser.feed(html)
    text = unescape(parser.text())
    # collapse runs of blank lines / spaces
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text or None


class Adapter(ABC):
    """Base class for every ATS adapter."""

    # subclasses set this to their registry key (e.g. "greenhouse")
    ats: str = ""

    def __init__(self, client: httpx.Client | None = None):
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client()
            self._owns_client = True
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    @abstractmethod
    def fetch(self, company: CompanyConfig) -> list[Job]:
        """Return every current posting for the given company."""
        raise NotImplementedError


# ---- registry -------------------------------------------------------------

_REGISTRY: dict[str, Type[Adapter]] = {}


def register(cls: Type[Adapter]) -> Type[Adapter]:
    """Class decorator that adds an adapter to the registry under its `ats` key."""
    if not cls.ats:
        raise ValueError(f"{cls.__name__} must set a non-empty `ats` key")
    _REGISTRY[cls.ats] = cls
    return cls


def get_adapter(ats: str, client: httpx.Client | None = None) -> Adapter:
    """Instantiate the adapter registered for `ats`."""
    try:
        cls = _REGISTRY[ats]
    except KeyError as exc:
        raise KeyError(f"no adapter registered for ATS '{ats}'") from exc
    return cls(client=client)


def registered_ats() -> list[str]:
    return sorted(_REGISTRY)
