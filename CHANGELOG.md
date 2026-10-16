# Changelog

## 0.2.0 - 2026-10-16

### Added
- **A native macOS app** (`macos/`): double-click, Dock icon, menus, and the server starts and stops with it. A thin shell around the same web UI, built from source with `macos/Scripts/build-app.sh`. The server exits by itself if the app is force-quit.
- **A light CRM.** Contacts with a history of messages, calls, meetings and notes; a person you wrote to who has not answered is *waiting*, and **They replied** records the answer. Messages logged from an application also appear on the person.
- **Reply statistics per person** (Analytics ▸ Reaching people): how many of the people you wrote to answered, how fast, by channel and by how you found them, with intervals and the small-sample warning.
- **Reminders** on applications and contacts, on the Follow-ups page, and in the Mac app as a Dock badge and one notification per reminder per day.
- **Finding people without scraping:** search links you open yourself, and the emails, LinkedIn profile links and names that a posting states (names from the local model, each with a quote that must be in the posting).
- Demo data now includes people, replies and reminders.
- `docs/decisions/0003-outreach-crm-scope.md`: what the CRM does, and what is deliberately not built (scraping, sending messages, reading a mailbox, guessing emails).

### Fixed
- Recording a follow-up to a contact at a different company no longer changes the application's status before being refused.

### Upgrading
Starting this version on an existing database adds the new tables and leaves your data untouched. Make a copy of `data/jobtrail.db` first if you want to be safe. If you use the Mac app, rebuild it.

## 0.1.0 - 2026-10-16

First public release: application tracking with a full status history, funnel analytics that report their own sample size, CSV import, and a local-LLM assistant (posting analysis, CV fit, CV advice and message drafts) whose claims are checked against your CV by code.
