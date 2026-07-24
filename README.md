# paper-fetch — a publisher-aware full-text PDF fetcher

![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Publisher routes verified](https://img.shields.io/badge/publisher%20routes-20%2B%20verified-brightgreen)

Give it a DOI; it walks a **route ladder** to get the full-text PDF the cheapest, most
legitimate way first — open access, then official publisher text-mining (TDM) APIs, then
your own institutional library proxy, and finally it just prints your library's resolver
link so you can finish by hand. Every route is one you're already allowed to use; there is
no Sci-Hub fallback here.

It's the **download end** of a small paper pipeline — the piece that was missing longest,
because it's the one that fights publishers:

| Stage | Repo | What it does |
|---|---|---|
| discovery | [paper-radar](https://github.com/drpwchen/paper-radar) | journal/PubMed feeds → interest-scored → private triage |
| **download** | **paper-fetch** (you are here) | **DOI → full-text PDF, via the route ladder** |
| reading | [paper-review-and-digest](https://github.com/drpwchen/paper-review-and-digest) | `/paper-review` appraisal, `/paper-digest` content digest |

```bash
# 30 seconds to first PDF (the OA route needs zero keys — just your email in config.yaml)
git clone https://github.com/drpwchen/paper-fetch && cd paper-fetch
pip install -r requirements.txt && cp config.example.yaml config.yaml   # set unpaywall_email
python paper_fetch.py 10.1186/s12984-023-01168-x out.pdf
```

> **This project ships no institution's access.** You supply your own library's endpoints
> (in `config.yaml`) and your own account (in a local secret store). It automates *your
> own* authenticated session — it is not a paywall bypass and it does not share credentials.
> Most readers will point an AI agent at this repo to understand and adapt the method; the
> code — and [AGENTS.md](AGENTS.md) — are written for exactly that.

## The route ladder (the idea)

The whole design is: **try the cheapest, most-permitted route first, fall through on failure.**

```
DOI
 ├─ 1. Open Access ─────── Unpaywall + Semantic Scholar openAccessPdf → every oa_location,
 │                          PMC→Europe PMC render, landing-page citation_pdf_url (repositories)
 ├─ 2. Publisher TDM API ─ Elsevier / Wiley / Springer official text-mining endpoints
 │                          (you register for your own key; entirely sanctioned)
 ├─ 3. Institutional proxy your library's off-campus remote-auth + EZproxy/NetScaler proxy
 │                          (your own login; for subscriptions you already have)
 └─ 4. Resolver link ────── print your library's SFX/OpenURL link → finish manually
```

Layer 1 works out of the box for anyone. Layers 2–3 are where the work is.

Two rules worth stealing:
- **Validate `%PDF` magic bytes**, never trust `Content-Type`. Paywalls and Cloudflare love
  to return `200 text/html` that *looks* like a PDF response but isn't.
- **Before blaming a route, check whether you're entitled to that article.** A working route
  and a route you have no access through look *identical*. See
  [docs/holdings.md](docs/holdings.md) — this is the single most expensive mistake in this
  problem space.

## Publisher routes, worked out the hard way

The point of this repo. Each publisher exposes a different shape; these are the ones mapped
and verified against live articles.

| Shape | How it works | DOI prefixes |
|---|---|---|
All four shapes live in one dispatch table — `ROUTES` in `library_session.py`, keyed by DOI
prefix with a `kind` of `tpl` / `meta` / `lww` — and as of v1.0 every one of them ships
working code:

| Shape | How it works | DOI prefixes |
|---|---|---|
| **template** (`kind: tpl`) | build the PDF URL from a host + path template | `10.1002`/`10.1111` Wiley · `10.1007`/`10.1186` Springer/BMC · `10.1056` NEJM · `10.1177` Sage · `10.1080` T&F · `10.2214` AJR · `10.1148` Radiology/RSNA · `10.1142` World Scientific |
| **citation-meta** (`kind: meta`) | resolver → article HTML's `<meta name="citation_pdf_url">` → fetch with Referer. Headless. | `10.1001` JAMA · `10.1093` Oxford · `10.1542` Pediatrics · `10.1183` ERJ · `10.3171` J Neurosurg · `10.1038` Nature |
| **citation-meta + headful nav** (`kind: meta, nav: true`) | same, but the resolver runs as a real headful navigation to clear a Cloudflare challenge | `10.1136` BMJ · `10.3174` AJNR · `10.2967` J Nucl Med |
| **signed-URL** (`kind: lww`) | multi-step walk to a signed PDF URL: resolver → scrape article number → viewer HTML → signed `pdfUrl` with the right Referer chain, plus an Ovid-OCE fallback for ahead-of-print articles and concurrent-licence-seat (E3) back-off | `10.1097`/`10.1161`/`10.1213`/`10.2215` LWW/Ovid |

`10.1016` Elsevier goes through the TDM API in `paper_fetch.py`, never the proxy.

**Adding a new publisher? Reach for the `citation_pdf_url` route first.** Many sites with no
DOI→PDF template still advertise the exact PDF URL in a `<meta name="citation_pdf_url">` tag on
the article page (it's what Google Scholar indexes). Resolve the DOI through the proxy, read the
meta, fetch it with the article as `Referer`. No reverse engineering needed.

**And if a route comes back `cf_block`, try headful navigation before declaring it dead.** BMJ was
documented here as a Cloudflare dead end that no stealth browser could clear. That was simply
wrong: the WAF only blocks *headless* requests, and a real headful navigation passes on the first
try — that's the whole `_HEADFUL_META_PREFIXES` variant. (Highwire sites like AJNR/JNM also loop
on the generic `doi-org` resolver → give them an explicit `host` so the route uses that site's own
`/lookup/doi/`.)

## How this compares to other paper fetchers

Plenty of tools turn a DOI into a PDF. They differ on two axes: *where the PDFs come from*
and *what happens when a route fails*.

| Capability | OA-only clients (unpywall, …) | Sci-Hub-based (PyPaperBot, scidownl, …) | Multi-source fetchers | **paper-fetch** |
|---|---|---|---|---|
| Open-access ladder (Unpaywall + S2 + PMC/Europe PMC + `citation_pdf_url`) | partial (usually one index) | — | ✅ | ✅ two independent OA indexes + PMCID direct-conversion |
| Official publisher TDM APIs (your own keys) | — | — | rare | ✅ Elsevier · Wiley · Springer |
| Institutional proxy layer (your own login, persistent session) | — | — | IP/cookie passthrough at best | ✅ full remote-auth walk, session survives reboots |
| Per-publisher route shapes (URL template / `citation_pdf_url` / headful CF nav / multi-step signed-URL) | — | — | generic stealth browser | ✅ 20+ verified, named, documented |
| **Entitlement ground truth** (your library's A–Z holdings → local SQLite, checked *before* blaming a route) | — | — | — | ✅ `holdings.py`, as far as we know unique |
| Route health from access logs (`stats`, per-route success history) | — | — | — | ✅ |
| `%PDF` magic-byte validation | rare | rare | some do | ✅ |
| Agent-native output (`--json` envelope, typed exit codes) | — | — | some do | ✅ |

The two rows nothing else has — entitlement ground truth and log-driven route health — exist
because they answer the question every other tool leaves you guessing on: **"is this route
broken, or do I just not have access to this article?"**

## Install

```bash
python -m patchright install chromium      # only needed for the institutional proxy layer
```

(The clone / `pip install` / `cp config.example.yaml config.yaml` steps are in the quickstart
above.) Then store publisher and library credentials in a local DPAPI secret store (Windows) —
never in `config.yaml`:

```powershell
powershell -File ~/.secrets/secret.ps1 set ELSEVIER_TDM_KEY
powershell -File ~/.secrets/secret.ps1 set WILEY_TDM_TOKEN
powershell -File ~/.secrets/secret.ps1 set LIB_USER   # your own library account
powershell -File ~/.secrets/secret.ps1 set LIB_PASS
```

(Any secret store works — the code shells out to `~/.secrets/secret.ps1 get <NAME>`; swap in
your own if you're not on Windows DPAPI.)

How to register for the publisher TDM APIs (Elsevier / Wiley / Springer / Unpaywall) →
[docs/publisher-tdm-apis.md](docs/publisher-tdm-apis.md).

## Use

```bash
python paper_fetch.py 10.1371/journal.pone.0000000 out.pdf   # OA / TDM — works out of the box
python paper_fetch.py --json 10.1016/xxx out.pdf             # agent mode: JSON envelope on stdout
python holdings.py 10.1097/xxxxx                             # do I even have access to this?
python library_session.py check                              # proxy layer (after config.yaml `auth:` is set)
python library_session.py fetch 10.1002/xxxxx out.pdf
python library_session.py stats                              # rate / block analysis
python library_session.py routes                             # per-route scorecard + holdings gaps
```

`examples/example-note.md` shows the intended Zotero + Obsidian workflow around it.

### Agent mode (`--json`)

`paper_fetch.py --json` prints **exactly one JSON envelope on stdout** (all diagnostics go
to stderr), so an orchestrator can `json.loads` the last line without scraping logs:

```json
{"schema": 1, "doi": "…", "ok": true, "route": "elsevier", "tried": ["elsevier"],
 "bytes": 117204, "sha256": "…", "path": "out.pdf", "elapsed_s": 1.4}
```

On failure `ok` is `false`, `tried` lists every route attempted, and `resolver_url` carries
your library's SFX link for a manual finish. `sha256` lets batch callers dedupe.

### Exit codes (one table for both scripts)

| Code | Meaning | What the caller should do |
|---|---|---|
| `0` | PDF obtained | validate `%PDF`, carry on |
| `1` | usage error | fix the command |
| `2` | no route / auth failed | genuinely unavailable — stop |
| `4` | profile busy (another fetch holds the lock) — `library_session.py` only | **retry serially** — not a missing paper |
| `5` | watchdog abort (`PAPERFETCH_TIMEOUT_S`, default 240 s) — `library_session.py` only | **retry once** — not a missing paper |

Codes `4` and `5` mean "try again, one at a time" — never record them as "no full text."
Conflating them is the single easiest way to wrongly conclude a paper is unobtainable.

### ⚠ Before you batch or parallelize

The proxy layer is **strictly serial with a 15 s courtesy delay** — publishers block an
entire institution's IP range when they detect systematic downloading, and the browser
profile is an exclusive resource (parallel callers deadlock, then wrongly record papers as
missing). Rate-limit reasoning, batch patterns, and how to call this from LLM agents →
[docs/operations.md](docs/operations.md). Never wrap the script in `timeout` — it has its
own watchdog.

### ⚠ When you get blocked (mostly a first-time-setup problem)

The classic first-day loop is: fail → immediately re-run → fail → re-run. **Rapid repeated
login or fetch attempts get you temporarily blocked** — by your library's login gate, by
Cloudflare, or by a publisher — and a temporary block looks exactly like a broken tool or
wrong credentials.

| Signal | Meaning | What to do |
|---|---|---|
| `[login] FAILED after retries` | wrong creds, CAPTCHA misreads, or the gate is rate-limiting you | verify the credentials by logging in **manually in a browser** first; if they work there, you're temporarily blocked → wait 30–60 min |
| `cf_challenge` / `cf_block` | Cloudflare intercepted the request | the tool retries headful once automatically; if it persists, wait — don't hammer |
| Ovid/LWW route stalls or errors | E3 concurrent-licence-seat limit | wait 30 min (`PAPERFETCH_OVID_COOLDOWN_S`); the tool backs off on its own |
| exit `4` / `5` | profile busy / watchdog | retry serially later — **not** a missing paper |

One rule covers all of it: **the same failure twice in a row means stop**, run
`python library_session.py stats` to see what's being blocked, and wait 30–60 minutes.
Retrying in a tight loop only escalates a temporary block — worst case against your whole
institution's shared IP.

## Adapting it to YOUR library

| Layer | This repo | You supply |
|---|---|---|
| OA + publisher TDM APIs (`paper_fetch.py`) | ✅ complete, runnable | your own API keys + email |
| Institutional proxy (`library_session.py`) | ✅ complete as of v1.0 — every route kind including the LWW/Ovid signed-URL flow, plus a generic form login | your library's endpoints + form selectors in `config.yaml` (SSO gates: a custom `login()`) |
| Endpoints (resolver / proxy / remote-auth) | placeholders in `config.example.yaml` | your library's real values |
| Entitlement table (`holdings.py`) | ✅ query side + schema | the table itself, from your library's A–Z list |

Off-campus access comes in four families (EZproxy, OpenAthens/Shibboleth, VPN, custom
portal) and **only `login()` differs between them**. The two form-based families — EZproxy
and Django/NetScaler-style portals — are covered by the generic form login: set
`auth.family: form` and point the selectors in `config.yaml` at your gate's login page
(inspect it once in devtools; numeric-CAPTCHA gates are handled by the built-in offline
OCR). SSO redirect chains (OpenAthens/Shibboleth) don't reduce to one form — set
`auth.family: custom` and implement `login()` for your IdP; everything else works
unchanged. [docs/library-setup.md](docs/library-setup.md) walks you through identifying
your family and finding your endpoints.

Then build your entitlement table → [docs/holdings.md](docs/holdings.md). It's what tells
"this route is broken" apart from "you don't have access to this article" — and those two look
exactly alike from the outside.

**Not a programmer?** That's the intended case. Point an AI coding agent at this repo and ask it
to wire up your library; [AGENTS.md](AGENTS.md) is written for it — deployment steps, the
verification smoke test, and the hard lines it must not cross.

## Red lines

- For people who **already have legitimate subscription access**. It automates your own
  authenticated session; it is not a way around a paywall and not a way to share an account.
- **Your account, your responsibility.** Use your own credentials and follow your library's
  license terms and each publisher's ToS.
- **Do not remove the rate limit to bulk-download.** Publishers respond to systematic
  downloading by blocking the *institution's* whole IP range — your colleagues pay for it.
- Never commit `config.yaml`, `*.dpapi`, or `access_log.jsonl` (the `.gitignore` blocks them).

Full acceptable-use terms, and what is and isn't your responsibility →
**[DISCLAIMER.md](DISCLAIMER.md)**. Short version: no warranty, use your own account, follow
your library's licence and each publisher's ToS, don't redistribute what you download.

MIT licensed. Contributions that add publisher route templates or adapt the proxy layer to
other library systems are welcome. Notable changes: [CHANGELOG.md](CHANGELOG.md).

---

☕ Find this useful? [Buy me a boba](https://drpwchen.bobaboba.me).

---

## 🌱 Start here if you're new to AI agents ／ AI agent 新手起點

This tool is one piece of my personal AI workflow. If you want to learn how to use AI agents like Claude Code from zero (no programming background needed), I wrote a beginner series (in Traditional Chinese):

這個工具是我個人 AI 工作流的一部分。想從零開始學怎麼用 Claude Code 這類 AI agent（不需要程式背景），可以從我的入門系列開始：

1. [從零開始：安裝、看懂 GitHub、跑起你的第一個工具](https://drpwchen.com/posts/getting-started/)
2. [怎麼跟 AI agent 講話：心法、元技能與規則檔](https://drpwchen.com/posts/talking-to-agents/)
3. [自動化流程不是設計出來的，是長出來的](https://drpwchen.com/posts/growing-your-workflow/)

Full map of my tools and posts ／ 所有工具與文章的全貌 → [drpwchen.com/map](https://drpwchen.com/map/)
