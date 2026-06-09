# Changelog

## 2.2.0 — 2026-06-08

### Packaging

- The published **wheel** and **source distribution** now ship only the intended
  files. Previously the wheel bundled the whole repo (including a top-level
  `tests` package) into `site-packages`, and the sdist could sweep in stray local
  files; both are now restricted to explicit allowlists.

### Bug Fixes

- **`on_circuit_open` hook isolation**: a raising `on_circuit_open` hook no longer
  propagates out of the call or masks the real retry error — it is now suppressed
  like `on_retry` / `on_fail`.
- **`total_timeout` no longer surfaces an internal sentinel**: a very small
  `total_timeout` previously raised `RetryTimeout` with a placeholder exception
  and `attempts=0`. The first attempt now always runs (at-least-once), so
  `RetryTimeout` always carries a real `last_exception`.

### Features

- **`abort_on` accepts a bare exception type** (e.g. `abort_on=ValueError`), not
  just a sequence — symmetric with `retry_on`.
- **Typed decorator**: `@retry(...)` now preserves the decorated function's full
  signature (parameters and return type, sync and async) for type checkers, via
  `ParamSpec`. No runtime change.

### Documentation

- Clarified that the circuit breaker gates *new calls*, not the in-flight retry
  loop (keep `circuit_threshold >= max_attempts` to trip only between calls).
- Noted that `retry_on_result` embeds a truncated repr of a rejected value in the
  `RetryExhausted` message.

### Infrastructure

- Added CI (GitHub Actions): pytest on Python 3.10–3.13 plus a `mypy` type-check,
  with third-party actions pinned to commit SHAs.

## 2.1.0 — 2026-04-03

### Bug Fixes

- **Circuit breaker half-open race condition**: The previous implementation reset
  `_failures = 0` on timeout expiry, allowing multiple concurrent callers through
  when only one probe should be allowed. Now uses an explicit three-state machine
  (`CLOSED` / `OPEN` / `HALF_OPEN`): the transition into `HALF_OPEN` happens under
  the breaker lock, so exactly one probe call passes in half-open state.

- **`record_failure()` re-triggering**: Previously, every failure past the threshold
  re-fired `on_circuit_open` and reset `_opened_at`, which could extend the open
  window indefinitely and spam the hook. Now `on_open` fires only on the
  `CLOSED → OPEN` transition. Failures while already `OPEN` are no-ops.

- **`RetryTimeout.elapsed` reported configured value, not real time**: The `elapsed`
  attribute was set to `total_timeout` (the configured budget) instead of the actual
  measured elapsed time. Now uses `time.monotonic()` to compute real elapsed.

- **`retry_on_result` predicate exceptions swallowed**: If the user's predicate
  raised an exception, it was caught by the broad `except retry_on_t` and treated
  as a retryable failure. The predicate is now evaluated outside the try block so
  its exceptions propagate immediately.

### New Features

- **Parameter validation**: `retry()`, `retrying()`, and `async_retrying()` now
  validate their arguments at construction time. Invalid values (`max_attempts < 1`,
  `backoff < 0`, `max_delay < 0`, `total_timeout < 0`, `circuit_timeout <= 0` when
  circuit is enabled) raise `ValueError` with clear messages.

- **Context manager feature parity**: `retrying` and `async_retrying` now support
  `total_timeout`, `abort_on`, and `on_retry` parameters. The final exception is
  wrapped in `RetryExhausted` instead of propagating raw.

### Breaking Changes

- Version bumped to 2.1.0.
- `retrying` / `async_retrying` now raise `RetryExhausted` on final failure instead
  of the raw exception. Code that catches the raw exception type will need to catch
  `RetryExhausted` or access `.last_exception`.

## 2.0.0

Initial public release.
