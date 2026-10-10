# The Kernel

A tech newsletter that writes itself, carefully. On a schedule, it collects recent stories from
about twenty outlets, Hacker News, Reddit and a set of YouTube channels, hands the candidates to a
Claude-powered editor agent that reads, verifies and writes up only the stories that matter, and
emails the result to subscribers, sorted into the sections each reader picked.

## How an issue gets made

```
collectors ──> ~150 ranked candidates ──> editor agent (Claude) ──> Digest ──> per-reader email
 RSS, HN,       deduped across outlets,     read_article             validated,   only the sections
 Reddit,        past coverage removed       web_search               links        they chose
 YouTube                                    get_video_transcript     checked
                                            hn_discussion
                                            submit_digest
```

1. **Collect** (`app/sources/`). Deterministic and cheap. Pulls the coverage window (30 hours for
   daily, 7 days for weekly), drops deal posts and other junk, merges the same story across outlets,
   removes anything already covered in a recent issue, and ranks by cross-outlet coverage and
   engagement.
2. **Edit** (`app/agent/`). Claude Opus 5.5 gets the candidates and a detailed editorial brief
   (`app/agent/prompts.py`). It triages, reads sources, searches to verify and fill gaps, transcribes
   promising videos, checks Hacker News reaction, then submits a structured issue. Every URL it cites
   is checked against what it actually saw; unknown links get bounced back for correction.
3. **Deliver** (`app/pipeline.py`). The issue is stored, rendered per subscriber (HTML and plain
   text), and sent through Resend with one-click unsubscribe headers.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY at minimum
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 for the landing page.

With `EMAIL_BACKEND=console` (the default) nothing is emailed. Every email is written to `outbox/`
as an HTML file you can open in a browser, so you can test the whole signup flow locally.

### Make your first issue

```bash
python -m app.cli add you@example.com --cadence daily   # a confirmed test subscriber
python -m app.cli collect daily                        # see what the collectors found (free)
python -m app.cli run daily --no-send                  # build an issue (calls Claude)
open outbox/edition-1.html                             # look at it
python -m app.cli send 1 --to you@example.com          # send it to one address
```

Or use the admin page at `http://localhost:8000/admin?token=<ADMIN_TOKEN>` to build, preview
(HTML and text), test-send and send issues.

A daily run typically takes several minutes: the editor reads a few dozen articles.

## Configuration

Everything is in `.env`; see `.env.example` for the full list.

| Variable | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | Required. |
| `EDITOR_MODEL` / `EDITOR_EFFORT` | Defaults to `claude-opus-5-5` at `high` effort. `medium` is cheaper and faster; `xhigh` is more thorough. |
| `BRAVE_API_KEY` | Web search for the editor. Free tier at brave.com/search/api. Without it the editor works from feeds and articles only. |
| `EMAIL_BACKEND` | `console` (write to `outbox/`) or `resend`. |
| `RESEND_API_KEY`, `EMAIL_FROM` | Resend credentials and sender. Until you verify a domain, Resend only delivers to your own account address. |
| `BASE_URL` | Public URL, used in every link inside emails. Set it when you deploy. |
| `DAILY_SEND_HOUR`, `WEEKLY_SEND_DAY`, `TIMEZONE` | When issues go out. |
| `DATABASE_URL` | SQLite by default; any SQLAlchemy URL works (Postgres recommended in production). |
| `WHISPER_FALLBACK` | Transcribe videos with no captions locally (needs `requirements-whisper.txt` and ffmpeg). |

To change sources, edit `app/sources/registry.py`. To change sections, edit `app/categories.py`
(the agent's brief and the signup form pick them up automatically).

## Deploying

The app is a single container:

```bash
docker compose up -d --build
```

Things to know:
- **Run exactly one instance with the scheduler on.** The in-process scheduler fires the daily and
  weekly jobs. If you'd rather use cron or a platform scheduler, set `SCHEDULER_ENABLED=false` and
  call `python -m app.cli run daily` / `run weekly` on your schedule. A unique key on
  (cadence, date) stops an issue from being built or sent twice either way.
- Set `BASE_URL` to your real domain, verify that domain in Resend, and set `EMAIL_FROM` to an
  address on it.
- Use Postgres (`DATABASE_URL`) on platforms with ephemeral disks. You'll need to add the driver,
  e.g. `pip install "psycopg[binary]"`.
- Put it behind HTTPS (any platform load balancer, Caddy, or nginx).

## Notes and limits

- **Reddit** rate-limits anonymous clients aggressively, so the Reddit collector sometimes gets
  nothing. Every other source still works, and a failing source never blocks an issue. For reliable
  Reddit data, register a Reddit app and switch `fetch_reddit` to OAuth.
- **Paywalled articles** can't be read; the editor is told to find other coverage instead.
- **YouTube captions** are fetched from YouTube directly. Cloud IPs sometimes get blocked; the
  editor treats that as a failed tool call and moves on. `youtube-transcript-api` supports proxies
  if you need them.
- **Cost**: a daily issue is one agent run per day, shared by all subscribers, so cost doesn't grow
  with your list. Token usage for each issue is shown on the admin page.

## Project layout

```
app/
  main.py            web app: landing, signup, confirm, preferences, unsubscribe, archive, admin
  pipeline.py        collect -> edit -> store -> deliver
  scheduler.py       daily/weekly jobs
  cli.py             command line
  agent/
    prompts.py       the editor's system prompt and per-issue briefing
    editor.py        tool loop, URL verification, Claude API calls
    schema.py        the Digest structure the editor submits
  sources/           feed registry and collectors
  tools/             web search, article reader, transcripts, HN, email
  templates/         landing and account pages, email templates
```
