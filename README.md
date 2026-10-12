# The Kernel: an AI-edited tech newsletter

The Kernel collects recent tech news from about twenty outlets, Hacker News, Reddit and a set of
YouTube channels. A Claude-powered editor agent reads it, checks it and writes up only the stories
that matter, and the result goes out by email, daily or weekly. Each reader picks the sections
they want.

It ships with a landing page and signup flow, an admin page for building and previewing issues,
a command line, a built-in scheduler, and a Docker setup.

**Contents**

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [What each file does](#what-each-file-does)
- [Quick start (local)](#quick-start-local)
- [Using it](#using-it)
- [Configuration](#configuration)
- [Deploying](#deploying)
- [Development notes](#development-notes)
- [Troubleshooting](#troubleshooting)

---

## How it works

Each issue goes through three stages:

1. **Collect.** Deterministic, cheap code gathers a few hundred recent items from RSS feeds, Hacker
   News, Reddit and YouTube. It drops junk (deal posts, gift guides, quiz hints), merges the same
   story from different outlets, removes stories already covered in recent issues, and ranks what's
   left. The top 120 candidates go forward.
2. **Edit.** Claude (Haiku 5.5 by default) gets the candidates plus a detailed editorial brief and
   works like an editor:
   - It triages the list.
   - It reads the strongest sources (about 30 per daily issue). Each source is condensed into
     short notes by a separate small call before the editor sees it.
   - It searches the web to verify claims and fill gaps.
   - It transcribes promising videos.
   - It checks how Hacker News is reacting.
   - It writes the issue and submits it as structured data.

   It cites sources by short ids (`c12`, `s3`), and code turns those into links, so it can't
   invent a URL.
3. **Deliver.** The issue is stored and rendered separately for each subscriber (HTML and plain
   text), including only the sections they chose. It's sent through Resend with one-click
   unsubscribe headers.

A daily issue takes about 2 minutes and costs about **$0.03** in Claude usage with the defaults
(Claude Haiku 5.5, measured on live runs). Switching the editor to Claude Opus 5.5 reads more
sources and catches more detail, for about $1 per issue. Either way the editor runs once per
issue no matter how many subscribers you have, so cost doesn't grow with your list. Each issue's
estimated cost is shown on the admin page.

---

## Architecture

```
                         ┌──────────────────────────── app/sources ───────────────────────────┐
  20 RSS feeds ─┐        │                                                                     │
  Hacker News ──┤        │  collect.py: fetch in parallel → drop junk → merge duplicates →     │
  6 subreddits ─┼───────▶│  remove already-covered URLs → rank by coverage + engagement        │
  8 YouTube ────┘        │                                                                     │
  channels               └───────────────────────────────┬─────────────────────────────────────┘
                                                         │ 120 ranked candidates (ids c1…c120)
                                                         ▼
                         ┌──────────────────────────── app/agent ─────────────────────────────┐
                         │  editor.py: tool loop with Claude                                   │
                         │    system prompt (prompts.py) + briefing with candidates            │
                         │                                                                     │
                         │    tools (app/tools):                    condense.py (small model)  │
                         │      read_article ────── fetch.py ─────────▶ ~150-word notes        │
                         │      web_search ──────── web_search.py   (Brave; results s1, s2…)   │
                         │      get_video_transcript transcripts.py ──▶ ~150-word notes        │
                         │      hn_discussion ───── hn.py ────────────▶ ~150-word notes        │
                         │      submit_digest ───── strict schema (schema.py); cited ids are   │
                         │                          checked and resolved to URLs               │
                         │    usage.py: tokens + estimated cost for every call                 │
                         └───────────────────────────────┬─────────────────────────────────────┘
                                                         │ Digest (JSON)
                                                         ▼
  ┌── app/pipeline.py ──────────────────────────────────────────────────────────────────────────┐
  │  store Edition + CoveredStory rows (db.py) → render per subscriber (render.py + templates/) │
  │  → send via Resend in batches of 100 (tools/email.py) → record Delivery rows                │
  └──────────────────────────────────────────────────────────────────────────────────────────────┘
        ▲                                   ▲                               ▲
        │ cron trigger                      │ admin buttons                 │ commands
  app/scheduler.py (APScheduler)      app/main.py /admin              app/cli.py
```

### Key design decisions

- **The agent doesn't gather the news.** Collection is ordinary code: fast, free and predictable.
  The model's job is judgment: what matters, whether it's true, and how to say it briefly. That
  keeps runs cheap and stops the agent from wandering.
- **One shared issue per day, personalized at render time.** The agent writes one digest per
  cadence and date. Section filtering happens when each email is rendered, so 10 or 10,000
  subscribers cost the same in model usage.
- **The model never writes a URL.** Candidates get ids (`c1`…), search results get ids (`s1`…),
  and the editor cites those. Code maps ids to URLs. An id that doesn't exist is sent back for
  correction, and after two failed attempts it's removed rather than published.
- **Context is engineered to stay small.** See [Cost and context engineering](#cost-and-context-engineering).
- **Fetched content is treated as untrusted.** Article text and transcripts are wrapped in
  `untrusted="true"` tags, and the system prompt tells the model to report on page content, never
  follow instructions in it.
- **The house style is enforced twice.** The system prompt bans common machine-writing patterns
  (filler words, em dashes, emoji, rhetorical questions, moral-of-the-story endings), and
  `render.tidy()` strips dashes and markdown that slip through anyway.
- **No double sends.** Editions have a unique key on (cadence, date), deliveries have a unique key
  on (edition, subscriber), and the scheduler won't start a job that's already running. If the
  process dies after building but before sending, the next run sends the existing issue.

### Cost and context engineering

Every turn of the editor re-reads the whole conversation, so cost is driven by how big the
conversation gets and how many turns it takes. Measured on the same day's news:

| | Before (Opus 5.5, raw sources) | After (Haiku 5.5, condensed) |
|---|---|---|
| Estimated cost per daily issue | ~$1.06 | ~$0.025 |
| Peak prompt size | ~100K tokens | ~35K tokens |
| Editor turns | 9 | 4 |
| Run time | ~4 min | ~2 min |

What keeps it small:

1. **Condensed sources** (`app/agent/condense.py`). An article added 1,000–1,700 tokens to the
   conversation, and a video transcript about 12,000. Now each source goes through one
   independent Haiku call (effort `low`) that returns about 150 words of facts, aimed at the
   `focus` the editor asked for. The editor can still ask for `full` text when notes aren't
   enough, typically for the lead.
2. **Ids instead of URLs.** The briefing shows a short id and the domain for each candidate
   instead of the full URL, and citations use the ids. That saves tokens in both directions.
3. **A compact briefing.** 120 candidates instead of ~180, one line each, with only the first
   sentence of each feed snippet. That cut the briefing from ~26K to ~9K tokens.
4. **A context budget** (`CONTEXT_BUDGET_TOKENS`, default 80K). Once one request's prompt reaches
   it, research tools are closed and the editor is told to submit. Claude Haiku 5.5 bills
   prompts over 100K tokens at 5x the rate, so the budget keeps every request on the cheap rate.
5. **Fewer, fuller turns.** The prompt asks the editor to read its whole shortlist in parallel
   in one or two turns, because every extra turn re-reads everything.
6. **Prompt caching.** The system prompt and tool definitions are static, so they're cached
   across runs, and within a run each turn reads the earlier turns from cache.

The tradeoff: the cheaper setup reads fewer sources and works from notes, so it sometimes misses
a detail that only a second source carried. If that matters more than cost, set
`EDITOR_MODEL=claude-opus-5-5` and/or `EDITOR_EFFORT=high`.

### Claude API usage

- Editor: `EDITOR_MODEL` (default `claude-haiku-5-5`) with adaptive thinking and
  `EDITOR_EFFORT` (default `medium`). Condenser: `CONDENSER_MODEL` at effort `low`.
- Streaming requests (`client.beta.messages.stream(...).get_final_message()`), so the long
  final turn that writes the whole issue never hits an HTTP timeout.
- `submit_digest` uses strict tool use (`strict: true`), which constrains generation to the
  schema, so the issue always arrives as valid JSON. The result is still validated with
  pydantic, and if the SDK ever can't parse a tool call, the request is retried (up to twice).
- Server-side refusal fallback (`fallbacks="default"`) is sent only for models that support it
  (Opus 5.5, Opus 5, Sonnet 5.5, Fable 5.1). Claude Haiku 5.5 doesn't, so its refusals end the
  run with an error.
- Harness reminders ("turns left", "context budget used up") are sent as mid-conversation
  `system` messages, not mixed into tool results.
- Automatic prompt caching (`cache_control` on every request), and the conversation is
  append-only, which keeps the cache and earlier thinking blocks valid.

---

## What each file does

```
.
├── app/
│   ├── main.py            Web app (FastAPI): landing page, signup, confirm, preferences,
│   │                      unsubscribe, issue archive, admin page. Starts the scheduler on boot.
│   ├── pipeline.py        One issue end to end: build_edition() (collect → edit → store) and
│   │                      deliver_edition() (render → send → record). run_issue() does both.
│   ├── scheduler.py       APScheduler cron jobs for the daily and weekly issues.
│   ├── cli.py             Command line: collect, run, send, preview, add.
│   ├── config.py          All settings, read from environment variables / .env.
│   ├── db.py              SQLAlchemy models (Subscriber, Edition, CoveredStory, Delivery) and
│   │                      session handling. SQLite by default, any SQLAlchemy URL works.
│   ├── categories.py      The eight newsletter sections: key, label, blurb, and the scope text
│   │                      the editor uses to file stories. Change sections here.
│   ├── render.py          Turns a digest into per-subscriber HTML + plain text, and builds the
│   │                      confirmation email. tidy() enforces house style on output.
│   │
│   ├── agent/
│   │   ├── prompts.py     The editor's system prompt (editorial standards, process, accuracy
│   │   │                  rules, voice, banned phrases) and build_briefing(), which formats the
│   │   │                  per-issue message: date, targets, recently covered stories, candidates.
│   │   ├── condense.py    Turns one article, transcript or comment thread into ~150 words of
│   │   │                  notes with a small, separate model call.
│   │   ├── usage.py       Token accounting and estimated cost per run (prices per model).
│   │   ├── editor.py      The agent loop: tool definitions, the id registry (c12/s3 → URL),
│   │   │                  context budget, running tool calls in parallel,
│   │   │                  turn budget, URL verification, refusal / max_tokens handling.
│   │   └── schema.py      Pydantic models for the issue (Digest, LeadStory, Section, Story,
│   │                      QuickHit, WatchItem). Also produces the submit_digest tool schema.
│   │
│   ├── sources/
│   │   ├── registry.py    The source lists: RSS feeds (with ranking weights), subreddits,
│   │   │                  YouTube channel IDs, Hacker News points threshold.
│   │   └── collect.py     Collectors for each source type, junk filter, URL normalization,
│   │                      duplicate merging, ranking, collect_candidates().
│   │
│   ├── tools/             Plain functions; the editor wraps them as Claude tools.
│   │   ├── fetch.py       read_article(): download a page, extract article text (trafilatura).
│   │   ├── web_search.py  web_search(): Brave web/news search.
│   │   ├── transcripts.py get_transcript(): YouTube captions, optional local Whisper fallback.
│   │   ├── hn.py          hn_discussion(): a Hacker News story's points and top comments.
│   │   └── email.py       send_email() / send_batch(): Resend API, or write to outbox/ in
│   │                      console mode.
│   │
│   ├── templates/
│   │   ├── base.html            Site layout (header, footer, fonts).
│   │   ├── landing.html         Home page: pitch, signup form, latest-issue preview, FAQ.
│   │   ├── message.html         Generic confirmation/error page.
│   │   ├── preferences.html     Change cadence and sections.
│   │   ├── unsubscribe.html     Unsubscribe confirmation.
│   │   ├── archive.html         List of past issues.
│   │   ├── admin.html           Admin page: stats, build/send controls, previews.
│   │   └── email/
│   │       ├── issue.html       The newsletter email (table layout, inline styles).
│   │       ├── issue.txt        Plain-text version of the email.
│   │       └── confirm.html     Double opt-in confirmation email.
│   └── static/site.css    Styles for the website (not the emails, which are inline-styled).
│
├── requirements.txt          Python dependencies.
├── requirements-whisper.txt  Optional: faster-whisper + yt-dlp for the transcription fallback.
├── .env.example              Every setting, with comments. Copy to .env.
├── Dockerfile                Production image (Python 3.12, uvicorn, one worker).
└── docker-compose.yml        Single-service setup with a volume for the SQLite database.
```

Generated at runtime and git-ignored: `.env`, `data.db` (SQLite database), and `outbox/`
(emails written in console mode, plus CLI previews).

### Database tables

| Table | Purpose |
|---|---|
| `subscribers` | Email, cadence (`daily`/`weekly`), chosen sections, confirmed/active flags, and a secret token used in confirm, preferences and unsubscribe links. |
| `editions` | One row per issue (cadence + date): status (`drafting` → `ready` → `sent`, or `failed`), the digest JSON, error text, and stats (tool calls, tokens, timing). |
| `covered_stories` | URLs and headlines that have run, so later issues skip them (4-day lookback for daily, 21 for weekly). |
| `deliveries` | Who received which edition, with the provider message ID. Stops duplicate sends. |

Tables are created automatically on startup. There's no migration tool yet (see
[Development notes](#development-notes)).

---

## Quick start (local)

Requirements: Python 3.11+.

```bash
git clone https://github.com/farhanmukit0/tech_newsletter_ai.git
cd tech_newsletter_ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set at least `ANTHROPIC_API_KEY`. Change `ADMIN_TOKEN` to a long random string
(`openssl rand -hex 16`). Then start the app:

```bash
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000.

By default `EMAIL_BACKEND=console`, so **no email is actually sent**. Every email (confirmations
and issues) is saved as an HTML file in `outbox/` that you can open in a browser. That lets you
test the whole signup flow without an email provider.

> `--reload` restarts the server when code changes, which also restarts the scheduler. That's fine
> for development. Set `SCHEDULER_ENABLED=false` if you don't want scheduled runs while developing.

---

## Using it

### Make your first issue

```bash
python -m app.cli add you@example.com --cadence daily   # add yourself as a confirmed subscriber
python -m app.cli collect daily                        # preview what the collectors found (free, no Claude)
python -m app.cli run daily --no-send                  # build today's issue (calls Claude, ~4 min)
open outbox/edition-1.html                             # read it
python -m app.cli send 1 --to you@example.com          # send edition 1 to one address only
python -m app.cli send 1                               # send to every confirmed daily subscriber
```

### Command line reference

| Command | What it does |
|---|---|
| `python -m app.cli collect daily\|weekly` | Runs the collectors and prints the ranked candidates. No API calls, no database writes. |
| `python -m app.cli run daily\|weekly` | Builds today's issue and sends it to subscribers. Does nothing if today's issue already exists, except send it if it was built but never sent. |
| `python -m app.cli run daily --no-send` | Builds the issue only and writes a preview to `outbox/`. |
| `python -m app.cli run daily --force` | Rebuilds today's issue even if it exists, replacing the old one. |
| `python -m app.cli send <id>` | Sends an edition to every confirmed, active subscriber on its cadence who hasn't received it yet. |
| `python -m app.cli send <id> --to addr` | Sends an edition to one address only (a test send). Doesn't mark the edition as sent. |
| `python -m app.cli preview <id>` | Writes the edition's HTML and text to `outbox/edition-<id>.*`. |
| `python -m app.cli add <email> [--cadence weekly]` | Adds a confirmed subscriber, skipping the confirmation email. For testing. |

### Admin page

Go to `/admin?token=<ADMIN_TOKEN>`. From there you can:

- see subscriber counts (active daily, active weekly, unconfirmed);
- **build** an issue (preview first) or **build and send** in one step;
- open the **HTML** or **text** preview of any issue;
- **test-send** an issue to one address, or **send** it to everyone;
- see each issue's stats (editor turns, run time, token usage, tool calls) and any error.

Builds run in the background. Refresh the page to see progress.

The admin page is protected only by the token in the URL. Use a long random token, serve the site
over HTTPS, and don't share admin links.

### Public pages

| Path | Page |
|---|---|
| `/` | Landing page with signup form and a preview of the latest issue |
| `POST /subscribe` | Signup. Sends a confirmation email (double opt-in). Has a honeypot field and a per-IP rate limit (5 per 10 minutes). |
| `/confirm/<token>` | Confirms a subscription |
| `/preferences/<token>` | Change cadence and sections |
| `/unsubscribe/<token>` | Unsubscribe page. `POST` also handles one-click unsubscribe from mail clients (RFC 8058). |
| `/issues` | Archive of past issues |
| `/issues/<cadence>/<YYYY-MM-DD>` | Web version of an issue |
| `/healthz` | Health check for load balancers (`{"ok": true}`) |

### Schedule

With `SCHEDULER_ENABLED=true`, the app builds and sends:

- the **daily** issue every day at `DAILY_SEND_HOUR:DAILY_SEND_MINUTE`;
- the **weekly** issue on `WEEKLY_SEND_DAY` at `WEEKLY_SEND_HOUR`.

All times are in `TIMEZONE`. A daily issue takes a few minutes to build, so it arrives a few
minutes after the scheduled time. If the server was down at the scheduled time, the job still runs
when it comes back, within 3 hours (daily) or 6 hours (weekly).

---

## Configuration

All settings come from environment variables. Locally they're read from `.env`; see
`.env.example` for the full annotated list.

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none, required) | Claude API key. |
| `ANTHROPIC_WORKSPACE_ID` | (empty) | Only for keys not scoped to a workspace. The API's error message tells you when you need it. |
| `EDITOR_MODEL` | `claude-haiku-5-5` | Model for the editor agent. `claude-opus-5-5` is more thorough, at roughly 40x the cost. |
| `EDITOR_EFFORT` | `medium` | `low`, `medium`, `high`, `xhigh` or `max`. Higher means more thorough editing and more tokens. |
| `MAX_AGENT_TURNS` | `40` | Hard cap on editor turns per issue. Typical runs use 4–9. |
| `CONDENSER_MODEL` | `claude-haiku-5-5` | Model that condenses each source into notes for the editor. |
| `CONDENSE_SOURCES` | `true` | Set `false` to give the editor raw (truncated) text instead of notes. Costs more. |
| `CONTEXT_BUDGET_TOKENS` | `80000` | Research stops once one request's prompt reaches this size. Keep it under 100K on Haiku 5.5. |
| `CANDIDATE_LIMIT` | `120` | How many ranked candidates the editor chooses from. |
| `BRAVE_API_KEY` | (empty) | Brave Search key (free tier available). Without it the editor works from the feeds and the articles it reads. Recommended. |
| `EMAIL_BACKEND` | `console` | `console` writes emails to `outbox/`; `resend` sends them. |
| `RESEND_API_KEY` | (empty) | Resend API key. |
| `EMAIL_FROM` | `The Kernel <onboarding@resend.dev>` | Sender. Must be on a domain verified in Resend before you can email anyone except yourself. |
| `EMAIL_REPLY_TO` | (empty) | Where replies go. |
| `NEWSLETTER_NAME` / `NEWSLETTER_TAGLINE` | `The Kernel` / … | Branding used in the site, emails and system prompt. |
| `BASE_URL` | `http://localhost:8000` | Public URL. Every link in every email is built from this, so **set it in production**. |
| `TIMEZONE` | `America/New_York` | Timezone for schedules and issue dates. |
| `DATABASE_URL` | SQLite file `data.db` | Any SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@host:5432/db`. |
| `SCHEDULER_ENABLED` | `true` | Runs the in-process scheduler. |
| `DAILY_SEND_HOUR` / `DAILY_SEND_MINUTE` | `7` / `0` | When the daily issue goes out. |
| `WEEKLY_SEND_DAY` / `WEEKLY_SEND_HOUR` | `sun` / `8` | When the weekly issue goes out. |
| `ADMIN_TOKEN` | (empty) | Token for `/admin`. The admin page is disabled while this is empty. |
| `WHISPER_FALLBACK` / `WHISPER_MODEL` | `false` / `base` | Local transcription for videos without captions. Needs `requirements-whisper.txt` and ffmpeg. |

---

## Deploying

The app is a single process that serves the website and runs the scheduler, so it needs one
always-on container or server. Anything that can run a Docker container or a Python process works.

### Before you deploy

1. **Email domain.** In Resend, add and verify your domain (DNS records: SPF, DKIM, and ideally
   DMARC). Set `EMAIL_FROM` to an address on it, e.g. `The Kernel <hello@yourdomain.com>`, and set
   `EMAIL_BACKEND=resend`.
2. **Public URL.** Set `BASE_URL=https://yourdomain.com`, with no trailing slash.
3. **Secrets.** Set `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `BRAVE_API_KEY` and a long random
   `ADMIN_TOKEN` in your host's secret settings. Never commit them.
4. **Database.** SQLite is fine on a server with a persistent disk. On platforms whose disks are
   wiped on each deploy, use Postgres: set `DATABASE_URL` and add `psycopg[binary]` to
   `requirements.txt`.

### Option A: Docker on any server (VPS, EC2, Droplet, etc.)

```bash
git clone https://github.com/farhanmukit0/tech_newsletter_ai.git
cd tech_newsletter_ai
cp .env.example .env    # fill in production values
docker compose up -d --build
docker compose logs -f  # watch it start; look for "scheduled daily issue, next run ..."
```

The SQLite database lives in the `newsletter-data` Docker volume, so it survives rebuilds. To
update after pushing new code:

```bash
git pull && docker compose up -d --build
```

Put HTTPS in front of port 8000 with any reverse proxy. A minimal Caddy config:

```
yourdomain.com {
    reverse_proxy localhost:8000
}
```

### Option B: a container platform (Render, Railway, Fly.io, Cloud Run, etc.)

Point the platform at this repository; it builds from the `Dockerfile`. Then:

- Set the environment variables listed above in the platform's settings.
- Expose port **8000** and use `/healthz` as the health check path.
- Run **exactly one instance** and turn off scale-to-zero, or the in-process scheduler won't fire.
  If your platform sleeps idle apps or runs several instances, use Option C for scheduling.
- Give it persistent storage mounted at `/data` (SQLite), or set `DATABASE_URL` to a managed
  Postgres database.

### Option C: external scheduler (cron, platform cron jobs)

If you'd rather not run the scheduler inside the web process, set `SCHEDULER_ENABLED=false` on the
web service and trigger issues from cron or your platform's job scheduler:

```cron
# m h dom mon dow   (server time; adjust for your TIMEZONE)
0 7 * * *   cd /srv/tech_newsletter_ai && .venv/bin/python -m app.cli run daily
0 8 * * 0   cd /srv/tech_newsletter_ai && .venv/bin/python -m app.cli run weekly
```

The job must use the same `DATABASE_URL` as the web app. Running a job twice on the same day is
safe: the second run sees the existing issue and does nothing.

### Production checklist

- [ ] `BASE_URL` is your real HTTPS URL.
- [ ] Domain verified in Resend; `EMAIL_FROM` uses it; `EMAIL_BACKEND=resend`.
- [ ] `ADMIN_TOKEN` is long and random.
- [ ] Only one instance has `SCHEDULER_ENABLED=true`.
- [ ] Database is on persistent storage (volume or Postgres) and backed up.
- [ ] You've built an issue from `/admin`, previewed it, and test-sent it to yourself.
- [ ] You've signed up through the real landing page and received the confirmation email.
- [ ] Consider adding a physical mailing address to the email footer
      (`app/templates/email/issue.html`). Anti-spam laws such as CAN-SPAM require one for
      commercial email.

---

## Development notes

### Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # EMAIL_BACKEND=console is the default, so nothing gets emailed
uvicorn app.main:app --reload --port 8000
```

Useful loops:

- **Work on collectors:** `python -m app.cli collect daily` (free and fast; nothing touches Claude).
- **Work on email templates:** build one issue once, then edit `app/templates/email/issue.html` and
  run `python -m app.cli preview <id>`. No model calls are needed to iterate on design.
- **Work on the prompt:** `python -m app.cli run daily --no-send --force` rebuilds today's issue.
  Each run costs about $0.03 at default settings (about $1 with Opus). Compare
  `outbox/edition-<id>.txt` between runs; the admin page shows each run's cost.
- **Use a scratch database:** `DATABASE_URL=sqlite:////tmp/scratch.db python -m app.cli ...` keeps
  experiments out of your real `data.db`.

### Common changes

- **Add or remove a source:** edit the lists in `app/sources/registry.py`. RSS feeds take a weight
  (about 0.6–1.2) that nudges ranking; use higher weights for outlets that break news. YouTube
  needs the channel ID (starts with `UC`), not the handle. Failing sources are logged and skipped,
  so a bad URL won't break an issue.
- **Change the sections:** edit `CATEGORIES` in `app/categories.py`. The key is stored on
  subscribers and digests; the scope text tells the editor what belongs there. The signup form,
  preferences page, system prompt and schema all pick up changes automatically. If you rename or
  remove a key, existing subscribers who chose it simply won't see that section.
- **Tune the writing:** the editorial brief is `SYSTEM_PROMPT` in `app/agent/prompts.py`. Story
  counts per issue are in `TARGETS` in the same file. Field-level guidance (headline length,
  summary length) lives in the `Field(description=...)` strings in `app/agent/schema.py`, which
  become part of the tool schema the model sees.
- **Add an agent tool:** write a plain function in `app/tools/`, add its definition to `TOOLS` in
  `app/agent/editor.py`, and add a branch in `_Session.run()`. If the tool returns URLs the editor
  might cite, register them with `self._new_search_ref(_Ref(url, title, source))` and show the
  returned id to the model, or citations of them will be rejected. Return condensed text (via
  `self._condense`), not raw pages, to keep the context small. Mention
  when to use it in the "How to work" part of the system prompt.
- **Change the email design:** `app/templates/email/issue.html`. Keep the table layout and inline
  styles; many email clients (Outlook, Gmail) ignore stylesheets and modern CSS. Update
  `issue.txt` to match.

### Things to know

- **Keep the system prompt static.** Anything that changes per run (date, candidates, recently
  covered stories) belongs in `build_briefing()`, not `SYSTEM_PROMPT`. A changing system prompt
  breaks prompt caching across runs.
- **Keep the conversation append-only.** `run_editor()` only ever appends to `messages`. Editing or
  removing earlier turns invalidates the prompt cache and the model's earlier thinking blocks.
- **Variable names.** The model settings are `EDITOR_MODEL` / `EDITOR_EFFORT`, not
  `CLAUDE_MODEL` / `CLAUDE_EFFORT`, because Claude Code sets `CLAUDE_EFFORT` in its own shells and
  would silently override the setting.
- **Run state is stored on the issue.** Each edition's `stats` column has tool call counts, token
  usage, run time, source failures and any dropped URLs. Check it (or the admin page) first when an
  issue looks wrong.
- **Rebuilds are clean.** `--force` deletes that edition's `covered_stories` rows before
  collecting, so a rebuild doesn't treat its own stories as already covered.
- **Schema changes.** Tables are created with `create_all()`, which adds new tables but never
  changes existing ones. If you change a model in `db.py`, delete `data.db` in development, or add
  Alembic before you have production data you care about.
- **Thread safety.** When the model makes several tool calls in one turn, `_Session.run()` runs
  them in a thread pool (up to 8 at once). Shared state is limited to the id registry (new search
  ids are assigned under a lock), the usage tracker (also locked) and a dict of call counts. New
  tools should not add other shared mutable state.
- **No automated tests yet.** The agent loop was verified with a fake client that replays scripted
  tool calls, which is the best place to start a test suite: it exercises tool dispatch, URL
  verification and the pipeline without spending API credits. Template rendering and the signup
  flow (via FastAPI's `TestClient`) are also easy to cover.

### Ideas for later

- Reddit OAuth app, for reliable Reddit data (anonymous access is heavily rate-limited).
- Click and open tracking via Resend webhooks.
- Alembic migrations.
- A proper admin login instead of the URL token.
- Per-subscriber topic weighting beyond on/off sections.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `This API key is not scoped to a workspace...` | Create a key inside a workspace in the Anthropic Console, or set `ANTHROPIC_WORKSPACE_ID` (starts with `wrkspc_`). |
| Issue status `failed` on the admin page | The error is shown next to the issue. Common causes: missing API key, a model refusal, or the turn budget running out (raise `MAX_AGENT_TURNS`). |
| `reddit r/... failed: 429` in the logs | Reddit rate-limits anonymous clients. Harmless; the issue is built without Reddit. |
| Video transcripts fail on a cloud server | YouTube sometimes blocks cloud IP ranges. The editor skips that video. `youtube-transcript-api` supports proxies if you need transcripts there. |
| `read_article` errors for some sites | Paywalls and JavaScript-only pages can't be read. The editor is told to find other coverage instead. |
| Resend error about the sender domain | Verify your domain in Resend, or send test emails only to your own Resend account address. |
| Emails link to `localhost` | `BASE_URL` isn't set in production. |
| Scheduled issues never arrive | Check that `SCHEDULER_ENABLED=true` on exactly one always-on instance and look for `scheduled daily issue, next run ...` at startup. On platforms that sleep idle apps, use cron (Option C). |
| `nothing to do (issue already exists; use --force)` | Today's issue was already built. Use `--force` to rebuild, or `send <id>` to send the existing one. |
| Changed `CLAUDE_EFFORT` but nothing changed | The setting is `EDITOR_EFFORT` (see Development notes). |
