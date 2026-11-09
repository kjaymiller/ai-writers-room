"""The room's running order. Code decides which role runs next, with what in front of it, and what
happens when a role fails; each role only does its one job.

scout:   signals (code) -> scout pitches -> editor per pitch -> 'proposed' (a person approves)
pitch:   a person's prompt -> editor -> 'approved'
write:   researcher -> [poc on Nevia] -> writer (gates in the loop) -> reviewer
         -> a committed branch on the blog
"""

import asyncio
import json
import time
from datetime import date

from . import agent, cell, db, flow, gates, publish, signals
from .config import Config

Stop = flow.Stop


def persona(cfg, slug):
    a = cfg.authors()[slug]
    return (
        f"## You are writing as {a['name']} ({a['slug']})\n"
        f"Role: {a['role']}\nBeat: {a['beat']}\nBio: {a['bio']}\nVoice: {a['voice']}\n"
        f"Signature move (do it): {a['signature']}"
    )


def check_budget(cfg, conn, pitch_id):
    usd, tokens = db.spent(conn, pitch_id)
    b = cfg["budget"]
    if usd >= b["pitch_usd"] or tokens >= b["pitch_tokens"]:
        raise Stop(
            "escalated",
            f"budget spent: ${usd:.2f} and {tokens} tokens. Raise [budget] in"
            " room.toml and run `room write` again to continue.",
        )


def escalation(conn, pitch_id, since_version):
    rows = conn.execute(
        "select body, version from notes where pitch_id = ? and kind = 'escalation'"
        " order by version desc limit 1",
        (pitch_id,),
    ).fetchone()
    if rows and rows["version"] > since_version:
        e = json.loads(rows["body"])
        steps = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(e["next_steps"]))
        return f"{e['role']} escalated: {e['reason']}\n{steps}"
    return None


def note_version(conn, pitch_id, kind):
    (v,) = conn.execute(
        "select coalesce(max(version), 0) from notes where pitch_id = ? and kind = ?",
        (pitch_id, kind),
    ).fetchone()
    return v


def run_role(cfg, conn, pitch_id, role, prompt, expect, *, say, extra_system="", runner=None):
    """Run a role and require that it submitted `expect` (a note kind). Returns the new note."""
    runner = runner or agent.run
    check_budget(cfg, conn, pitch_id)
    before, esc_before = (
        note_version(conn, pitch_id, expect),
        note_version(conn, pitch_id, "escalation"),
    )
    say(f"[{role}] {cfg.model(role)}")
    res = runner(cfg, conn, pitch_id, role, prompt, extra_system=extra_system, say=say)
    say(f"[{role}] {res.outcome}, ${res.usd:.3f}, {sum(res.usage.values())} tokens")
    if esc := escalation(conn, pitch_id, esc_before):
        raise Stop("escalated", esc)
    if note_version(conn, pitch_id, expect) > before:
        return db.read_note(conn, pitch_id, expect)
    if res.outcome == "over budget":
        raise Stop("escalated", f"{role} ran out of budget before it submitted")
    return None


# ── scouting ──────────────────────────────────────────────────────────────────────────────────


def scout(cfg: Config, conn, say=print, runner=None):
    say("[signals] collecting")
    items, errors = signals.collect(cfg, say)
    if not items:
        raise SystemExit("no signals came back: " + "; ".join(errors))
    db.write_note(conn, 0, "signals", items)
    before = note_version(conn, 0, "submitted_pitches")
    prompt = (
        f"Today is {date.today()}. Read the signals note ({len(items)} items) and the blog's"
        " existing posts, then pitch up to "
        f"{cfg['scout']['max_pitches']} posts. Call submit_pitches once."
    )
    run_role(cfg, conn, 0, "scout", prompt, "submitted_pitches", say=say, runner=runner)
    if note_version(conn, 0, "submitted_pitches") == before:
        raise SystemExit("the scout finished without submitting pitches")
    ids = db.read_json(conn, 0, "submitted_pitches")
    for pid in ids:
        assign(cfg, conn, pid, say=say, runner=runner)
    return ids


# ── assignment ────────────────────────────────────────────────────────────────────────────────


def pitch_note(row):
    return {
        "title": row["title"],
        "angle": row["angle"],
        "source": row["source"],
        "prompt": row["prompt"],
        "evidence": json.loads(row["evidence"]),
        "suggested_author": row["author"],
        "suggested_poc": bool(row["needs_poc"]),
    }


