# 0003. Contacts and reminders: what jobtrail does and does not automate

Status: accepted, 2026-10-16 (phase 1 built; phase 2 not built).

## Context

The question was whether jobtrail could (1) find the HR manager or recruiter for an application,
(2) write and send the message, and (3) work as a CRM that shows whether they answered.

The honest answer is "some of it". Part of the automation is safe and useful; part of it breaks other
people's terms of service, damages the sender's reputation, or reaches into data the user has not
chosen to share. The job search that motivated this project also argues against volume: hundreds of
untargeted applications produced no interviews, while the one that went through a person did.

## Decision

**Built (phase 1):**

* **Contacts and a history with each one**: who, where they work, how you found them, every message
  sent or received, calls and meetings. A person you wrote to and who has not answered is *waiting*;
  a button, *They replied*, records the answer.
* **Reply statistics per person** (not per message), with the same sample-size warnings as the rest of
  the analytics: of the people you wrote to, how many answered, how fast, through which channel and
  found in which way.
* **Reminders** ("look at this again on that day"), listed on the Follow-ups page, and on the Mac app's
  Dock badge and notifications.
* **Finding people without scraping:** search links for the company that you open yourself, and the
  names, emails and LinkedIn profile links that the posting itself states (emails and links by plain
  pattern matching; names by the local model, each with a quote that must be in the posting).

**Deliberately not built, and why:**

| Not built | Reason |
|---|---|
| Looking people up automatically (scraping LinkedIn or other sites, "people search" bots) | Against those sites' terms; the usual outcome is a restricted account. jobtrail also never fetches a page: you paste what you want it to read. |
| Sending messages (email or LinkedIn) on your behalf, or sending in bulk | Bulk and bot messages harm your reputation and deliverability, and a message in your name should be one you read and sent. jobtrail drafts and logs; you send. |
| Reading your mailbox or LinkedIn inbox to detect replies | It needs access to a very private store of data, and LinkedIn offers no legitimate way to do it. In phase 1, "replied" is a button. |
| Guessing email addresses, or buying contact data from third parties | Bounces and spam complaints, and personal data of third parties flowing to a service. |
| Pre-filling who to contact from a model's memory | A model can invent a plausible person. Names are only taken from text the user supplied, and verified against it. |

**Phase 2 (optional, not built; would need its own design before any code):**

1. Open a draft in your own mail app (a `mailto:` link), so sending stays in your hands.
2. Read-only reply detection by IMAP or the Gmail API, **opt-in**, limited to messages from addresses
   of contacts you saved, with credentials kept out of the repo and out of the database.

Anything beyond that (automatic sending, scraping, enrichment services) is out of scope unless this
decision is revisited with a new record.

## Consequences

* Everything the CRM knows is typed in or pressed by you. That is less magic and more trustworthy:
  the numbers in *Reaching people* can be believed because nothing guessed fed them.
* Reply tracking only works for what you log. A reply that arrives and is not recorded counts as
  "waiting". The Follow-ups page lists who you are waiting on, so it is hard to forget.
* The contacts are personal data about third parties, stored only on your machine, with a delete button
  for each person. Keep only what you need. Writing to people you do not know (in the EU, under the GDPR
  and Spanish rules) can carry obligations such as identifying yourself and offering an opt-out. This is
  not legal advice; check before you do it at any scale.
