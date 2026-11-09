import io
import json
import os
import stat

import pytest

from room import agent, cell, config, db


class FakeProc:
    def __init__(self, events, rc=0):
        self.stdout = io.BytesIO(b"".join(json.dumps(e).encode() + b"\n" for e in events))
        self.stderr = io.BytesIO(b"")
        self.rc, self.pid = rc, 999999

    def wait(self):
        return self.rc


def assistant(inp, out, text="done"):
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {"input": inp, "output": out, "cacheRead": 0, "cacheWrite": 0},
        },
    }


def test_runner_meters_usage_logs_events_and_isolates_pi(cfg, conn, monkeypatch):
    monkeypatch.setenv("AIVEN_AI_GATEWAY_KEY", "sk-secret-value-1234")
    seen = {}

    def popen(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return FakeProc(
            [
                {"type": "session"},
                assistant(100_000, 0),
                {"type": "tool_execution_start", "toolName": "fetch_url", "args": {}},
                {"type": "turn_end"},
                assistant(0, 10_000),
            ]
        )

    res = agent.run(cfg, conn, 0, "scout", "go", say=lambda _: 0, popen=popen)
    assert res.outcome == "ok" and res.tool_calls == ["fetch_url"]
    assert res.usage == {"input": 100_000, "output": 10_000, "cache_read": 0, "cache_write": 0}
    assert res.usd == pytest.approx(3.0 * 0.1 + 15.0 * 0.01)  # scout uses haiku, priced in conftest
    for flag in ("--no-builtin-tools", "--no-extensions", "--no-context-files", "--no-skills"):
        assert flag in seen["argv"]
    assert seen["argv"][seen["argv"].index("--model") + 1] == "aiven/claude-haiku-5-5"
    pi_dir = seen["env"]["PI_CODING_AGENT_DIR"]
    with open(os.path.join(pi_dir, "models.json")) as f:
        models = json.load(f)
    assert models["providers"]["aiven"]["apiKey"] == "$AIVEN_AI_GATEWAY_KEY"  # never the value
    assert "sk-secret-value-1234" not in json.dumps(models)
    assert db.spent(conn, 0)[0] == pytest.approx(res.usd)


def test_runner_stops_a_role_that_goes_over_budget(cfg, conn, monkeypatch):
    monkeypatch.setenv("AIVEN_AI_GATEWAY_KEY", "k")
    killed = []
    monkeypatch.setattr(agent, "_kill", lambda p: killed.append(p))
    events = [assistant(1_000_000, 0)] * 5  # $3 each against a $1 scout budget
    res = agent.run(
        cfg, conn, 0, "scout", "go", say=lambda _: 0, popen=lambda *a, **k: FakeProc(events)
    )
    assert res.outcome == "over budget" and killed


def test_runner_refuses_to_start_without_the_key(cfg, conn, monkeypatch):
    monkeypatch.delenv("AIVEN_AI_GATEWAY_KEY", raising=False)
    with pytest.raises(SystemExit, match="fnox"):
        agent.run(cfg, conn, 0, "scout", "go", popen=None)


@pytest.fixture
def fake_nevia(tmp_path, monkeypatch):
    """A `nevia` that runs `exec` commands locally with HOME in a temp dir."""
    home = tmp_path / "home"
    home.mkdir()
    script = tmp_path / "nevia"
    script.write_text(f"""#!/usr/bin/env bash
echo "$@" >> {tmp_path}/calls
case "$1" in
  new|rm) exit 0;;
  exec) shift; shift; while [ "$1" != "--" ]; do shift; done; shift
        HOME={home} exec "$@";;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    shim = tmp_path / "bin"  # macOS has no getent; the computer (Debian) does
    shim.mkdir()
    (shim / "getent").write_text("#!/bin/sh\nexit 0\n")
    (shim / "getent").chmod(0o755)
    # coreutils `timeout --kill-after=10 N cmd...` -> just run cmd
    (shim / "timeout").write_text('#!/bin/sh\nshift; shift; exec "$@"\n')
    (shim / "timeout").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    monkeypatch.setattr(cell, "NEVIA", str(script))
    return home, tmp_path / "calls"


def test_cell_round_trips_files_output_and_exit_codes(fake_nevia, tmp_path):
    _home, calls = fake_nevia
    cell.create("c1")
    assert "new c1 --wait" in calls.read_text()
    cell.write("c1", "app/main.py", "print('héllo ✓')\n")
    assert cell.read("c1", "app/main.py") == "print('héllo ✓')\n"
    rc, out = cell.run("c1", "python3 app/main.py; echo err >&2; exit 3", 30)
    assert rc == 3 and "héllo ✓" in out and "err" in out
    # a background child holding stdout must not keep the call open
    rc, out = cell.run("c1", "sleep 30 & echo started", 30)
    assert rc == 0 and "started" in out
    files = cell.copy_out("c1", tmp_path / "out")
    assert "app/main.py" in files


def test_cell_refuses_paths_outside_poc():
    for bad in ("/etc/passwd", "../x", "a/../../b"):
        with pytest.raises(cell.CellError):
            cell.safe_rel(bad)


def test_a_provider_error_is_not_ok_even_though_pi_exits_0(cfg, conn, monkeypatch):
    monkeypatch.setenv("AIVEN_AI_GATEWAY_KEY", "k")
    err = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [],
            "stopReason": "error",
            "errorMessage": "Connection error.",
        },
    }
    res = agent.run(
        cfg, conn, 1, "editor", "go", say=lambda _: 0, popen=lambda *a, **k: FakeProc([err])
    )
    assert res.outcome == "model error: Connection error."


def test_the_tool_extension_loads_the_same_config_file(cfg, conn, monkeypatch):
    monkeypatch.setenv("AIVEN_AI_GATEWAY_KEY", "k")
    seen = {}

    def popen(argv, **kw):
        seen["env"] = kw["env"]
        return FakeProc([])

    other = cfg.path.with_name("other.toml")  # not room.toml, so a guessed path would be wrong
    other.write_text(cfg.path.read_text())
    cfg2 = config.load(other)
    agent.run(cfg2, conn, 1, "editor", "go", say=lambda _: 0, popen=popen)
    assert seen["env"]["ROOM_CONFIG"] == str(other)
