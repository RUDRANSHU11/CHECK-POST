"""Tamper-evident ledger — a diary that can prove it has not been rewritten.

Two independent defences, deliberately:

1. **SQLite triggers** reject UPDATE and DELETE on the ledger table outright. An
   honest bug, or an ORM doing something clever, cannot quietly rewrite history.
2. **A hash chain** means that even someone who drops the triggers and edits a
   row with raw SQL leaves a visible break: every entry hashes the entry before
   it, so one altered byte invalidates that entry and every entry after it.

Defence 1 is what stops accidents. Defence 2 is what makes the log evidence.

The hash covers the *stored bytes* of the payload, not a re-serialisation of the
parsed object. Re-serialising would let a tamper that only changes key order or
whitespace slip through, and would also make verification depend on the exact
JSON encoder version — neither is acceptable in something we ask a judge to
trust.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from engine.schema import LedgerEntry, utcnow

#: prev_hash of the first entry. 64 zeros, so the chain has a fixed root.
GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_type  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    entry_hash  TEXT NOT NULL UNIQUE
);

CREATE TRIGGER IF NOT EXISTS ledger_no_update
BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: UPDATE rejected');
END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete
BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: DELETE rejected');
END;
"""


def canonical(payload: dict[str, Any]) -> str:
    """Deterministic JSON. Sorted keys, no incidental whitespace, UTC datetimes
    as ISO strings. Two runs of the same data must produce byte-identical text
    or the chain is not reproducible."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def compute_hash(
    seq: int, entry_type: str, payload_text: str, recorded_at: str, prev_hash: str
) -> str:
    """The chain link. Field separator is \\x1f (unit separator) so that a value
    containing a delimiter cannot be crafted to shift the field boundaries."""
    blob = "\x1f".join([str(seq), entry_type, payload_text, recorded_at, prev_hash])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class VerifyResult:
    ok: bool
    entries_checked: int
    broken_seq: int | None = None
    detail: str = ""

    def __str__(self) -> str:
        if self.ok:
            return f"ledger intact — {self.entries_checked} entries verified"
        return (
            f"LEDGER BROKEN at entry #{self.broken_seq} "
            f"after {self.entries_checked} good entries: {self.detail}"
        )


class Ledger:
    """Append-only, hash-chained decision log."""

    def __init__(self, db_path: str | Path = "data/checkpost.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        #: Every touch of `conn` below is taken under this. check_same_thread is
        #: off because FastAPI serves sync endpoints on a threadpool, and that
        #: hands one connection to several threads at once — which sqlite3
        #: permits and does not make safe. The dashboard polls /v1/ledger,
        #: /v1/ledger/verify and /v1/pending together, so the overlap is the
        #: normal case, not an edge one. Unserialised it fails three ways:
        #: InterfaceError, IndexError out of _row_to_entry, and — the one that
        #: matters — json.loads(None) from a row whose columns came back
        #: misaligned. A read that returns the wrong bytes could fail verify()
        #: on an intact chain, which is a false tamper alarm in the one place
        #: this project asks to be believed.
        #:
        #: This comment used to claim the lock existed. It did not.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- writing ---------------------------------------------------------- #

    def append(self, entry_type: str, payload: dict[str, Any]) -> LedgerEntry:
        """Add one entry and return it. The only way to write to this table."""
        payload_text = canonical(payload)
        recorded_at = utcnow().isoformat()

        # Read-then-write: the lock spans both, or two appends can read the same
        # head and chain themselves to the same predecessor.
        with self._lock:
            cur = self.conn.cursor()
            row = cur.execute(
                "SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["entry_hash"] if row else GENESIS_HASH
            seq = (row["seq"] + 1) if row else 1

            entry_hash = compute_hash(seq, entry_type, payload_text, recorded_at, prev_hash)
            cur.execute(
                "INSERT INTO ledger (seq, entry_type, payload, recorded_at, prev_hash, entry_hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (seq, entry_type, payload_text, recorded_at, prev_hash, entry_hash),
            )
            self.conn.commit()

        return LedgerEntry(
            seq=seq,
            entry_type=entry_type,
            payload=json.loads(payload_text),
            recorded_at=datetime.fromisoformat(recorded_at),
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )

    # -- reading ---------------------------------------------------------- #

    def __len__(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) AS n FROM ledger").fetchone()["n"])

    def head(self) -> str:
        """Hash of the newest entry — the single value that commits to the whole
        history. Print it at the end of a demo run and anyone can re-verify."""
        with self._lock:
            row = self.conn.execute(
                "SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return row["entry_hash"] if row else GENESIS_HASH

    def entries(self, entry_type: str | None = None, limit: int | None = None) -> list[LedgerEntry]:
        sql = "SELECT * FROM ledger"
        args: list[Any] = []
        if entry_type:
            sql += " WHERE entry_type = ?"
            args.append(entry_type)
        sql += " ORDER BY seq"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        with self._lock:
            return [self._row_to_entry(r) for r in self.conn.execute(sql, args)]

    def tail(self, limit: int = 50) -> list[LedgerEntry]:
        """The newest ``limit`` entries, returned oldest first.

        Not ``entries()[-limit:]``. That built a LedgerEntry for every row in
        the table — 6,097 json.loads and 6,097 model constructions — and threw
        all but the last few hundred away, on the one endpoint the dashboard
        polls every three seconds. It also held the lock for the whole of it,
        against a /v1/ledger/verify poll that legitimately needs every row.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM ledger ORDER BY seq DESC LIMIT ?", (max(0, limit),)
            ).fetchall()
        return [self._row_to_entry(r) for r in reversed(rows)]

    def _iter_rows(self) -> Iterator[sqlite3.Row]:
        # Fetched under the lock and then iterated, rather than streamed: a bare
        # generator holds the cursor open across the caller's work, and verify()
        # hashes every row as it goes. That is the window another thread's read
        # walked into.
        with self._lock:
            rows = self.conn.execute("SELECT * FROM ledger ORDER BY seq").fetchall()
        return iter(rows)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(
            seq=row["seq"],
            entry_type=row["entry_type"],
            payload=json.loads(row["payload"]),
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
            prev_hash=row["prev_hash"],
            entry_hash=row["entry_hash"],
        )

    # -- verifying -------------------------------------------------------- #

    def verify(self) -> VerifyResult:
        """Walk the chain and report the first break, with the reason.

        Distinguishes two failures on purpose: a *content* break means the row's
        own bytes were altered; a *link* break means a row was removed or
        reordered. A demo that can name which one happened is far more
        convincing than one that just says "invalid".
        """
        expected_prev = GENESIS_HASH
        expected_seq = 1
        checked = 0

        for row in self._iter_rows():
            if row["seq"] != expected_seq:
                return VerifyResult(
                    False,
                    checked,
                    row["seq"],
                    f"sequence jumped: expected #{expected_seq}, found #{row['seq']} "
                    f"(an entry was removed)",
                )
            if row["prev_hash"] != expected_prev:
                return VerifyResult(
                    False,
                    checked,
                    row["seq"],
                    f"link break: entry claims to follow {row['prev_hash'][:12]}… "
                    f"but the previous entry hashes to {expected_prev[:12]}…",
                )

            recomputed = compute_hash(
                row["seq"],
                row["entry_type"],
                row["payload"],
                row["recorded_at"],
                row["prev_hash"],
            )
            if recomputed != row["entry_hash"]:
                return VerifyResult(
                    False,
                    checked,
                    row["seq"],
                    f"content break: stored hash {row['entry_hash'][:12]}… but the "
                    f"row's own bytes hash to {recomputed[:12]}… (this entry was edited)",
                )

            expected_prev = row["entry_hash"]
            expected_seq += 1
            checked += 1

        return VerifyResult(True, checked)

    def close(self) -> None:
        with self._lock:
            self.conn.close()


# --------------------------------------------------------------------------- #
# CLI: python -m engine.ledger verify [db]
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import os
    import sys

    from engine.console import setup_console

    setup_console()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    # Same CHECKPOST_DB the server reads. Without this the CLI verified
    # data/checkpost.db no matter which run you had just served, and an empty
    # ledger verifies clean — "intact, 0 entries" is the most convincing wrong
    # answer this tool can give.
    db = sys.argv[2] if len(sys.argv) > 2 else os.getenv("CHECKPOST_DB", "data/checkpost.db")
    ledger = Ledger(db)

    if cmd == "verify":
        result = ledger.verify()
        print(f"{db}: {result}")
        print(f"head: {ledger.head()}")
        sys.exit(0 if result.ok else 1)
    elif cmd == "tail":
        for entry in ledger.entries()[-20:]:
            print(f"#{entry.seq:<5} {entry.entry_type:<18} {entry.entry_hash[:12]}…")
    else:
        print(f"unknown command: {cmd} (try: verify, tail)")
        sys.exit(2)
