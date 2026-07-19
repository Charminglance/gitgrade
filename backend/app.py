"""
gitgrade — backend
Fetches a user's public GitHub repos, preprocesses them into compact
metadata, then asks Gemini to synthesize a read of the profile:
primary stack, depth vs breadth, strengths, gaps, and next-project
suggestions.

Run:
    pip install -r requirements.txt
    export GITHUB_TOKEN=ghp_xxx        # optional but raises rate limit 60->5000/hr
    export GEMINI_API_KEY=xxx
    python app.py
"""

import os
import time
import json
import base64
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

# Simple in-memory cache: {username: (timestamp, result)}
CACHE = {}
CACHE_TTL_SECONDS = 60 * 60  # 1 hour


def gh_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def fetch_user(username):
    r = requests.get(f"{GITHUB_API}/users/{username}", headers=gh_headers(), timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def fetch_repos(username):
    repos = []
    page = 1
    while True:
        r = requests.get(
            f"{GITHUB_API}/users/{username}/repos",
            headers=gh_headers(),
            params={"per_page": 100, "page": page, "type": "owner", "sort": "updated"},
            timeout=15,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
        if page > 5:  # safety cap: 500 repos max
            break
    return [r for r in repos if not r.get("fork")]


def fetch_readme_snippet(username, repo_name):
    try:
        r = requests.get(
            f"{GITHUB_API}/repos/{username}/{repo_name}/readme",
            headers=gh_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            return ""
        content = r.json().get("content", "")
        decoded = base64.b64decode(content).decode("utf-8", errors="ignore")
        return decoded[:400]
    except Exception:
        return ""


def fetch_languages(username, repo_name):
    try:
        r = requests.get(
            f"{GITHUB_API}/repos/{username}/{repo_name}/languages",
            headers=gh_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            return {}
        return r.json()
    except Exception:
        return {}


def has_workflows(username, repo_name):
    try:
        r = requests.get(
            f"{GITHUB_API}/repos/{username}/{repo_name}/contents/.github/workflows",
            headers=gh_headers(),
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def has_tests(username, repo_name):
    try:
        r = requests.get(
            f"{GITHUB_API}/repos/{username}/{repo_name}/contents",
            headers=gh_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            return False
        names = [item.get("name", "").lower() for item in r.json() if isinstance(item, dict)]
        return any(n in ("test", "tests", "__tests__", "spec") for n in names)
    except Exception:
        return False


def preprocess_repo(username, repo):
    name = repo["name"]
    languages = fetch_languages(username, name)
    primary_lang = max(languages, key=languages.get) if languages else (repo.get("language") or "Unknown")

    updated = repo.get("updated_at")
    created = repo.get("created_at")
    days_active = None
    if updated and created:
        d1 = datetime.fromisoformat(created.replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        days_active = max((d2 - d1).days, 0)

    return {
        "name": name,
        "description": (repo.get("description") or "")[:150],
        "primaryLang": primary_lang,
        "languages": list(languages.keys())[:5],
        "hasReadme": bool(fetch_readme_snippet(username, name)),
        "hasTests": has_tests(username, name),
        "hasCI": has_workflows(username, name),
        "stars": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "topics": repo.get("topics", [])[:6],
        "daysActive": days_active,
        "lastCommit": updated,
        "isArchived": repo.get("archived", False),
    }


def build_gemini_prompt(username, repos_meta):
    return f"""You are analyzing the public GitHub profile of "{username}" to produce an
honest, evidence-based read of their engineering profile based ONLY on the
repo metadata below. Do not invent facts not supported by the data. If
evidence is thin, say so rather than guessing.

Repo metadata (JSON array, one object per repo):
{json.dumps(repos_meta, indent=2)}

IMPORTANT — numeric accuracy: do not state specific day counts, ages, or
durations (e.g. "600 days", "8 months") anywhere in your response, even
though "daysActive" appears in the data. That field is unreliable (it
reflects GitHub's created_at, which can predate actual work due to
imports/reinitialized history) and you are prone to misreading it. Use
only qualitative, safely-hedged descriptions of activity ("actively
maintained", "recently started", "long-dormant") instead of any number.

Respond with ONLY a JSON object (no markdown fences, no preamble) matching
exactly this schema:
{{
  "primaryStack": "short string, e.g. 'Full-stack leaning frontend: React + Node, one Flask project'",
  "depthVsBreadth": "1-2 sentence verdict on whether the profile shows deep focused work or broad shallow exploration",
  "consistencyScore": <integer 0-100, based on activity spread and repo count/quality>,
  "skillDimensions": {{
    "frontend": <0-100>,
    "backend": <0-100>,
    "devops": <0-100>,
    "testing": <0-100>,
    "documentation": <0-100>,
    "projectBreadth": <0-100>
  }},
  "strengths": ["short phrase", "short phrase", "short phrase"],
  "gaps": ["short phrase", "short phrase"],
  "suggestions": [
    "concrete next-project suggestion tied to an identified gap",
    "concrete next-project suggestion tied to an identified gap",
    "concrete next-project suggestion tied to an identified gap"
  ]
}}
"""


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "response_mime_type": "application/json",
        },
    }
    r = requests.post(GEMINI_URL, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


@app.route("/api/analyze/<username>", methods=["GET"])
def analyze(username):
    force = request.args.get("force", "false").lower() == "true"

    cached = CACHE.get(username)
    if cached and not force and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
        return jsonify(cached[1])

    user = fetch_user(username)
    if user is None:
        return jsonify({"error": f"GitHub user '{username}' not found"}), 404

    repos = fetch_repos(username)
    if not repos:
        return jsonify({"error": f"'{username}' has no public non-fork repositories"}), 404

    # Cap to the 25 most recently updated repos to keep the prompt small
    repos = repos[:25]
    repos_meta = [preprocess_repo(username, r) for r in repos]

    try:
        prompt = build_gemini_prompt(username, repos_meta)
        analysis = call_gemini(prompt)
    except Exception as e:
        return jsonify({"error": f"Analysis failed: {str(e)}"}), 502

    result = {
        "username": username,
        "profile": {
            "avatarUrl": user.get("avatar_url"),
            "name": user.get("name") or username,
            "bio": user.get("bio"),
            "publicRepos": user.get("public_repos"),
            "followers": user.get("followers"),
        },
        "repoCount": len(repos_meta),
        "analysis": analysis,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }

    CACHE[username] = (time.time(), result)
    return jsonify(result)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)