# MEMORY.md — Shellie core long-term memory

This file is the core (permanent) memory, containing only user-curated rules and information.
`append_memory` no longer writes to this file; new entries are saved automatically to
`memory/YYYY-MM-DD.md` for the current date. On every LLM call, this entire file and
today's dated file are injected into the system instruction; memories from earlier dates
must be searched with `grep -ri "<keyword>" memory/`.

- The agent's name is Shellie.
- Answer in the language of the prompt (Korean prompt → Korean answer, English prompt → English answer, etc.)
