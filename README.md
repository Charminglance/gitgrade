# gitgrade

Enter a public GitHub username, get an evidence-based read of the profile:
primary stack, depth vs breadth, strengths and gaps tied to specific repos,
placement-readiness scoring for student/early-career hiring, and concrete
next steps — backed by real repo metadata and Gemini reasoning.

**Live:** https://gitgrade.safeel.in

## Features

- **Shareable results** — `/u/<username>` auto-runs the analysis on load;
  a Copy Link button on the result card shares the same URL. Browser
  back/forward works.
- **Try buttons** — `gaearon` / `sindresorhus` / `Charminglance` pre-fill
  and run instantly from the hero for zero-friction demoing.
- **Compare mode** — after a result loads, open a second input and get a
  side-by-side card of both profiles' scores, with the higher value on
  each row highlighted.
- **Score color coding + count-up** — placement/consistency scores animate
  from 0 on load and color green (≥70) / amber (40–69) / red (<40).
- **Cached badge** — result card shows `cached` when served from the
  1-hour in-memory cache, with a Refresh button to force a fresh run.
- **Avatar → GitHub** — clicking the profile picture opens their GitHub
  profile in a new tab. Bio renders under the name.
- **Placement readiness disclaimer** — a one-line note under the score
  clarifying it judges repo hygiene against entry-level hiring signals,
  not overall skill or seniority (added after high-profile accounts with
  no tests/CI scored surprisingly low despite real technical merit).

## Structure

```
gitgrade/
├── vercel.json          Vercel routing: static frontend + Python API
├── requirements.txt      Deps for both local dev and Vercel's Python builder
├── api/
│   └── index.py          Vercel serverless entrypoint
├── backend/
│   └── app.py             Flask app — GitHub fetch, preprocessing, Gemini call
└── frontend/
    └── index.html         Dashboard (no build step)
```

## Local development

```bash
python -m venv venv
venv\Scripts\Activate.ps1      # Mac/Linux: source venv/bin/activate
pip install -r requirements.txt
cd backend
```

Create `backend/.env` (gitignored):

```
GEMINI_API_KEY=your_key_here
GITHUB_TOKEN=your_token_here     # optional, raises GitHub rate limit 60 -> 5000/hr
```

```bash
python app.py
```

Open `frontend/index.html` in a browser — it auto-detects localhost vs
production and points at the right API automatically.

## Deployment

Hosted on Vercel. `vercel.json` routes `/api/*` to the Python function and
everything else to the static frontend. Environment variables
(`GEMINI_API_KEY`, `GITHUB_TOKEN`) are set in the Vercel dashboard.

## API

- `GET /api/analyze/<username>` — profile + analysis JSON
- `GET /api/analyze/<username>?force=true` — bypasses the 1-hour cache
- `GET /api/health` — health check

## How it works

Fetches a username's public, owner (non-fork) repos sorted by last-updated,
then takes the top **10** for analysis. For each, gathers language
breakdown, a README snippet, top-level file listing, detected dependency
files (`package.json`, `requirements.txt`, `Dockerfile`, etc.), license,
whether CI is configured, and whether tests exist anywhere in the repo tree
(not just a top-level `tests/` folder — walks the full tree recursively).

Also pulls the user's **merged pull requests on repos they don't own**
(via the GitHub Search API) as a separate "real open-source contribution"
signal, distinct from their own projects.

Gemini is prompted to ground every claim in a named repo, weigh external
contributions positively when present, and avoid generic advice — plus a
separate placement-readiness score judging the profile the way a campus
recruiter's 30-second skim would, distinct from the general technical read.

## Capacity

Each uncached analysis makes ~52 GitHub "core" API calls (5 per repo × 10
repos, plus user + repo-list lookups) and 1 Search API call. With
`GITHUB_TOKEN` set (5,000 req/hr), that's roughly **~95 uncached analyses
per hour** before hitting GitHub's rate limit — should be fine for a
launch-video spike. Without a token (60 req/hr), it's effectively ~1
analysis/hour for the whole app — always set the token in production.

The tighter real-world constraint is likely Vercel's function timeout, not
the rate limit: the 52 GitHub calls run sequentially (not parallelized),
plus Gemini's own 10–20s reasoning time, so a single request can run long.
Worth checking `maxDuration` before assuming rate limits are the ceiling.

## Known gaps

- **Cache doesn't survive cold starts on Vercel.** `CACHE` is an in-memory
  dict in the serverless function; each cold start wipes it, so the
  1-hour TTL rarely does anything in production. Would need Vercel KV or
  similar to actually persist.
- **Only top 10 repos are analyzed**, not the full recently-updated set,
  and no pinned-repos signal — a user's most-recently-updated repos aren't
  always their best/most representative work.
- **Per-repo GitHub calls run sequentially**, not in parallel — this is
  the main lever for both faster responses and higher effective capacity
  before Vercel's function timeout kicks in.
