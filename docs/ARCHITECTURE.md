# Architecture

One Python process serves a server-rendered web app over a SQLite file. The only outbound call is to a local Ollama server. This page maps the code and the reasoning behind its shape; the *why* of individual decisions is in [`decisions/`](decisions/).

## Layers

```
 browser ──HTTP──▶ web/            FastAPI routes, Jinja2 templates, htmx (vendored)
                    │               security middleware (Host and Origin checks)
                    ▼
                  services/         business logic; the only code that writes data
                    │   tracker, analytics, matching,
                    │   posting_parser, fit_scorer, cv_advisor, message_drafter
        ┌───────────┼───────────────┐
        ▼           ▼               ▼
     domain/     database/        llm/            schemas/
     rules with  SQLAlchemy       LLMClient       Pydantic models for what
     no I/O      models, SQLite   protocol,       the model returns and
                                  Ollama client,  what we keep
                                  FakeLLM, retry
```

Rules that keep it testable: `domain/` imports nothing from the other layers; `services/` take a SQLAlchemy `Session` and an `LLMClient` as arguments (nothing global); the web layer calls services and never touches the database directly except to commit.

## Modules

| Path | Responsibility |
|---|---|
| `domain/status.py` | The status lifecycle as one transition table. Terminal states (rejected, withdrawn) have no way out. |
| `domain/followup.py` | Kinds of message (follow-up, thank-you, recruiter intro) and the statuses each one fits. |
| `domain/text_match.py` | `quote_in_text` (whole-word, normalised), `unsupported_numbers`, `unsupported_terms`: the deterministic checks every model claim goes through. |
| `domain/scoring.py` | The fit score formula. |
| `domain/outreach.py` | `thread_state`: from a contact's history, did they answer and are you still waiting? One place for the rule. |
| `database/models.py` | `Company`, `Application`, `StatusEvent`, `Followup`, `Channel`, `Track`, `CVVersion`, `Posting`, `FitAnalysis`, `Contact`, `Interaction`, `Reminder`. |
| `services/tracker.py` | Create/edit/delete applications, change status (validated, ordered, recorded as events), log messages, manage channels and tracks. |
| `services/analytics.py` | Funnel, Wilson intervals, median time to reply, stale rule, weekly activity. |
| `services/posting_parser.py`, `fit_scorer.py`, `cv_advisor.py`, `message_drafter.py` | The four LLM tasks. Each builds a prompt, asks for JSON constrained by a schema, verifies the answer in code, and retries with the reasons if verification fails. |
| `services/contacts.py` | Contacts, interactions ("They replied" is an inbound message), reminders, search links, and the emails/profile links a posting states. |
| `services/outreach_stats.py`, `contact_extractor.py` | Reply statistics per person; names a posting mentions (model output verified against the posting). |
| `services/matching.py` | Stores CVs, postings and fit analyses and glues the LLM tasks to the database. |
| `ingestion/csv_import.py`, `cv.py` | CSV import (idempotent, row-level errors) and CV text extraction (PDF, DOCX, MD, TXT). |
| `llm/` | `LLMClient` protocol; `OllamaClient`; `FakeLLM` for tests; tolerant JSON extraction; the retry harness. |
| `web/` | Pages in `routes.py` (tracker) and `matching_routes.py` (CV, postings, advice, drafts); `security.py`; templates and static files. |
| `cli.py`, `config.py`, `demo.py`, `reporting.py` | `serve`, `demo`, `import-csv`, `stats`; settings from `JOBTRAIL_*`; synthetic data; text report. |

## Data

The current status of an application is a cache of its latest `StatusEvent`; the events are the record, and the funnel is computed from them. An application's *waiting clock* runs from its latest activity, which is the later of its last status event and its last logged `Followup`.

People are kept apart from applications: a `Contact` belongs to a company, an `Interaction` may point at an application, and deleting an application keeps the people and what was said (the link is cleared). A message logged from an application to a contact is stored once as a `Followup` and once as an outbound `Interaction`, so both views agree.

Postings, fit analyses and CV versions hang off the application loosely: deleting an application keeps its postings (they are unlinked), and deleting a CV version deletes only the analyses made with it. A fit analysis stores its per-requirement verdicts and, once requested, the CV advice, as JSON.

## One LLM task, end to end (fit analysis)

1. The route loads the posting's stored requirements and the chosen CV version's text.
2. `FitScorer` truncates the CV to the model's budget (and remembers that it did), builds the prompt, and calls `LLMClient.generate(prompt, schema=...)`; Ollama constrains the output to the JSON schema.
3. The answer is parsed (`json_utils` tolerates fences and Python `None`), validated with Pydantic, then **verified**: for each *met* or *partial*, `quote_in_text(quote, cv)`. A failure becomes *missing*; the original claim is kept alongside for the UI.
4. If any claim failed verification and retries remain, the model is called again with the failing quotes in the prompt; on the last attempt the downgraded result is accepted.
5. The score is computed from the final verdicts and the analysis is stored with the model name.

If Ollama is down or the model is missing, `LLMUnavailable` becomes a readable error on the page and nothing is stored.

## Security model

jobtrail listens on localhost, but a browser can still be pointed at it by any page you visit. `web/security.py` therefore rejects requests whose `Host` is not a loopback name (DNS rebinding) and state-changing requests whose `Origin` or `Sec-Fetch-Site` shows another site (CSRF). User, CV, posting and model text is auto-escaped by Jinja2; `next=` redirects are limited to local paths; posting URLs are limited to `http(s)` at write time and again at render time.

## Testing

* No test calls a real model: they script a `FakeLLM` with the exact answers, including invented quotes, bad JSON, and a model that is down.
* Rules that carry a guarantee are tested by what they must *refuse*: an invented quote, "Java" against "JavaScript", a future-dated reply, a cross-origin POST, an off-site redirect.
* Guards were verified by disabling each in turn and confirming a test fails. That exposed one test that stayed green (it did not isolate the follow-up log), which was rewritten.
* `scripts/compare_models.py` and `scripts/screenshots.py` run against a real Ollama and are not part of the suite.

## The macOS app

`macos/` is a Swift package that builds `jobtrail.app`: a SwiftUI window around a `WKWebView` that shows the same server-rendered UI. It does not talk to the server through any special API; it starts `.venv/bin/jobtrail serve` on a free port, waits for `/health`, and loads the page. The server watches the app's process id (`JOBTRAIL_PARENT_PID`) and exits if it disappears, so a crashed app cannot leave a server behind. It also polls `GET /api/reminders` (read-only JSON of what is due) to show the Dock badge and post one notification per reminder per day. The logic that carries risk (project resolution, environment, URL policy, server lifecycle, which reminders to announce) is separated from the views and unit-tested; see [`macos/README.md`](../macos/README.md).

## Extending

* **Another model server:** implement `LLMClient` (`generate`, `check`, `model`) and pass it as `create_app(llm_factory=...)`.
* **Another channel or track:** add it on the Settings page; they are data, not code.
* **Another kind of message:** add a `FollowupKind`, the statuses it fits, and its guidance text in `message_drafter.py`.
