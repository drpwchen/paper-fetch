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
 ├─ 1. Open Access ─────── Unpaywall → every oa_location, PMC→Europe PMC render,
 │                          landing-page citation_pdf_url meta (covers repositories)
 ├─ 2. Publisher TDM API ─ Elsevier / Wiley / Springer official text-mining endpoints
 │                          (you register for your own key; entirely sanctioned)
 ├─ 3. Institutional proxy your library's off-campus remote-auth + EZproxy/NetScaler proxy
 │                          (your own login; for subscriptions you already have)
 └─ 4. Resolver link ────── print your library's SFX/OpenURL link → finish manually
```

Two design rules worth stealing:
- **Validate `%PDF` magic bytes**, never trust `Content-Type`. Paywalls and Cloudflare love
  to return `200 text/html` that *looks* like a PDF response but isn't.
- **Don't trust the resolver's "subscribed / not subscribed" flag** — attempt the proxy
  anyway; coverage metadata is often stale.

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
