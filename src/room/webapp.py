"""The writers room in a browser: the pitch board, each pitch's story, and conversations with the
writers. `mise run web` serves it on http://127.0.0.1:8250.

It is a local tool for the one person who runs the room. It binds to 127.0.0.1, refuses requests
whose Host is not this machine (DNS rebinding), and refuses cross-site POSTs, because a POST here
spends model money. Everything it does goes through the same code as the CLI: pipeline, durable,
tools. Long work (a write, a scout, an editor pass) runs on a background thread and reports into
the pitch's activity log, which the page follows.
"""

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from . import agent, config, db, durable, gates, pipeline

HERE = Path(__file__).parent / "web"
PORT = 8250

# Plain words for each status, from the person's side of the desk.
COLUMNS = [
    ("Needs you", {"escalated", "failed"}, "The room stopped and wants a decision."),
    ("Ideas", {"proposed"}, "Pitches waiting for your OK."),
    ("Ready to write", {"approved"}, "Approved. Start the write when you like."),
    ("In the room", {"researching", "prototyping", "writing", "reviewing"}, "Being worked on."),
    ("Drafted", {"drafted"}, "On a branch of the blog, waiting for you to merge."),
]
DOING = {
    "researching": "is researching",
    "prototyping": "is building the proof of concept",
    "writing": "is writing",
    "reviewing": "'s draft is with the fact-checker",
}

md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
_link_open = md.renderer.rules.get("link_open")


def _external_links(tokens, idx, options, env):
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer")
    return md.renderer.renderToken(tokens, idx, options, env)


md.renderer.rules["link_open"] = _external_links


def render_md(text):
    return md.render(text or "")


def initials(name):
    return "".join(w[0] for w in name.split()[:2]).upper()


def hue(slug):
    return sum(map(ord, slug or "?")) * 47 % 360


def ago(ts):
    if not ts:
        return ""
    s = time.time() - ts
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return "just now"


class Jobs:
    """Background work started from the page, one at a time per pitch (and one scout)."""

    def __init__(self):
        self.threads: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()

    def running(self, key):
        t = self.threads.get(key)
        return bool(t and t.is_alive())

    def start(self, key, fn):
        with self.lock:
            if self.running(key):
                return False
            t = threading.Thread(target=fn, name=f"room-{key}", daemon=True)
            self.threads[key] = t
            t.start()
            return True


def activity_path(cfg, pitch_id):
    return cfg.job_dir(pitch_id) / "activity.log"


def logger(cfg, pitch_id):
    path = activity_path(cfg, pitch_id)

    def say(line):
        stamp = datetime.now().strftime("%H:%M:%S")
        with path.open("a") as f:
            f.write(f"{stamp} {line}\n")

    return say


