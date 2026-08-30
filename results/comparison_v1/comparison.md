# Baseline V1 versus Advanced V1

This comparison uses the same frozen ten-request StudioLane synthetic benchmark
and unchanged deterministic scorer. Baseline V1 and Advanced V1 are curated
artifacts; the Advanced V1 source candidate was copied byte-for-byte and was not
rerun, edited, or rescored during curation.

## Provenance

| | Baseline V1 | Advanced V1 |
|---|---|---|
| Result | `results/baseline_v1` | `results/advanced_v1` |
| Provider/model | Google / `gemini-3.6-flash` | Google / `gemini-3.6-flash` |
| Generation commit | `550997316c59f91d3ef11e1e6b429b17111dd16d` | `1ed35e9ca4919985468e94d3b5b8cb3260b76210` |
| Scoring commit | `6d80f84af9bf003b06f54c64d6cc6a0c78e45611` | `1ed35e9ca4919985468e94d3b5b8cb3260b76210` |
| Prompt hash | `369b16540e18ac3592867bdcde4a9d37e156ef8ee726371e1782380edb48a687` | `a8876f80458f64e300dee1a2b22bff248831c6ffe743506de6f9995553fc6a3a` |
| Dataset hash | `4e060fa08f08be87151735ada77f9a3bf7876d6832e425173d5776ecb77a3585` | same |

The future retry-diagnostic repair is commit
`970d18c1b431df14c3c2416da00a6588f04c7391`; it did not alter or rerun
Advanced V1.

## Aggregate metrics

| Metric | Baseline | Advanced | Difference |
|---|---:|---:|---:|
| Strict passes | 8/10 | 10/10 | +2 |
| Classification accuracy | 0.90 | 1.00 | +0.10 |
| Macro precision | 0.95 | 1.00 | +0.05 |
| Macro recall | 0.90 | 1.00 | +0.10 |
| Macro F1 | 0.9048 | 1.00 | +0.0952 |
| Citation validity | 1.00 | 1.00 | 0 |
| Classification-appropriate evidence hit | 1.00 | 1.00 | 0 |
| Clarification accuracy | 0.90 | 1.00 | +0.10 |
| Clarification precision | 1.00 | 1.00 | 0 |
| Clarification recall | 0.50 | 1.00 | +0.50 |
| Contradiction recall | 1.00 | 1.00 | 0 |
| Cumulative-drift accuracy | 0.90 | 1.00 | +0.10 |
| Cumulative-drift detection rate | 1.00 | 1.00 | 0 |
| Related request-ID accuracy | 1.00 | 1.00 | 0 |
| Related decision-ID accuracy | 1.00 | 1.00 | 0 |
| Evidence-Grounded Scope Accuracy | 0.80 | 1.00 | +0.20 |

## Per-request changes

| Request | Expected | Baseline | Advanced | Outcome change |
|---|---|---|---|---|
| CR-001 | `IN_SCOPE` | `IN_SCOPE` | `IN_SCOPE` | Unchanged pass |
| CR-002 | `IN_SCOPE` | `IN_SCOPE` | `IN_SCOPE` | Unchanged pass |
| CR-003 | `AMBIGUOUS` | `AMBIGUOUS` | `AMBIGUOUS` | Unchanged pass |
| CR-004 | `AMBIGUOUS` | `POTENTIAL_SCOPE_CHANGE` | `AMBIGUOUS` | Advanced ambiguity gate also requested the expected clarification |
| CR-005 | `OUT_OF_SCOPE` | `OUT_OF_SCOPE` | `OUT_OF_SCOPE` | Unchanged pass |
| CR-006 | `POTENTIAL_SCOPE_CHANGE` | same | same | Unchanged pass |
| CR-007 | `POTENTIAL_SCOPE_CHANGE` | same label; drift false positive | same label; drift correctly false | Advanced approved-memory analysis removed the false positive |
| CR-008 | `CONTRADICTS_APPROVED_DECISION` | same | same | Unchanged contradiction pass |
| CR-009 | `CONTRADICTS_APPROVED_DECISION` | same | same | Unchanged contradiction pass |
| CR-010 | `POTENTIAL_SCOPE_CHANGE` | same | same | Unchanged cumulative-drift pass |

Advanced V1 fixed both observed Baseline V1 strict failures: CR-004's
ambiguity/clarification miss and CR-007's drift false positive. Both systems
retained perfect citation validity, classification-appropriate evidence hits,
and contradiction recall.

## Runtime and token tradeoff

| | Baseline | Advanced | Advanced / baseline |
|---|---:|---:|---:|
| Runtime | 101.00 s | 216.59 s | 2.14x |
| Prompt tokens | 38,062 | 103,177 | 2.71x |
| Candidate tokens | 1,793 | 2,782 | 1.55x |
| Thought tokens | 10,711 | 12,094 | 1.13x |
| Total tokens | 50,566 | 118,053 | 2.33x |

No cost claim is made because explicit pricing input is absent.

## Interpretation and limitations

The observed improvement belongs to the combined advanced pipeline: retrieval,
deterministic ambiguity and taxonomy gates, approved decision memory,
cumulative-drift analysis, verification, and human-state orchestration. It must
not be attributed to RAG alone.

This is one run on one frozen ten-case synthetic project. It supports neither a
generalization claim nor a statistical-significance claim, and a perfect result
does not establish general perfection. CR-007's first failed provider-attempt
diagnostic is unrecoverable in Advanced V1; the post-run repair affects only
future evaluations.
