"""DNSimple CLI - Domain management tool."""

from dnsimple_cli.cli import app


def main():
    """Entry point for the CLI."""
    app()


__all__ = ["main", "app"]
