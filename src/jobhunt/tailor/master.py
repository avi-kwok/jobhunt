"""Load the master resume and render it as a compact fact block for the model."""

from __future__ import annotations

from pathlib import Path

import yaml

MASTER_PATH = Path("master_resume.yaml")


def load_master(path: Path | str = MASTER_PATH) -> dict:
    """Read master_resume.yaml into a dict (no validation — it's free-form facts)."""
    return yaml.safe_load(Path(path).read_text()) or {}


def profile_summary(master: dict, max_chars: int = 600) -> str:
    """A short candidate summary for the Haiku relevance check."""
    summary = (master.get("summary") or "").strip().replace("\n", " ")
    skills = master.get("skills", {})
    langs = ", ".join(skills.get("languages", []))
    frameworks = ", ".join(skills.get("frameworks", []))
    edu = master.get("education", [{}])
    degree = edu[0].get("degree", "") if edu else ""
    text = f"{summary} Degree: {degree}. Languages: {langs}. Frameworks: {frameworks}."
    return text[:max_chars].strip()


def as_fact_block(master: dict) -> str:
    """Render the whole master resume as readable YAML for the tailor prompt.

    The model is instructed to use ONLY facts appearing here.
    """
    return yaml.safe_dump(master, sort_keys=False, allow_unicode=True)
