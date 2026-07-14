# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] — 2026-07-14

### Added
- **`holdings.py` — the entitlement layer, now actually shipped.** DOI → ISSN + year
  (CrossRef) → your library's holdings table → `subscribed` / `covered` / platform. This is
  what tells "the route is broken" apart from "you have no access to this article", the two
  being indistinguishable from the outside. Query side + schema are here; the table itself is
  yours to build (`docs/holdings.md`) — every library's A–Z e-journal page is different HTML,
  so no scraper is shipped. Previously the README claimed this capability without the code.
  New optional config key `holdings_db`.
- **`docs/holdings.md`**: why per-article link-resolver queries are an unreliable entitlement
  oracle, the six-column table schema, the multi-segment `coverage` parsing trap, and the
  "not in the table ≠ no access" caveat.
- **`AGENTS.md` rewritten for the agents that actually deploy this**: a verification smoke test
  in the happy path (OA DOI must yield a PDF before touching the library layer); a
  "check entitlement before you 'fix' a route" section; the orchestrator contract (`--json`
  envelope, exit codes `4`/`5` mean *retry*, never "no full text"; strictly serial; no external
  `timeout`); links to the four-family library setup guide.

### Changed
- **README reordered**: the three-repo pipeline table moved to the top; the verified
  publisher-route table promoted above Install (it is the substance of the repo); the
  entitlement trap demoted to a two-line pointer into `docs/holdings.md`; duplicate
  clone/install block removed; adaptation guidance consolidated into one "Adapting it to YOUR
  library" section after Use.

### Fixed
- `AGENTS.md` pointed at a README section that had moved to `docs/library-setup.md` in 0.4.1.

## [0.4.1] — 2026-07-14

### Changed
- **README restructured**: features first (route ladder → comparison → entitlement trap →
  verified routes), install/usage below; three reference sections moved to `docs/`
  (`publisher-tdm-apis.md`, `library-setup.md`, `operations.md`). Sci-Hub positioning toned
  down to a single factual sentence; dedicated badge and manifesto paragraph removed.

## [0.4.0] — 2026-07-14

### Added
- **Agent mode: `paper_fetch.py --json`.** Prints exactly one JSON envelope on stdout
  (`{schema, doi, ok, route, tried[], bytes, sha256, path, resolver_url?, elapsed_s}`);
  all route diagnostics are rerouted to stderr. Built for LLM-agent / batch orchestration:
  parse stdout, branch on typed exit codes, dedupe on `sha256`.
- README: capability-comparison table vs. other fetcher families (OA-only clients,
  Sci-Hub-based, multi-source-with-piracy-fallback), an explicit statement that Sci-Hub is
  absent from this architecture by design, a 30-second quickstart with a verified OA DOI,
  badges, and a four-family guide (EZproxy / OpenAthens–Shibboleth / VPN / custom portal)
  to identifying your library's off-campus setup and what `login()` work each implies.

### Changed
- **`paper_fetch.py` exit codes now match `library_session.py`'s table**: `0` PDF obtained ·
  `1` usage error · `2` all automatic routes exhausted. Previously usage errors exited `1`
  via a bare `sys.exit(msg)` and route exhaustion exited `2` — the numbers are unchanged in
  effect, but they are now an explicit, documented contract shared by both scripts.

## [0.3.1] — 2026-07-14

### Fixed
- **Elsevier TDM: dropped the `view=FULL` query parameter.** With `Accept: application/pdf`
  it is unnecessary, and Elsevier rejects it with `HTTP 400 INVALID_INPUT ("View parameter
  specified in request is not valid")` for a subset of articles — observed on several
  *Archives of PM&R* DOIs across publication years, while the identical request without the
  parameter returns the PDF. The 400 masqueraded as a coverage gap for an entire journal.
  Lesson: on a 400 from a TDM API, read the error body — the status code alone misleads.

## [0.3.0] — 2026-07-14

