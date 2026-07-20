# Relay — AI-model router

Single CLI (`relay`) that routes a natural-language task to the cheapest capable agentic
framework: `opencode` + OpenRouter for levels L0–L2 (free/cheap), `claude` (Pro)
for L3. Conserves the Claude Pro limit by sending routine work to cheap models
and escalating only on failure.

Ladder: **L0** North Mini Code (free) · **L1** Laguna M.1 (free) ·
**L2** DeepSeek V4 Flash ($0.14/$0.28 per 1M) · **L3** Claude Code (Pro subscription).

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

Interactive session (like `claude` / `gemini`) — just run `relay` with no arguments:

    relay
    relay> /l3 redesign the billing module
    relay> --dry-run add a test for parse_args
    relay> journal
    relay> exit                       # or Ctrl-D

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

Each run prints the model chosen (with the reason) and, for OpenRouter levels, a
token/context/cost footer parsed from opencode's json stream, e.g.:

    → L2 · openrouter/deepseek/deepseek-v4-flash  (классификатор LLM: сложность 3/5)
    …answer…
      ⛁ токены: 8.4K in · 4 out · 16 reasoning · контекст 9.5K · $0.0009

The real cost is recorded in the decision journal.

## Config

Edit `orchestrator/config.toml`: model ladder (fallback lists), framework command
templates, step limit, cost ceiling, Pro-window thresholds.

## Not yet enforced

- **Per-task step limit.** `--max-steps` / `max_steps` is accepted and stored,
  but the default `opencode_cmd` / `claude_cmd` command templates in
  `config.toml` have no `{steps}` placeholder, so the limit is never actually
  passed to the underlying framework process.
- **Cost ceiling.** `cost_ceiling_usd` and per-task cost are not enforced or
  recorded; every journal entry currently logs `cost_usd` as `0.0`.

The escalation triggers that do work today are a nonzero exit code from the
framework and (optionally) a failing `--test-cmd`. Wiring up step-limit and
cost enforcement is a follow-up.
