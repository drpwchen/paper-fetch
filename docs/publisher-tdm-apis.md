# Publisher full-text APIs (all public, worth a bookmark)

These are the sanctioned routes. Register for your own credentials:

| Publisher | Route | How to get access |
|---|---|---|
| **Unpaywall** | `api.unpaywall.org/v2/{doi}?email=you@x` | Free. Just pass your email. OA only. |
| **Elsevier TDM** | `api.elsevier.com/content/article/doi/{doi}` with `Accept: application/pdf` — do **not** add `view=FULL`; it's unnecessary for PDF and gets rejected (`400 INVALID_INPUT`) for a subset of articles, masquerading as a coverage gap | Register at [dev.elsevier.com](https://dev.elsevier.com); header `X-ELS-APIKey`. Off-campus paywalled content also needs an `X-ELS-Insttoken` from your library. |
| **Wiley TDM** | `api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}` | Accept the click-through at [static.wiley.com/tdm](https://static.wiley.com/tdm/); header `Wiley-TDM-Client-Token`. |
| **Springer** | `api.springernature.com/openaccess/json?q=doi:{doi}` | Register at [dev.springernature.com](https://dev.springernature.com). OA direct (`link.springer.com/content/pdf/{doi}.pdf`) often works with no key. |
| **Crossref / Europe PMC** | metadata + OA full text | No key. Great for resolving and for PMC-hosted OA. |
