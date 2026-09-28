"""Output: human (Rich) or machine (`--json`), one place for both.

Contract under `--json`: stdout carries exactly one JSON object and nothing else.
Success is `{"ok": true, ...}`; failure is `{"ok": false, "error": "..."}` with a non-zero
exit. Progress and human tables go to stderr. Colour is off when `NO_COLOR` is set or the
stream is not a terminal.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import typer
from rich.console import Console


class _State:
    json: bool = False
    emitted: bool = False
    last_error: str | None = None
    changes: list[dict[str, Any]] = []
    console: Console | None = None


STATE = _State()

#: Exit code when `--json` hits a write that needs `--yes`.
EXIT_CONFIRMATION_REQUIRED = 3

_MARKUP = re.compile(r"\[/?[a-z0-9 #,._-]*\]", re.I)


def _plain(stream) -> bool:
    return bool(os.environ.get("NO_COLOR")) or not stream.isatty()


def configure(json_mode: bool) -> None:
    """Set the mode and build the console. Called once by `main()` before any command runs."""
    STATE.json = json_mode
    STATE.emitted = False
    STATE.last_error = None
    STATE.changes = []
    stream = sys.stderr if json_mode else sys.stdout
    plain = _plain(stream)
    STATE.console = Console(
        stderr=json_mode,
        color_system=None if plain else "auto",
        highlight=not plain,
        emoji=not plain,
    )


class _ConsoleProxy:
    """`console.print(...)` everywhere in the CLI; remembers the last error line."""

    def print(self, *objects: Any, **kwargs: Any) -> None:
        if STATE.console is None:
            configure(False)
        assert STATE.console is not None
        if objects and isinstance(objects[0], str) and "[red]" in objects[0]:
            STATE.last_error = _MARKUP.sub("", objects[0]).strip().removeprefix("Error:").strip()
        STATE.console.print(*objects, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if STATE.console is None:
            configure(False)
        return getattr(STATE.console, name)


console = _ConsoleProxy()


def show(renderable: Any) -> None:
    """A human-only renderable (tables). Skipped under `--json`; the data goes to `emit`."""
    if not STATE.json:
        console.print(renderable)


def emit(data: dict[str, Any]) -> None:
    """Write the command's result. Under `--json` this is the one stdout object."""
    if STATE.json and not STATE.emitted:
        payload = {"ok": True, **data}
        if STATE.changes and "changes" not in payload:
            payload["changes"] = STATE.changes
        sys.stdout.write(json.dumps(payload, default=_default, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        STATE.emitted = True


def emit_error(message: str) -> None:
    if STATE.json and not STATE.emitted:
        sys.stdout.write(json.dumps({"ok": False, "error": message}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        STATE.emitted = True


def fail(message: str, code: int = 1) -> None:
    """Report an error and exit non-zero."""
    console.print(f"[red]Error:[/red] {message}")
    STATE.last_error = message
    raise typer.Exit(code)


def ask(prompt: str) -> bool:
    """Confirm a write. Under `--json` there is no one to answer, so refuse: use `--yes`."""
    if STATE.json:
        STATE.last_error = "confirmation required: re-run with --yes to apply"
        raise typer.Exit(EXIT_CONFIRMATION_REQUIRED)
    return typer.confirm(prompt)


def record_change(op: str, **fields: Any) -> None:
    """Called for every write sent to DNSimple, so `--json` can report what changed."""
    STATE.changes.append({"op": op, **fields})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None) -> str | None:
    """ISO 8601 in UTC with a `Z`, to the second. Naive values are read as local time
    (what versions before 0.2.0 stored) and converted."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # fallback: pre-0.2.0 stamps were naive local time
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return utc_iso(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    return str(o)
