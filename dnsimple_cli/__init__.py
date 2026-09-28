"""DNSimple CLI - Domain management tool."""

import sys

import typer

from dnsimple_cli import output


def _is_usage_error(e: BaseException) -> bool:
    """Click's UsageError family, or the same classes in Typer's vendored Click (0.27+).

    Matched by shape, not by import, because Typer stopped depending on Click: importing
    `click` here broke `uv tool install`, which resolves the newest Typer.
    """
    return hasattr(e, "exit_code") and hasattr(e, "format_message")


def main(argv: list[str] | None = None) -> None:
    """Entry point. `--json` may appear anywhere on the command line."""
    args = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args
    args = [a for a in args if a != "--json"]
    output.configure(json_mode)

    from dnsimple_cli.cli import app

    code = 0
    try:
        # With standalone_mode=False the parser *returns* a typer.Exit's code rather than
        # raising it; anything else a command returns is not an exit code.
        rv = app(args=args, prog_name="dnsimple", standalone_mode=False)
        code = rv if isinstance(rv, int) else 0
    except typer.Exit as e:
        code = getattr(e, "exit_code", 0)
    except typer.Abort:
        code = 1
        output.STATE.last_error = "aborted"
        if not json_mode:
            output.console.print("Aborted.")
    except Exception as e:  # noqa: BLE001 - usage errors, then the JSON contract's last resort
        if _is_usage_error(e):
            code = int(getattr(e, "exit_code", 2))
            output.STATE.last_error = e.format_message()  # type: ignore[attr-defined]
            if not json_mode and hasattr(e, "show"):
                e.show()  # type: ignore[attr-defined]
        elif not json_mode:
            raise
        else:
            code = 1
            output.STATE.last_error = f"{type(e).__name__}: {e}"

    if json_mode and not output.STATE.emitted:
        if code == 0:
            output.emit({})
        else:
            output.emit_error(output.STATE.last_error or f"exit code {code}")
    sys.exit(code)


__all__ = ["main"]
