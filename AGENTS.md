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
   (Expected log since 2026-09: the Springer direct link comes back `js_challenge` — that is
   the publisher's bot wall, not your config. Since 2026-10 PMC and Europe PMC wall scripts
   too (`pmc_challenge`), so the PDF arrives from another OA copy — the Research Square
   preprint, which `--title` accepts at 65 %. Since 1.9.0 that is reported as what it is:
   exit `0`, saved as `out.preprint.pdf`, envelope `version: "preprint"`, and the walled PMC /
   publisher URLs listed in `vor_blocked`. That output is the expected smoke result, not a bug.)
5. Only then the proxy layer. As of v1.0 all route code (including the full LWW/Ovid flow)
   ships working — what remains is authentication config: set `auth.family: form` and point
   the selectors in `config.yaml` at the gate's login form (inspect it in devtools; EZproxy
   preset in docs/library-setup.md), plus `auth.persist_cookies`. Only SSO gates
   (OpenAthens/Shibboleth) need a custom `login()` implementation. Then:
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

Each of these cost real time once; the full account of each (what the log looked like, what
fixed it) is in [docs/route-lessons.md](docs/route-lessons.md):

- **A "dead end" verdict can be wrong.** BMJ's `cf_block` was only the WAF refusing *headless*
  requests — try `nav=True` (headful navigation) before concluding anything.
- **A verdict that once held can go stale.** A `cf_block` / 403 on a route that used to pass
  means "Cloudflare, wait or finish by hand", not "the flag is broken" or "we lost the
  subscription". Check the failure's *name* too: a 403 was once logged as `no_pdf_meta`.
- **Free at the publisher but walled everywhere else is the direct route's job**
  (`library_session.py fetch --direct`: no proxy, real browser). `publisher_refused` = the
  publisher declines this network; wait or read the HTML, do not "fix" the route.
- **No `citation_pdf_url` is not "no route"**: a `meta` route can take `pdf_from_landing`.
- **A headful SPA "timeout" may be a modal you can no longer see** (`ck` `boot_timeout`). When a
  human's browser succeeds on the same URL, diff the two — auth response bodies, then the DOM.
- **A wall that answers the automation profile is not a wall that answers the library**
  (Springer "Client Challenge"): exit `7`, never `2`; the finish is the user's own browser.
  And **a DOI prefix names who minted the DOI, not who serves it** — `fetch` matches the
  landing host against the routes before saying `no_route`.
- **A small HTML page from PMC / Europe PMC is a wall, not "no PDF"** (`pmc_challenge`, exit
  `7`); the one honest "no PDF" is Europe PMC's `hasPDF=N`, which is checked first.

## Calling it from an orchestrator

- `paper_fetch.py --json <DOI> <out>` prints **exactly one JSON envelope on stdout** (all
  diagnostics go to stderr): `{schema, doi, ok, route, tried[], bytes, sha256, path,
  resolver_url?, version?, elapsed_s}`. Parse that; do not scrape logs.
- **Exit codes** (same table for both scripts): `0` PDF obtained · `1` usage error · `2` a route
  ran and came back empty · `3` **auth failed / session expired** · `4` profile busy · `5`
  watchdog abort · `6` no route for this publisher prefix · `7` **blocked** — every attempt
  (headless request, then the headful-navigation retry) ended at a bot wall. `paper_fetch.py`
  uses `0`/`1`/`2`/`7` (since 1.8.0 `7` when only bot walls answered, envelope `blocked: true`).
- **Only `2` is evidence about the PAPER.** `3`, `4`, `5`, `6` and `7` all mean "fix it and
  retry" — recording any of them as unavailable is the easiest way to wrongly write a paper
  off. `7` in particular used to be `2`: Springer's JS challenge on a *subscribed* article
  was being logged as "route ran, came back empty".
- **Exit `0` does not mean "version of record".** Read `version` (since 1.9.0): `preprint` /
  `accepted` are saved as `<stem>.preprint.pdf` / `<stem>.accepted.pdf`, not under the requested
  name, with `vor_url`, `vor_blocked` (walled VoR candidates — hand them to the user to open in
  a browser) and `zotero_attachment_title`. `null` = nothing said which version it is. A
  systematic review must not file a preprint as the published article. `library_session.py
  fetch` (1.10.0) carries on after a layer-1 preprint to the direct and proxy routes; the
  preprint is replaced (and deleted) only by a verified VoR, otherwise kept, still exit `0`.
