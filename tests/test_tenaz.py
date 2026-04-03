"""Comprehensive tests for tenaz retry library."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tenaz import (
    retry,
    retrying,
    async_retrying,
    RetryExhausted,
    RetryTimeout,
    CircuitOpen,
    _CircuitBreaker,
    _BreakerState,
    _calc_delay,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Decorator — Sync
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetrySyncBasic:
    def test_succeeds_first_try(self):
        @retry(max_attempts=3, backoff=0)
        def ok():
            return 42

        assert ok() == 42

    def test_succeeds_after_failures(self):
        calls = 0

        @retry(max_attempts=4, backoff=0, jitter=False)
        def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError(f"fail {calls}")
            return "ok"

        assert flaky() == "ok"
        assert calls == 3

    def test_exhausted_raises(self):
        @retry(max_attempts=2, backoff=0, jitter=False)
        def always_fail():
            raise ValueError("boom")

        with pytest.raises(RetryExhausted) as exc_info:
            always_fail()

        assert exc_info.value.attempts == 2
        assert isinstance(exc_info.value.last_exception, ValueError)

    def test_no_retry_with_max_attempts_1(self):
        calls = 0

        @retry(max_attempts=1)
        def once():
            nonlocal calls
            calls += 1
            raise RuntimeError("fail")

        with pytest.raises(RetryExhausted):
            once()
        assert calls == 1


class TestRetrySyncAbortOn:
    def test_abort_on_stops_immediately(self):
        calls = 0

        @retry(max_attempts=5, backoff=0, abort_on=(TypeError,))
        def fn():
            nonlocal calls
            calls += 1
            raise TypeError("bad type")

        with pytest.raises(TypeError):
            fn()
        assert calls == 1

    def test_abort_on_overrides_retry_on(self):
        calls = 0

        @retry(
            max_attempts=5,
            backoff=0,
            retry_on=(Exception,),
            abort_on=(ValueError,),
        )
        def fn():
            nonlocal calls
            calls += 1
            raise ValueError("abort this")

        with pytest.raises(ValueError):
            fn()
        assert calls == 1


class TestRetrySyncRetryOnResult:
    def test_retries_on_rejected_result(self):
        calls = 0

        @retry(
            max_attempts=5,
            backoff=0,
            jitter=False,
            retry_on_result=lambda r: r is None,
        )
        def poll():
            nonlocal calls
            calls += 1
            return "ready" if calls >= 3 else None

        assert poll() == "ready"
        assert calls == 3

    def test_predicate_exception_propagates(self):
        """If retry_on_result predicate itself raises, it must NOT be swallowed."""

        def bad_predicate(result):
            raise RuntimeError("predicate bug")

        @retry(max_attempts=3, backoff=0, retry_on_result=bad_predicate)
        def fn():
            return "some value"

        with pytest.raises(RuntimeError, match="predicate bug"):
            fn()


class TestRetrySyncTimeout:
    def test_total_timeout_raises(self):
        calls = 0

        @retry(max_attempts=100, backoff=0.05, jitter=False, total_timeout=0.15)
        def slow_fail():
            nonlocal calls
            calls += 1
            raise ConnectionError("down")

        with pytest.raises(RetryTimeout) as exc_info:
            slow_fail()

        assert exc_info.value.attempts > 0
        assert exc_info.value.elapsed > 0
        # elapsed should reflect real time, not the configured budget
        assert exc_info.value.elapsed <= 0.5  # generous upper bound

    def test_elapsed_is_real_time(self):
        @retry(max_attempts=100, backoff=0.02, jitter=False, total_timeout=0.1)
        def fn():
            raise ValueError("fail")

        with pytest.raises(RetryTimeout) as exc_info:
            fn()

        # elapsed should be close to 0.1, not exactly 0.1
        assert 0.05 <= exc_info.value.elapsed <= 0.5


class TestRetrySyncHooks:
    def test_on_retry_called(self):
        hook_calls = []

        @retry(
            max_attempts=3,
            backoff=0,
            jitter=False,
            on_retry=lambda a, e, d: hook_calls.append((a, type(e).__name__)),
        )
        def fn():
            raise ValueError("fail")

        with pytest.raises(RetryExhausted):
            fn()

        assert len(hook_calls) == 2  # called before retry 2 and 3
        assert hook_calls[0] == (1, "ValueError")
        assert hook_calls[1] == (2, "ValueError")

    def test_on_fail_called(self):
        fail_hook = MagicMock()

        @retry(max_attempts=2, backoff=0, on_fail=fail_hook)
        def fn():
            raise ValueError("boom")

        with pytest.raises(RetryExhausted):
            fn()

        fail_hook.assert_called_once()
        args = fail_hook.call_args[0]
        assert isinstance(args[0], ValueError)
        assert args[1] == 2

    def test_on_retry_for_result_retry(self):
        hook_calls = []
        calls = 0

        @retry(
            max_attempts=3,
            backoff=0,
            jitter=False,
            retry_on_result=lambda r: r is None,
            on_retry=lambda a, e, d: hook_calls.append((a, e)),
        )
        def fn():
            nonlocal calls
            calls += 1
            return None if calls < 3 else "done"

        assert fn() == "done"
        assert len(hook_calls) == 2
        # exception arg is None for result-based retries
        assert all(e is None for _, e in hook_calls)


# ═══════════════════════════════════════════════════════════════════════════════
# Decorator — Async
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetryAsync:
    @pytest.mark.asyncio
    async def test_succeeds_after_failures(self):
        calls = 0

        @retry(max_attempts=4, backoff=0, jitter=False)
        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError(f"fail {calls}")
            return "ok"

        assert await flaky() == "ok"
        assert calls == 3

    @pytest.mark.asyncio
    async def test_exhausted_raises(self):
        @retry(max_attempts=2, backoff=0)
        async def always_fail():
            raise ValueError("boom")

        with pytest.raises(RetryExhausted) as exc_info:
            await always_fail()
        assert exc_info.value.attempts == 2

    @pytest.mark.asyncio
    async def test_abort_on(self):
        calls = 0

        @retry(max_attempts=5, backoff=0, abort_on=(TypeError,))
        async def fn():
            nonlocal calls
            calls += 1
            raise TypeError("abort")

        with pytest.raises(TypeError):
            await fn()
        assert calls == 1

    @pytest.mark.asyncio
    async def test_retry_on_result(self):
        calls = 0

        @retry(
            max_attempts=5,
            backoff=0,
            jitter=False,
            retry_on_result=lambda r: r is None,
        )
        async def poll():
            nonlocal calls
            calls += 1
            return "done" if calls >= 3 else None

        assert await poll() == "done"
        assert calls == 3

    @pytest.mark.asyncio
    async def test_predicate_exception_propagates(self):
        def bad_predicate(result):
            raise RuntimeError("async predicate bug")

        @retry(max_attempts=3, backoff=0, retry_on_result=bad_predicate)
        async def fn():
            return "value"

        with pytest.raises(RuntimeError, match="async predicate bug"):
            await fn()

    @pytest.mark.asyncio
    async def test_total_timeout(self):
        @retry(max_attempts=100, backoff=0.02, jitter=False, total_timeout=0.1)
        async def fn():
            raise ValueError("fail")

        with pytest.raises(RetryTimeout) as exc_info:
            await fn()
        assert 0.05 <= exc_info.value.elapsed <= 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# Circuit Breaker
# ═══════════════════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    def test_opens_after_threshold(self):
        cb = _CircuitBreaker(threshold=3, timeout=10.0)
        assert not cb.is_open

        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open

        cb.record_failure()
        assert cb.is_open

    def test_success_resets(self):
        cb = _CircuitBreaker(threshold=2, timeout=10.0)
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert not cb.is_open  # counter was reset

    def test_on_open_fires_once(self):
        hook = MagicMock()
        cb = _CircuitBreaker(threshold=2, timeout=10.0, on_open=hook)

        cb.record_failure()
        cb.record_failure()  # opens — hook fires
        cb.record_failure()  # already open — hook does NOT fire
        cb.record_failure()

        hook.assert_called_once()

    def test_half_open_allows_one_probe(self):
        cb = _CircuitBreaker(threshold=1, timeout=0.05)
        cb.record_failure()  # opens
        assert cb.is_open

        time.sleep(0.06)  # wait for timeout

        # First caller gets through (half-open probe)
        assert not cb.is_open
        # Second caller is rejected
        assert cb.is_open

    def test_half_open_success_closes(self):
        cb = _CircuitBreaker(threshold=1, timeout=0.05)
        cb.record_failure()
        time.sleep(0.06)

        assert not cb.is_open  # half-open, probe allowed
        cb.record_success()
        assert cb._state == _BreakerState.CLOSED
        assert not cb.is_open

    def test_half_open_failure_reopens(self):
        cb = _CircuitBreaker(threshold=1, timeout=0.05)
        cb.record_failure()
        time.sleep(0.06)

        assert not cb.is_open  # probe allowed
        cb.record_failure()  # probe failed
        assert cb._state == _BreakerState.OPEN
        assert cb.is_open

    def test_record_failure_while_open_is_noop(self):
        cb = _CircuitBreaker(threshold=2, timeout=10.0)
        cb.record_failure()
        cb.record_failure()  # opens
        opened_at = cb._opened_at

        cb.record_failure()  # should be no-op
        assert cb._opened_at == opened_at  # not updated

    def test_concurrent_half_open_only_one_probe(self):
        """Multiple threads checking is_open after timeout: only one should get through."""
        cb = _CircuitBreaker(threshold=1, timeout=0.05)
        cb.record_failure()
        time.sleep(0.06)

        results = []
        barrier = threading.Barrier(5, timeout=2)

        def check():
            barrier.wait()
            results.append(not cb.is_open)  # True = got through

        threads = [threading.Thread(target=check) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1  # exactly one probe

    def test_decorator_circuit_integration(self):
        calls = 0

        @retry(
            max_attempts=1,
            backoff=0,
            circuit_threshold=3,
            circuit_timeout=0.05,
        )
        def fn():
            nonlocal calls
            calls += 1
            raise ValueError("fail")

        # Trip the breaker
        for _ in range(3):
            with pytest.raises(RetryExhausted):
                fn()
        assert calls == 3

        # Now circuit should be open
        with pytest.raises(CircuitOpen):
            fn()
        assert calls == 3  # not called


# ═══════════════════════════════════════════════════════════════════════════════
# Backoff
# ═══════════════════════════════════════════════════════════════════════════════


class TestBackoff:
    def test_exponential_no_jitter(self):
        assert _calc_delay(0, 1.0, 60.0, False) == 1.0
        assert _calc_delay(1, 1.0, 60.0, False) == 2.0
        assert _calc_delay(2, 1.0, 60.0, False) == 4.0
        assert _calc_delay(3, 1.0, 60.0, False) == 8.0

    def test_max_delay_caps(self):
        assert _calc_delay(10, 1.0, 5.0, False) == 5.0

    def test_jitter_within_range(self):
        for _ in range(100):
            delay = _calc_delay(2, 1.0, 60.0, True)
            assert 0 <= delay <= 4.0

    def test_zero_backoff(self):
        assert _calc_delay(0, 0, 60.0, False) == 0
        assert _calc_delay(5, 0, 60.0, False) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Context Manager — Sync
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetryingSync:
    def test_basic_retry(self):
        calls = 0
        for attempt in retrying(max_attempts=3, backoff=0, jitter=False):
            with attempt:
                calls += 1
                if calls < 2:
                    raise ConnectionError("fail")

        assert calls == 2

    def test_exhausted_raises_retry_exhausted(self):
        with pytest.raises(RetryExhausted) as exc_info:
            for attempt in retrying(max_attempts=2, backoff=0, jitter=False):
                with attempt:
                    raise ValueError("always fail")

        assert exc_info.value.attempts == 2
        assert isinstance(exc_info.value.last_exception, ValueError)

    def test_abort_on(self):
        calls = 0
        with pytest.raises(TypeError):
            for attempt in retrying(
                max_attempts=5, backoff=0, abort_on=(TypeError,)
            ):
                with attempt:
                    calls += 1
                    raise TypeError("abort")
        assert calls == 1

    def test_total_timeout(self):
        with pytest.raises(RetryTimeout):
            for attempt in retrying(
                max_attempts=100, backoff=0.05, jitter=False, total_timeout=0.1
            ):
                with attempt:
                    raise ValueError("fail")

    def test_on_retry_hook(self):
        hook_calls = []
        calls = 0

        for attempt in retrying(
            max_attempts=3,
            backoff=0,
            jitter=False,
            on_retry=lambda a, e, d: hook_calls.append(a),
        ):
            with attempt:
                calls += 1
                if calls < 3:
                    raise ValueError("fail")

        assert len(hook_calls) == 2

    def test_success_no_extra_iterations(self):
        iterations = 0
        for attempt in retrying(max_attempts=5, backoff=0):
            with attempt:
                iterations += 1
        assert iterations == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Context Manager — Async
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetryingAsync:
    @pytest.mark.asyncio
    async def test_basic_retry(self):
        calls = 0
        async for attempt in async_retrying(max_attempts=3, backoff=0, jitter=False):
            with attempt:
                calls += 1
                if calls < 2:
                    raise ConnectionError("fail")

        assert calls == 2

    @pytest.mark.asyncio
    async def test_exhausted_raises(self):
        with pytest.raises(RetryExhausted):
            async for attempt in async_retrying(
                max_attempts=2, backoff=0, jitter=False
            ):
                with attempt:
                    raise ValueError("fail")

    @pytest.mark.asyncio
    async def test_total_timeout(self):
        with pytest.raises(RetryTimeout):
            async for attempt in async_retrying(
                max_attempts=100, backoff=0.05, jitter=False, total_timeout=0.1
            ):
                with attempt:
                    raise ValueError("fail")


# ═══════════════════════════════════════════════════════════════════════════════
# Parameter Validation
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidation:
    def test_max_attempts_zero(self):
        with pytest.raises(ValueError, match="max_attempts"):
            retry(max_attempts=0)

    def test_max_attempts_negative(self):
        with pytest.raises(ValueError, match="max_attempts"):
            retry(max_attempts=-1)

    def test_backoff_negative(self):
        with pytest.raises(ValueError, match="backoff"):
            retry(backoff=-1)

    def test_max_delay_negative(self):
        with pytest.raises(ValueError, match="max_delay"):
            retry(max_delay=-1)

    def test_total_timeout_negative(self):
        with pytest.raises(ValueError, match="total_timeout"):
            retry(total_timeout=-1)

    def test_circuit_threshold_negative(self):
        with pytest.raises(ValueError, match="circuit_threshold"):
            retry(circuit_threshold=-1)

    def test_circuit_timeout_zero_with_circuit_enabled(self):
        with pytest.raises(ValueError, match="circuit_timeout"):
            retry(circuit_threshold=5, circuit_timeout=0)

    def test_retrying_validation(self):
        with pytest.raises(ValueError, match="max_attempts"):
            retrying(max_attempts=0)
        with pytest.raises(ValueError, match="backoff"):
            retrying(backoff=-1)
        with pytest.raises(ValueError, match="total_timeout"):
            retrying(total_timeout=-1)

    def test_async_retrying_validation(self):
        with pytest.raises(ValueError, match="max_attempts"):
            async_retrying(max_attempts=0)
        with pytest.raises(ValueError, match="backoff"):
            async_retrying(backoff=-1)


# ═══════════════════════════════════════════════════════════════════════════════
# Edge Cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_preserves_function_metadata(self):
        @retry(max_attempts=2)
        def my_function():
            """My docstring."""
            pass

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."

    def test_non_retryable_exception_propagates(self):
        calls = 0

        @retry(max_attempts=5, backoff=0, retry_on=(ConnectionError,))
        def fn():
            nonlocal calls
            calls += 1
            raise TypeError("not retryable")

        with pytest.raises(TypeError):
            fn()
        assert calls == 1

    def test_single_exception_type_not_tuple(self):
        calls = 0

        @retry(max_attempts=3, backoff=0, retry_on=ValueError)
        def fn():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ValueError("retry this")
            return "ok"

        assert fn() == "ok"
        assert calls == 2
