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

## Calling it from an orchestrator

- `paper_fetch.py --json <DOI> <out>` prints **exactly one JSON envelope on stdout** (all
  diagnostics go to stderr): `{schema, doi, ok, route, tried[], bytes, sha256, path,
  resolver_url?, elapsed_s}`. Parse that; do not scrape logs.
- **Exit codes** (same table for both scripts): `0` PDF obtained · `1` usage error · `2` a route
  ran and came back empty · `3` **auth failed / session expired** · `4` profile busy · `5`
  watchdog abort · `6` no route for this publisher prefix.
- **Only `2` is evidence about the PAPER.** `3`, `4`, `5` and `6` all mean "fix it and retry" —
  recording any of them as unavailable is the easiest way to wrongly write a paper off.
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
