# Project Progress

- **Date:** 2026-08-30
- **Milestone 5/6:** Failure-Informed Advanced Agent
- **Status:** Advanced V1 curated; failure-informed comparison complete

## Completed

- Milestone 1 project foundation retained
- Local Git repository initialized
- Private GitHub remote connected
- Python 3.13 virtual environment created
- Missing pip repaired with `python -m ensurepip --upgrade`
- Provider-neutral project-control skeleton created
- Five-label taxonomy frozen
- Privacy, formatting, TOML, package, ignore, and staging validations passed
- One wholly fictional StudioLane benchmark authored and manually audited
- Predetermined ground truth frozen before any model run
- Evidence IDs, decision chronology, evidence cutoffs, human outcomes,
  supersession, and cumulative-drift expectations validated
- Strict Pydantic schemas added for requests, ground truth, predictions, and
  evaluation results
- Generic dataset validation and frozen-demo assertions implemented
- Per-request temporal evidence availability enforced for citation scoring
- Deterministic five-label classification, clarification, contradiction,
  citation, and cumulative-drift metrics implemented without an API
- Strict Evidence-Grounded Scope Accuracy defined and tested
- Constructed-fixture dataset and scoring tests passing on Python 3.13
- Provider-aware Google GenAI configuration and structured-output adapter added
- Fixed direct-prompt instructions and SHA-256 prompt hashing added
- Per-request temporal prompt reconstruction implemented without future evidence
- Independent, stateless request calls and bounded structured-output retries implemented
- Raw responses, parsing failures, run metadata, and per-request assembled-input
  hashes can be preserved during a future approved run
- Configuration validation completed without displaying the API key
- Offline dry-run validation completed for CR-001, CR-007, and CR-010
- The first real CR-001 smoke test failed with a `ClientError`; the original
  provider cause is unrecoverable because the stored message was excessively
  sanitized and retained no safe status or category context
- No parsed prediction or official benchmark result was produced by that failed
  smoke test
- Secret-safe structured provider diagnostics now retain exception type, safe
  status, normalized category, sanitized message, request ID, attempt number,
  and retryability
- CLI exit codes now report failure when any explicitly requested case fails
- Incomplete and uncurated score artifacts are explicitly marked non-official
- The second CR-001 smoke test produced a diagnosable HTTP 400
  `INVALID_ARGUMENT`: Gemini rejected the SDK-converted Pydantic schema field
  `additional_properties`; no prediction or official result was produced
- Gemini structured output now uses an explicit provider-compatible JSON schema
  with unsupported metadata removed recursively, while returned JSON still
  undergoes the original strict `ModelPrediction` Pydantic validation
- A later CR-001 smoke test established that `gemini-2.5-flash` was unavailable
  to this new-user account; the provider directed the account to
  `gemini-3.6-flash`
- A controlled CR-001 smoke test then parsed successfully with
  `gemini-3.6-flash`
- The ten-case direct-prompt baseline candidate completed successfully with ten
  independent calls using `gemini-3.6-flash`; it remains uncurated and
  explicitly non-official
- A scorer-semantics defect was identified before curation: contradiction cases
  were incorrectly required to duplicate expected conflict evidence into
  `supporting_evidence_ids`
- Classification-appropriate evidence placement is now implemented and tested;
  contradiction cases use expected `conflicting_evidence_ids`, while other
  classes use accepted supporting evidence
- The candidate's raw predictions and original pre-audit scores remain
  byte-for-byte unchanged
- Corrected scores were independently reproduced from the copied predictions
  using scoring commit `6d80f84af9bf003b06f54c64d6cc6a0c78e45611`
- Baseline V1 was curated with 10 successful requests, 0 technical failures,
  and two strict failures (CR-004 and CR-007)
- The curated result preserves the original generation manifest, raw responses,
  predictions, pre-audit scores, corrected scores, rescore provenance, and error
  analysis
- Advanced Agent Checkpoint 1 completed with strict Pydantic contracts for
  source evidence, retrieval, temporal status, human review, and ledger snapshots
- Deterministic convention-based parsing produces a 39-item StudioLane scope
  anchor with stable source hashes and a deterministic anchor hash
- Scope, constraints, exclusions, assumptions, unresolved questions, and
  decisions remain distinct in the scope anchor
- Four facet-specific supersession edges preserve unaffected behavior; partial
  supersession does not deactivate an entire evidence item
- Deterministic Unicode-normalized lexical and metadata retrieval implements
  category-balanced quotas, effective-decision priority, cutoff filtering,
  deterministic tie-breaking, score explanations, and one bounded expansion
- Frozen-project retrieval evaluation at k=12 measured evidence Recall@k of
  0.9091, contradiction-decision recall of 1.00, unresolved-question recall of
  1.00, classification-appropriate evidence recall of 1.00, mean category
  coverage of 0.95, and zero temporal leakage
- Built-in SQLite persistence now separates original approved anchor evidence
  from human-reviewed ledger entries, enables foreign keys and file-backed WAL,
  and exposes deterministic immutable snapshots
- Raw requests and assessments are recorded without changing approved memory;
  new ledger entries require an explicit human review transaction
- The final Checkpoint 1 audit moved all ground-truth-dependent retrieval metrics
  out of production modules and into the offline test/evaluation boundary
- Advanced Agent Checkpoint 2 completed with strict contracts for ambiguity,
  conflicts, capability signatures, drift, verification, human recommendations,
  resumable state, trajectories, and change-impact packages
