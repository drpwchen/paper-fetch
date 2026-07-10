---
name: paper-download
description: >
  Add academic papers to Zotero via MCP (DOI/PMID/title); fetch full-text PDFs through
  a publisher-aware route ladder (Unpaywall → Elsevier/Wiley/Springer TDM → institutional
  proxy → SFX); PDFs land in a linked-files folder (ZotMoov), never uploaded to Zotero cloud.
  Trigger: "下載論文", "抓全文", "download paper", "get full text", "加進 Zotero",
  or when /paper-review needs a paper added to the library.
---

# Paper Download — 論文入庫

## Overview

Two separable jobs:

1. **Metadata → Zotero**: `add_by_doi` (MCP) fetches CrossRef/PubMed metadata + Better
   BibTeX citekey. (In local mode this is **metadata-only** — it does NOT attach a PDF.)
2. **PDF → linked file (not uploaded)**: fetch the PDF via a publisher-aware route ladder,
   then let it become a Zotero attachment. **ZotMoov** auto-moves it to your linked-files
   folder and converts it to a **linked file** → it never consumes Zotero cloud storage.

## 「不上傳」保證（two locks）

- **ZotMoov** (`enable_automove=true`, `file_behavior=move`, `dst_dir=<your linked-files
  folder>`, `subdirectory_string={%c}/{%y} {%a} - {%t}`): any attachment added to an item is
  moved to your folder within ~5s and turned into a linked file. Linked files are never uploaded.
- **Zotero file sync OFF for My Library** (Settings → Sync → uncheck *Sync attachment files
  in My Library*; keep metadata sync ON). Guards the brief window before ZotMoov runs and
  any OA import done via the GUI.

## Architecture

```
DOI / PMID / title
   ↓  resolve to a trustworthy DOI (Semantic Scholar / PubMed) — never guess
   ↓  MCP add_by_doi → metadata + BBT citekey (no PDF in local mode)
   ↓  get the PDF (route ladder — see below)
   ↓  PDF becomes a Zotero attachment
        ├─ OA: Zotero "Find Available PDF" or Zotadata right-click → attaches in place
        └─ TDM route: paper_fetch.py saves PDF → drag the file onto the item
   ↓  ZotMoov auto-moves → linked-files folder + converts to linked file
   ↓  Obsidian note frontmatter: citekey + zotero URI
```

## Prerequisites

- **Zotero** running, Local API enabled (Settings → Advanced → ✅ Allow other apps).
- **Zotero MCP** (`ZOTERO_LOCAL=true`).
- Plugins: **Better BibTeX** (citekeys), **ZotMoov** (linked files — see above),
  **Zotadata** (stronger PDF/metadata discovery than built-in Find Available PDF).
- **Semantic Scholar MCP** (PMID→DOI, title search).
- `config.yaml` filled in (copy from `config.example.yaml`): your email + your library's
  endpoints.
- DPAPI tokens for paywalled-but-mineable publishers (optional, read by `paper_fetch.py`;
  missing token → script prints the `secret.ps1 set` command, never leaks a value):
  - `ELSEVIER_TDM_KEY` — register at dev.elsevier.com
  - `WILEY_TDM_TOKEN` — accept click-through at https://static.wiley.com/tdm/ then store
  - `SPRINGER_API_KEY` — register at dev.springernature.com (OA direct often works without it)
- DPAPI credentials for the institutional proxy path (`library_session.py`):
  `LIB_USER` / `LIB_PASS` — your own library account.

## Main Flow

### Step 1: Resolve to DOI

**CRITICAL: Never guess or fabricate DOIs.** Always verify through an authoritative source.

| Input | Method |
|-------|--------|
| DOI | Verify it exists: Semantic Scholar `get_paper_details` with `DOI:{doi}` |
| PMID | Semantic Scholar `get_paper_details` with `PMID:{pmid}` → extract DOI |
| Title | Semantic Scholar `search_papers` → confirm title/authors → extract DOI |
| Author + Year | `search_papers` with `"{author} {year}"` → match by title → DOI |
| eTOC email | Extract title + authors → search Semantic Scholar/PubMed → DOI. **Do NOT construct DOIs from journal numbering** |

If Semantic Scholar has no result (too new), fall back to PubMed MCP `search_articles` or
`lookup_article_by_citation`.

### Step 2: Check if already in Zotero

`search_items` with the DOI → if found, skip to Step 5 and tell the user it already exists.

### Step 3: Add metadata to Zotero

`add_by_doi` with the DOI (optionally `collections`, `tags`). Zotero fetches full metadata
from CrossRef/PubMed and Better BibTeX generates the citekey. **No PDF is attached in local
mode** — that's Step 4's job.

### Step 4: Get the PDF (route ladder)

Try in order; each route falls through to the next, ending at the SFX institutional link.

| DOI prefix | Publisher | Route |
|---|---|---|
| OA (any) | — | Zotero **Find Available PDF** or **Zotadata** right-click → attaches in place ✅ |
| 10.1016 | Elsevier / ScienceDirect | `paper_fetch.py` → Elsevier TDM (`ELSEVIER_TDM_KEY`) |
| 10.1002 / 10.1111 | **Wiley** | `paper_fetch.py` → Wiley TDM (`WILEY_TDM_TOKEN`) |
| 10.1007 / 10.1186 | Springer / BMC | `paper_fetch.py` → OA content/pdf (Springer API key optional) |
| anything left | — | `paper_fetch.py` → Unpaywall direct |
| paywalled (Wiley/LWW/Sage/NEJM…) | institutional | `library_session.py fetch` — **fully automatic** off-campus (see below) |

