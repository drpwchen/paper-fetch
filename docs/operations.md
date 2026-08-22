# Operations: rate limits, batching, and calling from agents

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

## Calling this from parallel workers / LLM agents

**Don't.** `library_session.py` drives one exclusive browser profile and is serial by design;
a cross-process lock makes concurrent callers queue and then fail with exit `4`. If you are
batch-processing papers (e.g. an agent per paper), **fetch the PDFs in a serial pre-pass
first**, then hand the resulting file paths to the workers. Letting N agents each race for
the browser deadlocks them, and each will independently retry — burning time and, if they're
LLM agents, tokens.

Likewise, **never wrap this script in `timeout`**: that kills the parent and orphans the
chromium child (leaked RAM). The built-in watchdog already bounds every run and tree-kills
its own browser.

## The two things a batch gets wrong (both cost a whole batch once)

### 1. Check the session first, and abort the batch if it's dead

```bash
python library_session.py check || { echo "session dead — run: library_session.py login"; exit 1; }
```

An expired session doesn't fail *the batch*; it fails **each paper separately**, and every one
of those failures reads like "this paper has no route". One run recorded nine papers as
paywalled that way. They weren't: `login` succeeded on the first try (it is fully automatic —
stored credentials plus offline CAPTCHA OCR) and the papers came down. That is why auth has its
own exit code now:

| code | meaning | is it about the paper? |
|---|---|---|
| `2` | a route ran and came back empty | **yes — the only one** |
| `3` | auth failed / session expired | no — fix the session |
| `4` / `5` | profile busy / watchdog | no — retry serially |
| `6` | no route for this publisher prefix | no — check holdings, consider adding one |

==Any "no full text available" conclusion is only valid if the session was valid at the time.==
If your batch log records exit codes, record which ones — a log that only says "failed" cannot
tell these apart after the fact.

### 2. Pass `--title`, or you will file whole volumes as articles

```bash
python paper_fetch.py --title "$title" "$doi" "out/$id.pdf"
python library_session.py fetch "$doi" "out/$id.pdf" --title "$title"
```

Conference abstracts usually carry the **supplement's** DOI rather than their own, so every
resolver in the ladder honestly returns the entire proceedings volume. It is a valid PDF, it is
large, and it passes every byte-level check — one 563-page, 48 MB volume was logged `ok` as if
it were the one-page research letter that was asked for. Across one 49-paper batch, **10 (20%)
of the "successful" downloads were whole volumes.**

With a title, the fetch verifies the article is actually in the file and, for a volume, cuts out
the pages it sits on — keeping the volume as `<stem>_volume.pdf`, because it took an
institutional session to obtain and it is the only way to re-check the cut. Verdicts:
`match` · `volume_like` (cut) · `title_absent` · `no_text` · `unreadable`; the last three are
reported, never silently accepted.

Downstream, a whole volume is worse than a missing file: a reader (human or LLM) will produce a
confident answer about the wrong study.
