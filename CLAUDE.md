# CLAUDE.md — dnsimple-tool

An operations CLI for a DNSimple account (Python, Typer, Rich, TinyDB). Repo-specific
notes only; workspace conventions are in `../CLAUDE.md`, and whether something belongs
here at all is `../BOUNDARY.md`.

## Commands

```bash
uv sync
uv run dnsimple --help
uv run pytest            # offline tests, including the boundary test
uv tool install --editable .   # global `dnsimple` from this checkout
```

## Architecture

1. `api.py` — DNSimple API client: domains, records (CRUD), zones, contacts, TLDs, pricing,
   transfers, delegation. Rate limiting and retries.
2. `storage.py` — `get_state_dir()` (`$DNSIMPLE_STATE_DIR`, default `~/.simt/dnsimple`),
   TinyDB in `<state>/data/db.json` with `domains`, `metadata` and `transfers` tables;
   `process_api_data()` runs detection during `sync`.
3. `cli.py` — Typer commands. Credentials load from `<state>/dnsimple.env`, falling back to
   `./.env`; the environment wins over both.
4. `report.py` — HTML audit report from the database plus
   `<state>/data/report-recommendations.json`; categories come from that file.

| Module | Purpose |
|---|---|
| `models.py` | Pydantic models; subdomain helpers (`is_special_prefix()`) |
| `services.py` | Pattern detection: services, apps, email MX, email SMTP |
| `email_setup.py` / `app_setup.py` / `clerk_setup.py` / `redirect_setup.py` | Record-set templates plus pre-flight conflict checks |
| `health_check.py` | MX, SPF, DKIM, DMARC, NS, www, apex checks |

## Design decisions

- **State is never relative to the cwd or the install location.** The package may be
  installed anywhere (`uv tool install git+…`); everything mutable is in the state dir.
- **Subdomains with their own mail** (`send.<domain>`, `front-mail.<domain>`) are separate
  Domain entries with `parent_domain` set. `_dmarc`, `_acme-challenge`, `*._domainkey` are
  *not* subdomains — they belong to their parent.
- **TXT content arrives quoted** from DNSimple; detection strips the quotes.
- **Verify writes on the delegated edge nameservers** (`ns1.dnsimple-edge.com` …), not
  `ns1.dnsimple.com`; propagation can lag the API by minutes.
- **Writes confirm unless `--yes`.** `record-create` is the one command that does not
  confirm; keep that in mind when scripting it.

## Output contract (`dnsimple_cli/output.py`)

- `main()` strips `--json` from anywhere in argv and configures one console: stdout in human
  mode, stderr under `--json`, plain whenever `NO_COLOR` is set or the stream is not a TTY.
- Commands print progress with `console.print`, human-only tables with `show()`, and their
  result with `emit({...})` (snake_case keys, as DNSimple sends them). `fail(msg)` for
  errors. `ask(prompt)` instead of `typer.confirm`: it refuses under `--json` (exit 3).
- `get_client()` returns a `RecordingClient`, so every API write lands in `changes`
  without each command tracking it. `main()` guarantees exactly one stdout object.
- Store and print time in UTC (`utcnow()`, `utc_iso()`); 0.1.0's naive local stamps are
  converted on read.
- The marketplace `/simt-local:dnsimple` skill drives this CLI through `--json`; changing a
  key is a breaking change for it.

## Rules

- No names of any organisation, client, site or person in code, docs or fixtures —
  `tests/test_boundary.py` enforces the workspace's private list at
  `../_boundary/denylist.txt` (or `$COMMONS_DENYLIST`). Never copy that list into this repo;
  a test fails if a denylist file appears here. Use `example.com`, `example.org`,
  `example.net`.
- Copyright Simtricity Limited only. Comment fallbacks with `# fallback: …`.
- Never push, tag or create the GitHub repo without the maintainer's explicit go-ahead.
