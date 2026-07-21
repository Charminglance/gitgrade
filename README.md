# gitgrade

Enter a public GitHub username, get an evidence-based read of the profile:
primary stack, depth vs breadth, strengths and gaps (each tied to specific
repos, not generic boilerplate), and concrete next steps — backed by real
repo metadata (READMEs, file listings, dependency files, languages) and
Gemini reasoning.

**Live:** https://gitgrade.safeel.in

## Structure

```
gitgrade/
├── vercel.json          Vercel routing: static frontend + Python API
├── requirements.txt      Root deps, read by Vercel's Python builder
├── api/
│   └── index.py          Vercel serverless entrypoint (re-exports the Flask app)
├── backend/
│   ├── app.py             Flask app — GitHub fetch, preprocessing, Gemini call
│   └── requirements.txt   Same deps + gunicorn, for local/Render use
└── frontend/
    └── index.html         Self-contained dashboard (no build step)
```

## Local development

```bash
cd backend
python -m venv venv
venv\Scripts\Activate.ps1      # Windows; use `source venv/bin/activate` on Mac/Linux
pip install -r requirements.txt
```

Create `backend/.env` (gitignored, never committed) with:
```
GEMINI_API_KEY=your_key_here
GITHUB_TOKEN=your_token_here     # optional, raises GitHub rate limit 60 -> 5000/hr
```

Run the backend:
```bash
python app.py
```

Open `frontend/index.html` directly in a browser — it auto-detects
localhost and points at `http://localhost:5000/api` when running locally,
or the production API otherwise.

## Deployment (Vercel)

1. Push to GitHub
2. Import the repo on vercel.com, root directory set to `./` (repo root)
3. Add environment variables in the Vercel dashboard: `GEMINI_API_KEY`, `GITHUB_TOKEN`
4. Deploy — `vercel.json` routes `/api/*` to the Python function and
   everything else to the static frontend

## API

`GET /api/analyze/<username>` → returns profile + analysis JSON.
`GET /api/analyze/<username>?force=true` → bypasses the 1-hour cache.
`GET /api/health` → health check.

## How the analysis works

- Repos are capped to the 25 most recently updated, to keep the prompt
  small. Forks are excluded.
- For each repo, the backend pulls language breakdown, a README snippet,
  the top-level file listing, and detected dependency files
  (`package.json`, `requirements.txt`, `Dockerfile`, etc.) — not just
  booleans, so Gemini has real evidence to reason from.
- The prompt requires every strength, gap, and suggestion to name the
  specific repo(s) it's based on, and bans generic advice that could
  apply to any profile ("add tests", "write a README") in favor of
  reasoning tied to what a specific project actually needs.
- Results are cached in-memory per username for 1 hour.

## Notes

- The in-memory cache resets on each serverless cold start on Vercel —
  acceptable for this project's traffic level, but worth knowing if
  results seem to "forget" between visits.
- Numeric ages/day-counts from GitHub's `created_at` are intentionally
  excluded from the analysis, since repos with imported/reinitialized
  history can report misleading creation dates.

## Ideas for later

- Move the cache to something external (Upstash Redis) so it survives
  cold starts
- "Compare two profiles" mode
- Placement-readiness scoring tuned for student profiles
