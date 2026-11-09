import contextlib
import json
import subprocess

from room import agent, db, pipeline, publish, tools, web
from room.tools import Ctx

PAGE = "Aiven for PostgreSQL includes pgvector. Plans start on the free tier. HNSW indexes work."
SRC = "https://aiven.io/docs/products/postgresql/pgvector"


def good_draft(slug="pgvector-on-aiven"):
    words = " ".join(["detail"] * 450)
    return (
        f"---\ntitle: Pgvector on Aiven\nslug: {slug}\nsummary: How to use it.\n"
        "tags: [postgresql]\nfaq:\n  - q: a?\n    a: b\n  - q: c?\n    a: d\n---\n"
        f"**TL;DR**\n\n- it works\n\n## Setup\n\n{words}\n\nSource: [docs]({SRC}).\n"
    )


class Script:
    """A fake agent: each role does what a well-behaved model would, through the real tools."""

    def __init__(self, cfg, monkeypatch, review=("approve",), draft=good_draft, poc_works=True):
        self.calls, self.reviews, self.draft, self.poc_works = [], list(review), draft, poc_works
        monkeypatch.setattr(web, "fetch", lambda url, **_: (url, PAGE))
        monkeypatch.setattr(tools.cell, "run", lambda name, cmd, t: (0, "1 passed\n"))

    def __call__(self, cfg, conn, pid, role, prompt, *, extra_system="", say=print):
        self.calls.append(role)
        c = Ctx(cfg, conn, pid, role)
        t = lambda name, **a: tools.call(c, name, a)  # noqa: E731
        if role == "editor":
            t(
                "submit_assignment",
                decision="accept",
                reasons=["useful"],
                value=7,
                author="ada-sharding",
                needs_poc=False,
                outline=["a", "b", "c"],
                duplicate_of="",
            )
        elif role == "researcher":
            t("fetch_url", url=SRC)
            claim = {"claim": "has pgvector", "source": SRC, "quote": "includes pgvector"}
            t(
                "submit_research",
                summary="s",
                claims=[claim] * 3,
                sources=[SRC],
                open_questions=["free tier limits"],
                competitors=["Neon"],
            )
        elif role == "poc":
            t("cell_run", command="uv run pytest -q")
            t(
                "submit_poc",
                works=self.poc_works,
                summary="ok",
                how_to_run=["uv run pytest -q"],
                evidence=[{"command": "uv run pytest -q", "output": "1 passed"}],
                files=["test_demo.py"],
                limitations=["local PostgreSQL stand-in"],
            )
        elif role == "writer":
            assert "Ada Sharding" in extra_system
            # pi hands a gate failure back to the model; this fake just gives up
            with contextlib.suppress(tools.ToolError):
                t("submit_draft", markdown=self.draft())
        elif role == "reviewer":
            v = self.reviews.pop(0) if self.reviews else "approve"
            t("submit_review", verdict=v, reasons=["fix the intro"] if v != "approve" else [])
        return agent.RunResult("ok", {"input": 1000, "output": 200}, 0.01)


class FakeCells:
    CellError = tools.cell.CellError

    def __init__(self):
        self.created, self.removed = [], []

    def create(self, name, image=""):
        self.created.append(name)

    def exists(self, name):
        return name in self.created and name not in self.removed

    def remove(self, name):
        self.removed.append(name)

    def copy_out(self, name, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "test_demo.py").write_text("def test_ok():\n    assert True\n")
        return ["test_demo.py"]


