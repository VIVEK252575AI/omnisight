# Week 2 — Code-Response Extraction & Rejection (W2.6)

How raw generator text becomes a validated patch proposal — or is rejected.
Mid-project **Extraction Check**: isolate executable patch content from an AI
answer that mixes prose and code, **without ever executing it**.

Implemented in `backend/app/patch_extraction.py` and
`backend/app/patch_code_checks.py`, wired into
`backend/app/action_engine.py` and `backend/app/routers/patches.py`.

## Required generator output format

The prompt template `backend/app/ai/prompts/patch-proposal-v1.txt`
(`PATCH_PROPOSAL_PROMPT_VERSION = "patch-proposal-v1"`) asks the generator for:

- a **single JSON object** matching `PatchProposal`, or
- exactly **one** fenced block tagged `omnisight-patch` containing that object.

Everything outside the object/block is explanation and is discarded
(`discarded_prose_chars`). Untagged code fences are **never** used as patches.

## Extraction order

1. the whole (BOM-stripped) text parses as one JSON object -> use it;
2. else exactly one ```` ```omnisight-patch ```` block -> use its body;
3. else:
   - several `omnisight-patch` blocks -> `ambiguous_multiple_patches`;
   - output looks cut off -> `truncated`;
   - otherwise -> `no_patch_found`.

Edge cases handled: UTF-8 BOM, CRLF, curly ("smart") quotes in JSON (one
best-effort recovery pass), unterminated/nested fences, prose containing
code-like text, and oversized output (capped **before** parsing ->
`too_large`).

## Validation pipeline (`SourcePatchExtractor`)

| Step | What it does | Codes |
| --- | --- | --- |
| schema | payload -> `PatchProposal`; `finding_id` must match | `schema_invalid` |
| paths | normalise, forbidden-file, extension, allow-list | `path_unsafe`, `forbidden_file`, `unsupported_file_type`, `path_not_approved` |
| applicability | read the target file **read-only**; `old_text` must occur exactly once | `edit_not_applicable`, `edit_ambiguous` |
| payload | structural checks on the *inserted* `new_text` | `forbidden_construct` |

`old_text` and file text are newline-normalised to `\n` on both sides, so a
CRLF file and an LF edit still match.

## Structural checks (`patch_code_checks`)

Applied to the **inserted** text only (never `old_text`, which is pre-existing
code that must not be judged). No CSS/JS parser ships in the backend, so these
are deliberately **heuristics** — deep syntax validation is deferred to the
**W3.3 sandbox build**, where the candidate tree is actually compiled.

- **delimiter balance** — `{}`/`()`/`[]` must balance outside string literals;
  only enforced when the text *opens* a structure, so bracket-free fragments
  (e.g. `height: auto;`) are allowed.
- **JSX tag balance** — a best-effort heuristic, only when a closing tag (`</`)
  is present; fragments (`<>`/`</>`) handled; `<` must be followed immediately
  by a letter or `/`, so `a < b` is never read as a tag.
- **CSS forbidden constructs** — external `@import url(`, `expression(`,
  `javascript:`.
- **JS/TS/JSX forbidden constructs** — `eval(`, `Function(`, `child_process`,
  `fs` string literals, `XMLHttpRequest`, `<script`, `document.write`,
  `dangerouslySetInnerHTML`, `fetch(`/`import(` to an absolute URL.

## Result type

`ExtractionResult{status, proposal|null, reasons[], discarded_prose_chars}`
(`ExtractionStatus.accepted | rejected`). `rejected` is a normal outcome and is
recorded as a reviewable proposal record with reason codes — never raised,
never reported as success.

## Safety

Nothing here runs `eval`, `exec`, `subprocess`, `node` or any interpreter on
model text. Input is size-capped before parsing, the fence regex is lazy and
linear over bounded input, and on any uncertainty the answer is rejected.
