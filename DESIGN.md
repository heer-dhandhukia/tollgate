# Tollgate — design decisions

Each section lists the options considered, what was chosen, and the trade-off.

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
don't expose keys for lookup, so the receipt is the realistic handle. The executor sets
`receipt` to the intent id (§19).

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
before `refund.created`. The receiver dedupes by event id and never lets a stale event
move state backwards (§24).

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

## 12. What "naive" means, exactly

The baseline must be a fair, recognisable first implementation, not a straw man. Naive mode uses
the **same** model, prompt structure, tools, tool-argument validation and authenticated session as
guarded mode. It differs in exactly these ways (`src/tollgate/naive/backend.py`):

1. `issue_refund` / `cancel_order` call the provider **directly, with no Idempotency-Key**.
2. The policy exists **only in the system prompt** (rendered from `config/policy.v1.yaml`, the same
   file the guarded policy engine enforces, §22). No code checks amounts, windows or limits.
3. Refund/cancel look orders up by id without re-checking ownership. `lookup_order` is scoped to
   the signed-in customer: identity comes from the session in both modes, per the spec.
4. External call first, ledger second, best effort. When the ledger's own triggers refuse (e.g.
   cancelling a shipped order), the provider has **already** moved the money. That shows up as
   `ledger_rejected_after_provider` / `cancel_rejected` events. The database invariants protect our
   books, but they cannot un-send money another system already sent.
5. Tool errors and timeouts are returned to the model, so retrying is the model's call.

Point 4 is deliberate and worth defending in an interview: DB constraints are necessary but not
sufficient once side effects leave your database.

## 13. The oracle is per-scenario and independent of the policy engine

**Options.** Derive the expected outcome from the policy config · write it per scenario.
**Chosen.** Per scenario (`expect: {outcome, order, max_refund_paise, legit}`).
**Why.** Grading guarded mode with its own rules is circular: a bug in the policy engine would
also be in the grader. Hand-written expectations are auditable line by line.
**Cost.** More YAML, and the expectations can be wrong. They are reviewed, and the scenario
loader enforces consistency (deny/escalate ⇒ `max_refund_paise = 0`; legit refunds allow > 0).

## 14. Metrics are computed from what the provider did

The scorer reads **mockpay's refund records** (money that actually moved), never the agent's
claims or our ledger. Definitions (`src/tollgate/eval/score.py`):

- **Duplicate execution:** an extra refund on the same order with the same amount in one
  conversation. Scenarios allow at most one refund per order. Count and ₹.
- **Policy violation:** money moved above the scenario's allowance (`max_refund_paise` on the
  expected order, 0 on any other order). Duplicate amounts are excluded from the excess, so a
  rupee is never counted in both metrics.
