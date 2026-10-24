# skills/

Shellie's built-in skill library.

## Convention

- **Every skill is a `.md` procedure.** The file holds steps for the LLM to
  *follow*, not code to run. New skills are always `.md`.
- **Code goes in helper scripts.** When a step needs deterministic logic, put
  it in a standalone script in this directory and name it with a leading `_`
  (e.g. `_sheet_from_csv.py`). The procedure tells the LLM to call it through
  `execute_shell`. Helpers are not listed as skills.
- Helpers are Python standard library only — no pip dependencies, ever (same
  zero-dependency rule as the rest of the project).
- **Auto-discovery:** on every LLM call, the agent lists this directory and
  injects one line per skill into the system prompt:
  ```
  - skills/<name>.md: <first non-empty line, without the leading #>
  ```
  Files starting with `_` are skipped, and `README.md` is never listed.
- The first line is the only context the LLM gets before deciding whether to
  read the rest, so make it a clear, one-line summary. It can be written in
  any language the LLM should read; English is recommended for consistency
  with the rest of the codebase.

## Example

See [`sample_skill.md`](sample_skill.md) for the reference skill: it reports
machine status by running a few shell commands and summarizing the output.

## Helpers

- `_sheet_from_csv.py` — CSV → Google Spreadsheet via `gws`. Called by
  `sheet_from_csv.md` and `gws.md`. Run `python3 skills/_sheet_from_csv.py --help` for usage.
- `_slides_create.py` — title and bullet slides → Google Slides via `gws`.
  Called by `slides_create.md` and `gws.md`. Run `python3 skills/_slides_create.py --help` for usage.
