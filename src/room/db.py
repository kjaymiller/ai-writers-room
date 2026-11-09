"""State is one SQLite file: pitches, versioned notes, role runs and spend."""

import json
import sqlite3
import time

SCHEMA = """
create table if not exists pitches (
  id integer primary key,
  created real not null,
  source text not null,              -- 'prompt' (a person typed it) or 'scout'
  title text not null,
  angle text not null default '',
  prompt text not null default '',   -- the person's words, verbatim, when source = 'prompt'
  evidence text not null default '[]',
  status text not null default 'proposed',
  -- proposed -> approved -> researching -> [prototyping] -> writing -> reviewing -> drafted
  -- or: rejected | escalated | failed
  author text,
  needs_poc integer not null default 0,
  slug text,
  branch text,
  reason text not null default ''
);
create table if not exists notes (
  id integer primary key,
  pitch_id integer not null,         -- 0 for room-wide notes (a scout run's signals)
  kind text not null,
  version integer not null,
  body text not null,
  created real not null
);
create index if not exists notes_kind on notes(pitch_id, kind, version);
create table if not exists runs (
  id integer primary key,
  pitch_id integer not null,
  role text not null,
  model text not null,
  started real not null,
  ended real,
  outcome text,
  input_tokens integer not null default 0,
  output_tokens integer not null default 0,
  cache_read integer not null default 0,
  cache_write integer not null default 0,
  usd real not null default 0,
  log_path text,
  pid integer,                       -- pi's process id while it runs
  chat_id integer                    -- set for a conversation turn; those never count as a write
);
create table if not exists tool_calls (
  run_id integer not null,
  name text not null,
  at real not null
);
create table if not exists chats (
  id integer primary key,
  created real not null,
  author text not null,              -- the persona you are talking with
  pitch_id integer not null default 0,  -- the pitch it is about, or 0 for a general chat
  session_id text not null,          -- the pi session the conversation continues
  title text not null default ''
);
create table if not exists messages (
  id integer primary key,
  chat_id integer not null,
  created real not null,
  who text not null,                 -- 'you' or 'writer'
  body text not null,
  pinned integer not null default 0  -- pinned 'you' messages become the pitch's direction note
);
"""

STATUSES = {
    "proposed",
    "approved",
    "researching",
    "prototyping",
    "writing",
    "reviewing",
    "drafted",
    "rejected",
    "escalated",
    "failed",
}


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("pragma table_info(runs)")}
    if "pid" not in cols:  # databases made before durable writes
        conn.execute("alter table runs add column pid integer")
    if "chat_id" not in cols:  # databases made before the web UI
        conn.execute("alter table runs add column chat_id integer")
    return conn


def add_pitch(
    conn,
    *,
    source,
    title,
    angle="",
    prompt="",
    evidence=(),
    author=None,
    needs_poc=False,
    status="proposed",
):
    cur = conn.execute(
        "insert into pitches (created, source, title, angle, prompt, evidence, author, needs_poc,"
        " status) values (?,?,?,?,?,?,?,?,?)",
        (
            time.time(),
            source,
            title,
            angle,
            prompt,
            json.dumps(list(evidence)),
            author,
            int(needs_poc),
            status,
        ),
    )
    conn.commit()
    return cur.lastrowid


def pitch(conn, pitch_id) -> sqlite3.Row:
    row = conn.execute("select * from pitches where id = ?", (pitch_id,)).fetchone()
    if row is None:
        raise SystemExit(f"no pitch {pitch_id}")
    return row


def update_pitch(conn, pitch_id, **fields):
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"unknown status {fields['status']}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"update pitches set {sets} where id = ?", (*fields.values(), pitch_id))
    conn.commit()


def write_note(conn, pitch_id, kind, body) -> int:
    body = body if isinstance(body, str) else json.dumps(body, indent=2)
    (v,) = conn.execute(
        "select coalesce(max(version), 0) + 1 from notes where pitch_id = ? and kind = ?",
        (pitch_id, kind),
    ).fetchone()
    conn.execute(
        "insert into notes (pitch_id, kind, version, body, created) values (?,?,?,?,?)",
        (pitch_id, kind, v, body, time.time()),
    )
    conn.commit()
    return v


def read_note(conn, pitch_id, kind) -> str | None:
    row = conn.execute(
        "select body from notes where pitch_id = ? and kind = ? order by version desc limit 1",
        (pitch_id, kind),
    ).fetchone()
    return row["body"] if row else None


