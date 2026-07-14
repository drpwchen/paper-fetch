#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Entitlement lookup: DOI → ISSN + year (CrossRef) → your library's holdings table.

WHY THIS EXISTS — the single most expensive mistake in this problem space:
an article your library does NOT hold makes a *working* publisher route return reader HTML
or a 403, which is indistinguishable from a broken route. Three publishers in this project
were each written off as "needs reverse engineering" and all three worked the moment they
were retested with an article the library actually holds. So: check entitlement FIRST, and
only *then* believe a route is broken.

WHY NOT ASK THE LINK RESOLVER (SFX / 360 / OpenURL) PER ARTICLE: it is not a reliable
oracle. The same DOI returned a full-text target on one call and none a few minutes later.
Building an entitlement check on it manufactures false negatives.

Instead, use your library's A–Z e-journal list — journal → platform → coverage years. It is
journal-level, stable, offline and reproducible. Scrape it ONCE into the table below.
Every library's A–Z page is different HTML, so this repo ships no scraper: build the table
however you like (see docs/holdings.md for the schema — it is four columns).

    holdings.sqlite
      journals(title TEXT, publisher TEXT, issn_print TEXT, issn_e TEXT,
               is_free INT, coverage TEXT)

⚠ "not in the table" ≠ "no access". JAMA is absent from one library's e-journal list (it
lives under a separate database entry) yet the proxy serves its PDFs fine. So a miss means
UNKNOWN, not no-go: warn, and try the proxy anyway.

Usage:
    python holdings.py 10.1097/PHM.0000000000003036
    python holdings.py platforms      # which platforms you subscribe to, and how many journals
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import requests

from paper_config import CFG

DB = Path(CFG.get("holdings_db") or Path(__file__).with_name("holdings.sqlite"))
UA = f"paper-fetch holdings (mailto:{CFG.get('rate', {}).get('contact') or CFG.get('unpaywall_email') or 'unknown'})"
CACHE = Path(__file__).with_name(".doi_issn_cache.json")


def _norm(s):
    if not s:
        return None
    s = re.sub(r"[^0-9Xx]", "", s).upper()
    return f"{s[:4]}-{s[4:8]}" if len(s) == 8 else None


def doi_meta(doi):
    """DOI → dict(issns=[...], journal=str, year=int|None) via CrossRef. Cached on disk.

    Match on ISSN *and* journal title: CrossRef often returns only the e-ISSN while the
    holdings list records the print ISSN (or vice versa), so ISSN-only matching misses.
    """
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    except Exception:
        cache = {}
    if doi in cache:
        return cache[doi]
    meta = {"issns": [], "journal": "", "year": None}
    try:
        r = requests.get(f"https://api.crossref.org/works/{doi}",
                         headers={"User-Agent": UA}, timeout=20)
        if r.status_code == 200:
            msg = (r.json() or {}).get("message", {})
            for i in (msg.get("ISSN") or []):
                n = _norm(i)
                if n and n not in meta["issns"]:
                    meta["issns"].append(n)
            ct = msg.get("container-title") or []
            meta["journal"] = ct[0] if ct else ""
            for key in ("published", "issued", "published-online", "published-print"):
                parts = ((msg.get(key) or {}).get("date-parts") or [[None]])[0]
                if parts and parts[0]:
                    meta["year"] = int(parts[0])
                    break
    except Exception:
        return meta
    cache[doi] = meta
    try:
        CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return meta


def _tnorm(s):
    """Normalise a journal title for comparison: case/punctuation/spacing, '&'→'and', drop a
    leading 'the', drop a trailing '(core journal)'-style annotation some catalogues add."""
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"\(core journal\)", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return re.sub(r"^the", "", s)


def lookup(meta):
    """CrossRef meta → holdings rows [(title, publisher, is_free, coverage)]. ISSN first,
    then title."""
    if not DB.exists():
        return []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        for i in meta.get("issns", []):
            rows = con.execute(
                "select title, publisher, is_free, coverage from journals "
                "where issn_print=? or issn_e=?", (i, i)).fetchall()
            if rows:
                return rows
        j = _tnorm(meta.get("journal"))
        if not j:
            return []
        for title, pub, free, cov in con.execute(
                "select title, publisher, is_free, coverage from journals"):
            if _tnorm(title) == j:
                return [(title, pub, free, cov)]
        return []
    finally:
        con.close()


