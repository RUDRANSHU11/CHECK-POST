# flow.md — how Checkpost works

Read this first when you come back to the project. It describes the system as it
is right now, not as it is planned. When the structure changes, this file changes
in the same pass.

Companion file: `decisions.md` records *why* each choice was made. This one is
*how it works*.

---

## 1. What this is

An AI agent wants to spend the merchant's money — send a paid SMS, retry a
charge, issue a refund, block an order. It is not allowed to just do that. It
asks Checkpost, Checkpost consults a rulebook, and the answer plus its reasoning
goes into a log that cannot be quietly edited afterwards.

Three thin agents ride on top (recovery, risk, reconcile). They exist to prove
the layer is general. The depth is in the layer, not in them.

---

## 2. Stack

| Piece | What it is | Why |
|---|---|---|
| Python 3.12 | everything server-side | already on the machine, and the whole team's strongest language |
| pydantic 2 | every domain type in `engine/schema.py` | validation at the edge; `extra="forbid"` turns a typo'd agent field into a 422 instead of a value that silently never reaches a rule |
| FastAPI + uvicorn | `engine/api.py` | thin HTTP layer, free OpenAPI docs at `/docs` for the demo |
| SQLite | the ledger, `data/checkpost.db` | zero setup, and its triggers enforce append-only at the storage layer |
| pytest | `tests/` | 127 tests, all green |
| google-genai 2.20 | `agents/recovery.py` | LLM planner. Written, **never run** — no valid key on this machine |
| one static HTML file | `web/index.html` | dashboard, served by the API process itself. No build step and no CDN: a page that needs a toolchain or the network to render is a page that fails on demo day |

Dependencies are pinned loosely in `requirements.txt` and installed into `.venv`.

---

## 3. Directory map

```
checkpost/
├── engine/                 the layer itself
│   ├── schema.py           every shared type + the action price list
│   ├── policy.py           the rulebook — 16 rules
│   ├── ledger.py           hash-chained append-only log
│   ├── gateway.py          the single door agents call
│   ├── store.py            in-memory view of the merchant's data
│   ├── api.py              FastAPI wrapper over the gateway
│   ├── economics.py        the money check — is this attempt worth making?
│   ├── risk_model.py       the fraud signals, recomputed by the gateway
│   └── console.py          UTF-8 stdout so the rupee sign does not crash Windows
├── agents/
│   ├── recovery.py         the recovery agent + both planners
│   ├── risk.py             scores a payment, proposes a block or a review
│   └── reconcile.py        settlements against the merchant's own books
├── harness/
│   ├── generate.py         synthetic merchant month
│   ├── outcomes.py         what happened afterwards + the analyst (reads truth)
│   ├── batch.py            day-3 runner, no holdout — superseded by replay.py
│   ├── replay.py           80/20 treated vs holdout, and the scorecard
│   └── redteam.py          18 attacks, each expecting a named rule to stop it
├── tests/                  204 tests
├── data/
│   ├── dataset.json        what agents may read
│   ├── ground_truth.json   what actually would have happened — agents must not read
│   └── checkpost.db        the ledger
├── web/index.html          the dashboard — scorecard, feed, ledger
├── decisions.md            why things are the way they are
└── flow.md                 this file
```

---

## 4. Runtime flow

### The main path: an agent asks to do something

1. An agent builds an `ActionRequest` — `engine/schema.py:262`. It names the
   action, the customer, optionally an invoice and payment, an amount, a
   plain-language `rationale`, and `evidence`: whatever the agent read to reach
   its conclusion. Evidence is untrusted by construction.
2. It arrives at `Gateway.submit()` — `engine/gateway.py:144`. Either over HTTP
   via `POST /v1/actions` (`engine/api.py:74`) or in-process, which is how the
   batch runner calls it.
3. **The request is written to the ledger first**, before any rule runs —
   `engine/gateway.py:148`. If evaluation crashes, the ask is still on record.
4. `build_context()` assembles a `PolicyContext` — `engine/gateway.py:124`. This
   is everything the rulebook and the gate are allowed to know: the customer,
   invoice and payment from the store, plus five counters the gateway maintains —
   contacts in the last 24h, attempts on this invoice, paise already spent
   chasing it, paise already refunded against the payment, and the set of used
   idempotency keys.
5. `policy.decide()` runs **every** applicable rule and takes the strictest
   verdict — `engine/policy.py:421`. Rules are pure functions; they read nothing
   but the request and the context.
