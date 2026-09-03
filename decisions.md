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

---

## 2026-09-01 — Fraud is correlated with things an agent can see

**Decision:** `is_fraudster` no longer sits independent of behaviour. Fraudsters
open accounts days before using them (70% of them; the other 30% operate bought
aged accounts), pay by card, and leave a burst of hard declines before one
attempt lands. `harness/generate.py` builds that footprint deliberately.

**Why:** The day-3 generator drew fraud as `rng.random() < 0.02` with nothing
observable attached. Against that data no risk scorer can beat the 2% base rate,
however good it is — there is no signal to find. The honest report would have
been "the risk agent is noise", which is true of the data and says nothing about
the agent. Real fraud leaves a footprint, so the synthetic fraud has to leave one
or the scorecard measures the random seed instead of the scorer.

**The part that was nearly a mistake:** the first version made the footprint
*perfect* — every fraudster on a fresh account. The model then scored 1.00
precision and the false-positive line of the scorecard came out at zero, which
is the single number this project exists to print. `FRAUD_AGED_ACCOUNT_SHARE` and
`GENUINE_RETRY_STORM_RATE` put the overlap back: rings that buy aged accounts,
and genuine customers whose card keeps declining. Measured separation after the
change: median account age 28 days against 453, median ticket ₹10,333 against
₹1,357, prior declines 9 against 1 — strong, and not perfect.

---

## 2026-09-01 — Settlement error rates are inflated, and say so

**Decision:** `UNKNOWN_PAYMENT_RATE` 0.08, `AMOUNT_MISMATCH_RATE` 0.10,
`DUPLICATE_RATE` 0.05 per batch — far above what a real acquirer produces.

**Why:** A month at realistic rates (well under 1%) yields one or two exceptions.
That is not enough to tell a working reconciler from a broken one, in a test
suite or in a five-minute demo. The reconciler is scored against the injected
labels, so inflation costs nothing in honesty — as long as nobody quotes these as
industry figures, which is why the constant block says so in the file.

The same run also needed more batches: one settlement a day gave the month 30
of them, so days are split into two to four batches as an acquirer actually pays
out. 132 settlements, 103 clean, 29 carrying an injected error.

---

## 2026-09-01 — The gateway recomputes the risk score; it never takes the agent's

**Decision:** The scoring model lives in `engine/risk_model.py`, not in
`agents/risk.py`. The agent's score travels on `req.evidence["claimed_score"]` as
an assertion. `Gateway.build_context` recomputes the score from the merchant's
own payment records and puts *its* number on `PolicyContext.risk_score`, which is
what `r15_risk_evidence` and the economics gate act on.

**Why:** An agent that can assert its own risk score can justify any block it
likes. That is not a hypothetical about a rogue agent — it is what an honest LLM
does after reading a memo somebody wrote for it. The property that matters is not
that two models exist; it is that the number the gate acts on was derived from
data rather than supplied by the thing being policed. So both sides import the
same function and only one of them is trusted to run it.

`r15` refuses when the claim runs more than `RISK_CLAIM_TOLERANCE` ahead of the
recomputation, and puts both numbers in the reason. Red-team case
`risk_agent_overstates_its_score` is that path.

**Consequence worth naming:** the risk agent is now very thin — score, compare to
two thresholds, propose. That is a finding, not a shortcut. Once the score is
computed somewhere auditable and the thresholds are written down, there is not
much left in an autonomous fraud blocker. What was stopping companies shipping
one was never the model.

---

## 2026-09-01 — A block is priced against the sale it destroys

**Decision:** `economics.assess_risk` prices `BLOCK_ORDER` as expected fraud
prevented (`p x value`) against expected lost sale (`(1-p) x value x
LOST_SALE_MULTIPLIER`, multiplier 1.6). `FLAG_FOR_REVIEW` is priced against ₹15
of analyst time at `REVIEW_CATCH_RATE` 0.80. Both left the `NO_OPINION` set.

**Why:** A block costs no postage, so a fraud system left alone will block too
much: every catch is visible and every false positive is somebody else's problem.
Pricing the false positive is the only thing that stops the drift, and it makes
the break-even a *consequence* of one stated constant rather than a threshold
someone typed in — `m/(1+m)`, about 0.62 at 1.6. `test_the_block_break_even_
follows_from_the_lost_sale_multiplier` asserts exactly that, so the number cannot
be quietly detached from the reasoning behind it.

