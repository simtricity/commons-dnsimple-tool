# Changelog

## 0.1.0 — unreleased

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
