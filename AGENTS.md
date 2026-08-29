# SpecTrace Repository Rules

## Product scope

SpecTrace is an evidence-grounded requirements and scope intelligence agent for
Business Analysts and Project Managers. The human reviewer remains responsible
for consequential scope decisions.

## Current implementation order

1. Build and manually validate one fully synthetic project pack and its
   predetermined ground truth.
2. Implement the direct-prompt baseline and deterministic scoring.
3. Preserve baseline outputs before building the advanced system.
4. Implement deterministic keyword/metadata retrieval, stateful decision
   memory, verification, and cumulative-drift analysis incrementally.
5. Add the Streamlit interface only after the core workflow is testable.

Do not add empty architecture, speculative abstractions, or dependencies for a
future milestone before they are needed.

## Evaluation contract

Both baseline and advanced systems must use the same complete case evidence and
this frozen taxonomy:

- `IN_SCOPE`
- `AMBIGUOUS`
- `OUT_OF_SCOPE`
- `CONTRADICTS_APPROVED_DECISION`
- `POTENTIAL_SCOPE_CHANGE`

The baseline receives the complete SOW, decision history, and ordered request
sequence. It must not use retrieval tools, a persistent ledger, a verification
pass, or dedicated cumulative-drift analysis.

Ground truth must be fixed before model runs. Never invent, omit, improve, or
retrofit benchmark labels, evidence, results, metrics, citations, costs, or
completed work. Preserve failures and disclose differences between systems.

## Evidence and data rules

- Use stable evidence IDs in source documents, requests, decisions, outputs,
  and ground truth.
- Every evidence-grounded claim must reference evidence that exists and
  supports it.
- Use only synthetic project data. Never include internship data, client data,
  identifying information, credentials, recordings, or private artifacts.
- Keep unresolved assumptions explicit; do not turn them into requirements or
  workflow nodes.
- Do not update approved decision memory without a human approval action.

## Engineering rules

- Target Python 3.13. Move to Python 3.11 or 3.12 only after documenting a
  demonstrated compatibility problem.
- Keep model providers configurable. Do not hard-code a provider, SDK, model,
  API endpoint, or credential name into the core domain logic.
- Start advanced retrieval with deterministic keyword and metadata matching.
  Retain embeddings only if evaluation demonstrates useful improvement.
- Use `pyproject.toml` as the main project and dependency configuration.
- Add dependencies only when the current milestone requires them.
- Keep secrets in local environment variables; never commit them.
- Add tests with implementation and run relevant tests before reporting work
  complete.
- Do not claim a command, test, evaluation, or feature succeeded unless it was
  actually run and verified.

## Results policy

Temporary and exploratory runs remain ignored. Curated benchmark inputs,
outputs, manifests, summaries, and failures must eventually be committed with
enough metadata to reproduce them. Do not create a results directory until an
actual run exists.
