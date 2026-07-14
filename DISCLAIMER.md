# Disclaimer and acceptable use

**Read this before you run the tool, and before you change its rate limit.**

paper-fetch is a personal research utility. It is published in the hope it is useful; it is
**not** legal advice, and its author is not a lawyer.

## No warranty, no liability

This software is provided **"AS IS", without warranty of any kind**, as stated in the
[MIT LICENSE](LICENSE). The author accepts no liability for any claim, damage, account
suspension, IP block, or other consequence arising from your use of it. **You** are the one
running it, with **your** credentials, against **your** institution's licences.

## What this tool does and does not do

- It **does** automate routes you are already entitled to use: open-access repositories,
  publisher text-and-data-mining APIs you registered for yourself, and your own
  authenticated library session for subscriptions your institution already pays for.
- It **does not** bypass paywalls, break access controls, crack DRM, or share credentials.
  There is no Sci-Hub route and none will be accepted as a contribution.
- It ships **no institution's endpoints and no accounts**. You supply your own.

## Your responsibilities

1. **Use your own account.** Never use someone else's credentials, and never share yours.
2. **Follow your library's licence terms and each publisher's Terms of Service.** They bind
   you, not this repo. Most publisher agreements permit downloads for your own research and
   **prohibit systematic or bulk downloading — even for legitimate subscribers.**
3. **Do not remove the rate limit.** `rate.min_interval_s` (15 s, strictly serial) is not a
   performance setting. Publishers detect systematic downloading and respond by **blocking
   the entire institution's IP range** — your colleagues, your library, your hospital. If you
   disable it, that consequence is yours to own.
4. **Respect TDM API terms.** The Elsevier / Wiley / Springer text-mining APIs are licensed
   for your own research use; they generally do not permit redistributing the full text you
   retrieve. Read the terms you agreed to when you got your key.
5. **Do not redistribute downloaded PDFs.** Articles you retrieve are for your own reading,
   under your institution's licence. Passing them on is a separate act, and this tool does not
   make it lawful.
6. **If you have no legitimate access, the answer is a resolver link or an interlibrary loan**,
   not a workaround. That is exactly why route 4 of the ladder prints the link instead of
   trying harder.

## No affiliation

This project is not affiliated with, endorsed by, or sponsored by any publisher, library,
university, or hospital. All product and platform names belong to their respective owners.
