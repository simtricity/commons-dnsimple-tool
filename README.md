# dnsimple-tool

An operations CLI for a [DNSimple](https://dnsimple.com/) account: sync every zone to a
local database, see at a glance which services, apps and mail providers each domain uses,
set up common record sets safely, move domains in from other registrars, and produce an
HTML audit report.

> Unofficial. Not affiliated with or endorsed by DNSimple. © Simtricity Limited, MIT.

**Who it's for:** anyone looking after more than a handful of domains in DNSimple who wants
more than the API client gives them. DNSimple's official libraries
([`dnsimple-python`](https://github.com/dnsimple/dnsimple-python),
[`dnsimple-node`](https://github.com/dnsimple/dnsimple-node)) are excellent API clients;
this is the *operations* layer on top: classification, audits, pre-flight checks,
templated record sets and registrar housekeeping.

## Install

```bash
uv tool install git+https://github.com/simtricity/commons-dnsimple-tool   # global `dnsimple` command
# or, from a checkout (editable, picks up code changes):
uv tool install --editable .
```

Requires Python 3.11+ and, for transfer pre-flight checks, the `whois` binary.

## Configuration

Everything the tool keeps lives in one **state directory**, never in the current directory:

| Path | What |
|---|---|
| `~/.simt/dnsimple/` | default state dir; override with `DNSIMPLE_STATE_DIR` |
| `<state>/dnsimple.env` | credentials (mode 0600) |
| `<state>/data/db.json` | local database written by `sync` |
| `<state>/data/report-recommendations.json` | input for `report` (see below) |
| `<state>/reports/` | generated HTML reports |

`<state>/dnsimple.env`:

```
DNSIMPLE_ACCOUNT_ID=12345
DNSIMPLE_ACCESS_TOKEN=<token>
```

Variables already in the environment win, so `doppler run -- dnsimple …` works unchanged.
If `<state>/dnsimple.env` does not exist, `./.env` in the current directory is read instead.

## Usage

### Sync and browse

```bash
dnsimple whoami                  # verify credentials (exits non-zero on failure)
dnsimple sync                    # incremental sync from the API
dnsimple sync --force            # full sync of all zones
dnsimple domains [--all]         # primary domains [plus subdomains with their own mail]
dnsimple domain example.com      # one domain in detail
dnsimple records example.com     # every record in the zone
dnsimple services                # ownership verifications across all domains
dnsimple apps                    # traffic routing (CNAME/ALIAS targets) across all domains
dnsimple email [--mx|--smtp]     # inbound (MX) and outbound (SPF/DKIM) providers
```

Records are classified as **services** (ownership verifications: Google, Apple, ACME
challenges…), **apps** (routing to Fly.io, Deno Deploy, GitHub Pages…), **email MX**
(Google Workspace, ImprovMX, Amazon SES…) and **email SMTP** (senders identified via SPF
includes and DKIM).

### Record sets and edits

Every command below that writes shows the change and asks for confirmation; `--yes` skips
the prompt. **`record-create` is the exception: it writes immediately.**

```bash
dnsimple email-setup example.com --provider google       # Google Workspace MX + SPF
dnsimple email-setup example.org --provider improvmx     # ImprovMX forwarding MX + SPF
dnsimple app-setup example.com --acme-token TOKEN        # Deno Deploy: www + apex redirect + ACME
dnsimple app-setup example.com --apex --acme-token TOKEN # Deno Deploy on the apex (ALIAS)
dnsimple clerk-setup example.com --instance-id ID        # Clerk: 5 CNAMEs
dnsimple redirect-setup example.net --target https://www.example.com
dnsimple record-create example.com TXT "" "verification=VALUE"
dnsimple record-update example.com 12345 --content "new-value"
dnsimple record-delete example.com 12345
```

### Transfers and delegation

```bash
dnsimple contacts                                   # registrant contacts
dnsimple transfer-check example.net                 # TLD, pricing and WHOIS lock pre-flight
dnsimple transfer example.net --auth-code CODE      # initiate an inbound transfer
dnsimple transfer-status --refresh                  # progress of every transfer
dnsimple transfer-cancel example.net
dnsimple nameservers example.net [--set-dnsimple | --ns a --ns b]
dnsimple delegation-audit                           # domains not yet delegated to DNSimple
```

### Health and reporting

```bash
dnsimple health example.com   # MX, SPF, DKIM, DMARC, NS, www and apex checks
dnsimple report               # HTML audit report into <state>/reports/
```

`report` needs `<state>/data/report-recommendations.json`: a list of `groups`, each with
`name`, `category`, `primary_domain`, `domains` and `recommendations` (`severity`, `title`,
`description`). Categories are yours to define with an optional top-level list:

```json
{
  "categories": [
    {"key": "production", "title": "Production", "label": "Prod", "color": "blue"},
    {"key": "parked", "title": "Parked domains", "color": "gray"}
  ],
  "groups": [ ... ]
}
```

Without `categories`, one section is made per distinct `category` value. Colours:
`purple`, `blue`, `green`, `amber`, `gray`.

## Verifying a change

After a write, check the **delegated** nameservers, not the API: `dig +short NS <domain>`
shows them (DNSimple's are `ns1.dnsimple-edge.com` and siblings; `ns1.dnsimple.com` is not
authoritative for delegated zones). Edge propagation can lag a few minutes behind the API.

## Development

```bash
uv sync
uv run pytest        # offline: boundary, state resolution, record generators
```

## Licence

MIT, © Simtricity Limited. See `LICENSE`.
