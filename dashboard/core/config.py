"""Settings: one flat config.json. The core and each module declare their keys with `Setting`."""

import json
import os
import re
import threading

from dashboard.core.storage import DATA_GEN
from dashboard.core.util import to_bool

READ_ONLY_MESSAGE = ("Settings are read-only: \"read_only\" is enabled in config.json on the server. "
                     "To change settings, set it to false there and restart the dashboard.")


class Setting:
    """A config.json key.

    parse      validates and normalises a value; raises ValueError ("must be ..." messages get the
               key name prepended)
    page       what a change from the web page may do: "allow", "ignore" (silently skipped; file
               only) or "reject" (an error, e.g. read_only)
    invalid    value to use when config.json holds an invalid value (default: the default), e.g.
               read_only fails closed to True"""

    def __init__(self, default, parse=None, page="allow", invalid=None):
        self.default, self.parse, self.page, self.invalid = default, parse, page, invalid


# --------------------------------------------------------------------------- reusable parsers

def boolean(v):
    return to_bool(v)


def integer(lo, hi):
    def parse(v):
        v = int(v)
        if not lo <= v <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return v
    return parse


def number(lo, hi):
    def parse(v):
        v = float(v)
        if not lo <= v <= hi:
            raise ValueError(f"must be between {lo:g} and {hi:g}")
        return v
    return parse


def choice(*options):
    def parse(v):
        v = str(v).strip().lower()
        if v not in options:
            raise ValueError("must be " + " or ".join(f"'{o}'" for o in options))
        return v
    return parse


def text(v):
    return str(v).strip()


def nonempty_text(v):
    v = str(v).strip()
    if not v:
        raise ValueError("must not be empty")
    return v


def url(required):
    def parse(v):
        v = str(v).strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        if required and not v:
            raise ValueError("is required")
        return v
    return parse


def hostname(v):
    v = str(v).strip().lower()
    if not re.match(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", v):
        raise ValueError("must be a hostname like near.org")
    return v


def existing_path(v):
    v = str(v).strip() or "/"
    if not os.path.exists(v):
        raise ValueError(f"{v!r} does not exist")
    return v


def validate(registry, patch, from_file=False):
    """Validate a settings change. `from_file` is True when loading config.json; only then may
    file-only keys (read_only, modules, db_path) be set, so the page can never unlock itself."""
    if not isinstance(patch, dict):
        raise ValueError("settings must be a JSON object")
    out = {}
    for key, value in patch.items():
        s = registry.get(key)
        if s is None:
            continue
        if not from_file and s.page != "allow":
            if s.page == "reject":
                raise ValueError(f"{key} can only be changed by editing config.json on the server")
            continue
        try:
            out[key] = s.parse(value) if s.parse else value
        except (ValueError, TypeError) as e:
            msg = str(e)
            raise ValueError(f"{key} {msg}" if msg.startswith(("must", "is required")) else f"{key}: {msg}")
    return out


class Config:
    """The live settings. Collectors call get()/snapshot(); the page changes them via update().
    Each subscriber gets its own Event, set on every change (wakes collection loops early)."""

    def __init__(self, path, registry):
        self.path, self.registry = path, registry
        self.lock = threading.Lock()
        self._subscribers = []
        self.data = {k: s.default for k, s in registry.items()}
        if os.path.exists(path):
            with open(path) as f:
                loaded = json.load(f)
            # Validate each setting from the file; a bad value falls back to its default (or the
            # setting's fail-safe value) instead of crashing a collector thread later.
            for key, value in loaded.items():
                try:
                    self.data.update(validate(registry, {key: value}, from_file=True))
                except (ValueError, TypeError) as e:
                    s = registry[key]
                    if s.invalid is not None:
                        self.data[key] = s.invalid
                        print(f"[config] invalid {key} value {value!r} in {path}: {e}; treating it as "
                              f"{s.invalid!r}.", flush=True)
                        continue
                    print(f"[config] ignoring invalid {key!r} in {path}: {e}; using default {s.default!r}", flush=True)
        else:
            self.save()

    def subscribe(self):
        ev = threading.Event()
        self._subscribers.append(ev)
        return ev

    def get(self, key):
        with self.lock:
            return self.data[key]

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def update(self, patch):
        """Apply a settings change from the web page. Refused entirely in read-only mode."""
        with self.lock:
            if self.data.get("read_only"):
                raise PermissionError(READ_ONLY_MESSAGE)
        clean = validate(self.registry, patch)
        with self.lock:
            if self.data.get("read_only"):  # re-check under the same lock as the write
                raise PermissionError(READ_ONLY_MESSAGE)
            self.data.update(clean)
            self.save()
        DATA_GEN.bump()
        for ev in self._subscribers:
            ev.set()
        return self.snapshot()

    @property
    def interval(self):
        with self.lock:
            return 1 if self.data["fast_mode"] else max(1, int(self.data["interval_seconds"]))
