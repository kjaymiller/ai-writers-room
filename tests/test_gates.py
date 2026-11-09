from room import gates

BODY_WORDS = " ".join(["word"] * 420)


def draft(front="", body=None):
    front = front or (
        "title: Pgvector on Aiven\nslug: pgvector-on-aiven\nsummary: One sentence.\n"
        "tags: [postgresql, pgvector]\nfaq:\n"
        '  - q: one?\n    a: Yes, it does.\n  - q: two?\n    a: "no"\n'
    )
    body = (
        body
        if body is not None
        else (
            "**TL;DR**\n\n- a point\n\n## Setup\n\n"
            + BODY_WORDS
            + "\n\nSee [the docs](https://aiven.io/docs/products/postgresql) and"
            " [the source](https://example.com/page).\n"
        )
    )
    return f"---\n{front}---\n{body}"


def run(text, sources=("https://example.com/page",), slugs=(), own=None):
    return gates.check(
        text,
        assigned_author="ada-sharding",
        existing_slugs=set(slugs),
        sources=set(sources),
        own_slug=own,
    )


def test_a_good_draft_passes():
    res = run(draft())
    assert res.ok, res.errors
    assert res.meta["slug"] == "pgvector-on-aiven"


def test_a_link_nobody_read_fails():
    res = run(draft(), sources=())
    assert any("https://example.com/page" in e for e in res.errors)


def test_links_inside_code_are_not_citations():
    body = "TL;DR\n\n## A\n\n" + BODY_WORDS + "\n\n```\ncurl http://localhost:8000/x\n```\n"
    assert run(draft(body=body)).ok


def test_existing_slug_fails_unless_it_is_this_pitch():
    assert not run(draft(), slugs={"pgvector-on-aiven"}).ok
    assert run(draft(), slugs={"pgvector-on-aiven"}, own="pgvector-on-aiven").ok


def test_wrong_author_fails():
    text = draft().replace("slug: pgvector", "author: kai-offset\nslug: pgvector")
    assert any("assigned" in e for e in run(text).errors)


def test_missing_tldr_short_body_placeholders_and_open_fence():
    body = "## A\n\nTODO write this\n\n```python\nx = 1\n"
    errors = " ".join(run(draft(body=body)).errors)
    for needle in ("TL;DR", "too short", "placeholder", "code fence"):
        assert needle in errors


def test_no_front_matter():
    assert "front matter" in run("just text").errors[0]


def test_naming_is_a_warning_not_an_error():
    body = "TL;DR\n\n## A\n\nUse Aiven PostgreSQL today. " + BODY_WORDS
    res = run(draft(body=body))
    assert res.ok
    assert any("Aiven for PostgreSQL" in w for w in res.warnings)


def test_yaml_booleans_in_faq_are_caught():
    text = draft().replace("a: Yes, it does.", "a: yes")
    assert any("quote every" in e for e in run(text).errors)
