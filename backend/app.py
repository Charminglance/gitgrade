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
from dotenv import load_dotenv

load_dotenv()

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


def fetch_top_level_files(username, repo_name):
    try:
        r = requests.get(
            f"{GITHUB_API}/repos/{username}/{repo_name}/contents",
            headers=gh_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            return []
        return [item.get("name", "") for item in r.json() if isinstance(item, dict)]
    except Exception:
        return []


def has_tests(username, repo_name, top_level_files=None):
    names = top_level_files if top_level_files is not None else fetch_top_level_files(username, repo_name)
    lower = [n.lower() for n in names]
    return any(n in ("test", "tests", "__tests__", "spec") for n in lower)


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

    top_level_files = fetch_top_level_files(username, name)
    readme_snippet = fetch_readme_snippet(username, name)

    dependency_signals = [
        f for f in top_level_files
        if f.lower() in (
            "package.json", "requirements.txt", "pyproject.toml", "pipfile",
            "dockerfile", "docker-compose.yml", "go.mod", "cargo.toml",
            "pom.xml", "build.gradle", "gemfile",
        )
    ]

    return {
        "name": name,
        "description": (repo.get("description") or "")[:150],
        "primaryLang": primary_lang,
        "languages": list(languages.keys())[:5],
        "readmeSnippet": readme_snippet[:400] if readme_snippet else "",
        "topLevelFiles": top_level_files[:20],
        "dependencySignals": dependency_signals,
        "hasTests": has_tests(username, name, top_level_files),
        "hasCI": has_workflows(username, name),
        "stars": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "topics": repo.get("topics", [])[:6],
        "daysActive": days_active,
        "lastCommit": updated,
        "isArchived": repo.get("archived", False),
    }


def build_gemini_prompt(username, repos_meta):
    return f"""You are a senior engineer doing a real technical read of "{username}"'s
GitHub profile — the kind of close read a hiring engineer or a technical
co-founder would do before deciding to work with this person. This is not
a generic portfolio summary. Base every claim ONLY on the repo metadata
below (README snippets, file listings, dependency files, languages,
topics). Do not invent facts. If evidence is thin for a claim, say so
explicitly rather than padding with generic advice.

Repo metadata (JSON array, one object per repo — includes readmeSnippet,
topLevelFiles, and dependencySignals like package.json/requirements.txt):
{json.dumps(repos_meta, indent=2)}

HARD RULES — violating these makes the analysis useless, avoid them:
1. Every strength, gap, and suggestion MUST name the specific repo(s) it's
   based on. Never write an unattributed claim like "lacks testing" —
   write "no test directory or CI config found in papertrail-server,
   MedScan-AI, or any other repo" instead.
2. Do NOT give generic boilerplate advice that applies to any GitHub
   profile ("add tests", "write a README", "set up CI"). Instead, tie each
   suggestion to what the actual code/stack in a specific repo is missing
   relative to what that project needs to be production-credible. E.g.
   instead of "add CI to MedScan-AI", say something like "MedScan-AI has
   a Flask backend with no requirements.txt pinning and no CI — for a
   medical-data project specifically, that's a bigger risk than for a
   typical side project, since reproducibility matters more."
3. Use readmeSnippet content to judge actual project scope and maturity,
   not just its existence. A one-line README and a detailed one with
   setup instructions are very different signals — say which is which,
   by name.
4. Use dependencySignals and topLevelFiles to infer real technical choices
   (e.g. "requirements.txt present but no version pinning", "package.json
   present but no lockfile committed") rather than only looking at the
   primaryLang field.
5. If two or more repos show a repeated pattern (e.g. always frontend-
   heavy, always missing tests), say so as a PATTERN across named repos —
   that's more valuable than restating the same gap per-repo.
6. Do not state specific day counts, ages, or durations (e.g. "600 days",
   "8 months") anywhere — the daysActive field is unreliable. Use only
   qualitative descriptions of activity instead.

Respond with ONLY a JSON object (no markdown fences, no preamble) matching
exactly this schema:
{{
  "primaryStack": "specific string naming actual repos as evidence, e.g. 'Frontend-leaning: React in papertrail, vanilla JS in gitgrade; one Flask backend in MedScan-AI with no test coverage'",
  "depthVsBreadth": "2-3 sentences, naming which specific repos represent depth (long-term/complex) vs which represent breadth (shallow/experimental), and what that split implies",
  "consistencyScore": <integer 0-100, based on activity spread and repo count/quality>,
  "skillDimensions": {{
    "frontend": <0-100>,
    "backend": <0-100>,
    "devops": <0-100>,
    "testing": <0-100>,
    "documentation": <0-100>,
    "projectBreadth": <0-100>
  }},
  "strengths": [
    "specific claim naming the repo(s) it's based on",
    "specific claim naming the repo(s) it's based on",
    "specific claim naming the repo(s) it's based on"
  ],
  "gaps": [
    "specific claim naming the repo(s) it's based on, framed as a pattern if it recurs across repos",
    "specific claim naming the repo(s) it's based on"
  ],
  "suggestions": [
    "concrete, non-generic next step tied to a named repo's actual missing piece — explain WHY it matters for that specific project, not just what to add",
    "concrete, non-generic next step tied to a named repo's actual missing piece — explain WHY it matters for that specific project, not just what to add",
    "concrete, non-generic next step tied to a named repo's actual missing piece — explain WHY it matters for that specific project, not just what to add"
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