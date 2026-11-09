"""Run one role as one pi process, and meter it.

pi gets no built-in tools (no shell, no file access), no discovered extensions, skills or context
files: only the room's own tools for that role. Its model provider is Aiven AI Gateway, with the
key read from the environment at request time. Every event is kept in a log file, and the run is
stopped if it goes over its turns, its wall clock, or the pitch's budget.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from . import db
from .config import PKG, Config

EXT = PKG / "extension" / "room-tools.ts"
ROLES = PKG / "roles"
PROVIDER = "aiven"


@dataclass
class RunResult:
    outcome: str  # "ok" | "exit N" | "over budget" | "timed out" | "too many turns"
    usage: dict = field(default_factory=dict)
    usd: float = 0.0
    text: str = ""  # the agent's last words, for the log line when it fails
    reply: str = ""  # the full text of its last message with text (a chat answer)
    tool_calls: list = field(default_factory=list)


def usd_for(usage, price):
    m = 1_000_000
    return (
        usage.get("input", 0) * price.get("input", 0)
        + usage.get("output", 0) * price.get("output", 0)
        + usage.get("cache_read", 0) * price.get("cache_read", price.get("input", 0))
        + usage.get("cache_write", 0) * price.get("cache_write", price.get("input", 0))
    ) / m


def add_usage(total, u):
    """pi's assistant message usage -> running totals. Each message_end is one model response."""
    if not isinstance(u, dict):
        return total
    for ours, theirs in (
        ("input", "input"),
        ("output", "output"),
        ("cache_read", "cacheRead"),
        ("cache_write", "cacheWrite"),
    ):
        total[ours] = total.get(ours, 0) + (u.get(theirs) or 0)
    return total


def write_pi_config(cfg: Config) -> str:
    """A pi agent dir of the room's own, so the user's ~/.pi settings and extensions never load."""
    d = cfg.state_dir / "pi"
    d.mkdir(exist_ok=True)
    g = cfg["gateway"]
    models = sorted(set(cfg["models"].values()))
    providers = {
        PROVIDER: {
            "baseUrl": g["base_url"],
            "api": g["api"],
            "apiKey": f"${g['key_env']}",
            "authHeader": True,
            "models": [
                {
                    "id": m,
                    "name": m,
                    "reasoning": True,
                    "input": ["text"],
                    "cost": {
                        k: cfg.price(m).get(k, 0)
                        for k in ("input", "output", "cacheRead", "cacheWrite")
                    },
                    "contextWindow": 200_000,
                    "maxTokens": 32_000,
                }
                for m in models
            ],
        }
    }
    (d / "models.json").write_text(json.dumps({"providers": providers}, indent=2))
    (d / "settings.json").write_text(json.dumps({"quietStartup": True}))
    return str(d)


def argv(cfg, role, prompt, session_dir, extra_system="", session_id=None):
    system = (ROLES / f"{role}.md").read_text()
    if extra_system:
        system += "\n\n" + extra_system
    thinking = cfg.get("thinking", {}).get(role)
    return [
        "pi",
        "--mode",
        "json",
        "-p",
        "--no-builtin-tools",
        "--no-extensions",
        "--no-skills",
        "--no-context-files",
        "--no-prompt-templates",
        "--no-themes",
        "--no-mcp",
        "--offline",
        "-e",
        str(EXT),
        "--model",
        f"{PROVIDER}/{cfg.model(role)}",
        *(["--thinking", thinking] if thinking else []),
        "--system-prompt",
        system,
        "--session-dir",
        str(session_dir),
        *(["--session-id", session_id] if session_id else []),
        prompt,
    ]


def progress(event) -> str | None:
    t = event.get("type")
    if t == "tool_execution_start":
        args = json.dumps(event.get("args", {}))
        return f"  → {event.get('toolName')} {args[:140]}"
    if t == "tool_execution_end" and event.get("isError"):
        content = (event.get("result") or {}).get("content") or [{}]
        return f"  ✗ {event.get('toolName')}: {str(content[0].get('text', ''))[:300]}"
    return None


