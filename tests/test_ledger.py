"""The ledger's whole claim is that it cannot be quietly rewritten. These tests
try to rewrite it quietly."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from engine.ledger import GENESIS_HASH, Ledger, canonical, compute_hash


def fill(lg: Ledger, n: int = 5) -> None:
    for i in range(n):
        lg.append(
            "decision",
            {"request_id": f"rq_{i}", "verdict": "allow", "amount_paise": i * 100},
        )


def test_empty_ledger_verifies(ledger):
    result = ledger.verify()
    assert result.ok and result.entries_checked == 0
    assert ledger.head() == GENESIS_HASH


def test_append_and_verify(ledger):
    fill(ledger)
    assert len(ledger) == 5
    result = ledger.verify()
    assert result.ok
    assert result.entries_checked == 5


def test_head_commits_to_the_whole_history(ledger):
    fill(ledger, 3)
    head_before = ledger.head()
    ledger.append("decision", {"request_id": "rq_x"})
    assert ledger.head() != head_before


def test_first_entry_chains_to_genesis(ledger):
    entry = ledger.append("request", {"a": 1})
    assert entry.prev_hash == GENESIS_HASH
    assert entry.seq == 1


def test_each_entry_chains_to_the_previous(ledger):
    a = ledger.append("request", {"a": 1})
    b = ledger.append("request", {"a": 2})
    assert b.prev_hash == a.entry_hash


# -- defence 1: the database refuses edits --------------------------------- #

def test_update_is_rejected_by_trigger(ledger):
    fill(ledger, 3)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("UPDATE ledger SET payload = '{}' WHERE seq = 2")


def test_delete_is_rejected_by_trigger(ledger):
    fill(ledger, 3)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("DELETE FROM ledger WHERE seq = 2")


# -- defence 2: the chain catches whoever gets past defence 1 --------------- #

def _drop_triggers(path) -> sqlite3.Connection:
    raw = sqlite3.connect(str(path))
    raw.executescript("DROP TRIGGER ledger_no_update; DROP TRIGGER ledger_no_delete;")
    return raw


def test_edited_payload_breaks_the_chain(ledger, tmp_path):
    fill(ledger, 5)
    path = ledger.path
    ledger.close()

    raw = _drop_triggers(path)
    raw.execute("UPDATE ledger SET payload = replace(payload, '300', '999999') WHERE seq = 4")
    raw.commit()
    raw.close()

    result = Ledger(path).verify()
    assert not result.ok
    assert result.broken_seq == 4
    assert "content break" in result.detail
    # Everything before the tamper is still provably good.
    assert result.entries_checked == 3


def test_removed_entry_breaks_the_chain(ledger):
    fill(ledger, 5)
    path = ledger.path
    ledger.close()

    raw = _drop_triggers(path)
    raw.execute("DELETE FROM ledger WHERE seq = 3")
    raw.commit()
    raw.close()

    result = Ledger(path).verify()
    assert not result.ok
    assert result.broken_seq == 4
    assert "removed" in result.detail


def test_rehashed_entry_still_breaks_the_following_link(ledger):
    """The sophisticated attack: edit a row *and* recompute its hash so the row
    verifies against itself. The next entry's prev_hash is what catches it."""
    fill(ledger, 5)
    path = ledger.path
    ledger.close()

    raw = _drop_triggers(path)
    raw.row_factory = sqlite3.Row
    row = raw.execute("SELECT * FROM ledger WHERE seq = 3").fetchone()
    forged_payload = canonical({"request_id": "rq_2", "verdict": "allow", "amount_paise": 999999})
    forged_hash = compute_hash(
        row["seq"], row["entry_type"], forged_payload, row["recorded_at"], row["prev_hash"]
    )
    raw.execute(
        "UPDATE ledger SET payload = ?, entry_hash = ? WHERE seq = 3",
        (forged_payload, forged_hash),
    )
    raw.commit()
    raw.close()

    result = Ledger(path).verify()
    assert not result.ok
    assert result.broken_seq == 4
    assert "link break" in result.detail


# -- canonicalisation ------------------------------------------------------ #

