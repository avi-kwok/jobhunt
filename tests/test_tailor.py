"""Tailor: section splitting + file output, with a fake Anthropic client."""

from jobhunt.tailor.tailor import _split_sections, resolve_input, tailor


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResp(self._text)


class _FakeClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


_REPLY = """\
===RESUME===
# Jordan Rivera
- Built a RAG chatbot with FastAPI and ChromaDB.

===GAP_REPORT===
- Kubernetes (not in master)
- Go (not in master)

===COVER_LETTER===
Dear hiring team, I am excited to apply.
"""


def test_split_sections():
    resume, gap, cover = _split_sections(_REPLY)
    assert "RAG chatbot" in resume
    assert "Kubernetes" in gap
    assert "excited to apply" in cover


def test_resolve_input_raw_text():
    assert resolve_input("Some pasted JD text") == "Some pasted JD text"


def test_tailor_writes_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # minimal master resume
    (tmp_path / "master_resume.yaml").write_text(
        "contact:\n  name: Jordan\nskills:\n  languages: [Python]\n"
    )
    fake = _FakeClient(_REPLY)
    written = tailor(
        "Backend Engineer Intern — Python, FastAPI",
        cover_letter=True,
        company="Stripe",
        role="SWE Intern",
        master_path=tmp_path / "master_resume.yaml",
        client=fake,
    )
    assert written["resume"].read_text().startswith("# Jordan Rivera")
    assert "Kubernetes" in written["gap_report"].read_text()
    assert "excited to apply" in written["cover_letter"].read_text()
    # anti-fabrication rule must be present in the system prompt actually sent
    assert "NEVER invent" in fake.messages.last_kwargs["system"]


def test_tailor_no_cover_letter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "master_resume.yaml").write_text("contact:\n  name: A\n")
    fake = _FakeClient(_REPLY)
    written = tailor(
        "JD text",
        cover_letter=False,
        company="X",
        role="Y",
        master_path=tmp_path / "master_resume.yaml",
        client=fake,
    )
    assert "cover_letter" not in written
