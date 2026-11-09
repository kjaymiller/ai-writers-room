"""What's being talked about this week, fetched by code from the sources in room.toml. The scout
reads the result; it does not decide where to look first."""

from . import web


def collect(cfg, say=print) -> tuple[list[dict], list[str]]:
    s = cfg["scout"]
    ua, days, n = s["user_agent"], s["lookback_days"], s["per_source"]
    items, errors = [], []

    def grab(label, fn, **kw):
        try:
            got = fn(user_agent=ua, limit=n, **kw)
            items.extend(got)
            say(f"  {label}: {len(got)}")
        except Exception as e:  # one source being down is not a reason to stop
            errors.append(f"{label}: {e}")
            say(f"  {label}: failed ({e})")

    for q in s.get("hn_queries", []):
        grab(f"hn '{q}'", web.search_hn, query=q, days=days)
    for f in s.get("feeds", []):
        days_f = f.get("days", days)
        if f.get("kind") == "planet-kafka":
            grab(f["name"], web.planet_kafka, name=f["name"], days=days_f)
        else:
            grab(f["name"], web.read_feed, url=f["url"], name=f["name"], days=days_f)
    for topic in s.get("github_topics", []):
        grab(f"github topic:{topic}", web.search_github, query=f"topic:{topic}", days=days)

    # Feeds first, in the order room.toml lists them (the changelog leads), then the scored
    # sources by score. Feeds have no scores; sorted together they would always come last.
    feed_names = [f["name"] for f in s.get("feeds", [])]
    rank = {n: i for i, n in enumerate(feed_names)}
    seen, unique = set(), []
    for it in sorted(items, key=lambda i: (rank.get(i["source"], len(rank)),
                                           -(i.get("score") or 0))):  # fmt: skip
        key = web.normalize(it["url"])
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique, errors
