---
name: covledger
description: Run pytest through CovLedger, select and semantically analyze the highest-priority uncovered behavior with bounded Jev cost, improve it, and verify the change.
---

# CovLedger agent workflow

Use this skill when a Python project uses CovLedger or the user asks you to improve current test coverage or quality with CovLedger.

## Default improve-one-gap loop

Run from the project root. Initialize only when CovLedger is not initialized:

```bash
covledger init
```

Establish fresh test evidence:

```bash
covledger run -- pytest -q
```

If the suite fails, diagnose and fix the failed suite first. Do not spend a Jev request on current-gap semantic analysis from a failed suite. `covledger run` is deterministic and makes zero Jev API requests.

Analyze the single highest-priority current gap:

```bash
covledger analyze --json
```

This is the default agent command. It selects the same target as `covledger next` using deterministic current evidence and permits at most one uncached Jev request. Existing content-addressed semantic results are reused automatically.

Read the first target's `primary_gap`, `gaps`, `priority`, `coverage`, `deterministic`, `semantic`, `proofs`, `source.function_text`, and `agent.recommended_sequence`, along with top-level `suite`, `coverage`, `selection`, and `cost`. The function source is live and hash-verified; do not make another lookup merely to retrieve it.

Inspect nearby existing tests before editing. Prefer the smallest coherent behavior-focused test that exercises the selected gap. Change production code only when the path is incorrect, unreachable, misleading, or a small testability refactor is justified by the evidence. CovLedger does not edit source or generate tests for you.

Run a focused pytest target when useful, then refresh the authoritative current analysis:

```bash
covledger run -- pytest -q
```

Verify that the selected gap was removed or reduced and that the suite still passes. Stop after one target unless the user explicitly asked for continued iteration or a coverage campaign.

## Cost-safe batch analysis

Do not use batch semantic analysis by default. Before a requested batch, inspect its complete cache and request preflight:

```bash
covledger analyze --all --plan --json
```

Planning makes zero Jev requests. If the user wants the batch and the uncached request count is acceptable, run it with an explicit hard budget:

```bash
covledger analyze --all --max-requests N --json
```

CovLedger checks every selected function's cache entry before the first uncached call. If the request budget is absent or insufficient for uncached batch targets, the batch is blocked before any new Jev request; it does not partially analyze the batch. Multiple gaps in one function still require only one function-level semantic request.

Use cached semantic results without network/API calls when requested:

```bash
covledger analyze --cache-only --json
```

Never use `--refresh` unless the user explicitly asks for fresh semantic judgments. Refresh ignores cache results for selected targets but never bypasses the hard request budget. Never clear the semantic cache merely to make results “fresh”.

`cost.api_requests` is the authoritative number of uncached Jev requests made by the command. Preserve returned model, request ID, and token usage when reporting semantic evidence. Missing usage remains unknown; do not replace it with zero or estimate a dollar amount.

## Command roles

- `covledger run`: run pytest + coverage and publish deterministic current evidence; no Jev calls.
- `covledger next`: inspect the deterministic next target; no Jev calls.
- `covledger inspect ID`: inspect current live source/proofs for a known ID; no Jev calls.
- `covledger analyze`: enrich the one selected current target with semantic evidence (zero or one uncached request).
- `covledger analyze --all`: explicit batch semantic analysis with complete preflight and a hard explicit request budget for uncached results.
- `covledger quality --semantic`: broad source-quality analysis; do not use it for the default next-gap workflow.

## Safety and scope

Respect `.ledger/covledger/config.toml` include/exclude policy and explicit decisions. Do not bypass scope exclusions to increase coverage.

Do not add tests merely to execute lines without asserting behavior. Use the coverage gap as evidence of missing exercised behavior, then choose a focused test or small implementation change with a clear behavioral purpose.

Do not treat an absent semantic judgment as a negative judgment. Deterministic coverage and structural findings remain separate from optional semantic evidence.

Do not persist generated CovLedger reports or source snapshots unless the user explicitly requests an export.