1.6 is the most arguable constant in the codebase and should be argued about.

---

## 2026-09-01 — The recovery agent stops working an invoice once a human has it

**Decision:** `RecoveryAgent.closed` — an invoice whose escalation or write-off
was *allowed* is no longer the agent's. Refused handovers do not close it.

**Why:** A bug that day 3's five-day runs could not surface. Past four attempts
the planner proposes `ESCALATE_TO_HUMAN` every single day; the idempotency key
carries the attempt count so it differs each time, `ESCALATE_TO_HUMAN` is
deliberately outside `ATTEMPT_ACTIONS`, and nothing else stops it. Each one costs
₹50. Over five days that is a rounding error. Over the 31-day replay it was on
course to be the largest line in the cost column — an agent buying the same
handover thirty times.

The fix is in the agent rather than in the rulebook because it is not a policy
question. Handing work to a person is exactly what the agent should do; doing it
repeatedly is the agent failing to notice it already had.

---

## 2026-09-01 — The holdout is split by hash, not by shuffle

**Decision:** `replay.assign_holdout` takes `blake2b(f"{seed}:{invoice_id}")` and
compares the first four bytes against the fraction. No RNG, no shuffle.

**Why:** Python salts `hash()` per process, so a holdout built on it would differ
between the run that produced a number and the run that checks it — silently.
Anyone holding the invoice ids and the seed can recompute this assignment and
confirm the split was not chosen after seeing the outcome, which is the specific
accusation an uplift claim has to be able to answer.

---

## 2026-09-01 — The uplift is reported with a confidence interval

**Decision:** `bootstrap_uplift` resamples invoices within each group 1,000 times
and reports a 95% interval next to the point estimate. The scorecard prints both,
and flags when the interval crosses zero.

**Why:** This was caught by a test, not by design, and the catch matters. On the
full month the estimator lands within 10% of ground truth. On a 500-invoice
fixture it was 88% off — the same estimator, the same code, a smaller sample. A
point estimate with no interval reads as a measurement when it is a draw, which
is precisely the overclaiming this project was built to refuse. Reporting
₹6,30,056 with no interval on a book that supports ±₹4,00,000 would have been
the project committing the sin it was written to expose.

The test now asserts that the true value falls *inside the claimed interval*,
rather than that the point estimate is near the truth with a tolerance loose
enough to pass. Resampling at the invoice level is deliberate: the noise comes
from which invoices landed in which group, so that is what gets resampled.

---

## 2026-09-01 — The analyst works the fraud queue and nothing else

**Decision:** `HumanReviewer` in `harness/outcomes.py` clears escalated *blocks*
overnight at 85% accuracy. Escalated recovery work is counted as pending and left
that way.

**Why:** Without a reviewer the layer escalated every block above ₹25,000 and
nobody ever acted, so the largest fraud in the month sailed through and the risk
numbers were misleading in the agent's favour — missed fraud ₹6,55,942 against
₹2,19,047 prevented. Adding the reviewer inverted it to ₹2,13,421 against
₹6,61,568.

Only the fraud queue, because a block is time-critical — the payment is settling
now — while escalated recovery goes on a list somebody works through on Monday.
Simulating a human who promptly resolves everything would quietly delete the
largest real cost of an escalation, which is that it waits. Accuracy is 0.85
rather than 1.0 for the same reason: at 1.0 escalation becomes a free
correctness oracle and every ceiling in the rulebook looks costless.

The reviewer approves the *request*, and the agent resubmits; the rulebook runs
again with the signature in hand. `r05` only records idempotency keys from
allowed actions, so a `needs_human` decision leaves the key unused and the
resubmission is not mistaken for a duplicate.

---

## 2026-09-01 — Two red-team cases were wrong, and the engine was right

**Decision:** Both were fixed in `harness/redteam.py`, not in the engine.

**Why it is worth recording:** the first run came out 16/18, and neither failure
was a defect.

`uneconomic_chase` expected a ₹4.50 call on a ₹40 invoice to be refused, citing
the README's "₹80 of SMS to recover ₹40". The engine allowed it, correctly: a 28%
chance of collecting ₹40 is worth ₹4.50. The README's claim is about *cumulative*
spend, which is the budget cap, not expected value — they are separate checks for
exactly this reason. The case now makes a second call and is refused at the 20%
budget of ₹8.00.

