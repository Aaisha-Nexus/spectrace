# SpecTrace

SpecTrace is a requirements and scope intelligence agent for Business Analysts,
Project Managers, freelancers, and small software agencies. It is designed to
compare incoming requests with approved scope and decision history, surface
contradictions and cumulative scope drift, and keep a human reviewer in control.

> Project status: the wholly synthetic StudioLane benchmark and its predetermined,
> frozen ground truth now exist. The model runner, scorer, advanced agent, and
> user interface remain unimplemented, and no model or API has been run.

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

## Planned milestones

1. Synthetic project pack and predetermined ground truth
2. Direct-prompt LLM baseline
3. Evaluation and scoring
4. Evidence-grounded advanced agent
5. Streamlit interface
6. Tests, documentation, and preserved results

## Local environment

The project currently targets Python 3.13. In PowerShell, activate the existing
virtual environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

Dependencies have not been declared or installed yet.

See `SpecTrace_Master_Plan.md` for the complete scope and delivery plan and
`AGENTS.md` for repository rules.
