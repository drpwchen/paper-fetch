---
name: paper-fetch
description: >
  Add academic papers to Zotero via MCP (DOI/PMID/title); fetch full-text PDFs through
  a publisher-aware route ladder (Unpaywall → Elsevier/Wiley/Springer TDM → institutional
  proxy → SFX); PDFs land in a linked-files folder (ZotMoov), never uploaded to Zotero cloud.
  Also verifies that a downloaded PDF really is that article (and cuts it out of a whole
  conference supplement when it isn't).
  Trigger: "下載論文", "抓全文", "download paper", "get full text", "加進 Zotero",
  "抓到整本", "PDF 是不是這篇", or when /paper-review needs a paper added to the library.
---

# Paper Fetch — 論文入庫

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
- DPAPI tokens for paywalled-but-mineable publishers (optional, read by `paper_fetch.py`,
  which is still Windows-only — missing token → script prints the `secret.ps1 set` command,
  never leaks a value):
  - `ELSEVIER_TDM_KEY` — register at dev.elsevier.com
  - `WILEY_TDM_TOKEN` — accept click-through at https://static.wiley.com/tdm/ then store
  - `SPRINGER_API_KEY` — register at dev.springernature.com (OA direct often works without it)
- Secret-store credentials for the institutional proxy path (`library_session.py`):
  `LIB_USER` / `LIB_PASS` — your own library account. The backend follows the platform
  (DPAPI on Windows, login Keychain on macOS, env vars elsewhere) — README → Install.

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
| 10.1016 | Elsevier / ScienceDirect | `paper_fetch.py` → Elsevier TDM (`ELSEVIER_TDM_KEY`); when TDM serves only an in-press cover, **or only the first page because the key is not entitled** (`X-ELS-Status: WARNING … not entitled`, HTTP 200 — v1.5.1), falls through to the ClinicalKey `ck` route (headful window, organization modal auto-answered — v1.4.0). `library_session.py fetch … --skip-layer1` forces `ck` directly |
| 10.1002 / 10.1111 | **Wiley** | `paper_fetch.py` → Wiley TDM (`WILEY_TDM_TOKEN`) |
| 10.1007 / 10.1186 | Springer / BMC | `paper_fetch.py` → OA content/pdf (Springer API key optional) |
| anything left | — | `paper_fetch.py` → Unpaywall direct |
| paywalled (Wiley/LWW/Sage/NEJM…) | institutional | `library_session.py fetch` — **fully automatic** off-campus (see below) |

For OA papers the easiest path is letting Zotero/Zotadata attach it directly — then ZotMoov
handles the linked file. Use `paper_fetch.py` when the publisher blocks automated fetch:

```
python paper_fetch.py <DOI> <out.pdf>
python paper_fetch.py --title "<article title>" <DOI> <out.pdf>
```

- ==批次取件一律加 `--title`==：`%PDF` 這種位元組層檢查分不出「這篇文章」與「這篇文章所在
  的那一本」。會議摘要常掛**增刊的 DOI**，於是 Unpaywall／TDM／機構 proxy 三條路都會誠實地
  回整本——一個 563 頁、48 MB 的會議摘要集完全合乎 `%PDF-` ＋大小檢查而被記成 `ok`。
  給了 `--title` 就會做內容驗證，判為整本時**自動切出該篇**（整本保留成 `<stem>_volume.pdf`）。
  見下面「內容層驗證」。
- Routes by DOI prefix, validates `%PDF`, falls back to Unpaywall, and on total failure
  prints your institution's SFX link (from `config.yaml`).
- Keys read from DPAPI, never printed. Windows-only for now (this script has not been
  moved onto the pluggable secret store yet; `library_session.py` has).
- **TDM route lands a file on disk → drag it onto the Zotero item.** ZotMoov then moves it
  to your linked-files folder and converts to linked.

#### Institutional paywalled full text — `library_session.py` (off-campus, fully automatic)

For subscribed-but-paywalled papers (Wiley, LWW, Sage, NEJM…) when working from home,
`paper_fetch.py` can't reach them. Use the remote-auth downloader **with your own account**:

```
python library_session.py fetch <DOI> <out.pdf>
python library_session.py fetch <DOI> <out.pdf> --title "<article title>"
python library_session.py check    # session still valid?  ← 批次開跑前一定要先跑
python library_session.py stats    # rate / block analysis
python library_session.py routes   # per-route scorecard + holdings gaps
```

- Logs into your library's remote-auth system automatically: credentials from the secret
  store (`LIB_USER`/`LIB_PASS`), numeric CAPTCHA solved offline by **ddddocr**. Session
  persists across browser close/reboot (cookies saved as `LIB_COOKIE_*`; the `env` backend
  can't persist them, so that one logs in each run).
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
- **Exit codes**: `0` PDF · `1` usage · `2` 路線跑了但回空 · `3` 認證失敗／session 過期 ·
  `4` busy · `5` watchdog · `6` 此 prefix 無路線。==只有 `2` 是關於這篇論文的證據==；
  其餘都是「修好再跑」。見下面「批次取件的兩條硬規矩」。
- ⚠️ `is_oa: true` from Unpaywall does **not** guarantee a PDF: hybrid and ahead-of-print
  articles report OA with no usable `url_for_pdf`. Fall through instead of giving up.
- **Rate awareness**: every proxy hit is logged; run `stats` to watch for the first block
  and learn the true daily ceiling. The courtesy delay (`rate.min_interval_s`) can be
  lowered but bulk download risks blocking the whole institution's IP — see the script.

#### 批次取件的兩條硬規矩（2026-08-21 用九篇論文換來的）

**① 批次開始前先 `check`，session 無效就中止整批。**
一個 session 已死的批次，會**每篇各產生一次失敗**，而每一筆看起來都像「這篇沒有管道」。
一輪 9 篇就這樣全被判成付費牆抓不到，事後 `login` 一次就成功（憑證＋離線 OCR 全自動），
9 個假 failed 其實是 **1 個認證失敗**。
==任何「無全文／無管道」的結論，前提都是「當下 session 有效」；前提壞了，結論不成立。==

**② exit code 已拆開，不要再把非 0 都當成缺全文。**

| code | 意思 | 對這篇論文的意涵 |
|---|---|---|
| `0` | PDF 到手 | — |
| `1` | 用法錯 | — |
| `2` | 路線跑了，回來是空的 | ==只有這個==是關於論文本身的證據 |
| `3` | **認證失敗／session 過期** | 對論文**零資訊** → 修 session 再跑 |
| `4` / `5` | profile 忙／watchdog | 串行重試，不是缺全文 |
| `6` | 這個出版社還沒有路線 | 館藏可能仍有訂閱 → 值得補路線 |

（`3` 與 `6` 以前都混在 `2` 裡，這正是那九筆假 failed 的成因。`check` 的 EXPIRED 也回 `3`。）

**③ SFX／link resolver 的服務清單不是館藏全貌。** 它免登入可讀，可以當「先試哪條路」的排序
訊號，但**不能當判決**：有期刊在清單上完全沒有全文 target，館方其實訂了（`10.1136` BMJ 即是
實例，holdings 顯示 subscribed／covered）。==要標成「無管道」，必須在有效 session 下實際走過
一次路線表。==

#### 內容層驗證：抓到的是不是「那一篇」— `pdf_verify.py`

```
python pdf_verify.py <pdf> --title "<article title>" [--extract]
```

- **為什麼要有**：取件腳本一路只用 `%PDF-` ＋大小當合格條件，於是 48 MB／563 頁的會議摘要集
  被標成 `ok`。全掃 49 篇後發現 ==10 篇（20%）拿到的是整本==，而且全是核心研究。
  ==拿錯文件去跑全文篩選比缺全文更糟——它會對錯的研究產出一個有信心的答案。==
- **成因是常態不是意外**：會議摘要常掛**增刊的 DOI**，每一條路線都會誠實地把整本交給你。
- **判準**：標題要在頁面裡**連續**出現（不是關鍵詞集合——摘要集一頁塞好幾篇，bag-of-words
  會讓隔壁兩篇各出一個詞湊出假命中）；正規化時接回跨行斷字。判定：`match` /
  `volume_like`（切）/ `title_absent` / `no_text` / `unreadable`。
- **整本一律保留成 `<stem>_volume.pdf` 不刪**：它是花一次機構 session 換來的，也是複查
  「切的那一頁對不對」唯一的依據。定位模稜兩可時**寧可不切**。
- ==驗證方法選錯，會製造出比原始錯誤更難察覺的假警報==：第一版複驗只印每頁前 230 字抽查，
  據此判定「4 筆有 3 筆切錯」——錯的是那個判定（標題在頁面下半）。改成整頁比對後，10 個
  候選頁 100% 命中、第二名只有 20–45%。檢查的粒度要對得上文件的排版。

### Step 5: Verify and bidirectional links

- Confirm the item: `search_by_citation_key` with the BBT key.
- ==**The citekey is an exact-match string — copy it, never generate it.**== Read
  `citationKey` off the `add_by_doi` / `search_items` response (or
  `curl -s "http://localhost:23119/better-bibtex/export/library?/1/library.bibtex" | grep -i <doi>`)
  and paste it character for character. The familiar `authorShortTitle2024` shape is for
  *recognising* a key, not producing one: BBT keeps hyphens (`guerra-armas…`), case-folds
  unpredictably, and appends a disambiguator (`…2018a`) when an author publishes twice in a
  year. A key you reconstruct looks right in review and points nowhere. Can't retrieve one
  (Zotero closed, item not in the library)? Write `citekey: ""` and say so — an empty value
  is better than an invented one.
- Prove it, don't eyeball it:
  ```bash
  python {REPO}/citekey_lint.py <note.md>      # 0 clean · 1 fabricated · 3 unverifiable
  ```
  On exit 1 the lint prints the authoritative key for that DOI, so the fix is a copy.
  Exit 3 means Zotero was unreachable — report it as unverified, not as a pass.
- If a PDF was attached, confirm it became a **linked file** in your folder (not in
  `Zotero/storage/`) — that's the "not uploaded" guarantee.
- **Obsidian → Zotero** frontmatter:
  ```yaml
  citekey: "{citekey}"
  zotero: "zotero://select/items/@{citekey}"
  ```
- **Zotero → Obsidian**: `create_note` child note with `obsidian://open?vault=<vault>&file={note}`.

## Batch Mode (eTOC / RSS / DOI list)

1. Collect DOIs **和標題**（標題是內容驗證的必要輸入，不是裝飾）。
2. `add_by_doi` for each (metadata).
3. ==批次動工前先 `library_session.py check`==；EXPIRED（exit `3`）就先 `login`，
   **不要讓整批帶著死 session 跑**。
4. Run `paper_fetch.py --title "<title>" <DOI> <out.pdf>` per DOI → PDFs to a staging folder；
   仍失敗的走 `library_session.py fetch <DOI> <out.pdf> --title "<title>"`（==SERIAL==）。
5. 逐筆記 exit code（`2` 才是缺全文；`3` 認證、`6` 無路線要另外處理），並記下
   `pdf_verify` 的判定；`volume_like` 已自動切頁，`title_absent`／`no_text` 要人工看一眼。
6. Drag the batch of PDFs onto their items; ZotMoov auto-processes all (one move per file).
7. Summary: added / already-exists / PDF-linked / needs-SFX / 內容驗證異常。

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
| `library_session.py` exit `3`（認證） | `login` 一次（全自動）→ 重跑。**不要記成缺全文**；批次中出現就中止整批 |
| `library_session.py` exit `6`（無路線） | 查 `holdings.py <DOI>`；訂閱中就值得補 ROUTES，`subscribed=None` 也不代表沒權限 |
| `library_session.py` 印 `reader_html`／「疑無訂閱」 | 出版社給的是閱讀器頁不是 PDF ＝ **無訂閱或年份在 coverage 外**，不是路線壞、不是認證。==不要帶 `--title` 重跑==（`--title` 只管驗證，不影響取得）→ 記成「無管道」，給 SFX 連結或跟作者要 |
| `library_session.py` 印 `timeout`（proxy 30 s 無回應） | 先 `check`；session VALID 就是出版社節流／無訂閱（Sage 對無權限請求會拖到 timeout），不要立刻重試，走 SFX |
| 抓到的 PDF 是整本增刊 | 給 `--title` 就會自動切出該篇（整本留成 `<stem>_volume.pdf`）；或事後 `pdf_verify.py <pdf> --title "…" --extract` |
| `pdf_verify` 判 `title_absent`／`no_text` | 不自動處理——人工開檔看一眼（掃描件、勘誤頁、抓到別篇都長這樣） |
| PDF not moving to linked folder | Check ZotMoov `enable_automove`; for already-synced files temporarily set `process_synced_files=true`, then "Move + Convert to Linked" |
| Zotero not running | Local API needs Zotero open |
| Duplicate detected | Show existing item's citekey |
| PMID has no DOI | Semantic Scholar related DOI, or add manually |
