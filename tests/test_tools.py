import json

import pytest

from room import db, tools, web
from room.tools import Ctx, ToolError


def ctx(cfg, conn, role, pitch_id=1):
    return Ctx(cfg, conn, pitch_id, role)


def fake_fetch(pages):
    def fetch(url, **_):
        return url, pages[url]

    return fetch


def test_roles_only_get_their_own_tools(cfg, conn):
    with pytest.raises(ToolError, match="cannot call"):
        tools.call(ctx(cfg, conn, "writer"), "cell_run", {"command": "id"})
    with pytest.raises(ToolError, match="may read"):
        tools.call(ctx(cfg, conn, "editor"), "read_note", {"kind": "research"})
    names = {s["name"] for s in tools.specs_for("writer")}
    assert "cell_run" not in names and "submit_draft" in names


def test_every_role_tool_has_a_spec_and_handler():
    for role, names in tools.ROLE_TOOLS.items():
        assert set(names) <= set(tools.SPECS), role
        assert set(names) <= set(tools.HANDLERS), role


def test_research_quotes_must_be_on_the_fetched_page(cfg, conn, monkeypatch):
    pid = db.add_pitch(conn, source="prompt", title="t")
    page = "Aiven for PostgreSQL supports the pgvector extension for vector search."
    monkeypatch.setattr(web, "fetch", fake_fetch({"https://a.example/doc": page}))
    c = ctx(cfg, conn, "researcher", pid)
    out = tools.call(c, "fetch_url", {"url": "https://a.example/doc"})
    assert "<untrusted" in out
    claim = {
        "claim": "c",
        "source": "https://a.example/doc",
        "quote": "supports the pgvector  extension",
    }
    brief = {
        "summary": "s",
        "claims": [claim] * 3,
        "sources": ["https://a.example/doc"],
        "open_questions": [],
        "competitors": [],
    }
    assert "Brief recorded" in tools.call(c, "submit_research", brief)

    bad = dict(claim, quote="supports 400 million vectors")
    with pytest.raises(ToolError, match="not on the page"):
        tools.call(c, "submit_research", dict(brief, claims=[bad, claim, claim]))
    with pytest.raises(ToolError, match="never fetched"):
        tools.call(c, "submit_research", dict(brief, sources=["https://never.example"]))


def test_search_hits_are_seen_not_fetched(cfg, conn, monkeypatch):
    pid = db.add_pitch(conn, source="prompt", title="t")
    hits = [{"url": "https://hit.example", "title": "x"}]
    monkeypatch.setattr(web, "search_hn", lambda **_: hits)
    tools.call(ctx(cfg, conn, "researcher", pid), "search_hn", {"query": "pg"})
    assert tools.fetched_urls(conn, pid) == set()
    assert tools.fetched_urls(conn, pid, ("seen",)) == {"https://hit.example"}


def test_scout_evidence_must_come_from_signals(cfg, conn):
    db.write_note(conn, 0, "signals", [{"url": "https://news.example/a", "title": "a"}])
    c = ctx(cfg, conn, "scout", 0)
    p = {
        "title": "T",
        "angle": "A",
        "why_now": "W",
        "products": ["Aiven for PostgreSQL"],
        "evidence": ["https://made.up/x"],
        "suggested_author": "ada-sharding",
    }
    with pytest.raises(ToolError, match="no evidence left"):
        tools.call(c, "submit_pitches", {"pitches": [p]})
    good = dict(p, evidence=["https://news.example/a", "https://made.up/x"])
    out = tools.call(c, "submit_pitches", {"pitches": [p, good]})
    # the good pitch is kept without its made-up link; the one with nothing real is dropped
    assert "Recorded 1" in out and "was not recorded" in out
    assert json.loads(db.pitch(conn, 1)["evidence"]) == ["https://news.example/a"]
    assert db.pitch(conn, 1)["status"] == "proposed"


def test_docs_index_results_can_back_a_pitch(cfg, conn, monkeypatch):
    monkeypatch.setattr(web, "aiven_docs_index", lambda terms, **_: ["https://aiven.io/docs/x"])
    c = ctx(cfg, conn, "scout", 0)
    tools.call(c, "aiven_docs", {"terms": ["x"]})
    p = {
        "title": "T",
        "angle": "A",
        "why_now": "W",
        "products": [],
        "evidence": ["https://aiven.io/docs/x"],
    }
    assert "Recorded 1" in tools.call(c, "submit_pitches", {"pitches": [p]})


def test_editor_rules(cfg, conn):
    pid = db.add_pitch(conn, source="scout", title="t")
    c = ctx(cfg, conn, "editor", pid)
    a = {
        "decision": "accept",
        "reasons": ["r"],
        "value": 6,
        "author": "nobody",
        "needs_poc": False,
        "outline": ["a", "b", "c"],
        "duplicate_of": "",
    }
    with pytest.raises(ToolError, match="author must be"):
        tools.call(c, "submit_assignment", a)
    with pytest.raises(ToolError, match="poc_goal"):
        tools.call(c, "submit_assignment", dict(a, author="ada-sharding", needs_poc=True))
    with pytest.raises(ToolError, match="slug of an existing"):
        tools.call(c, "submit_assignment", dict(a, decision="duplicate", duplicate_of="nope"))
    tools.call(
        c,
        "submit_assignment",
        dict(a, decision="duplicate", duplicate_of="kafka-topics-for-beginners"),
    )


def test_poc_cannot_claim_commands_it_never_ran(cfg, conn, monkeypatch):
    pid = db.add_pitch(conn, source="prompt", title="t")
    db.write_note(conn, pid, "cell", "room-poc-1")
    monkeypatch.setattr(tools.cell, "run", lambda name, cmd, t: (0, "ok\n"))
    c = ctx(cfg, conn, "poc", pid)
    tools.call(c, "cell_run", {"command": "python demo.py"})
    rep = {
        "works": True,
        "summary": "s",
        "how_to_run": ["python demo.py"],
        "files": ["demo.py"],
        "limitations": [],
        "evidence": [{"command": "pytest -q", "output": "1 passed"}],
    }
    with pytest.raises(ToolError, match="actually ran"):
        tools.call(c, "submit_poc", rep)
    rep["evidence"] = [{"command": "python demo.py", "output": "ok"}]
    tools.call(c, "submit_poc", rep)
    with pytest.raises(ToolError, match="relative"):
        tools.call(c, "submit_poc", dict(rep, files=["../../etc/passwd"]))


def test_submit_draft_returns_gate_failures_to_the_writer(cfg, conn):
    pid = db.add_pitch(conn, source="prompt", title="t", author="ada-sharding")
    c = ctx(cfg, conn, "writer", pid)
    with pytest.raises(ToolError, match="gate failure"):
        tools.call(c, "submit_draft", {"markdown": "---\ntitle: x\n---\nshort"})
    assert json.loads(db.read_note(conn, pid, "gates"))["ok"] is False
    assert db.read_note(conn, pid, "draft") is None


def test_url_safety():
    for bad in (
        "file:///etc/passwd",
        "http://localhost:8099/",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest",
        "http://box.tail1.ts.net/",
    ):
        with pytest.raises(web.FetchError):
            web.check_public(bad)


def test_html_to_text_keeps_headings_and_links():
    t = web.html_to_text(
        "<html><title>T</title><script>x()</script><h2>Setup</h2><p>Use <a href='/d'>docs</a>"
        '</p><p>Run <a href="https://a.io/x">this</a></p></html>'
    )
    assert t.startswith("Title: T") and "## Setup" in t and "this (https://a.io/x)" in t
    assert "x()" not in t