6. **If the rulebook allowed it**, `economics.assess()` runs — is it worth it?
   `engine/gateway.py:158`. It can only tighten the answer. It is skipped
   entirely once the rulebook has refused: pricing an action we will never take
   would put a misleading expected return in the audit trail.
7. A `Decision` is built with the verdict, every rule's reasoning, the cost of
   the action and the expected recovery — `engine/gateway.py:164`.
8. The decision is written to the ledger, carrying enough of the request to
   rebuild every counter from decision entries alone — `engine/gateway.py:181`.
9. **Only if allowed**, counters move — `engine/gateway.py:189`. A denied action
   did not happen, so it consumes no quota, no budget and no idempotency key.
10. The `Decision` goes back to the agent, reasons attached.

### The rulebook

Thirteen rules in `engine/policy.py`, each `(request, context) -> RuleResult | None`
where `None` means "not applicable". Registered by the `@rule` decorator at
`engine/policy.py:121`.

| # | Rule | Fires when | Verdict |
|---|---|---|---|
| r01 | `opt_out` | customer replied STOP | deny — **no human override** |
| r02 | `contact_frequency` | 3rd contact in 24h | deny |
| r03 | `quiet_hours` | 21:00–09:00 IST, email exempt | deny |
| r04 | `refund_ceiling` | refund > ₹5,000 | needs_human |
| r05 | `duplicate_action` | idempotency key already approved | deny |
| r06 | `refund_exceeds_payment` | refund > what remains refundable | deny |
| r07 | `attempt_limit` | 4 attempts already on this invoice | needs_human |
| r08 | `unretryable_failure` | retrying a hard decline | deny |
| r09 | `disputed_invoice` | invoice is under dispute | deny |
| r10 | `settled_invoice` | invoice already paid or written off | deny |
| r11 | `discount_ceiling` | discount > 30% of invoice | needs_human |
| r12 | `write_off_ceiling` | write-off > ₹1,000 | needs_human |
| r13 | `prompt_injection` | instruction-like text in memo or evidence | deny |

Every threshold is a named constant in one block at `engine/policy.py:38` — the
whole rulebook's configuration is readable at a glance.

Severity: `deny > needs_human > allow`. A rule can only make a decision stricter,
never looser, so no ordering of rules can produce an approval that some rule
objected to.

### The economics gate

`engine/economics.py`. Runs after the rulebook, emits a `RuleResult` with
`rule_id="economics"` so the audit trail is uniform, but is deliberately not a
registered rule — see `decisions.md`.

```
expected recovery = P(this action gets us paid) × what is still owed
allow             if expected recovery > cost × 1.25
```

`P` is built at `engine/economics.py:171` from a base rate for the channel
(`CHANNEL_BASE_RATE`) or the failure reason (`RETRY_SUCCESS_RATE`), multiplied by
`ATTEMPT_DECAY ** attempts` and an age modifier. Every number is a named constant
at the top of the file. **The model never reads ground truth.**

Two checks, in this order — `engine/economics.py:199`:

1. **Expected value.** Explains *this* attempt: "₹50.00 to attempt, expected
   recovery only ₹20.00".
2. **Cumulative budget.** Total spend on one invoice may not exceed 20% of it,
   however good each attempt looked on its own. Explains *accumulation*.

What it refuses in practice: escalating a ₹40 invoice to a human, a fourth phone
call after decay has eaten the odds, a 90% discount that gives away more than it
recovers. What it allows: a 25-paise SMS chasing ₹40, which is good business.

It has no opinion on refunds, blocks, flags or settlement matching — those are
either a different calculation or not a recovery decision at all.

### The recovery agent

`agents/recovery.py`. Proposes, never acts.

- `RulePlanner` — `agents/recovery.py:85`. Deterministic escalation ladder:
  retry a transient decline first (cheapest money on the table), then walk
  email → SMS → WhatsApp → call, skipping rungs an invoice cannot justify at
  either end. Exhausted ladder escalates to a human.
- `GeminiPlanner` — `agents/recovery.py:199`. Same job, LLM with a tool-call
  schema. **Written, never executed** — no valid key on this machine. Falls back
  to `RulePlanner` on any failure.
- `RecoveryAgent.work()` — `agents/recovery.py:291`. Plans one action, builds the
  `ActionRequest`, carries the invoice memo through as `evidence` so the
  injection guard can see what the planner was reading, and submits.

The agent keeps its own `tried` tally, counting attempts including refusals. The
gateway separately counts allowed actions. **The two are expected to diverge** —
that drift is what the checkpoint exists to catch, so the architecture makes it
possible rather than defining it away.