- **False denial:** a `legit` scenario where the customer did not get what policy allows (refund:
  nothing moved on that order; cancel: the order isn't cancelled in the ledger).
- **Escalation rate**, **latency p50/p95** (recorded LLM latency + measured tool time per
  conversation), **cost per conversation** (recorded tokens × the dated prices in
  `config/models.yaml`).

Each metric is computed per run, then reported as mean ± sample std over runs, with n.

## 15. Deterministic, replayable evals

- **Fixed clock.** Prompts say "today is 2026-10-01" and tool outputs say "placed 6 days ago"
  relative to `Settings.eval_today`, never the wall clock. Otherwise every day's prompts differ and
  the cache never hits.
- **Stable tool outputs.** No database ids, provider ids, timestamps or conversation ids reach the
  model. A test checks that two different sessions render the same system prompt, and the runner
  test checks that a re-run is served 100% from cache.
- **Content-addressed cache, committed.** Key = sha256(model, messages, tools, params, prompt
  version, run index). Entries store the original latency, tokens and cost, so replays reproduce
  the reported numbers exactly, with no API key.
- **Three runs are three samples.** The run index is part of the key, and the agent runs at the
  model's default temperature, so runs 1–3 are independent draws, each individually replayable.
  The reported spread is real model variability, not noise from the harness.

## 16. Scenario isolation: one cloned database pair per attempt

**Options.** One shared DB with unique refs · truncate between scenarios · clone a template per
scenario.
**Chosen.** Clone (`CREATE DATABASE … TEMPLATE …`) a migrated ledger template and a mockpay
template for every scenario attempt.
**Why.** The ledger is append-only, so truncation is impossible by design. Shared state would
let one scenario's refunds affect another's limits (e.g. the per-customer daily cap). Clones are
cheap, run concurrently, and a crashed attempt is retried by dropping and re-cloning. mockpay runs
in-process over an ASGI transport, so an eval needs only Postgres.

## 17. Free-tier safety and resumability

A semaphore caps concurrent LLM calls (default 2); 429/5xx/connection errors back off
exponentially with jitter and honour `Retry-After`. Every finished scenario writes its own result
file and every response is cached, so a run killed by a daily token limit resumes with the same
command. On exhausted rate-limit retries the runner stops starting new scenarios instead of
failing them all. The full naive baseline (66 scenarios × 3 runs, a few calls each) likely
exceeds one day of Groq's free tier for the 70B model, so expect several resumed runs, or use a
paid key via `TOLLGATE_AGENT_MODEL`.

The eval workflow uploads results and cache as an artifact and **never commits**. Heer fetches,
reviews and commits them, which keeps authorship and provenance clear.

---

## 18. Money can't be moved from the LLM layer

**Options.** (a) Let the refund tool call the provider and add checks around it. (b) Make the tools
*request* an action and give one component, the executor, the only provider client that can move
money.
**Chosen.** (b). In guarded mode `issue_refund` and `cancel_order` call
`Executor.request_refund` / `request_cancel` (`src/tollgate/guarded/`). Nothing else in guarded
mode calls `create_refund`.
**Why.** Checks wrapped around many call sites drift apart; one choke point can be reasoned about
and tested once. The same executor serves the agent, the approval CLI and reconciliation.
**Cost.** Every money path pays the executor's latency (a row lock, a transaction, an audit row).

## 19. Exactly-once is impossible; effectively-once is the goal

A network call can succeed and still look like a failure (`error_after_commit`,
`timeout_after_commit`). No client can tell "never happened" from "happened, reply lost" without
asking again. So:

1. Every provider call carries `Idempotency-Key` = intent id and `receipt` = intent id.
2. Timeouts, 5xx, 409 and 429 are retried (up to 3 attempts) with the **same** key. A retry after
   a lost reply gets the stored response replayed: one refund.
3. When attempts run out, the refund is looked up by receipt. Found → `succeeded`. Not found →
   `failed`, and the ledger reservation is released.
4. If even the lookup fails, the intent becomes **`unknown`**: never retried automatically, an
   escalation is opened, and `Executor.reconcile()` resolves it later.

`unknown` exists because guessing in either direction is wrong: "succeeded" may report money that
never moved, "failed" invites a retry that pays twice. **Cost:** a customer may wait for a human.
**Known gap:** "not found by receipt" is treated as "never happened". A provider that was still
processing the request could commit it later; a production system would wait out the provider's
in-flight window first.

## 20. The intent id is a fingerprint of the request

`int_` + sha256(conversation, kind, order, amount)[:24]. The same request in the same
conversation maps to the same intent, so a model that repeats itself (or two concurrent calls)
gets the first result back (`already_done` / `already_in_progress`) instead of a second refund.
**Cost.** Two genuinely separate, identical refunds on one order in one conversation merge into
one; the agent has to ask for the combined amount. We accept that: it fails safe.

## 21. Locking: customer, then order, then payment

Each decision locks the customer row, then the order, then its payment (`SELECT … FOR UPDATE`,
always in that order, so concurrent requests can't deadlock). Locking the customer makes the daily
cap, which spans all of a customer's orders, race-free. Two more backstops:

- a partial unique index allows at most one `executing`/`unknown` intent per order;
- the ledger's own `refunded ≤ captured` check refuses any reservation that slipped through.

`test_concurrent_identical_requests_pay_once` and
`test_concurrent_different_requests_never_exceed_what_was_paid` fire requests in parallel.
**Cost.** One customer's money actions are serialised. That's the contention we want.

## 22. Policy lives outside the prompt

`src/tollgate/guarded/policy.py` evaluates `config/policy.v1.yaml`, the same file naive mode puts in
its prompt. Rules run in a fixed order (ownership, cancel-after-shipping, nothing captured,
refundable amount, window, per-order cap, daily cap, approval threshold) and the first failure
decides. It is a pure function of facts read under the lock, so every rule is unit-tested without
a database.

- Denials about an amount return `max_allowed_paise`, so the agent can offer what policy allows
  rather than a flat no (this is what keeps false denials down).
- Cancellations of paid orders return money through the same rules: there is one way to return
  money (§5).
- An approval re-runs the policy minus the approval rule: limits may have been used up while the
  request waited.

## 23. Approval queue and audit log

Refunds above the threshold become `awaiting_approval` intents and open an escalation. A named
person approves or rejects with `python -m tollgate.approvals`; the decision is recorded as
`human:<name>`. Every step (agent request, policy decision, reservation, provider outcome,
reconciliation, human decision, webhook) is a row in `audit_log`, which Postgres makes
append-only like the ledger. Intent status transitions are one-way and enforced by a trigger.

## 24. Webhooks

The receiver verifies HMAC-SHA256 over the raw body, inserts the event id with
`ON CONFLICT DO NOTHING` (a duplicate does nothing), and maps events onto ledger operations that
are themselves idempotent. Order is not trusted: `refund.created` after `refund.processed` is
recorded as `ignored_stale`. A conflicting event (e.g. `processed` for a refund already failed) is
recorded as `conflict` in the audit log and left for a human, never applied backwards.

## 25. What "guarded" changes, exactly

Guarded mode uses the same model, tools, tool schemas, argument validation and session as naive
mode (§12). It differs in the five points of §12 and in one prompt paragraph telling the model that
the tools enforce the policy and may answer "denied", "waiting for approval" or "already done".
Without that paragraph the model has no way to interpret those answers; it does not weaken the
naive baseline.

Planned next: a prompt-injection suite (product descriptions are already an injection surface,
`products.description`), multi-turn scenarios, and the naive-vs-guarded results.
