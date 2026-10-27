"""Load FastInsight environment from the process env and an optional .env file."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and not os.environ.get(key, "").strip():
            os.environ[key] = value


def load_env(env_file: Optional[Path] = None) -> Path:
    """Load the first existing .env without overwriting already-set variables."""
    candidates = []
    if env_file:
        candidates.append(Path(env_file))
    explicit = os.environ.get("FASTINSIGHT_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend((ROOT / ".env", Path.cwd() / ".env"))
    seen = set()
    for path in candidates:
        resolved = path.expanduser()
        try:
            resolved = resolved.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        load_env_file(resolved)
    roster = os.environ.get("FASTINSIGHT_ROSTER", "").strip()
    if roster:
        roster_path = Path(roster).expanduser()
        if not roster_path.is_absolute():
            os.environ["FASTINSIGHT_ROSTER"] = str((ROOT / roster_path).resolve())
    elif not os.environ.get("FASTINSIGHT_ROSTER"):
        os.environ["FASTINSIGHT_ROSTER"] = str(ROOT / "assets" / "colleagues.yaml")
    return ROOT


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def require_env(*names: str) -> None:
    missing = [name for name in names if not env(name)]
    if missing:
        raise SystemExit("请设置 " + " 和 ".join(missing))
