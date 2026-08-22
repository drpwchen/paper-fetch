# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] — 2026-08-22

Everything here comes from one 147-paper systematic-review retrieval run, which surfaced two
failures no unit test would have caught: **an expired session is indistinguishable from a
missing paper**, and **a whole conference volume is indistinguishable from the article inside
it** — unless something goes and looks.

> ⚠ **Callers: exit code `2` has narrowed.** Auth failure is now `3`, and "no route for this
> prefix" is now `6`. An orchestrator that treats "non-zero = no full text" was already wrong;
> it is now wrong in a way it can fix. `check` returns `3` (not `2`) when the session expired.

### Added
- **`pdf_verify.py` — content-level verification, and rescue of an article from a whole
  volume.** `verify(pdf, title)` answers "is this file that article?"; `--extract` locates the
  article inside a proceedings volume and cuts out its pages, keeping the original as
  `<stem>_volume.pdf`. Acceptance is the longest **contiguous** run of the title in a page's
  normalised text (a bag-of-words score matches a proceedings page whose neighbouring
  abstracts merely share vocabulary), hyphenation across line breaks is folded back, and an
  ambiguous volume yields nothing rather than a guess. Verdicts: `match` · `volume_like` ·
  `title_absent` · `no_text` · `unreadable` · `no_title`. Validated against 11 real volumes:
  the page it picks matched the hand-checked page 11/11 (title 100% contiguous on the winner,
  runner-up 23–45%).
- **`--title "<article title>"` on both `paper_fetch.py` and `library_session.py fetch`** —
  runs that verification on whatever the ladder returns, cutting volumes down to the article
  automatically. `library_session.py` forwards the title to `paper_fetch.py` on layer 1. The
  envelope gains `verify`, `verify_detail`, `volume_path`, `extracted_pages`; `bytes` and
  `sha256` are recomputed after a cut.
- **Typed exit codes `3` (auth) and `6` (no route)**, split out of the old catch-all `2`, plus
  a documented batch discipline: run `check` first, abort the batch when the session is dead.
- **`10.1210` (Endocrine Society / JCEM)** as a `meta` route — the same Silverchair platform as
  OUP `10.1093`; it had been logging `no_route` while those papers were fetched by hand. The
  route comment records the verified Silverchair mechanics (article-pdf URL → watermark token
  URL that needs no cookie; non-navigation fetches are 403; the first navigation can bounce).
- **`pdf_from_landing` option for `meta` routes**, used by `10.1038`: derive the PDF URL from
  the landing URL when the article page carries no `citation_pdf_url` at all.
- `routes` now also lists routeless prefixes whose entitlement is *unknown* (`subscribed=None`).
  The old filter showed only prefixes known to be subscribed — hiding exactly the
  database-level platforms that are not in an A–Z list yet fetch fine.

### Fixed
- **A blocked resolver was reported as `no_pdf_meta`.** A Cloudflare 403 is an HTML page with
  no `citation_pdf_url` in it, so the meta route fell through and logged "this publisher
  advertises no PDF" for what was really "we never reached the article page" — the same wall
  under two names, on consecutive runs of the same DOI. Resolver responses are now classified
  (CF / 4xx / 5xx / login bounce) *before* the meta tag is looked for, and a resolver that
  lands directly on a PDF is accepted instead of discarded.
- `_citation_meta_pdf` no longer touches `r.status` when a headful navigation returned no
  response object, and it uses the landing URL as `Referer`.
- **A login failure inside `fetch` printed "route could not fetch"** — indistinguishable from a
  paper that has no route. It now says in as many words that this is an authentication problem
  and that `login` (which opens a real window) is the fix, and exits `3`.

### Changed
- The bundled skill is now `skills/paper-fetch/` (was `skills/paper-download/`), matching the
  project name. Invoke it as `/paper-fetch`.
