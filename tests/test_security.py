"""Key-protection safeguards: redaction, env-only key access, file perms, audit."""

import stat

import pytest

from jobhunt import security


def test_redact_masks_key():
    out = security.redact("using sk-ant-api03-ABCDEF123456 now")
    assert "sk-ant-api03-ABCDEF123456" not in out
    assert "REDACTED" in out


# ---- webhook redaction ----------------------------------------------------
# A webhook URL is a credential: whoever holds it can post to the channel.
# httpx embeds the request URL in HTTPStatusError, so an HTTP failure would
# otherwise print the secret straight into logs/cron.log.

@pytest.mark.parametrize(
    "secret",
    [
        "https://discord.com/api/webhooks/1234567890123456/aBcDeF_ghIJKlmnOpQrs",
        "https://discordapp.com/api/v10/webhooks/999/tok_ABC123",
        "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXX",
        "https://self-hosted.internal/webhook/sup3rs3cr3ttoken",
    ],
)
def test_redact_masks_webhook_urls(secret):
    out = security.redact(f"Client error '404 Not Found' for url '{secret}'")
    token = secret.rsplit("/", 1)[-1]
    assert token not in out
    assert "REDACTED" in out


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.airbnb.com/positions/7995199?gh_jid=7995199",
        "https://company.example.com/services/consulting",
    ],
)
def test_redact_leaves_ordinary_urls_intact(url):
    """Posting URLs must survive redaction — they are the product output."""
    assert security.redact(f"see {url}") == f"see {url}"


def test_get_api_key_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test123456")
    assert security.get_api_key() == "sk-ant-test123456"


def test_get_api_key_missing_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(security.SecurityError):
        security.get_api_key()


def test_get_api_key_missing_optional_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert security.get_api_key(required=False) is None


def test_harden_env_permissions_sets_600(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-secret\n")
    env.chmod(0o644)  # world-readable
    notes = security.harden_env_permissions(env)
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600
    assert notes  # reported the tightening


def test_harden_noop_when_already_600(tmp_path):
    env = tmp_path / ".env"
    env.write_text("x")
    env.chmod(0o600)
    assert security.harden_env_permissions(env) == []


def test_audit_reports_missing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("x")
    env.chmod(0o600)
    all_ok, checks = security.audit(env)
    labels = {label: ok for label, ok, _ in checks}
    assert labels["API key present in env"] is False
    assert all_ok is False
