# How the software factory works

The factory builds, maintains and improves this repository from `VISION.md`, inside its own GitHub
Actions, with nothing but the workflow's own token and two provider keys. You write the vision, read a
weekly digest, and steer whenever you like. Two AI agents do the work; one loop decides what happens next.

| Who | Does | Holds |
|---|---|---|
| **You, the owner** | the vision, steering, approvals only you can give | the repository and its settings |
| **The loop** (`.factory/loop.py`, run by `.github/workflows/factory.yml`) | decides what happens next, and does every action on GitHub: issues, branches, pull requests, merges | the workflow's token, for one run at a time |
| **The author** (an AI agent) | makes everything: plans, code, tests, docs, the digest | a copy of the project's files, in a sandbox |
| **The reviewer** (another AI agent) | checks everything the author makes, before it counts | a copy of the project's files, in a sandbox |
| **The check** (the `check` job) | runs the project's own checks on every change | read access only |

The author and the reviewer run on two different coding agents, each through harness-interface. Which is
which is set in `.github/workflows/factory.yml`, the one file neither agent ever sees. They know each
other only as "the author" and "the reviewer".

## You, the owner

**You are responsible for:**
- `VISION.md`: what the project is for, its principles, what matters most, and what it must not do.
- Answering what only you can decide: issues labelled `factory:needs-owner`, assigned to you.
- Approving changes to your paths (`owner_paths` in `config.toml`): `VISION.md`, the loop, the check,
  the configuration and `.github/`. The factory can propose them, but they merge only when you approve.
- The limits in `.factory/config.toml`: the daily budget, the tries, how often it plans and reports.
- The repository's settings and secrets (see the setup in the root README).

**How you send input.** The factory reads everything you write, and only what you write (comments by
anyone else are ignored). It reads it on its next tick: at once while it is running, otherwise when you
next start it.

| To | Do this | What happens |
|---|---|---|
| change direction | edit `VISION.md` and push | the next tick plans against the new vision |
| ask for something | open an issue, in plain words | the author turns it into planned work and answers you on it |
| say anything | comment on any issue, including the digest and the home issue | the author acts on it and answers in that thread; a 👀 shows it was read |
| change a pull request | comment on it | that counts as review: the author must address it before it can merge |
| approve your paths | approve the pull request | it merges at the next tick |
| skip the plan review | add the label `factory:ready` to an issue | the author builds it next |
| stop something | close the issue or pull request | the factory leaves it alone |
| start it | Actions → factory → Run workflow | it works until there is nothing to do or it needs you |
| pause everything | set the repository variable `FACTORY_ENABLED` to anything but `true` | nothing runs until you set it back |
| get a digest now | Actions → factory → Run workflow, task `digest` | it is written at once |

**What you receive:**

| Where | What | When |
|---|---|---|
| the **Digest** issue, assigned to you | what shipped, what's in progress, what's waiting on you, what went wrong, what's next, and numbers counted by the loop | weekly (`digest_every_days`) |
| the **Software factory** issue (label `factory:home`) | what's waiting on you, today's spend, the last actions | always current |
| issues labelled `factory:needs-owner`, assigned to you | a question, the options and a recommendation | when only you can decide |
| review requests | a change to your paths that passed its check and its review | when one is ready |
| pull requests and commits | every change, with its review and its check | as they happen |
| each run's artifact | every agent turn's record and log | 30 days |

## The agents

**What goes into an agent (its only input):**

| | The author | The reviewer |
|---|---|---|
| its instructions | `.factory/author.md` and `.factory/memory.md` | `.factory/reviewer.md` and `.factory/memory.md` |
| the project | the repository's text files (each up to 200 KB, 4 MB in all), as of the work's branch | the same, as the change leaves them, plus the change as `change.diff` |
| the task | the issue it is working on, the failure or review to address (verbatim), your words, or the factory's numbers | the issue or plan it is judging |
| never | your words, unless they are about the task; any credential but its own provider's; `factory.yml`; the other agent's identity | the same |

**What comes out of an agent (all it can touch):** the files in its own sandbox, and one answer file,
`.factory-result.json`. It cannot reach GitHub, git, your secrets or the network beyond its model
provider and the package indexes in `egress`. The loop decides what to do with what it leaves:

| The agent leaves | The loop |
|---|---|
| the author's changed files | commits them to `factory/<issue>`, opens or updates the pull request, starts the check |
| the author's plan (`create`, `update`, `replies`) | creates or edits issues and posts the replies, as `### Author` |
| the author's `blocked` question | labels the issue `factory:needs-owner`, assigns it to you, posts the question |
| the author's digest text | posts it as the Digest issue, with numbers the loop counted itself |
| the reviewer's verdict | sets the `factory/review` status on that exact commit and posts the findings, as `### Review` |
| anything else | nothing: the reviewer's file changes and any other output are ignored |

**What no agent change can ever do, whatever the review says:** touch `VISION.md` or
`.github/workflows/`, or delete a test file. The loop refuses it before anything is pushed. GitHub
itself refuses workflow changes from the workflow's token.

## The loop

Each tick does the first of these that applies, then exits; when it did something, the next tick starts
at once. Ticks never overlap. **You start it:** Actions → factory → Run workflow. One start runs until
there is nothing to do, it needs you, or a brake stops it; then it waits for your next start. (To run on
its own, every hour and whenever you write, uncomment the `auto` lines in `factory.yml`.)

1. **You wrote something new:** the author acts on it and answers you.
2. **`main`'s check failed:** an urgent fix issue, built before anything else.
3. **A pull request needs something:** start its check; or the reviewer reviews it; or the author fixes
   what failed; or it merges.
4. **A proposed issue is unreviewed:** the reviewer reviews the plan.
5. **A reviewed plan was sent back:** the author revises it.
6. **A ready issue has no pull request:** the author builds it.
7. **The digest is due:** the author writes it.
8. **The retrospective is due:** the author proposes improvements to the factory itself.
9. **Nothing else:** the author plans the next work from `VISION.md`, at most once a day unless the vision changed.

**Work moves through labels:** `factory:proposed` → (reviewed) → `factory:ready` → a pull request → merged.
A plan the reviewer sends back is `factory:revise`. Anything stuck is `factory:needs-owner`.

**A pull request merges only when** its `check` passed and the reviewer approved, both on its latest
commit; and, if it touches your paths, you approved that commit too. It is squash-merged, so every commit
on `main` is one reviewed change.

**Brakes:** one action per tick; `tries` author turns per issue before it comes to you; `open_prs` pull
requests at once; a daily budget in reported dollars and a hard cap in turns; `FACTORY_ENABLED`.

## How it improves itself

Weekly, the author runs a retrospective on the loop's own record: what was sent back, what failed, what
escalated, what it cost. It proposes at most two changes to how the factory works, each naming the
problem in the numbers, the change, and how next week's numbers will show whether it worked. They go
through the same plan review, build, check and code review as any other work.

| It can change | Merges after |
|---|---|
| `.factory/author.md`, `.factory/reviewer.md` (how the agents work) | the check and the reviewer |
| `.factory/memory.md` (lessons both agents follow) | the check and the reviewer |
| `.factory/loop.py`, `.factory/check`, `.factory/config.toml` (the loop, what passing means, the limits) | the check, the reviewer and you |
| `.github/workflows/factory.yml` | never by the factory: it writes the change out for you to make |

The digest shows the same numbers each week, so you can see whether a change helped.

## Safety

- **The token never meets project code.** The `tick` job has the token and runs no project code; the
  `check` job runs project code with read access only and no secrets.
- **Agents run in sandboxes.** Each turn is a fresh container that can reach only its model provider
  and `egress`. In Actions the loop refuses any turn that didn't run in one.
- **One writer.** Ticks share a concurrency group; all state lives in GitHub (labels, comments, and the
  hidden state in the home issue), so a lost runner loses nothing.
- **Only you steer.** Only the owner's comments and issues are read as direction; the owner is
  `FACTORY_OWNER`, or the repository's owner.

## Files

| File | Owner | Purpose |
|---|---|---|
| `VISION.md` | you | the direction |
| `AGENTS.md` | the agents | the project's handbook: layout, commands, conventions |
| `.factory/config.toml` | you | limits and your paths |
| `.factory/check` | you | what "passing" means |
| `.factory/loop.py` | you (the factory may propose) | the loop |
| `.factory/author.md`, `reviewer.md`, `memory.md` | the factory (after review) | how the agents work |
| `.factory/sandbox.Dockerfile` (optional) | you | the project's toolchain for the agents, `FROM harness-interface/default:base` |
| `.factory/tests/` | the factory (after review) | the loop's own tests, run by every check |
| `.github/workflows/factory.yml` | you | the three jobs, and which agent is which |