- `10.1136` (BMJ) is annotated as **Cloudflare-flaky** rather than "passes first try": with
  `nav` verifiably in effect, the same subscribed DOI logged `cf_block` twice and one 403, two
  months after the route was added. The flag works; the wall moved. `AGENTS.md` gains the
  general form — *a verdict that once held can go stale* — and its corollary: check the **name**
  of a failure before trusting it.
- `README.md`, `AGENTS.md`, `docs/operations.md` and the skill all carry the new exit-code
  table, the check-before-batch rule, and the `--title` rule.

## [1.4.0] — 2026-08-19

ClinicalKey proxy route (`ck`) — closes the gap [#2](https://github.com/drpwchen/paper-fetch/issues/2)
left open: for *in press* Elsevier articles the TDM API serves only a cover sheet (now
rejected by the 1.3.1 content gate), and at libraries whose Elsevier access lives in
ClinicalKey rather than ScienceDirect, the ladder previously ended there.

### Added
- **`ck` route for ClinicalKey** (`10.1016` now dispatches to it after the TDM layer):
  navigate the remote-auth gate to CK's `content/playBy/doi/{doi}`, let the SPA bootstrap,
  then fetch `/service/content/pdf/watermarked/1-s2.0-{PII}.pdf` from the same context.
  The PII comes from CrossRef `alternative-id` — no SPA scraping. Verified end-to-end on
  two in-press APMR RCTs (10 pp/47k chars and 28 pp/55k chars, both passing the content
  gate; the TDM API had served covers for both).
- CK's "Choose organization" modal is handled automatically: options are buttons (not
  radio inputs), picked by new config `clinicalkey.institution_match` (regex; documented
  in `config.example.yaml`). A single option is picked without config; several unmatched
  options fail with the list printed. The modal can reappear on every run — the route
  handles it each time.
- New access-log statuses for the route: `no_pii`, `institution_unmatched`, `gate_reject`,
  `boot_timeout`.

