# ai-writers-room

A writers room for the [Aiven Bot Blog](../aiven-bot-blog). The blog's six AI author personas
(`content/authors.json`) get a scout, an editor, a researcher, a proof-of-concept builder and a
fact-checker. Together they find what developers are talking about, or take your prompt, research
it, build a small demo on [Nevia](https://nevia.cloud) when that's what the post needs, write the
post in the assigned persona's voice, and commit it to its own branch of the blog. A person merges.

The design comes from [the factory](../factory): deterministic code decides what runs, what it may
spend and what reaches the blog. The models only do the research and the writing.

## How it works

```
room scout                                    room pitch "..."
  signals (code: Aiven changelog, Planet       your prompt, verbatim
    PostgreSQL/Kafka/Valkey, HN, GitHub)
  -> scout pitches (evidence must be real)      -> editor assigns author, outline, POC?
  -> editor per pitch                           -> approved
  -> proposed: waits for `room approve`
                         \                     /
                          room write <id>
  1. researcher  brief of claims. Each claim quotes a page it fetched, and the tool checks the quote
  2. poc         only if assigned: a fresh Nevia computer, deleted afterwards
  3. writer      persona voice; gates run inside submit_draft, so failures go straight back to it
  4. reviewer    a different model, never sees the writer's reasoning; approve or send back
  5. publish     a worktree on branch room/<slug>, real `mise run build`, one commit, no push
```

Each `room write` is a **Temporal workflow** (`room-write-<pitch>`), and each step above is one
activity. The control flow lives in `src/room/flow.py`, and the same function also runs
in-process when Temporal is off.
- **A crash resumes at the step that was running.** Finished steps aren't run or paid for again.
  Every stage also checks its own notes and branches first.
- **The POC computer's delete sits in the workflow's `finally`.** It runs on the next worker even if
  the process that created the computer died, and it retries until Nevia confirms the computer is
  gone.
- **A retried role first stops the pi process the dead attempt left running** (`agent.reap`).
- **A second `room write` for the same pitch joins the running workflow** instead of starting
  another.

| Role | Model (room.toml) | Can do |
| --- | --- | --- |
| scout | Haiku | read signals, search, fetch, pitch |
| editor | Sonnet | read the pitch and existing posts, assign |
| researcher | Sonnet | search, fetch, Aiven docs index, submit a brief, escalate |
| poc | Sonnet | run commands, write and read files **on the Nevia computer only** |
| writer | Sonnet | read notes and posts, fetch, submit a draft, escalate |
| reviewer | Opus, high thinking | read everything, fetch, approve or request changes |

## Lessons carried over from the factory

- **Code holds the line.** Each role has one job and submits one structured result through a
  tool. The tool validates it and fails the call with what to fix, so the model corrects itself
  in the same run. Statuses, retries, budgets and the "a person merges" rule live in Python.
- **Untrusted input is data.** Every page, search result and signal reaches the model wrapped as
  untrusted third-party text. Every role prompt says so.
- **No shell on your machine.** pi runs with `--no-builtin-tools --no-extensions --no-skills
  --no-context-files` and its own agent dir (`.room/pi`), so your `~/.pi` setup never loads. A
  role sees only its own tools (`src/room/tools.py`). A prompt injection in a web page has nothing
  to run.
- **Claims trace to sources, and code checks the trace.** A research claim's quote must appear on
  the page as it was fetched. A draft may link only to pages that were fetched, the brief's
  sources, the pitch's evidence, or Aiven docs. A POC report saying `works: true` must quote
  commands that really ran and exited 0. Search hits count as "seen", not "read", so they can back
  a pitch but never a claim.
- **The writer and the reviewer are separate.** Like the factory's implementer and reviewer, the
  reviewer reads only the notes and the draft.
- **Escalating is a good outcome.** Researcher, POC builder and writer can `escalate` with next
  steps. A person's pitch is never refused by the editor, only flagged.
- **Meter everything.** Usage comes from pi's own events, is priced from `[prices]` and checked
  against `[budget]` before each role and while it runs. Every run's raw event stream is kept in
  `.room/jobs/<id>/logs/` (the factory once lost the output of a broken run).
- **The Nevia bugs, designed out** (`known-bugs.md` in the factory):
  - The agent runs here and drives the computer one short `nevia exec` at a time. The model key
    never enters the computer, and no single stream has to survive a whole run.
  - Computers are created with `--wait`, and the room waits for DNS before the first command.
  - Output and files cross as base64.
  - Each command's output goes to a file in the computer, so a server started with `&` can't hold
    the exec stream open.
  - There's no branching of sleeping computers.
  - The computer is deleted in a `finally`, even when the run fails.

## Running it

```bash
mise run up        # builds the image, starts Temporal, the web page and a worker
                   # web  http://127.0.0.1:8250   Temporal UI  http://127.0.0.1:8243
mise run logs      # SERVICE=web|worker|temporal
mise run down      # state stays in the volumes
```

`compose.yaml` runs three containers from one image (`Dockerfile`: Python and uv, Node and pi
1.4.0, nevia 0.1.14, git):

| Service | Does |
| --- | --- |
| `web` | the page, and the writes and chats started from it |
| `worker` | a Temporal worker that resumes any write that was interrupted |
| `temporal` | Temporal's dev server, its SQLite state in a volume, on 7243/8243 so the factory's 7233 is untouched |

- **Secrets.** `up` runs `docker compose` under `fnox exec`, so the gateway key is passed through
  the environment and never written to a file. The Nevia key file (found in
  `~/.config/nevia/keys`, or set `NEVIA_KEY_PATH`) is mounted as a compose secret. Without one,
  everything works except proof-of-concept builds.
- **The blog.** `../aiven-bot-blog` (or `ROOM_BLOG_DIR`) is mounted at `/blog`, so drafts are still
  committed to `room/<slug>` branches in your checkout. The container builds the blog with its
  own Linux virtualenv (the `blog-venv` volume), because your Mac's `.venv` can't run there.
- **State.** The room's database and logs live in the `room-state` volume. The CLI tasks
  (`mise run pitch`, `scout`, `pitches`, `approve`, `write`, `spend`, or any command with
  `mise run room -- show 3`) run inside the `web` container, against that same state.
- **Without containers**, `mise run dev:temporal`, `dev:web` and `dev:worker` run the same room on
  this machine, with its state in `.room/`.

## The web page

`mise run up` serves the room at **http://127.0.0.1:8250**.

- **Pitches.** A board in plain words: *Needs you*, *Ideas*, *Ready to write*, *In the room*,
  *Drafted*. Pitch a post, ask the scout for this week's ideas, approve or pass on pitches, and
  start or resume a write.
- **A pitch's page.** Its story so far: what you asked for, the editor's plan, the research and
  each claim's quote and source, the demo, the fact-checker's notes, and the draft rendered as an
  article. While the room works, a live log shows each step.
- **Writers.** The six personas, each with a *Talk to* button. A chat runs that persona on its own
  model (`[models] chat`) with read-only tools, so it can look things up, read the blog and the
  pitch's notes, and put a pitch on the board when you agree on one. It can't change drafts or
  approve anything. Each conversation has its own budget (`[budget] chat_usd`), separate from any
  pitch's write budget.
- **Pin as direction.** Pin one of your own messages in a chat about a pitch, and it becomes that
  pitch's `direction` note. The researcher, demo builder, writer and fact-checker read it on the
  next write and treat it as trusted, unlike anything they read on the web.

The page listens only on 127.0.0.1 and refuses requests whose Host isn't this machine, and
cross-site POSTs, because a POST here spends model money. Drafts and replies render through
Markdown with raw HTML escaped.

## Use

```bash
mise install && mise run install    # includes the Temporal CLI
mise run test && mise run lint      # durable tests run against a real Temporal dev server

mise run up                         # everything, in containers (see Running it)

# Your own prompt (pre-approved). Optional: AUTHOR=slug, POC=1
mise run pitch PROMPT="Semantic caching for Claude Code sessions with Aiven for Valkey"
mise run write ID=1

# Let the scout find topics. Pitches wait for you.
mise run scout
mise run pitches            # or: uv run room pitches -v
uv run room show 3          # the pitch, assignment, brief, review...
mise run approve ID=3
mise run write ID=3

mise run spend
```

`room write` ends with a commit on `room/<slug>` in the blog repo. Look at it with
`git -C ../aiven-bot-blog show room/<slug>`, then merge or delete the branch. Nothing is pushed.
`room write` hosts a Temporal worker while it waits. If you Ctrl-C it, the workflow pauses, and the
next `room write <id>` (or a long-running `mise run worker`) carries on from the same step.
Temporal's state is in `.room/temporal.db`, and the factory's server on 7233 is untouched.
`ROOM_TEMPORAL=off` writes in-process instead. A stopped pitch (`escalated`, `failed`) keeps its
research and POC, so writing it again doesn't pay for them twice.

Secrets: `AIVEN_AI_GATEWAY_KEY` comes from fnox. `fnox.toml` is gitignored (as in the factory), so
on a fresh clone run `fnox set AIVEN_AI_GATEWAY_KEY` once.
Every mise task that calls a model runs under `fnox exec`.

## What's verified and what isn't

Verified on 2026-11-09:
- 46 tests pass (7 of them for the web page, with the model faked), 5 of them against a real Temporal dev server: a durable write reaches a branch; a
  publish that fails once is retried without re-running research or writing; a POC whose build
  fails for good still has its computer deleted; a finished pitch can't be written again; and
  Temporal being down gives a clear message. The rest cover: gates, tool rules, budget kills, the full pipeline with a scripted agent, publishing
  to a temp git repo, and the Nevia cell scripts against a fake `nevia`.
- The four configured model IDs exist on Aiven AI Gateway.
- pi loads the extension and sends exactly the role's tools and system prompt (captured at a local
  fake gateway).
- The real blog build (render-engine) accepts a room-made post, checked in a throwaway clone.

**Not run yet:** a live run against real models (it costs money), and a POC on a real Nevia
computer. Model prices in `room.toml` are `0.0` until you fill them in, so until then only the
token cap (`pitch_tokens`) can stop a run.

## Not built (yet)

- Scouting isn't durable (`room scout` runs in-process). It's cheap, and a rerun just pitches again.
- A workflow that is *terminated* (as opposed to cancelled) skips its `finally`, so it won't delete
  a POC computer. Check `nevia ls` after terminating one.

- Creating real Aiven services for a POC. `[poc] allow_aiven_services = false` and the POC role
  is told to use local stand-ins. If a post needs real services, set them up yourself, or ask to
  have the OpenTofu for them written, and hand the connection details to the POC as Nevia secrets.
- A metering proxy like the factory's gateway. The budget here comes from pi's reported usage, not
  from the gateway, so a misbehaving pi could under-report.
- Opening pull requests and scheduling `room scout`. Both are outward-facing, so they're left to you.
