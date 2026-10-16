# 0002. How drafts and CV advice are kept honest

Status: accepted, 2026-10-16.

## Context

M6 adds two features where a local model writes or picks text the user may act on: a draft of a
follow-up message, and advice on which CV lines to put forward for a posting. The rule is the one from
M5: the model must not be able to claim experience the CV does not contain. A first end-to-end run with
`qwen3:8b` showed that the M5 guard (quotes must exist in the CV) is **not enough** for free text:

* The draft said "my experience includes ... deploying models with Kubernetes". The CV quotes it listed
  were real, but the sentence was not backed by them. The model had copied Kubernetes from the posting's
  requirements, which had been handed to it as background facts.
* The advisor proposed real CV lines for requirements they have nothing to do with (a "multi-agent
  pipeline" for Java), and so reported only one requirement as lacking evidence. The quote existed; it did
  not support the requirement.

## Decisions

1. **Withhold what the model could mistake for the candidate's own experience.** The drafter gets the
   company, role title, location, dates and message history, and the CV when the user chooses to use it. It does
   **not** get the posting's requirements. Fixing the cause beats policing the symptom.
2. **Scan the draft deterministically.** Every number must appear in the CV or the facts
   (`unsupported_numbers`), and every proper noun or acronym must too (`unsupported_terms`: capitalised
   words mid-sentence, and acronyms anywhere). A failure is sent back to the model with the offending
   terms named; on the last attempt the draft is kept and shown with a visible warning, because it is
   a draft the user edits and sends.
3. **Limit the advisor by the fit analysis.** Advice is generated for a fit analysis, and highlights are only
   accepted for requirements that analysis found met or partial; the model is not even shown the others.
   Requirements without a verified highlight are reported as "not evidenced" by code, not by the model.
4. **Do not display claims we cannot check.** The model's `cv_quotes` for a draft ("the words I relied on")
   are verified to exist, but nothing shows the text actually uses them, so the UI does not present them as
   backing.

## Consequences and limits

* The term scan is a net, not a proof: lower-case terms (pandas, docker) pass, and an unsupported claim
  made in ordinary words ("I led a team") is not caught. The UI therefore says to read the draft, and the
  draft is never sent by jobtrail.
* It can flag harmless words (a day name, a place not in the facts). That costs a retry or a warning, not
  correctness.
* Re-run after the changes (same synthetic case, `qwen3:8b`): the advisor highlights only the three requirements
  the fit analysis verified and lists Java, Kubernetes and Spark as not evidenced; five drafts (en/es) had no
  fabricated skills and needed no retries. At temperature 0 the outputs are nearly identical, so this is
  closer to one observation than five.
* Spanish drafts can be gendered ("quedo atento") and sometimes use English words ("update"): the
  language model does not know the sender's gender or preferred phrasing. Edit before sending.
