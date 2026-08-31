# Learning Log

- An activated virtual environment may still lack pip.
- `python -m ensurepip --upgrade` repaired pip locally without recreating the environment.
- Git does not track empty directories.
- Benchmark ground truth and the classification taxonomy must be frozen before any model run.
- The baseline and advanced system must use the same evidence and label taxonomy for a fair comparison.
- Provider-neutral configuration avoids prematurely coupling the project to one API.
- Python 3.13 is retained provisionally; later retrieval dependencies must be compatibility-tested.
- Repository structure should grow by implemented milestone rather than speculative empty architecture.
- Privacy and reproducibility rules should be established before dataset creation.
- Explicit exclusion and potential scope change require operationally distinct definitions.
- Raw client requests do not update approved scope; only recorded human decisions do.
- Evidence cutoffs prevent future-information leakage.
- "Not approved" is different from "explicitly rejected."
- Classification ambiguity is different from missing implementation acceptance details.
- Cumulative drift should be evaluated separately from the request's primary classification.
- A citation can reference a real project ID and still be temporally invalid if that evidence was unavailable at the request cutoff.
- Cumulative-drift false positives must be penalized, not only missed true drift.
- Constructed perfect test fixtures are verification tools, not measured model results.
- Structural citation validity does not by itself prove semantic support.
- A fair temporal baseline must reconstruct only evidence available at each
  request cutoff.
- Independent calls prevent hidden conversational memory from helping the
  baseline.
- Prompt and assembled-input hashes allow later runs to be traced to exact
  inputs.
- Offline fixture tests are not real model performance.
- Excessive sanitization can preserve privacy while destroying diagnostic
  usefulness.
- Secure provider errors must retain safe status and category context while
  redacting credentials.
- Process completion and successful execution are different; exit codes must
  reflect failures.
- Partial or failed runs must never be presented as official benchmark results.
- Retry decisions should depend on categorized failure type.
- Provider structured-output schemas may support a smaller subset than local
  Pydantic JSON Schema.
- Provider-side schema compatibility and strict local validation should remain
  separate layers.
- Evidence supporting a contradiction classification may logically belong in
  `conflicting_evidence_ids`.
- Evaluation schemas must distinguish evidence validity from
  classification-appropriate evidence placement.
- Scorer defects must be separated from model failures before comparison claims
  are made.
- Partial supersession must deactivate only the affected capability facet; the
  unaffected behavior in the older evidence remains current.
- Retrieval quality must include temporal leakage and category coverage, not
  only Recall@k.
- Raw requests and model assessments are not approved project memory.
- Classification-critical recall can remain complete even when secondary
  evidence recall is imperfect.
- The benchmark parser is deliberately convention-based rather than a universal
  document parser.
- Ambiguity must gate classification only when the requested capability cannot
  be identified; unresolved acceptance details alone must not force AMBIGUOUS.
- Cumulative drift is a property of approved-memory evolution, so raw requests
  and unapproved assessments must never count as scope-expansion increments.
- Verification failure should preserve the evidence and pause for human review;
  it must not be converted into a guessed or silently repaired final answer.
- A serialized run is safe to resume only when both its scope-anchor hash and
  paused ledger snapshot still match the current project state.
- Human decisions must store only explicitly approved capability clauses, not
  entire mixed decision sections containing neutral or unapproved facets.
- The current proposal must be included when verifying its own cumulative
  relationship to an emerging subsystem.
- Evaluator access to frozen human-outcome fixtures must occur only after the
  agent pauses for human review.
- A complete fake replay validates orchestration, isolation, and artifact
  handling; it is not measured model performance.
- A retry that ultimately succeeds must still preserve the failed attempt's
  timestamped, secret-safe diagnostic; call counts alone cannot recover why the
  earlier attempt failed.
- The combined advanced system fixed both observed baseline failures on the
  frozen benchmark: CR-004 ambiguity and CR-007 drift over-detection.
- Reliability gains came with materially higher token use and runtime.
- A perfect result on a small single-project benchmark is not evidence of
  general perfection.
- Improvement attribution belongs to the combined retrieval, deterministic
  gates, approved memory, verification, and human-state pipeline, not RAG alone.
- A product demo can remain honest and available during provider outages by
  replaying hash-verified recorded events, provided the UI clearly distinguishes
  replay from fresh execution and never writes replay data into live run state.
- A recorded human-review outcome must be shown exactly as preserved; replaying
  a paused trajectory does not authorize a new decision or ledger mutation.
