# Decisions

Why the code is the way it is. Append-only, newest at the bottom.
Companion to `flow.md`, which describes how it works *now*; this file records
*why* each choice was made, in the order the choices happened.

---

## 2026-08-31 — Money is an integer count of paise

**Decision:** Every amount in the system is `int` paise. `float` never touches a
monetary value. `rupees()` converts at the edges, `fmt()` renders for display.
Both live in `engine/schema.py:25`.

**Why:** The project's headline claim is honest arithmetic — a scorecard that
subtracts holdout recovery from treated recovery and reports the difference. That
sum runs over thousands of amounts. In float, `0.1 + 0.2 != 0.3`, and the error
accumulates in exactly the digit a judge would check. Integer paise makes every
total exact by construction.

**Alternatives considered:** `Decimal` is also exact, but it serialises awkwardly
to JSON, is slower, and invites mixed-type arithmetic where one operand slips
through as a float. Integers have none of those failure modes.

---

## 2026-08-31 — Timestamps are timezone-aware UTC; IST is a fixed offset

**Decision:** All datetimes are tz-aware UTC. The quiet-hours rule converts to IST
using a hardcoded `timezone(timedelta(hours=5, minutes=30))` rather than
`ZoneInfo("Asia/Kolkata")` — `engine/policy.py:58`.

**Why:** Naive datetimes compare wrongly without raising, which in a
quiet-hours rule means silently calling people at 3am. Separately, `zoneinfo`
on Windows needs the `tzdata` package, which CPython does not ship — so
`ZoneInfo("Asia/Kolkata")` raises `ZoneInfoNotFoundError` on the machine this
demo runs on. India has never observed DST, so a fixed offset is not an
approximation here; it is exactly correct, with one less dependency to fail on
demo day.

**Alternatives considered:** Adding `tzdata` to requirements. Rejected — a
dependency whose only job is to express a constant that has never changed.

---

## 2026-08-31 — The ledger gets two independent defences

**Decision:** `engine/ledger.py` protects the log twice: SQLite `BEFORE UPDATE`
and `BEFORE DELETE` triggers that `RAISE(ABORT)` (`engine/ledger.py:41`), plus a
SHA-256 hash chain where each entry hashes the one before it
(`engine/ledger.py:80`).

**Why:** The two defend against different attackers. Triggers stop accidents —
a bug, a migration, an ORM doing something clever. They do not stop anyone who
can run `DROP TRIGGER`. The hash chain stops that person: editing a row leaves a
break that `verify()` locates by sequence number. Either alone is a weaker claim
than "you cannot edit this without it being obvious."

**Alternatives considered:** Hash chain only. Rejected because an append-only log
that silently permits `UPDATE` invites a bug that corrupts history between
verifications. Triggers only. Rejected because they are trivially removable and
prove nothing to a third party.

---

## 2026-08-31 — The hash covers the stored bytes, not a re-serialised object

**Decision:** `verify()` hashes the exact `payload` text as read from the row. It
never parses the JSON and re-dumps it. Fields are joined with `\x1f` (unit
separator) before hashing — `engine/ledger.py:80`.

**Why:** Re-serialising would make the hash depend on the JSON encoder's key
ordering and whitespace, so a verification could fail after a library upgrade, or
worse, a tamper that only reorders keys could pass. Hashing stored bytes means
one changed byte is one broken hash. The `\x1f` separator is chosen because it
cannot appear in JSON text, so a payload cannot be crafted to shift field
boundaries and collide — `tests/test_ledger.py` asserts this directly.

---

## 2026-08-31 — The ledger is the only source of truth; counters are rebuilt from it

**Decision:** The gateway keeps contact counts, attempt counts, refunded totals
and used idempotency keys in memory, but rebuilds all of them by replaying the
ledger on startup — `engine/gateway.py:72`.

**Why:** If durable state lived in two places they could disagree, and the
in-memory copy would be the one lying. Rebuilding from the log means the log is
definitionally correct: anything not in it did not happen. It also makes restart
behaviour free rather than a feature to remember to build.

**Alternatives considered:** A separate state table. Rejected — it creates a
second thing to keep consistent, and a second thing an auditor has to trust.

---

## 2026-08-31 — The request is written to the ledger before it is judged

**Decision:** `Gateway.submit()` appends the `request` entry, *then* evaluates
rules, *then* appends the `decision` entry — `engine/gateway.py:138`.

**Why:** A log that only records completed decisions can be emptied by crashing
at the right moment. Writing the ask first means an interrupted evaluation still
leaves evidence that the agent asked. The cost is that a crash can orphan a
request with no decision, which is the far better failure: a visible gap rather
than a silent omission.

