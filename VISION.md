# Vision

The owner's file: the factory reads it and never changes it.

## North star

harness-interface is the execution layer for coding harnesses: the command-line coding agents people
already use. One request reaches any harness, from a CLI (`hi`), an OpenAI Responses-compatible API or a
Python library, so people can build agents, loops and workflows on whichever harness fits, with every
run sandboxed and recorded. It manages execution, not harness protocol.

This repository starts empty. The factory builds harness-interface here from this file.

## Principles

1. **Harnesses live outside the server.** A harness is a folder of configuration that users set up to
   run their way: what to install, how to invoke it, how to read its answer. The shipped harnesses are
   templates. The server has no code for any particular harness.
2. **Agnostic, with a closed contract.** The execution layer doesn't care that it runs a harness: it
   gives a program its inputs (a prompt, files, environment) and returns its outputs (an answer, files,
   usage), streamed or not. The contract is never extended, so any harness, today's or tomorrow's, fits.
3. **Configuration is infrastructure users bring.** Providers (a model API's URL and the name of the
   environment variable that holds its key), sandboxes (a Dockerfile and the hosts it may reach) and
   harnesses are resources that users own, kept apart from the server and changed with no restart.
4. **Strict sandbox by default, overridable.** Every run is a fresh container that can reach only its
   model provider, unless the user's sandbox allows more. Keys are injected at run time, never baked in.
5. **Simple enough to read.** Keep it simple and say each thing once. Give each module one job, use
   objects for state, and make what must never happen impossible. The library stays near 2,000 lines.

## Priorities

In order: the factory works on the first before the second.

1. **The foundation.** A Python package with a passing test suite, `.factory/check` running it, and
   `AGENTS.md` describing the layout. The library is in layers, each using only those below it, and
   there is a test that enforces that:
   - execution;
   - runtimes (a local process, and Docker with an egress allow-list);
   - the workspace;
   - the harness contract and one harness turn;
   - resources (providers, sandboxes, harnesses) stored on disk;
   - the platform that ties them together.
2. **Run a harness from the library.** Load a harness folder, run one turn in a sandbox, get the
   answer, the session and the usage back, with every run recorded on disk.
3. **The API and the CLI.** An HTTP server with an OpenAI Responses-compatible `POST /responses`
   (`model` names the harness, `previous_response_id` continues its conversation, streaming optional),
   plus endpoints to manage providers, sandboxes and harnesses. `hi` drives the same server.
4. **Shipped harnesses.** Templates for the popular coding CLIs, each reporting its answer, session,
   usage and cost as fully as its CLI allows. The docs say exactly what each one reports.
5. **Runs anywhere, fast.** It installs and runs cleanly as root, in containers and in CI, with clear
   errors when something is missing. A run starts in seconds once the sandbox image exists.
6. **Docs for users and contributors.** Getting started, how it works, the CLI, the API and the
   library, each true to the code.

## Non-goals

- Translating harnesses into a common event schema or protocol.
- Adding to the harness contract.
- Spending caps or budgets enforced by the server: loops own their budgets.
- Per-request overrides of a harness's configuration: a different configuration is a different harness.
- Authentication or multi-tenancy, for now. The server listens on 127.0.0.1 only.

## Constraints

- Python 3.11 and later; Linux and macOS. Apache 2.0, as in `LICENSE`.
- The library uses only Python's standard library. The server and the CLI may add small,
  well-known dependencies.
- Tests run without model provider keys and without network access to model providers. Tests that need
  Docker skip cleanly when it is not available.