def create_app(cfg: config.Config | None = None) -> FastAPI:
    cfg = cfg or config.load()
    app = FastAPI(title="Writers Room", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.filters.update(md=render_md, initials=initials, hue=hue, ago=ago)
    jobs = Jobs()
    app.state.jobs = jobs  # tests wait on it
    chat_locks: dict[int, threading.Lock] = {}
    allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "testserver"}
    extra = os.environ.get("ROOM_WEB_HOSTS", "")
    allowed_hosts |= {h for h in extra.split(",") if h}

    def conn():
        return db.connect(cfg.db_path)

    def key_ok():
        return bool(os.environ.get(cfg["gateway"]["key_env"]))

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return PlainTextResponse("This page only answers on 127.0.0.1.", status_code=421)
        if request.method == "POST":
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site")
            if (origin and origin.split("://", 1)[-1] not in allowed_hosts) or site == "cross-site":
                return PlainTextResponse("Cross-site requests are refused.", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' https://fonts.googleapis.com 'unsafe-inline';"
            " font-src https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def page(request, name, **ctx):
        authors = cfg.authors()
        return tpl.TemplateResponse(
            request,
            name,
            {
                "authors": authors,
                "key_ok": key_ok(),
                "key_env": cfg["gateway"]["key_env"],
                "durable": bool(durable.settings(cfg)),
                **ctx,
            },
        )

    def named(text):
        for slug, a in cfg.authors().items():
            text = text.replace(f"(to {slug})", f"(to {a['name'].split()[0]})")
        return text

    def pitch_view(c, row):
        usd, tokens = db.spent(c, row["id"])
        status = row["status"]
        busy = jobs.running(f"pitch-{row['id']}")
        return {
            **dict(row),
            "usd": usd,
            "tokens": tokens,
            "busy": busy,
            "doing": DOING.get(status),
            "evidence": json.loads(row["evidence"] or "[]"),
            "stalled": status in DOING and not busy,
        }

    # ── the board ─────────────────────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def board(request: Request):
        c = conn()
        rows = [pitch_view(c, r) for r in c.execute("select * from pitches order by id desc")]
        columns = [
            {"title": t, "blurb": b, "pitches": [p for p in rows if p["status"] in s]}
            for t, s, b in COLUMNS
        ]
        passed = [p for p in rows if p["status"] == "rejected"]
        total = c.execute("select coalesce(sum(usd), 0) from runs").fetchone()[0]
        return page(
            request,
            "board.html",
            columns=columns,
            passed=passed,
            total_usd=total,
            scouting=jobs.running("scout"),
        )

    @app.post("/scout")
    def scout():
        if not key_ok():
            raise HTTPException(400, "the model key is not set")

        def go():
            c = conn()
            say = logger(cfg, 0)
            try:
                pipeline.scout(cfg, c, say=say)
            except (SystemExit, pipeline.Stop) as e:
                say(f"[room] scout stopped: {e}")

        jobs.start("scout", go)
        return RedirectResponse("/", status_code=303)

    @app.get("/scout/log", response_class=PlainTextResponse)
    def scout_log():
        path = activity_path(cfg, 0)
        return path.read_text()[-20_000:] if path.exists() else ""

    # ── pitches ───────────────────────────────────────────────────────────────────────────────

    @app.get("/pitches/new", response_class=HTMLResponse)
    def new_pitch_form(request: Request, author: str = ""):
        return page(request, "new.html", chosen=author)

    @app.post("/pitches")
    def create_pitch(
        prompt: str = Form(...),
        author: str = Form(""),
        poc: bool = Form(False),
        write_now: bool = Form(False),
    ):
        if not key_ok():
            raise HTTPException(400, "the model key is not set")
        prompt = prompt.strip()
        if not prompt:
            raise HTTPException(400, "say what the post should be about")
        if author and author not in cfg.authors():
            raise HTTPException(400, f"no writer {author!r}")
        c = conn()
        title = prompt.splitlines()[0][:110]
        pid = db.add_pitch(
            c, source="prompt", title=title, prompt=prompt, author=author or None, needs_poc=poc
        )
        say = logger(cfg, pid)

        def go():
            c2 = conn()
            say("[room] the editor is reading your pitch")
            pipeline.assign(cfg, c2, pid, forced_author=author or None, forced_poc=poc, say=say)
            if write_now and db.pitch(c2, pid)["status"] == "approved":
                run_write(c2, pid, say)

        jobs.start(f"pitch-{pid}", go)
        return RedirectResponse(f"/pitches/{pid}", status_code=303)

    def run_write(c, pid, say):
        try:
            if durable.settings(cfg):
                durable.write(cfg, c, pid, say=say)
            else:
                pipeline.write(cfg, c, pid, say=say)
        except SystemExit as e:
            say(f"[room] could not write: {e}")
        except Exception as e:
            say(f"[room] the write stopped with an error: {type(e).__name__}: {e}")

    @app.get("/pitches/{pid}", response_class=HTMLResponse)
    def pitch_page(request: Request, pid: int):
        c = conn()
        try:
            row = db.pitch(c, pid)
        except SystemExit as e:
            raise HTTPException(404, "no such pitch") from e
        notes = {
            k: db.read_json(c, pid, k)
            for k in ("pitch", "assignment", "research", "poc", "review", "gates", "escalation")
        }
        draft = db.read_note(c, pid, "draft")
        draft_meta, draft_body = {}, ""
        if draft:
            try:
                draft_meta, draft_body = gates.split_front_matter(draft)
            except ValueError:
                draft_body = draft
        chats = c.execute(
            "select c.*, (select count(*) from messages m where m.chat_id = c.id) n"
            " from chats c where pitch_id = ? order by id desc",
            (pid,),
        ).fetchall()
        runs = c.execute(
            "select role, model, outcome, usd, started, ended from runs where pitch_id = ?"
            " and chat_id is null order by id",
            (pid,),
        ).fetchall()
        return page(
            request,
            "pitch.html",
            p=pitch_view(c, row),
            notes=notes,
            draft_meta=draft_meta,
            draft_body=draft_body,
            direction=named(db.pinned_direction(c, pid)),
            chats=chats,
            runs=runs,
            blog=cfg.blog,
            budget=cfg["budget"]["pitch_usd"],
        )

    @app.get("/pitches/{pid}/log", response_class=PlainTextResponse)
    def pitch_log(pid: int):
        path = activity_path(cfg, pid)
        return path.read_text()[-30_000:] if path.exists() else ""

    @app.get("/pitches/{pid}/state")
    def pitch_state(pid: int):
        c = conn()
        row = db.pitch(c, pid)
        return JSONResponse({"status": row["status"], "busy": jobs.running(f"pitch-{pid}")})

    @app.post("/pitches/{pid}/approve")
    def approve(pid: int):
        c = conn()
        row = db.pitch(c, pid)
        if row["status"] not in ("proposed", "escalated", "rejected", "failed"):
            raise HTTPException(409, f"pitch is {row['status']}")
        if db.read_note(c, pid, "assignment"):
            db.update_pitch(c, pid, status="approved", reason="")
            return RedirectResponse(f"/pitches/{pid}", status_code=303)
        if not key_ok():
            raise HTTPException(400, "the model key is not set")
        say = logger(cfg, pid)

        def go():  # a pitch from a chat has no assignment yet: the editor plans it, you approved it
            c2 = conn()
            say("[room] the editor is planning the post you approved")
            pipeline.assign(cfg, c2, pid, forced_author=row["author"], say=say)
            if db.read_note(c2, pid, "assignment"):
                db.update_pitch(c2, pid, status="approved", reason="")

        jobs.start(f"pitch-{pid}", go)
        return RedirectResponse(f"/pitches/{pid}", status_code=303)

    @app.post("/pitches/{pid}/reject")
    def reject(pid: int, reason: str = Form("")):
        c = conn()
        db.update_pitch(c, pid, status="rejected", reason=reason.strip() or "passed on by you")
        return RedirectResponse("/", status_code=303)

    @app.post("/pitches/{pid}/write")
    def write(pid: int):
        if not key_ok():
            raise HTTPException(400, "the model key is not set")
        c = conn()
        try:
            pipeline.check_writable(c, pid)
        except SystemExit as e:
            raise HTTPException(409, str(e)) from e
        say = logger(cfg, pid)

        def go():
            say("[room] starting the write")
            run_write(conn(), pid, say)

        jobs.start(f"pitch-{pid}", go)
        return RedirectResponse(f"/pitches/{pid}", status_code=303)

    # ── writers and conversations ─────────────────────────────────────────────────────────────

    @app.get("/writers", response_class=HTMLResponse)
    def writers(request: Request):
        c = conn()
        stats = {
            a: {
                "pitches": c.execute(
                    "select count(*) from pitches where author = ?", (a,)
                ).fetchone()[0],
                "drafted": c.execute(
                    "select count(*) from pitches where author = ? and status = 'drafted'", (a,)
                ).fetchone()[0],
            }
            for a in cfg.authors()
        }
        chats = c.execute(
            "select c.*, (select body from messages m where m.chat_id = c.id order by id desc"
            " limit 1) last, (select created from messages m where m.chat_id = c.id order by id"
            " desc limit 1) at from chats c order by coalesce(at, c.created) desc limit 30"
        ).fetchall()
        return page(request, "writers.html", stats=stats, chats=chats)

    @app.post("/chats")
    def new_chat(author: str = Form(...), pitch_id: int = Form(0)):
        if author not in cfg.authors():
            raise HTTPException(400, f"no writer {author!r}")
        c = conn()
        title = db.pitch(c, pitch_id)["title"] if pitch_id else ""
        cid = db.add_chat(c, author=author, pitch_id=pitch_id, title=title)
        return RedirectResponse(f"/chats/{cid}", status_code=303)

    def get_chat(c, cid):
        chat = c.execute("select * from chats where id = ?", (cid,)).fetchone()
        if chat is None:
            raise HTTPException(404, "no such conversation")
        return chat

    @app.get("/chats/{cid}", response_class=HTMLResponse)
    def chat_page(request: Request, cid: int):
        c = conn()
        chat = get_chat(c, cid)
        msgs = c.execute("select * from messages where chat_id = ? order by id", (cid,)).fetchall()
        pitch = db.pitch(c, chat["pitch_id"]) if chat["pitch_id"] else None
        usd, _ = db.chat_spent(c, cid)
        return page(
            request,
            "chat.html",
            chat=chat,
            a=cfg.authors()[chat["author"]],
            msgs=msgs,
            pitch=pitch,
            usd=usd,
            cap=cfg["budget"].get("chat_usd", 1.0),
        )

    @app.post("/chats/{cid}/messages")
    async def send(request: Request, cid: int):
        body = await request.json()
        text = str(body.get("text", "")).strip()[:8000]
        if not text:
            raise HTTPException(400, "empty message")
        if not key_ok():
            raise HTTPException(400, "the model key is not set")
        c = conn()
        chat = get_chat(c, cid)
        lock = chat_locks.setdefault(cid, threading.Lock())
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "the writer is still answering")
        mid = db.add_message(c, cid, "you", text)
        events: list = []
        done = threading.Event()

        def turn():
            c2 = conn()
            a = cfg.authors()[chat["author"]]
            first = (
                c2.execute("select count(*) from runs where chat_id = ?", (cid,)).fetchone()[0] == 0
            )
            prompt = text
            if first and chat["pitch_id"]:
                p = db.pitch(c2, chat["pitch_id"])
                prompt = (
                    f"(This conversation is about pitch #{p['id']}, {p['title']!r}, status"
                    f" {p['status']}. Read its notes when you need them.)\n\n{text}"
                )
            try:
                res = agent.run(
                    cfg,
                    c2,
                    chat["pitch_id"],
                    "chat",
                    prompt,
                    extra_system=pipeline.persona(cfg, chat["author"]),
                    say=lambda _: None,
                    chat={
                        "id": cid,
                        "session_id": chat["session_id"],
                        "cap_usd": cfg["budget"].get("chat_usd", 1.0),
                    },
                    on_event=events.append,
                )
                reply = res.reply.strip() if res.outcome == "ok" else ""
                if res.outcome != "ok":
                    reply = f"_({a['name']} couldn't answer: {res.outcome})_"
                db.add_message(c2, cid, "writer", reply or "_(no reply)_")
                events.append({"type": "_done", "usd": res.usd, "outcome": res.outcome})
            except Exception as e:
                db.add_message(c2, cid, "writer", f"_(error: {e})_")
                events.append({"type": "_done", "outcome": str(e)})
            finally:
                lock.release()
                done.set()

        threading.Thread(target=turn, daemon=True).start()

        def stream():
            yield json.dumps({"type": "sent", "id": mid}) + "\n"
            i = 0
            while True:
                while i < len(events):
                    ev = events[i]
                    i += 1
                    out = stream_event(ev)
                    if out:
                        yield json.dumps(out) + "\n"
                    if ev.get("type") == "_done":
                        return
                if done.is_set() and i >= len(events):
                    return
                time.sleep(0.05)

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.post("/messages/{mid}/pin")
    def pin(mid: int):
        c = conn()
        m = c.execute("select * from messages where id = ?", (mid,)).fetchone()
        if m is None or m["who"] != "you":
            raise HTTPException(400, "only your own messages can be pinned as direction")
        c.execute("update messages set pinned = 1 - pinned where id = ?", (mid,))
        c.commit()
        return JSONResponse({"pinned": not m["pinned"]})

    return app


