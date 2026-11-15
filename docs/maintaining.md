# Maintaining paper-fetch (the author's own deployment)

Other users can ignore this page. [AGENTS.md](../AGENTS.md) keeps the release ritual and the
rules that apply to every contributor; the details below were moved out of it.

## Two copies

This public repo is the source of truth for code and docs. A **live** copy runs as the author's
local Claude skill (`~/.claude/skills/paper-fetch/`, real `config.yaml`, real
`holdings.sqlite`). Every fix lands here first, then is synced to live — back live up to
`Documents/Projects/_holding/paper-fetch-live-pre-<ver>-<date>/` before overwriting.

Files that exist only in live and must never be copied here:

- `webbridge_fallback.py`, `ck_webbridge_fallback.py`, `reddit_post_webbridge.py`,
  `edge_bridge.py` — they drive the author's own browser.
- `scrape_holdings.py` — the public repo ships the holdings *schema*, not a scraper (see
  [holdings.md](holdings.md)).

The former private institution-specific fork is retired (2026-08-08); do not revive it.

## Reviewing an external PR

Run a diff harness on master vs the PR branch — secret-store fingerprint (sha256 of an existing
secret), set/get round-trip, miss message, `_pid_alive`, single-instance lock (stale takeover /
live → exit 4), OA ladder end to end. Output identical except renamed APIs and wording = no
Windows regression. The platform the contributor does not have is the half we must test
ourselves. Rebase-merge to keep the author.
