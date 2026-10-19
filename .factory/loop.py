"""The software factory: one loop that builds, maintains and improves this repository from VISION.md.

Each tick (one run of .github/workflows/factory.yml) reads the repository's state from GitHub, does the
first thing on this list that applies, records it, and exits. When it did something it starts the next
tick at once; otherwise it waits for the next start (see factory.yml). Ticks never overlap.

    1. the owner wrote something new             the author acts on it and answers
    2. main's check failed                       an urgent fix
    3. a pull request needs something            the reviewer reviews it, the author fixes it, or it merges
    4. a proposed issue is unreviewed            the reviewer reviews the plan
    5. a reviewed plan needs changes             the author revises it
    6. a ready issue has no pull request         the author implements it
    7. the weekly digest is due                  the author writes it for the owner
    8. the weekly retrospective is due           the author proposes improvements to the factory itself
    9. there is nothing to do                    the author plans the next work from VISION.md

Two agents do the work, through harness-interface: the author makes everything (code, tests, docs,
plans, the digest) and the reviewer checks everything the author makes. Neither knows who the other is.
This loop holds every credential and takes every action on GitHub; the agents get files in and leave
files behind. See .factory/README.md.

    python3 .factory/loop.py [tick|digest|improve]
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

RESULT = ".factory-result.json"                       # how an agent answers; never part of a change
FORBIDDEN = ("VISION.md", ".github/workflows/*")      # the owner's, always: no agent change may touch them
NOISE = ("__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/", "node_modules/", ".venv/", RESULT)
FILE_CAP, SEED_CAP = 200_000, 4_000_000               # what an agent is handed, at most
UNSEEN = (".github/workflows/factory.yml",)           # it names which harness plays which role: agents never see it
CHECK, REVIEW = "check", "factory/review"             # the two verdicts every pull request needs
WORK = ("factory:proposed", "factory:revise", "factory:ready", "factory:needs-owner")
LABELS = {
    "factory:home": ("0e8a16", "The factory's status and memory"),
    "factory:proposed": ("c5def5", "Planned work waiting for review"),
    "factory:revise": ("fbca04", "Planned work the reviewer sent back"),
    "factory:ready": ("1d76db", "Reviewed work the author will build"),
    "factory:urgent": ("b60205", "Built before anything else"),
    "factory:needs-owner": ("d93f0b", "Waiting on the owner"),
    "factory:improve": ("5319e7", "A change to how the factory works"),
    "factory:digest": ("0e8a16", "The weekly digest"),
    "factory:owner-asked": ("d93f0b", "A change waiting for the owner's approval"),
}
DEFAULTS = {
    "branch": "main", "budget_usd_per_day": 20.0, "turns_per_day": 40, "tries": 3, "open_prs": 3,
    "plan_every_hours": 24, "digest_every_days": 7, "improve_every_days": 7, "egress": [],
    "owner_paths": ["VISION.md", ".factory/loop.py", ".factory/check", ".factory/config.toml", ".github/*"],
    "test_paths": ["test/*", "tests/*", "*/test/*", "*/tests/*", "test_*", "*/test_*", "*_test.*", "*.test.*", "*.spec.*"],
}
STATE = re.compile(r"<!-- factory:state (\{.*\}) -->", re.S)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def matches(path: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def config(root: Path) -> dict:
    path = root / ".factory" / "config.toml"
    return {**DEFAULTS, **(tomllib.loads(path.read_text()) if path.is_file() else {})}


class OutOfBudget(Exception):
    pass


# ── GitHub: everything the loop does there, with the job's GITHUB_TOKEN ────────────────────────────
class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


class GitHub:
    def __init__(self, repo: str, token: str, api: str = "https://api.github.com"):
        self.repo, self._token, self.api = repo, token, api

    def __repr__(self) -> str:
        return f"GitHub({self.repo})"                 # never the token

    def call(self, method: str, path: str, body=None):
        req = urllib.request.Request(f"{self.api}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise GitHubError(e.code, e.read().decode(errors="replace")[:500]) from None
        return json.loads(raw) if raw else None

    def _r(self, path: str) -> str:
        return f"/repos/{self.repo}{path}"

    def _pages(self, path: str, limit: int = 10) -> list:
        out, sep = [], "&" if "?" in path else "?"
        for page in range(1, limit + 1):
            batch = self.call("GET", self._r(f"{path}{sep}per_page=100&page={page}"))
            out += batch
            if len(batch) < 100:
                break
        return out

    # issues and comments
    def issue(self, n: int) -> dict:
        return self.call("GET", self._r(f"/issues/{n}"))

    def issues(self, label: str = "", state: str = "open", creator: str = "", since: str = "") -> list:
        q = f"/issues?state={state}&sort=created&direction=asc"
        q += (f"&labels={label}" if label else "") + (f"&creator={creator}" if creator else "")
        q += f"&since={since}" if since else ""
        return [i for i in self._pages(q) if "pull_request" not in i]

    def create_issue(self, title: str, body: str, labels: list, assignees: list = ()) -> dict:
        return self.call("POST", self._r("/issues"), {"title": title[:250], "body": body, "labels": labels,
                                                      "assignees": list(assignees)})

    def edit_issue(self, n: int, **fields) -> None:
        self.call("PATCH", self._r(f"/issues/{n}"), fields)

    def comment(self, n: int, body: str) -> None:
        self.call("POST", self._r(f"/issues/{n}/comments"), {"body": body})

    def owner_comments(self, login: str, since: str) -> list:
        """Every comment `login` made on any issue or pull request after `since`, oldest first."""
        found = self._pages(f"/issues/comments?since={since}&sort=created&direction=asc")
        return [c for c in found if c["user"]["login"] == login and c["created_at"] > since]

    def react(self, what: str, number: int) -> None:
        """👀 on a comment ("comment") or an issue ("issue"): the owner sees it was read."""
        path = f"/issues/comments/{number}/reactions" if what == "comment" else f"/issues/{number}/reactions"
        try:
            self.call("POST", self._r(path), {"content": "eyes"})
        except GitHubError:
            pass                                      # a courtesy; the cursor is what counts

    def add_labels(self, n: int, labels: list) -> None:
        self.call("POST", self._r(f"/issues/{n}/labels"), {"labels": labels})

    def remove_label(self, n: int, label: str) -> None:
        try:
            self.call("DELETE", self._r(f"/issues/{n}/labels/{label}"))
        except GitHubError as e:
            if e.status != 404:
                raise

    def ensure_label(self, name: str, color: str, description: str) -> None:
        try:
            self.call("POST", self._r("/labels"), {"name": name, "color": color, "description": description})
        except GitHubError as e:
            if e.status != 422:                       # 422: it exists
                raise

    def assign(self, n: int, login: str) -> None:
        try:
            self.call("POST", self._r(f"/issues/{n}/assignees"), {"assignees": [login]})
        except GitHubError:
            pass                                      # the label and the comment still say it

    # pull requests
    def pulls(self) -> list:
        return self._pages("/pulls?state=open&sort=created&direction=asc")

    def merged_since(self, since: str) -> list:
        found = self._pages("/pulls?state=closed&sort=updated&direction=desc", limit=3)
        return [p for p in found if (p.get("merged_at") or "") > since]

    def pull_files(self, n: int) -> list:
        return [f["filename"] for f in self._pages(f"/pulls/{n}/files")]

    def create_pull(self, title: str, head: str, base: str, body: str) -> dict:
        return self.call("POST", self._r("/pulls"), {"title": title[:250], "head": head, "base": base, "body": body})

    def merge(self, n: int, sha: str, title: str, message: str) -> None:
        self.call("PUT", self._r(f"/pulls/{n}/merge"), {"sha": sha, "commit_title": title, "commit_message": message,
                                                        "merge_method": "squash"})

    def delete_branch(self, branch: str) -> None:
        try:
            self.call("DELETE", self._r(f"/git/refs/heads/{branch}"))
        except GitHubError:
            pass

    def reviews(self, n: int) -> list:
        return self._pages(f"/pulls/{n}/reviews")

    def request_review(self, n: int, login: str) -> None:
        try:
            self.call("POST", self._r(f"/pulls/{n}/requested_reviewers"), {"reviewers": [login]})
        except GitHubError:
            pass

    # commits: the check and the review
    def branch_sha(self, branch: str) -> str:
        return self.call("GET", self._r(f"/branches/{branch}"))["commit"]["sha"]

    def check(self, sha: str) -> dict | None:
        """The latest run of the `check` job on this commit, if there is one."""
        runs = self.call("GET", self._r(f"/commits/{sha}/check-runs?check_name={CHECK}&filter=latest"))["check_runs"]
        return runs[0] if runs else None

    def failure(self, check: dict) -> str:
        """What the failed check said: its annotations, which the check job writes."""
        notes = self.call("GET", self._r(f"/check-runs/{check['id']}/annotations"))
        return "\n\n".join(a.get("message", "") for a in notes) or f"the check failed: {check.get('html_url', '')}"

    def status(self, sha: str, context: str) -> tuple[str, str]:
        for s in self.call("GET", self._r(f"/commits/{sha}/status"))["statuses"]:
            if s["context"] == context:
                return s["state"], s.get("description") or ""
        return "", ""

    def set_status(self, sha: str, context: str, state: str, description: str) -> None:
        self.call("POST", self._r(f"/statuses/{sha}"), {"state": state, "context": context,
                                                        "description": description[:140]})

    def dispatch(self, task: str, ref: str) -> None:
        """Start the factory's workflow: its own actions start no workflow, a dispatch does."""
        self.call("POST", self._r("/actions/workflows/factory.yml/dispatches"), {"ref": ref, "inputs": {"task": task}})


