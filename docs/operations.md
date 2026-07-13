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
