"""Checkpost engine.

The one thing this package does at import: read the repo's own `.env`.

`.env.example` documents `GEMINI_API_KEY` and `CHECKPOST_DB`, and
`requirements.txt` has carried python-dotenv since day 1, but nothing ever
called it — so a key written into `.env` was silently ignored, `build_planner()`
fell back to the rule planner, and the only visible symptom was that the LLM
planner "didn't work". Loading it here covers every entry point (the API, the
harness, the agents, the ledger CLI) because all of them import `engine`.

The path is explicit rather than `load_dotenv()`'s default, which walks *up* the
directory tree until it finds any `.env` at all. On this machine that found
`C:\\Users\\rudra\\.env` from an unrelated project and handed Checkpost its
expired key: the probe then reported `planner: gemini` and failed on every
invoice. An app that silently adopts another project's credentials is worse than
one with no key at all.

`override=False` is the default and is the behaviour we want: a variable already
set in the shell beats the file, so `CHECKPOST_DB=... python -m ...` still wins.
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
