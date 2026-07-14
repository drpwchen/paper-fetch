# The holdings table — entitlement ground truth

`holdings.py` answers one question per DOI: **does my library actually hold this article?**
You need that answer *before* you believe a publisher route is broken, because an article you
have no entitlement to makes a perfectly good route return reader HTML or a 403.

## Why not just ask the link resolver?

Because it lies. Per-article resolver queries (SFX / 360 / OpenURL `getFullTxt`) are not a
stable oracle — the same DOI returned a full-text target on one call and nothing a few minutes
later. Entitlement built on that produces false negatives you will then "fix" in code.

Your library's **A–Z e-journal list** is journal-level, stable, offline and reproducible. Scrape
it once; query it forever.

## The table

`holdings.py` reads a SQLite DB (path: `holdings_db` in `config.yaml`, default
`holdings.sqlite` next to the script) with exactly one table:

```sql
CREATE TABLE journals (
  title       TEXT,   -- journal title as the library lists it
  publisher   TEXT,   -- the PLATFORM it is read on (Wiley, LWW, ScienceDirect, …) — not the imprint
  issn_print  TEXT,   -- "0749-8047"  (either ISSN may be blank; both are matched)
  issn_e      TEXT,   -- "1536-5409"
  is_free     INT,    -- 1 = free/OA (the OA layer suffices), 0 = paid subscription
  coverage    TEXT    -- the coverage string, verbatim: "Available from 1997/03/01 … until …"
);
```

Two columns carry all the subtlety:

- **`publisher` is the platform**, because that is what decides the route. A journal published
  by an obscure society but hosted on Wiley Online Library goes through the Wiley route.
- **`coverage` is kept verbatim** and parsed at query time. It can hold *several* ranges
  (`from 1997…`, `from 2013 until 2013`, `from 2019 until 2019`). A range with no `until` runs
  to the present. Any matching range means covered. Parsing only the first `from`+`until` pair
  is a real bug that reports current articles as out of coverage.

## Building it for your library

**This repo ships no scraper on purpose** — every library's A–Z page is different HTML (some
paginate, some are a JSON API behind the page, some hand you a CSV if you ask the librarian).
Get the list however is easiest for you, and write those six columns.

Practical route: open your library's A–Z e-journal list, point an AI coding agent at the page
and at this schema, and have it write the twenty-line scraper. Check the row count against what
the page claims, then spot-check three journals you know you subscribe to.

If your library will simply *give* you the list (many will export one), take the export. It is
the same data without the scraping.

## Using it

```bash
python holdings.py 10.1097/PHM.0000000000003036   # one DOI → subscribed? covered? platform?
python holdings.py platforms                      # every platform you subscribe to + journal counts
```

`platforms` is how you answer "which publishers still need a route?" — compare it against the
`ROUTES` table in `library_session.py`.

## The one caveat that matters

**"Not in the table" ≠ "no access."** JAMA is absent from one library's e-journal list entirely
(it lives under a separate *database* entry) and yet the proxy serves its PDFs fine. So
`check()` returns `subscribed=None` for a miss, meaning **unknown** — warn, and try the proxy
anyway. Never turn a miss into a hard skip.
