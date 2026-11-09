"""Put an approved draft on its own branch of the blog, build the site with it, and commit.

Nothing is pushed. A person reads the branch and merges it.
"""

import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import yaml

from . import gates


class PublishError(Exception):
    pass


def display_model(model_id: str) -> str:
    """claude-sonnet-5-5 -> Claude Sonnet 5.5"""
    words, nums = [], []
    for part in model_id.split("-"):
        (nums if part.isdigit() else words).append(part)
    return " ".join([w.capitalize() for w in words] + ([".".join(nums)] if nums else []))


def finalize(
    markdown: str,
    *,
    author: str,
    model: str,
    pitch: dict,
    pitch_id: int,
    poc_path: str | None,
    today: date,
) -> tuple[str, dict]:
    """Code owns the fields that are facts about the run, not the writing."""
    meta, body = gates.split_front_matter(markdown)
    meta.update(
        {
            "date": today,
            "author": author,
            "model": display_model(model),
            "room_pitch": pitch_id,
            "prompt": pitch.get("prompt") or pitch.get("angle") or pitch["title"],
        }
    )
    if poc_path:
        meta["poc"] = poc_path
    order = ["title", "slug", "date", "author", "model", "summary", "tags", "faq", "prompt"]
    ordered = {k: meta[k] for k in order if k in meta} | {
        k: v for k, v in meta.items() if k not in order
    }
    head = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=100)
    return f"---\n{head}---\n{body.lstrip(chr(10))}", ordered


def git(repo, *args, check=True):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode:
        raise PublishError(f"git {' '.join(args)}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def branch_name(blog, prefix, slug):
    name, n = f"{prefix}{slug}", 2
    while git(blog, "branch", "--list", name):
        name, n = f"{prefix}{slug}-{n}", n + 1
    return name


def existing_branch(cfg, pitch_id, slug) -> str | None:
    """The branch an earlier, interrupted publish of this pitch already committed, if any."""
    blog, prefix = cfg.blog, cfg["blog"]["branch_prefix"]
    for name in git(blog, "branch", "--list", f"{prefix}{slug}", f"{prefix}{slug}-*").split():
        name = name.lstrip("*+ ")
        if f"(pitch {pitch_id})" in git(blog, "log", "-1", "--format=%B", name):
            return name
    return None


def publish(cfg, *, markdown, meta, poc_dir: Path | None, pitch_id, say=print) -> tuple[str, str]:
    blog, b = cfg.blog, cfg["blog"]
    if not (blog / ".git").exists():
        raise PublishError(f"{blog} is not a git repository")
    slug = meta["slug"]
    branch = branch_name(blog, b["branch_prefix"], slug)
    wt = cfg.state_dir / "worktrees" / f"{pitch_id}-{slug}"
    if wt.exists():
        git(blog, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)
    base = git(blog, "rev-parse", "HEAD")
    git(blog, "worktree", "add", "-q", "-b", branch, str(wt), base)
    try:
        post = wt / b["posts_dir"] / f"{meta['date']}-{slug}.md"
        post.write_text(markdown)
        files = [str(post.relative_to(wt))]
        if poc_dir and poc_dir.exists() and any(poc_dir.iterdir()):
            dest = wt / b["pocs_dir"] / slug
            shutil.copytree(poc_dir, dest)
            files.append(str(dest.relative_to(wt)))
        say(f"  building the site on {branch}")
        # The blog's own .venv, unless the room runs where that one can't execute (a Linux
        # container next to a macOS checkout): then ROOM_BLOG_VENV names one of its own.
        env_venv = os.environ.get("ROOM_BLOG_VENV") or str(blog / ".venv")
        build_cmd = os.environ.get("ROOM_BLOG_BUILD") or b["build"]
        build = subprocess.run(
            f"mise trust -q . >/dev/null 2>&1; {build_cmd}",
            shell=True,
            cwd=wt,
            capture_output=True,
            text=True,
            timeout=600,
            env={**_env(), "UV_PROJECT_ENVIRONMENT": env_venv, "MISE_YES": "1"},
        )
        if build.returncode:
            raise PublishError(
                "the blog build failed with this draft:\n" + (build.stderr or build.stdout)[-2000:]
            )
        out = wt / "output"
        page = next(out.glob(f"**/{slug}.html"), None) if out.exists() else None
        if page is None:
            raise PublishError(f"the build passed but no {slug}.html was written")
        git(wt, "add", "--", *files)
        msg = (
            f"Add draft: {meta['title']}\n\nWritten by the AI writers room (pitch {pitch_id})"
            f" as {meta['author']}. A person reviews and merges."
        )
        git(
            wt,
            "-c",
            "user.name=AI Writers Room",
            "-c",
            "user.email=writers-room@localhost",
            "commit",
            "-q",
            "-m",
            msg,
        )
        return branch, str(post.relative_to(wt))
    except Exception:
        git(blog, "worktree", "remove", "--force", str(wt), check=False)
        git(blog, "branch", "-D", branch, check=False)
        raise
    finally:
        if wt.exists():
            git(blog, "worktree", "remove", "--force", str(wt), check=False)


def _env():
    return {k: v for k, v in os.environ.items() if not re.match(r"VIRTUAL_ENV$", k)}
