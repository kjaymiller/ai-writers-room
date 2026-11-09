"""Durable writes: `room write` as a Temporal workflow per pitch.

The workflow is `flow.run`, the same control flow the in-process write uses, and each of its steps
(research, poc_create, poc_build, poc_remove, write_once, review, publish, finish) is one activity.
What that buys:

- **A crash resumes at the step that was running, not from the top.** Finished steps are in the
  workflow's history and are not run (or paid for) again. The stages also check their own notes,
  so a step that finished its work but not its activity does nothing the second time.
- **The POC computer is always deleted.** `poc_remove` sits in the workflow's `finally`. If the
  process that created the computer dies, the workflow resumes on the next worker and deletes it.
  The activity retries until Nevia says it is gone.
- **One write per pitch.** The workflow id is `room-write-<pitch>`. A second `room write` while one
  is running joins it and waits for the same result.
- **A dead worker's step is picked up.** Activities heartbeat. When they stop, Temporal runs the
  step again on the next worker, and `agent.reap` stops the earlier attempt's pi first.

There is no separate daemon to run. `room write` hosts a worker while it waits, so killing it
pauses the workflow and the next `room write` (or `room worker`) carries on. Temporal's own state
lives in .room/temporal.db (`mise run temporal`).

Not covered: a workflow that is terminated (not cancelled) by hand skips its `finally`, so a POC
computer it made is not deleted. Terminating is for wedged workflows; check `nevia ls` after.
"""

import asyncio
import os

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.worker import Worker

from . import config, db, pipeline
from .workflow import ACTIVITY, HEARTBEAT_SECONDS, WriteWorkflow


def settings(cfg, env=None):
    """The `[temporal]` settings, or None when durable writes are off."""
    env = os.environ if env is None else env
    t = cfg.get("temporal", {})
    if env.get("ROOM_TEMPORAL", "").lower() in ("0", "off", "false", "no"):
        return None
    if not t.get("enabled", False) and not env.get("ROOM_TEMPORAL_ADDRESS"):
        return None
    return {
        "address": env.get("ROOM_TEMPORAL_ADDRESS") or t.get("address", "127.0.0.1:7243"),
        "namespace": t.get("namespace", "default"),
        "task_queue": t.get("task_queue", "writers-room"),
    }


def workflow_id(pitch_id):
    return f"room-write-{pitch_id}"


def make_activity(cfg, say=print, stages_kw=None):
    """The one activity: run the named stage of `pipeline.Stages` on a thread, heartbeating.
    `stages_kw` lets tests swap the runner, publisher and cells."""

    @activity.defn(name=ACTIVITY)
    async def run_stage(payload: dict):
        name, args = payload["name"], payload["args"]
        info = activity.info()
        if info.attempt > 1:
            say(f"[temporal] {name} attempt {info.attempt}")

        def work():
            conn = db.connect(cfg.db_path)  # sqlite connections stay on the thread that made them
            try:
                stages = pipeline.Stages(cfg, conn, say=say, durable=True, **(stages_kw or {}))
                return stages.call(name, *args)
            finally:
                conn.close()

        async def beat():
            while True:
                activity.heartbeat(name)
                await asyncio.sleep(HEARTBEAT_SECONDS)

        pulse = asyncio.create_task(beat())
        try:
            return await asyncio.to_thread(work)
        finally:
            pulse.cancel()

    return run_stage


async def connect(st):
    try:
        return await Client.connect(st["address"], namespace=st["namespace"])
    except Exception as e:
        raise SystemExit(
            f"Temporal is not reachable at {st['address']} ({e}).\n"
            "Start it with `mise run temporal` (in another terminal), or write without it:"
            " ROOM_TEMPORAL=off room write <id>"
        ) from e


def worker(client, st, cfg, say=print, stages_kw=None):
    return Worker(
        client,
        task_queue=st["task_queue"],
        workflows=[WriteWorkflow],
        activities=[make_activity(cfg, say, stages_kw)],
        max_concurrent_activities=4,
    )


async def _write(cfg, st, pitch_id, say, stages_kw=None, client=None):
    client = client or await connect(st)
    args = {
        "pitch": pitch_id,
        "limits": pipeline.limits(cfg),
        "role_minutes": cfg["limits"]["role_wall_clock_min"],
        "retry_seconds": cfg.get("temporal", {}).get("retry_seconds", 30),
    }
    async with worker(client, st, cfg, say, stages_kw):
        handle = await client.start_workflow(
            WriteWorkflow.run,
            args,
            id=workflow_id(pitch_id),
            task_queue=st["task_queue"],
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        say(f"[temporal] workflow {handle.id} (run {handle.result_run_id})")
        return await handle.result()


def write(cfg: config.Config, conn, pitch_id, say=print, stages_kw=None, client=None):
    """Start the pitch's workflow, or join the one already running, and wait for its result."""
    pipeline.check_writable(conn, pitch_id)
    st = settings(cfg)
    out = asyncio.run(_write(cfg, st, pitch_id, say, stages_kw, client))
    return out.get("branch")


async def _serve(cfg, st, say):
    client = await connect(st)
    async with worker(client, st, cfg, say):
        say(f"[temporal] worker on {st['address']} ({st['task_queue']}); Ctrl-C to stop")
        await asyncio.Event().wait()


def serve(cfg, say=print):
    """A long-running worker: picks up any write whose `room write` was interrupted."""
    st = settings(cfg)
    if st is None:
        raise SystemExit("durable writes are off: set [temporal] enabled = true in room.toml")
    asyncio.run(_serve(cfg, st, say))