def test_canonical_is_key_order_independent():
    assert canonical({"b": 1, "a": 2}) == canonical({"a": 2, "b": 1})


def test_canonical_serialises_datetimes():
    from engine.schema import utcnow

    text = canonical({"when": utcnow()})
    assert "T" in text and "when" in text


def test_hash_changes_when_any_field_changes():
    base = compute_hash(1, "decision", '{"a":1}', "2026-08-31T00:00:00+00:00", GENESIS_HASH)
    assert base != compute_hash(2, "decision", '{"a":1}', "2026-08-31T00:00:00+00:00", GENESIS_HASH)
    assert base != compute_hash(1, "request", '{"a":1}', "2026-08-31T00:00:00+00:00", GENESIS_HASH)
    assert base != compute_hash(1, "decision", '{"a":2}', "2026-08-31T00:00:00+00:00", GENESIS_HASH)
    assert base != compute_hash(1, "decision", '{"a":1}', "2026-08-31T00:00:01+00:00", GENESIS_HASH)
    assert base != compute_hash(1, "decision", '{"a":1}', "2026-08-31T00:00:00+00:00", "f" * 64)


def test_field_separator_cannot_be_forged():
    """Concatenating fields with a printable delimiter would let a payload
    ending in that delimiter shift the boundaries and collide."""
    a = compute_hash(1, "decision", "x", "t", GENESIS_HASH)
    b = compute_hash(1, "decision|x", "", "t", GENESIS_HASH)
    assert a != b


def test_concurrent_readers_do_not_corrupt_each_other(ledger):
    """One connection, several threads — the shape FastAPI actually serves.

    Sync endpoints run on a threadpool, so the dashboard polling /v1/ledger,
    /v1/ledger/verify and /v1/pending hands the same connection to several
    threads at once. Unserialised that fails three ways: InterfaceError,
    IndexError inside _row_to_entry, and json.loads(None) from a row whose
    columns came back misaligned. The last is the reason this is a test and not
    a shrug — a read that returns the wrong bytes can fail verify() on an intact
    chain, and a false tamper alarm is the worst lie this project can tell.

    The rest of the suite misses it because TestClient drives requests one at a
    time, so nothing before this ever overlapped two reads.
    """
    # 800 rows, not a handful: the reads have to overlap in time to collide, and
    # at 60 rows each one finishes before the next thread starts. Checked by
    # running this against a no-op lock — at 60 it passes either way, which
    # would have made it decoration. It fails every run at 800.
    fill(ledger, 800)
    errors: list[Exception] = []
    start = threading.Barrier(8)

    def hammer() -> None:
        start.wait()  # release every thread on the same instant
        for _ in range(10):
            try:
                assert len(ledger.entries(limit=800)) == 800
                assert ledger.verify().ok
            except Exception as exc:  # pylint: disable=broad-exception-caught
                errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"{len(errors)} concurrent failures, first: {errors[0]!r}"


def test_concurrent_appends_each_get_their_own_link(ledger):
    """Two appends must not read the same head and chain to the same parent.

    The read-then-write in append() is only atomic because the lock spans both.
    A chain with a duplicate seq or a forked prev_hash still *looks* fine entry
    by entry; verify() is what catches it, so that is what this asserts.
    """
    start = threading.Barrier(8)

    def writer(n: int) -> None:
        start.wait()
        for i in range(10):
            ledger.append("decision", {"request_id": f"rq_{n}_{i}"})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = ledger.verify()
    assert result.ok, result.detail
    assert result.entries_checked == 80


def test_tail_returns_the_newest_entries_oldest_first(ledger):
    """The feed endpoint's read. It used to be entries()[-limit:], which built a
    model for every row in the table to keep the last few — on the one endpoint
    the dashboard polls every three seconds."""
    for i in range(120):
        ledger.append("decision", {"request_id": f"rq_{i}"})

    tail = ledger.tail(10)
    assert [e.payload["request_id"] for e in tail] == [f"rq_{i}" for i in range(110, 120)]
    assert [e.seq for e in tail] == sorted(e.seq for e in tail)
    assert ledger.tail(500) == ledger.entries()
    assert ledger.tail(0) == []