### Added
- **Semantic Scholar `openAccessPdf` fallback** in the OA layer (`paper_fetch.py`). It's an
  OA index independent of Unpaywall — it catches preprint-server and some hybrid-OA PDFs that
  Unpaywall misses. No API key required (429 is silently skipped).
- **Headful-navigation variant of the citation-meta route** (`_HEADFUL_META_PREFIXES`,
  `_citation_meta_pdf(nav=True, host=…)`). Some publishers front the resolver with a
  Cloudflare challenge that blocks headless requests; a real headful navigation clears it.
  Highwire sites additionally need an explicit resolver `host` (their generic `doi-org`
  resolver loops). Enables BMJ (`10.1136`), AJNR (`10.3174`), J Nucl Med (`10.2967`).
- More verified template/meta publishers: AJR (`10.2214`), Radiology/RSNA (`10.1148`),
  World Scientific (`10.1142`), Pediatrics (`10.1542`), European Respiratory J (`10.1183`),
  J Neurosurg (`10.3171`), Nature (`10.1038`).

### Changed
- **Corrected the BMJ verdict.** 0.2.0 documented BMJ as a Cloudflare "WAF dead end" clearable
  neither headless nor headful. That was a headless-only artifact: a headful navigation passes.
  BMJ is now a working route, and the README reframes the WAF section as a cautionary tale.
- `_classify` recognises the CF WAF interstitial ("Attention Required") and an unregistered
  proxy subdomain ("Host does not match" / "Oh noes!") as distinct terminal states.

### Notes
- Documented genuine dead ends (no route to add): JOSPT — no online entitlement; Thieme / JCO /
  Liebert — a library-side proxy misconfiguration (unregistered subdomain), not a route bug.

## [0.2.0] — 2026-07-14

