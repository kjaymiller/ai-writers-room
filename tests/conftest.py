import json
import subprocess
import textwrap

import pytest

from room import config, db

AUTHORS = [
    {
        "slug": "ada-sharding",
        "name": "Ada Sharding",
        "role": "PostgreSQL writer",
        "beat": "PostgreSQL",
        "bio": "b",
        "voice": "v",
        "signature": "a 'what to verify' checklist",
    },
    {
        "slug": "kai-offset",
        "name": "Kai Offset",
        "role": "Streaming writer",
        "beat": "Kafka",
        "bio": "b",
        "voice": "v",
        "signature": "draws the data flow",
    },
]

EXISTING = """---
title: Kafka topics for beginners
slug: kafka-topics-for-beginners
date: 2026-10-26
author: kai-offset
summary: Topics.
tags: [kafka]
---
Body.
"""


@pytest.fixture
def blog(tmp_path):
    b = tmp_path / "blog"
    (b / "content" / "posts").mkdir(parents=True)
    (b / "content" / "authors.json").write_text(json.dumps(AUTHORS))
    (b / "content" / "posts" / "2026-10-26-kafka-topics-for-beginners.md").write_text(EXISTING)
    # a stand-in for render-engine: writes output/<slug>.html for every post
    (b / "build.sh").write_text(
        textwrap.dedent("""\
        set -e
        mkdir -p output
        for f in content/posts/*.md; do
          slug=$(sed -n 's/^slug: *//p' "$f" | head -1)
          echo "<html>$slug</html>" > "output/$slug.html"
        done
    """)
    )
    (b / ".gitignore").write_text("output/\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(b)], check=True)
    subprocess.run(["git", "-C", str(b), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(b), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        check=True,
    )
    return b


@pytest.fixture
def cfg(tmp_path, blog, monkeypatch):
    raw = (config.ROOT / "room.toml").read_text()
    raw = raw.replace('path = "../aiven-bot-blog"', f'path = "{blog}"')
    raw = raw.replace('build = "mise run build"', 'build = "sh build.sh"')
    raw = raw.replace("input = 0.0\noutput = 0.0", "input = 3.0\noutput = 15.0", 1)
    p = tmp_path / "room.toml"
    p.write_text(raw)
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "house.md").write_text("house rules")
    monkeypatch.setenv("ROOM_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("ROOM_TEMPORAL", "off")  # durable tests turn it on themselves
    return config.load(p)


@pytest.fixture
def conn(cfg):
    return db.connect(cfg.db_path)
