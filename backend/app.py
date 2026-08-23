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
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1/models/"
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
    r = requests.get(f"{GITHUB_API}/users/{username}", headers=gh_headers(), timeout=8)
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
            timeout=8,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
        if page > 1:
            break
    return [r for r in repos if not r.get("fork")]


def fetch_readme_snippet(username, repo_name):
    try:
        r = requests.get(
            f"{GITHUB_API}/repos/{username}/{repo_name}/readme",
            headers=gh_headers(),
            timeout=5,
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
            timeout=5,
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
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def fetch_top_level_files(username, repo_name):
    try:
        r = requests.get(
            f"{GITHUB_API}/repos/{username}/{repo_name}/contents",
            headers=gh_headers(),
            timeout=5,
        )
        if r.status_code != 200:
            return []
        return [item.get("name", "") for item in r.json() if isinstance(item, dict)]
    except Exception:
        return []


def has_tests(username, repo_name, default_branch="main", top_level_files=None):
    """Walks the full repo tree (not just top-level) looking for test files/dirs,
    so tests nested in src/ or named test_*.py / *.test.js aren't missed."""
    test_dir_names = ("test", "tests", "__tests__", "spec", "specs")
    test_file_patterns = ("test_", "_test.", ".test.", ".spec.", "test.")

    try:
        r = requests.get(
            f"{GITHUB_API}/repos/{username}/{repo_name}/git/trees/{default_branch}",
            headers=gh_headers(),
            params={"recursive": "1"},
            timeout=8,
        )
        if r.status_code == 200:
            tree = r.json().get("tree", [])
            for item in tree:
                path = item.get("path", "").lower()
                parts = path.split("/")
                filename = parts[-1]
                if any(p in test_dir_names for p in parts[:-1]):
                    return True
                if any(filename.startswith(pat) or pat in filename for pat in test_file_patterns):
                    return True
            return False
    except Exception:
        pass

    # Fallback: old top-level-only check if the tree call failed
    names = top_level_files if top_level_files is not None else fetch_top_level_files(username, repo_name)
    lower = [n.lower() for n in names]
    return any(n in test_dir_names for n in lower)


def fetch_external_contributions(username):
    """Merged PRs authored by the user on repos they don't own — the
    'contributed to real open source' signal that owner-only repo scans miss."""
    try:
        r = requests.get(
            f"{GITHUB_API}/search/issues",
            headers=gh_headers(),
            params={"q": f"is:pr author:{username} is:merged", "per_page": 10, "sort": "created", "order": "desc"},
            timeout=8,
        )
        if r.status_code != 200:
            return {"totalMergedPRs": None, "externalRepos": []}
        data = r.json()
        items = data.get("items", [])
        external = []
        seen = set()
        for item in items:
            repo_url = item.get("repository_url", "")
            # repository_url looks like https://api.github.com/repos/{owner}/{repo}
            parts = repo_url.rstrip("/").split("/")
            if len(parts) < 2:
                continue
            owner, repo_name = parts[-2], parts[-1]
            if owner.lower() == username.lower():
                continue  # PR on their own repo, not "external"
            key = f"{owner}/{repo_name}"
            if key not in seen:
                seen.add(key)
                external.append(key)
        return {
            "totalMergedPRs": data.get("total_count", 0),
            "externalRepos": external[:8],
        }
    except Exception:
        return {"totalMergedPRs": None, "externalRepos": []}


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
    default_branch = repo.get("default_branch") or "main"

    dependency_signals = [
        f for f in top_level_files
        if f.lower() in (
            "package.json", "requirements.txt", "pyproject.toml", "pipfile",
            "dockerfile", "docker-compose.yml", "go.mod", "cargo.toml",
            "pom.xml", "build.gradle", "gemfile",
        )
    ]

    license_info = repo.get("license") or {}

    return {
        "name": name,
        "description": (repo.get("description") or "")[:150],
        "primaryLang": primary_lang,
        "languages": list(languages.keys())[:5],
        "readmeSnippet": readme_snippet[:400] if readme_snippet else "",
        "topLevelFiles": top_level_files[:20],
        "dependencySignals": dependency_signals,
        "hasTests": has_tests(username, name, default_branch, top_level_files),
        "hasCI": has_workflows(username, name),
        "license": license_info.get("name"),
        "stars": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "topics": repo.get("topics", [])[:6],
        "daysActive": days_active,
        "lastCommit": updated,
        "isArchived": repo.get("archived", False),
    }


def build_gemini_prompt(username, repos_meta, external_contribs=None):
    external_contribs = external_contribs or {"totalMergedPRs": None, "externalRepos": []}
    external_block = ""
    if external_contribs.get("totalMergedPRs"):
        external_block = f"""
External open-source contributions (merged PRs on repos NOT owned by
"{username}" — a genuine signal separate from their own projects):
{json.dumps(external_contribs, indent=2)}
"""

    return f"""You are a senior engineer doing a real technical read of "{username}"'s
GitHub profile — the kind of close read a hiring engineer or a technical
co-founder would do before deciding to work with this person. This is not
a generic portfolio summary. Base every claim ONLY on the repo metadata
below (README snippets, file listings, dependency files, languages,
topics). Do not invent facts. If evidence is thin for a claim, say so
explicitly rather than padding with generic advice.

Repo metadata (JSON array, one object per repo — includes readmeSnippet,
topLevelFiles, dependencySignals like package.json/requirements.txt,
license, hasTests, hasCI):
{json.dumps(repos_meta, indent=2)}
{external_block}
HARD RULES — violating these makes the analysis useless, avoid them:
1. Every strength, gap, and suggestion MUST name the specific repo(s) it's
   based on. Never write an unattributed claim like "lacks testing" —
   write "no test directory or CI config found in papertrail-server,
   MedScan-AI, or any other repo" instead.
2. Do NOT give generic boilerplate advice that applies to any GitHub
   profile ("add tests", "write a README", "set up CI"). Instead, tie each
   suggestion to what the actual code/stack in a specific repo is missing
   relative to what that project needs to be production-credible.
3. Use readmeSnippet content to judge actual project scope and maturity,
   not just its existence. A one-line README and a detailed one with
   setup instructions are very different signals — say which is which,
   by name.
4. Use dependencySignals and topLevelFiles to infer real technical choices
   rather than only looking at the primaryLang field.
5. If two or more repos show a repeated pattern, say so as a PATTERN
   across named repos.
6. Do not state specific day counts, ages, or durations anywhere — use
   only qualitative descriptions of activity instead.
7. If external open-source contributions are present above, treat merged
   PRs on repos the user does NOT own as a strong positive signal distinct
   from their own projects — this is evidence of working in someone else's
   codebase and getting real code accepted, which weighs meaningfully on
   placement readiness. If none are present, do not penalize for it or
   speculate about why.

Additionally, score PLACEMENT READINESS — how this profile would read to a
campus placement panel or an off-campus tech recruiter screening a
student/early-career candidate, NOT a senior engineer's general opinion.

Respond with ONLY a JSON object (no markdown fences, no preamble) matching
exactly this schema:
{{
  "primaryStack": "specific string naming actual repos as evidence",
  "depthVsBreadth": "2-3 sentences naming which repos represent depth vs breadth",
  "consistencyScore": <integer 0-100>,
  "placementReadiness": {{
    "score": <integer 0-100>,
    "verdict": "2-3 sentences on how this profile lands with a placement panel",
    "quickWins": [
      "specific fast fix tied to a named repo",
      "specific fast fix tied to a named repo"
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
    "specific claim naming the repo(s)",
    "specific claim naming the repo(s)",
    "specific claim naming the repo(s)"
  ],
  "gaps": [
    "specific claim naming the repo(s)",
    "specific claim naming the repo(s)"
  ],
  "suggestions": [
    "concrete next step tied to a named repo",
    "concrete next step tied to a named repo",
    "concrete next step tied to a named repo"
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
        timeout=90,
    )
    if not r.ok:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:500]}")
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


@app.route("/api/analyze/<username>", methods=["GET"])
def analyze(username):
    force = request.args.get("force", "false").lower() == "true"

    cached = CACHE.get(username)
    if cached and not force and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
        result = dict(cached[1])
        result["cached"] = True
        return jsonify(result)

    user = fetch_user(username)
    if user is None:
        return jsonify({"error": f"GitHub user '{username}' not found"}), 404

    repos = fetch_repos(username)
    if not repos:
        return jsonify({"error": f"'{username}' has no public non-fork repositories"}), 404

    repos = repos[:10]
    repos_meta = [preprocess_repo(username, r) for r in repos]
    external_contribs = fetch_external_contributions(username)

    try:
        prompt = build_gemini_prompt(username, repos_meta, external_contribs)
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
        "cached": False,
    }

    CACHE[username] = (time.time(), result)
    return jsonify(result)


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.route("/api/stream/<username>", methods=["GET"])
def stream(username):
    force = request.args.get("force", "false").lower() == "true"

    def generate():
        cached = CACHE.get(username)
        if cached and not force and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
            result = dict(cached[1])
            result["cached"] = True
            yield _sse("progress", {"message": "Loading cached result…"})
            yield _sse("result", result)
            return

        try:
            yield _sse("progress", {"message": "Looking up GitHub profile…"})
            user = fetch_user(username)
            if user is None:
                yield _sse("error", {"error": f"GitHub user '{username}' not found"})
                return

            yield _sse("progress", {"message": "Fetching public repositories…"})
            repos = fetch_repos(username)
            if not repos:
                yield _sse("error", {"error": f"'{username}' has no public non-fork repositories"})
                return

            repos = repos[:10]
            total = len(repos)
            repos_meta = []
            for i, r in enumerate(repos, start=1):
                yield _sse("progress", {"message": f"Scanning repos… ({i}/{total}) {r['name']}"})
                repos_meta.append(preprocess_repo(username, r))

            yield _sse("progress", {"message": "Sending to Gemini for analysis…"})
            prompt = build_gemini_prompt(username, repos_meta)

            yield _sse("progress", {"message": "Reasoning about the profile…"})
            analysis = call_gemini(prompt)
        except Exception as e:
            yield _sse("error", {"error": f"Analysis failed: {str(e)}"})
            return

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
        yield _sse("result", result)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)