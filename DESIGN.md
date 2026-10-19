# Tollgate — design decisions

Each section lists the options considered, what was chosen, and the trade-off. Sections marked
*(planned)* are written in the phase that builds them.

---

## 1. Money is integer paise, end to end

**Options.** `float` rupees · `Decimal` rupees · integer paise.
**Chosen.** Integer paise (`BIGINT` in Postgres, `StrictInt` in Pydantic via `tollgate.money.Paise`).
**Why.** Floats cannot represent 0.10 exactly, so sums drift. `Decimal` is exact but invites mixed
`Decimal`/`float` arithmetic and needs scale discipline at every boundary. Paise make every
amount an exact integer, and comparisons like "refunded ≤ captured" become plain integer
checks the database can enforce. `StrictInt` rejects `10.0`, `"1000"` and `True` at every API and
tool boundary, so a float never reaches the ledger. `BIGINT` because INT4 overflows at
₹2.1 crore.
**Cost.** Display code must format (`format_inr`), and any LLM-produced amount has to be converted
explicitly. That is deliberate: it forces the conversion to happen in one audited place.

## 2. Ledger invariants live in the database, not only in app code

**Options.**
(a) Check in Python before writing.
(b) Single-row `CHECK` constraints only.
(c) `CHECK` constraints plus triggers for cross-row rules.
**Chosen.** (c).
**Why.** Every later phase adds code paths that touch money (executor, reconciliation, webhook
handler, approval CLI, chaos scripts). With (a), *any* buggy path, a manual `psql` fix, or two
racing transactions can break an invariant. With (c), Postgres refuses the commit no matter who
wrote it. `tests/ledger/test_constraints.py` proves this by writing raw SQL past the service.
**Cost.** Logic is split between Python and PL/pgSQL, and trigger errors are less friendly. The
service still pre-checks the common cases (`LedgerError`) so callers get readable errors; the
database is the backstop, not the first line.

| Invariant | Enforced by |
|---|---|
| total refunded ≤ captured, per payment | `payments.refunded_paise` counter maintained by `refunds` triggers + `CHECK (refunded_paise <= captured_paise)` |
| counter can't be faked | trigger rejects direct writes to `refunded_paise` (allowed only at trigger depth ≥ 2) |
| ledger balances | deferred constraint trigger: each journal has ≥ 2 entries summing to 0 at `COMMIT` |
| ledger is immutable | `journals`/`ledger_entries` reject `UPDATE`, `DELETE`, `TRUNCATE` |
| no refund on an order cancelled before capture | refunds require `payments.status = 'captured'`; a cancelled order's payment can't be captured; status transitions are one-way |

## 3. Refunds reserve against the payment (counter) instead of summing on insert