### A batch run

`harness/batch.py`. One simulated day at a time:

1. `sim.settle_spontaneously()` — invoices that were going to pay anyway settle,
   spread across the window so some get chased first
2. the agent picks one action per still-open invoice
3. the gateway allows or refuses
4. `sim.observe()` resolves allowed actions into outcomes
5. outcomes go back into the ledger

`harness/outcomes.py` is the **only** module that reads `ground_truth.json`. It
lives in `harness/` for exactly that reason — the import graph is the
enforcement. It records attribution at the moment of settlement as
`(paise, "spontaneous" | "chased")` rather than deriving it by subtraction later.

### The ledger

- `append()` — `engine/ledger.py:120`. Reads the current head, computes
  `sha256(seq ⋮ type ⋮ payload ⋮ timestamp ⋮ prev_hash)`, inserts.
- `verify()` — `engine/ledger.py:186`. Walks from entry 1, recomputing each hash
  and checking each link. Names the exact broken entry and distinguishes three
  failures: **content break** (row edited), **link break** (row edited and
  re-hashed, caught by the next entry), **sequence jump** (row deleted).
- Two SQLite triggers reject `UPDATE` and `DELETE` outright —
  `engine/ledger.py:41`. Getting past them requires `DROP TRIGGER`, and the chain
  still catches you.
- `head()` — one hash committing to the entire history. Print it at the end of a
  demo run; anyone can re-verify against it.

### Restart

`Gateway.__init__` calls `_rebuild_from_ledger()` — `engine/gateway.py:72` — which
replays every decision and approval entry to restore all counters. The ledger is
therefore the only durable state. Anything not in it did not happen.

### Human approval

`approve()` — `engine/gateway.py:202` — records a signature and does **not**
re-run the request. The agent must resubmit, and the rules run again with
`human_approved=True`. A signature raises a ceiling; it never bypasses the
rulebook, so an approval granted yesterday cannot authorise contacting someone
who opted out this morning.

---

## 5. Data model

Agent-facing, in `data/dataset.json`:

- **Customer** — `customer_id`, contact details, `opted_out_at` + `opt_out_channel`
- **Invoice** — `amount_paise`, `issued_at`, `due_at`, `status`
  (open/paid/overdue/disputed/written_off), and `memo`, the untrusted free-text
  field where a poisoned PDF's contents land
- **Payment** — `amount_paise`, `method`, `status`, `failure_reason`;
  `is_retryable` is derived from the failure reason
