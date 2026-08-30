"""Console setup.

Python on Windows defaults stdout to cp1252, which cannot encode the rupee sign
and raises UnicodeEncodeError mid-print — so a scorecard would die halfway
through the number it exists to show. Every CLI entry point calls setup_console()
first.
"""

from __future__ import annotations

import sys


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                # Already-detached or non-reconfigurable stream: printing plain
                # ASCII still works, so this is not worth failing a run over.
                pass
