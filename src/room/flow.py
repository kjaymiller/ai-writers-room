"""The running order of `room write`, as control flow over named steps and nothing else.

The same function drives a write in two ways. In-process, each step calls a stage in
`pipeline.Stages` directly. Under Temporal, `durable.WriteWorkflow` runs it as the workflow and each
step is an activity, so a crash resumes at the step that was running and the workflow's `finally`
deletes the POC computer even if the process that created it died. That is why this module may
import nothing and do nothing but await steps: Temporal replays it, so it must be deterministic.
"""

STOP = "__stop__"


class Stop(Exception):
    """End the pitch here with a status and a reason."""

    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


async def run(step, pid: int, limits: dict) -> dict:
    """-> {"status": "drafted", "branch": ...} or {"status": <stop status>, "reason": ...}.

    `step(name, *args)` runs one stage and returns what it returned. A stage that stops the pitch
    returns {STOP: [status, reason]} instead of raising, so the stop crosses an activity boundary
    as a value, not as a failure Temporal would retry."""

    async def s(name, *args):
        out = await step(name, *args)
        if isinstance(out, dict) and STOP in out:
            raise Stop(*out[STOP])
        return out

    try:
        plan = await s("prepare", pid)
        if not plan["has_research"]:
            await s("research", pid)
        if plan["needs_poc"] and not plan["has_poc"]:
            name = await s("poc_create", pid, plan["cell_name"])
            try:
                await s("poc_build", pid, name)
            finally:
                await step("poc_remove", pid, name)  # never skipped, even on a stop

        review_rounds = 0
        while True:
            for attempt in range(limits["max_write_rounds"] + 1):
                if await s("write_once", pid, attempt):
                    break
            else:
                await s("gates_exhausted", pid)  # stops the pitch
            review = await s("review", pid)
            if review["verdict"] == "approve":
                break
            review_rounds += 1
            if review_rounds > limits["max_review_rounds"]:
                raise Stop(
                    "escalated",
                    f"the reviewer still requests changes after {review_rounds} rounds:"
                    f" {review['reasons']}",
                )
        branch = await s("publish", pid)
    except Stop as e:
        await step("finish", pid, e.status, e.reason, None)
        return {"status": e.status, "reason": e.reason}
    except Exception as e:  # a step that failed for good (under Temporal: after its retries)
        cause = getattr(e, "cause", None) or e
        reason = f"{type(cause).__name__}: {cause}"[:500]
        await step("finish", pid, "failed", reason, None)
        return {"status": "failed", "reason": reason}
    await step("finish", pid, "drafted", "", branch)
    return {"status": "drafted", "branch": branch}
