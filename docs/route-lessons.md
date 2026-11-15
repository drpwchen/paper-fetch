# Route lessons: when a route "looks broken"

The short version lives in [AGENTS.md](../AGENTS.md) ("When a route 'looks broken', check
entitlement FIRST"). This page keeps the full account of each lesson — what happened, what the
log looked like, and what the fix was — so the one-line rules there stay one line.

Read the entitlement check first (`python holdings.py <DOI>`); everything below assumes
`subscribed=True, covered=True` and the route still fails.

## A "dead end" verdict can be wrong

BMJ was documented here as a Cloudflare dead end; in fact the WAF only blocks *headless*
requests, and a real headful navigation passes first try. If a citation-meta route returns
`cf_block`, try `nav=True` before concluding anything.

## A verdict that once held can go stale

The mirror image. BMJ's headful `nav` route passed on the first try when it was added; two
months later the same DOI logged `cf_block` twice and a 403 with `nav` still in effect. Read
that as "Cloudflare, wait or finish by hand", not as "the flag is broken" or "we lost the
subscription" — `holdings.py` still says subscribed. And check the *name* of the failure before
you trust it: that 403 was logged as `no_pdf_meta` (a 403 page has no `citation_pdf_url` in it
either), which is the same wall wearing a different label. Resolver responses are now
classified before the meta tag is looked for.

## Free at the publisher, walled everywhere else: the direct route (2026-09-06)

**"PubMed says free, Unpaywall says closed, publisher is Cloudflare-403, prefix has no proxy
route" is the direct route's job, not a dead end.** `library_session.py fetch` runs it on its
own when layer 1 reports `free_at_publisher` or `blocked_by`; `--direct` forces it. It opens
the publisher page in a real browser with NO proxy and fetches the PDF from inside the page. If
it logs `publisher_refused`, the publisher is declining PDFs for this network while serving the
HTML — a person in their own browser gets the same — so do not "fix" the route; wait, or read
the HTML.

## "No `citation_pdf_url`" is not "no route"

Some entitled articles simply don't carry the tag (Nature news/comment items). A `meta` route
can take `pdf_from_landing: "{landing}.pdf"` to derive the PDF URL from the article URL instead
of giving up.

## A headful SPA route that "times out" may be waiting on a modal you can no longer see

The `ck` route logged `boot_timeout` with the PDF endpoint at HTTP 500 on every poll — reads
like "ClinicalKey is down". The SPA had simply redrawn its organization-choice modal (buttons →
radio inputs) and the selector matched nothing; the user's own browser sailed through because
it carried a remember-me token. When an automation profile fails where a human's browser
succeeds on the *same URL*, diff the two — response bodies of the auth call, then the DOM —
before blaming the publisher.

## A wall that answers the automation profile is not a wall that answers the library (2026-09-08)

Springer through the proxy, article the library holds: `request.get` on the PDF URL → "Client
Challenge" (Fastly) twice; a headful navigation to the article page → the same challenge, whose
`script.js` answers HTTP 400 — for patchright's Chromium *and* for the real Google Chrome binary
launched under CDP. The user's own logged-in Chrome, same proxy, same URL, passed on the first
try and the in-page `fetch()` returned the 1.6 MB PDF. So: the tpl route now retries once as a
real navigation (it covers walls that only block `request.get`), a wall that survives that
retry is exit `7`, never `2`, and the finish for that case is the user's own browser, not
another automation trick.

Also: **a DOI prefix names who minted the DOI, not who serves it today** — journals move
publishers (Respiratory Care `10.4187` → Sage, Liebert `10.1089` → Sage) and their back
catalogue moves with them. `fetch` resolves `doi.org` once for an unknown prefix and matches
the landing host against the routes before saying `no_route` — and since 1.8.0 for a known
prefix too (Ann Rheum Dis `10.1136/annrheumdis-*` is BMJ's prefix on an Elsevier journal; the
BMJ meta route was answering exit 2 for it).

## A small HTML page from PMC / Europe PMC is a wall, not "no PDF" (2026-10-15, issue #7)

`pmc.ncbi.nlm.nih.gov/articles/PMC…/pdf/` (~1.8 KB) and `europepmc.org/…?pdf=render` (~5.5 KB)
answer scripts with a JavaScript challenge; `paper_fetch.py` names it `pmc_challenge` and exits
`7`. A real browser passes only by navigating to the **PDF URL itself** and fetching it from
inside that page — opening the article page first is not enough. The one honest "no PDF" is
Europe PMC's own `hasPDF=N` (author manuscript as HTML only), which is checked.

Re-captured 2026-11-03 (1.8.1): the page depends on host and User-Agent — PMC sends a
proof-of-work page (`POW_CHALLENGE`, 1.8 KB) or, to a browser UA, "Checking your browser -
reCAPTCHA" (20.5 KB); Europe PMC sends a Cloudflare **403** "Just a moment...". All three are
`pmc_challenge`, and both hosts serve them even for a `hasPDF=N` PMCID — so `hasPDF` is looked
up before any of them is called a wall.
