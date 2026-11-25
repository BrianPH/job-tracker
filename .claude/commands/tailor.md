---
description: Manually produce generation.json for a job whose CV/letter generation was handed off (issue #11/M3b)
argument-hint: <job_id or path to request.json>
---

A job-tracker generation attempt failed and handed off to you instead of
silently falling back to a template. Your job: read the written request,
act as the tailor model yourself, and write back a `generation.json` the
web UI can upload and render through the exact same validator a live API
reply goes through.

## 1. Find request.json

Argument given: `$ARGUMENTS`

- If it looks like a path (contains `/` or ends in `.json`), use it directly.
- Otherwise treat it as a job id: search `applications/*/request.json` and
  pick the one whose `job.job_id` field matches. If none matches, say so and
  stop — don't guess a folder from the job id's title/company, since an
  ad-hoc (URL/paste-ingested) job's folder name may not match what you'd
  expect.

Read that file. It contains:
- `job`: title, company, location, description (the vacancy).
- `analysis`: the structured `AnalysisResult` if the analyse call already
  succeeded before the failure, else `null` — if `null`, you must do that
  step yourself first (see below).
- `error`: why the live call failed (for your context only; it does not
  change what you produce).
- `language`: "nl" or "en" — the facts and vacancy's language.
- `analyse_system_prompt` / `tailor_system_prompt`: the exact system prompts
  the live API calls use.
- `bullet_bank`, `education`, `skills`: the candidate's facts, already
  formatted the same way the live prompt sees them.
- `style_example`: prior letters by the same candidate, for tone.
- `response_schema`: the JSON schema `generation.json` must validate against
  (this is `TailorResult`'s schema).

## 2. Analyse (only if `analysis` is `null`)

Follow `analyse_system_prompt` against `job.description` and produce the
`AnalysisResult` shape yourself (language, requirements, keywords, contact,
deadline, application_requirements) — you don't need to write this to any
file, just use it as input to step 3.

## 3. Tailor

Follow `tailor_system_prompt` exactly — in particular:
- Never invent employers, projects, degrees, skills, years of experience,
  or certifications. Every claim must trace to `bullet_bank`/`education`/
  `skills` as given.
- `experience_order` and the keys of `bullets` must be role ids from
  `bullet_bank`, verbatim. `bullets` values must be bullet ids from that
  SAME role, verbatim.
- `skills` is an ordered subset of the names in `skills`, verbatim.
- Write `profile` and `letter_paragraphs` in the vacancy's `language`,
  matching the length/paragraph constraints in `tailor_system_prompt`.
- `requirement_map` is for the candidate's review only, never sent to the
  employer — an honest `"gap"` is more useful than a forced `"met"`.

Produce a JSON object matching `response_schema` exactly.

## 4. Write generation.json

Write that JSON object — the bare `TailorResult` shape, no wrapper, no
extra keys like `model` or `stop_reason` — to `generation.json` in the
SAME folder as `request.json`. Tell the user the path, and that uploading
it in the web UI (or via the `/jobs/{job_id}/generation-upload` route)
will run it through the same validator a live reply goes through: an id
that doesn't exist in `candidate.yaml` will be rejected with a clear error
rather than rendered.