**Options.**
(a) On each refund insert, a trigger runs `SUM(amount) FROM refunds WHERE payment_id = …` and compares.
(b) Keep a running `refunded_paise` on `payments`, updated by trigger, checked by `CHECK`.
**Chosen.** (b), and **pending refunds count** (it is a reservation; a failed refund releases it).
**Why.** (a) is racy under READ COMMITTED: two concurrent inserts each see the other's row as
invisible and both pass, unless the trigger also locks the payment row, at which point it's (b) with
extra work. In (b) the `UPDATE payments …` takes the payment's row lock, so concurrent refunds on
one payment **serialise**: the second waits, re-reads the committed counter, and fails the `CHECK`.
`test_concurrent_refunds_serialise_on_payment_row_lock` shows the second transaction blocking.
Counting pending refunds closes the window where two refunds are both "in flight" at the
provider and together exceed the capture.
**Cost.** Refunds on the *same* payment are serialised (fine: that's the contention we want).
Refunds on different payments don't contend.

## 4. Double-entry with signed amounts and a deferred balance check

**Options.** Separate debit/credit columns · signed amounts.
**Chosen.** One signed `amount_paise` per entry (debit > 0, credit < 0); a journal balances iff its
entries sum to 0.

| Event | Debit | Credit |
|---|---|---|
| capture | provider_clearing | revenue |
| refund reserved (pending) | refunds_contra | refunds_payable |
| refund settled | refunds_payable | provider_clearing |
| refund released (failed) | refunds_payable | refunds_contra |

The balance check is a `DEFERRABLE INITIALLY DEFERRED` constraint trigger because entries are
inserted one row at a time; the journal only has to balance by `COMMIT`. Corrections are new
(reversing) journals, never edits. The property test also checks that account balances equal what
the operational tables imply (e.g. `provider_clearing = Σ captured − Σ settled refunds`).

## 5. Cancellation

Before capture, cancelling voids the authorisation: no money moved, nothing to post, and the
payment can never be captured or refunded afterwards. After capture (not shipped), the order is
marked cancelled but money only goes back through a refund, the same guarded path as any
other refund. There is exactly one way to return money.

## 6. The mock provider is a separate system

**Options.** Mock the provider in-process · separate service sharing our tables · separate service
and separate database.
**Chosen.** `mockpay` is its own FastAPI service, own package (a test fails if it imports
`tollgate`), own database (`mockpay`) on the same Postgres server.
**Why.** The failure we care about is *two systems disagreeing*: the provider committed a refund
and we don't know. Shared state would hide that. Like real providers, mockpay also refuses
refunds beyond the captured amount, so the damage the naive agent does in A2 is measured as
duplicates the provider actually *accepted* (partial refunds repeated), not impossible ones.
**Cost.** Seeding has to create the payment in both systems. Its schema uses `create_all` (no
migrations) because it is a test double.

## 7. Idempotency-Key semantics

Modelled on Stripe and the IETF `Idempotency-Key` HTTP header draft:

- same key + same body → stored response replayed with `Idempotent-Replayed: true`;
- same key + different body → `422`;
- same key while the first request is still running → `409`;
- 4xx outcomes (e.g. over-refund) are cached under the key too; requests that failed *before*
  commit (`error_before_commit`) store nothing, so a retry executes normally.

The key is claimed in its own short transaction (`INSERT … ON CONFLICT DO NOTHING`), then the
refund and the stored response are written in **one** transaction, so a key is never "done"
without its refund or vice versa. The key is **optional**: without one, every call is a new
refund. That is how the naive agent gets hurt.

**Reconciliation lookup.** Refunds carry a merchant `receipt` (as in Razorpay), and
`GET /v1/refunds?receipt=` finds them. The plan said lookup by idempotency key; real providers
don't expose keys for lookup, so the receipt is the realistic handle. A3's executor will set
`receipt` to the intent id.

## 8. Fault injection

Three sources, in priority order: `X-Mock-Fault` header (per request) → scripted queue
(`POST /_admin/faults`, one entry per request, for deterministic chaos) → seeded random draws from
env probabilities. Same seed gives the same fault sequence.

The two faults that matter most **commit first, then lose the response**:
`error_after_commit` (returns 502) and `timeout_after_commit` (hangs past the client's timeout).
From the client's side, both look like "maybe it happened". A blind retry without a key refunds
twice (`test_error_after_commit_unkeyed_retry_double_refunds`); a keyed retry replays the
original (`test_error_after_commit_refund_exists_and_keyed_retry_replays`).

## 9. Webhooks

HMAC-SHA256 signature over the raw body (`X-Mockpay-Signature`), a unique event `id`, and a
monotonically increasing `sequence`. Delivery is at-least-once with retries. Faults:
`duplicate_webhook=n` delivers each event n times; `out_of_order_webhooks` holds a refund's events
and releases them newest-first once the terminal event exists, so `refund.processed` arrives
before `refund.created`. The receiver (A3) must dedupe by event id and must not let a stale event
move state backwards.

## 10. How the invariants are tested

- **Bypass tests** write raw SQL straight at Postgres and expect it to refuse.
- **A hypothesis state machine** runs random sequences of place/capture/ship/cancel/refund/settle/
  fail, plus over-refunds, tampering with the counter and unbalanced raw journals. After every step
  it checks that Postgres accepted *exactly* what a small reference model says is legal, and that
  all global invariants and account identities hold.
- Committed test data is always written through the service, so the shared test database stays
  valid and the global checks stay meaningful.

## 11. Tooling

pip + venv + pinned `requirements*.txt` (chosen over uv/Poetry for familiarity). CI on GitHub
Actions runs ruff and the full suite against a `postgres:15` service container on every push.

---

## Planned sections

- *(A3)* Why money can't be moved from the LLM layer
- *(A3)* Why exactly-once is impossible, and how idempotency + reconciliation gives effectively-once
- *(A3)* Why an `UNKNOWN` state exists
- *(A3)* Locking strategy for intents
- *(A4)* Why policy lives outside the prompt
