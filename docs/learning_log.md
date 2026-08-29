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
