# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Documentation
- ⚠️ **A publisher probe is only as good as your test article's entitlement.** If you probe a
  publisher with an article your institution doesn't subscribe to, the PDF endpoint hands back
  a reader/interstitial HTML — which looks exactly like a broken route. Two "unsupported
  publisher" verdicts in this project were later traced to unsubscribed samples (and one to a
  bug in the *subscription detector* itself). Confirm entitlement first, then blame the route.
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

[Unreleased]: https://github.com/drpwchen/paper-fetch/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/drpwchen/paper-fetch/releases/tag/v0.1.0