def read_json(conn, pitch_id, kind):
    body = read_note(conn, pitch_id, kind)
    return json.loads(body) if body else None


def start_run(conn, pitch_id, role, model, log_path, chat_id=None) -> int:
    cur = conn.execute(
        "insert into runs (pitch_id, role, model, started, log_path, chat_id) values (?,?,?,?,?,?)",
        (pitch_id, role, model, time.time(), str(log_path), chat_id),
    )
    conn.commit()
    return cur.lastrowid


def set_run_pid(conn, run_id, pid):
    conn.execute("update runs set pid = ? where id = ?", (pid, run_id))
    conn.commit()


def open_runs(conn, pitch_id):
    """Runs of this pitch that started and never ended: their process died, or is still going."""
    return conn.execute(
        "select id, role, pid from runs where pitch_id = ? and ended is null and chat_id is null",
        (pitch_id,),
    ).fetchall()


def count_tool_call(conn, run_id, name) -> int:
    """Record one lookup for this run; return how many it has made, this one included."""
    conn.execute("insert into tool_calls (run_id, name, at) values (?,?,?)",
                 (run_id, name, time.time()))  # fmt: skip
    conn.commit()
    return conn.execute("select count(*) from tool_calls where run_id = ?", (run_id,)).fetchone()[0]


def save_usage(conn, run_id, usage, usd):
    """Usage so far, written as the run goes, so a run killed mid-way still shows what it spent."""
    conn.execute(
        "update runs set input_tokens = ?, output_tokens = ?, cache_read = ?, cache_write = ?,"
        " usd = ? where id = ?",
        (usage.get("input", 0), usage.get("output", 0), usage.get("cache_read", 0),
         usage.get("cache_write", 0), usd, run_id),
    )  # fmt: skip
    conn.commit()


def close_run(conn, run_id, outcome):
    """End a run without touching the usage it recorded."""
    conn.execute("update runs set ended = ?, outcome = ? where id = ?",
                 (time.time(), outcome, run_id))  # fmt: skip
    conn.commit()


def end_run(conn, run_id, outcome, usage, usd):
    conn.execute(
        "update runs set ended = ?, outcome = ?, input_tokens = ?, output_tokens = ?,"
        " cache_read = ?, cache_write = ?, usd = ? where id = ?",
        (
            time.time(),
            outcome,
            usage.get("input", 0),
            usage.get("output", 0),
            usage.get("cache_read", 0),
            usage.get("cache_write", 0),
            usd,
            run_id,
        ),
    )
    conn.commit()


def spent(conn, pitch_id) -> tuple[float, int]:
    usd, tokens = conn.execute(
        "select coalesce(sum(usd), 0), coalesce(sum(input_tokens + output_tokens + cache_read"
        " + cache_write), 0) from runs where pitch_id = ? and chat_id is null",
        (pitch_id,),
    ).fetchone()
    return usd, tokens


def chat_spent(conn, chat_id) -> tuple[float, int]:
    usd, tokens = conn.execute(
        "select coalesce(sum(usd), 0), coalesce(sum(input_tokens + output_tokens + cache_read"
        " + cache_write), 0) from runs where chat_id = ?",
        (chat_id,),
    ).fetchone()
    return usd, tokens


def add_chat(conn, *, author, pitch_id=0, title=""):
    import uuid

    cur = conn.execute(
        "insert into chats (created, author, pitch_id, session_id, title) values (?,?,?,?,?)",
        (time.time(), author, pitch_id, str(uuid.uuid4()), title),
    )
    conn.commit()
    return cur.lastrowid


def add_message(conn, chat_id, who, body) -> int:
    cur = conn.execute(
        "insert into messages (chat_id, created, who, body) values (?,?,?,?)",
        (chat_id, time.time(), who, body),
    )
    conn.commit()
    return cur.lastrowid


def pinned_direction(conn, pitch_id) -> str:
    """Every message you pinned in a chat about this pitch, oldest first, as one note."""
    rows = conn.execute(
        "select m.body, c.author, m.created from messages m join chats c on c.id = m.chat_id"
        " where c.pitch_id = ? and m.pinned = 1 and m.who = 'you' order by m.created",
        (pitch_id,),
    ).fetchall()
    return "\n\n".join(f"- (to {r['author']}) {r['body']}" for r in rows)
