"""Vercel entrypoint — the same app as ``python -m uvicorn engine.api:app``.

Two things a serverless filesystem changes, and nothing else:

1. **The ledger has to be writable.** Vercel mounts the deployment read-only
   except for the temp directory, and ``Ledger.__init__`` opens SQLite
   read-write (it runs CREATE TABLE IF NOT EXISTS on every open, before any
   endpoint is reached). So the bundled replay ledger is copied out once per
   cold start. Writes then work, but they live and die with the instance:
   approving something on the deployed site does not persist, and a second
   instance will not see it. That is the right trade for a public read-only
   view of a finished run, and the wrong one for anything that matters.
2. **Paths must be absolute.** ``engine.api`` reads CHECKPOST_SCORECARD at
   import and defaults it to the relative ``out/scorecard.json``, which only
   resolves if the process happens to be started from the repo root. Hence the
   env vars below, and hence they are set *before* the import at the bottom.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

os.environ.setdefault("CHECKPOST_DATASET", str(ROOT / "data" / "dataset.json"))
os.environ.setdefault("CHECKPOST_SCORECARD", str(ROOT / "out" / "scorecard.json"))

# ponytail: per-instance ephemeral ledger. Point CHECKPOST_DB at a hosted
# Postgres/Turso if approvals ever need to outlive a single cold start.
_bundled = ROOT / "data" / "replay.db"
_writable = Path(tempfile.gettempdir()) / "checkpost.db"
if _bundled.exists() and not _writable.exists():
    shutil.copyfile(_bundled, _writable)
os.environ.setdefault("CHECKPOST_DB", str(_writable))

from engine.api import app  # noqa: E402  # must follow the env setup above

__all__ = ["app"]
