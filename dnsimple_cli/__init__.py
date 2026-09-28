"""DNSimple CLI - Domain management tool."""

import sys

import click

from dnsimple_cli import output


def main(argv: list[str] | None = None) -> None:
    """Entry point. `--json` may appear anywhere on the command line."""
    args = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args
    args = [a for a in args if a != "--json"]
    output.configure(json_mode)

    from dnsimple_cli.cli import app

    code = 0
    try:
        # With standalone_mode=False, Click *returns* the code of a typer.Exit rather
        # than raising it; anything else a command returns is not an exit code.
        rv = app(args=args, prog_name="dnsimple", standalone_mode=False)
        code = rv if isinstance(rv, int) else 0
    except click.exceptions.Exit as e:
        code = e.exit_code
    except click.ClickException as e:
        code = e.exit_code
        output.STATE.last_error = e.format_message()
        if not json_mode:
            e.show()
    except click.exceptions.Abort:
        code = 1
        output.STATE.last_error = "aborted"
        if not json_mode:
            output.console.print("Aborted.")
    except Exception as e:  # noqa: BLE001 - last-resort handler for the JSON contract
        if not json_mode:
            raise
        code = 1
        output.STATE.last_error = f"{type(e).__name__}: {e}"

    if json_mode and not output.STATE.emitted:
        if code == 0:
            output.emit({})
        else:
            output.emit_error(output.STATE.last_error or f"exit code {code}")
    sys.exit(code)


__all__ = ["main"]
