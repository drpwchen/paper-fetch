# Example: fetching an open-access paper end-to-end

A worked example using a **genuinely open-access** DOI, so nothing here touches a paywall.

Paper: *"The PRISMA 2020 statement: an updated guideline for reporting systematic reviews"*
(Page et al., 2021), DOI `10.1136/bmj.n71` — OA under CC BY.

## 1. Fetch the PDF

```bash
python paper_fetch.py 10.1136/bmj.n71 prisma2020.pdf
```

What happens (route ladder):

```
DOI: 10.1136/bmj.n71
→ 試 unpaywall
  OA 候選 PDF: https://www.bmj.com/content/bmj/372/bmj.n71.full.pdf
  GET ... → HTTP 200 · 1240213 bytes
✓ PDF 已存 → prisma2020.pdf (1240213 bytes)
```

No key needed — Unpaywall found the OA copy directly and the `%PDF` check passed.

## 2. Add to Zotero + link the file

1. `add_by_doi` (Zotero MCP) with `10.1136/bmj.n71` → metadata + a Better BibTeX citekey
   like `page2021prisma`.
2. Drag `prisma2020.pdf` onto the new Zotero item.
3. ZotMoov moves it to your linked-files folder and converts it to a **linked file** — it
   never uploads to Zotero cloud storage.

## 3. Cross-link in Obsidian

Note frontmatter:

```yaml
citekey: "page2021prisma"
zotero: "zotero://select/items/@page2021prisma"
```

## For paywalled papers

The same first command tries the publisher TDM APIs (if you've stored a key) and then your
institutional proxy (if you've configured it and implemented `login()`), before falling
back to printing your library's resolver link. See the README route-ladder section.
