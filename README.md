# jobtrail

A local-first job search tracker: log applications with a reliable status history, see which channel, track and CV version actually gets responses, and (optionally) get help from a local LLM. No accounts, no cloud, no paid APIs.

> **Status: early development (milestone M4 done).** Tracker, CSV import, demo data, funnel analytics and the web UI work end to end. LLM features (fit scoring, CV advice, message drafts) are next. See [`docs/DESIGN.md`](docs/DESIGN.md) for scope and milestones.

## Why

Applying in volume (e.g. LinkedIn Easy Apply) leaves you with a spreadsheet of "Applied" rows and no signal on what works. jobtrail is built for a targeted search: fewer applications, each tracked, with funnel analytics that say plainly when the sample is too small to conclude anything.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/AlvaroVerona/jobtrail.git
cd jobtrail
uv sync
uv run jobtrail serve        # http://127.0.0.1:8000
```

### The web app

`uv run jobtrail serve` opens a local app at http://127.0.0.1:8000:

- **Board**: one column per status, with a quick "move to" on every card; filter by track.
- **Applications**: searchable, filterable table. Click through for the full history, to edit details, or to change status with a date and a note.
- **Follow-ups**: applications waiting longer than `JOBTRAIL_STALE_AFTER_DAYS`, with one-click next steps.
- **Analytics**: the funnel tables described below.
- **Settings**: manage your own channels and tracks.

It works as plain HTML forms and is enhanced with [htmx](https://htmx.org) (vendored in `web/static/`, so nothing loads from a CDN). It only accepts requests addressed to localhost and refuses state changes that come from another site, so a web page you visit cannot silently edit your data.

### Try it with demo data

```bash
uv run jobtrail demo                                   # synthetic data, in data/demo.db
JOBTRAIL_DB_FILE=demo.db uv run jobtrail serve
```

The demo database is separate from your real one. The demo companies are fictional and the outcomes are made up (referrals are generated to convert better than Easy Apply) so the analytics have something to show; they say nothing about real hiring.

### See what is working

```bash
uv run jobtrail stats --demo     # or without --demo for your own data
```

Prints the funnel overall and by channel and by track, the median days to a reply, applications that have been waiting too long, and applications sent per week.

How to read it: stages count what an application *ever* reached (a rejection after a screening still counts as advanced), and the denominator is applications actually sent (wishlist rows are not). Every rate shows a 95% interval, and a group with fewer than 20 applications (`JOBTRAIL_MIN_SAMPLE`) is flagged as too small to compare. A reply whose date is unknown, as with imports lacking a response date, counts as a reply but is left out of the timing instead of being reported as 0 days.

### Import your own applications

```bash
uv run jobtrail import-csv applications.csv --dry-run  # preview: writes nothing
uv run jobtrail import-csv applications.csv
```

Needs a `Company` column; optional: `Role`, `Status`, `Applied date`, `Canal`/`Channel`, `Track`, `Expected salary`, `Asked salary`, `Fecha respuesta`/`Response date`, `Notes`. Headers are matched case-insensitively in English or Spanish, so a Notion database exported as CSV works as is. Dates may be `2026-05-19`, `19/05/2026` (day first) or `May 19, 2026`. Importing the same file twice is safe: rows already present (same company, role and applied date) are skipped, and bad rows are reported without stopping the import.

Optional LLM features (planned, M5) use [Ollama](https://ollama.com) on localhost; everything else works without it.

## Development

```bash
uv sync
uv run ruff check .
uv run pytest
```

Tests are hermetic: LLM calls go through fakes, never a real model.

## Privacy

Your data stays on your machine. `data/`, `.env` and any `*.db` are git-ignored, and demo data is synthetic.

## License

MIT
