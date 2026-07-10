# AGENTS.md — for AI agents deploying or adapting paper-fetch

Most people will point you (an AI coding agent) at this repo and say "set this up for my
library" or "explain how this works." This file tells you how to do that correctly and
where the hard lines are.

## What this tool is

A publisher-aware full-text PDF fetcher built as a **route ladder**: OA (Unpaywall) →
official publisher TDM APIs → the user's own institutional library proxy → a printed
resolver link. It is the download end of a pipeline; the reading end is
`claude-paper-tools`, the discovery end is `paper-radar`.

## Deploying it for a user (the happy path)

1. `pip install -r requirements.txt`. For the proxy layer also `python -m patchright install chromium`.
2. `cp config.example.yaml config.yaml`. Fill in:
   - `unpaywall_email`, `rate.contact` — the user's email.
   - `institution.sfx_base / remote_auth_base / proxy_suffix` — the user's library's own
     endpoints. **Ask the user for these or help them find them** (README → "Finding your
     own library's endpoints"). Do NOT reuse endpoints from anywhere else.
3. Help the user store their own credentials in the local secret store (see README). Never
   put credential values in `config.yaml` or anywhere in the repo.
4. The OA + TDM routes (`paper_fetch.py`) work immediately once keys are stored. The proxy
   routes (`library_session.py`) need you to implement `login()` and, if the user needs
   LWW/Ovid, `_lww_ovid_pdf()` — both are documented stubs; adapt the selectors and request
   sequence to the user's own library by inspecting it in devtools.

## Hard lines — do not cross

- **Never fabricate or reuse another institution's proxy/resolver/remote-auth endpoints.**
  The user supplies their own. This tool ships none.
- **Never bake credential values into code or config.** They live in the local secret store.
- **Never remove or weaken the rate throttle to enable bulk downloading.** `rate.min_interval_s`
  exists to protect the whole institution's shared IP. If the user asks to disable it,
  surface the warning in the README and confirm they understand the consequence.
- **This is not a paywall bypass.** It automates a session the user is already entitled to.
  If a user has no legitimate access to a resource, the correct answer is a resolver link or
  an interlibrary-loan request, not a workaround.

## Explaining how it works

The docstrings in `library_session.py` describe the full technique (two-phase headless→headful
Cloudflare handling, the dots→dashes proxy host rewrite, session-cookie persistence, and the
LWW/Ovid signed-URL + Referer-chain flow). Read them to answer "how did they do this" — they
are written to teach the method, not just to run.
