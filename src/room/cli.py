"""room: the writers room's command line."""

import argparse
import json
import sys

from . import config, db, durable, pipeline, tools


def _conn(cfg):
    return db.connect(cfg.db_path)


def cmd_tools(cfg, a):
    print(json.dumps(tools.specs_for(a.role)))


def cmd_tool(cfg, a):
    """Called by the pi extension, once per tool call. Arguments arrive on stdin as JSON."""
    conn = _conn(cfg)
    ctx = tools.Ctx.from_env(cfg, conn)
    try:
        args = json.loads(sys.stdin.read() or "{}")
        print(tools.call(ctx, a.name, args))
    except tools.ToolError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except TypeError as e:  # wrong or missing arguments
        print(f"bad arguments for {a.name}: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_scout(cfg, a):
    conn = _conn(cfg)
    try:
        ids = pipeline.scout(cfg, conn)
    except pipeline.Stop as s:
        raise SystemExit(f"scout stopped: {s.reason}") from s
    print(f"\n{len(ids)} pitch(es). Review with `room pitches`, then `room approve <id>`.")


def cmd_pitch(cfg, a):
    conn = _conn(cfg)
    pid = pipeline.new_pitch(cfg, conn, a.prompt, author=a.author, poc=a.poc)
    row = db.pitch(conn, pid)
    print(f"\npitch #{pid}: {row['status']}" + (f" ({row['reason']})" if row["reason"] else ""))
    if row["status"] == "approved":
        print(f"Next: `room write {pid}`")
        if a.write:
            cmd_write(cfg, argparse.Namespace(id=pid))


def cmd_assign(cfg, a):
    conn = _conn(cfg)
    pipeline.assign(cfg, conn, a.id, forced_author=a.author, forced_poc=a.poc)


def cmd_pitches(cfg, a):
    conn = _conn(cfg)
    q = "select * from pitches" + (" where status = ?" if a.status else "") + " order by id desc"
    rows = conn.execute(q, (a.status,) if a.status else ()).fetchall()
    for r in rows:
        usd, _ = db.spent(conn, r["id"])
        poc = " +poc" if r["needs_poc"] else ""
        print(
            f"#{r['id']:<4} {r['status']:<12} {r['source']:<6} {r['author'] or '-':<16}"
            f" ${usd:<6.2f}{poc}  {r['title']}"
        )
        if a.verbose and r["reason"]:
            print(f"       {r['reason'][:300]}")
        if r["branch"]:
            print(f"       branch: {r['branch']}")


def cmd_show(cfg, a):
    conn = _conn(cfg)
    r = db.pitch(conn, a.id)
    print(json.dumps(dict(r), indent=2))
    for kind in ("pitch", "assignment", "research", "poc", "gates", "review", "escalation"):
        body = db.read_note(conn, a.id, kind)
        if body:
            print(f"\n── {kind} ──\n{body[:4000]}")


def cmd_approve(cfg, a):
    conn = _conn(cfg)
    r = db.pitch(conn, a.id)
    if r["status"] not in ("proposed", "escalated", "rejected"):
        raise SystemExit(f"pitch {a.id} is {r['status']}")
    fields = {"status": "approved", "reason": ""}
    if a.author:
        if a.author not in cfg.authors():
            raise SystemExit(f"no author {a.author!r}")
        fields["author"] = a.author
    db.update_pitch(conn, a.id, **fields)
    print(f"pitch #{a.id} approved. Next: `room write {a.id}`")


def cmd_reject(cfg, a):
    conn = _conn(cfg)
    db.update_pitch(conn, a.id, status="rejected", reason=a.reason or "rejected by a person")
    print(f"pitch #{a.id} rejected")


def cmd_write(cfg, a):
    conn = _conn(cfg)
    if durable.settings(cfg):
        branch = durable.write(cfg, conn, a.id)
    else:
        branch = pipeline.write(cfg, conn, a.id)
    if branch:
        print(
            f"\nDraft committed on {branch} in {cfg.blog}. Review it with"
            f" `git -C {cfg.blog} show {branch}`; nothing was pushed."
        )
    else:
        sys.exit(1)


def cmd_web(cfg, a):
    from . import webapp

    print(f"Writers Room on http://127.0.0.1:{webapp.PORT}")
    webapp.main()


def cmd_worker(cfg, a):
    durable.serve(cfg)


def cmd_spend(cfg, a):
    conn = _conn(cfg)
    rows = conn.execute(
        "select pitch_id, role, count(*) n, sum(usd) usd, sum(input_tokens) i,"
        " sum(output_tokens) o, sum(cache_read) cr from runs group by pitch_id, role"
        " order by pitch_id, role"
    ).fetchall()
    total = 0.0
    for r in rows:
        total += r["usd"]
        print(
            f"#{r['pitch_id']:<4} {r['role']:<11} runs {r['n']:<3} ${r['usd']:<7.3f}"
            f" in {r['i']:>9} out {r['o']:>8} cache {r['cr']:>9}"
        )
    print(
        f"total ${total:.3f}" + ("  (prices in room.toml are 0: tokens only)" if not total else "")
    )


def main(argv=None):
    p = argparse.ArgumentParser(prog="room", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scout", help="pitch posts from this week's signals")
    s.set_defaults(fn=cmd_scout)
    s = sub.add_parser("pitch", help="pitch a post from your own prompt (pre-approved)")
    s.add_argument("prompt")
    s.add_argument("--author", help="author slug; overrides the editor")
    s.add_argument("--poc", action="store_true", help="build a proof of concept on Nevia")
    s.add_argument("--write", action="store_true", help="go straight on to `room write`")
    s.set_defaults(fn=cmd_pitch)
    s = sub.add_parser("assign", help="run the editor on a pitch again")
    s.add_argument("id", type=int)
    s.add_argument("--author")
    s.add_argument("--poc", action="store_true")
    s.set_defaults(fn=cmd_assign)
    s = sub.add_parser("pitches", help="list pitches")
    s.add_argument("--status")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_pitches)
    s = sub.add_parser("show", help="a pitch and its notes")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("approve", help="approve a scouted (or escalated) pitch")
    s.add_argument("id", type=int)
    s.add_argument("--author")
    s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("reject")
    s.add_argument("id", type=int)
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_reject)
    s = sub.add_parser("write", help="research, prototype, write, review and commit a draft")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_write)
    s = sub.add_parser("web", help="the writers room in your browser")
    s.set_defaults(fn=cmd_web)
    s = sub.add_parser("worker", help="run a Temporal worker that resumes interrupted writes")
    s.set_defaults(fn=cmd_worker)
    s = sub.add_parser("spend")
    s.set_defaults(fn=cmd_spend)
    s = sub.add_parser("tools", help=argparse.SUPPRESS)
    s.add_argument("role", choices=list(tools.ROLE_TOOLS))
    s.set_defaults(fn=cmd_tools)
    s = sub.add_parser("tool", help=argparse.SUPPRESS)
    s.add_argument("name")
    s.set_defaults(fn=cmd_tool)
    a = p.parse_args(argv)
    a.fn(config.load(), a)


if __name__ == "__main__":
    main()
