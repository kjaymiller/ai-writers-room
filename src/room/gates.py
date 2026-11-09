"""Deterministic checks on a draft. A model cannot talk its way past these; it gets the list back
and fixes the draft."""

import re
from dataclasses import dataclass, field
from datetime import date

import yaml

from . import web

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PLACEHOLDER = re.compile(
    r"\b(TODO|TBD|FIXME|XXX|lorem ipsum)\b|<insert|\[citation needed\]|\{\{.*?\}\}", re.I
)
LINK = re.compile(
    r"\]\((https?://[^)\s]+)\)|<(https?://[^>\s]+)>|(?<![(<\"'])\b(https?://[^\s)\]>\"'`]+)"
)
FENCE = re.compile(r"^\s*(```|~~~)", re.M)
# Aiven names its managed services "Aiven for <Product>". These are the shorthands that slip in.
NAMING = re.compile(
    r"\bAiven (PostgreSQL|Postgres|Kafka|ClickHouse|OpenSearch|Valkey|MySQL|Grafana)\b"
)
WRITER_FIELDS = ("title", "slug", "summary", "tags", "faq")
# A link to one of these hosts never needs a source: they are the products being written about.
ALWAYS_OK = ("https://aiven.io/docs", "https://aiven.io/pricing", "https://console.aiven.io")


@dataclass
class Result:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    body: str = ""

    @property
    def ok(self):
        return not self.errors


def split_front_matter(text: str):
    if not text.startswith("---"):
        raise ValueError("the draft must start with YAML front matter between --- lines")
    parts = text.split("\n---", 2)
    if len(parts) < 2:
        raise ValueError("front matter is not closed with a --- line")
    head = parts[0].removeprefix("---")
    body = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
    meta = yaml.safe_load(head) or {}
    if not isinstance(meta, dict):
        raise ValueError("front matter must be a YAML mapping")
    return meta, body


def links(body: str) -> list[str]:
    # Code blocks hold example URLs (localhost, placeholders) that are not citations.
    prose = re.sub(r"```.*?```|~~~.*?~~~|`[^`\n]+`", " ", body, flags=re.S)
    found = []
    for m in LINK.finditer(prose):
        url = next(g for g in m.groups() if g).rstrip(".,;:")
        found.append(url)
    return found


def check(
    text: str,
    *,
    assigned_author: str,
    existing_slugs: set[str],
    sources: set[str],
    own_slug: str | None = None,
) -> Result:
    res = Result()
    try:
        meta, body = split_front_matter(text)
    except (ValueError, yaml.YAMLError) as e:
        res.errors.append(f"front matter: {e}")
        return res
    res.meta, res.body = meta, body

    for key in WRITER_FIELDS:
        if not meta.get(key):
            res.errors.append(f"front matter: `{key}` is missing or empty")
    title, slug, summary = meta.get("title"), meta.get("slug"), meta.get("summary")
    if title and (not isinstance(title, str) or len(title) > 110):
        res.errors.append("front matter: `title` must be a string of at most 110 characters")
    if slug:
        if not isinstance(slug, str) or not SLUG.match(slug):
            res.errors.append(f"front matter: `slug` {slug!r} must be lowercase-kebab-case")
        elif slug in existing_slugs and slug != own_slug:
            res.errors.append(f"front matter: slug {slug!r} is already a post on the blog")
    if summary and (not isinstance(summary, str) or len(summary) > 300):
        res.errors.append("front matter: `summary` must be one sentence, at most 300 characters")
    tags = meta.get("tags")
    if tags and not (
        isinstance(tags, list) and all(isinstance(t, str) and SLUG.match(t) for t in tags)
    ):
        res.errors.append("front matter: `tags` must be a list of lowercase-kebab-case strings")
    faq = meta.get("faq")
    if faq is not None:
        if not isinstance(faq, list) or len(faq) < 2:
            res.errors.append("front matter: `faq` needs at least two entries")
        elif not all(isinstance(f, dict) and f.get("q") and f.get("a") for f in faq):
            res.errors.append("front matter: every `faq` entry needs a `q` and an `a`")
        elif not all(isinstance(f["q"], str) and isinstance(f["a"], str) for f in faq):
            # YAML reads a bare yes/no/on/off or a number as something other than text
            res.errors.append("front matter: quote every `faq` question and answer as a string")
    if meta.get("author") and meta["author"] != assigned_author:
        res.errors.append(
            f"front matter: author is {meta['author']!r} but this post is assigned to"
            f" {assigned_author!r}. Write as the assigned author, or leave `author` out."
        )
    if meta.get("date") and not isinstance(meta["date"], date):
        res.errors.append("front matter: `date` must be YYYY-MM-DD, or leave it out")

    if "tl;dr" not in body[:1500].lower():
        res.errors.append("body: a TL;DR must come first, within the opening few paragraphs")
    if not re.search(r"^## ", body, re.M):
        res.errors.append("body: use `##` section headings")
    if len(body.split()) < 400:
        res.errors.append(f"body: {len(body.split())} words is too short for a post (400 minimum)")
    if len(FENCE.findall(body)) % 2:
        res.errors.append("body: a code fence is opened and never closed")
    for m in PLACEHOLDER.finditer(body):
        res.errors.append(
            f"body: placeholder text {m.group(0)!r}; replace it or mark the claim [TO VERIFY]"
        )
    if re.search(r"^#\s", body, re.M):
        res.errors.append("body: no `#` level-1 heading; the template prints the title")

    allowed = {web.normalize(s) for s in sources}
    unsourced = sorted(
        {u for u in links(body) if web.normalize(u) not in allowed and not u.startswith(ALWAYS_OK)}
    )
    for u in unsourced:
        res.errors.append(
            f"links: {u} is not in the research brief's sources and was not fetched for this post."
            " Cite only pages that were read, or drop the link."
        )

    for m in sorted(set(NAMING.findall(body))):
        res.warnings.append(f"naming: 'Aiven {m}' should be 'Aiven for {m}'")
    n_verify = body.count("[TO VERIFY]")
    if n_verify:
        res.warnings.append(f"{n_verify} claim(s) marked [TO VERIFY]")
    return res


def format_result(res: Result) -> str:
    if res.ok:
        lines = ["All gates passed."]
    else:
        lines = [f"{len(res.errors)} gate failure(s). Fix every one and submit again:"]
        lines += [f"- {e}" for e in res.errors]
    if res.warnings:
        lines += ["Warnings (not blocking):"] + [f"- {w}" for w in res.warnings]
    return "\n".join(lines)
