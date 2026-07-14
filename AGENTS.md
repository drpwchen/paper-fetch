# AGENTS.md — for AI agents deploying, adapting, or calling paper-fetch

Most people will point you (an AI coding agent) at this repo and say "set this up for my
library" or "explain how this works." This file tells you how to do that correctly, how to
know it worked, and where the hard lines are.

## What this tool is

A publisher-aware full-text PDF fetcher built as a **route ladder**: open access (Unpaywall +
Semantic Scholar + PMC) → official publisher TDM APIs → the user's own institutional library
proxy → a printed resolver link for a manual finish. It is the download end of a pipeline; the
reading end is `claude-paper-tools`, the discovery end is `paper-radar`.

## Deploying it for a user (the happy path)

1. `pip install -r requirements.txt`. For the proxy layer also `python -m patchright install chromium`.
2. `cp config.example.yaml config.yaml`. Fill in:
   - `unpaywall_email`, `rate.contact` — the user's email.
   - `institution.sfx_base / remote_auth_base / proxy_suffix` — the user's library's own
     endpoints. **Ask the user for these, or help them find them** — the four off-campus
     families and where each value hides are in [docs/library-setup.md](docs/library-setup.md).
     Do NOT reuse endpoints from anywhere else.
3. Help the user store their own credentials in the local secret store (see README). Never put
   credential values in `config.yaml` or anywhere in the repo.
4. **Verify layer 1 before touching anything else** — this is your smoke test:
   ```bash
   python paper_fetch.py 10.1186/s12984-023-01168-x out.pdf   # a real OA article; must yield a PDF
   ```
   If that fails, the problem is config or network, not the library. Fix it here, not later.
5. Only then the proxy layer: implement `login()` and — if the user's library uses LWW/Ovid —
   `_lww_ovid_pdf()`. Both are documented stubs; adapt the selectors and request sequence to
   the user's own library by inspecting it in devtools. Then:
   ```bash
   python library_session.py check                 # is the session alive?
   python library_session.py fetch <DOI> out.pdf   # a DOI the library definitely holds
   ```
6. Build the holdings table if the user has a library ([docs/holdings.md](docs/holdings.md)).
   It is what makes step 7 possible.

## When a route "looks broken", check entitlement FIRST

**This is the most expensive mistake in this codebase's history — do not repeat it.**

An article the library does **not** hold makes a *working* route return reader HTML or a 403.
That signal is indistinguishable from a broken URL template. Sage, Taylor & Francis and Oxford
were each declared "dead, needs reverse engineering" on this basis; all three worked on the
first retry with an article the library actually holds.

So, before you debug, patch, or reverse-engineer any publisher route:

```bash
python holdings.py <DOI>     # subscribed? and is this article's YEAR inside the coverage?
```

- `subscribed=True, covered=True` and it still fails → **now** you have a real bug worth fixing.
- `covered=False` → the library holds that journal but not that year. Not a bug. Pick another
  test article.
- `subscribed=None` (not in the table) → **unknown, not no-go.** Some resources (JAMA in one
  library) sit outside the e-journal list yet are served fine. Warn and try anyway.
- Never build an entitlement check on per-article link-resolver (SFX/OpenURL) queries — the same
  DOI returns a full-text target on one call and nothing minutes later.

Second corollary: **a "dead end" verdict can be wrong.** BMJ was documented here as a Cloudflare
dead end; in fact the WAF only blocks *headless* requests, and a real headful navigation passes
first try. If a citation-meta route returns `cf_block`, try `nav=True` before concluding anything.

## Calling it from an orchestrator

- `paper_fetch.py --json <DOI> <out>` prints **exactly one JSON envelope on stdout** (all
  diagnostics go to stderr): `{schema, doi, ok, route, tried[], bytes, sha256, path,
  resolver_url?, elapsed_s}`. Parse that; do not scrape logs.
- **Exit codes** (same table for both scripts): `0` PDF obtained · `1` usage error · `2` no route
  / auth failed (genuinely unavailable) · `4` profile busy · `5` watchdog abort.
- **`4` and `5` mean "retry, serially" — they do NOT mean "this paper has no full text."**
  Recording them as unavailable is the easiest way to wrongly write a paper off.
- The proxy layer is **strictly serial** with a courtesy delay. Never parallelise it: the browser
  profile is an exclusive resource (parallel callers deadlock, then get logged as missing papers),
  and systematic downloading gets the institution's whole IP range blocked. Batch patterns are in
  [docs/operations.md](docs/operations.md).
- Never wrap the script in an external `timeout` — it has its own watchdog (`PAPERFETCH_TIMEOUT_S`).

## Hard lines — do not cross

- **Never fabricate or reuse another institution's proxy/resolver/remote-auth endpoints.** The
  user supplies their own. This tool ships none.
- **Never bake credential values into code or config.** They live in the local secret store.
- **Never remove or weaken the rate throttle to enable bulk downloading.** `rate.min_interval_s`
  protects the whole institution's shared IP. If the user asks to disable it, tell them what it
  costs their colleagues and confirm they understand.
- **This is not a paywall bypass.** It automates a session the user is already entitled to. If a
  user has no legitimate access to a resource, the correct answer is a resolver link or an
  interlibrary-loan request, not a workaround. Do not add a Sci-Hub route.

## Explaining how it works

The docstrings in `library_session.py` describe the full technique (two-phase headless→headful
Cloudflare handling, the dots→dashes proxy host rewrite, session-cookie persistence, the
`citation_pdf_url` route, and the LWW/Ovid signed-URL + Referer chain). `holdings.py`'s docstring
explains the entitlement model. They are written to teach the method, not just to run.
