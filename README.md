# Orchestrator — AI-model router

Single CLI that routes a natural-language task to the cheapest capable agentic
framework: `opencode` + OpenRouter for levels L0–L3 (free/cheap), `claude` (Pro)
for L4. Conserves the Claude Pro limit by sending routine work to cheap models
and escalating only on failure.

## Install

    pip install -e .

Requires: Python 3.13, `opencode` and `claude` on PATH, `OPENROUTER_API_KEY` set.

## Use

    orchestrator "rename the variable foo to bar"        # auto-routed
    orchestrator /l4 "redesign the billing module"       # forced level
    orchestrator --dry-run "add a test for parse_args"    # show, don't run
    orchestrator journal                                  # view decision log

Levels: `/l0` trivial · `/l1` simple edits · `/l2` workhorse · `/l3` long
sessions · `/l4` architecture / hard bugs.

## How routing works

1. Explicit `/lN` prefix wins.
2. Heuristics: keyword lists + grep-based file count.
3. Cheap-LLM score (1–5) for the rest.

On failure (nonzero exit, step limit, loop, failing tests) the task escalates one
level up, carrying a journal + `git diff`. A checkpoint commit is made before any
agent runs — roll back with `git reset --hard <checkpoint>`.

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