def assign(cfg, conn, pitch_id, *, forced_author=None, forced_poc=False, say=print, runner=None):
    row = db.pitch(conn, pitch_id)
    db.write_note(conn, pitch_id, "pitch", pitch_note(row))
    authors = ", ".join(f"{a['slug']} ({a['beat']})" for a in cfg.authors().values())
    prompt = (
        f"Decide on pitch #{pitch_id}: {row['title']!r}. Authors: {authors}."
        f" POC builds are {'available' if cfg['poc']['enabled'] else 'switched off'}."
    )
    try:
        a = run_role(cfg, conn, pitch_id, "editor", prompt, "assignment", say=say, runner=runner)
    except Stop as s:
        db.update_pitch(conn, pitch_id, status=s.status, reason=s.reason)
        return
    if a is None:
        db.update_pitch(conn, pitch_id, status="failed", reason="the editor did not decide")
        return
    a = json.loads(a)
    # Code applies the rules on top of the editor's call. A person's word beats the editor's.
    author = forced_author or a["author"]
    needs_poc = (forced_poc or a["needs_poc"]) and cfg["poc"]["enabled"]
    if row["source"] == "prompt":
        if a["decision"] != "accept":
            # A person asked for this post. The editor can warn, not refuse.
            status, reason = "escalated", f"editor says {a['decision']}: {'; '.join(a['reasons'])}"
            if a.get("duplicate_of"):
                reason += f" (duplicate of {a['duplicate_of']})"
        else:
            status, reason = "approved", ""
    else:
        status = "proposed" if a["decision"] == "accept" else "rejected"
        reason = "; ".join(a["reasons"])
    db.update_pitch(
        conn, pitch_id, status=status, reason=reason, author=author, needs_poc=int(needs_poc)
    )
    if forced_author or forced_poc:
        a.update(author=author, needs_poc=needs_poc)
        if needs_poc and not a.get("poc_goal"):
            a["poc_goal"] = "Show the post's main workflow running end to end."
        db.write_note(conn, pitch_id, "assignment", a)
    say(
        f"[editor] pitch #{pitch_id}: {a['decision']} -> {status}, {author}, value {a['value']}"
        + (", with POC" if needs_poc else "")
    )


def new_pitch(cfg, conn, text, *, author=None, poc=False, say=print, runner=None):
    if author and author not in cfg.authors():
        raise SystemExit(f"no author {author!r}; choose from {sorted(cfg.authors())}")
    title = text.strip().splitlines()[0][:110]
    pid = db.add_pitch(
        conn, source="prompt", title=title, prompt=text.strip(), author=author, needs_poc=poc
    )
    assign(cfg, conn, pid, forced_author=author, forced_poc=poc, say=say, runner=runner)
    return pid


# ── writing ───────────────────────────────────────────────────────────────────────────────────

# A pitch in one of these may be written (again). An in-progress status means an earlier write was
# interrupted; under Temporal the workflow id makes a second concurrent write join the first.
WRITABLE = {"approved", "escalated", "failed", "researching", "prototyping", "writing", "reviewing"}


def check_writable(conn, pitch_id):
    row = db.pitch(conn, pitch_id)
    if row["status"] not in WRITABLE:
        raise SystemExit(
            f"pitch {pitch_id} is {row['status']}; it must be approved first"
            + (" (`room approve`)" if row["status"] == "proposed" else "")
        )
    if not db.read_note(conn, pitch_id, "assignment"):
        raise SystemExit(f"pitch {pitch_id} has no assignment; run `room assign {pitch_id}`")


def limits(cfg):
    lim = cfg["limits"]
    return {k: lim[k] for k in ("max_write_rounds", "max_review_rounds")}


def write(cfg: Config, conn, pitch_id, say=print, runner=None, publisher=None, cells=cell):
    """Write a pitch in this process. Without Temporal; `durable.write` is the durable way."""
    check_writable(conn, pitch_id)
    stages = Stages(cfg, conn, say=say, runner=runner, publisher=publisher, cells=cells)

    async def step(name, *args):
        return stages.call(name, *args)

    out = asyncio.run(flow.run(step, pitch_id, limits(cfg)))
    return out.get("branch")


