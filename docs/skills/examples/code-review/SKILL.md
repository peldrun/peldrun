---
name: code_review
description: Systematic code review that finds bugs, security issues, and maintainability problems in a codebase or patch.
---

# Code Review

You are performing a **systematic code review**. Your goal is to find real
defects and risk, not to produce a style report. Every finding must be
backed by evidence from the code you actually read.

## Review Checklist

Work through the code in this order. Skip sections that do not apply to
the files you were asked to review, but do not skip a section merely
because it seems unlikely — verify.

### 1. Correctness

- Does the code do what its name and comments say it does?
- Are all branches and error paths handled?
- Are edge cases (empty input, null, zero, negative, extremely large,
  concurrent access) handled?
- Do loops terminate on all inputs?
- Is any mutation of shared state potentially racy?

### 2. Security

- Is user input validated before use (SQL, shell, path, HTML, JSON,
  deserialization)?
- Are secrets hard-coded, logged, or sent over insecure channels?
- Are file paths confined to an intended root (`..` traversal)?
- Are authorization checks present at every privileged boundary?
- Is cryptographic material handled with a maintained library, not
  ad-hoc logic?

### 3. Error Handling

- Are exceptions caught, or silently swallowed?
- Does a failed operation leave state in a half-updated condition?
- Are error messages useful to a caller without leaking internals to an
  end user?

### 4. Maintainability

- Are names accurate and specific?
- Is any block doing more than one thing that would be easier to test
  if split?
- Is there dead code, unreachable branches, or obviously-stale comments?
- Are magic numbers and strings explained or extracted?

### 5. Tests

- Do the changes include tests for the new behaviour?
- Do the existing tests still exercise the changed code path?

## Output Format

Produce **one section per finding**, ordered by severity (Critical first).
If a finding has multiple variants, group them.

For each finding, emit:

    ### [Severity] Short one-line title

    **Location:** `path/to/file.py:LINE` (or `:LINE-LINE`)

    **Issue:** One or two sentences describing the defect.

    **Evidence:**
excerpt from the code


**Fix:** Concrete recommendation. If multiple approaches are viable,
say so and note the trade-off.

Severity levels:

- **Critical** — data loss, security breach, crash on realistic input,
silent corruption.
- **High** — incorrect behaviour on common inputs, resource leak,
missing authorization.
- **Medium** — incorrect behaviour on edge cases, maintainability risk
that is likely to cause a future bug.
- **Low** — style, clarity, minor refactor suggestions.

## Rules

1. **Do not invent findings.** If a file has no issues, say so in one
sentence and move on.
2. **Quote the code you are critiquing.** Do not paraphrase; paste the
actual excerpt.
3. **Verify before flagging.** A "suspicious" line is not a finding
until you confirm the issue by reading enough surrounding code.
4. **Separate verified defects from suggestions.** A suggestion goes
under a `## Suggestions` section at the end, not under a severity
heading.
5. **If you cannot review a file** (permission error, parse error,
binary), report that explicitly rather than skipping it silently.
