# Changelog

## 0.2.0 — unreleased

Agent-friendly output and fixes found while building a Claude skill on the CLI.

- **`--json` on every command**, anywhere on the command line. stdout carries exactly one
  object: `{"ok": true, ...}` or `{"ok": false, "error": "..."}` with a non-zero exit.
  Progress and tables go to stderr. Writes report a `changes` list. A write that needs
  confirmation refuses under `--json` (exit 3) unless `--yes` is given.
- **`records` shows the record ID** (table and JSON), which `record-update` and
  `record-delete` need.
- **Timestamps are UTC with a `Z`** (`2026-09-28T15:15:32Z`); stamps written by 0.1.0 as
  naive local time are converted on display. **`sync` refreshes `synced_at` even when
  nothing changed**, so the stamp proves freshness. Read commands include `synced_at`.
- **`transfer-check` on a domain already registered at DNSimple** says so and stops,
  instead of printing a lock warning, a price and a transfer checklist.
- **Colour follows `NO_COLOR`** and is off whenever the output is not a terminal.
- **`nameservers` is read-only.** Changing delegation is the new `nameservers-set
  <domain> --dnsimple | --ns <host>…`, which confirms unless `--yes`. The old
  `nameservers --set-dnsimple / --ns` flags now fail with a pointer to the new command.
  *Breaking for scripts that used them.*
- **`health` understands send-only domains** (no root MX, but DKIM or a sender subdomain
  with its own SPF/MX): MX passes, and a missing root SPF is a warning recommending
  `v=spf1 -all` instead of a failure. Results carry an optional `recommendation`, and the
  report a `mail_mode` (`receive`, `send-only`, `none`).
- Tests: the JSON contract, ids, stamps, colour, `nameservers` and health modes (29 total).
- Works on Typer 0.27+, which no longer depends on Click (the entry point had imported
  `click`, breaking `uv tool install`). Lock upgraded to Typer 0.27.2; CI now smoke-tests
  the tool-installed command, not only the locked venv.

## 0.1.0 — 2026-09-28 (on `main`, untagged)

First release in simt-commons. Behaviour is unchanged from the private tool it was
extracted from, except where state is kept:

- **State directory.** Credentials, the database and reports now live in
  `$DNSIMPLE_STATE_DIR` (default `~/.simt/dnsimple/`) instead of relative to the current
  directory. Credentials fall back to `./.env`; the environment still wins.
- **Report categories are data.** `report-recommendations.json` may declare its own
  `categories`; without them, sections are derived from the groups.
- Offline tests: boundary (no organisation names; the private list lives outside the repo),
  state resolution, record generators. CI runs them plus pyright on every push.
- Type fixes: DNSimple timestamps are parsed to `datetime` explicitly; `transfer` guards an
  unset registrant before calling the API.
- `uv.lock` is committed.