class Stages:
    """Each step of a write. Every stage is safe to run again after a crash: it checks what is
    already done (a note, a branch, a computer) before it does or pays for anything."""

    def __init__(
        self, cfg, conn, *, say=print, runner=None, publisher=None, cells=cell, durable=False
    ):
        self.cfg, self.conn, self.say, self.durable = cfg, conn, say, durable
        self.runner = runner
        self.publisher = publisher or publish.publish
        self.cells = cells

    def call(self, name, *args):
        try:
            return getattr(self, name)(*args)
        except Stop as e:
            return {flow.STOP: [e.status, e.reason]}

    def _role(self, pid, role, prompt, expect, **kw):
        return run_role(
            self.cfg, self.conn, pid, role, prompt, expect, say=self.say, runner=self.runner, **kw
        )

    def prepare(self, pid):
        row = db.pitch(self.conn, pid)
        return {
            "has_research": bool(db.read_note(self.conn, pid, "research")),
            "needs_poc": bool(row["needs_poc"]),
            "has_poc": bool(db.read_note(self.conn, pid, "poc")),
            # chosen here, once, so a retried poc_create finds the computer it already made
            "cell_name": f"room-poc-{pid}-{int(time.time())}",
        }

    def research(self, pid):
        if db.read_note(self.conn, pid, "research"):
            return True
        row = db.pitch(self.conn, pid)
        db.update_pitch(self.conn, pid, status="researching")
        brief = self._role(
            pid,
            "researcher",
            f"Research pitch #{pid} for {row['author']}."
            " Read the pitch and assignment notes first.",
            "research",
        )
        if brief is None:
            raise Stop("failed", "the researcher did not submit a brief")
        return True

    def poc_create(self, pid, name):
        db.update_pitch(self.conn, pid, status="prototyping")
        if db.read_note(self.conn, pid, "cell") == name and self.cells.exists(name):
            self.say(f"[poc] reusing Nevia computer {name}")
            return name
        self.say(f"[poc] creating Nevia computer {name}")
        try:
            self.cells.create(name, self.cfg["poc"]["image"])
        except self.cells.CellError as e:
            raise Stop("failed", f"could not start the POC computer: {e}") from e
        db.write_note(self.conn, pid, "cell", name)
        return name

    def poc_build(self, pid, name):
        if db.read_note(self.conn, pid, "poc"):
            return True
        report = self._role(
            pid,
            "poc",
            f"Build the proof of concept for pitch #{pid}. Read the assignment's poc_goal first.",
            "poc",
        )
        if report is None:
            db.write_note(
                self.conn,
                pid,
                "poc",
                {
                    "works": False,
                    "summary": "The POC builder stopped without a report.",
                    "how_to_run": [],
                    "evidence": [],
                    "files": [],
                    "limitations": ["no report"],
                },
            )
        elif json.loads(report)["works"]:
            files = self.cells.copy_out(name, self.cfg.job_dir(pid) / "poc")
            self.say(f"[poc] copied {len(files)} file(s) out")
        return True

    def poc_remove(self, pid, name):
        if self.cfg["poc"]["keep_computer"]:
            self.say(f"[poc] keeping {name} ([poc] keep_computer)")
            return True
        try:
            self.cells.remove(name)
        except self.cells.CellError as e:
            if self.durable:
                raise  # the activity retries until the computer is gone
            self.say(f"[poc] could not delete {name}: {e}. Delete it with `nevia rm {name} --yes`")
            return False
        self.say(f"[poc] deleted {name}")
        return True

    def write_once(self, pid, attempt):
        row = db.pitch(self.conn, pid)
        db.update_pitch(self.conn, pid, status="writing")
        ask = (
            f"Write the post for pitch #{pid}. Read the pitch, assignment, research, poc and"
            " review notes first."
        )
        if attempt:
            ask += (
                " Your last run ended without a draft that passed the gates; read the gates"
                " note and fix every failure."
            )
        draft = self._role(
            pid, "writer", ask, "draft", extra_system=persona(self.cfg, row["author"])
        )
        return bool(draft)

    def gates_exhausted(self, pid):
        g = db.read_json(self.conn, pid, "gates") or {}
        n = self.cfg["limits"]["max_write_rounds"] + 1
        raise Stop("escalated", f"no draft passed the gates after {n} attempts: {g.get('errors')}")

    def review(self, pid):
        db.update_pitch(self.conn, pid, status="reviewing")
        review = self._role(
            pid,
            "reviewer",
            f"Review the draft for pitch #{pid} against the research brief.",
            "review",
        )
        if review is None:
            raise Stop("failed", "the reviewer did not submit a verdict")
        review = json.loads(review)
        if review["verdict"] != "approve":
            self.say(f"[reviewer] changes requested ({len(review['reasons'])} reasons)")
        return {"verdict": review["verdict"], "reasons": review["reasons"]}

    def publish(self, pid):
        row = db.pitch(self.conn, pid)
        markdown = db.read_note(self.conn, pid, "draft")
        meta, _ = gates.split_front_matter(markdown)
        # A crash after the commit and before the status update left a branch behind: reuse it.
        if done := publish.existing_branch(self.cfg, pid, meta["slug"]):
            self.say(f"[room] already committed on {done}")
            return done
        poc = db.read_json(self.conn, pid, "poc")
        poc_dir = self.cfg.job_dir(pid) / "poc" if poc else None
        try:
            final, meta = publish.finalize(
                markdown,
                author=row["author"],
                model=self.cfg.model("writer"),
                pitch=db.read_json(self.conn, pid, "pitch") or {},
                pitch_id=pid,
                poc_path=f"{self.cfg['blog']['pocs_dir']}/{meta['slug']}" if poc_dir else None,
                today=date.today(),
            )
            branch, path = self.publisher(
                self.cfg, markdown=final, meta=meta, poc_dir=poc_dir, pitch_id=pid, say=self.say
            )
        except publish.PublishError as e:
            raise Stop("failed", str(e)) from e
        self.say(f"[room] committed {path} on branch {branch} in {self.cfg.blog}")
        return branch

    def finish(self, pid, status, reason, branch):
        fields = {"status": status, "reason": reason}
        if branch:
            fields["branch"] = branch
        db.update_pitch(self.conn, pid, **fields)
        self.say(f"[room] pitch #{pid} {status}" + (f": {reason}" if reason else f" on {branch}"))
        return True
