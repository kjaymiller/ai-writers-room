import json
import time

import pytest
from fastapi.testclient import TestClient

from room import agent, db, pipeline, tools, webapp
from room.tools import Ctx

from .test_pipeline import good_draft


@pytest.fixture
def app(cfg, monkeypatch):
    monkeypatch.setenv("AIVEN_AI_GATEWAY_KEY", "k")
    a = webapp.create_app(cfg)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def settle(app):
    for _ in range(200):
        if not any(t.is_alive() for t in app.state.jobs.threads.values()):
            return
        time.sleep(0.02)
    raise AssertionError("background job did not finish")


def fake_assign(cfg, conn, pid, *, forced_author=None, forced_poc=False, say=print, runner=None):
    db.write_note(
        conn,
        pid,
        "assignment",
        {
            "decision": "accept",
            "reasons": ["Good fit."],
            "value": 7,
            "author": forced_author or "ada-sharding",
            "needs_poc": forced_poc,
            "outline": ["Why", "How", "Verify"],
            "duplicate_of": "",
        },
    )
    row = db.pitch(conn, pid)
    db.update_pitch(
        conn,
        pid,
        author=forced_author or "ada-sharding",
        status="approved" if row["source"] == "prompt" else "proposed",
    )
    say("[editor] accepted")


def test_the_board_groups_pitches_in_plain_words(client, conn):
    db.add_pitch(conn, source="scout", title="Kafka for agents", author="kai-offset")
    pid = db.add_pitch(conn, source="prompt", title="Stuck one", author="ada-sharding")
    db.update_pitch(conn, pid, status="escalated", reason="the docs contradict the pitch")
    html = client.get("/").text
    assert "Needs you" in html and "the docs contradict the pitch" in html
    assert "Kafka for agents" in html and "Kai Offset" in html


def test_pitching_from_the_page_plans_and_writes(app, client, conn, monkeypatch):
    monkeypatch.setattr(pipeline, "assign", fake_assign)
    wrote = []
    monkeypatch.setattr(webapp.pipeline, "write", lambda cfg, c, pid, say: wrote.append(pid))
    r = client.post(
        "/pitches",
        data={
            "prompt": "Valkey caching\nmore detail",
            "author": "ada-sharding",
            "write_now": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    settle(app)
    assert wrote == [pid]
    page = client.get(f"/pitches/{pid}").text
    assert "Valkey caching" in page and "plan" in page and "Why" in page
    assert "[editor] accepted" in client.get(f"/pitches/{pid}/log").text


def test_a_draft_renders_as_an_article_and_its_html_is_escaped(client, conn):
    pid = db.add_pitch(conn, source="prompt", title="t", author="ada-sharding")
    db.write_note(conn, pid, "draft", good_draft() + "\n<script>alert(1)</script>\n")
    db.write_note(
        conn,
        pid,
        "research",
        {
            "summary": "s",
            "claims": [{"claim": "c", "source": "https://a.io/x", "quote": "q"}],
            "sources": [],
            "open_questions": [],
            "competitors": [],
        },
    )
    html = client.get(f"/pitches/{pid}").text
    assert "<h2>Setup</h2>" in html and "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_requests_from_other_sites_and_hosts_are_refused(client):
    assert client.post("/scout", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/scout", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.get("/", headers={"host": "evil.example"}).status_code == 421


def test_talking_to_a_writer_streams_and_pinning_sends_direction(cfg, client, conn, monkeypatch):
    pid = db.add_pitch(conn, source="prompt", title="pgvector post", author="ada-sharding")
    r = client.post(
        "/chats", data={"author": "ada-sharding", "pitch_id": pid}, follow_redirects=False
    )
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    seen = {}

    def fake_run(cfg, conn, pitch_id, role, prompt, *, chat, on_event, **kw):
        seen.update(role=role, prompt=prompt, chat=chat, persona=kw["extra_system"])
        on_event(
            {"type": "tool_execution_start", "toolName": "read_note", "args": {"kind": "research"}}
        )
        for d in ("Lead with ", "the rollback."):
            on_event(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": d},
                }
            )
        return agent.RunResult("ok", reply="Lead with **the rollback**.")

    monkeypatch.setattr(webapp.agent, "run", fake_run)
    r = client.post(f"/chats/{cid}/messages", json={"text": "Open with the rollback plan."})
    lines = [json.loads(x) for x in r.text.splitlines()]
    assert {"type": "tool", "name": "read_note", "label": "reading the research notes"} in lines
    assert "".join(x["text"] for x in lines if x["type"] == "delta") == "Lead with the rollback."
    assert seen["role"] == "chat" and "pgvector post" in seen["prompt"]
    assert "Ada Sharding" in seen["persona"]

    page = client.get(f"/chats/{cid}").text
    assert "<strong>the rollback</strong>" in page and "Pin as direction" in page
    mine = conn.execute("select id from messages where who = 'you'").fetchone()["id"]
    assert client.post(f"/messages/{mine}/pin").json() == {"pinned": True}
    note = tools.call(Ctx(cfg, conn, pid, "writer"), "read_note", {"kind": "direction"})
    assert "Open with the rollback plan." in note and "trusted" in note


def test_chat_spend_never_counts_against_the_pitch(conn):
    pid = db.add_pitch(conn, source="prompt", title="t")
    cid = db.add_chat(conn, author="ada-sharding", pitch_id=pid)
    run = db.start_run(conn, pid, "chat", "m", "log", chat_id=cid)
    db.end_run(conn, run, "ok", {"input": 10}, 0.5)
    assert db.spent(conn, pid) == (0, 0) and db.chat_spent(conn, cid)[0] == 0.5
    assert db.open_runs(conn, pid) == []  # and a write never reaps a chat turn


def test_a_writer_can_put_a_pitch_on_the_board_from_a_chat(cfg, app, client, conn, monkeypatch):
    cid = db.add_chat(conn, author="kai-offset")
    monkeypatch.setenv("ROOM_CHAT", str(cid))
    out = tools.call(
        Ctx(cfg, conn, 0, "chat"),
        "propose_pitch",
        {"title": "Event-driven agents", "angle": "Why the log matters"},
    )
    pid = int(out.split("#")[1].split()[0])
    row = db.pitch(conn, pid)
    assert (row["source"], row["status"], row["author"]) == ("chat", "proposed", "kai-offset")
    with pytest.raises(tools.ToolError, match="cannot call"):
        tools.call(Ctx(cfg, conn, pid, "chat"), "submit_draft", {"markdown": "x"})

    monkeypatch.setattr(pipeline, "assign", fake_assign)
    client.post(f"/pitches/{pid}/approve")
    settle(app)
    row = db.pitch(conn, pid)
    assert row["status"] == "approved" and row["author"] == "kai-offset"
