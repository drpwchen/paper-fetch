# When every route fails: an r/Scholar request (drafted, never posted)

```bash
python reddit_request.py draft 10.1136/annrheumdis-2021-219846   # OA gate, then title + body
python reddit_request.py dedupe 10.1136/annrheumdis-2021-219846  # has someone asked already?
python reddit_request.py track https://www.reddit.com/r/Scholar/comments/<id>/ --doi <DOI>
python reddit_request.py poll                                    # schedule this every 30 min
python reddit_request.py verify <id> received.pdf                # is it the article? → verified
python reddit_request.py status
```

Interlibrary loan and writing to the author are the copyright-clean finishes; asking on
r/Scholar is your call. If you make it, this script does the bookkeeping — **it does not post**.

- **`draft`** writes the request in the subreddit's pinned format, author included:
  `[Article] <Title> + So et al. (Ann Rheum Dis, 2021)`, body with the citation, DOI, doi.org
  link and PMID (metadata from PubMed, Crossref when the DOI is not in PubMed). It **refuses
  (exit `8`) when Unpaywall says the article is open access** and prints the OA location
  instead: a publisher that blocks scripts is not a paywall, and asking strangers for a gold-OA
  paper is the mistake this gate exists for. Without a working Unpaywall check it refuses too,
  unless you pass `--skip-oa-check`.
- **`paper_fetch.py --json`** attaches the same draft to a failed envelope as `reddit_draft` —
  or `{"refused": …}` when the article is OA, behind a bot wall, or flagged free by PubMed
  (those go to `library_session.py fetch`, which opens the publisher in a real browser).
- **`poll`** reads each tracked thread's comment feed: a comment from anyone but you and
  AutoModerator → `replied`; one with an off-Reddit link → `fulfilled` (a candidate — links are
  untrusted and never downloaded). Reddit allows an unauthenticated client about **one request
  per minute** (`x-ratelimit-remaining: 0` after a single GET); `poll` waits out that budget
  between threads and, on a 429, records a backoff and exits `7` instead of retrying. **Reddit
  chat is in no RSS feed** — a fulfiller who answers only by chat is invisible to `poll`. Your
  private inbox feed (`reddit.inbox_feed_secret`) adds comment replies and private messages.
- **`verify`** runs `pdf_verify` against the article title (optionally `--registry NCT…`) and
  reminds you to reply `thanks solution verified`, which is how the subreddit scores fulfillers.
  If you checked the file by hand, `verify <id> --manual` records that; if you also replied
  yourself, the next `poll` sees your comment and marks the request `verified` on its own.
- **`status`** is the tracking table — when each request went out, how many replies, whether a
  link was offered, its status, whether you thanked the fulfiller. `<state_dir>/status.md` holds
  the same table as Markdown and is rewritten on every change (`status --md` / `--json` too).
- **Notifications** go to `<state_dir>/events.jsonl` always — but nobody reads that file, so
  set up at least one channel in `reddit.notify`. Until you do, `track`, `poll` and the top of
  `status.md` say so loudly. Events carry title, DOI, status and comment count — never
  comment text.

  | Channel | You need | Secret (store with `python library_session.py secret set <NAME>`) |
  |---|---|---|
  | `ntfy` | the ntfy app, a long random topic name | access token — only for a protected topic |
  | `discord` | a channel webhook (Channel settings → Integrations → Webhooks → Copy URL) | the webhook URL → `url_secret` |
  | `telegram` | a bot from @BotFather; press **Start** in your bot once | the bot token → `token_secret`; `chat_id` is not secret (`notify-test` lists it for you) |
  | `email` | an SMTP server (Gmail: `smtp.gmail.com`, 587, an **app password**) | the SMTP password → `password_secret` |
  | `webhook` | anything that accepts the raw event JSON (not Discord/Slack — they want their own format) | `url_secret` |
  | `command` | your own script; it gets the event JSON on stdin (use absolute paths) | — |

  Each sink may take `events: [fulfilled, stale]` to receive only those (`replied`,
  `fulfilled` = someone left a link, `verified`, `stale` = closed without a reply, `inbox`).
  **`python reddit_request.py notify-test`** sends a test through every sink and prints
  `OK` / `FAIL — why` per sink (exit `9` if any failed; `--only discord` tests one). A sink
  that fails later, during a scheduled `poll`, is named at the top of `status.md` until a
  delivery through it succeeds again.

Requests nobody answers within `reddit.stale_days` (default 21) are closed, so polling
ends. `reddit.on_idle` / `on_active` run a command when nothing is left to watch / when a
request opens — point them at your scheduler to switch the job off and on (example in
`config.example.yaml`).

Scheduling `poll` every 30 minutes — Windows:
`schtasks /Create /SC MINUTE /MO 30 /TN paper-fetch-reddit-poll /TR "python C:\path\to\reddit_request.py poll"`;
cron: `*/30 * * * * cd /path/to/paper-fetch && python reddit_request.py poll >> poll.log 2>&1`.

Back to the [README](../README.md).