`block_with_no_evidence` expected `r15` to object and got `economics` instead.
The payment it used had a prior history that fired `outsized_ticket`, so there
*was* a signal and `r15` was right to stand aside. The case now uses an account
with no history at all.

Both were the test being wrong about the arithmetic. Recording them because a red
team whose failures are always fixed in the engine is a red team nobody is
reading carefully — and because the "right verdict from the wrong rule" check is
what caught the second one. Verdict alone would have passed it.

---

## 2026-09-01 — Nothing that decides an outcome may key on a request id

**Decision:** `HumanReviewer._rng` seeds on `payment_id`, not `request_id`.

**Why:** `request_id` is a `uuid4` minted fresh on every run. The analyst keyed
on it, so the same seed produced a different fraud figure each time — ₹6,61,568
one run, ₹5,71,651 the next, with nothing in the output hinting that the number
had moved. It was found by writing the README, not by a test: two numbers taken
from two runs an hour apart did not agree.

The rule this generalises to is now in `flow.md`'s conventions. Identity may be
random; a *decision* has to key on something the dataset fixes. `test_the_same_
seed_produces_the_same_scorecard` runs the whole replay twice and compares six
figures, so this cannot come back quietly.

Worth noting what the existing tests could not have caught: every seeded RNG in
the harness was already deterministic *given its inputs*, and each was unit
tested that way. The defect was in what was fed to one of them, which only a
whole-run comparison could see.

---

## 2026-09-01 — The dashboard is one static file, served by the API process

**Decision:** `web/index.html` — inline CSS and JS, no dependencies, no build
step — served at `/` by the same FastAPI app that serves the JSON. The README's
Next.js commitment is dropped and the stack line rewritten.

**Why:** the dashboard is three read-only views over endpoints that already
existed. A framework would have bought a build step, a second process on a
second port, CORS configuration, and an `npm install` that can fail on the
morning of the pitch — in exchange for nothing the page needed. Demo day is now
one command:

    CHECKPOST_DB=data/replay.db python -m uvicorn engine.api:app

Nothing is fetched from a CDN either, and a test asserts it: a dashboard that
needs the network to render is a dashboard that fails in a room with bad wifi.

**The cost, stated plainly:** the README promised Next.js and no longer
delivers it. If a judge asks why there is no frontend framework, the answer is
the paragraph above, and it is a better answer than a framework would have been.

**The scorecard is read, never computed.** A full replay takes about twenty
seconds, so `harness.replay` writes `out/scorecard.json` and `/v1/scorecard`
serves the file. That also fixes the relationship honestly: the dashboard is a
view over a completed run, so every number on screen came from a command anyone
can re-run and check.

---

## 2026-09-01 — The lint config keeps the checks that would catch a regression

**Decision:** `.pylintrc`, with each disable carrying its reason. Everything
pylint found that was an actual defect was **fixed**, not silenced: thirteen
unused imports, five dead variables, twelve over-long lines, two mixed line
endings, an f-string with nothing in it. The five one-off deliberate choices
(`Exception_`, the API's module-level gateway singleton and its `global`, two
deliberately broad excepts) carry inline disables at the site, so the rest of
the codebase still gets naming and exception checks.

**Why the workflow was failing:** the starter workflow GitHub adds through its
web UI installs *only* pylint and then lints the whole tree, so every
`import pydantic` and `import fastapi` came back `E0401 import-error`. It also
ran a 3.8/3.9/3.10 matrix against a codebase that requires 3.12. Both are fixed;
the lint now runs on 3.12 with the project's own dependencies installed.

**What stays enabled, and why it matters:** `missing-module-docstring`. This
project's documentation lives at module level — every module opens with an essay
on what it refuses to do — so enforcing that one is meaningful, while
`missing-function-docstring` on a suite whose test names are full sentences is
not.

A second workflow runs the 203 tests and the red team on every push. The red
team is a gate that exits non-zero, so an attack getting through fails the build
rather than appearing in a log nobody reads.

**Watch out for:** appending to a file with a shell heredoc writes LF into files
that are otherwise CRLF, and pylint's `mixed-line-endings` catches it. It caught
this twice in one session.

