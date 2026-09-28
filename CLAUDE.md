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

## Known gaps

- No `--json` output yet. The workspace convention is `--json` on every command; add it to
  the read commands (`whoami`, `domains`, `domain`, `records`, `services`, `apps`, `email`,
  `health`, `delegation-audit`, `transfer-status`) first.

## Rules

- No names of any organisation, client, site or person in code, docs or fixtures —
  `tests/test_boundary.py` enforces the workspace's private list at
  `../_boundary/denylist.txt` (or `$COMMONS_DENYLIST`). Never copy that list into this repo;
  a test fails if a denylist file appears here. Use `example.com`, `example.org`,
  `example.net`.
- Copyright Simtricity Limited only. Comment fallbacks with `# fallback: …`.
- Never push, tag or create the GitHub repo without the maintainer's explicit go-ahead.
