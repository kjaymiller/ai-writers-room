"""Durable writes against a real Temporal dev server (the `temporal` CLI from mise.toml)."""

import asyncio
import shutil
import subprocess

import pytest

from room import db, durable, pipeline

from .test_pipeline import FakeCells, Script

TEMPORAL = (
    shutil.which("temporal")
    or subprocess.run(["mise", "which", "temporal"], capture_output=True, text=True).stdout.strip()
)
pytestmark = pytest.mark.skipif(not TEMPORAL, reason="temporal CLI not installed (mise install)")


@pytest.fixture(scope="module")
def server():
    from temporalio.testing import WorkflowEnvironment

    loop = asyncio.new_event_loop()
    env = loop.run_until_complete(
        WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL, ui=False)
    )
    yield env
    loop.run_until_complete(env.shutdown())
    loop.close()


@pytest.fixture
def dcfg(cfg, server, monkeypatch):
    monkeypatch.delenv("ROOM_TEMPORAL")
    cfg.raw["temporal"].update(retry_seconds=1, task_queue=f"test-{id(cfg)}")
    monkeypatch.setenv("ROOM_TEMPORAL_ADDRESS", server.client.service_client.config.target_host)
    return cfg


def write(cfg, conn, pid, **stages_kw):
    lines = []
    branch = durable.write(cfg, conn, pid, say=lines.append, stages_kw=stages_kw)
    return branch, lines


def test_a_durable_write_reaches_a_branch(dcfg, conn, monkeypatch):
    s = Script(dcfg, monkeypatch)
    pid = pipeline.new_pitch(dcfg, conn, "pgvector", runner=s, say=lambda _: 0)
    branch, lines = write(dcfg, conn, pid, runner=s)
    assert branch == "room/pgvector-on-aiven"
    assert db.pitch(conn, pid)["status"] == "drafted"
    assert any("room-write-" in ln for ln in lines)


def test_a_failed_step_is_retried_and_finished_steps_are_not(dcfg, conn, monkeypatch):
    s = Script(dcfg, monkeypatch)
    pid = pipeline.new_pitch(dcfg, conn, "pgvector", runner=s, say=lambda _: 0)
    calls = []
    real = pipeline.publish.publish

    def flaky(*a, **k):  # the machine fell over during the first publish
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("disk went away")
        return real(*a, **k)

    branch, lines = write(dcfg, conn, pid, runner=s, publisher=flaky)
    assert branch and len(calls) == 2
    assert s.calls.count("researcher") == 1 and s.calls.count("writer") == 1
    assert any("publish attempt 2" in ln for ln in lines)


def test_the_poc_computer_is_deleted_when_the_build_fails_for_good(dcfg, conn, monkeypatch):
    s = Script(dcfg, monkeypatch)
    pid = pipeline.new_pitch(dcfg, conn, "pgvector", poc=True, runner=s, say=lambda _: 0)

    def runner(cfg, conn, pid, role, prompt, **kw):
        if role == "poc":
            raise RuntimeError("the worker died mid-POC")
        return s(cfg, conn, pid, role, prompt, **kw)

    cells = FakeCells()
    branch, _ = write(dcfg, conn, pid, runner=runner, cells=cells)
    assert branch is None
    row = db.pitch(conn, pid)
    assert row["status"] == "failed" and "worker died" in row["reason"]
    assert cells.created and cells.removed == cells.created  # one computer, and it is gone


def test_a_finished_pitch_cannot_be_written_again(dcfg, conn, monkeypatch):
    s = Script(dcfg, monkeypatch)
    pid = pipeline.new_pitch(dcfg, conn, "pgvector", runner=s, say=lambda _: 0)
    write(dcfg, conn, pid, runner=s)
    with pytest.raises(SystemExit, match="drafted"):
        write(dcfg, conn, pid, runner=s)


def test_settings_and_a_clear_message_when_temporal_is_down(cfg, monkeypatch):
    assert durable.settings(cfg) is None  # ROOM_TEMPORAL=off from conftest
    monkeypatch.delenv("ROOM_TEMPORAL")
    assert durable.settings(cfg)["address"] == "127.0.0.1:7243"
    with pytest.raises(SystemExit, match="mise run temporal"):
        asyncio.run(durable.connect({"address": "127.0.0.1:1", "namespace": "default"}))
