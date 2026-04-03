"""
tenaz — Tenacious retry. One file. Zero deps.

    @retry(max_attempts=5, backoff=0.5)
    def call_api(): ...

    @retry(retry_on_result=lambda r: r is None, total_timeout=30.0)
    async def poll(): ...

    for attempt in retrying(max_attempts=3):
        with attempt:
            fragile_operation()

See CHANGELOG.md for version history.
License: MIT
"""

from __future__ import annotations

import asyncio
import enum
import functools
import random
import time
import threading
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Optional,
    Sequence,
    Type,
    TypeVar,
    Union,
)

__version__ = "2.1.0"
__all__ = [
    "retry",
    "retrying",
    "async_retrying",
    "CircuitOpen",
    "RetryExhausted",
    "RetryTimeout",
]

T = TypeVar("T")


# ─── Exceptions ───────────────────────────────────────────────────────────────


class RetryExhausted(Exception):
    """All retry attempts consumed."""

    def __init__(self, last_exception: BaseException, attempts: int):
        self.last_exception = last_exception
        self.attempts = attempts
        super().__init__(f"Failed after {attempts} attempts: {last_exception}")


class RetryTimeout(Exception):
    """Total timeout exceeded across all retry attempts."""

    def __init__(self, last_exception: BaseException, elapsed: float, attempts: int):
        self.last_exception = last_exception
        self.elapsed = elapsed
        self.attempts = attempts
        super().__init__(
            f"Timeout after {elapsed:.1f}s / {attempts} attempts: {last_exception}"
        )


class CircuitOpen(Exception):
    """Circuit breaker is open — call rejected without execution."""

    def __init__(self, until: float):
        self.until = until
        remaining = max(0.0, until - time.monotonic())
        super().__init__(f"Circuit open. Retry in {remaining:.1f}s")


# ─── Circuit Breaker (internal) ───────────────────────────────────────────────