---

## 2026-08-31 — Every rule runs on every request, and the worst verdict wins

**Decision:** `decide()` does not short-circuit on the first denial. It collects
every applicable rule's `RuleResult` and takes the strictest verdict —
deny > needs_human > allow (`engine/policy.py:415`). Allow decisions keep their
reasoning too, not just denials.

**Why:** "Why was this blocked?" should be answerable with every objection, not
just whichever rule happened to be registered first. The demo shows a refund that
is both above the ceiling *and* on a poisoned invoice; showing one reason would
understate the case. Keeping reasons on allows matters as much — "nothing
objected, and here is what was checked" is auditable, while an empty list is
indistinguishable from rules never having run.

**Alternatives considered:** Short-circuit on first deny, for speed. Rejected —
thirteen predicate functions cost nothing measurable, and the audit trail is the
product.

---

## 2026-08-31 — Rules are pure functions over an explicit context

**Decision:** Every rule has the signature
`(ActionRequest, PolicyContext) -> RuleResult | None`. Rules read no globals, open
no database, and never call `now()` themselves — everything arrives on
`PolicyContext` (`engine/policy.py:93`). The gateway assembles it at
`engine/gateway.py:121`.

**Why:** This is what makes all thirteen testable without standing up a server,
and it is why a rule behaves identically live and in a replay of last month —
which the Day 4 holdout comparison depends on entirely. A rule that read the
clock itself could not be replayed.

---

## 2026-08-31 — Only allowed actions move a counter

**Decision:** A denied request consumes no contact quota, increments no attempt
count, and does not burn its idempotency key — `engine/gateway.py:172`.

**Why:** A denied action did not happen. If refusals consumed the daily contact
allowance, an agent that made two bad requests at 3am would be locked out of two
legitimate ones at noon — the safety layer would be causing the harm it exists to
prevent. Keeping the key reusable matters for the same reason: a request refused
for a fixable reason should be resubmittable once fixed.

---

## 2026-08-31 — Opt-out is the one rule a human signature cannot raise

**Decision:** `human_approved` on the context lifts the refund ceiling
(`r04`) and the write-off ceiling (`r12`). It has no effect on `r01_opt_out`,
which returns `DENY` unconditionally — `engine/policy.py:146`.

**Why:** Every other threshold in the book is a risk appetite, and a person with
authority is entitled to change it. Consent is not a risk appetite. "A manager
approved it" is not a defence for messaging someone who withdrew consent, so the
code should not offer that path at all. `tests/test_policy.py` asserts the
override fails.

---

## 2026-08-31 — Thirteen rules, not the eight to ten the README planned

**Decision:** Shipped `r01`–`r13`. The four beyond the original sketch are
`r09_disputed_invoice`, `r10_settled_invoice`, `r11_discount_ceiling` and
`r12_write_off_ceiling`.

**Why:** Each closes a hole the demo would otherwise be asked about. `r10` in
particular guards the ugliest bug in any recovery system — a stale read sending
a dunning message to somebody who paid an hour ago. They are ten to fifteen
lines each and share the existing rule harness, so the cost was close to zero.

**Alternatives considered:** Holding to ten and adding the rest later. Rejected
because the marginal cost now is far below the cost of a judge finding the gap.

---

## 2026-08-31 — Escalating and writing off do not count as recovery attempts

**Decision:** `ATTEMPT_ACTIONS` excludes `ESCALATE_TO_HUMAN` and `WRITE_OFF`, and
`r07_attempt_limit` returns `None` for both — `engine/gateway.py:47`.

**Why:** Otherwise the rule that exists to stop an endless chase would block the
only two moves that end one. An invoice at the attempt limit would have no legal
exit, and the agent would be stuck resubmitting forever.

---

## 2026-08-31 — Ground truth lives in a separate file agents cannot open

**Decision:** `harness/generate.py` writes `data/dataset.json` (what agents see)
and `data/ground_truth.json` (what actually would have happened). `DataStore` has
no code path that opens the second file.

**Why:** The project's central claim is that it does not cherry-pick its numbers.
Keeping the answer key in the same object the agents read makes that claim rest
on our own discipline. Keeping it in a file nothing on the agent path opens makes
it rest on the architecture instead. `tests/test_generate.py` asserts none of the
ground-truth keys appear anywhere in the agent-facing dataset.

---

## 2026-08-31 — The data models two separate propensities per invoice

