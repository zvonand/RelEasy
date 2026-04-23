"""Optional capture of the full process terminal stream to a log file.

When :func:`configure` is called with a path (typically from config
``log_file:``), ``sys.stdout`` and ``sys.stderr`` are wrapped so that
everything the Rich console, Click, the logging module, and tracebacks
emit is appended to that file in addition to the real terminal. When
``configure(None)`` runs, wrappers are removed and the file is closed.
"""

from __future__ import annotations

import atexit
import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console

# Captured on import — the real TTY (or whatever ``sys`` pointed at) before
# we install tees.
_real_stdout: TextIO = sys.stdout
_real_stderr: TextIO = sys.stderr

_log_fp: TextIO | None = None
_patched: bool = False
_console: Console | None = None


class _TeeIO(io.TextIOBase):
    """Write to two text streams; colors follow the primary (terminal)."""

    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self._p = primary
        self._s = secondary

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._p.encoding

    @property
    def errors(self) -> str | None:  # type: ignore[override]
        return getattr(self._p, "errors", "strict")  # type: ignore[no-any-return]

    def write(self, s: str) -> int:  # type: ignore[override]
        n = self._p.write(s)
        self._s.write(s)
        return n

    def flush(self) -> None:  # type: ignore[override]
        self._p.flush()
        self._s.flush()

    def isatty(self) -> bool:  # type: ignore[override]
        return self._p.isatty()

    def fileno(self) -> int:  # type: ignore[override]
        return self._p.fileno()


def _reset_console() -> None:
    global _console
    _console = None


def _teardown() -> None:
    global _log_fp, _patched
    if _patched:
        sys.stdout = _real_stdout
        sys.stderr = _real_stderr
        _patched = False
    if _log_fp is not None:
        _log_fp.close()
        _log_fp = None
    _reset_console()


atexit.register(_teardown)


def configure(log_file: Path | str | None) -> None:
    """Enable or disable file mirroring. Safe to call repeatedly.

    * ``log_file is None`` — remove tees and close the log file.
    * Otherwise — open the file in append mode, tee stdout/stderr, write a
      short session header. The path should already be absolute (as after
      :func:`~releasy.config.load_config` resolves ``log_file:`` in YAML).
    """
    _teardown()
    if not log_file:
        return
    path = Path(log_file) if not isinstance(log_file, Path) else log_file
    path.parent.mkdir(parents=True, exist_ok=True)
    global _log_fp, _patched
    _log_fp = open(path, "a", encoding="utf-8")
    ts = datetime.now(timezone.utc).isoformat()
    _log_fp.write(
        f"\n{'=' * 60}\nreleasy session start {ts}\n{'=' * 60}\n"
    )
    _log_fp.flush()
    sys.stdout = _TeeIO(_real_stdout, _log_fp)
    sys.stderr = _TeeIO(_real_stderr, _log_fp)
    _patched = True
    _reset_console()


def get_console() -> Console:
    """Return the shared :class:`rich.console.Console`, creating it lazily.

    The console always targets the current ``sys.stdout`` (after any tee
    installed by :func:`configure`). Call :func:`configure` before the
    first :meth:`~rich.console.Console.print` if you use ``log_file``.
    """
    global _console
    if _console is None:
        _console = Console()
    return _console


class _ConsoleProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_console(), name)


# Backwards-compatible ``from releasy.termlog import console`` — every
# attribute is resolved on the lazily created ``Console`` instance.
console = _ConsoleProxy()
