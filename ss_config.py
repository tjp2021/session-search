"""Portable paths and operator configuration for session-search."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import tomllib
from collections.abc import Mapping


@dataclasses.dataclass(frozen=True)
class Paths:
    data_dir: pathlib.Path
    db: pathlib.Path
    last_results: pathlib.Path
    lock: pathlib.Path
    handoff_dir: pathlib.Path
    model_cache: pathlib.Path


def _expanded(path: str | pathlib.Path, home: pathlib.Path) -> pathlib.Path:
    value = str(path)
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return pathlib.Path(value).expanduser()


def _configured_data_dir(home: pathlib.Path) -> pathlib.Path | None:
    path = home / ".config" / "session-search" / "config.toml"
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except FileNotFoundError:
        return None
    storage = payload.get("storage", {})
    if not isinstance(storage, dict):
        return None
    value = storage.get("data_dir")
    if not isinstance(value, str) or not value.strip():
        return None
    return _expanded(value.strip(), home)


def resolve_paths(
    home: pathlib.Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Paths:
    resolved_home = pathlib.Path(home or pathlib.Path.home()).expanduser()
    env = os.environ if environ is None else environ
    configured = _configured_data_dir(resolved_home)
    data_dir = _expanded(
        env.get("SS_DATA_DIR")
        or configured
        or resolved_home / "Library" / "Application Support" / "session-search",
        resolved_home,
    )
    return Paths(
        data_dir=data_dir,
        db=data_dir / "session-search.sqlite",
        last_results=data_dir / "last-results.json",
        lock=data_dir / "session-search.lock",
        handoff_dir=data_dir / "context-packets",
        model_cache=data_dir / "models",
    )
