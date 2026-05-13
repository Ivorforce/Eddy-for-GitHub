"""User-flippable settings, stored in `config/settings.toml`.

Lives outside the DB so a `data/` wipe doesn't reset toggles, and so power
users can pre-set values (and discover knobs not exposed in the UI — see
`config/settings.example.toml`) by hand-editing the file.

Defaults live here in `DEFAULTS`; missing keys fall back silently. UI writes
go through `set()`, which validates against the per-key spec and rewrites the
whole file atomically. Read access is in-process cached and refreshed lazily
on file mtime change so a hand-edit while the app runs picks up on the next
read.

Runtime state (`schema_version`, `last_poll_at`, the HTTP `last_modified_*`
caches) stays in the `meta` table — it's not a setting, it's bookkeeping.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import tomllib
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.toml"


# Per-key spec: default value + validator. The validator returns the coerced
# value or raises ValueError. Keeping the catalogue here means
# `settings.example.toml` is the doc surface and this dict is the source of
# truth for what's a real setting.
def _one_of(*allowed: str) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        if not isinstance(v, str) or v not in allowed:
            raise ValueError(f"must be one of {allowed!r}, got {v!r}")
        return v
    return check


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"must be a bool, got {type(v).__name__}")
    return v


SPEC: dict[str, tuple[Any, Callable[[Any], Any]]] = {
    "triage_mode": ("manual", _one_of("manual", "ai")),
    "auto_refresh": ("live", _one_of("live", "hourly", "daily", "manual")),
    "quiet_bystanders": (True, _bool),
    "ai_auto_judge": (False, _bool),
}

DEFAULTS = {k: default for k, (default, _) in SPEC.items()}


_lock = threading.Lock()
_cache: dict[str, Any] = {}
_mtime: float = 0.0


def _load_unlocked() -> None:
    global _cache, _mtime
    if not SETTINGS_PATH.exists():
        _cache = {}
        _mtime = 0.0
        return
    try:
        with SETTINGS_PATH.open("rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("settings: failed to load %s (%s); using defaults", SETTINGS_PATH, e)
        _cache = {}
        _mtime = 0.0
        return
    coerced: dict[str, Any] = {}
    for k, v in raw.items():
        spec = SPEC.get(k)
        if spec is None:
            # Unknown keys pass through untouched so power-user-only knobs
            # added by hand survive UI writes.
            coerced[k] = v
            continue
        _default, validate = spec
        try:
            coerced[k] = validate(v)
        except ValueError as e:
            log.warning("settings: invalid value for %s (%s); using default", k, e)
    _cache = coerced
    try:
        _mtime = SETTINGS_PATH.stat().st_mtime
    except OSError:
        _mtime = 0.0


def _refresh_if_stale_unlocked() -> None:
    try:
        mtime = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else 0.0
    except OSError:
        return
    if mtime != _mtime:
        _load_unlocked()


def _dump_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        return json.dumps(v)  # JSON strings are valid TOML basic strings
    raise TypeError(f"unsupported TOML value type: {type(v).__name__}")


def _serialize(data: dict[str, Any]) -> str:
    # Flat schema only. Known keys (from SPEC) first in declaration order so
    # the file is predictable; unknown power-user keys sorted after.
    known = [k for k in SPEC if k in data]
    unknown = sorted(k for k in data if k not in SPEC)
    lines = [f"{k} = {_dump_value(data[k])}" for k in known + unknown]
    return "\n".join(lines) + "\n" if lines else ""


def _write_unlocked(data: dict[str, Any]) -> None:
    global _mtime
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".toml.tmp")
    tmp.write_text(_serialize(data), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)
    try:
        _mtime = SETTINGS_PATH.stat().st_mtime
    except OSError:
        _mtime = 0.0


def get(key: str) -> Any:
    """Return the current value for `key`, falling back to the spec default."""
    with _lock:
        _refresh_if_stale_unlocked()
        if key in _cache:
            return _cache[key]
        spec = SPEC.get(key)
        if spec is None:
            raise KeyError(f"unknown setting: {key!r}")
        return spec[0]


def set(key: str, value: Any) -> Any:
    """Validate and persist `key`. Returns the coerced value."""
    spec = SPEC.get(key)
    if spec is None:
        raise KeyError(f"unknown setting: {key!r}")
    _, validate = spec
    coerced = validate(value)
    with _lock:
        _refresh_if_stale_unlocked()
        _cache[key] = coerced
        _write_unlocked(_cache)
    return coerced


def init(meta_migrate: Callable[[], dict[str, Any]] | None = None) -> None:
    """Load the file into cache. If the file doesn't exist and a
    `meta_migrate` callable is supplied, call it and seed the file from the
    returned key/value map. Idempotent — re-init after migration just reloads.
    """
    with _lock:
        if SETTINGS_PATH.exists():
            _load_unlocked()
            return
        seed: dict[str, Any] = {}
        if meta_migrate is not None:
            try:
                raw = meta_migrate() or {}
            except Exception:
                log.exception("settings: meta migration callback failed")
                raw = {}
            for k, v in raw.items():
                spec = SPEC.get(k)
                if spec is None:
                    continue
                _, validate = spec
                try:
                    seed[k] = validate(v)
                except ValueError as e:
                    log.warning("settings: skipping meta value for %s (%s)", k, e)
        if seed:
            _write_unlocked(seed)
            _cache.update(seed)
            log.info("settings: migrated %d key(s) from meta to %s",
                     len(seed), SETTINGS_PATH)
        else:
            _cache.clear()
