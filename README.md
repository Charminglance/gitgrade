# Skillprint — GitHub Skill Fingerprint Analyzer

Enter a public GitHub username, get an evidence-based "skill fingerprint":
primary stack, depth vs breadth, strengths, gaps, and concrete next-project
suggestions — backed by real repo metadata + Gemini reasoning.

## Structure

```
backend/
  app.py            Flask API — GitHub fetch, preprocessing, Gemini call
  requirements.txt
  .env.example
frontend/
  index.html        Self-contained dashboard (no build step)
```

## Setup

### 1. Backend

```bash
cd backend
pip install -r requirements.txt

export GITHUB_TOKEN=ghp_xxx      # optional, raises rate limit from 60 to 5000/hr
export GEMINI_API_KEY=xxx        # required — get one at https://aistudio.google.com/apikey

python app.py
```

Runs on `http://localhost:5000`.

### 2. Frontend

Just open `frontend/index.html` in a browser (or serve it with any static
server). It calls the backend at `http://localhost:5000/api`.

## API

`GET /api/analyze/<username>` → returns profile + fingerprint JSON.
`GET /api/analyze/<username>?force=true` → bypasses the 1-hour cache.
`GET /api/health` → health check.

## Notes

- Repos are capped to the 25 most recently updated, to keep the Gemini
  prompt small and cheap.
- Results are cached in-memory per username for 1 hour — restart the
  server or use `?force=true` to refresh.
- Fork repos are excluded from analysis.
- The Gemini call requests structured JSON output directly (no manual
  markdown-fence stripping needed).

## Next steps / ideas

- Swap the in-memory cache for SQLite so results survive restarts.
- Add a "compare two profiles" mode.
- Add a placement-readiness score tuned for student profiles.
