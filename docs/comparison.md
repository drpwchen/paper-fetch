# How this compares to other paper fetchers

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

Back to the [README](../README.md).
