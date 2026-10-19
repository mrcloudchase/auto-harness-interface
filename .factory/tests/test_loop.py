"""The factory's loop, end to end, with stand-ins for GitHub and the agents and a real git origin.
Each test states one promise of .factory/README.md. Run: python3 -m unittest discover -s .factory/tests"""
import itertools
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))

import loop  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class FakeGitHub:
    """GitHub as the loop sees it, in memory; pull request heads come from the real origin."""

    def __init__(self, origin: Path, clock: Clock, owner="owner"):
        self.origin, self.clock, self.owner = origin, clock, owner
        self.ids = itertools.count(1)
        self.items, self.comments_ = {}, []
        self.statuses, self.checks, self.reviews_ = {}, {}, {}
        self.dispatched, self.merged, self.requested, self.reactions = [], [], [], []

    def _now(self):
        return loop.iso(self.clock())

    def _sha(self, ref):
        return git(self.origin, "rev-parse", ref)

    # issues
    def issue(self, n):
        return self.items[int(n)]

    def issues(self, label="", state="open", creator="", since=""):
        return [i for i in self.items.values() if "pull_request" not in i and (state == "all" or i["state"] == state)
                and (not label or label in {x["name"] for x in i["labels"]})
                and (not creator or i["user"]["login"] == creator) and (not since or i["created_at"] > since)]

    def create_issue(self, title, body, labels, assignees=(), user="github-actions[bot]"):
        n = next(self.ids)
        self.items[n] = {"number": n, "title": title, "body": body, "labels": [{"name": x} for x in labels],
                         "state": "open", "created_at": self._now(), "user": {"login": user},
                         "assignees": [{"login": a} for a in assignees]}
        return self.items[n]

    def edit_issue(self, n, **fields):
        self.items[int(n)].update(fields)

    def comment(self, n, body, user="github-actions[bot]"):
        kind = "pull" if "pull_request" in self.items[int(n)] else "issues"
        c = {"id": next(self.ids) + 10_000, "body": body, "user": {"login": user}, "created_at": self._now(),
             "issue_url": f"https://api/repos/o/r/issues/{n}", "html_url": f"https://github/o/r/{kind}/{n}#c"}
        self.comments_.append(c)
        return c

    def comments(self, n):
        return [c for c in self.comments_ if c["issue_url"].endswith(f"/{n}")]

    def owner_comments(self, login, since):
        return [c for c in self.comments_ if c["user"]["login"] == login and c["created_at"] > since]

    def react(self, what, number):
        self.reactions.append((what, number))

    def add_labels(self, n, labels):
        have = {x["name"] for x in self.items[int(n)]["labels"]}
        self.items[int(n)]["labels"] += [{"name": x} for x in labels if x not in have]

    def remove_label(self, n, label):
        self.items[int(n)]["labels"] = [x for x in self.items[int(n)]["labels"] if x["name"] != label]

    def has(self, n, label):
        return label in {x["name"] for x in self.items[int(n)]["labels"]}

    def ensure_label(self, *a):
        pass

    def assign(self, n, login):
        self.items[int(n)]["assignees"].append({"login": login})

    # pull requests
    def pulls(self):
        out = []
        for p in self.items.values():
            if "pull_request" in p and p["state"] == "open":
                p["head"]["sha"] = self._sha(f"refs/heads/{p['head']['ref']}")
                git(self.origin, "update-ref", f"refs/pull/{p['number']}/head", p["head"]["sha"])
                out.append(p)
        return out

    def create_pull(self, title, head, base, body, user="github-actions[bot]"):
        pr = self.create_issue(title, body, [], user=user)
        pr.update(pull_request={}, head={"ref": head, "sha": ""}, base={"ref": base})
        self.pulls()
        return pr

    def merged_since(self, since):
        return [p for p in self.items.values() if p.get("merged_at", "") > since]

    def pull_files(self, n):
        p = self.items[int(n)]
        return git(self.origin, "diff", "--name-only", f"{p['base']['ref']}...{p['head']['sha']}").splitlines()

    def merge(self, n, sha, title, message):
        p = self.items[int(n)]
        assert p["head"]["sha"] == sha, "merged a commit nobody checked"
        p.update(state="closed", merged_at=self._now())
        self.merged.append({"number": n, "title": title, "message": message})

    def delete_branch(self, branch):
        git(self.origin, "update-ref", "-d", f"refs/heads/{branch}")

    def reviews(self, n):
        return self.reviews_.get(int(n), [])

    def review(self, n, login, state, sha):
        self.reviews_.setdefault(int(n), []).append({"user": {"login": login}, "state": state, "commit_id": sha})

    def request_review(self, n, login):
        self.requested.append((n, login))

    # commits
    def branch_sha(self, branch):
        return self._sha(f"refs/heads/{branch}")

    def check(self, sha):
        return self.checks.get(sha)

    def finish_check(self, sha, conclusion, message=""):
        self.checks[sha] = {"id": sha, "status": "completed", "conclusion": conclusion, "message": message}

    def failure(self, check):
        return check.get("message") or "failed"

    def status(self, sha, context):
        return self.statuses.get((sha, context), ("", ""))

    def set_status(self, sha, context, state, description):
        self.statuses[(sha, context)] = (state, description)

    def dispatch(self, task, ref):
        self.dispatched.append((task, ref))


