# Changelog

## 2.1.0 — 2025-XX-XX

### Bug Fixes

- **Circuit breaker half-open race condition**: The previous implementation reset
  `_failures = 0` on timeout expiry, allowing multiple concurrent callers through
  when only one probe should be allowed. Now uses an explicit three-state machine
  (`CLOSED` / `OPEN` / `HALF_OPEN`) with an atomic `_half_open_allowed` flag so
  exactly one probe call passes in half-open state.

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