- **With `--title`, exit `0` also means "verified to be this article".** A PDF whose first
  pages do not carry the title is moved to `<stem>.rejected.pdf` and the ladder continues; if
  nothing better turns up the run is non-zero and the envelope says `rejected_path`. Do not
  key success on "a file exists under the requested name" — that is exactly how an OSTI
  preprint by a different author got filed as an Elsevier article.
- **Run `check` before a batch, and abort the whole batch if the session is invalid.** A dead
  session produces one failure per paper, and every one of them reads like "this paper has no
  route": nine papers were written off as paywalled that way while `3` still shared code `2` —
  a single (fully automatic) `login` fixed all nine. ==A "no full text" conclusion is only
  valid if the session was valid at the time.==
- **Pass `--title "<article title>"` whenever you have it.** A `%PDF` check cannot tell the
  article from the whole supplement it sits in: conference abstracts carry the *supplement's*
  DOI, so every route honestly returns the entire proceedings volume — 20% of one 49-paper
  batch, including a 563-page file logged `ok`. With a title, `pdf_verify.py` confirms the
  article is in there and cuts it out of the volume (the volume is kept alongside). Handing a
  whole volume to a downstream reader is worse than handing it nothing: it produces a
  confident answer about the wrong study.
- **`reddit_draft` in a failed envelope is a draft, never a post.** Posting to r/Scholar is the
  user's own act — show them the draft; do not post on their behalf unless they explicitly ask
  for that specific post. `reddit_request.py draft` exits `8` for an OA article: fetch it in a
  real browser (`library_session.py fetch --direct`), do not ask strangers for it. Never pass
  PDFs obtained through the library or a TDM key on to other people — that breaks the licence
  and gets the institution's IP range blocked. `poll` exit `7` = Reddit rate limit: wait.
- The proxy layer is **strictly serial** with a courtesy delay. Never parallelise it: the browser
  profile is an exclusive resource (parallel callers deadlock, then get logged as missing papers),
  and systematic downloading gets the institution's whole IP range blocked. Batch patterns are in
  [docs/operations.md](docs/operations.md).
- Never wrap the script in an external `timeout` — it has its own watchdog (`PAPERFETCH_TIMEOUT_S`).
- **The first `library_session.py` call of a session may hit the 240 s watchdog** (browser cold
  start) and exit `5`. Rerun once before reading anything into it — it says nothing about the paper.
- **The same failure twice in a row means stop, not retry.** Rapid repeated attempts turn a
  temporary block (login gate, Cloudflare, Ovid E3 seat limit) into a longer one — worst case
  against the institution's shared IP. Tell the user to wait 30–60 minutes; triage table in
  the README's "When you get blocked" section. If credentials are suspect, have the user
  verify them by logging in manually in a browser — do not tell them to apply for new
  accounts or permissions they already have.

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

## Maintainer notes (the author's own deployment — other users can ignore this section)

- **Two copies.** This public repo is the source of truth; a **live** copy runs as the author's
  local Claude skill (`~/.claude/skills/paper-fetch/`). Every fix lands here first, then is
  synced to live, backing live up before overwriting. A few live-only files must never be
  copied here; the list and the backup path are in [docs/maintaining.md](docs/maintaining.md).
  The former private institution-specific fork is retired; do not revive it.
- `config.yaml` in this checkout is a **test config with fake `example.edu` endpoints** (gitignored),
  kept only so regression runs work. If a route "fails" here, check which config you are on first.
- **Release ritual**: CHANGELOG entry → commit explicit paths (never `git add -A`) → `git tag vX.Y.Z`
  → GitHub release → reply to and close the issues it fixes → sync live. Docs-only changes (README,
  this file, docs/) ride the next release tag; they are not tagged on their own.
- **Reviewing an external PR**: run a diff harness on master vs the PR branch (secret store,
  lock, OA ladder); identical output = no Windows regression. Rebase-merge to keep the author.
  Checklist: [docs/maintaining.md](docs/maintaining.md).
- Markdown here renders on GitHub: use **bold**, never `==mark==`.
- Institution-specific facts (subscribed platforms, broken proxy subdomains that need a library
  ticket, the ClinicalKey institution-modal value) are deliberately **not** here — they live in the
  author's private notes and the live `config.yaml`.