- **Settlement** — `utr`, `amount_paise` (net, after the acquirer's fee),
  `fee_paise`, `payment_ids`. The bank's *claim* about what it paid for; the
  reconciler re-derives the gross from the merchant's own payment records and
  compares

Produced by the layer, in the ledger:

- **ActionRequest** → **Decision** (verdict + `RuleResult[]` + cost) → **Outcome**
  (what actually happened, which is what the scorecard is computed from)
- **LedgerEntry** — `seq`, `entry_type`, `payload`, `prev_hash`, `entry_hash`.
  Entry types: `request`, `decision`, `outcome`, `human_approval`.

Scoring-only, in `data/ground_truth.json` — **no agent may read this**:

- `would_pay_anyway` — pays with no contact at all; the holdout recovers these
- `pays_if_contacted` — pays only because chased; this is the only real uplift
- `payments[id].is_fraud` — blocking it prevents a loss, blocking its neighbour
  costs a sale. Correlated with observable behaviour on purpose: uncorrelated
  fraud would make any scorer indistinguishable from the base rate
- `settlements[id].exception` — which of the four disagreements was injected
- `unsettled_payment_ids` — captured payments no batch ever mentions

Money is always integer paise. Never a float, never rupees.

---

## 6. Conventions

Follow these so new code matches what is there:

- **Money in paise, as `int`.** `rupees()` at the edges, `fmt()` for display.
- **Datetimes are tz-aware UTC.** Never `datetime.now()` — use
  `schema.utcnow()`. Rules receive the clock on the context; they never read it.
- **New rule?** Write a pure function in `engine/policy.py`, decorate with
  `@rule`, return `None` when it does not apply. Put its threshold in the
  constants block. Add a test to `tests/test_policy.py` and bump the count in
  `test_rulebook_has_not_shrunk`.
- **New action type?** Add to `ActionType` *and* `ACTION_COST_PAISE` — the assert
  at `engine/schema.py:171` fails at import if you forget the price. Decide
  whether it belongs in `CONTACT_ACTIONS`, `MONEY_OUT_ACTIONS`, `ATTEMPT_ACTIONS`.
- **Reasons are written for a human.** Every `RuleResult.reason` is a sentence a
  judge can read aloud, with the actual numbers in it. Never a rule ID alone.
- **The ledger is append-only.** Nothing but `Ledger.append()` ever writes to it.
- **Rules stay pure.** No I/O, no globals, no clock. If a rule needs a new fact,
  add a field to `PolicyContext` and fill it in `build_context()`.
- **Nothing under `engine/` or `agents/` may read `ground_truth.json`.** Only
  `harness/` may. If you need truth to compute something, that something belongs
  in the harness.
- **Record attribution, never derive it.** Any accounting that subtracts one
  total from another to work out where money came from should be suspected —
  that is the shape of the bug that overstated recovery by 3.6×.
- **New economic parameter?** Named constant at the top of `engine/economics.py`,
  never inline. A judge should be able to read the whole model in one screen.
- **Never trust an agent's own assessment of itself.** A score, a confidence, a
  claim that something reconciles — all of it arrives on `req.evidence` as an
  assertion. If a rule or the gate needs the number, the *gateway* recomputes it
  from the store and puts its own answer on `PolicyContext`. See
  `_risk_facts` and `_settlement_facts` in `engine/gateway.py`.
- **New red-team case?** Name the rule that must stop it, not just the verdict.
  `harness/redteam.py` fails a case that gets the right answer from the wrong
  rule — a coincidence stops working when the data moves.
- **Any headline number gets an interval.** A point estimate with no interval
  reads as a measurement when it is one draw. `bootstrap_uplift` is the pattern.
- **CLI entry points call `setup_console()` first**, or the rupee sign crashes
  the run on Windows.
- **Tests use a frozen clock** (`NOON_IST` / `NIGHT_IST` in `tests/conftest.py`).
  Anything submitting a contact action against the live wall clock will pass by
  day and fail after 21:00 IST.

---

## 7. Run it

```bash
cd C:\Users\rudra\checkpost

# once
.venv\Scripts\python.exe -m pip install -r requirements.txt

# generate the month (deterministic; seed recorded in the output)
.venv\Scripts\python.exe -m harness.generate

# tests
.venv\Scripts\python.exe -m pytest -q

# the scorecard: all three agents, 80/20 treated vs holdout, whole month
.venv\Scripts\python.exe -m harness.replay
.venv\Scripts\python.exe -m harness.replay --block-threshold 0.45   # price the dial
.venv\Scripts\python.exe -m harness.replay --llm                    # Gemini, if a key exists

# the attacks — exits non-zero if anything got through
.venv\Scripts\python.exe -m harness.redteam

# day 3's runner, kept for comparison. No holdout, so its recovery figure is
# the flattering one and it says so.
.venv\Scripts\python.exe -m harness.batch --limit 400 --days 5

# serve — dashboard at http://127.0.0.1:8000/, API docs at /docs
# CHECKPOST_DB picks which run the feed and ledger views show. PowerShell
# syntax: `set VAR=value` is cmd, and in PowerShell it sets nothing and says
# nothing, so the dashboard quietly reads the default empty ledger instead.
$env:CHECKPOST_DB = "data/replay.db"
.venv\Scripts\python.exe -m uvicorn engine.api:app --reload

# `[Errno 10048] only one usage of each socket address` after a clean
# "Application startup complete" is a port clash, not an app failure: something
# else already holds 8000. Add --port 8010.

# lint, with every disable justified in .pylintrc
.venv\Scripts\python.exe -m pylint $(git ls-files '*.py')

# check the log has not been rewritten
.venv\Scripts\python.exe -m engine.ledger verify
.venv\Scripts\python.exe -m engine.ledger tail
```

On Windows set `PYTHONIOENCODING=utf-8` if you run a script that bypasses
`setup_console()`.

Endpoints: `POST /v1/actions` · `POST /v1/actions/{id}/approve` ·
`POST /v1/outcomes` · `GET /v1/stats` · `GET /v1/rules` · `GET /v1/ledger` ·
`GET /v1/ledger/verify` · `GET /health`

---

## 8. Current state

**Working, tested (204 tests green, red team 18/18, pylint clean):**

- Event schema, price list, money and time handling
- Synthetic month: 1,200 customers, 3,400 invoices, 5,000 payments,
  132 settlement batches, ₹76,08,874 outstanding, 79 opted-out customers,
  23 poisoned memos, 622 would-pay-anyway vs 600 pay-only-if-chased,
  23 fraud rings, 83 captured payments the bank never settled
- Hash-chained ledger with triggers and a three-way `verify()`
- Policy engine, 16 rules
- Economics gate: expected value, attempt decay, staleness, 20% budget cap on
  the recovery side; fraud-prevented against lost-sale on the risk side
- Gateway: submit → rulebook → gate → logged, counters, restart, human approval
- FastAPI surface over all of it
- Recovery agent with a deterministic planner; Gemini planner written but unrun
- Risk agent over a shared, gateway-recomputed signal model
- Reconciler with four named exception classes and an unsettled sweep
- Replay harness: 80/20 treated vs holdout, bootstrap confidence interval
- Red team: 18 attacks, each asserted against the rule that must stop it
- Dashboard at `/` — scorecard, live decision feed, ledger viewer. One static
  file served by the API process; the feed polls, so an action submitted
  through `/docs` during a demo appears as it is judged
- CI on every push: pylint, the test suite, and the red team as a gate

**Last full replay** — every collectable invoice, 31 simulated days:

```
Treated 964 invoices (₹61,42,080 book) · holdout 250 (₹14,60,398, never contacted)

Recovered, treated                  ₹ 10,27,597
Recovered, holdout scaled            ₹ 3,97,540    x4.21 from ₹94,523
UPLIFT the agent caused              ₹ 6,30,056    95% CI ₹1,94,936 to ₹10,79,642

Fraud prevented                      ₹ 6,61,568    27 blocked, 11 caught in review
Lost sales, blocked genuine                  ₹ 0    0 customers turned away
Fraud that got through               ₹ 2,13,421    12 payments

Settlements checked 119 · 101 unresolved exceptions, ₹13,09,022 at stake
  unsettled 69 · unknown_payment 12 · duplicate_payment 10 · amount_mismatch 10

Actions requested 2,445 · allowed 1,320 · denied 1,080 · escalated 45
Refusals: economics 917 · opt_out 80 · attempt_limit 39 · unretryable 37 ·
          prompt_injection 31 · contact_frequency 13 · block_ceiling 7 · disputed 2
Ledger: 6,097 entries, intact

METHOD CHECK — measured ₹6,30,056 against ground truth ₹5,71,192, 10% off
```

The economics gate is still the single largest source of refusals by a wide
margin, which is the point: it is the part nobody else builds.

**Read the interval, not the point estimate.** ₹6,30,056 is one draw of one
month. The book supports an interval nearly as wide as the estimate, and on a
500-invoice fixture the same estimator came out 88% off. It excludes zero, so
the effect is real; the magnitude is not pinned down to the rupee and the
scorecard says so. Quote the interval.

**Two numbers that are honest and read badly:**

- *Lost sales ₹0.* The block threshold (0.70) is deliberately conservative and
  the month produced no false positive at it. That is not a free lunch — the
  cost shows up in the next line instead, as ₹2,13,421 of fraud that got
  through. `--block-threshold` prices the dial: lowering it to 0.45 makes things
  *worse*, because the economics gate then refuses the extra blocks while the
  flags that were catching fraud disappear into them.
- *101 unresolved exceptions.* Most of them (69) are captured payments the bank
  never settled. That is money the merchant is owed, found by a sweep no
  batch-by-batch reconciler would do, and reported rather than resolved.

**Not built yet, in plan order:**

| Day | What |
|---|---|
| 6 | Demo video, architecture diagram, three pitch dry-runs |

Submit **5 Sept**, early in the day.

**Known gaps to watch:**

- The Gemini planner has never round-tripped. The key in the sibling projects is
  malformed (50 chars, `Ab8R…`); a real AI Studio key is 39 and starts `AIza`.
  This is now the largest single risk left: every number above comes from the
  deterministic planner, and the LLM path is the one a judge will ask about.
- The risk agent has no LLM variant at all. The `Scorer` protocol is there for
  one, and the gateway already refuses inflated claims, so the interesting demo
  (an LLM talked into blocking a competitor) is reachable — but not written.
- No confidence interval on the *fraud* numbers. The uplift has one; prevented
  and lost-sale are point counts over a few dozen events and are noisier than
  they look.
- The reconciler matches whole batches only. A real one nets partial refunds and
  chargebacks against a settlement; this one would report those as mismatches.
- The dashboard reads a completed run. It cannot start one, and there is no
  button to. That is the honest relationship, but it does mean a stale
  `out/scorecard.json` will be shown without complaint if nobody re-runs the
  replay — the page prints the run's timestamp so the staleness is visible.
