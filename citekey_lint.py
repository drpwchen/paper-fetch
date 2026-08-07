#!/usr/bin/env python3
"""citekey_lint — prove every citekey in your notes exists in Zotero.

A citekey is an exact-match string: it must equal Better BibTeX's `citationKey`
character for character. Rebuilding one from the familiar `authorShortTitle2024`
shape produces a key that *looks* right in review and silently points nowhere —
BBT keeps hyphens (`guerra-armas…`), case-folds unpredictably, and appends
disambiguators (`…2018a`) when an author publishes twice in a year.

So: copy the key from Zotero, and run this to prove you did.

Source of truth: the Better BibTeX library export from a running Zotero. If
Zotero is unreachable the lint exits 3 (UNVERIFIABLE) — never 0. A check that
cannot reach its source of truth must not read as a pass.

Usage
  python citekey_lint.py <note.md|dir> ...        # lint notes (or set NOTES_DIR)
  python citekey_lint.py --keys k1 k2             # check bare keys (pre-write)
  python citekey_lint.py --cards items.json       # JSON array of {citekey: ...}
  python citekey_lint.py --bibtex lib.bib ...     # offline, cached export

Exit: 0 clean · 1 offenders found · 3 cannot verify (Zotero down / no keys)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BBT_URL = "http://localhost:23119/better-bibtex/export/library?/1/library.bibtex"
NOTES_DIR = os.environ.get("NOTES_DIR", "").strip()

ENTRY_RE = re.compile(r"^@\w+\{([^,\s]+)\s*,", re.MULTILINE)
DOI_RE = re.compile(r"^\s*doi\s*=\s*[{\"]([^}\"]+)", re.MULTILINE | re.IGNORECASE)
FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.DOTALL)
FIELD_RE = r'^{field}\s*:\s*["\']?([^"\'\r\n]+)'


def fetch_library(bibtex_file: str | None) -> str:
    if bibtex_file:
        return Path(bibtex_file).read_text(encoding="utf-8", errors="replace")
    with urllib.request.urlopen(BBT_URL, timeout=30) as r:  # noqa: S310 (localhost)
        if r.status != 200:
            raise RuntimeError(f"BBT returned HTTP {r.status}")
        return r.read().decode("utf-8", errors="replace")


def parse_library(bib: str) -> tuple[set[str], dict[str, str]]:
    """→ (all citekeys, doi(lower) → citekey)."""
    keys, by_doi = set(), {}
    chunks = bib.split("\n@")
    for i, chunk in enumerate(chunks):
        text = chunk if i == 0 else "@" + chunk
        m = ENTRY_RE.search(text)
        if not m:
            continue
        key = m.group(1)
        keys.add(key)
        d = DOI_RE.search(text)
        if d:
            by_doi.setdefault(d.group(1).strip().lower(), key)
    return keys, by_doi


def frontmatter(path: Path) -> dict[str, str]:
    m = FM_RE.match(path.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return {}
    fm = m.group(1)
    out = {}
    for field in ("citekey", "doi"):
        f = re.search(FIELD_RE.format(field=field), fm, re.MULTILINE)
        if f:
            out[field] = f.group(1).strip()
    return out


def collect_notes(targets: list[str]) -> list[Path]:
    paths: list[Path] = []
    for t in targets:
        p = Path(t)
        paths.extend(sorted(p.rglob("*.md")) if p.is_dir() else [p])
    return paths


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="notes or dirs (default: $NOTES_DIR)")
    ap.add_argument("--keys", nargs="+", metavar="KEY", help="check bare citekeys")
    ap.add_argument("--cards", metavar="JSON", help="JSON array of objects with a citekey field")
    ap.add_argument("--ignore-prefix", metavar="STR", default="",
                    help="skip keys with this prefix (deliberately synthetic ones)")
    ap.add_argument("--bibtex", metavar="FILE", help="use a cached BBT export instead of localhost")
    ap.add_argument("--quiet", action="store_true", help="only print offenders")
    args = ap.parse_args()

    try:
        keys, by_doi = parse_library(fetch_library(args.bibtex))
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        print(f"UNVERIFIABLE: cannot read Better BibTeX library ({e}).", file=sys.stderr)
        print("  Open Zotero (with Better BibTeX) and re-run, or pass --bibtex <cached.bib>.", file=sys.stderr)
        print("  Do NOT report the citekeys as verified.", file=sys.stderr)
        return 3
    if not keys:
        print("UNVERIFIABLE: BBT export contained 0 entries.", file=sys.stderr)
        return 3

    offenders: list[str] = []
    checked = 0

    for key in args.keys or []:
        checked += 1
        if key not in keys:
            offenders.append(f"--keys · NOT IN ZOTERO: {key}")

    if args.cards:
        cards = json.loads(Path(args.cards).read_text(encoding="utf-8"))
        for c in cards:
            key = str(c.get("citekey", ""))
            checked += 1
            if args.ignore_prefix and key.startswith(args.ignore_prefix):
                continue  # deliberately synthetic keys (e.g. ad-hoc study cards)
            if key not in keys:
                offenders.append(f"{args.cards} · card NOT IN ZOTERO: {key} ({c.get('title', '')[:40]})")

    if not args.keys and not args.cards and not args.targets:
        if not NOTES_DIR:
            print("Nothing to check: pass notes/dirs, --keys, or --cards "
                  "(or set NOTES_DIR).", file=sys.stderr)
            return 3
        args.targets = [NOTES_DIR]
    for note in collect_notes(args.targets):
        fm = frontmatter(note)
        key = fm.get("citekey", "")
        if not key:
            continue
        checked += 1
        if key in keys:
            continue
        doi = fm.get("doi", "").strip().lower()
        fix = by_doi.get(doi)
        hint = f"  → authoritative key for that DOI: {fix}" if fix else "  → no Zotero entry for its DOI either"
        offenders.append(f"{note.name}\n    citekey: {key}   NOT IN ZOTERO\n  {hint}")

    if not args.quiet:
        print(f"Zotero BBT: {len(keys)} citekeys · checked {checked}")
    if offenders:
        print(f"\n❌ {len(offenders)} fabricated/stale citekey(s):\n")
        for o in offenders:
            print("  " + o)
        print("\nFix by copying the key verbatim from Zotero (zotero_search_items → citationKey),")
        print("never by rebuilding it from the authorShortTitleYear pattern.")
        return 1
    if not args.quiet:
        print("✅ all citekeys exist in Zotero")
    return 0


if __name__ == "__main__":
    sys.exit(main())
