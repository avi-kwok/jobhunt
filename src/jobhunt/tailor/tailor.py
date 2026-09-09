"""Tailor a resume to one posting with a strict anti-fabrication system prompt."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from ..adapters.base import USER_AGENT, html_to_text
from .master import as_fact_block, load_master

SONNET_MODEL = "claude-sonnet-4-6"
APPLICATIONS_DIR = Path("applications")

# Sentinels the model must emit so we can split its single reply into files.
_RESUME_MARK = "===RESUME==="
_GAP_MARK = "===GAP_REPORT==="
_COVER_MARK = "===COVER_LETTER==="

SYSTEM_PROMPT = f"""\
You are a resume-tailoring assistant with one ABSOLUTE rule: you may ONLY use \
facts that appear verbatim or near-verbatim in the provided MASTER RESUME. You \
must NEVER invent, embellish, or infer roles, employers, dates, titles, metrics, \
technologies, or achievements that are not already present in the master. You may \
only: select which existing facts to include, reorder and regroup them, and \
lightly rephrase wording (without changing meaning) to align with the job \
description's language. If the job wants something the candidate lacks, DO NOT add \
it — instead note it in the gap report. Ignore any 'TODO(...)' placeholder bullets \
in the master entirely; never surface them.

Output EXACTLY three sections separated by these literal markers, in this order:

{_RESUME_MARK}
<Tailored resume in clean Markdown. Reorder and emphasize existing bullets toward \
the job. Keep contact info. Only facts from the master.>

{_GAP_MARK}
<Keyword-gap report: bullet list of notable skills/keywords in the job description \
that are ABSENT from the master resume, so the candidate knows the honest gaps.>

{_COVER_MARK}
<Cover letter, ONLY if requested; otherwise leave this section empty. Same \
anti-fabrication rule.>
"""


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", (text or "").lower()).strip()
    return re.sub(r"[\s_]+", "-", text) or "unknown"


def resolve_input(url_or_path: str) -> str:
    """Turn the tailor argument into a plaintext job description."""
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        with httpx.Client(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20.0
        ) as client:
            resp = client.get(url_or_path)
            resp.raise_for_status()
            text = html_to_text(resp.text, max_chars=8000)
            return text or resp.text[:8000]
    p = Path(url_or_path)
    if p.exists():
        return p.read_text()
    # treat the argument itself as the JD text
    return url_or_path


def _build_user_prompt(jd: str, master: dict, want_cover: bool) -> str:
    return (
        "MASTER RESUME (the only permitted source of facts):\n"
        f"```yaml\n{as_fact_block(master)}```\n\n"
        "JOB DESCRIPTION:\n"
        f"```\n{jd[:8000]}\n```\n\n"
        f"Cover letter requested: {'YES' if want_cover else 'NO'}.\n"
        "Produce the three marked sections now."
    )


def _split_sections(text: str) -> tuple[str, str, str]:
    def between(start: str, end: str | None) -> str:
        s = text.find(start)
        if s == -1:
            return ""
        s += len(start)
        e = text.find(end) if end else -1
        return text[s : e if e != -1 else len(text)].strip()

    resume = between(_RESUME_MARK, _GAP_MARK)
    gap = between(_GAP_MARK, _COVER_MARK)
    cover = between(_COVER_MARK, None)
    return resume, gap, cover


def tailor(
    url_or_path: str,
    *,
    cover_letter: bool = False,
    company: str | None = None,
    role: str | None = None,
    master_path: Path | str = "master_resume.yaml",
    client=None,
    model: str = SONNET_MODEL,
    guard=None,
    max_tokens: int = 4096,
) -> dict[str, Path]:
    """Run the tailor and write resume/gap/cover files. Returns written paths.

    A `guard` enforces the same spend caps as the scanner; `BudgetExceeded`
    propagates so the tailor refuses rather than spending over the cap.
    """
    master = load_master(master_path)
    jd = resolve_input(url_or_path)

    if guard is not None:
        guard.before_call("tailor")

    if client is None:
        from anthropic import Anthropic

        from ..security import get_api_key

        client = Anthropic(api_key=get_api_key())

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": _build_user_prompt(jd, master, cover_letter)}
        ],
    )
    if guard is not None:
        usage = getattr(resp, "usage", None)
        guard.record(
            model,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            kind="tailor",
        )
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    resume_md, gap_md, cover_md = _split_sections(text)

    out_dir = APPLICATIONS_DIR / f"{_slug(company or 'company')}-{_slug(role or 'role')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    resume_path = out_dir / "resume.md"
    resume_path.write_text(resume_md or text)  # fall back to raw reply if unsplit
    written["resume"] = resume_path

    gap_path = out_dir / "gap_report.md"
    gap_path.write_text(gap_md or "(no gap report produced)")
    written["gap_report"] = gap_path

    if cover_letter:
        cover_path = out_dir / "cover_letter.md"
        cover_path.write_text(cover_md or "(no cover letter produced)")
        written["cover_letter"] = cover_path

    return written