def test_a_prompt_goes_all_the_way_to_a_committed_branch(cfg, conn, blog, monkeypatch):
    s = Script(cfg, monkeypatch)
    pid = pipeline.new_pitch(cfg, conn, "Write about pgvector on Aiven", runner=s, say=lambda _: 0)
    assert db.pitch(conn, pid)["status"] == "approved"
    branch = pipeline.write(cfg, conn, pid, runner=s, say=lambda _: 0)
    assert branch == "room/pgvector-on-aiven"
    assert s.calls == ["editor", "researcher", "writer", "reviewer"]
    assert db.pitch(conn, pid)["status"] == "drafted"
    files = subprocess.run(
        ["git", "-C", str(blog), "show", "--name-only", "--format=", branch],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert files == [f"content/posts/{__import__('datetime').date.today()}-pgvector-on-aiven.md"]
    post = subprocess.run(
        ["git", "-C", str(blog), "show", f"{branch}:{files[0]}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "author: ada-sharding" in post and "model: Claude Sonnet 5.5" in post
    assert "prompt: Write about pgvector on Aiven" in post
    # the person's checkout is untouched: still on main, no worktree left behind
    assert (
        subprocess.run(
            ["git", "-C", str(blog), "branch", "--show-current"], capture_output=True, text=True
        ).stdout.strip()
        == "main"
    )
    assert (
        "worktrees"
        not in subprocess.run(
            ["git", "-C", str(blog), "worktree", "list"], capture_output=True, text=True
        ).stdout
    )


def test_review_sends_it_back_and_then_gives_up(cfg, conn, monkeypatch):
    s = Script(cfg, monkeypatch, review=["request_changes"] * 10)
    pid = pipeline.new_pitch(cfg, conn, "x", author="ada-sharding", runner=s, say=lambda _: 0)
    assert pipeline.write(cfg, conn, pid, runner=s, say=lambda _: 0) is None
    row = db.pitch(conn, pid)
    assert row["status"] == "escalated" and "still requests changes" in row["reason"]
    assert s.calls.count("reviewer") == cfg["limits"]["max_review_rounds"] + 1


def test_a_draft_that_never_passes_the_gates_escalates(cfg, conn, monkeypatch):
    s = Script(cfg, monkeypatch, draft=lambda: "no front matter")
    pid = pipeline.new_pitch(cfg, conn, "x", runner=s, say=lambda _: 0)
    assert pipeline.write(cfg, conn, pid, runner=s, say=lambda _: 0) is None
    assert "passed the gates" in db.pitch(conn, pid)["reason"]
    assert s.calls.count("writer") == cfg["limits"]["max_write_rounds"] + 1


def test_poc_runs_on_a_computer_that_is_always_deleted(cfg, conn, blog, monkeypatch):
    s = Script(cfg, monkeypatch)
    cells = FakeCells()
    pid = pipeline.new_pitch(cfg, conn, "x", poc=True, runner=s, say=lambda _: 0)
    branch = pipeline.write(cfg, conn, pid, runner=s, say=lambda _: 0, cells=cells)
    assert "poc" in s.calls and cells.created == cells.removed
    files = subprocess.run(
        ["git", "-C", str(blog), "show", "--name-only", "--format=", branch],
        capture_output=True,
        text=True,
    ).stdout
    assert "content/pocs/pgvector-on-aiven/test_demo.py" in files


def test_escalation_stops_the_pitch(cfg, conn, monkeypatch):
    s = Script(cfg, monkeypatch)
    pid = pipeline.new_pitch(cfg, conn, "x", runner=s, say=lambda _: 0)

    def researcher_gives_up(cfg, conn, pid, role, prompt, **_):
        tools.call(
            Ctx(cfg, conn, pid, role),
            "escalate",
            {"reason": "pgvector is not offered", "next_steps": ["check the docs"]},
        )
        return agent.RunResult("ok")

    assert pipeline.write(cfg, conn, pid, runner=researcher_gives_up, say=lambda _: 0) is None
    assert "pgvector is not offered" in db.pitch(conn, pid)["reason"]


def test_budget_is_checked_before_each_role(cfg, conn, monkeypatch):
    s = Script(cfg, monkeypatch)
    pid = pipeline.new_pitch(cfg, conn, "x", runner=s, say=lambda _: 0)
    run = db.start_run(conn, pid, "researcher", "m", "log")
    db.end_run(conn, run, "ok", {"input": 1}, cfg["budget"]["pitch_usd"] + 1)
    assert pipeline.write(cfg, conn, pid, runner=s, say=lambda _: 0) is None
    assert "budget" in db.pitch(conn, pid)["reason"]


def test_a_persons_pitch_is_flagged_not_refused(cfg, conn, monkeypatch):
    def editor_says_dup(cfg, conn, pid, role, prompt, **_):
        tools.call(
            Ctx(cfg, conn, pid, role),
            "submit_assignment",
            {
                "decision": "duplicate",
                "reasons": ["exists"],
                "value": 2,
                "author": "kai-offset",
                "needs_poc": False,
                "outline": [],
                "duplicate_of": "kafka-topics-for-beginners",
            },
        )
        return agent.RunResult("ok")

    pid = pipeline.new_pitch(cfg, conn, "Kafka topics", runner=editor_says_dup, say=lambda _: 0)
    row = db.pitch(conn, pid)
    assert row["status"] == "escalated" and "kafka-topics-for-beginners" in row["reason"]


def test_finalize_owns_the_run_facts():
    from datetime import date

    text, meta = publish.finalize(
        good_draft().replace("slug:", "author: someone\nslug:"),
        author="ada-sharding",
        model="claude-sonnet-5-5",
        pitch={"title": "t", "prompt": "p"},
        pitch_id=3,
        poc_path=None,
        today=date(2026, 11, 9),
    )
    assert meta["author"] == "ada-sharding" and meta["date"] == date(2026, 11, 9)
    assert text.startswith("---\ntitle: Pgvector on Aiven\nslug:")
    assert json.dumps(list(meta)[:5]) == '["title", "slug", "date", "author", "model"]'
