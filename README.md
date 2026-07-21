# Relay — AI-model router

Single CLI (`relay`) that routes a natural-language task to the cheapest capable agentic
framework: `opencode` + OpenRouter for levels L0–L2 (free/cheap), `claude` (Pro)
for L3. Conserves the Claude Pro limit by sending routine work to cheap models
and escalating only on failure.

Ladder: **L0** North Mini Code (free) · **L1** Laguna M.1 (free) ·
**L2** DeepSeek V4 Flash ($0.14/$0.28 per 1M) · **L3** Claude Code (Pro subscription).

## Quick start

    git clone <repo> && cd Relay
    ./setup.sh          # installs relay, checks tools, seeds ~/.orchestrator/.env
    # put your OpenRouter key in ~/.orchestrator/.env
    relay doctor        # verify Python / opencode / claude / key are ready
    cd ~/your-project && relay

## Install

    pip install -e .

Requires: Python 3.13, `opencode` (`npm i -g opencode-ai`) and `claude` on PATH,
and an OpenRouter API key.

Provide the key in any of these (checked in order; a real shell variable always wins):

1. `export OPENROUTER_API_KEY=...` in your shell
2. `./.env` in the current project (project-specific override)
3. `~/.orchestrator/.env` — the global location, so `relay` finds the key from
   any directory (`OPENROUTER_API_KEY="sk-or-..."`)

`.env` files are loaded on startup and never override a real shell variable. The
same key is passed through to `opencode`, which reaches the L0–L3 models via its
`openrouter/` provider.

## Use

Interactive session (like `claude` / `gemini`) — just run `relay` with no arguments.
For normal use you don't need any flags — type the task, the level is auto-chosen:

    relay
    ❯ добавь докстринги в utils.py
    ❯ /l3 redesign the billing module      # force a level

**Session modes** — set once, applied to every task, shown in the prompt (no need
to retype flags):

    ❯ /dry                 # dry-run mode on   → prompt becomes  [dry] ❯
    ❯ /auto                # agent edits files without asking permission
    ❯ /steps 10            # cap agent steps   → [dry steps=10] ❯
    ❯ /test pytest -q      # run tests after each task, escalate on failure
    ❯ /guard off           # skip the checkpoint commit
    ❯ /undo                # roll back the last task's changes
    ❯ /stats               # analytics: tasks per level, cost, escalation rate
    ❯ /journal             # decision log · /config settings · /help all commands

Command history (↑/↓, Ctrl-R search), line editing and `/`-command tab-completion
work in the REPL. Subcommands also work one-shot: `relay stats`, `relay doctor`.

One-shot (task as arguments):

    relay "rename the variable foo to bar"        # auto-routed
    relay /l3 "redesign the billing module"       # forced level
    relay --dry-run "add a test for parse_args"    # show, don't run
    relay journal                                  # view decision log

(`orchestrator` is a still-supported alias for `relay`.)

Levels: `/l0` trivial (North Mini Code) · `/l1` simple edits (Laguna M.1) ·
`/l2` workhorse (DeepSeek V4 Flash) · `/l3` architecture / hard bugs (Claude Code).

## How routing works

1. Explicit `/lN` prefix wins.
2. Heuristics: keyword lists + grep-based file count.
3. Cheap-LLM score (1–5) for the rest.

On failure (nonzero exit, step limit, loop, failing tests) the task escalates one
level up, carrying a journal + `git diff`. A checkpoint commit is made before any
agent runs — roll back with `git reset --hard <checkpoint>`.

Each run prints the model chosen (with the reason) and a token/context/cost
footer parsed from the framework's json stream — both opencode (L0–L2) and
Claude Code (L3), e.g.:

    → L2 · openrouter/deepseek/deepseek-v4-flash  (классификатор LLM: сложность 3/5)
    …answer…
      ⛁ токены: 8.4K in · 4 out · 16 reasoning · контекст 9.5K · $0.0009

The real cost is recorded in the decision journal.

## Config

Edit `orchestrator/config.toml`: model ladder (fallback lists), framework command
templates, step limit, cost ceiling, Pro-window thresholds.

## Safeguards

Neither `opencode` nor `claude` exposes a step-limit flag, so Relay enforces both
limits itself by watching the framework's json stream and terminating the process:

- **Per-task step limit** (`--max-steps N`, default `max_steps` in config). Steps
  are counted from the stream (opencode `step_finish` events / claude assistant
  turns). Exceeding it terminates the run and **escalates** one level up
  (a stuck cheap model → a better one).
- **Cost ceiling** (`cost_ceiling_usd` in config). Cumulative real cost is summed
  from the stream; exceeding it terminates the run and **stops** the task without
  escalating (escalating would cost more). Exit code 6.

Escalation triggers: step limit, nonzero exit, repeated-output loop, and an
optional failing `--test-cmd`. Real per-task cost is recorded in the journal.
