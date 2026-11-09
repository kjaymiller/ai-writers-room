"""room.toml, resolved against the directory it lives in."""

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
ROLES = ["scout", "editor", "researcher", "poc", "writer", "reviewer", "chat"]


@dataclass
class Config:
    raw: dict
    root: Path
    path: Path | None = None  # the file it was loaded from; the tool extension loads the same one

    def __getitem__(self, key):
        return self.raw[key]

    def get(self, key, default=None):
        return self.raw.get(key, default)

    @property
    def state_dir(self) -> Path:
        d = Path(os.environ.get("ROOM_STATE", self.root / ".room"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        return self.state_dir / "room.db"

    @property
    def blog(self) -> Path:
        # ROOM_BLOG_PATH: where the blog is mounted when the room runs in a container
        return (self.root / os.environ.get("ROOM_BLOG_PATH", self.raw["blog"]["path"])).resolve()

    def authors(self) -> dict:
        data = json.loads((self.blog / self.raw["blog"]["authors_file"]).read_text())
        return {a["slug"]: a for a in data}

    def posts_dir(self) -> Path:
        return self.blog / self.raw["blog"]["posts_dir"]

    def job_dir(self, pitch_id: int) -> Path:
        d = self.state_dir / "jobs" / str(pitch_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def model(self, role: str) -> str:
        return self.raw["models"][role]

    def price(self, model: str) -> dict:
        return self.raw.get("prices", {}).get(model, {})


def load(path: Path | None = None) -> Config:
    path = Path(path or os.environ.get("ROOM_CONFIG", ROOT / "room.toml")).resolve()
    raw = tomllib.loads(path.read_text())
    missing = [r for r in ROLES if r not in raw.get("models", {})]
    if missing:
        raise SystemExit(f"room.toml: [models] has no entry for {', '.join(missing)}")
    return Config(raw, path.parent, path)