def stream_event(ev):
    """pi event -> what the chat page shows while the writer is answering."""
    t = ev.get("type")
    if t == "message_update":
        a = ev.get("assistantMessageEvent") or {}
        if a.get("type") == "text_delta":
            return {"type": "delta", "text": a.get("delta", "")}
        if a.get("type") == "text_start":
            return {"type": "restart"}  # a new text block after a tool call
    if t == "tool_execution_start":
        return {"type": "tool", "name": ev.get("toolName"), "label": tool_label(ev)}
    if t == "_done":
        return {"type": "done", "outcome": ev.get("outcome"), "usd": ev.get("usd", 0)}
    return None


def tool_label(ev):
    args = ev.get("args") or {}
    name = ev.get("toolName")
    return {
        "fetch_url": f"reading {re.sub(r'^https?://', '', str(args.get('url', '')))[:60]}",
        "search_hn": f"searching Hacker News for “{args.get('query', '')}”",
        "search_github": f"searching GitHub for “{args.get('query', '')}”",
        "aiven_docs": f"looking in the Aiven docs for {' '.join(args.get('terms', []))}",
        "read_note": f"reading the {args.get('kind', '')} notes",
        "list_posts": "looking through the blog",
        "read_post": f"rereading {args.get('slug', '')}",
        "propose_pitch": "putting a pitch on the board",
    }.get(name, name)


def main():
    import uvicorn

    # 127.0.0.1 on a laptop. In a container ROOM_WEB_BIND=0.0.0.0, and compose publishes the port
    # on the host's loopback only, so it is still reachable from this machine alone.
    host = os.environ.get("ROOM_WEB_BIND", "127.0.0.1")
    uvicorn.run(create_app(), host=host, port=PORT, log_level="warning")
