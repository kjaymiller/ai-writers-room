"""A Nevia computer for one proof of concept.

What the factory learned running cells on Nevia, and what this does about each:
- `nevia new` can return before the computer can run a command: create with `--wait`.
- A new or restored computer cannot resolve names for its first ~15-20 s: wait for DNS before
  the agent's first command, so its first `pip install` does not fail for no reason it can see.
- `nevia exec` output is not binary-clean: everything crosses as base64.
- A background child that still holds the exec's stdout breaks the stream ("Error decoding
  response body"): every command's output goes to a file in the computer, and only that file is
  printed, so a server the agent starts with `&` cannot hold the stream open.
- A failed build once left a computer running and billing: the computer is deleted when the POC
  role ends, pass or fail, unless `[poc] keep_computer` says otherwise.
- Branching a sleeping computer returned a stale disk: there is no branching here. Each POC gets
  a fresh computer from the image.

The agent never runs inside the computer. pi runs on this machine and each tool call is one short
`nevia exec`, so the model key never enters it and no single stream has to stay up for the whole
run.
"""

import base64
import io
import re
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath

NEVIA = "nevia"
HOME_POC = "$HOME/poc"
MAX_OUT = 20_000
MAX_ARCHIVE = 20 * 1024 * 1024

_RUN = r"""
mkdir -p "$HOME/poc" && cd "$HOME/poc" || exit 97
timeout --kill-after=10 "$1" bash -c "$2" >"$HOME/.room-out" 2>&1 </dev/null
rc=$?
tail -c 200000 "$HOME/.room-out" | base64 | tr -d "\n"
printf '\n__ROOM_RC__ %s\n' "$rc"
"""
_WRITE = r"""
case "$1" in /*|*..*) echo "path must be relative to ~/poc" >&2; exit 2;; esac
mkdir -p "$HOME/poc/$(dirname "$1")" && base64 -d >"$HOME/poc/$1"
"""
_READ = r"""
case "$1" in /*|*..*) echo "path must be relative to ~/poc" >&2; exit 2;; esac
head -c "$2" "$HOME/poc/$1" | base64 | tr -d "\n"
"""
_PACK = r"""
cd "$HOME/poc" && tar czf - --exclude=.venv --exclude=node_modules --exclude=__pycache__ \
  --exclude=.git . | base64 | tr -d "\n"
"""


class CellError(Exception):
    pass


def _nevia(*args, input=None, timeout=120):
    try:
        p = subprocess.run(
            [NEVIA, *args], input=input, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise CellError(f"nevia {args[0]} timed out after {timeout}s") from e
    return p.returncode, p.stdout, p.stderr


def _exec(name, script, *args, input=None, timeout=120):
    argv = ["exec", name, "--entrypoint", "", "--", "bash", "-c", script, "room", *args]
    if input is None:
        argv.insert(2, "-n")  # send no stdin
    return _nevia(*argv, input=input, timeout=timeout)


def safe_rel(path: str) -> str:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not str(p).strip():
        raise CellError("paths are relative to the POC directory (~/poc) and may not contain ..")
    return str(p)


def create(name, image="", dns_wait=90, sleep=time.sleep):
    args = ["new", name, "--wait", "--timeout", "10m"]
    if image:
        args += ["--image", image]
    rc, out, err = _nevia(*args, timeout=700)
    if rc:
        raise CellError(f"nevia new failed ({rc}): {(err or out).strip()[-500:]}")
    deadline = time.monotonic() + dns_wait
    while True:
        rc, _, _ = _exec(name, "getent hosts pypi.org >/dev/null", timeout=30)
        if rc == 0:
            return
        if time.monotonic() > deadline:
            raise CellError(f"{name} could not resolve names {dns_wait}s after it was created")
        sleep(2)


def exists(name) -> bool:
    """True if the computer exists and is running. A retried step reuses it instead of making a
    second one."""
    rc, out, _ = _nevia("status", name, timeout=60)
    return rc == 0 and bool(re.search(r"^state\s+(ready|running)\b", out, re.M))


def remove(name):
    rc, out, err = _nevia("rm", name, "--yes", "--wait", timeout=300)
    if rc not in (0, 3):  # 3: already gone
        raise CellError(f"nevia rm {name} failed ({rc}): {(err or out).strip()[-300:]}")


def run(name, command, timeout_s=600):
    rc, out, err = _exec(name, _RUN, str(int(timeout_s)), command, timeout=timeout_s + 60)
    encoded, marker, code = out.rpartition("__ROOM_RC__ ")
    if not marker:
        raise CellError(f"nevia exec failed ({rc}): {(err or out).strip()[-500:]}")
    text = base64.b64decode(encoded.strip() or b"").decode("utf-8", errors="replace")
    if len(text) > MAX_OUT:
        text = f"[... {len(text) - MAX_OUT} earlier characters cut ...]\n" + text[-MAX_OUT:]
    code = int(code.strip() or 1)
    if code == 124:
        text += f"\n[timed out after {timeout_s}s]"
    return code, text


def write(name, path, content: str):
    path = safe_rel(path)
    data = base64.b64encode(content.encode()).decode()
    if len(data) > 1_000_000:
        raise CellError("a file sent in one call must be under ~750 KB")
    rc, out, err = _exec(name, _WRITE, path, input=data, timeout=120)
    if rc:
        raise CellError(f"write {path} failed: {(err or out).strip()[-300:]}")


def read(name, path, max_bytes=60_000):
    path = safe_rel(path)
    rc, out, err = _exec(name, _READ, path, str(max_bytes), timeout=120)
    if rc:
        raise CellError(f"read {path} failed: {(err or out).strip()[-300:]}")
    return base64.b64decode(out.strip() or b"").decode("utf-8", errors="replace")


def copy_out(name, dest: Path):
    """The POC directory, minus environments and caches, unpacked into `dest`."""
    rc, out, err = _exec(name, _PACK, timeout=300)
    if rc:
        raise CellError(f"packing ~/poc failed: {(err or out).strip()[-300:]}")
    raw = base64.b64decode(out.strip() or b"")
    if len(raw) > MAX_ARCHIVE:
        raise CellError(f"the POC is {len(raw)} bytes packed; the limit is {MAX_ARCHIVE}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")  # refuses absolute paths, .. and device files
    return sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