**Decision:** Ground truth carries `would_pay_anyway` and `pays_if_contacted` as
independent draws per invoice — `harness/generate.py:122`.

**Why:** This is the whole basis of the holdout claim. A recovery agent that only
ever reaches customers who were going to pay regardless produces a large
"recovered" number and zero real uplift. For the scorecard to expose that, the
data has to contain the trap: treated recovery is
`would_pay_anyway OR (contacted AND pays_if_contacted)`, holdout recovery is
`would_pay_anyway` alone, and the difference is the only number worth reporting.
Generating a single "will pay" flag would have made the uplift arithmetic
tautological.

---

## 2026-08-31 — Generation is seeded and deterministic

**Decision:** `generate(seed=42)` produces byte-identical output every run; the
seed is recorded in both output files.

**Why:** The scorecard is the deliverable. A number nobody else can reproduce is
not evidence. Anyone can regenerate the month and re-derive the figures.

---

## 2026-08-31 — Gateway logic and HTTP are separate modules

**Decision:** Deviates from the README's layout. `engine/gateway.py` holds the
`Gateway` class with no web dependency; `engine/api.py` is a thin FastAPI wrapper
over it. The README also did not anticipate `engine/schema.py` or
`engine/store.py`; both were added.

**Why:** The Day 4 replay harness pushes thousands of requests through
`Gateway.submit()` in-process. Routing that through HTTP would add latency and a
second failure mode for no benefit. Splitting them also means the rules are
tested without a server. `schema.py` exists because every module needs the same
types and importing them from `policy.py` would have made the rulebook a
dependency of the ledger.

---

## 2026-08-31 — API configuration is read at startup, not at import

**Decision:** `CHECKPOST_DATASET` and `CHECKPOST_DB` are read inside the FastAPI
lifespan handler rather than at module level — `engine/api.py:34`.

**Why:** Import-time configuration cannot be redirected to a temp directory by a
test without reimporting the module, and a server whose data location is fixed
the moment Python parses the file is awkward to run twice on one machine.

---

## 2026-08-31 — Email is exempt from quiet hours

**Decision:** `QUIET_HOURS_EXEMPT = {SEND_EMAIL}` — `engine/policy.py:64`.

**Why:** The rule protects people from being woken up. Email is silent and
asynchronous; it does not buzz a phone at 3am. Blocking it overnight would cost
recovery for no welfare gain.

**Consequence worth recording:** this surfaced a real defect in the first
version of `tests/test_api.py`. The HTTP layer reads the wall clock, so a test
that submitted an SMS passed during the day and failed after 21:00 IST — it
failed on the first full run for exactly that reason. API tests now use email so
they are time-independent; rule behaviour is tested against a frozen clock in
`tests/test_policy.py` instead. Any future test that submits a contact action
over HTTP has the same trap waiting.

---

## 2026-08-31 — Decision ledger entries duplicate key request fields

**Decision:** The `decision` entry copies `action`, `customer_id`, `invoice_id`,
`payment_id`, `amount_paise` and `idempotency_key` from the request alongside the
verdict — `engine/gateway.py:165`.

**Why:** Counter rebuilding, and any later audit, can then work from decision
entries alone without joining back to request entries. Verification should never
depend on two separate log entries agreeing with each other. The duplication is a
few dozen bytes per entry and buys a materially simpler correctness argument.

---

## 2026-08-31 — Generated data is gitignored, not committed

**Decision:** `data/*.json` is in `.gitignore`. The repo carries the generator,
not its 3.2 MB of output.

**Why:** Generation is deterministic and the seed is recorded in both output
files, so `python -m harness.generate` rebuilds them byte for byte. Committing
them would add megabytes to every clone to store something a one-line command
reproduces — and would invite the two drifting apart if someone edited the JSON
by hand.

**Consequence:** a fresh clone must run the generator before `engine.api` will
start. The lifespan handler fails with that exact instruction rather than a
`FileNotFoundError`.

---

## 2026-08-31 — The economics gate is a separate step, not a fourteenth rule

**Decision:** `engine/economics.py` runs after `policy.decide()` inside
`Gateway.submit()` — `engine/gateway.py:154`. It emits an ordinary `RuleResult`
with `rule_id="economics"` so the audit trail is uniform, but it is not
registered with `@rule`.

**Why:** It answers a different question. The rulebook answers "are we allowed
to?", the gate answers "is it worth it?", and a merchant needs to tell those two
refusals apart — one is a compliance matter, the other a budgeting one. It also
returns a *quantity*, the expected recovery, which gets recorded on the decision;
no other rule produces a number. Keeping it out of the rule registry means
`policy.py` stays a set of pure predicates over facts, with no pricing model
inside it.

