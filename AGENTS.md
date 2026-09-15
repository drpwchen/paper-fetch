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
   the publisher's bot wall, not your config — and the PDF arrives from Europe PMC.)
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

Second corollary: **a "dead end" verdict can be wrong.** BMJ was documented here as a Cloudflare
dead end; in fact the WAF only blocks *headless* requests, and a real headful navigation passes
first try. If a citation-meta route returns `cf_block`, try `nav=True` before concluding anything.

Third corollary, the mirror image: **a verdict that once held can go stale.** BMJ's headful `nav`
route passed on the first try when it was added; two months later the same DOI logged `cf_block`
twice and a 403 with `nav` still in effect. Read that as "Cloudflare, wait or finish by hand",
not as "the flag is broken" or "we lost the subscription" — `holdings.py` still says subscribed.
And check the *name* of the failure before you trust it: that 403 was logged as `no_pdf_meta`
(a 403 page has no `citation_pdf_url` in it either), which is the same wall wearing a different
label. Resolver responses are now classified before the meta tag is looked for.

Fifth (2026-09-06): **"PubMed says free, Unpaywall says closed, publisher is Cloudflare-403,
prefix has no proxy route" is the direct route's job, not a dead end.** `library_session.py
fetch` runs it on its own when layer 1 reports `free_at_publisher` or `blocked_by`; `--direct`
forces it. It opens the publisher page in a real browser with NO proxy and fetches the PDF
from inside the page. If it logs `publisher_refused`, the publisher is declining PDFs for this
network while serving the HTML — a person in their own browser gets the same — so do not
"fix" the route; wait, or read the HTML.

Fourth: **"no `citation_pdf_url`" is not "no route".** Some entitled articles simply don't carry
the tag (Nature news/comment items). A `meta` route can take `pdf_from_landing: "{landing}.pdf"`
to derive the PDF URL from the article URL instead of giving up.

Fifth: **a headful SPA route that "times out" may be waiting on a modal you can no longer see.**
The `ck` route logged `boot_timeout` with the PDF endpoint at HTTP 500 on every poll — reads
like "ClinicalKey is down". The SPA had simply redrawn its organization-choice modal (buttons →
radio inputs) and the selector matched nothing; the user's own browser sailed through because
it carried a remember-me token. When an automation profile fails where a human's browser
succeeds on the *same URL*, diff the two — response bodies of the auth call, then the DOM —
before blaming the publisher.

Sixth (2026-09-08): **a wall that answers the automation profile is not a wall that answers
the library.** Springer through the proxy, article the library holds: `request.get` on the
PDF URL → "Client Challenge" (Fastly) twice; a headful navigation to the article page → the
same challenge, whose `script.js` answers HTTP 400 — for patchright's Chromium *and* for the
real Google Chrome binary launched under CDP. The user's own logged-in Chrome, same proxy, same
URL, passed on the first try and the in-page `fetch()` returned the 1.6 MB PDF. So: the tpl
route now retries once as a real navigation (it covers walls that only block `request.get`),
a wall that survives that retry is exit `7`, never `2`, and the finish for that case is the
user's own browser, not another automation trick. Also: **a DOI prefix names who minted the
DOI, not who serves it today** — journals move publishers (Respiratory Care `10.4187` → Sage,
Liebert `10.1089` → Sage) and their back catalogue moves with them. `fetch` resolves `doi.org`
once for an unknown prefix and matches the landing host against the routes before saying
`no_route`.

## Calling it from an orchestrator

- `paper_fetch.py --json <DOI> <out>` prints **exactly one JSON envelope on stdout** (all
  diagnostics go to stderr): `{schema, doi, ok, route, tried[], bytes, sha256, path,
  resolver_url?, elapsed_s}`. Parse that; do not scrape logs.
- **Exit codes** (same table for both scripts): `0` PDF obtained · `1` usage error · `2` a route
  ran and came back empty · `3` **auth failed / session expired** · `4` profile busy · `5`
  watchdog abort · `6` no route for this publisher prefix · `7` **blocked** — every attempt
  (headless request, then the headful-navigation retry) ended at a bot wall.
- **Only `2` is evidence about the PAPER.** `3`, `4`, `5`, `6` and `7` all mean "fix it and
  retry" — recording any of them as unavailable is the easiest way to wrongly write a paper
  off. `7` in particular used to be `2`: Springer's JS challenge on a *subscribed* article
  was being logged as "route ran, came back empty".
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

- **Two copies.** This public repo is the source of truth for code and docs. A **live** copy runs as
  the author's local Claude skill (`~/.claude/skills/paper-fetch/`, real `config.yaml`, real
  `holdings.sqlite`). Every fix lands here first, then is synced to live — back live up to
  `Documents/Projects/_holding/paper-fetch-live-pre-<ver>-<date>/` before overwriting. Files that
  exist only in live and must never be copied here: `webbridge_fallback.py` /
  `ck_webbridge_fallback.py` (they drive the author's own browser bridge) and `scrape_holdings.py`
  (the public repo ships the holdings *schema*, not a scraper — see docs/holdings.md). The former
  private institution-specific fork is retired (2026-08-08); do not revive it.
- `config.yaml` in this checkout is a **test config with fake `example.edu` endpoints** (gitignored),
  kept only so regression runs work. If a route "fails" here, check which config you are on first.
- **Release ritual**: CHANGELOG entry → commit explicit paths (never `git add -A`) → `git tag vX.Y.Z`
  → GitHub release → reply to and close the issues it fixes → sync live. Docs-only changes (README,
  this file, docs/) ride the next release tag; they are not tagged on their own.
- **Reviewing an external PR**: run a diff harness on master vs the PR branch — secret-store
  fingerprint (sha256 of an existing secret), set/get round-trip, miss message, `_pid_alive`,
  single-instance lock (stale takeover / live → exit 4), OA ladder end to end. Output identical
  except renamed APIs and wording = no Windows regression. The platform the contributor does not
  have is the half we must test ourselves. Rebase-merge to keep the author.
- Markdown here renders on GitHub: use **bold**, never `==mark==`.
- Institution-specific facts (subscribed platforms, broken proxy subdomains that need a library
  ticket, the ClinicalKey institution-modal value) are deliberately **not** here — they live in the
  author's private notes and the live `config.yaml`.
