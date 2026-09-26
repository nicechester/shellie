# skills/

Shellie's dynamic skill (script) library.

## Convention

- A skill is a standalone, runnable script placed in this directory. Python
  standard library only — no pip dependencies are allowed, ever (same
  zero-dependency rule as the rest of the project).
- There is no loader. The agent's LLM runs skills directly through its
  `execute_shell` tool, e.g.:
  ```sh
  python3 skills/sample_skill.py
  ```
- **Auto-discovery:** on every LLM call, the agent lists this directory and
  injects one line per skill into the system prompt:
  ```
  - skills/<name>.py: <first line of the module docstring>
  ```
  Only files ending in `.py` are considered, and files starting with `_` are
  skipped (so you can keep private helpers or works-in-progress out of the
  list). The line is built from `ast.get_docstring()` on the module — if a
  skill has no docstring, it is still listed, just without a description.
- Because that first docstring line is the only context the LLM gets before
  deciding whether to run a skill, make it a clear, one-line summary of what
  the script does. It can be written in any language the LLM should read;
  English is recommended for consistency with the rest of the codebase.
- Keep the rest of the usage instructions (arguments, expected output) in
  the docstring too — the LLM only sees the first line automatically, but it
  can `execute_shell("python3 skills/<name>.py --help")` or read the file via
  `execute_shell("cat skills/<name>.py")` if it needs more detail.

## Example

`sample_skill.py` — the reference skill, prints the current time and
hostname:

```python
"""Sample skill: prints the current time and hostname.

Usage: python3 skills/sample_skill.py
"""
import datetime
import socket

def main():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} @ {socket.gethostname()}")

if __name__ == "__main__":
    main()
```

This is the pattern to copy for new skills: a one-line docstring summary, a
`main()` guarded by `if __name__ == "__main__":`, and nothing outside the
standard library.

## Markdown skills

A `.md` file in this directory is a procedure document, not code — it holds
steps for the LLM to *follow*, rather than a script for it to *run*. The
agent's auto-discovery only injects the file's **first line** into the
system prompt (the same way it injects the .py docstring's first line); when
a request matches, the LLM reads the full file with
`execute_shell("cat skills/<name>.md")` and follows the steps described
there.

Because that first non-empty line (usually a `# Title` heading) is the only
context the LLM gets before deciding whether to read the rest, make it a
clear, one-line summary — it is the discovery hook, exactly like the
docstring first line is for `.py` skills.

Use a `.md` skill for procedures, domain knowledge, or conventions where the
exact steps may vary by situation and benefit from LLM judgment. Use a `.py`
skill when the same behavior must happen deterministically, byte-for-byte,
every time. As with `.py` skills, files starting with `_` are skipped, and
`README.md` itself is never listed.

See [`sample_skill.md`](sample_skill.md) for the reference example.