### Fixed
- **"ClinicalKey through a rewriting proxy cannot load" was a headless artifact.** The CK
  SPA bootstraps fine in a *headful* context (same failure class as the Ovid "please
  wait" interstitial); headless hangs on the splash screen forever. The `ck` route is
  headful accordingly. While the SPA boots, the PDF endpoint answers HTTP 902/JSON or
  500 — the route polls (default 120 s, `PAPERFETCH_CK_BOOT_S`) instead of giving up.
- A synthetic click on CK's own download link does not trigger a download; fetching the
  URL from the page's context does. The route never relies on download events.

## [1.3.1] — 2026-08-19

Content-level PDF validation ([#2](https://github.com/drpwchen/paper-fetch/issues/2)).
`is_pdf()` only checked magic bytes, so for *in press* Elsevier DOIs the TDM API's one-page
cover sheet was accepted as full text and the route ladder stopped — worse than a plain
failure, because callers then believe they hold the article. File size does not discriminate
(an 876 KB cover was observed next to a 259 KB one); only page count plus extracted-text
volume does.

### Fixed
- **Every route's PDF now passes a content gate** (PyMuPDF; skipped with a warning if it is
  not installed): multi-page documents pass unconditionally, so scanned/image-only PDFs are
  unaffected. A single-page document is rejected when it carries a **"Journal Pre-proof"**
  or **"ARTICLE IN PRESS"** marker — the two in-press cover variants the Elsevier TDM API
  was observed to serve (the second contains title/authors/abstract and ~4k characters, so
  a bare character threshold misses it) — or when it has fewer than 3000 extracted
  characters. A rejected response no longer ends the run: the ladder keeps walking.
- Rejected-but-valid PDFs are kept at `<out>.partial` (still useful as a metadata source)
  and reported via new envelope fields `partial_path` / `reject_reason`.
- **Failure messages now distinguish three cases** instead of one generic "possibly
  paywalled" line: content rejected (in press — retry later or go institutional), Unpaywall
  `is_oa:true` with every candidate URL dead (`oa_claimed` in the envelope; the full text
  likely exists), and the genuine paywall/Cloudflare case.
- **`library_session.py`: Springer/BMC in-press articles now download.** The canonical
  `/content/pdf/{doi}.pdf` 404s while an article is in press, and `citation_pdf_url`
  still advertises that dead URL — but the landing page's own Download button serves an
  Article-in-Press proof at `{doi}_reference.pdf` (verified: a DOI whose every automatic
  route 404'd delivered a 70-page proof). tpl routes now accept optional `alt_paths`
  tried in order after the primary template; the Springer and BMC entries carry the
  `_reference.pdf` fallback.
- **`library_session.py`: a SweetAlert2 announcement modal on the gate's login page no
  longer wedges the run.** The modal's backdrop intercepts every pointer event, so the
  login submit click retried until the watchdog killed the browser — before anything
  reached the access log. `_login_submit_here` now confirms or removes such overlays
  (`_dismiss_overlays`) before submitting.

## [1.3.0] — 2026-08-15

Chinese-language sources. Everything so far started from a DOI; Taiwanese theses have none,
and Airiti's articles are addressed by the platform's own DocID. Two new entry points, both
verified end to end against live records.

### Added
- **`ndltd.py` — 臺灣博碩士論文知識加值系統 (ndltd.ncl.edu.tw).** `login` / `check` /
  `search` / `fetch`, by title, author or thesis ID, with `--json` envelopes
  (`ndltd.search/1`, `ndltd.fetch/1`). Full texts there are free; the work was in the site's
  machinery, and all four parts of it fail *silently*: a load-shedding captcha gate that can
  replace any navigation (it looks exactly like a session expiry — this is what defeated an
  earlier attempt at the site); a path-borne `ccd=` session token that makes hand-built URLs
  dead on arrival; a login bound to that token unless 保持我的登入狀態 is ticked; and a
  copyright-declaration popup carrying its own captcha, where clicking 我同意 unsolved only
  fires a JS alert. Downloads arrive as a WinZip of per-chapter PDFs, which the module merges
  in order into a single verified PDF (`--keep-zip` also keeps the original container).
- **`airiti.py` — 華藝線上圖書館.** `fetch` by DocID or DOI, plus a browser-rendered `search`
  (Airiti's result list is client-side, so a plain HTTP GET returns an empty shell). Uses the
  institutional proxy and existing session cookies.
- **[docs/chinese-sources.md](docs/chinese-sources.md)** — both platforms, their traps, and
  the entitlement rule below.
- `pymupdf` is now a declared dependency (chapter merge, page-count verification).

### Fixed
- **Airiti articles previously written off as "not subscribed" download fine.** Two bugs
  stacked: the cookie-consent banner cannot be dismissed with a normal click (Playwright sees
  the span as not visible, the exception gets swallowed, and the still-open banner then eats
  every later click with no error and no network request), and the PDF arrives as a **blob
  download** rather than a readable response body, so watching response bodies sees nothing.
  Fixed with an in-page JS dismiss plus download-event capture; an article that had been
  declared unavailable now downloads byte-identical to a manual download.
- Documented the matching entitlement rule: `holdings.py` is built from the A–Z e-journal
  list and Airiti is normally a **database-level** subscription, so a journal's absence from
  holdings says nothing about Airiti access. More generally — **"my automation cannot get it"
  is not evidence about your institution's entitlements.**

## [1.2.0] — 2026-08-08

Runs on macOS and Linux. The institutional-proxy layer was Windows-only in three
load-bearing places, none of which surfaced until the first credential read.

### Added
- **Pluggable secret store** — three backends behind one interface: `dpapi` (Windows DPAPI,
  unchanged), `keychain` (macOS login Keychain, service `paper-fetch`), `env` (plain
  environment variables). Selected by `secrets.backend` in `config.yaml` or the
  `SECRETS_BACKEND` environment variable; the default `auto` resolves per platform, so
  **existing Windows installs behave exactly as before with no config change.**
  Contributed by [@drivysu](https://github.com/drivysu) (#1).
- **POSIX process handling** — `_pid_alive` uses `os.kill(pid, 0)` off Windows, and the
  watchdog's tree-kill falls back to a recursive `pgrep -P` walk instead of `taskkill`.
  Deliberately not `os.killpg`: this process usually shares its process group with the
  interactive shell, so killing the group would take the user's terminal down with it. (#1)
- `psutil` promoted to a real dependency. It was always the preferred teardown path; without
  it an orphaned chromium leaks RAM until reboot.
- Per-platform install matrix in the README, and a `secrets.backend` section in
  [docs/library-setup.md](docs/library-setup.md).

### Changed
- `dpapi_get` / `dpapi_get_opt` / `dpapi_set` are now `secret_get` / `secret_get_opt` /
  `secret_set`; the DPAPI implementations stay as private helpers. A miss names the active
  backend and the command that stores the secret **for that backend**, rather than always
  printing the PowerShell one.

### Fixed
- `_dpapi_set` creates `~/.secrets` when absent (a first-run write used to fail).
- An unrecognized `secrets.backend` now exits at startup. It previously raised only on read
  while `secret_set` silently skipped — so a typo'd name looked like a stored cookie that
  kept disappearing.
- `keychain` on a machine with no `security(1)` raised a bare `FileNotFoundError`,
  indistinguishable from "secret not stored" — it sent you looking for the wrong problem.
  It now names the backend and what it needs.
- A locked macOS Keychain (SSH session, no GUI) no longer aborts a fetch that already
  succeeded: failing to persist a cookie degrades to a warning, the trade-off `env` already
  made.
- The POSIX tree-kill reports a missing `pgrep` on stderr instead of returning silently — a
  silent return reads as a clean teardown while chromium is still running.

### Known limitation
- `paper_fetch.py` still reads publisher TDM keys through the Windows PowerShell store, so
  **the TDM layer stays Windows-only** — `10.1016` (Elsevier) included. Off Windows those
  routes are skipped gracefully (but the printed hint still names PowerShell), leaving the
  open-access ladder and the full institutional-proxy layer. Porting it is next.

### Verification
- Windows regression on 11 Pro (26200) / Python 3.12.1 / psutil 7.2.2, diffing `master`
  against the merged commit: pre-existing DPAPI secrets decrypt to identical hashes,
  set→get round-trips (including non-ASCII), the miss path, `_pid_alive`, and the whole
  single-instance lock behaviour (stale-lock steal, live-lock exit 4) are unchanged; the OA
  ladder still returns a PDF end to end. The contributor verified macOS 15 / Python 3.12
  against a Django-style gate with a numeric CAPTCHA. **The Linux `env` path is reasoned
  about, not field-tested.**

## [1.1.0] — 2026-08-07

### Added
- **`citekey_lint.py`** — proves every citekey in your notes exists in Zotero's Better BibTeX
  library, and prints the authoritative key for that DOI when one doesn't. Works on notes/dirs
  (or `$NOTES_DIR`), bare `--keys` before you write them, and `--cards` JSON payloads. Exits
  0 clean / 1 offenders / **3 unverifiable** — if Zotero isn't running the check cannot pass,
  it can only decline to answer.

### Changed
- **The citekey is copied from Zotero, never reconstructed** (`skills/paper-download/SKILL.md`).
  The previous wording introduced the BBT key *as* the pattern `authorShortTitle2024`, which
  invites rebuilding it from author + title + year. Real BBT keys keep hyphens
  (`guerra-armas…`), case-fold unpredictably, and gain a `…2018a` disambiguator when an author
  publishes twice in a year — so a reconstructed key reads correct in review and points
  nowhere. No key retrievable → write `citekey: ""` and report it. No behavior change to the
  fetch pipeline.

## [1.0.1] — 2026-07-19

Docs only — gaps found watching a real first-time install (a colleague's AI agent
misdiagnosed a temporary rate-limit block as broken credentials and told the user to
apply for library accounts they already had).

### Added
- README **"When you get blocked"** section: block-signal triage table (login-gate
  rate-limit vs `cf_challenge` vs Ovid E3 seat cooldown vs exit `4`/`5`), how long to
  wait, and the stop-after-two-identical-failures rule.
- AGENTS.md: same stop-don't-retry discipline for agents, plus "verify credentials by
  manual browser login — never send the user off to apply for accounts they already have."

## [1.0.0] — 2026-07-14

**No more stubs.** Everything platform-generic that the reference implementation had been
holding back is now in the public edition; what remains yours to supply is genuinely yours
(your library's endpoints, your account, and — for SSO gates only — your `login()`).

### Added
- **The full LWW/Ovid signed-URL flow** (`_lww_ovid_pdf`), previously a documented stub:
  proxy DOI resolver → scrape the article number → `downloadpdf.aspx` viewer → signed
  `pdfUrl` fetched with the exact Referer chain (viewer, not article — an article Referer
  gets HTTP 503 and is what makes this route look dead), with retry while the proxy warms
  the PDF backend.
- **Ovid OCE fallback** (`_ovid_oce_pdf`) for ahead-of-print articles whose LWW viewer
  carries no signed `pdfUrl` — the PDF still exists on Ovid's other platform. Includes the
  pdf.js-viewer trap (match on `content-type: application/pdf`, never on the URL: the
  viewer's own URL contains the literal string `application-pdf`, and the naive regex saves
  88 KB of viewer HTML as a "PDF").
- **Ovid concurrent-licence-seat (E3) discipline**: "License Service Failure (Code: E3)"
  means a *seat* is occupied, not a rate limit — and it is raised above the proxy, so the
  response classifier never sees it. One attempt per fetch, `about:blank` immediately after
  to release the seat, a persisted cooldown (`PAPERFETCH_OVID_COOLDOWN_S`, default 30 min),
  and a dedicated `license_seat_e3` status so `stats` finally shows it.
- **Classic-Ovid platform branch**: an LWW licence can live only on the OCE/journals-lww
  platforms while the same subscribed article shows `/abstract/` on classic `www-ovid` —
  neither proves "not subscribed". The branch resolves it via the SFX detailed-XML API
  (`&sfx.response_type=multi_obj_detailed_xml`, a standard ExLibris feature): the LWW
  `getFullTxt` target 302s to the licensed OCE article, article number included, without
  costing an Ovid seat (`_sfx_lww_target`).
- **Generic form login — `login()` is no longer a stub.** `auth:` section in `config.yaml`
  (family / login path / field selectors / optional numeric-CAPTCHA keys / cookies to
  persist). `auth.family: form` covers the two most common gate families out of the box —
  EZproxy and Django/NetScaler-style portals, with offline CAPTCHA OCR (ddddocr) when
  configured; `auth.family: custom` remains for SSO redirect chains (OpenAthens/Shibboleth).
- **Per-subdomain proxy-handshake login** (`_login_submit_here`): on proxies that authorize
  each publisher subdomain separately, the login form must be submitted **on the bounce page
  itself** (its `?next=` chain completes the handshake) — a re-login at the gate's own page
  is a silent no-op while the session is still valid, and the subdomain stays unauthorized
  forever. Wired into the meta, LWW, and Ovid routes.
- **Unified `ROUTES` dispatch table** — one dict keyed by DOI prefix with
  `kind: tpl | meta | lww`, replacing `PROVIDER_ROUTES` + three parallel prefix sets.
  Adding a publisher is now one line. CJASN (`10.2215`, moved to LWW) added.
- **`routes` CLI command**: per-prefix scorecard from the access log, with
  subscribed/covered printed next to every failure — the dividing line between "route
  broken" and "article never entitled" — plus a holdings-gap list (subscribed articles
  hitting a prefix with no route).
- **Entitlement pre-check wired into `run_fetch`** (`holdings.py`, shipped in 0.5.0, now
  actually consulted before the proxy layer): warns when the article's year falls outside
  coverage ("the proxy will likely return reader HTML — NOT a broken route"), stamps
  `subscribed`/`covered`/`journal` onto every proxy log record, and honors
  `PAPERFETCH_SKIP_UNSUB=1` to skip the proxy for unentitled articles.
- **Route results carry a REASON** (`"pdf" | "auth" | "fail"`) instead of a bool: only an
  `"auth"` failure triggers the one fresh-login retry — re-running a `"fail"` is useless and
  on LWW costs a second Ovid seat.
- **Chromium launch self-heal**: headful launches can intermittently hang for minutes; a
  launch watchdog (`PAPERFETCH_LAUNCH_TIMEOUT_S`, default 90 s) kills the half-started
  chromium children (never the driver) and retries once. Plus `[t+…s]` progress marks on
  stderr so a hang pins down WHICH call blocked.
- Exception classification (`_classify_exc`): `redirect_loop` (the Highwire doi-org
  resolver signature — fixable by switching to a host resolver) and `timeout` are now
  distinguishable from generic `request_error` in the access log.
- `_classify` recognizes an unregistered proxy subdomain ("Host does not match" and
  friends) as `proxy_host_unregistered` — a library-side misconfiguration to report, not a
  route bug to debug.

### Changed
- **Throttle is now per-paper, not per-request**: the courtesy gap applies between papers;
  later steps of the same multi-step chain (resolver → viewer → PDF) use a small jitter.
  The full gap at every step added 30–45 s per paper for nothing.
- `paper_fetch.py` diagnostics are surfaced (last lines of stdout) when the OA/TDM layer
  comes back empty, instead of being swallowed.
- README / AGENTS.md / docs/library-setup.md rewritten around "complete, config-driven"
  instead of "architecture + stubs"; library-setup gains the `auth:` presets per family and
  a `persist_cookies` how-to.

### Migration
- `PROVIDER_ROUTES` / `_CITATION_META_PREFIXES` / `_HEADFUL_META_PREFIXES` / `_LWW_PREFIXES`
  no longer exist — custom entries move into `ROUTES` (`{"kind": "tpl", "host": …,
  "path": …}` / `{"kind": "meta", "nav": …, "host": …}` / `{"kind": "lww"}`).
- If you had implemented `login()` yourself, either translate it into `auth:` selectors
  (form gates) or keep your code under `auth.family: custom`.

## [0.5.2] — 2026-07-14

### Changed
- Pipeline cross-link updated: the reading-end repo `claude-paper-tools` was renamed to
  **`paper-review-and-digest`**. Docs only — no code change. The old GitHub URL redirects.

## [0.5.1] — 2026-07-14

### Added
- **`DISCLAIMER.md`** — acceptable use spelled out for people who won't read the Red lines:
  no warranty (MIT), use your own account, follow your library's licence and each publisher's
  ToS (systematic/bulk download is prohibited **even for legitimate subscribers**), don't
  remove the rate limit (it protects the institution's shared IP, not the publisher), respect
  the TDM API terms you signed for, don't redistribute retrieved PDFs, no affiliation with any
  publisher or institution. Linked from the README's Red lines.

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

[Unreleased]: https://github.com/drpwchen/paper-fetch/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/drpwchen/paper-fetch/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/drpwchen/paper-fetch/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/drpwchen/paper-fetch/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/drpwchen/paper-fetch/compare/v0.5.2...v1.0.0
[0.5.2]: https://github.com/drpwchen/paper-fetch/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/drpwchen/paper-fetch/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/drpwchen/paper-fetch/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/drpwchen/paper-fetch/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/drpwchen/paper-fetch/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/drpwchen/paper-fetch/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/drpwchen/paper-fetch/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/drpwchen/paper-fetch/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/drpwchen/paper-fetch/releases/tag/v0.1.0