class FakeAgents:
    """Scripted turns: `script(role, text)` returns {"files": {path: text | None}, "result": {...}, "answer": "", "cost": usd}."""

    def __init__(self, script):
        self.script, self.turns, self._done = script, [], {}

    def turn(self, role, text, files):
        did = self.script(role, text) or {}
        rid = f"resp_{len(self.turns)}"
        self.turns.append({"role": role, "text": text, "files": files})
        self._done[rid] = did
        return {"id": rid, "output": [{"content": [{"text": did.get("answer", "")}]}],
                "harness": {"runtime": "docker", "cost_usd": did.get("cost", 0.1)}}

    def changes(self, record):
        return {p: (v.encode() if isinstance(v, str) else v) for p, v in self._done[record["id"]].get("files", {}).items()}

    def result(self, record):
        return self._done[record["id"]].get("result", {})


def script(**by_role):
    """Each role's turns, in order; a role with no turns left approves (reviewer) or does nothing (author)."""
    queues = {role: iter(turns) for role, turns in by_role.items()}

    def play(role, text):
        default = {"result": {"verdict": "approve"}} if role == "reviewer" else {}
        return next(queues.get(role, iter(())), default)
    return play


ADD = {"files": {"src/app.py": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"},
       "result": {"title": "app: subtract", "summary": "Added sub, with its test."}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin, self.root = self.tmp / "origin.git", self.tmp / "project"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.root.mkdir()
        shutil.copytree(TEMPLATE / ".factory", self.root / ".factory", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        (self.root / ".github" / "workflows").mkdir(parents=True)
        shutil.copy(TEMPLATE / ".github" / "workflows" / "factory.yml", self.root / ".github" / "workflows")
        (self.root / "VISION.md").write_text("# Vision\nA calculator.\n")
        (self.root / "AGENTS.md").write_text("# Agents\nPython.\n")
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_app.py").write_text("def test_add():\n    assert True\n")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "start")
        git(self.root, "remote", "add", "origin", str(self.origin))
        git(self.root, "push", "-q", "origin", "main")
        self.clock = Clock()
        self.gh = FakeGitHub(self.origin, self.clock)
        self.gh.finish_check(self.gh.branch_sha("main"), "success")
        self.cfg = dict(loop.DEFAULTS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def factory(self, play):
        self.agents = FakeAgents(play)
        return loop.Factory(self.cfg, self.gh, self.agents, loop.Repo(self.root), "owner", self.clock)

    def ready(self, title="app: subtract", body="Add sub.\n\n## Acceptance criteria\n- sub(3, 1) == 2"):
        return self.gh.create_issue(title, body, ["factory:ready"])["number"]

    def the_pr(self):
        return next(p for p in self.gh.items.values() if "pull_request" in p)


class Planning(Base):
    def test_with_nothing_planned_the_author_plans_from_the_vision_and_the_reviewer_checks_it(self):
        plan = {"result": {"create": [{"title": "app: subtract", "body": "## Acceptance criteria\n- sub works"}]}}
        f = self.factory(script(author=[plan], reviewer=[{"result": {"verdict": "approve"}}]))
        self.assertTrue(f.tick())
        n = next(i["number"] for i in self.gh.issues(label="factory:proposed"))
        self.assertIn("VISION.md", self.agents.turns[0]["files"])               # the author saw the vision
        self.assertTrue(f.tick())                                               # the reviewer reviews it next
        self.assertTrue(self.gh.has(n, "factory:ready") and not self.gh.has(n, "factory:proposed"))
        self.assertEqual([t["role"] for t in self.agents.turns], ["author", "reviewer"])

    def test_a_plan_sent_back_is_revised_and_reviewed_again(self):
        n = self.gh.create_issue("app: things", "vague", ["factory:proposed"])["number"]
        f = self.factory(script(reviewer=[{"result": {"verdict": "changes", "findings": "say which things"}}],
                                author=[{"result": {"title": "app: subtract", "body": "## Acceptance criteria\n- sub"}}]))
        f.tick()
        self.assertTrue(self.gh.has(n, "factory:revise"))
        f.tick()
        self.assertIn("say which things", self.agents.turns[1]["text"])         # the author got the review verbatim
        self.assertEqual(self.gh.issue(n)["title"], "app: subtract")
        self.assertTrue(self.gh.has(n, "factory:proposed"))

    def test_a_healthy_project_plans_once_a_day_and_otherwise_does_nothing(self):
        f = self.factory(script(author=[{"result": {"no_op": "nothing missing"}}]))
        self.assertFalse(f.tick())
        self.assertFalse(f.tick())
        self.assertEqual(len(self.agents.turns), 1)                             # no churn
        self.assertIn("Nothing to do", self.gh.issue(f.home["number"])["body"])


class Building(Base):
    def test_a_ready_issue_becomes_a_pull_request_whose_check_is_started(self):
        n = self.ready()
        f = self.factory(script(author=[ADD]))
        self.assertTrue(f.tick())
        pr = self.the_pr()
        self.assertEqual((pr["head"]["ref"], pr["base"]["ref"]), (f"factory/{n}", "main"))
        self.assertIn(f"Closes #{n}", pr["body"])
        self.assertIn(("check", f"factory/{n}"), self.gh.dispatched)
        self.assertIn("sub", git(self.origin, "show", f"factory/{n}:src/app.py"))
        self.assertNotIn(".github/workflows/factory.yml", self.agents.turns[0]["files"])   # agents never see it

    def test_it_merges_only_with_a_passing_check_and_the_reviewers_approval_on_that_commit(self):
        n = self.ready()
        f = self.factory(script(author=[ADD], reviewer=[{"result": {"verdict": "approve"}}]))
        f.tick()
        pr = self.the_pr()
        self.assertFalse(f.tick() and self.gh.merged)                           # no check yet: nothing merges
        self.gh.finish_check(self.gh.pulls()[0]["head"]["sha"], "success")
        f.tick()                                                                # the reviewer reviews
        self.assertEqual(self.gh.status(pr["head"]["sha"], loop.REVIEW)[0], "success")
        self.assertIn("### Review", self.gh.comments(pr["number"])[-1]["body"])
        f.tick()                                                                # then it merges
        self.assertEqual(len(self.gh.merged), 1)
        self.assertIn(f"Closes #{n}", self.gh.merged[0]["message"])
        self.assertEqual(self.gh.issue(n)["state"], "closed")                  # its issue is done
        self.assertFalse(self.gh.has(n, "factory:ready"))
        self.assertEqual(git(self.origin, "branch", "--list", f"factory/{n}"), "")

    def test_a_failed_check_and_a_review_asking_for_changes_are_fixed_on_the_same_branch(self):
        self.ready()
        fixed = {"files": {"src/app.py": "def sub(a, b):\n    return a - b  # fixed\n"}, "result": {"summary": "fixed"}}
        f = self.factory(script(author=[ADD, fixed, fixed | {"files": {"src/app.py": "# reviewed\n"}}],
                                reviewer=[{"result": {"verdict": "changes", "findings": "src/app.py:1 is wrong"}}]))
        f.tick()
        first = self.gh.pulls()[0]["head"]["sha"]
        self.gh.finish_check(first, "failure", "AssertionError: sub(3, 1) != 2")
        f.tick()
        self.assertIn("sub(3, 1) != 2", self.agents.turns[1]["text"])          # the failure, verbatim
        second = self.gh.pulls()[0]["head"]["sha"]
        self.assertNotEqual(first, second)
        self.gh.finish_check(second, "success")
        f.tick()                                                                # review: changes
        f.tick()                                                                # the author addresses them
        self.assertIn("src/app.py:1 is wrong", self.agents.turns[3]["text"])

    def test_a_change_touching_the_owners_paths_waits_for_the_owners_approval(self):
        self.ready()
        f = self.factory(script(author=[{"files": {".factory/config.toml": "tries = 5\n"}, "result": {"title": "factory: more tries"}}]))
        f.tick()
        sha = self.gh.pulls()[0]["head"]["sha"]
        self.gh.finish_check(sha, "success")
        f.tick()                                                                # reviewed
        f.tick()                                                                # asks the owner
        pr = self.the_pr()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.gh.requested, [(pr["number"], "owner")])
        self.assertFalse(f.tick())                                              # and waits, quietly
        self.gh.review(pr["number"], "owner", "APPROVED", sha)
        f.tick()
        self.assertEqual(len(self.gh.merged), 1)

    def test_no_change_may_touch_the_vision_or_workflows_or_delete_tests(self):
        n = self.ready()
        for bad in ({"VISION.md": "# mine now\n"}, {".github/workflows/x.yml": "on: push\n"}, {"tests/test_app.py": None}):
            f = self.factory(script(author=[{"files": bad, "result": {"title": "x: y"}}]))
            f.tick()
            self.assertIn("not pushed", self.gh.comments(n)[-1]["body"])
        self.assertEqual([p for p in self.gh.items.values() if "pull_request" in p], [])

    def test_after_its_tries_an_issue_goes_to_the_owner(self):
        n = self.ready()
        f = self.factory(script(author=[{"files": {}, "result": {}}] * 3))
        for _ in range(4):
            f.tick()
        self.assertTrue(self.gh.has(n, "factory:needs-owner") and not self.gh.has(n, "factory:ready"))
        self.assertIn({"login": "owner"}, self.gh.issue(n)["assignees"])
        self.assertEqual(len(self.agents.turns), 3)

    def test_a_question_only_the_owner_can_answer_goes_to_the_owner_at_once(self):
        n = self.ready()
        f = self.factory(script(author=[{"result": {"blocked": "SQLite or Postgres? I recommend SQLite."}}]))
        f.tick()
        self.assertTrue(self.gh.has(n, "factory:needs-owner"))
        self.assertIn("SQLite or Postgres", self.gh.comments(n)[-1]["body"])

    def test_a_red_main_is_fixed_before_anything_else(self):
        older = self.ready("app: something else")
        self.gh.finish_check(self.gh.branch_sha("main"), "failure", "tests/test_app.py failed")
        f = self.factory(script(author=[ADD]))
        f.tick()
        urgent = self.gh.issues(label="factory:urgent")[0]["number"]
        f.tick()
        self.assertEqual(self.the_pr()["head"]["ref"], f"factory/{urgent}")
        self.assertNotEqual(urgent, older)

    def test_a_main_with_no_check_gets_one(self):
        self.gh.checks.clear()
        f = self.factory(script(author=[{"result": {"no_op": "fine"}}]))
        f.tick()
        self.assertIn(("check", "main"), self.gh.dispatched)


class TheOwner(Base):
    def test_what_the_owner_writes_is_acted_on_once_and_answered(self):
        n = self.gh.create_issue("Digest", "...", ["factory:digest"])["number"]
        f = self.factory(script(author=[{"result": {"no_op": "x"}},
                                        {"result": {"replies": [{"number": n, "text": "Will do."}],
                                                    "create": [{"title": "app: divide", "body": "## Acceptance criteria\n- div"}]}}]))
        f.tick()                                                                # sets the cursor
        self.clock.advance(minutes=5)
        self.gh.comment(n, "Add division next.", user="owner")
        self.gh.comment(n, "I am not the owner.", user="stranger")
        f.tick()
        steer = self.agents.turns[-1]["text"]
        self.assertIn("Add division next.", steer)
        self.assertNotIn("I am not the owner", steer)                          # only the owner steers
        self.assertIn("Will do.", self.gh.comments(n)[-1]["body"])
        self.assertTrue(any(i["title"] == "app: divide" for i in self.gh.issues(label="factory:proposed")))
        turns = len(self.agents.turns)
        f.tick()
        self.assertFalse(any("Add division next." in t["text"] for t in self.agents.turns[turns:]))   # once

    def test_an_issue_the_owner_opens_is_direction(self):
        f = self.factory(script(author=[{"result": {"no_op": "x"}}, {"result": {"update": [{"number": 2, "status": "ready"}]}}]))
        f.tick()
        self.clock.advance(minutes=5)
        n = self.gh.create_issue("Support negative numbers", "please", [], user="owner")["number"]
        f.tick()
        self.assertTrue(self.gh.has(n, "factory:ready"))

    def test_the_owners_comment_on_a_factory_pull_request_is_review_the_author_must_address(self):
        self.ready()
        f = self.factory(script(author=[ADD, {"files": {"src/app.py": "# renamed\n"}, "result": {"summary": "renamed"}}]))
        f.tick()
        pr = self.the_pr()
        self.gh.finish_check(pr["head"]["sha"], "success")
        self.clock.advance(minutes=5)
        self.gh.comment(pr["number"], "Call it subtract, not sub.", user="owner")
        f.tick()                                                                # read: review fails on the owner's word
        self.assertEqual(self.gh.status(pr["head"]["sha"], loop.REVIEW)[0], "failure")
        f.tick()
        self.assertIn("Call it subtract", self.agents.turns[-1]["text"])


class Brakes(Base):
    def test_out_of_budget_means_no_more_turns_today(self):
        self.cfg["turns_per_day"] = 1
        self.ready()
        self.ready("app: two")
        f = self.factory(script(author=[ADD, ADD]))
        f.tick()
        self.assertFalse(f.tick())
        self.assertEqual(len(self.agents.turns), 1)
        self.assertIn("budget is spent", self.gh.issue(f.home["number"])["body"])
        self.clock.advance(days=1)
        f.tick()
        self.assertEqual(len(self.agents.turns), 2)                             # a new day, a new budget

    def test_state_lives_on_github_only(self):
        f = self.factory(script(author=[{"result": {"no_op": "x"}}]))
        f.tick()
        again = self.factory(script())
        again.load()
        self.assertEqual(again.state["last_plan"], f.state["last_plan"])
        self.assertEqual(len(self.gh.issues(label="factory:home")), 1)


class Reporting(Base):
    def test_the_digest_and_the_retrospective_come_weekly(self):
        f = self.factory(script(author=[{"result": {"no_op": "x"}}, {"answer": "Shipped nothing yet."},
                                        {"result": {"create": [{"title": "factory: plan smaller", "body": "why"}]}}]))
        f.tick()
        self.clock.advance(days=8)
        f.tick()
        digest = self.gh.issues(label="factory:digest")[0]
        self.assertIn("Shipped nothing yet.", digest["body"])
        self.assertIn("| What happened | Times |", digest["body"])              # numbers counted by the loop
        self.assertIn({"login": "owner"}, digest["assignees"])
        f.tick()
        improve = self.gh.issues(label="factory:improve")[0]
        self.assertTrue(self.gh.has(improve["number"], "factory:proposed"))     # reviewed like any other plan


class Blind(unittest.TestCase):
    def test_no_factory_file_an_agent_can_see_names_a_harness_or_a_provider(self):
        # The factory's own files only: the project is free to name anything, harnesses included.
        seen = [p for p in (TEMPLATE / ".factory").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        for path in seen:
            text = path.read_text(errors="ignore").lower()
            for name in ("cla" "ude", "co" "dex", "anth" "ropic", "open" "ai"):     # split, so this file passes too
                self.assertNotIn(name, text, f"{path.relative_to(TEMPLATE)} names {name}")

    def test_build_noise_and_the_answer_file_are_never_changes(self):
        agents = loop.Agents("http://x", TEMPLATE, sandboxed=True)
        agents.file = lambda rid, p: b"x"
        record = {"id": "r", "harness": {"files": ["src/a.py", "src/__pycache__/a.pyc", ".pytest_cache/v", loop.RESULT,
                                                   "node_modules/x/i.js"], "deleted": ["old.py"]}}
        self.assertEqual(agents.changes(record), {"src/a.py": b"x", "old.py": None})


if __name__ == "__main__":
    unittest.main()