def _coverage_ok(coverage, year):
    """Is this article's year inside the subscribed range? True / False / None (unknowable).

    Coverage strings carry MULTIPLE segments — do not read only the first one:
      'Available from 1997/03/01 volume: 44 issue: 1.
       Available from 2013/07/01 ... until 2013/07/01 ...
       Available from 2019/07/01 ... until 2019/07/01 ...'
    Segment 1 has no 'until' = subscribed to the present. A naive regex grabs the first
    'from' and the first 'until' (which belongs to segment 2!) and wrongly reports a 2026
    article as out of coverage. Rule: split into segments; no 'until' = open-ended; ANY
    segment matching counts as covered.
    """
    if not coverage or not year:
        return None
    chunks = re.split(r"(?=\bfrom\s+\d{4})", coverage, flags=re.I)
    segs = []
    for c in chunks:
        f = re.search(r"from\s+(\d{4})", c, re.I)
        if not f:
            continue
        u = re.search(r"until\s+(\d{4})", c, re.I)
        segs.append((int(f.group(1)), int(u.group(1)) if u else 9999))
    if not segs:
        return None
    return any(lo <= year <= hi for lo, hi in segs)


def check(doi):
    """→ dict(subscribed, covered, platform, title, coverage, journal, year, issns).

    subscribed=True  → the library holds this journal as a paid subscription
    subscribed=False → held, but flagged free/OA (the OA layer should get it; skip the proxy)
    subscribed=None  → not in the table → UNKNOWN, still try the proxy (see the JAMA note)

    covered=True/False/None → whether this article's YEAR falls inside the coverage range.
    subscribed=True with covered=False is a real, common case (a library may hold a single
    1990s issue, or exclude ahead-of-print). That is exactly when the proxy returns reader
    HTML — do not misread it as a broken route.
    """
    meta = doi_meta(doi)
    rows = lookup(meta)
    if not rows:
        return dict(subscribed=None, covered=None, platform=None, title=None,
                    coverage=None, journal=meta.get("journal"), year=meta.get("year"),
                    issns=meta.get("issns"))
    title, pub, free, cov = rows[0]
    return dict(subscribed=not free, covered=_coverage_ok(cov, meta.get("year")),
                platform=pub, title=title, coverage=cov,
                journal=meta.get("journal"), year=meta.get("year"),
                issns=meta.get("issns"))


def print_platforms():
    """List every platform you subscribe to, with journal counts. This is the authoritative
    answer to 'which publishers still need a route?' — the other half of that question is
    library_session.py's ROUTES table."""
    if not DB.exists():
        sys.exit(f"no holdings table at {DB} — see docs/holdings.md for the schema.")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    rows = con.execute("SELECT publisher, COUNT(*) c FROM journals WHERE is_free=0 "
                       "GROUP BY publisher ORDER BY c DESC").fetchall()
    print(f"Subscribed platforms (is_free=0) — {sum(r[1] for r in rows)} journals:\n")
    for pub, c in rows:
        print(f"  {c:>5}  {pub}")
    free = con.execute("SELECT COUNT(*) FROM journals WHERE is_free=1").fetchone()[0]
    print(f"\nFree/OA journals (the OA layer suffices, no proxy needed): {free}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python holdings.py <DOI> | platforms")
    if sys.argv[1] == "platforms":
        print_platforms()
        sys.exit(0)
    d = sys.argv[1].strip().removeprefix("https://doi.org/")
    r = check(d)
    print(d)
    if r["subscribed"] is None:
        print(f"  journal: {r['journal'] or '?'} ({r['year'] or '?'})  ISSN: {r['issns'] or '(none)'}")
        print("  holdings: not in table → entitlement UNKNOWN (still try the proxy; JAMA is like this)")
    else:
        cov = {True: "✅ this article's year IS covered",
               False: "❌ year NOT covered (the proxy will return reader HTML — not a broken route)",
               None: "？coverage undeterminable"}[r["covered"]]
        print(f"  {r['title']} ({r['year'] or '?'})")
        print(f"  platform: {r['platform']} {'(subscribed)' if r['subscribed'] else '(free/OA)'}")
        print(f"  coverage: {r['coverage']}\n  → {cov}")
