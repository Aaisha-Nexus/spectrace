# SpecTrace

SpecTrace is a requirements and scope intelligence agent for Business Analysts,
Project Managers, freelancers, and small software agencies. It is designed to
compare incoming requests with approved scope and decision history, surface
contradictions and cumulative scope drift, and keep a human reviewer in control.

> Project status: the wholly synthetic StudioLane benchmark and its predetermined,
> frozen ground truth now exist. Dataset validation, strict schemas, an API-free
> deterministic scorer, and the direct-prompt baseline runner are implemented.
> Baseline V1 is curated from a completed ten-request Gemini run. The first
> advanced system now provides deterministic scope-anchor parsing, cutoff-safe
> lexical retrieval, approved-memory SQLite persistence, ambiguity and drift
> analysis, deterministic verification, and a resumable human-gated state
> machine. Baseline V1 and Advanced V1 are curated on the frozen StudioLane
> benchmark, with a failure-informed comparison preserved. The Streamlit product
> experience and workflow export are next and are not yet implemented.

## Development principles

- Benchmark before building the advanced agent.
- Use only synthetic, non-identifying project data.
- Freeze ground truth before any model run.
- Compare baseline and advanced systems on the same complete case evidence and
  the same five-label taxonomy.
- Require evidence for claims and preserve failures honestly.
- Keep the model provider configurable.

## Classification taxonomy

- `IN_SCOPE`
- `AMBIGUOUS`
- `OUT_OF_SCOPE`
- `CONTRADICTS_APPROVED_DECISION`
- `POTENTIAL_SCOPE_CHANGE`

## Milestones

1. Synthetic project pack and predetermined ground truth
2. Direct-prompt LLM baseline
3. Dataset validation, schemas, and deterministic scoring (implemented)
4. Evidence-grounded advanced agent
5. Streamlit interface
6. Tests, documentation, and preserved results

## Local environment

The project currently targets Python 3.13. In PowerShell, activate the existing
virtual environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the package and development dependencies into the environment:

```powershell
python -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and set the following local values before a model
run. `.env` is ignored and must never be committed.

- `LLM_PROVIDER=google` (required for the current implementation)
- `LLM_MODEL` (required)
- `LLM_API_KEY` (required and never displayed by validation)
- `LLM_BASE_URL` (optional; normally empty)

Validate the frozen synthetic pack and run the tests:

```powershell
python -m spectrace.dataset data/synthetic/demo_project
pytest
```

Validation failures return a nonzero process status and identify the violated
schema or benchmark invariant. Scoring is available through
`spectrace.scoring.score_predictions`; it performs no API calls.

## Deterministic evidence and memory foundation

Build and inspect the scope anchor without making a model call:

```powershell
python -m spectrace.scope_anchor data/synthetic/demo_project --cutoff DEC-006
```

Run one production retrieval without ground-truth access:

```powershell
python -m spectrace.retrieval data/synthetic/demo_project --request-id CR-004
```

The parser currently targets the documented StudioLane Markdown convention; it
is not a universal document parser. Retrieval uses dependency-free lexical and
metadata scoring, and production modules never read `ground_truth.json`.
Frozen-project retrieval metrics are calculated only by the offline test and
evaluation boundary. Raw requests and assessments cannot update approved ledger
memory; only an explicit human `APPROVE` or valid `OVERRIDE` payload can do so.

Run the evaluator-only frozen-project summary with development dependencies:

```powershell
python tests/test_retrieval.py
```

## Offline advanced analysis and state machine

The advanced prompt is fixed in `prompts/advanced.md`. Its structured provider
boundary accepts a caller-supplied strict Pydantic model, while the existing
baseline wrapper and prompt remain unchanged. Deterministic tools separately
assess blocking ambiguity, effective decision conflicts, exact classification
precedence, and cumulative drift from human-approved scope-changing ledger
entries only.

The explicit state machine retrieves cutoff-safe evidence, runs an injectable
structured client, verifies the result, and always pauses at
`AWAIT_HUMAN_REVIEW`. Raw requests, assessments, and fake model outputs do not
change approved memory. Resume validates the anchor and ledger snapshot before
applying one explicit human review transaction. Only an approved scope-changing
payload produces a full change-impact package; `NEEDS_CLARIFICATION`, `DEFER`,
and decisions that uphold an exclusion or contradiction produce review memos.

Run the offline fake-client and deterministic-tool harnesses without any API:

```powershell
pytest tests/test_analysis_tools.py tests/test_verification.py
pytest tests/test_advanced.py tests/test_change_package.py
```

These tests demonstrate behavior only; they are not advanced benchmark results.

## Direct-prompt baseline

The fixed instructions are stored in `prompts/baseline.md`. Each request is sent
in a fresh call with the complete SOW, only decisions available at its explicit
evidence cutoff, and only request messages through the current request. Ground
truth and future evidence are never included. Temperature is fixed at zero; an
optional seed is recorded as a best-effort reproducibility setting, not a claim
of complete determinism.

Validate local configuration without revealing the key:

```powershell
python -m spectrace.baseline validate-config
```

Render one offline prompt, or report its inclusion assertions and hashes:

```powershell
python -m spectrace.baseline dry-run --request-id CR-001
python -m spectrace.baseline dry-run --request-id CR-007 --summary
```

The following commands can contact Gemini and therefore require the explicit
confirmation flag. Do not run them until the benchmark dry run has been reviewed:

```powershell
python -m spectrace.baseline run --request-id CR-001 --confirm-api-call
python -m spectrace.baseline run-all --confirm-api-call
```

An actual confirmed run writes `results/<run-id>/manifest.json`, raw JSONL,
validated predictions, deterministic scores, and an error JSONL when failures
occur. Temporary runs are ignored by Git; a reviewed curated run can later be
force-added deliberately. No result directory is created by configuration
validation or dry-run commands.

## Baseline V1

[Baseline V1](results/baseline_v1/) is the curated direct-prompt result for the
ten-case StudioLane benchmark using provider `google` and model
`gemini-3.6-flash`. Generation used commit
`550997316c59f91d3ef11e1e6b429b17111dd16d`; corrected deterministic scoring used
commit `6d80f84af9bf003b06f54c64d6cc6a0c78e45611`.

- Classification accuracy: **0.90**
- Macro F1: **0.9048**
- Classification-appropriate evidence hit rate: **1.00**
- Clarification recall: **0.50**
- Cumulative-drift accuracy: **0.90**
- Evidence-Grounded Scope Accuracy: **0.80**

CR-004 and CR-007 are the two strict failures. The preserved
`error_analysis.md` separates those model failures from scorer and engineering
defects.

## Baseline V1 versus Advanced V1

[Advanced V1](results/advanced_v1/) is the curated sequential advanced-agent
candidate. The complete [comparison](results/comparison_v1/) uses the same
frozen ten-request StudioLane benchmark and unchanged scorer as
[Baseline V1](results/baseline_v1/).

| Metric | Baseline V1 | Advanced V1 |
|---|---:|---:|
| Strict passes | 8/10 | 10/10 |
| Classification accuracy | 0.90 | 1.00 |
| Macro precision | 0.95 | 1.00 |
| Macro recall | 0.90 | 1.00 |
| Macro F1 | 0.9048 | 1.00 |
| Citation validity | 1.00 | 1.00 |
| Classification-appropriate evidence hit | 1.00 | 1.00 |
| Clarification accuracy | 0.90 | 1.00 |
| Clarification recall | 0.50 | 1.00 |
| Contradiction recall | 1.00 | 1.00 |
| Cumulative-drift accuracy | 0.90 | 1.00 |
| Related request/decision ID accuracy | 1.00 / 1.00 | 1.00 / 1.00 |
| Evidence-Grounded Scope Accuracy | 0.80 | 1.00 |

Advanced V1 corrected the two observed baseline failures: CR-004's missed
ambiguity and clarification need, and CR-007's cumulative-drift false positive.
Citation validity, classification-appropriate evidence placement, and
contradiction recall remained complete in both runs.

### Advanced architecture

For each request, the advanced pipeline builds the frozen scope anchor, retrieves
only cutoff-available evidence, applies deterministic ambiguity and conflict
gates, obtains one independent structured model assessment, reconciles the fixed
taxonomy precedence, calculates drift from human-approved memory only, verifies
the assessment, and pauses for human review. Only an explicit review transaction
can update the SQLite decision ledger for later requests.

### Tradeoffs and limitations

Advanced V1 used 118,053 total tokens and 216.59 seconds, versus 50,566 tokens
and 101.00 seconds for Baseline V1: about 2.33x the tokens and 2.14x the runtime.
No cost is claimed because explicit pricing input is absent.

This is one run per system on one ten-case synthetic project. It provides no
statistical-significance or generalization evidence, and Advanced V1's perfect
score does not imply general perfection. The improvement belongs to the combined
retrieval, deterministic-gating, approved-memory, verification, and human-state
pipeline; it must not be attributed to RAG alone. CR-007's first failed provider
attempt diagnostic was not retained and is unrecoverable; the post-run repair
affects future runs only.

The next implementation phase is the Streamlit Product Experience and Workflow
Export. It has not begun.

## Deterministic metric definitions

All divisions use zero when their denominator is zero, except vacuous data-quality
rates (no cases, no citations, no evidence-expected cases, or no cumulative cases),
which use one. Every classification macro average always includes all five frozen
labels.

- **Classification accuracy:** correctly classified cases / all cases.
- **Per-label precision:** true positives / (true positives + false positives).
- **Per-label recall:** true positives / (true positives + false negatives).
- **Per-label F1:** `2 * precision * recall / (precision + recall)`.
- **Macro precision, recall, and F1:** arithmetic mean of the corresponding
  per-label values across all five labels.
- **Citation reference validity rate:** cited supporting and conflicting IDs that
  both exist and were available at that request's explicit evidence cutoff / all
  cited IDs. Per-case citation validity requires every cited ID to satisfy both
  conditions; a real but future decision ID is invalid for that request.
- **Classification-appropriate evidence hit rate:** cases citing at least one
  expected ID in the semantically appropriate field / cases where such evidence
  is expected. `CONTRADICTS_APPROVED_DECISION` cases must cite an expected
  `conflicting_evidence_id` in `conflicting_evidence_ids`; every other class must
  cite an accepted `valid_supporting_evidence_id` in `supporting_evidence_ids`.
  The same ID need not be duplicated across both prediction fields.
- **Clarification decision accuracy:** cases whose `requires_clarification`
  boolean matches ground truth / all cases.
- **Clarification precision:** correctly requested clarifications / all requested
  clarifications.
- **Clarification recall:** correctly requested clarifications / all cases that
  require clarification.
- **Contradiction detection recall:** cases predicted
  `CONTRADICTS_APPROVED_DECISION` among cases with that expected label / all
  cases with that expected label.
- **Cumulative-drift detection accuracy:** cases whose drift boolean matches the
  expected boolean / all cases.
- **Cumulative-drift detection rate:** expected cumulative cases where drift is
  detected / all expected cumulative cases.
- **Related request/decision ID accuracy:** expected cumulative cases with an
  exact set match for the corresponding related IDs / all expected cumulative
  cases. Ordering does not affect the match; extra and missing IDs do.
- **Evidence-Grounded Scope Accuracy:** strict passing cases / all
  cases. A case passes only when classification is correct, the required
  clarification decision is correct, at least one expected evidence ID is cited
  in the classification-appropriate field when evidence is expected, every
  cited evidence ID exists and was available at the request cutoff, and
  cumulative-drift detection exactly matches ground truth for every request.
  This penalizes both false negatives and false positives.

Evidence-ID validity is structural: it does not prove that every cited item
semantically supports every natural-language claim. SpecTrace does not yet claim
fully automated unsupported-claim detection. Current tests use constructed
predictions to verify formulas and failure behavior; they are not baseline or
measured model results.

See `SpecTrace_Master_Plan.md` for the complete scope and delivery plan and
`AGENTS.md` for repository rules.