For OA papers the easiest path is letting Zotero/Zotadata attach it directly — then ZotMoov
handles the linked file. Use `paper_fetch.py` when the publisher blocks automated fetch:

```
python paper_fetch.py <DOI> <out.pdf>
```

- Routes by DOI prefix, validates `%PDF`, falls back to Unpaywall, and on total failure
  prints your institution's SFX link (from `config.yaml`).
- Keys read from DPAPI, never printed. PC-only (DPAPI is PC-local).
- **TDM route lands a file on disk → drag it onto the Zotero item.** ZotMoov then moves it
  to your linked-files folder and converts to linked.

#### Institutional paywalled full text — `library_session.py` (off-campus, fully automatic)

For subscribed-but-paywalled papers (Wiley, LWW, Sage, NEJM…) when working from home,
`paper_fetch.py` can't reach them. Use the remote-auth downloader **with your own account**:

```
python library_session.py fetch <DOI> <out.pdf>
python library_session.py check    # session still valid?
python library_session.py stats    # rate / block analysis
```

- Logs into your library's remote-auth system automatically: credentials from DPAPI
  (`LIB_USER`/`LIB_PASS`), numeric CAPTCHA solved offline by **ddddocr**. Session persists
  across browser close/reboot (cookies saved to DPAPI `LIB_COOKIE_*`).
- Downloads via proxy-rewrite domains using **patchright** (stealth Playwright) so it
  passes the publisher's Cloudflare challenge **headless — no window, no interaction**.
  (`login` is the exception: it runs headful, because the proxy's JS-redirect interstitial
  never completes in a headless browser.)
- Fetched PDF → drag onto the Zotero item → ZotMoov links it as usual.
- **Verified**: Wiley (10.1002/10.1111), Springer (10.1007). **Untested templates**: NEJM,
  Sage, BMJ. Route map lives in `PROVIDER_ROUTES` — the code is the source of truth.
- **SERIAL BY DESIGN, enforced.** The browser profile is exclusive; a cross-process lock
  makes a concurrent caller queue, then exit `4`. **Never call this from parallel agents** —
  fetch serially first, then hand the PDF paths to the agents.
- **Bounded failure**: a watchdog (`PAPERFETCH_TIMEOUT_S`, default 240 s) exits `5` instead
  of hanging, and tree-kills its own chromium. Never wrap the script in a bare `timeout` —
  that kills the parent and orphans the browser.
- **Exit codes**: `0` PDF · `1` usage · `2` no route/auth failed · `4` busy · `5` watchdog.
  **`4`/`5` mean "retry serially", not "no full text".**
- ⚠️ `is_oa: true` from Unpaywall does **not** guarantee a PDF: hybrid and ahead-of-print
  articles report OA with no usable `url_for_pdf`. Fall through instead of giving up.
- **Rate awareness**: every proxy hit is logged; run `stats` to watch for the first block
  and learn the true daily ceiling. The courtesy delay (`rate.min_interval_s`) can be
  lowered but bulk download risks blocking the whole institution's IP — see the script.

### Step 5: Verify and bidirectional links

- Confirm the item: `search_by_citation_key` with the BBT key (`authorShortTitle2024`).
- If a PDF was attached, confirm it became a **linked file** in your folder (not in
  `Zotero/storage/`) — that's the "not uploaded" guarantee.
- **Obsidian → Zotero** frontmatter:
  ```yaml
  citekey: "{citekey}"
  zotero: "zotero://select/items/@{citekey}"
  ```
- **Zotero → Obsidian**: `create_note` child note with `obsidian://open?vault=<vault>&file={note}`.

## Batch Mode (eTOC / RSS / DOI list)

1. Collect DOIs.
2. `add_by_doi` for each (metadata).
3. Run `paper_fetch.py` per DOI that needs a TDM/OA fetch → PDFs to a staging folder.
4. Drag the batch of PDFs onto their items; ZotMoov auto-processes all (one move per file).
5. Summary: added / already-exists / PDF-linked / needs-SFX.

**⚠ Batch caution**: the proxy path is serial and throttled for a reason. Batching many
paywalled DOIs through `library_session.py` is exactly the pattern that trips a publisher's
systematic-download block and can cut off the whole institution's access. Keep batches small
and stop if `stats` shows a block.

## Error Handling

| Situation | Action |
|-----------|--------|
| DOI not found by Zotero | `add_by_url` with `https://doi.org/{DOI}` |
| Elsevier/Wiley/Springer blocked | `paper_fetch.py <DOI> <out.pdf>` (auto-routes by prefix) |
| Missing TDM token | Script prints `secret.ps1 set <NAME>` — user stores it; never read the key |
| All auto routes fail (paywall/Cloudflare) | Script prints SFX link → institutional login → manual download → drag onto item |
| PDF not moving to linked folder | Check ZotMoov `enable_automove`; for already-synced files temporarily set `process_synced_files=true`, then "Move + Convert to Linked" |
| Zotero not running | Local API needs Zotero open |
| Duplicate detected | Show existing item's citekey |
| PMID has no DOI | Semantic Scholar related DOI, or add manually |
