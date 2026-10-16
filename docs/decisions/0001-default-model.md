# 0001. Default local model: qwen3:8b

Status: accepted, 2026-10-16. Revisit when a new model is worth trying (rerun the script below).

## Context

The fit features need a local model that (a) extracts requirements from a posting, and (b) compares
them with a CV, quoting the CV. The design already assumes the model is not trusted: quotes are
checked against the source text, and the score is computed by code. So the question is which model
makes that guard cost the least, meaning it needs to correct and downgrade the least.

## Evidence

`scripts/compare_models.py` runs both steps on the synthetic fixtures in `tests/fixtures/` (a CV and
a posting with deliberate traps: the CV has JavaScript but the posting asks for Java; two requirements
are absent; one is borderline). Two runs per model, Ollama 0.34, temperature 0, 8192-token context.

| | llama3.1 | qwen3:8b |
|---|---|---|
| Requirements extracted (of 6) | 6 | 6 |
| Requirements dropped as unverifiable | 0 | 0 |
| Verdicts consistent with the ground truth | 6/6 | 6/6 |
| False positives after verification | 0 | 0 |
| Claims downgraded because the quote was not in the CV | 1 per run | 0 |
| Time: parse + fit | ~6 s + ~14 s | ~8 s + ~8 s |

The first draft of the prompts did worse and exposed two failure modes, both fixed in the prompts:

* `llama3.1` quoted the *requirement text* as if it were CV evidence. The verification caught it every
  time (this is the case the guard exists for); the prompt now says the quote must come from the CV.
* Quotes in postings merged a heading ("Nice to have:") with a bullet, so they were not verbatim and
  the requirement was dropped. The prompt now asks for one line, without headings.

## Decision

Default to `qwen3:8b` (`JOBTRAIL_OLLAMA_MODEL`), with `think=False`. It needed no downgrades and
finished the fit step about twice as fast. `llama3.1` works too.

## Limits of this evidence

One synthetic case, two runs per model. It supports "both are usable and qwen3:8b is at least as good
here", not a general ranking. The verification guarantees that cited evidence **exists** in the CV, not
that it **supports** the requirement: in the end-to-end run, "3+ years of experience" was marked partial
citing "Data analyst moving into machine learning.", which is weak. The UI shows every quote next to its
requirement for that reason, and says so.
