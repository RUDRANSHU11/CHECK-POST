"""The ledger's whole claim is that it cannot be quietly rewritten. These tests
try to rewrite it quietly."""

from __future__ import annotations

import sqlite3

import pytest

from engine.ledger import GENESIS_HASH, Ledger, canonical, compute_hash


def fill(lg: Ledger, n: int = 5) -> None:
    for i in range(n):
        lg.append("decision", {"request_id": f"rq_{i}", "verdict": "allow", "amount_paise": i * 100})


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