### Added
- **Generic `citation_pdf_url` route** (`_citation_meta_pdf`) — a whole class of publishers
  with no DOI→PDF template still advertise the exact PDF URL in a `<meta name="citation_pdf_url">`
  tag on the article page (it's what Google Scholar indexes). Resolve the DOI through the
  proxy, read the meta, fetch it with the article as `Referer`. No reverse-engineering, fully
  headless. Verified on JAMA Network (`10.1001`); opt a publisher in via `_CITATION_META_PREFIXES`.
- **DOI→PMCID OA fallback** — asks NCBI's idconv for a PMCID and, if there is one, fetches the
  Europe PMC `?pdf=render` endpoint. Catches NIH author manuscripts that live in PMC but that
  Unpaywall either under-indexes or only points at a landing page.

### Changed
- NEJM (`10.1056`) template verified end-to-end (was shipped as an unverified guess).
- **Sage (`10.1177`), Taylor & Francis (`10.1080`), Oxford (`10.1093`) all verified working.**
  Sage and T&F use the plain path template; Oxford goes through the `citation_pdf_url` route.
  Every one of these had previously been written off as "returns HTML, needs reverse
  engineering" — see below.
- **BMJ (`10.1136`) removed from the route map.** Its proxied subdomain sits behind a
  Cloudflare **WAF block** ("Attention Required!"), which a stealth browser does not clear
  headless *or* headful. That is a real dead end, unlike the three above.

### Documentation
- ⚠️ **A publisher probe is only as good as your test article's entitlement — this is the
  single biggest trap in this whole problem space.** If the article isn't covered by your
  institution's subscription, the publisher's PDF endpoint returns reader/interstitial HTML
  or a 403 — *indistinguishable from a broken route*. **Three publishers in this project
  (Sage, Taylor & Francis, Oxford) were each declared "unsupported, needs reverse
  engineering". All three worked the moment they were retested with an article the library
  actually holds.** Two further wrinkles that make this hard to see:
  - Coverage is per-journal AND per-year: a library may hold a journal for *one 1990s issue*,
    or exclude ahead-of-print. "Subscribed" is not enough — check the article's year.
  - Link resolvers (SFX/360) are unreliable as an entitlement oracle: the same DOI returned a
    full-text target on one call and none minutes later. Your library's **A–Z e-journal list**
    (journal + platform + coverage years) is the stable source of truth; scrape it once.
- Clarified a trap worth knowing before you trust any "no full text" verdict: **one route
  failing does not mean the PDF doesn't exist.** A publisher's own platforms disagree —
  e.g. an ahead-of-print article can be missing from the journal site's PDF viewer while the
  aggregator (Ovid) serves it fine.
- ⚠️ Some aggregators enforce **concurrent-licence seats**, not just rate limits. Hitting one
  can return a licence-service error after a mere handful of requests, and that failure
  happens *above* the proxy layer — so it never appears in `access_log.jsonl` or `stats`.
  Tripping it degrades access for **everyone at your institution**. Don't automate through an
  aggregator to grind a batch; fetch the odd stubborn paper by hand.

## [0.1.0] — 2026-07-10

First public release. `paper_fetch.py` (OA / publisher TDM route ladder) is complete and
works out of the box. `library_session.py` ships the institutional-proxy **architecture**
with `login()` and `_lww_ovid_pdf()` left as documented stubs — the two pieces that are
inherently specific to your own library and must be implemented against it.

### Added
- **`paper_fetch.py`** — DOI in, PDF out. Routes by DOI prefix: Elsevier / Wiley / Springer
  TDM APIs, falling back to Unpaywall, then to your institutional link resolver.
- **`library_session.py`** — reference implementation of off-campus institutional fetching:
  remote-auth session persistence, proxy host-rewrite, `patchright` (stealth Playwright) to
  clear Cloudflare headlessly, a publisher route map (`PROVIDER_ROUTES`), an access log, and
  `stats` for learning your real rate ceiling empirically.
- **Cross-process lock** (`profile_lock`) — the chromium profile is exclusive, so concurrent
  `fetch`/`login`/`check` runs now queue instead of racing. A caller that waits past
  `PAPERFETCH_LOCK_WAIT_S` exits **4** with an actionable message. Stale locks (dead pid, or
  older than 30 min) are stolen automatically.
- **Watchdog** (`PAPERFETCH_TIMEOUT_S`, default 240 s) — bounds every run and exits **5**
  rather than hanging. It **tree-kills its own chromium**, so an aborted run never orphans a
  browser. (Wrapping the script in a bare `timeout` does *not* do this — it kills the parent
  and leaks the child. Don't.)
- **Documented exit codes**: `0` ok · `1` usage · `2` no route / auth failed · `4` profile
  busy · `5` watchdog abort. **`4` and `5` mean "retry serially", not "no full text".**
- **Rate-limit guardrails**: 15 s courtesy delay by default; setting `min_interval_s: 0`
  prints a warning explaining that publishers block the *entire institution's* IP range.

### Notes for agent / batch callers
- This tool is **serial by design**. Fanning `fetch` out across parallel workers deadlocks
  them on the shared browser profile; each then retries, burning time (and tokens, if they
  are LLM agents). Fetch in a serial pre-pass, then hand the PDF paths to your workers.
- `login` runs **headful** on purpose: the proxy's JS-redirect interstitial never completes
  in a headless browser. `check` stays headless.
- Unpaywall reporting `is_oa: true` does **not** guarantee a PDF exists — hybrid and
  ahead-of-print articles routinely report OA while offering no `url_for_pdf`. Fall through
  to the institutional route instead of concluding the paper is unavailable.

[Unreleased]: https://github.com/drpwchen/paper-fetch/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/drpwchen/paper-fetch/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/drpwchen/paper-fetch/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/drpwchen/paper-fetch/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/drpwchen/paper-fetch/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/drpwchen/paper-fetch/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/drpwchen/paper-fetch/releases/tag/v0.1.0
