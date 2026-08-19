"""
gitgrade — backend
Fetches a user's public GitHub repos, preprocesses them into compact
metadata, then asks Gemini to synthesize a read of the profile.
"""

import os
import time
import json
import base64
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
# Remove the key from the URL
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
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
        if page > 5:
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

Additionally, score PLACEMENT READINESS — how this profile would read to a
campus placement panel or an off-campus tech recruiter screening a
student/early-career candidate, NOT a senior engineer's general opinion.
This is a distinct lens from the technical analysis above:
- Recruiters screening students skim fast: pinned repos, README clarity,
  and whether a project's PURPOSE is obvious in under 30 seconds matter
  more than architectural sophistication.
- A working live demo/deployed link is worth more here than clever code
  with no way to see it run.
- Breadth across a few different domains (web, AI, systems) reads better
  for placements than many near-identical projects.
- Repo/commit names like "test", "untitled", "final2" hurt more at this
  stage than they would for a senior engineer's profile, since they
  signal how a student presents work under scrutiny.
- Do not penalize lack of enterprise-grade DevOps (Kubernetes, complex CI)
  — that's not what placement panels expect from a student profile.

Respond with ONLY a JSON object (no markdown fences, no preamble) matching
exactly this schema:
{{
  "primaryStack": "specific string naming actual repos as evidence, e.g. 'Frontend-leaning: React in papertrail, vanilla JS in gitgrade; one Flask backend in MedScan-AI with no test coverage'",
  "depthVsBreadth": "2-3 sentences, naming which specific repos represent depth (long-term/complex) vs which represent breadth (shallow/experimental), and what that split implies",
  "consistencyScore": <integer 0-100, based on activity spread and repo count/quality>,
  "placementReadiness": {{
    "score": <integer 0-100, specifically for campus/early-career hiring context, not general engineering merit>,
    "verdict": "2-3 sentences on how this profile would land with a placement panel or recruiter in a 30-second skim, naming specific repos",
    "quickWins": [
      "a specific, fast (hours not weeks) fix tied to a named repo that would measurably improve the 30-second-skim impression — e.g. pinning a repo, adding a live demo link, renaming a repo, writing a one-paragraph README summary",
      "a specific, fast fix tied to a named repo"
    ]
  }},
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
    r = requests.post(
    GEMINI_URL,
    json=body,
    headers={"x-goog-api-key": GEMINI_API_KEY},
    timeout=60,
)
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def _do_analyze(username, force=False):
    """Core analysis logic — returns (result_dict, error_str, status_code)."""
    cached = CACHE.get(username)
    if cached and not force and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1], None, 200

    user = fetch_user(username)
    if user is None:
        return None, f"GitHub user '{username}' not found", 404

    repos = fetch_repos(username)
    if not repos:
        return None, f"'{username}' has no public non-fork repositories", 404

    repos = repos[:25]
    repos_meta = [preprocess_repo(username, r) for r in repos]

    try:
        prompt = build_gemini_prompt(username, repos_meta)
        analysis = call_gemini(prompt)
    except Exception as e:
        return None, f"Analysis failed: {str(e)}", 502

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
        "cached": False,
    }

    CACHE[username] = (time.time(), result)
    return result, None, 200


@app.route("/api/analyze/<username>", methods=["GET"])
def analyze(username):
    force = request.args.get("force", "false").lower() == "true"
    result, error, status = _do_analyze(username, force)
    if error:
        return jsonify({"error": error}), status
    return jsonify(result)


@app.route("/api/stream/<username>", methods=["GET"])
def stream_analyze(username):
    """
    SSE endpoint — emits progress events then the final result.
    Events: { type: "progress", message: "..." }
            { type: "result", data: { ...full result... } }
            { type: "error", message: "..." }
    """
    force = request.args.get("force", "false").lower() == "true"

    def generate():
        def emit(obj):
            return f"data: {json.dumps(obj)}\n\n"

        # Check cache first
        cached = CACHE.get(username)
        if cached and not force and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
            cached_result = dict(cached[1])
            cached_result["cached"] = True
            yield emit({"type": "progress", "message": "Loading cached result…"})
            yield emit({"type": "result", "data": cached_result})
            return

        yield emit({"type": "progress", "message": "Looking up GitHub profile…"})

        user = fetch_user(username)
        if user is None:
            yield emit({"type": "error", "message": f"GitHub user '{username}' not found"})
            return

        yield emit({"type": "progress", "message": "Fetching public repositories…"})

        try:
            repos = fetch_repos(username)
        except Exception as e:
            yield emit({"type": "error", "message": f"Failed to fetch repos: {str(e)}"})
            return

        if not repos:
            yield emit({"type": "error", "message": f"'{username}' has no public non-fork repositories"})
            return

        repos = repos[:25]
        total = len(repos)

        yield emit({"type": "progress", "message": f"Found {total} repos — reading READMEs and file trees…"})

        repos_meta = []
        for i, repo in enumerate(repos):
            meta = preprocess_repo(username, repo)
            repos_meta.append(meta)
            if (i + 1) % 5 == 0 or (i + 1) == total:
                yield emit({
                    "type": "progress",
                    "message": f"Scanning repos… {i + 1}/{total}"
                })

        yield emit({"type": "progress", "message": "Sending to Gemini for analysis…"})

        try:
            prompt = build_gemini_prompt(username, repos_meta)
            analysis = call_gemini(prompt)
        except Exception as e:
            yield emit({"type": "error", "message": f"Analysis failed: {str(e)}"})
            return

        yield emit({"type": "progress", "message": "Structuring results…"})

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
            "cached": False,
        }

        CACHE[username] = (time.time(), result)
        yield emit({"type": "result", "data": result})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)