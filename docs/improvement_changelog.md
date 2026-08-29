# Improvement Changelog

This log records observed failures and their corrections without treating code,
configuration, or scorer repairs as model-quality improvements.

## Generic `ClientError` prevented diagnosis

- **Observed failure:** The first CR-001 smoke test preserved only
  `provider request failed: ClientError`, which could not distinguish an
  authentication, permission, quota, model, schema, or transient failure.
- **Evidence:** The original failure remains in
  `results/20260829T161418.508124Z`; its provider cause is unrecoverable from the
  locally preserved diagnostic.
- **Correction:** Provider failures now preserve secret-safe structured fields:
  provider, exception type, status, normalized category, sanitized message,
  request ID, attempt number, and retryability.
- **Model output changed:** No. The failed artifact was not edited and contained
  no parsed prediction.
- **Result:** Later provider failures were diagnosable without exposing the API
  key.
- **Retained lesson:** Privacy-preserving sanitization must retain safe status
  and category context.

## Failed runs returned exit code 0

- **Observed failure:** The first one-request smoke process exited successfully
  despite zero successful predictions and one failure.
- **Evidence:** The first smoke artifact records zero successes and one failure;
  the failure-exit behavior is covered by offline single- and multi-request CLI
  regression tests.
- **Correction:** The CLI now exits zero only when every explicitly requested
  case succeeds and marks partial or failed runs incomplete and non-official.
- **Model output changed:** No. Exit status and run-status handling changed; no
  prediction was substituted.
- **Result:** Technical failures now produce a nonzero process status.
- **Retained lesson:** Process completion is distinct from successful execution.

## Gemini rejected unsupported Pydantic schema keywords

- **Observed failure:** Gemini returned HTTP 400 `INVALID_ARGUMENT` for
  `generation_config.response_schema`, identifying unsupported
  `additional_properties` metadata.
- **Evidence:** The structured failure remains in
  `results/20260829T173335.738423Z`.
- **Correction:** The Google adapter recursively removes unsupported outbound
  schema metadata while retaining strict local `ModelPrediction` validation.
- **Model output changed:** No. The failed response artifact was preserved; only
  later requests used the compatible outbound schema.
- **Result:** Schema submission advanced past the original compatibility error.
- **Retained lesson:** Provider schema compatibility and strict local validation
  are separate layers.

## `gemini-2.5-flash` was unavailable to new users

- **Observed failure:** Google reported that configured model
  `gemini-2.5-flash` was unavailable to new users and directed this account to
  `gemini-3.6-flash`.
- **Evidence:** The categorized CR-001 failure remains in
  `results/20260829T174148.419326Z`.
- **Correction:** The user changed only the private ignored `.env` model setting
  to `gemini-3.6-flash`; code, prompt, schema, benchmark, and generation settings
  were unchanged.
- **Model output changed:** No existing output was edited. A subsequent request
  necessarily used the newly configured model.
- **Result:** The next controlled CR-001 smoke test completed and parsed.
- **Retained lesson:** Provider model availability is account-specific and must
  be recorded rather than bypassed with an automatic fallback.

## Baseline completed using `gemini-3.6-flash`

- **Observed failure:** Not applicable; this entry records the first completed
  ten-case candidate after the preceding operational repairs.
- **Evidence:** `results/20260829T174721.614620Z` contains ten predictions, raw
  responses, a manifest, and pre-audit scores, with zero technical failures.
- **Correction:** No correction was made during the run; it used committed code,
  the frozen prompt and benchmark, and independent calls.
- **Model output changed:** New outputs were generated; the earlier CR-001 smoke
  output was not reused and no prediction was manually repaired.
- **Result:** A complete but uncurated candidate was preserved with
  `official_benchmark_result=false`.
- **Retained lesson:** A technically complete run must remain distinct from a
  curated or official benchmark result.

## Contradiction evidence-placement scoring defect

- **Observed failure:** CR-008 and CR-009 were correctly classified as
  `CONTRADICTS_APPROVED_DECISION` and cited their expected decisions in
  `conflicting_evidence_ids`, but strict scoring required those IDs to be
  duplicated in `supporting_evidence_ids`.
- **Evidence:** The unchanged predictions and `scores.json` in
  `results/20260829T174721.614620Z` preserve the discrepancy.
- **Correction:** The generic scorer now checks expected conflicting evidence
  for contradiction cases and accepted supporting evidence for every other
  classification. Citation existence and cutoff validity remain separate.
- **Model output changed:** No. Predictions, raw responses, and original scores
  remain unchanged.
- **Result:** Offline tests confirm classification-appropriate evidence can pass
  without cross-field duplication; corrected candidate rescoring remains
  pending.
- **Retained lesson:** Scorer defects must be resolved separately from model
  failures before comparison claims are made.
