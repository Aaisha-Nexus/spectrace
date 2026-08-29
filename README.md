# SpecTrace

SpecTrace is a requirements and scope intelligence agent for Business Analysts,
Project Managers, freelancers, and small software agencies. It is designed to
compare incoming requests with approved scope and decision history, surface
contradictions and cumulative scope drift, and keep a human reviewer in control.

> Project status: the wholly synthetic StudioLane benchmark and its predetermined,
> frozen ground truth now exist. Dataset validation, strict schemas, and an
> API-free deterministic scorer are implemented. The model runner, advanced
> agent, and user interface remain unimplemented, and no model or API has been run.

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

Validate the frozen synthetic pack and run the tests:

```powershell
python -m spectrace.dataset data/synthetic/demo_project
pytest
```

Validation failures return a nonzero process status and identify the violated
schema or benchmark invariant. Scoring is available through
`spectrace.scoring.score_predictions`; it performs no API calls.

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
- **Expected-evidence hit rate:** cases citing at least one supporting ID from
  `valid_supporting_evidence_ids` / cases where that list is nonempty.
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
- **Provisional Evidence-Grounded Scope Accuracy:** strict passing cases / all
  cases. A case passes only when classification is correct, the required
  clarification decision is correct, at least one expected supporting evidence
  ID is cited when evidence is expected, every cited evidence ID exists and was
  available at the request cutoff, and cumulative-drift detection exactly
  matches ground truth for every request. This penalizes both false negatives
  and false positives.

Evidence-ID validity is structural: it does not prove that every cited item
semantically supports every natural-language claim. SpecTrace does not yet claim
fully automated unsupported-claim detection. Current tests use constructed
predictions to verify formulas and failure behavior; they are not baseline or
measured model results.

See `SpecTrace_Master_Plan.md` for the complete scope and delivery plan and
`AGENTS.md` for repository rules.
