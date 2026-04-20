"""Per-project advisory locks.

Two ``releasy`` invocations on the same project (same ``name:``) must
serialize — they share one state file. Two invocations on *different*
projects can run concurrently. We enforce this with a ``fcntl.flock``
lock on a per-project lockfile under :func:`config.state_root`.

The lockfile body carries diagnostics (PID, command, host, started_at)
so a contending process can print a useful "blocked by …" message
instead of a generic "resource busy".
"""

from __future__ import annotations

import fcntl
import os
import platform
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import click

from releasy.config import Config, lock_file_path


def _format_holder(raw: str) -> str:
    """Render the lockfile body for a "blocked by" message; tolerate empty."""
    cleaned = raw.strip()
    return cleaned or "another releasy process (lockfile is empty)"


def _write_holder(fp, command: str | None = None) -> None:
    """Stamp the lockfile with our identity for contending readers.

    Truncates first so we never leave behind stale tail bytes from a
    previous holder when the new holder's payload is shorter.
    """
    fp.seek(0)
    fp.truncate()
    cmd = command if command is not None else " ".join(sys.argv)
    body = (
        f"pid={os.getpid()}\n"
        f"host={platform.node()}\n"
        f"command={cmd}\n"
        f"started={datetime.now(timezone.utc).isoformat()}\n"
    )
    fp.write(body)
    fp.flush()


@contextmanager
def project_lock(config: Config) -> Iterator[Path]:
    """Acquire an exclusive lock for ``config``'s project.

    Non-blocking: on contention raises :class:`click.ClickException` with
    the holder's identity. The lockfile is created on demand and removed
    on a clean release. Yields the lockfile path so callers can
    surface it in error messages if needed.
    """
    lock_path: Path = lock_file_path(config.name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open in read-write so we can both stamp our identity and read a
    # contending holder's stamp without re-opening the file.
    fp = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fp.seek(0)
            holder = fp.read()
            fp.close()
            raise click.ClickException(
                f"Project {config.name!r} is already locked by another "
                f"releasy process. Lockfile: {lock_path}\n"
                f"  {_format_holder(holder)}"
            )

        _write_holder(fp)
        try:
            yield lock_path
        finally:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            fp.close()
        except OSError:
            pass
        # Best-effort removal — leaving the file behind on a crash is
        # harmless (next run will simply lock + overwrite it).
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
