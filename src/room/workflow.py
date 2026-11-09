"""The Temporal workflow for one write. It lives on its own because Temporal re-imports a
workflow's module in a sandbox and replays it: this module imports only `flow`, which imports
nothing, so nothing here can touch the clock, the disk or the network. See durable.py."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from . import flow

ACTIVITY = "room-stage"
HEARTBEAT_SECONDS = 15
# Steps that run a model role, or wait on a computer, can take a long time; the rest are quick.
LONG = {"research", "poc_build", "write_once", "review", "poc_create", "publish"}


@workflow.defn(name="room-write")
class WriteWorkflow:
    @workflow.run
    async def run(self, args: dict) -> dict:
        role_minutes = args["role_minutes"]
        retry_s = args.get("retry_seconds", 30)

        async def step(name, *stage_args):
            long = name in LONG
            return await workflow.execute_activity(
                ACTIVITY,
                {"name": name, "args": list(stage_args)},
                # a role is bounded by its own wall clock in agent.run; this is the outer bound
                start_to_close_timeout=timedelta(minutes=role_minutes + 15 if long else 10),
                heartbeat_timeout=timedelta(seconds=HEARTBEAT_SECONDS * 4),
                retry_policy=RetryPolicy(
                    # poc_remove keeps trying: a computer left running bills until it is deleted
                    maximum_attempts=0 if name == "poc_remove" else 3,
                    initial_interval=timedelta(seconds=retry_s),
                    backoff_coefficient=2.0,
                    maximum_interval=timedelta(minutes=10),
                ),
            )

        return await flow.run(step, args["pitch"], args["limits"])