# ── harness-interface: the only way an agent runs ──────────────────────────────────────────────────
class Agents:
    """Each turn is one response from the harness-interface server the workflow started, on a harness
    registered under the role's name, in a sandbox that reaches only its provider (and `egress`)."""

    def __init__(self, url: str, root: Path, sandboxed: bool):
        self.url, self.root, self.sandboxed = url.rstrip("/"), root, sandboxed

    def _call(self, method: str, path: str, body=None, raw: bool = False):
        req = urllib.request.Request(f"{self.url}{path}", method=method, headers={"Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=3600) as r:
            data = r.read()
        return data if raw else json.loads(data)

    def instructions(self, role: str) -> str:
        """The role's prompt, then what the factory has learned. The project's own files arrive as files."""
        folder = self.root / ".factory"
        parts = [(folder / f"{role}.md").read_text().strip()]
        if (folder / "memory.md").is_file():
            parts.append((folder / "memory.md").read_text().strip())
        return "\n\n---\n\n".join(parts)

    def turn(self, role: str, text: str, files: dict[str, str]) -> dict:
        items = [{"type": "input_text", "text": text},
                 *({"type": "input_file", "filename": k, "file_data": v} for k, v in files.items())]
        record = self._call("POST", "/responses", {"model": role, "input": items, "instructions": self.instructions(role)})
        if self.sandboxed and record["harness"]["runtime"] != "docker":
            raise RuntimeError("refusing a turn that did not run in a Docker sandbox")
        return record

    def file(self, rid: str, path: str) -> bytes:
        return self._call("GET", f"/responses/{rid}/files/{path}", raw=True)

    def changes(self, record: dict) -> dict:
        """What the turn changed in the project: new bytes per path, None for a deletion. Build noise
        and the agent's answer file are not changes."""
        h = record["harness"]
        keep = lambda p: not any(f"/{n}" in f"/{p}" for n in NOISE)
        out = {p: self.file(record["id"], p) for p in h.get("files", []) if keep(p)}
        out.update({p: None for p in h.get("deleted", []) if keep(p)})
        return out

    def result(self, record: dict) -> dict:
        try:
            data = json.loads(self.file(record["id"], RESULT))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


def answer(record: dict) -> str:
    return "".join(c.get("text", "") for m in record.get("output", []) for c in m.get("content", [])).strip()


def cost(record: dict) -> float:
    return float(record["harness"].get("cost_usd") or 0.0)


# ── the checkout: where the loop turns an agent's files into commits ───────────────────────────────
class Repo:
    BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")

    def __init__(self, root: Path, token: str = ""):
        """`token` reaches only git's own requests to GitHub."""
        self.root, self._token = root, token
        exclude = root / ".git" / "info" / "exclude"
        if exclude.parent.is_dir() and RESULT not in (exclude.read_text() if exclude.is_file() else ""):
            with exclude.open("a") as f:
                f.write("\n# the factory: never part of a change\n" + "\n".join(NOISE) + "\n")

    def git(self, *args: str, remote: bool = False, check: bool = True) -> str:
        auth = []
        if remote and self._token:
            basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            auth = ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
        r = subprocess.run(["git", "-c", f"user.name={self.BOT[0]}", "-c", f"user.email={self.BOT[1]}", *auth, *args],
                           cwd=self.root, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[-500:]}")
        return r.stdout.strip()

    def start(self, branch: str, base: str, fresh: bool) -> None:
        """Check out `branch` as origin has it, or, `fresh`, new from `base`."""
        self.git("fetch", "-q", "origin", base, remote=True)
        if not fresh and self.git("ls-remote", "--heads", "origin", branch, remote=True):
            self.git("fetch", "-q", "origin", branch, remote=True)
            self.git("checkout", "-q", "-B", branch, f"origin/{branch}")
        else:
            self.git("checkout", "-q", "-B", branch, f"origin/{base}")
        self.git("clean", "-qfd")

    def checkout(self, ref: str) -> None:
        """Any ref, such as a pull request's head, detached."""
        self.git("fetch", "-q", "origin", ref, remote=True)
        self.git("checkout", "-q", "--detach", "FETCH_HEAD")
        self.git("clean", "-qfd")

    def seed(self) -> dict[str, str]:
        """The project's text files, as an agent's workspace starts."""
        out, total = {}, 0
        for rel in self.git("ls-files").splitlines():
            p = self.root / rel
            if rel in UNSEEN:
                continue
            if not p.is_file() or p.stat().st_size > FILE_CAP or total + p.stat().st_size > SEED_CAP:
                continue
            try:
                out[rel] = p.read_text()
            except UnicodeDecodeError:
                continue
            total += p.stat().st_size
        return out

    def apply(self, changes: dict) -> None:
        for rel, data in changes.items():
            p = (self.root / rel).resolve()
            if not p.is_relative_to(self.root.resolve()) or rel.startswith(".git/"):
                raise RuntimeError(f"refusing a path outside the project: {rel}")
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)

    def commit(self, message: str) -> bool:
        self.git("add", "-A")
        if not self.git("diff", "--cached", "--name-only"):
            return False
        self.git("commit", "-q", "-m", message)
        return True

    def push(self, branch: str, force: bool) -> None:
        self.git("push", "-q", *(["--force"] if force else []), "origin", f"HEAD:refs/heads/{branch}", remote=True)

    def diff(self, base: str) -> str:
        self.git("fetch", "-q", "origin", base, remote=True)
        return self.git("diff", f"origin/{base}...HEAD")


# ── the loop ───────────────────────────────────────────────────────────────────────────────────────
JSON_NOTE = "Answer by writing .factory-result.json in your working directory, in exactly this shape:"


class Factory:
    def __init__(self, cfg: dict, gh, agents, repo, owner: str, clock=now):
        self.cfg, self.gh, self.agents, self.repo, self.owner, self.clock = cfg, gh, agents, repo, owner, clock
        self.branch = cfg["branch"]
        self.home: dict | None = None
        self.state: dict = {}

    # state: one hidden comment in the home issue's body; GitHub is the only memory
    def load(self) -> None:
        found = self.gh.issues(label="factory:home")
        if not found:                                 # the first tick: the labels, and the home issue
            for name, (color, text) in LABELS.items():
                self.gh.ensure_label(name, color, text)
        self.home = found[0] if found else self.gh.create_issue("Software factory", "Starting.", ["factory:home"])
        m = STATE.search(self.home.get("body") or "")
        start = iso(self.clock())
        self.state = {"cursor": start, "day": "", "spent": 0.0, "turns": 0, "attempts": {}, "feedback": {},
                      "vision": "", "last_plan": "", "last_digest": start, "last_improve": start, "log": [],
                      **(json.loads(m.group(1)) if m else {})}
        today = start[:10]
        if self.state["day"] != today:
            self.state.update(day=today, spent=0.0, turns=0)

    def save(self, note: str = "") -> None:
        self.state["log"] = self.state["log"][-300:]
        self.gh.edit_issue(self.home["number"], body=self.render(note))

    def log(self, do: str, n, ok: str, usd: float = 0.0) -> None:
        self.state["log"].append({"at": iso(self.clock()), "do": do, "n": n, "ok": ok, "usd": round(usd, 4)})

    # the brakes and the agents
    def can_spend(self) -> bool:
        return self.state["spent"] < self.cfg["budget_usd_per_day"] and self.state["turns"] < self.cfg["turns_per_day"]

    def turn(self, role: str, text: str, files: dict, n: int | None = None) -> dict:
        if not self.can_spend():
            raise OutOfBudget()
        if role == "author" and n is not None:
            self.state["attempts"][str(n)] = self.state["attempts"].get(str(n), 0) + 1
        record = self.agents.turn(role, text, files)
        self.state["spent"] = round(self.state["spent"] + cost(record), 4)
        self.state["turns"] += 1
        return record

    def out_of_tries(self, n: int) -> bool:
        return self.state["attempts"].get(str(n), 0) >= self.cfg["tries"]

    def tick(self, force: str = "") -> bool:
        """One step. True if it did something, so the next tick should start now."""
        self.load()
        note = ""
        try:
            rules = (self.steer, self.main_health, self.pull_requests, self.review_plan, self.revise_plan,
                     self.implement, lambda: self.digest(force == "digest"), lambda: self.improve(force == "improve"),
                     self.plan)
            acted = any(rule() for rule in rules)    # any() stops at the first rule that acted
        except OutOfBudget:
            acted, note = False, "Today's budget is spent; work resumes tomorrow (UTC)."
        self.save(note or ("" if acted else "Nothing to do right now."))
        return acted

    # 1. the owner
    def steer(self) -> bool:
        since = self.state["cursor"]
        comments = self.gh.owner_comments(self.owner, since)
        opened = [i for i in self.gh.issues(state="all", creator=self.owner, since=since) if i["created_at"] > since
                  and not any(label["name"].startswith("factory:") for label in i.get("labels", []))]
        if not comments and not opened:
            return False
        said = []
        for c in comments:
            n = int(c["issue_url"].rsplit("/", 1)[1])
            if "/pull/" in c["html_url"]:
                self.pr_feedback(n, c["body"])
            else:
                said.append((n, c["body"]))
        said += [(i["number"], f"(a new issue) {i['title']}\n\n{i.get('body') or ''}") for i in opened]
        if said:
            self.act_on_owner(said)
        for c in comments:
            self.gh.react("comment", c["id"])
        for i in opened:
            self.gh.react("issue", i["number"])
        self.state["cursor"] = max(x["created_at"] for x in [*comments, *opened])
        return True

    def pr_feedback(self, number: int, text: str) -> None:
        """The owner commented on one of the factory's pull requests: that is review the author must address."""
        pr = next((p for p in self.gh.pulls() if p["number"] == number), None)
        if not pr or not pr["head"]["ref"].startswith("factory/"):
            return                                    # the owner's own pull requests are the owner's business
        n = int(pr["head"]["ref"].split("/", 1)[1])
        self.state["feedback"][str(n)] = f"{self.state['feedback'].get(str(n), '')}\n\nThe owner: {text}".strip()
        self.state["attempts"].pop(str(n), None)
        self.gh.remove_label(number, "factory:needs-owner")
        self.gh.set_status(pr["head"]["sha"], REVIEW, "failure", "The owner asked for changes")

    def act_on_owner(self, said: list) -> None:
        threads = []
        for n, text in said:
            issue = self.gh.issue(n)
            labels = ", ".join(label["name"] for label in issue.get("labels", []))
            threads.append(f"### On #{n}: {issue['title']} ({labels or 'no labels'})\n\n"
                           f"{(issue.get('body') or '')[:3000]}\n\n**The owner wrote:** {text}")
        record = self.turn("author", f"""The owner, who decides this project's direction, wrote to you. Act on it: change
the planned work to match (create issues, edit them, mark them ready, or close them), and answer the owner
briefly in each thread. The owner's direction outranks the existing plan; VISION.md is still the north star.
Planned work is open-work.json.

{chr(10).join(threads)}

{JSON_NOTE}
{{"replies": [{{"number": 12, "text": "..."}}],
 "create": [{{"title": "<area>: <summary>", "body": "what and why, then ## Acceptance criteria"}}],
 "update": [{{"number": 12, "title": "...", "body": "...", "status": "proposed | ready | closed"}}]}}""",
                           {**self.repo.seed(), "open-work.json": self.open_work()})
        result = self.agents.result(record)
        for r in result.get("replies", []):
            self.gh.comment(int(r["number"]), f"### Author\n\n{r['text']}")
        created = self.create(result.get("create", [])[:5], ["factory:proposed"])
        for u in result.get("update", []):
            self.update(int(u["number"]), u)
        self.log("steer", [n for n, _ in said], f"{len(created)} new, {len(result.get('update', []))} updated", cost(record))

    def update(self, n: int, u: dict) -> None:
        fields = {k: u[k] for k in ("title", "body") if u.get(k)}
        status = u.get("status", "")
        if status == "closed":
            fields["state"] = "closed"
        if fields:
            self.gh.edit_issue(n, **fields)
        if status in ("proposed", "ready"):
            for label in WORK:
                self.gh.remove_label(n, label)
            self.gh.add_labels(n, [f"factory:{status}"])
            self.state["attempts"].pop(str(n), None)

    def open_work(self) -> str:
        rows = [{"number": i["number"], "title": i["title"], "labels": [label["name"] for label in i["labels"]]}
                for i in self.gh.issues() if "pull_request" not in i]
        return json.dumps(rows, indent=1)

    # 2. main
    def main_health(self) -> bool:
        sha = self.gh.branch_sha(self.branch)
        run = self.gh.check(sha)
        if run is None or run.get("conclusion") in ("cancelled", "skipped", "stale"):
            self.gh.dispatch("check", self.branch)    # merges by the factory start no workflow
            return False
        if run.get("conclusion") != "failure" or self.gh.issues(label="factory:urgent"):
            return False
        issue = self.gh.create_issue(f"fix: the check fails on {self.branch}", f"""The check fails on `{sha[:12]}`, the head of
`{self.branch}`. Make it pass again without weakening it.

{self.gh.failure(run)[-6000:]}

## Acceptance criteria
- `.factory/check` passes on {self.branch}.""", ["factory:ready", "factory:urgent"])
        self.log("main-red", issue["number"], "fix filed")
        return True

    # 3. pull requests
    def pull_requests(self) -> bool:
        for pr in self.gh.pulls():
            mine = pr["head"]["ref"].startswith("factory/")
            labels = {label["name"] for label in pr.get("labels", [])}
            if (not mine and pr["user"]["login"] != self.owner) or "factory:needs-owner" in labels:
                continue
            sha = pr["head"]["sha"]
            run = self.gh.check(sha)
            if run is None or run.get("conclusion") in ("cancelled", "skipped", "stale"):
                if mine:
                    self.gh.dispatch("check", pr["head"]["ref"])
                continue
            if run.get("status") != "completed":
                continue
            n = int(pr["head"]["ref"].split("/", 1)[1]) if mine else None
            if run.get("conclusion") != "success":
                if mine:
                    return self.fix(pr, n, f"The check failed:\n\n{self.gh.failure(run)[-6000:]}")
                continue
            verdict, _ = self.gh.status(sha, REVIEW)
            if not verdict:
                return self.review(pr, n)
            if mine and verdict == "failure":
                return self.fix(pr, n, self.state["feedback"].pop(str(n), "The reviewer asked for changes."))
            if mine and verdict == "success":
                return self.merge(pr, n)
        return False

    def review(self, pr: dict, n: int | None) -> bool:
        self.repo.checkout(f"pull/{pr['number']}/head")
        diff = self.repo.diff(self.branch)
        task = self.gh.issue(n) if n else {"title": pr["title"], "body": pr.get("body") or ""}
        record = self.turn("reviewer", f"""Review this change. The task it is for is below; the change is change.diff, and
your working directory holds the project as the change leaves it. Judge it against the task, VISION.md,
AGENTS.md and your instructions. Change no project files.

# {task['title']}

{(task.get('body') or '')[:6000]}

{JSON_NOTE}
{{"verdict": "approve | changes", "findings": "what must change, citing file:line; empty to approve"}}""",
                           {**self.repo.seed(), "change.diff": diff[:400_000]})
        result = self.agents.result(record)
        ok = result.get("verdict") == "approve"
        findings = str(result.get("findings") or ("" if ok else "The reviewer gave no verdict."))
        self.gh.set_status(pr["head"]["sha"], REVIEW, "success" if ok else "failure", "approved" if ok else findings)
        self.gh.comment(pr["number"], f"### Review\n\n**{'Approved' if ok else 'Changes requested'}.**\n\n{findings}".strip())
        if n and not ok:
            self.state["feedback"][str(n)] = findings
        self.log("review", pr["number"], "approve" if ok else "changes", cost(record))
        return True

    def fix(self, pr: dict, n: int, why: str) -> bool:
        if self.out_of_tries(n):
            return self.escalate(n, f"The change in #{pr['number']} still fails after {self.cfg['tries']} tries:\n\n{why}",
                                 pr["number"])
        self.repo.start(pr["head"]["ref"], self.branch, fresh=False)
        issue = self.gh.issue(n)
        record = self.turn("author", f"""Your change for this task, in your working directory, did not pass. Fix it.

# {issue['title']}

{(issue.get('body') or '')[:6000]}

## What failed, verbatim

{why}

{JSON_NOTE}
{{"summary": "what you changed and why"}} or {{"blocked": "the question only the owner can answer, the options, your recommendation"}}""",
                           self.repo.seed(), n)
        return self.deliver(issue, record, pr)

    def merge(self, pr: dict, n: int) -> bool:
        owned = [p for p in self.gh.pull_files(pr["number"]) if matches(p, self.cfg["owner_paths"])]
        if owned and not self.owner_approved(pr):
            labels = {label["name"] for label in pr.get("labels", [])}
            if "factory:owner-asked" in labels:
                return False
            self.gh.add_labels(pr["number"], ["factory:owner-asked"])
            self.gh.request_review(pr["number"], self.owner)
            self.gh.comment(pr["number"], "### Author\n\nThis change passes its check and its review, and touches paths only "
                                          f"the owner approves: {', '.join(owned)}. Approve it to merge it.")
            self.log("ask-owner", pr["number"], ", ".join(owned))
            return True
        self.gh.merge(pr["number"], pr["head"]["sha"], f"{pr['title']} (#{pr['number']})",
                      f"Closes #{n}\n\nChecked-by: check\nReviewed-by: the reviewer")
        self.gh.delete_branch(pr["head"]["ref"])
        for label in WORK:
            self.gh.remove_label(n, label)
        self.gh.edit_issue(n, state="closed", state_reason="completed")    # done, whatever GitHub's own closing does
        self.state["attempts"].pop(str(n), None)
        self.log("merge", pr["number"], "merged")
        return True

    def owner_approved(self, pr: dict) -> bool:
        mine = [r for r in self.gh.reviews(pr["number"]) if r["user"]["login"] == self.owner]
        return bool(mine) and mine[-1]["state"] == "APPROVED" and mine[-1]["commit_id"] == pr["head"]["sha"]

    # 4 and 5. plans
    def review_plan(self) -> bool:
        proposed = self.gh.issues(label="factory:proposed")
        if not proposed:
            return False
        issue = proposed[0]
        self.repo.start(self.branch, self.branch, fresh=True)
        record = self.turn("reviewer", f"""Review this proposed piece of work before anyone builds it. Is it what VISION.md and the
project need now; one change small enough for one pull request; clear about what and why; and are its
acceptance criteria things a test or a reader can check? Your working directory holds the project.
Planned work is open-work.json.

# {issue['title']}

{(issue.get('body') or '')[:8000]}

{JSON_NOTE}
{{"verdict": "approve | changes", "findings": "what must change; empty to approve"}}""",
                           {**self.repo.seed(), "open-work.json": self.open_work()})
        result = self.agents.result(record)
        ok = result.get("verdict") == "approve"
        findings = str(result.get("findings") or ("" if ok else "The reviewer gave no verdict."))
        self.gh.remove_label(issue["number"], "factory:proposed")
        self.gh.add_labels(issue["number"], ["factory:ready" if ok else "factory:revise"])
        self.gh.comment(issue["number"], f"### Review\n\n**{'Approved' if ok else 'Changes requested'}.**\n\n{findings}".strip())
        if not ok:
            self.state["feedback"][str(issue["number"])] = findings
        self.log("review-plan", issue["number"], "approve" if ok else "changes", cost(record))
        return True

    def revise_plan(self) -> bool:
        sent_back = self.gh.issues(label="factory:revise")
        if not sent_back:
            return False
        issue, n = sent_back[0], sent_back[0]["number"]
        if self.out_of_tries(n):
            return self.escalate(n, f"The plan was sent back {self.cfg['tries']} times:\n\n"
                                    f"{self.state['feedback'].get(str(n), '')}")
        self.repo.start(self.branch, self.branch, fresh=True)
        record = self.turn("author", f"""The reviewer sent back this planned work. Revise it to answer the review, or close it if
the review shows it should not be done.

# {issue['title']}

{(issue.get('body') or '')[:8000]}

## The review, verbatim

{self.state['feedback'].pop(str(n), '')}

{JSON_NOTE}
{{"title": "<area>: <summary>", "body": "what and why, then ## Acceptance criteria"}} or {{"close": "why"}}""",
                           self.repo.seed(), n)
        result = self.agents.result(record)
        self.gh.remove_label(n, "factory:revise")
        if result.get("close"):
            self.gh.comment(n, f"### Author\n\n{result['close']}")
            self.gh.edit_issue(n, state="closed", state_reason="not_planned")
            self.log("revise", n, "closed", cost(record))
        else:
            self.gh.edit_issue(n, **{k: result[k] for k in ("title", "body") if result.get(k)})
            self.gh.add_labels(n, ["factory:proposed"])
            self.log("revise", n, "revised", cost(record))
        return True

    # 6. building
    def implement(self) -> bool:
        prs = [p for p in self.gh.pulls() if p["head"]["ref"].startswith("factory/")]
        if len(prs) >= self.cfg["open_prs"]:
            return False
        busy = {p["head"]["ref"].split("/", 1)[1] for p in prs}
        ready = [i for i in self.gh.issues(label="factory:ready") if str(i["number"]) not in busy]
        if not ready:
            return False
        issue = sorted(ready, key=lambda i: (not any(x["name"] == "factory:urgent" for x in i["labels"]), i["number"]))[0]
        n = issue["number"]
        if self.out_of_tries(n):
            return self.escalate(n, f"No acceptable change after {self.cfg['tries']} tries:\n\n"
                                    f"{self.state['feedback'].get(str(n), '')}")
        self.repo.start(f"factory/{n}", self.branch, fresh=True)
        feedback = self.state["feedback"].pop(str(n), "")
        record = self.turn("author", f"""Do this task: change the project in your working directory so its acceptance criteria are
met, with tests and docs. Run the project's checks (.factory/check) before you finish.

# {issue['title']}

{(issue.get('body') or '')[:8000]}
{f"{chr(10)}## Your last attempt was not accepted, verbatim{chr(10)}{chr(10)}{feedback}" if feedback else ""}

{JSON_NOTE}
{{"title": "<area>: <summary>", "summary": "what you changed and why"}}
or {{"no_op": "why nothing needs to change"}}
or {{"blocked": "the question only the owner can answer, the options, your recommendation"}}""", self.repo.seed(), n)
        return self.deliver(issue, record, None)

    def deliver(self, issue: dict, record: dict, pr: dict | None) -> bool:
        """What the author left behind becomes a commit on its branch, and the pull request it belongs to."""
        n, result, changes = issue["number"], self.agents.result(record), self.agents.changes(record)
        action = "fix" if pr else "implement"
        if result.get("blocked"):
            return self.escalate(n, str(result["blocked"]), pr["number"] if pr else None)
        if not changes and result.get("no_op") and pr is None:
            self.gh.comment(n, f"### Author\n\nNothing to change: {result['no_op']}")
            self.gh.edit_issue(n, state="closed", state_reason="completed")
            self.log(action, n, "no_op", cost(record))
            return True
        problem = self.guard(changes) or ("" if changes else "you changed nothing")
        if problem:
            self.state["feedback"][str(n)] = f"The change was not accepted: {problem}."
            self.gh.comment(n, f"### Author\n\nThe change was not pushed: {problem}.")
            self.log(action, n, "rejected", cost(record))
            return True
        title = str(result.get("title") or issue["title"])
        summary = str(result.get("summary") or "")
        self.repo.apply(changes)
        branch = f"factory/{n}"
        if not self.repo.commit(f"{title}\n\n{summary}\n\nRefs #{n}".strip()):
            self.state["feedback"][str(n)] = "The change was not accepted: it changed nothing."
            self.log(action, n, "rejected", cost(record))
            return True
        self.repo.push(branch, force=pr is None)
        if pr is None:
            pr = self.gh.create_pull(title, branch, self.branch, f"{summary}\n\nCloses #{n}\n\n"
                                     "_Made by the author. It merges when `check` and the reviewer both pass it._")
        else:
            self.gh.comment(pr["number"], f"### Author\n\n{summary or 'Fixed.'}")
        self.gh.dispatch("check", branch)
        self.log(action, n, "pushed", cost(record))
        return True

    def guard(self, changes: dict) -> str:
        """What no change may do, whatever the review says."""
        bad = [p for p in changes if matches(p, FORBIDDEN)]
        if bad:
            return f"it changes {', '.join(bad)}, which only the owner changes (say `blocked` with the change written out)"
        gone = [p for p, data in changes.items() if data is None and matches(p, self.cfg["test_paths"])]
        if gone:
            return f"it deletes tests: {', '.join(gone)}"
        return ""

    def escalate(self, n: int, why: str, pr: int | None = None) -> bool:
        for label in WORK:
            self.gh.remove_label(n, label)
        self.gh.add_labels(n, ["factory:needs-owner"])
        self.gh.assign(n, self.owner)
        if pr:
            self.gh.add_labels(pr, ["factory:needs-owner"])
        self.gh.comment(n, f"### Author: this needs the owner\n\n{why}\n\nReply here and the factory picks it up again.")
        self.log("escalate", n, "needs-owner")
        return True

    # 7. the digest
    def digest(self, force: bool) -> bool:
        since = self.state["last_digest"]
        if not force and self.clock() - parse(since) < timedelta(days=self.cfg["digest_every_days"]):
            return False
        merged = [{"number": p["number"], "title": p["title"]} for p in self.gh.merged_since(since)]
        waiting = [{"number": i["number"], "title": i["title"]} for i in self.gh.issues(label="factory:needs-owner")]
        numbers = self.numbers(since)
        record = self.turn("author", f"""Write this period's digest for the project's owner, who reads it instead of following
along and steers by replying to it. Plain markdown, brief and exact, every claim linked (#n). Sections:
What shipped; What's in progress; Waiting on you (each with the question and your recommendation);
What went wrong; Next. Do not write files: your reply is posted as it is.

## Merged since {since}
{json.dumps(merged, indent=1)}

## Waiting on the owner
{json.dumps(waiting, indent=1)}

## Planned work
{self.open_work()}

## The factory's numbers
{numbers}""", self.repo.seed())
        for old in self.gh.issues(label="factory:digest"):
            self.gh.edit_issue(old["number"], state="closed")
        body = f"{answer(record) or '(The author wrote nothing.)'}\n\n## The numbers\n\n{numbers}\n\n" \
               "_Reply to this issue to steer the factory: it reads every comment you make._"
        self.gh.create_issue(f"Digest: {iso(self.clock())[:10]}", body, ["factory:digest"], [self.owner])
        self.state["last_digest"] = iso(self.clock())
        self.log("digest", None, "posted", cost(record))
        return True

    def numbers(self, since: str) -> str:
        """Counted by the loop, not written by an agent."""
        log = [e for e in self.state["log"] if e["at"] >= since]
        counts: dict[str, int] = {}
        for e in log:
            counts[f"{e['do']}: {e['ok']}"] = counts.get(f"{e['do']}: {e['ok']}", 0) + 1
        spent = round(sum(e["usd"] for e in log), 2)
        merged = counts.get("merge: merged", 0)
        rows = "\n".join(f"| {k} | {v} |" for k, v in sorted(counts.items())) or "| nothing yet | 0 |"
        per = f"${spent / merged:.2f}" if merged else "n/a"
        return (f"| What happened | Times |\n|---|---|\n{rows}\n\nReported spend: ${spent:.2f}; per merged change: {per}. "
                "Some harnesses report no cost, so spend can be under the truth; turns are capped either way.")

    # 8. the retrospective: the factory improves itself
    def improve(self, force: bool) -> bool:
        since = self.state["last_improve"]
        if not force and self.clock() - parse(since) < timedelta(days=self.cfg["improve_every_days"]):
            return False
        self.state["last_improve"] = iso(self.clock())
        recent = [e for e in self.state["log"] if e["at"] >= since]
        if not recent and not force:
            return False
        self.repo.start(self.branch, self.branch, fresh=True)
        record = self.turn("author", f"""Retrospective: improve how this factory works, from the evidence below. The factory is the
loop in .factory/loop.py, the instructions in .factory/author.md and .factory/reviewer.md, the lessons in
.factory/memory.md, and the limits in .factory/config.toml. Find where work stalls, is sent back, escalates
or costs the most, and propose at most two changes. Each proposal names the problem in the evidence,
the change, and how the next period's numbers will show whether it worked. Changes to author.md,
reviewer.md and memory.md merge after review; changes to loop.py, check or config.toml also need the owner.
If nothing is worth changing, say so.

## The factory's numbers since {since}
{self.numbers(since)}

## What happened, in order
{json.dumps(recent[-150:], indent=0)}

{JSON_NOTE}
{{"create": [{{"title": "factory: <summary>", "body": "the problem, the change, how we will know; ## Acceptance criteria"}}]}}
or {{"no_op": "why"}}""", self.repo.seed())
        created = self.create(self.agents.result(record).get("create", [])[:2], ["factory:proposed", "factory:improve"])
        self.log("improve", created, f"{len(created)} proposed", cost(record))
        return True

    # 9. planning
    def plan(self) -> bool:
        if any(self.gh.issues(label=label) for label in ("factory:proposed", "factory:revise", "factory:ready")):
            return False
        vision = (self.repo.root / "VISION.md").read_text() if (self.repo.root / "VISION.md").is_file() else ""
        digest = hashlib.sha256(vision.encode()).hexdigest()[:16]
        last = self.state["last_plan"]
        if digest == self.state["vision"] and last and \
                self.clock() - parse(last) < timedelta(hours=self.cfg["plan_every_hours"]):
            return False
        self.repo.start(self.branch, self.branch, fresh=True)
        record = self.turn("author", f"""Plan the next work. Compare VISION.md with the project as it is (your working directory)
and with the planned and waiting work (open-work.json). Propose what is missing, most important first:
at most five issues, each one change for one pull request, with what, why, and acceptance criteria a test
or a reader can check. If the project has no AGENTS.md or no real checks yet (.factory/check), propose
those first. If the vision needs a decision only the owner can make, propose an issue that asks it.
If nothing is missing, say so.

{JSON_NOTE}
{{"create": [{{"title": "<area>: <summary>", "body": "what and why, then ## Acceptance criteria"}}]}}
or {{"no_op": "why nothing is missing"}}""", {**self.repo.seed(), "open-work.json": self.open_work()})
        created = self.create(self.agents.result(record).get("create", [])[:5], ["factory:proposed"])
        self.state.update(vision=digest, last_plan=iso(self.clock()))
        self.log("plan", created, f"{len(created)} proposed", cost(record))
        return bool(created)

    def create(self, items: list, labels: list) -> list:
        made = []
        for item in items:
            if item.get("title"):
                made.append(self.gh.create_issue(str(item["title"]), str(item.get("body") or ""), labels)["number"])
        return made

    # the home issue: what the owner sees at a glance
    def render(self, note: str) -> str:
        c = self.cfg
        waiting = self.gh.issues(label="factory:needs-owner")
        lines = "\n".join(f"| {e['at'][5:16].replace('T', ' ')} | {e['do']} | {e['n'] if e['n'] is not None else ''} | {e['ok']} |"
                          for e in reversed(self.state["log"][-15:])) or "| | | | nothing yet |"
        return f"""# Software factory

{note}

**Today:** ${self.state['spent']:.2f} of ${c['budget_usd_per_day']:.2f}; {self.state['turns']} of {c['turns_per_day']} agent turns.

## Waiting on you
{chr(10).join(f"- #{i['number']} {i['title']}" for i in waiting) or "- nothing"}

## Last actions
| When (UTC) | What | On | Outcome |
|---|---|---|---|
{lines}

To steer: edit `VISION.md`, open an issue, or comment anywhere, including here. To pause: set the repository
variable `FACTORY_ENABLED` to anything but `true`.

<!-- factory:state {json.dumps(self.state)} -->"""


def main(argv: list[str]) -> int:
    root = Path.cwd()
    token = os.environ["GITHUB_TOKEN"]
    cfg = config(root)
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], token)
    factory = Factory(cfg, gh, Agents(os.environ.get("HI_URL", "http://127.0.0.1:8000"), root,
                                      sandboxed=os.environ.get("GITHUB_ACTIONS") == "true"),
                      Repo(root, token), os.environ["OWNER"])
    task = argv[1] if len(argv) > 1 else "tick"
    if factory.tick(force=task):
        gh.dispatch("tick", cfg["branch"])            # more to do: the next tick starts now
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
