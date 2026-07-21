# gitgrade

Enter a public GitHub username, get an evidence-based read of the profile:
primary stack, depth vs breadth, strengths and gaps tied to specific repos,
placement-readiness scoring for student/early-career hiring, and concrete
next steps — backed by real repo metadata and Gemini reasoning.

**Live:** https://gitgrade.safeel.in

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

Pulls the 25 most recently updated public, non-fork repos for a username.
For each, gathers language breakdown, a README snippet, top-level file
listing, and detected dependency files (`package.json`,
`requirements.txt`, `Dockerfile`, etc.). Gemini is prompted to ground every
claim in a named repo and avoid generic advice — plus a separate
placement-readiness score judging the profile the way a campus recruiter's
30-second skim would, distinct from the general technical read.
