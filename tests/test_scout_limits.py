import json

import httpx
import pytest

from room import agent, db, tools, web
from room.tools import Ctx, ToolError

from .test_agent_and_cell import FakeProc, assistant


def test_signals_reach_the_scout_as_short_lines(cfg, conn):
    items = [{"source": "hn", "title": f"Story {i}", "url": f"https://s.example/{i}",
              "discussion": f"https://news.example/{i}", "score": 100 - i, "comments": 5}
             for i in range(95)]  # fmt: skip
    db.write_note(conn, 0, "signals", items)
    text = tools.call(Ctx(cfg, conn, 0, "scout"), "read_note", {"kind": "signals"})
    assert "## Hacker News" in text
    assert (
        "- Story 0 <https://s.example/0> [100pts 5c] (discussion: https://news.example/0)" in text
    )
    assert "80 more not shown" in text  # 15 per source
    assert len(text) < len(db.read_note(conn, 0, "signals")) * 0.6


def test_a_role_that_keeps_browsing_is_told_to_submit(cfg, conn, monkeypatch):
    monkeypatch.setenv("ROOM_RUN", "7")
    monkeypatch.setattr(web, "search_hn", lambda **_: [])
    c = Ctx(cfg, conn, 0, "scout")
    cap = cfg["limits"]["tool_calls"]["scout"]
    outs = [tools.call(c, "search_hn", {"query": "pg"}) for _ in range(cap)]
    assert "1 lookup left. Then call submit_pitches." in outs[-2]
    with pytest.raises(ToolError, match="call submit_pitches now"):
        tools.call(c, "search_hn", {"query": "pg"})
    tools.call(c, "read_note", {"kind": "house"})  # reading notes is never refused


def test_a_killed_run_keeps_what_it_spent(cfg, conn, monkeypatch):
    monkeypatch.setenv("AIVEN_AI_GATEWAY_KEY", "k")

    class Killed(FakeProc):
        def wait(self):  # the container went away mid-run: the run row is never ended
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        agent.run(
            cfg,
            conn,
            0,
            "scout",
            "go",
            say=lambda _: 0,
            popen=lambda *a, **k: Killed([assistant(100_000, 1_000)]),
        )
    agent.reap(conn, 0, say=lambda _: 0)
    row = conn.execute("select outcome, input_tokens, usd from runs").fetchone()
    assert row["outcome"] == "abandoned" and row["input_tokens"] == 100_000 and row["usd"] > 0


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Aiven Changelog</title>
<item><title><![CDATA[Valkey Bloom module]]></title><link>https://aiven.io/changelog/a1</link>
<pubDate>{recent}</pubDate>
<description>&lt;p&gt;Bloom filters on Aiven for Valkey&lt;/p&gt;</description></item>
<item><title>Old thing</title><link>https://aiven.io/changelog/old</link>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item></channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Planet Valkey</title>
<entry><title>Valkey 9.1 is out</title><link rel="alternate" href="https://valkey.io/blog/9-1"/>
<updated>{recent_iso}</updated><author><name>Valkey team</name></author>
<summary>What changed</summary></entry></feed>"""


@pytest.fixture
def served(monkeypatch):
    from datetime import UTC, datetime
    from email.utils import format_datetime

    now = datetime.now(UTC)
    pages = {
        "/changelog/feed.xml": RSS.format(recent=format_datetime(now)),
        "/atom.xml": ATOM.format(recent_iso=now.isoformat()),
        "/api/planet-kafka": json.dumps(
            {
                "hasLoadMore": False,
                "articles": [
                    {
                        "title": "Kafka 4.2 tiered storage",
                        "link": "https://kai.example/tiered",
                        "author": "Kai W",
                        "date": format_datetime(now),
                        "content": "Notes",
                        "feedName": "Kai's blog",
                    },
                    {
                        "title": "Ancient",
                        "link": "https://kai.example/old",
                        "date": "Mon, 01 Jan 2024 00:00:00 GMT",
                    },
                ],
            }
        ),
    }

    def handler(request):
        return httpx.Response(200, text=pages[request.url.path])

    real = httpx.Client
    monkeypatch.setattr(
        web.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    monkeypatch.setattr(web, "check_public", lambda url: None)


def test_rss_and_atom_feeds_keep_only_recent_items(served):
    rss = web.read_feed(
        "https://aiven.io/changelog/feed.xml",
        name="aiven-changelog",
        days=14,
        limit=10,
        user_agent="t",
    )
    assert [i["title"] for i in rss] == ["Valkey Bloom module"]
    assert rss[0]["summary"] == "Bloom filters on Aiven for Valkey"
    atom = web.read_feed(
        "https://planet.valkey.io/atom.xml", name="planet-valkey", days=21, limit=10, user_agent="t"
    )
    assert atom[0]["url"] == "https://valkey.io/blog/9-1" and atom[0]["author"] == "Valkey team"


def test_planet_kafka_is_read_from_its_api(served):
    got = web.planet_kafka(name="planet-kafka", days=7, limit=10, user_agent="t")
    assert [a["title"] for a in got] == ["Kafka 4.2 tiered storage"]
    assert got[0]["summary"] == "Notes (via Kai's blog)"


def test_the_scout_reads_every_source_and_never_reddit(cfg, served, monkeypatch):
    from room import signals

    monkeypatch.setattr(
        web,
        "search_hn",
        lambda **kw: [
            {"source": "hn", "title": "Show HN", "url": "https://h.example", "score": 300}
        ],
    )
    monkeypatch.setattr(web, "search_github", lambda **kw: [])
    cfg.raw["scout"]["feeds"] = [
        f for f in cfg.raw["scout"]["feeds"] if f["name"] != "planet-postgresql"
    ]  # no fixture page for it
    items, errors = signals.collect(cfg, say=lambda _: 0)
    assert not errors
    order = [i["source"] for i in items]
    assert order[0] == "aiven-changelog"  # what Aiven shipped leads, scores or not
    assert {"planet-kafka", "planet-valkey", "hn"} <= set(order)
    assert not any("reddit" in s for s in order)
    assert "search_reddit" not in tools.SPECS and "reddit" not in cfg.raw["scout"]
