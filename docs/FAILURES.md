# Failures log

An honest record of real bugs, unsafe behaviour and surprising results found while building
Tollgate. Each entry: what happened, root cause, fix (with its `fix:` commit), and the test that now
covers it. Entries are added only when something actually happened. None are invented.

## 2026-11-01 · A clean install failed

**What happened.** `pip install -r requirements-dev.txt` failed on a fresh machine: `litellm==1.55.10`
requires `httpx<0.28`, but `requirements.txt` pinned `httpx==0.28.1`. CI could not install.
**Root cause.** The two pins were added at different times and never installed together from
scratch.
**Fix.** Pin `httpx==0.27.2` (`fix: pin httpx 0.27.2 for litellm and fix three stale tests`).
**Covered by.** CI installs from scratch on every push.

## 2026-11-01 · The naive double-refund test could never pass

**What happened.** `test_naive_retry_after_lost_response_refunds_twice` expected a duplicate but got
none.
**Root cause.** It used a *full* refund. mockpay, like real providers, refuses refunds beyond the
captured amount, so the second full refund was rejected. The test was wrong, not the naive
backend: duplicates the provider accepts are partial ones (DESIGN.md §6).
**Fix.** Use the partial-refund scenario `legit-partial-011` (same commit as above).
**Covered by.** The test itself, and the chaos suite, which uses partial refunds on purpose.

## 2026-11-01 · Two more stale tests

**What happened.** `test_schemas_cover_all_tools_and_amount_is_integer` failed because a tool
*description* contains the word "customer"; `test_capture_after_cancel_rejected` expected one
error message but the amount guard fires first with another.
**Fix.** The schema test now checks that no tool *parameter* is about the customer (the property it
was meant to protect); the cancel test asserts both refusals with the message each raises (same
commit as above).