**Alternatives considered:** Registering it as `r14`. Rejected — it would have
needed the price list, the probability model and the spend counter injected into
every rule's context, and "not allowed" and "not worth it" would have become
indistinguishable in the log.

---

## 2026-08-31 — Expected value is checked before the budget cap

**Decision:** `assess()` evaluates the expected-value test first, then the
cumulative-spend cap — `engine/economics.py:214`.

**Why:** Both can fail at once, and the first one to fail supplies the reason
the merchant reads. Ordering the budget check first produced
"chasing this invoice has already cost ₹0.00; another ₹50.00 would exceed the
20% budget" on a *first* attempt, which is technically true and useless. The
expected-value message — "₹50.00 to attempt, expected recovery only ₹20.00" —
explains the actual problem. The budget message now also reads differently when
nothing has been spent yet.

---

## 2026-08-31 — The recovery probability model is hand-written, not learned

**Decision:** Channel base rates, retry success rates by failure reason, an
attempt-decay factor and age modifiers, all as named constants in
`engine/economics.py:56`.

**Why:** A fitted model would be more accurate and is the obvious next step. It
would also be unauditable in a five-minute demo: a judge cannot argue with a
weight vector. Every number here can be pointed at and disagreed with, which
right now is worth more than being right to three decimal places.

**Explicitly rejected:** letting the model read `ground_truth.json`. It would
make the gate clairvoyant and every number downstream a fiction.

---

## 2026-08-31 — The agent keeps its own attempt tally, allowed to be wrong

**Decision:** `RecoveryAgent.tried` counts what the agent *attempted*, including
refusals. The gateway separately counts what it *allowed*. The two are expected
to diverge — `agents/recovery.py`.

**Why:** In a real deployment the agent's view is its own, and it goes stale. An
architecture where the agent reads the gateway's counters would define that
drift out of existence and quietly remove the thing the checkpoint is for. A
test asserts the two disagree after a refusal.

---

## 2026-08-31 — Two planners behind one interface, rule-based first

**Decision:** `RulePlanner` (deterministic escalation ladder) and
`GeminiPlanner` (LLM with a tool-call schema) implement the same `Planner`
protocol. `build_planner()` picks Gemini only when `GEMINI_API_KEY` is set, and
falls back on any failure.

**Why:** The rule planner is not a stub. It is why the demo runs if the network
is bad or a key dies on the day, and it is what makes the batch numbers
reproducible. The LLM planner is what makes the system interesting — it handles
cases nobody enumerated — and it is also the component most likely to propose
something reckless, which is the argument for the checkpoint.

**Status:** the Gemini path is **written but never executed**. The only key on
this machine is the malformed one from earlier projects (50 chars, `Ab8R…`;
real AI Studio keys are 39 and start `AIza`). Treat its first live run as
debugging.

---

## 2026-08-31 — Two bugs in outcome attribution, and what they cost

**Decision:** `OutcomeSimulator` takes a `scope` of invoice ids, records
attribution at the moment of settlement as `(paise, "spontaneous" | "chased")`
rather than reconstructing it by subtraction, and spreads spontaneous
settlements across the run window with a second seeded draw.

**Why:** Both changes fix bugs that produced wrong numbers while every test
passed.

1. **Unscoped settlement.** `settle_spontaneously()` walked the entire open book
   while the batch subtracted only its own share. Money from invoices the run
   never touched landed in gross recovery. Attributable recovery printed
   ₹4,12,301; the correct figure was ₹1,14,320 — an overstatement of 3.6×.
2. **Every spontaneous payment landing on day one.** `_rng()` builds a fresh
   seeded generator per call, so the same invoice drew the same number every
   day: it either settled immediately or never. Everything that was going to pay
   anyway therefore settled *before* the agent chased anyone, and the simulation
   could never produce its own central case — money spent chasing somebody who
   was going to pay regardless.

**How to apply:** attribution is recorded, never derived. Any future accounting
that reaches for a subtraction between two totals should be suspected first.

---

## 2026-08-31 — Day 3's recovery figure is labelled as biased, not presented as uplift

**Decision:** `harness/batch.py` prints "attributable to chasing" and then states
in the output that the number is biased upward and must not be quoted.

**Why:** With no control group, a retry or escalation that lands on a
would-pay-anyway invoice is credited to chasing, because nothing can distinguish
the two. That is precisely the confound the day-4 holdout removes. A project
whose pitch is honest measurement cannot print a flattering number without
saying which way it is wrong.