## 2026-09-03 — Nothing that decides an outcome may iterate a set

**Decision:** the replay walks the treated book in a fixed list order, and the
bootstrap sorts its inputs before resampling. Sets stay, but only for membership
tests.

**What was wrong:** two runs of `python -m harness.replay` on one dataset
disagreed — 2,445 requests against 2,447, economics refusals 917 against 919,
and a 95% interval that moved by tens of thousands of rupees. Both came from
iterating `set[str]`: Python salts string hashing per process, so the set came
out in a different order in every process. The recovery agent therefore worked
the invoices in a different order each run, and with a daily budget cap and a
contact-frequency window, order decides which invoices get an action at all. The
bootstrap drew different invoices for the same RNG seed for the same reason.

**Why it hid:** every headline money figure — uplift, fraud prevented, the
exception list — was identical across runs, because those are sums over the
whole book. Only the counts and the interval moved, and nobody diffs counts.
`test_the_same_seed_produces_the_same_scorecard` missed it twice over: it ran
both replays inside one process, where the hash salt is fixed, and its list of
keys did not include the interval. It now checks the interval, and
`test_the_interval_does_not_depend_on_the_order_of_the_invoices` asserts the
property directly by shuffling the input.

**The rule this is the second instance of:** day 4 established that nothing
deciding an outcome may key on a request id. This is the same rule one level
out — nothing deciding an outcome may depend on iteration order either.
`assign_holdout` had carried a docstring refusing process-salted hashing since
the day it was written; the split was safe and the ordering was not.

**Cost of not finding it:** the README quoted an interval no judge could have
reproduced, and a live re-run during the pitch would have printed different
numbers from the video.

## 2026-09-03 — The LLM planner is tested against the SDK, not against a mock

**Decision:** six tests build real `google.genai.types` objects — a
`GenerateContentResponse` carrying a `FunctionCall` — and hand them to
`GeminiPlanner` through a fake transport. A hand-rolled mock would have proved
only that our own assumptions agree with themselves.

**What it caught immediately:** the SDK coerces `ACTION_TOOL` from a plain dict
into a `FunctionDeclaration` on the way in. The first version of the test
asserted `tool["name"]` and failed with `'FunctionDeclaration' object is not
subscriptable` — which is the test doing its job: a schema the SDK cannot parse
now fails in CI rather than on the first live call.

**What it also changed:** the prompt sent a Python dict repr. `None`,
single-quoted keys and `True` are not a format a model has been trained to read,
and the memo is the one field an attacker controls, so it now goes as JSON with
unambiguous string boundaries.

**What the tests deliberately do not cover:** the HTTP call. That is what
`python -m agents.recovery` is for — one invoice, five seconds, non-zero exit if
the call did not round-trip. `GeminiPlanner.last_error` exists for that probe:
the planner falls back to rules on any failure, which is right in a batch and
indistinguishable from success when you are trying to find out whether a key
works.

**The test worth having above all the others:** a planner that proposes a
₹50,000 discount on a ₹9,000 invoice gets `NEEDS_HUMAN` from
`discount_ceiling`. Everything else here checks that the planner works. That one
checks that it does not matter whether it does.


## 2026-09-03 — .env is loaded by explicit path, never by search

**Decision:** `engine/__init__.py` loads the repo's own `.env` by an absolute
path built from `__file__`, rather than calling bare `load_dotenv()`.

**Two bugs in one line.** python-dotenv had been in `requirements.txt` since day
1 and `.env.example` documented `GEMINI_API_KEY`, but nothing ever called it: a
key written into `.env` was simply ignored, and the only symptom was an LLM
planner that "didn't work" — the same symptom as a wrong key, a rate limit, or
no network.

Then the obvious fix, bare `load_dotenv()`, was worse. Its default walks *up*
the directory tree until it finds any `.env` at all, and on this machine that is
a file in the user's home directory belonging to an unrelated project. The probe
reported `planner: gemini`, built a client on that project's stale key, and
failed on every invoice. An app that silently adopts another project's
credentials is harder to debug than one with no key at all, and on a shared
machine it is a way to spend someone else's quota.

**Why the package `__init__` and not each entry point:** the API, the harness,
the three agents and the ledger CLI all import `engine`, and a config that
depends on which module you happened to run is a config that will be wrong
exactly once, on the day.