class _BreakerState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _CircuitBreaker:
    threshold: int  # failures before opening
    timeout: float  # seconds to stay open
    on_open: Optional[Callable] = None

    _state: _BreakerState = field(default=_BreakerState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _half_open_allowed: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._state == _BreakerState.CLOSED:
                return False
            if self._state == _BreakerState.HALF_OPEN:
                if self._half_open_allowed:
                    self._half_open_allowed = False
                    return False  # allow exactly one probe
                return True  # reject all others
            # OPEN — check if timeout expired
            if time.monotonic() - self._opened_at >= self.timeout:
                self._state = _BreakerState.HALF_OPEN
                self._half_open_allowed = False  # this caller is the probe
                return False
            return True

    def record_failure(self) -> None:
        with self._lock:
            if self._state == _BreakerState.HALF_OPEN:
                # Probe failed — reopen with fresh timer
                self._state = _BreakerState.OPEN
                self._opened_at = time.monotonic()
                self._failures = self.threshold
                return
            if self._state == _BreakerState.OPEN:
                return  # already open, no-op
            # CLOSED
            self._failures += 1
            if self._failures >= self.threshold:
                self._state = _BreakerState.OPEN
                self._opened_at = time.monotonic()
                if self.on_open:
                    self.on_open()

    def record_success(self) -> None:
        with self._lock:
            self._state = _BreakerState.CLOSED
            self._failures = 0


# ─── Backoff calculator ──────────────────────────────────────────────────────


def _calc_delay(
    attempt: int,
    backoff: float,
    max_delay: float,
    jitter: bool,
) -> float:
    delay = min(backoff * (2**attempt), max_delay)
    if jitter:
        delay = random.uniform(0, delay)  # Full jitter (AWS recommended)
    return delay


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _normalize_exc_types(
    val: Union[Type[BaseException], Sequence[Type[BaseException]]],
) -> tuple[Type[BaseException], ...]:
    if isinstance(val, type):
        return (val,)
    return tuple(val)


def _validate_common(
    max_attempts: int,
    backoff: float,
    max_delay: float,
) -> None:
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    if backoff < 0:
        raise ValueError(f"backoff must be >= 0, got {backoff}")
    if max_delay < 0:
        raise ValueError(f"max_delay must be >= 0, got {max_delay}")


# ─── The decorator ────────────────────────────────────────────────────────────


def retry(
    max_attempts: int = 3,
    backoff: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
    retry_on: Union[
        Type[BaseException], Sequence[Type[BaseException]]
    ] = (Exception,),
    abort_on: Sequence[Type[BaseException]] = (),
    retry_on_result: Optional[Callable[[Any], bool]] = None,
    on_retry: Optional[Callable[[int, BaseException | None, float], Any]] = None,
    on_fail: Optional[Callable[[BaseException, int], Any]] = None,
    total_timeout: float = 0.0,
    circuit_threshold: int = 0,
    circuit_timeout: float = 30.0,
    on_circuit_open: Optional[Callable] = None,
) -> Callable:
    """
    Universal retry decorator. Works on sync AND async functions.

    Args:
        max_attempts:       Total attempts (1 = no retry).
        backoff:            Base delay in seconds.
        max_delay:          Cap on single delay.
        jitter:             Full jitter (recommended for thundering herd).
        retry_on:           Exception types that trigger retry.
        abort_on:           Exception types that abort immediately (overrides retry_on).
        retry_on_result:    Predicate on return value — if True, treat as failure and retry.
        on_retry:           Hook(attempt, exception_or_None, delay) before each retry sleep.
        on_fail:            Hook(last_exception, total_attempts) when exhausted.
        total_timeout:      Max wall-clock seconds for all attempts combined (0 = unlimited).
        circuit_threshold:  Consecutive failures to trip breaker (0 = disabled).
        circuit_timeout:    Seconds breaker stays open before half-open test.
        on_circuit_open:    Hook called when breaker trips.
    """
    _validate_common(max_attempts, backoff, max_delay)
    if total_timeout < 0:
        raise ValueError(f"total_timeout must be >= 0, got {total_timeout}")
    if circuit_threshold < 0:
        raise ValueError(
            f"circuit_threshold must be >= 0, got {circuit_threshold}"
        )
    if circuit_threshold > 0 and circuit_timeout <= 0:
        raise ValueError(
            f"circuit_timeout must be > 0 when circuit is enabled, got {circuit_timeout}"
        )

    retry_on_t = _normalize_exc_types(retry_on)
    abort_on_t = tuple(abort_on)

    breaker: Optional[_CircuitBreaker] = None
    if circuit_threshold > 0:
        breaker = _CircuitBreaker(circuit_threshold, circuit_timeout, on_circuit_open)

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:

        # ── Async path ────────────────────────────────────────────────────
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> T:
                if breaker and breaker.is_open:
                    raise CircuitOpen(breaker._opened_at + breaker.timeout)

                start_time = time.monotonic()
                deadline = (
                    start_time + total_timeout if total_timeout > 0 else 0.0
                )
                last_exc: BaseException = Exception("unreachable")

                for attempt in range(max_attempts):
                    if deadline and time.monotonic() >= deadline:
                        raise RetryTimeout(
                            last_exc,
                            time.monotonic() - start_time,
                            attempt,
                        )
                    try:
                        result = await fn(*args, **kwargs)
                    except abort_on_t:
                        raise
                    except retry_on_t as exc:
                        last_exc = exc
                        if breaker:
                            breaker.record_failure()
                        if attempt < max_attempts - 1:
                            delay = _calc_delay(attempt, backoff, max_delay, jitter)
                            if deadline:
                                delay = min(
                                    delay, max(0, deadline - time.monotonic())
                                )
                            if on_retry:
                                on_retry(attempt + 1, exc, delay)
                            await asyncio.sleep(delay)
                        continue

                    # result obtained — check predicate outside try block
                    if retry_on_result and retry_on_result(result):
                        last_exc = ValueError(
                            f"retry_on_result rejected: {result!r}"
                        )
                        if breaker:
                            breaker.record_failure()
                        if attempt < max_attempts - 1:
                            delay = _calc_delay(
                                attempt, backoff, max_delay, jitter
                            )
                            if deadline:
                                delay = min(
                                    delay, max(0, deadline - time.monotonic())
                                )
                            if on_retry:
                                on_retry(attempt + 1, None, delay)
                            await asyncio.sleep(delay)
                        continue

                    if breaker:
                        breaker.record_success()
                    return result

                if on_fail:
                    on_fail(last_exc, max_attempts)
                raise RetryExhausted(last_exc, max_attempts)

            return async_wrapper  # type: ignore[return-value]

        # ── Sync path ─────────────────────────────────────────────────────
        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> T:
            if breaker and breaker.is_open:
                raise CircuitOpen(breaker._opened_at + breaker.timeout)

            start_time = time.monotonic()
            deadline = (
                start_time + total_timeout if total_timeout > 0 else 0.0
            )
            last_exc: BaseException = Exception("unreachable")

            for attempt in range(max_attempts):
                if deadline and time.monotonic() >= deadline:
                    raise RetryTimeout(
                        last_exc,
                        time.monotonic() - start_time,
                        attempt,
                    )

                try:
                    result = fn(*args, **kwargs)
                except abort_on_t:
                    raise
                except retry_on_t as exc:
                    last_exc = exc
                    if breaker:
                        breaker.record_failure()
                    if attempt < max_attempts - 1:
                        delay = _calc_delay(attempt, backoff, max_delay, jitter)
                        if deadline:
                            delay = min(
                                delay, max(0, deadline - time.monotonic())
                            )
                        if on_retry:
                            on_retry(attempt + 1, exc, delay)
                        time.sleep(delay)
                    continue

                # result obtained — check predicate outside try block
                if retry_on_result and retry_on_result(result):
                    last_exc = ValueError(
                        f"retry_on_result rejected: {result!r}"
                    )
                    if breaker:
                        breaker.record_failure()
                    if attempt < max_attempts - 1:
                        delay = _calc_delay(attempt, backoff, max_delay, jitter)
                        if deadline:
                            delay = min(
                                delay, max(0, deadline - time.monotonic())
                            )
                        if on_retry:
                            on_retry(attempt + 1, None, delay)
                        time.sleep(delay)
                    continue

                if breaker:
                    breaker.record_success()
                return result

            if on_fail:
                on_fail(last_exc, max_attempts)
            raise RetryExhausted(last_exc, max_attempts)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


# ─── Sync context manager ────────────────────────────────────────────────────


class _Attempt:
    """Per-attempt context manager that captures exceptions for retry."""

    def __init__(
        self,
        number: int,
        retry_on: tuple[Type[BaseException], ...],
        abort_on: tuple[Type[BaseException], ...],
        is_last: bool,
    ) -> None:
        self.number = number
        self.failed = False
        self.exception: BaseException | None = None
        self._retry_on = retry_on
        self._abort_on = abort_on
        self._is_last = is_last

    def __enter__(self) -> "_Attempt":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:  # type: ignore[type-arg]
        if exc_type is None:
            return False
        if isinstance(exc_val, self._abort_on):
            return False  # abort — propagate immediately
        if isinstance(exc_val, self._retry_on):
            self.failed = True
            self.exception = exc_val
            return True  # suppress — generator handles RetryExhausted
        return False  # non-retryable → propagate


class retrying:
    """
    Iterator + per-attempt context manager for inline retry.

    Usage:
        for attempt in retrying(max_attempts=3):
            with attempt:
                result = might_fail()
    """

    def __init__(
        self,
        max_attempts: int = 3,
        backoff: float = 1.0,
        max_delay: float = 60.0,
        jitter: bool = True,
        retry_on: Union[
            Type[BaseException], Sequence[Type[BaseException]]
        ] = (Exception,),
        abort_on: Sequence[Type[BaseException]] = (),
        total_timeout: float = 0.0,
        on_retry: Optional[Callable[[int, BaseException | None, float], Any]] = None,
    ) -> None:
        _validate_common(max_attempts, backoff, max_delay)
        if total_timeout < 0:
            raise ValueError(f"total_timeout must be >= 0, got {total_timeout}")
        self._max = max_attempts
        self._backoff = backoff
        self._max_delay = max_delay
        self._jitter = jitter
        self._retry_on = _normalize_exc_types(retry_on)
        self._abort_on = tuple(abort_on)
        self._total_timeout = total_timeout
        self._on_retry = on_retry

    def __iter__(self):
        start_time = time.monotonic()
        deadline = (
            start_time + self._total_timeout if self._total_timeout > 0 else 0.0
        )
        last_exc: BaseException | None = None

        for i in range(self._max):
            if deadline and time.monotonic() >= deadline:
                exc = last_exc or Exception("unreachable")
                raise RetryTimeout(exc, time.monotonic() - start_time, i)

            if i > 0:
                delay = _calc_delay(
                    i - 1, self._backoff, self._max_delay, self._jitter
                )
                if deadline:
                    delay = min(delay, max(0, deadline - time.monotonic()))
                if self._on_retry and last_exc is not None:
                    self._on_retry(i, last_exc, delay)
                time.sleep(delay)

            is_last = i == self._max - 1
            att = _Attempt(i + 1, self._retry_on, self._abort_on, is_last=is_last)
            yield att

            if not att.failed:
                return  # success → stop
            last_exc = att.exception

            # Last attempt failed — wrap in RetryExhausted
            if is_last and att.exception is not None:
                raise RetryExhausted(att.exception, self._max)


# ─── Async context manager ───────────────────────────────────────────────────


class async_retrying:
    """
    Async iterator + per-attempt context manager for inline retry.

    Usage:
        async for attempt in async_retrying(max_attempts=3):
            with attempt:
                result = await might_fail()
    """

    def __init__(
        self,
        max_attempts: int = 3,
        backoff: float = 1.0,
        max_delay: float = 60.0,
        jitter: bool = True,
        retry_on: Union[
            Type[BaseException], Sequence[Type[BaseException]]
        ] = (Exception,),
        abort_on: Sequence[Type[BaseException]] = (),
        total_timeout: float = 0.0,
        on_retry: Optional[Callable[[int, BaseException | None, float], Any]] = None,
    ) -> None:
        _validate_common(max_attempts, backoff, max_delay)
        if total_timeout < 0:
            raise ValueError(f"total_timeout must be >= 0, got {total_timeout}")
        self._max = max_attempts
        self._backoff = backoff
        self._max_delay = max_delay
        self._jitter = jitter
        self._retry_on = _normalize_exc_types(retry_on)
        self._abort_on = tuple(abort_on)
        self._total_timeout = total_timeout
        self._on_retry = on_retry

    def __aiter__(self):
        return self._generate()

    async def _generate(self):
        start_time = time.monotonic()
        deadline = (
            start_time + self._total_timeout if self._total_timeout > 0 else 0.0
        )
        last_exc: BaseException | None = None

        for i in range(self._max):
            if deadline and time.monotonic() >= deadline:
                exc = last_exc or Exception("unreachable")
                raise RetryTimeout(exc, time.monotonic() - start_time, i)

            if i > 0:
                delay = _calc_delay(
                    i - 1, self._backoff, self._max_delay, self._jitter
                )
                if deadline:
                    delay = min(delay, max(0, deadline - time.monotonic()))
                if self._on_retry and last_exc is not None:
                    self._on_retry(i, last_exc, delay)
                await asyncio.sleep(delay)

            is_last = i == self._max - 1
            att = _Attempt(i + 1, self._retry_on, self._abort_on, is_last=is_last)
            yield att

            if not att.failed:
                return
            last_exc = att.exception

            if is_last and att.exception is not None:
                raise RetryExhausted(att.exception, self._max)


# ─── Quick test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    counter = 0

    @retry(
        max_attempts=4,
        backoff=0.1,
        jitter=False,
        on_retry=lambda a, e, d: print(f"  -> attempt {a}, wait {d:.2f}s"),
    )
    def flaky():
        global counter
        counter += 1
        if counter < 3:
            raise ConnectionError(f"fail #{counter}")
        return "success"

    print("tenaz demo — decorator:")
    print(f"  result = {flaky()}")
    print(f"  total calls = {counter}")

    # Context manager demo
    print("\ntenaz demo — context manager:")
    cm_counter = 0
    for attempt in retrying(max_attempts=3, backoff=0.1, jitter=False):
        with attempt:
            cm_counter += 1
            print(f"  attempt {attempt.number}")
            if cm_counter < 2:
                raise ConnectionError(f"cm fail #{cm_counter}")
            print("  context manager success")

    # Retry on result demo
    poll_counter = 0

    @retry(
        max_attempts=5,
        backoff=0.05,
        jitter=False,
        retry_on_result=lambda r: r is None,
        on_retry=lambda a, e, d: print(f"  -> result retry {a}"),
    )
    def poll():
        global poll_counter
        poll_counter += 1
        return "ready" if poll_counter >= 3 else None

    print("\ntenaz demo — retry_on_result:")
    print(f"  result = {poll()}")
    print(f"  polls = {poll_counter}")
