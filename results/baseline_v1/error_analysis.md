# Baseline v1 Error Analysis

## Status and provenance

This is a curated working copy of a technically complete, uncurated baseline
candidate. The model predictions and raw responses were copied byte-for-byte
from `results/20260829T174721.614620Z`. Only deterministic scoring was rerun.
`official_benchmark_result` remains `false`, and no model or provider API call
occurred during rescoring.

## Model failures

### CR-004: wrong classification and missed clarification

The frozen expected classification is `AMBIGUOUS`, but the model predicted
`POTENTIAL_SCOPE_CHANGE`. The frozen record also requires clarification, while
the model returned `requires_clarification=false`. These are model-output
failures and remain strict failures under the corrected scorer.

### CR-007: false-positive cumulative drift

The request's primary classification was correct, but the model reported
cumulative drift where the frozen ground truth expects none. This is a model
false positive and remains a strict failure under the corrected scorer.

## Scorer defect, not model failures

### CR-008 and CR-009: contradiction evidence placement

Both requests were correctly classified
`CONTRADICTS_APPROVED_DECISION`. CR-008 cited expected decision `DEC-003`, and
CR-009 cited expected decision `DEC-002`, in `conflicting_evidence_ids`. Those
citations existed and were available at the corresponding request cutoffs. The
pre-audit scorer nevertheless failed both cases because it checked only
`supporting_evidence_ids` for an expected-evidence hit.

The corrected generic rule checks expected conflicting evidence for
contradiction cases and accepted supporting evidence for other classifications.
It does not require the same decision ID to be duplicated across both prediction
fields. Citation existence and temporal validity remain independently enforced.

## CR-002 extra citation

CR-002 cited `SOW-SCP-007` in addition to accepted evidence `SOW-SCP-012` and
`DEC-004`. `SOW-SCP-007` exists, was temporally available, and is plausibly
relevant to the requested transactional confirmation email, but it is outside
CR-002's frozen `valid_supporting_evidence_ids` set. The accepted citations are
sufficient for the classification-appropriate evidence hit.

The deterministic scorer treats the extra citation as structurally valid. It
does not automatically claim that every structurally valid citation supports
every natural-language statement, nor does it assert an unsupported semantic
claim from this citation alone. That assessment remains for human review.

## Benchmark limitations

- The benchmark contains only ten requests from one wholly synthetic project.
- It exercises a fixed five-label taxonomy and a deliberately limited set of
  scope, clarification, contradiction, and cumulative-drift patterns.
- This is one run from one provider/model configuration; temperature zero does
  not establish universal determinism or provide confidence intervals.
- Citation checks establish ID existence, temporal availability, and frozen
  expected-evidence placement, not complete automated semantic entailment.
- The result remains non-official until human review and explicit curation.

## No advanced-agent comparison claim

The advanced agent has not been implemented or evaluated. These baseline
results therefore support no claim about advanced-agent improvement, relative
performance, or production readiness.
