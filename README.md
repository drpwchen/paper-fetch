# paper-fetch — a publisher-aware full-text PDF fetcher

Give it a DOI; it walks a **route ladder** to get the full-text PDF the cheapest, most
legitimate way first — open access, then official publisher text-mining (TDM) APIs, then
your own institutional library proxy, and finally it just prints your library's resolver
link so you can finish by hand.

It's the **download end** of a small paper pipeline. The **reading end**
([claude-paper-tools](https://github.com/drpwchen/claude-paper-tools): appraisal +
digest) and the **discovery end** ([paper-radar](https://github.com/drpwchen/paper-radar):
an RSS learning radar) are separate repos.

> **This project ships no institution's access.** You supply your own library's endpoints
> (in `config.yaml`) and your own account (in a local secret store). It automates *your
> own* authenticated session — it is not a paywall bypass and it does not share credentials.
> Most readers will point an AI agent at this repo to understand and adapt the method; the
> code is written to be readable for exactly that.

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

Three design rules worth stealing:
- **Validate `%PDF` magic bytes**, never trust `Content-Type`. Paywalls and Cloudflare love
  to return `200 text/html` that *looks* like a PDF response but isn't.
- **Don't trust the resolver's "subscribed / not subscribed" flag** — attempt the proxy
  anyway; coverage metadata is often stale, and link resolvers are flaky besides (the same
  DOI returned a full-text target on one call and none minutes later).
- **Before blaming a route, check the article's entitlement.** See the trap below — it is the
  single most expensive mistake in this problem space.

## ⚠ The entitlement trap (read this before you "fix" a publisher route)

An article your library **doesn't hold** makes a *working* route return reader HTML or a 403 —
**a signal indistinguishable from a broken template.**

Three publishers in this project (Sage, Taylor & Francis, Oxford) were each written off as
"returns HTML, needs reverse engineering". All three worked the moment they were retested with
an article the library actually holds. Weeks of would-be reverse-engineering, avoided by
picking a better test article.

Two wrinkles that make this genuinely hard to see:
- **Coverage is per-journal AND per-year.** A library may hold a journal for a *single 1990s
  issue*, or exclude ahead-of-print. "We subscribe to that journal" is not enough — check the
  year of the article you're testing with.
- **Link resolvers (SFX/360/OpenURL) are unreliable oracles.** Same DOI, minutes apart,
  different answers. Don't build an entitlement check on one.

**What to use instead:** your library's **A–Z e-journal list** (journal → platform → coverage
years) is stable, complete, and usually a plain public page. Scrape it once into a local table,
then answer per DOI: CrossRef gives you ISSN + journal + year, the table gives you platform and
coverage. That is your ground truth — and when a route fails on an article that IS covered, only
*then* do you have a real bug.

One caveat worth encoding: **"not in the list" ≠ "no access."** JAMA isn't in this library's
e-journal list at all (it lives under a separate database entry) yet the proxy serves its PDFs
fine. So treat a miss as *unknown*, warn, and try the proxy anyway.

## Publisher routes verified working

There are three route shapes. Pick by what the publisher exposes:

| Shape | How it works | DOI prefixes |
|---|---|---|
| **template** (`PROVIDER_ROUTES`) | build the PDF URL from a host + path template | `10.1002`/`10.1111` Wiley · `10.1007`/`10.1186` Springer/BMC · `10.1056` NEJM · `10.1177` Sage · `10.1080` T&F · `10.2214` AJR · `10.1148` Radiology/RSNA · `10.1142` World Scientific |
| **citation-meta** (`_CITATION_META_PREFIXES`) | resolver → article HTML's `<meta name="citation_pdf_url">` → fetch with Referer. Headless. | `10.1001` JAMA · `10.1093` Oxford · `10.1542` Pediatrics · `10.1183` ERJ · `10.3171` J Neurosurg · `10.1038` Nature |
| **citation-meta + headful nav** (`_HEADFUL_META_PREFIXES`) | same, but the resolver runs as a real headful navigation to clear a Cloudflare challenge | `10.1136` BMJ · `10.3174` AJNR · `10.2967` J Nucl Med |
| **signed-URL** (`_LWW_PREFIXES`) | multi-step walk to a signed PDF URL (stub here; fully documented in the docstring) | `10.1097`/`10.1161`/`10.1213` LWW/Ovid |

`10.1016` Elsevier goes through the TDM API in `paper_fetch.py`, never the proxy.

> **BMJ is the cautionary tale.** An earlier version listed BMJ as a Cloudflare "WAF dead end"
> that no stealth browser could clear. That was wrong: the WAF only blocks *headless* requests
> (and `request.get`, even from a headful context). A real **headful navigation passes on the
> first try** — that's the whole `_HEADFUL_META_PREFIXES` variant. If a citation-meta route
> comes back `cf_block`, switch it to headful nav before concluding anything. (Highwire sites
> like AJNR/JNM also loop on the generic `doi-org` resolver → give them an explicit `host` so
> the route uses that site's own `/lookup/doi/`.)

The **`citation_pdf_url` route** is the one to reach for first with a new publisher: many sites
with no DOI→PDF template still advertise the exact PDF URL in a `<meta name="citation_pdf_url">`
tag on the article page (it's what Google Scholar indexes). Resolve the DOI through the proxy,
read the meta, fetch it with the article as `Referer`. No reverse engineering needed.

**A "broken route" is almost always "no entitlement to this article."** An article your library
doesn't subscribe to (or whose year is outside the coverage) returns reader HTML or a 403 from
the PDF endpoint — indistinguishable from a broken template. Sage, T&F, Oxford *and BMJ* were
each wrongly declared routeless on this basis. Verify entitlement (journal + coverage year)
against your library's holdings **before** concluding a route is broken.

## What's public here vs. what you supply

| Layer | This repo | You supply |
|---|---|---|
| OA + publisher TDM APIs (`paper_fetch.py`) | ✅ complete, runnable | your own API keys + email |
| Institutional proxy (`library_session.py`) | 🦴 architecture + generic scaffolding; `login()` and the LWW signed-URL flow are **documented stubs** | implement them for your library |
| Endpoints (resolver / proxy / remote-auth) | placeholders in `config.example.yaml` | your library's real values |

The proxy layer is deliberately a skeleton: the scaffolding (config, secret store, request
log, rate throttle, `stats`, host-rewrite, response classifier, the publisher route map,
two-phase orchestration) is all here and the technique is fully documented in the
docstrings — but `login()` and the LWW/Ovid multi-step flow are left for you to implement
against your own institution, because those are the parts that are specific to one library.

## Publisher full-text APIs (all public, worth a bookmark)

These are the sanctioned routes. Register for your own credentials:

| Publisher | Route | How to get access |
|---|---|---|
| **Unpaywall** | `api.unpaywall.org/v2/{doi}?email=you@x` | Free. Just pass your email. OA only. |
| **Elsevier TDM** | `api.elsevier.com/content/article/doi/{doi}?view=FULL` | Register at [dev.elsevier.com](https://dev.elsevier.com); header `X-ELS-APIKey`. Off-campus paywalled content also needs an `X-ELS-Insttoken` from your library. |
| **Wiley TDM** | `api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}` | Accept the click-through at [static.wiley.com/tdm](https://static.wiley.com/tdm/); header `Wiley-TDM-Client-Token`. |
| **Springer** | `api.springernature.com/openaccess/json?q=doi:{doi}` | Register at [dev.springernature.com](https://dev.springernature.com). OA direct (`link.springer.com/content/pdf/{doi}.pdf`) often works with no key. |
| **Crossref / Europe PMC** | metadata + OA full text | No key. Great for resolving and for PMC-hosted OA. |

## Finding your own library's endpoints (the institutional layer)

The proxy layer needs three values in `config.yaml`. None of them are secret — they're your
library's public infrastructure. Where to find them:

- **`sfx_base`** (link resolver): your library portal → *e-resources* / *databases* → *link
  resolver* / *SFX* / *Find it @ ...*. It's an OpenURL endpoint; the DOI goes in `id=doi:`.
- **`remote_auth_base`** (off-campus login gate): the page you log into to read journals
  from home. Often a Citrix NetScaler / *remote reader authentication* / *remote access* portal.
- **`proxy_suffix`** (the rewrite host): notice how a publisher URL changes when you open it
  through the proxy — `onlinelibrary.wiley.com` becomes something like
  `onlinelibrary-wiley-com.proxy.yourlib.edu:port`. The part after the dash-rewritten host
  is your `proxy_suffix`. **When in doubt, ask your librarian** — they'll know the EZproxy /
  proxy hostname.

If your library uses **EZproxy** (very common), the rewrite is usually `dots→dashes` +
a fixed proxy suffix, exactly the pattern `_proxy_host()` implements.

## Install

```bash
git clone <this-repo> && cd paper-fetch
pip install -r requirements.txt
python -m patchright install chromium      # only needed for the proxy layer
cp config.example.yaml config.yaml         # fill in your email + your library's endpoints
```

Store publisher/library credentials in a local DPAPI secret store (Windows), never in
`config.yaml`:

```powershell
powershell -File ~/.secrets/secret.ps1 set ELSEVIER_TDM_KEY
powershell -File ~/.secrets/secret.ps1 set WILEY_TDM_TOKEN
powershell -File ~/.secrets/secret.ps1 set LIB_USER   # your own library account
powershell -File ~/.secrets/secret.ps1 set LIB_PASS
```

(Any secret store works — the code shells out to `~/.secrets/secret.ps1 get <NAME>`; swap in
your own if you're not on Windows DPAPI.)

## Use

```bash
python paper_fetch.py 10.1371/journal.pone.0000000 out.pdf   # OA / TDM — works out of the box
python library_session.py check                              # proxy layer (after you implement login)
python library_session.py fetch 10.1002/xxxxx out.pdf
python library_session.py stats                              # rate / block analysis
```

`examples/example-note.md` shows the intended Zotero + Obsidian workflow around it.

### Exit codes (script it accordingly)

| Code | Meaning | What the caller should do |
|---|---|---|
| `0` | PDF written | validate `%PDF`, carry on |
| `1` | usage error | fix the command |
| `2` | no route / auth failed | genuinely unavailable — stop |
| `4` | profile busy (another fetch holds the lock) | **retry serially** — not a missing paper |
| `5` | watchdog abort (`PAPERFETCH_TIMEOUT_S`, default 240 s) | **retry once** — not a missing paper |

==Codes `4` and `5` mean "try again, one at a time" — never record them as "no full text."==
Conflating them is the single easiest way to wrongly conclude a paper is unobtainable.

### Calling this from parallel workers / LLM agents

**Don't.** `library_session.py` drives one exclusive browser profile and is serial by design;
a cross-process lock makes concurrent callers queue and then fail with exit `4`. If you are
batch-processing papers (e.g. an agent per paper), **fetch the PDFs in a serial pre-pass
first**, then hand the resulting file paths to the workers. Letting N agents each race for
the browser deadlocks them, and each will independently retry — burning time and, if they're
LLM agents, tokens.

Likewise, **never wrap this script in `timeout`**: that kills the parent and orphans the
chromium child (leaked RAM). The built-in watchdog already bounds every run and tree-kills
its own browser.

## ⚠ Rate limits & your institution's IP — read this

The proxy layer defaults to a **15-second courtesy delay** and is **strictly serial**. That
is not a performance limit — it's protection.

When a publisher detects *systematic downloading*, it blocks the **entire institution's IP
range**. Everyone at your institution loses access, not just you. So:

- `rate.min_interval_s` can be lowered or set to `0`, but `0` prints a warning. Only do that
  for a handful of papers.
- Run `stats` regularly to watch for the first `cf_block` / `rate_limited`.
- The true daily ceiling is unknown and publisher-specific — the tool logs every request so
  you can learn yours empirically. If you hit a block, note the request count it happened at.

## Red lines

- For people who **already have legitimate subscription access**. It automates your own
  authenticated session; it is not a way around a paywall and not a way to share an account.
- **Your account, your responsibility.** Use your own credentials and follow your library's
  license terms and each publisher's ToS.
- Never commit `config.yaml`, `*.dpapi`, or `access_log.jsonl` (the `.gitignore` blocks them).

MIT licensed. Contributions that add publisher route templates or adapt the proxy layer to
other library systems are welcome.

## Changelog

Notable changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## The rest of the pipeline

| Repo | Role |
|---|---|
| [paper-radar](https://github.com/drpwchen/paper-radar) | **discovery** — journal/PubMed feeds → interest-scored → private triage |
| **paper-fetch** (you are here) | **download** — DOI → full-text PDF via the route ladder |
| [claude-paper-tools](https://github.com/drpwchen/claude-paper-tools) | **reading** — `/paper-review` appraisal, `/paper-digest` content digest |

---

☕ Find this useful? [Buy me a boba](https://drpwchen.bobaboba.me).