def run(
    cfg: Config,
    conn,
    pitch_id: int,
    role: str,
    prompt: str,
    *,
    extra_system="",
    say=print,
    popen=subprocess.Popen,
    chat=None,
    on_event=None,
) -> RunResult:
    """`chat` = {"id", "session_id", "cap_usd"} runs a conversation turn: it continues that pi
    session, is metered against the conversation's own budget (never the pitch's write budget)
    and is never reaped by a write. `on_event` gets every pi event, for streaming to a page."""
    if not os.environ.get(cfg["gateway"]["key_env"]):
        raise SystemExit(f"{cfg['gateway']['key_env']} is not set. Run this under `fnox exec --`.")
    job = cfg.job_dir(pitch_id)
    (job / "logs").mkdir(exist_ok=True)
    model = cfg.model(role)
    log_path = job / "logs" / f"{role}-{int(time.time())}.jsonl"
    budget = cfg["budget"]
    if chat:
        run_id = db.start_run(conn, pitch_id, role, model, log_path, chat_id=chat["id"])
        cap_usd = chat["cap_usd"]
        prior_usd, prior_tokens = db.chat_spent(conn, chat["id"])
    else:
        reap(conn, pitch_id, say)
        run_id = db.start_run(conn, pitch_id, role, model, log_path)
        cap_usd = budget["scout_usd"] if pitch_id == 0 else budget["pitch_usd"]
        prior_usd, prior_tokens = db.spent(conn, pitch_id)
    lim = cfg["limits"]

    env = {
        **os.environ,
        "PI_CODING_AGENT_DIR": write_pi_config(cfg),
        "ROOM_BIN": json.dumps([sys.executable, "-m", "room"]),
        "ROOM_ROLE": role,
        "ROOM_PITCH": str(pitch_id),
        "ROOM_CHAT": str(chat["id"]) if chat else "",
        "ROOM_RUN": str(run_id),
        "ROOM_CONFIG": str(cfg.path or cfg.root / "room.toml"),
        "ROOM_STATE": str(cfg.state_dir),
        "PYTHONPATH": str(PKG.parent) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    proc = popen(
        argv(
            cfg,
            role,
            prompt,
            job / "sessions",
            extra_system,
            session_id=chat["session_id"] if chat else None,
        ),
        cwd=job,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    db.set_run_pid(conn, run_id, proc.pid)
    # stderr is drained on its own thread: a full pipe would stall pi (pi's json.md says the same
    # of stdout, which the loop below reads continuously).
    errors = []
    threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
    wall_min = lim.get("wall_clock_min", {}).get(role, lim["role_wall_clock_min"])
    max_turns = lim.get("max_turns", {}).get(role, lim["role_max_turns"])
    deadline = time.monotonic() + wall_min * 60
    res = RunResult("ok")
    turns = 0
    stop = model_error = None
    timer = threading.Timer(wall_min * 60, lambda: _kill(proc))
    timer.start()
    try:
        with log_path.open("wb") as log:
            for raw in proc.stdout:
                log.write(raw)
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if on_event:
                    on_event(ev)
                if line := progress(ev):
                    say(line)
                t = ev.get("type")
                if t == "tool_execution_start":
                    res.tool_calls.append(ev.get("toolName"))
                if t == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
                    msg = ev["message"]
                    add_usage(res.usage, msg.get("usage"))
                    texts = [
                        c.get("text", "")
                        for c in msg.get("content") or []
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    if any(texts):
                        res.reply = "\n".join(texts)
                        res.text = res.reply[-1500:]
                    if msg.get("stopReason") == "error":
                        # pi exits 0 after a provider error, so this is the only sign of one
                        model_error = msg.get("errorMessage") or "model request failed"
                        res.text = model_error
                    else:
                        model_error = None
                if t == "turn_end":
                    turns += 1
                if t == "message_end":
                    db.save_usage(conn, run_id, res.usage, usd_for(res.usage, cfg.price(model)))
                res.usd = usd_for(res.usage, cfg.price(model))
                tokens = sum(res.usage.values())
                if prior_usd + res.usd > cap_usd or prior_tokens + tokens > budget["pitch_tokens"]:
                    stop = "over budget"
                elif turns > max_turns:
                    stop = "too many turns"
                elif time.monotonic() > deadline:
                    stop = "timed out"
                if stop:
                    _kill(proc)
                    break
        rc = proc.wait()
    finally:
        timer.cancel()
    if stop:
        res.outcome = stop
    elif time.monotonic() > deadline:
        res.outcome = "timed out"
    elif model_error:
        res.outcome = f"model error: {model_error[:200]}"
    elif rc != 0:
        res.outcome = f"exit {rc}"
        tail = b"".join(errors[-20:]).decode(errors="replace").strip()
        res.text = res.text or tail[-1500:]
    db.end_run(conn, run_id, res.outcome, res.usage, res.usd)
    return res


def reap(conn, pitch_id, say=print):
    """End what an interrupted attempt left behind. When a worker dies, Temporal runs the step
    again, but the pi it started is in its own process group and keeps going (and spending), so
    it is stopped here before a new one starts."""
    for r in db.open_runs(conn, pitch_id):
        if r["pid"] and _is_pi(r["pid"]):
            say(f"  stopping {r['role']}'s earlier pi (pid {r['pid']}) from an interrupted attempt")
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(r["pid"], signal.SIGTERM)
        db.close_run(conn, r["id"], "abandoned")  # keeps the usage it saved while running


def _is_pi(pid) -> bool:
    """Only signal a pid that is still one of the room's pi processes; the number may have been
    reused. The extension path is on every room pi's command line and on nothing else's."""
    try:
        cmd = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return EXT.name in cmd and "--no-builtin-tools" in cmd


def _kill(proc):
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