- Deterministic sufficiency analysis distinguishes classification-blocking
  undefined roles and vague unmeasured targets from non-blocking acceptance detail
- Effective-conflict analysis distinguishes current approvals, specific
  rejections, neutral boundaries, exclusions, and facet-specific partial
  supersession without request-specific rules
- The advanced classifier enforces the frozen precedence: contradiction,
  ambiguity, in scope, out of scope, then potential scope change
- Cumulative drift uses only human-approved scope-changing ledger entries;
  raw requests and unapproved assessments do not count toward thresholds
- Configurable drift severity now distinguishes NONE, RELATED, EMERGING, and
  SUBSYSTEM, with the benchmark drift boolean true only for SUBSYSTEM
- A fixed advanced prompt and caller-supplied strict output schema preserve the
  provider-compatible sanitation boundary without changing baseline behavior
- Deterministic verification checks citation existence, cutoff availability,
  retrieval membership, semantic placement, precedence, ambiguity consistency,
  approval claims, and drift eligibility
- One bounded repair is permitted for repairable verification issues; unresolved
  or non-repairable failures pause safely for human review instead of producing
  an invented result
- The explicit state machine records concise append-only trajectory events and
  always pauses at AWAIT_HUMAN_REVIEW before any approved-memory mutation
- Resume validates both the scope-anchor hash and paused ledger snapshot before
  applying an explicit APPROVE, OVERRIDE, NEEDS_CLARIFICATION, or DEFER review
- Approved scope-changing reviews produce evidence-linked change-impact packages
  with DRAFT acceptance criteria; other reviews produce non-approving memos
- Offline fake-client demonstrations cover ambiguity, contradiction, all four
  human actions, failed verification, serialization/resume, and approved-history
  subsystem drift without network access or advanced result artifacts
- Final Checkpoint 2 verification passed 131 tests on Python 3.13; dataset,
  scope-anchor, retrieval, baseline-hash, diff, secret, and privacy checks passed
- Advanced Evaluation Runner checkpoint completed with a fresh temporary SQLite
  ledger, independent structured assessment calls, and approved-memory-only state
  carried sequentially across CR-001 through CR-010
- The complete offline ten-request fake-client replay produced ten verified
  predictions while preserving raw responses, trajectories, human-review events,
  ledger snapshots and hashes, prompt/tool/schema hashes, scores, errors, runtime,
  and token-usage metadata in deterministic uncurated artifacts
- Evaluator-only human-outcome fixtures are inaccessible to generation,
  retrieval, ambiguity, contradiction, drift, and verification; ground truth is
  read only after the agent reaches AWAIT_HUMAN_REVIEW for the current request
- DEC-005 after CR-006 and DEC-006 after CR-007 are applied through normal human
  review transactions, and approved ledger state persists into later requests
- Final Checkpoint 3 verification passed 144 tests on Python 3.13, including
  explicit ground-truth isolation, complete replay, ordering, fresh-database,
  incomplete-run, no-network, and frozen-baseline regressions
- The real Advanced V1 evaluation completed CR-001 through CR-010 in order with
  ten successful assessments, zero technical request failures, one bounded
  provider retry, zero verification repairs, and ten explicit human reviews
- Advanced V1 is curated from the immutable source candidate with all 42 source
  artifacts copied byte-for-byte, including raw responses, predictions, scores,
  trajectories, retrieval bundles, paused states, review events, snapshots, and
  the SQLite database and sidecars
- Advanced V1 passed all ten strict cases; it corrected Baseline V1's CR-004
  ambiguity/clarification miss and CR-007 drift false positive while retaining
  complete citation validity and contradiction recall
- The failure-informed comparison records every aggregate metric and request,
  with Advanced V1 Evidence-Grounded Scope Accuracy 1.00 versus Baseline V1 0.80
- Advanced reliability required 118,053 total tokens and 216.59 seconds versus
  50,566 tokens and 101.00 seconds for the baseline; no cost is claimed without
  explicit pricing input
- The recovered CR-007 retry exposed an unrecoverable missing first-attempt
  diagnostic in the generation artifact; future runs now preserve append-only,
  timestamped, secret-safe attempt records without altering Advanced V1
- Final retry-repair verification passed 146 tests with dataset, scope-anchor,
  retrieval, ground-truth isolation, baseline-hash, diff, secret, and privacy
  audits passing

## Milestone 2 benchmark files

- `data/synthetic/demo_project/sow.md`
- `data/synthetic/demo_project/decisions.md`
- `data/synthetic/demo_project/requests.json`
- `data/synthetic/demo_project/ground_truth.json`

Several controlled CR-001 smoke tests were used to repair diagnostics and
provider-schema compatibility. The final smoke test and subsequent ten-case
candidate completed with `gemini-3.6-flash`. All smoke and candidate results
remain preserved; the reviewed Baseline V1 result is now curated and official.

## Milestone 3 files

- `spectrace/models.py`
- `spectrace/dataset.py`
- `spectrace/scoring.py`
- `tests/test_dataset.py`
- `tests/test_scoring.py`

## Deliberately not implemented

- Streamlit interface
- Workflow export beyond the reviewed change-impact package
- Any retrieval, persistent ledger, verification pass, or agent tooling in the baseline

## Next step

Begin the Streamlit Product Experience and Workflow Export phase. Preserve the
curated Baseline V1, Advanced V1, and comparison artifacts unchanged while the
interface and export workflow are developed.
