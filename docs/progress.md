# Project Progress

- **Date:** 2026-08-29
- **Milestone 4:** Direct-Prompt Baseline
- **Status:** In progress; offline implementation and dry-run validation completed

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

## Milestone 2 benchmark files

- `data/synthetic/demo_project/sow.md`
- `data/synthetic/demo_project/decisions.md`
- `data/synthetic/demo_project/requests.json`
- `data/synthetic/demo_project/ground_truth.json`

One CR-001 smoke-test API call was attempted and failed before returning a
prediction. No official benchmark run or result exists.

## Milestone 3 files

- `spectrace/models.py`
- `spectrace/dataset.py`
- `spectrace/scoring.py`
- `tests/test_dataset.py`
- `tests/test_scoring.py`

## Deliberately not implemented

- Advanced agent
- Streamlit interface
- Retrieval, persistent ledger, verification pass, or agent tooling in the baseline
- Real model predictions or measured baseline performance

## Next step

Review the offline Gemini schema-compatibility repair before authorizing a third
CR-001 smoke test. Do not consider a full ten-request run yet.
