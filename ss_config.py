"""Portable paths and operator configuration for session-search."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import sys
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


def _config_root(home: pathlib.Path, environ: Mapping[str, str]) -> pathlib.Path:
    value = environ.get("XDG_CONFIG_HOME", "").strip()
    return _expanded(value, home) if value else home / ".config"


def _default_data_dir(home: pathlib.Path, environ: Mapping[str, str]) -> pathlib.Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "session-search"
    value = environ.get("XDG_DATA_HOME", "").strip()
    root = _expanded(value, home) if value else home / ".local" / "share"
    return root / "session-search"


def _configured_data_dir(
    home: pathlib.Path, environ: Mapping[str, str]
) -> pathlib.Path | None:
    path = _config_root(home, environ) / "session-search" / "config.toml"
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except FileNotFoundError:
        return None
    except tomllib.TOMLDecodeError as exc:
        print(
            f"Session Search ignored invalid config at {path}: {exc}",
            file=sys.stderr,
        )
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
    configured = _configured_data_dir(resolved_home, env)
    data_dir = _expanded(
        env.get("SS_DATA_DIR")
        or configured
        or _default_data_dir(resolved_home, env),
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
