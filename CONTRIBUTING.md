# Contributing to Relay

Thanks for your interest! Relay is a small, stdlib-only Python CLI that routes
coding tasks to the cheapest capable agentic framework.

## Dev setup

    git clone https://github.com/sm1ley68/Relay.git
    cd Relay
    pip install -e ".[dev]"
    python -m pytest -q        # 90+ tests, should be green

## Ground rules

- **Stdlib only at runtime.** No third-party runtime deps (the one exception is
  `pyreadline3` on Windows, marker-gated). Keep it that way.
- **TDD.** Every change ships with a test. Run `python -m pytest -q` before a PR.
- **Small, focused modules.** `cli` (interface), `router` (classification),
  `runner` (spawning frameworks), `escalate`, `budget`, `journal`, `config`.
- **No behavior change without a test that proves it.**

## Adding a framework

Frameworks are pluggable via `config.toml` `[frameworks.*]`:

    [frameworks.mytool]
    cmd = 'mytool run "{prompt}"'
    format = "text"          # or a json parser you add in runner._StreamParser
    auto = ["--yes"]

`format = "text"` streams any CLI live (no token stats). To surface tokens/cost,
add a parser branch to `runner._StreamParser` and map its format in `_JSON_KINDS`.

## PRs

- Branch from `main`, keep the diff focused, describe what and why.
- CI (GitHub Actions) runs the test suite on every PR — keep it green.
- By contributing you agree your work is licensed under the project's
  [PolyForm Noncommercial 1.0.0](LICENSE).
