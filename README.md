 tenaz

[![PyPI version](https://img.shields.io/pypi/v/tenaz)](https://pypi.org/project/tenaz/)
[![Python versions](https://img.shields.io/pypi/pyversions/tenaz)](https://pypi.org/project/tenaz/)
[![License](https://img.shields.io/pypi/l/tenaz)](https://github.com/ElEscribanoSilente/tenaz/blob/main/LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/ElEscribanoSilente/tenaz/ci.yml?label=tests)](https://github.com/ElEscribanoSilente/tenaz/actions)
[![Downloads](https://img.shields.io/pypi/dm/tenaz)](https://pypistats.org/packages/tenaz)

**Production-grade retry with built-in circuit breaker for Python.**
*Exponential backoff · Full jitter · Sync + Async · ~565 lines · Zero dependencies.*

```python
from tenaz import retry

@retry(max_attempts=5, backoff=0.5, jitter=True)
def call_api():
    return requests.get("https://api.example.com/data")
```

One decorator. One file. Everything you need to make failing calls resilient — without pulling in a framework to do it.

---

## Why tenaz?

Every retry library makes the same tradeoffs wrong:

| | tenaz | tenacity | backoff | retrying |
|---|:---:|:---:|:---:|:---:|
| Exponential backoff | ✓ | ✓ | ✓ | ✓ |
| Full jitter (AWS-style) | ✓ | ✓ | ✓ | ✗ |
| Circuit breaker built-in | ✓ | ✗ | ✗ | ✗ |
| Sync + async ONE decorator | ✓ | ✓ | partial | ✗ |
| Per-exception strategy | ✓ | ✓ | ✓ | ✗ |
| Retry on return value | ✓ | ✓ | ✗ | ✗ |
| Total timeout cap | ✓ | ✓ | ✗ | ✗ |
| Context manager mode | ✓ | ✗ | ✗ | ✗ |
| Async context manager | ✓ | ✗ | ✗ | ✗ |
| Lifecycle hooks | ✓ | partial | partial | ✗ |
| Dependencies | **0** | 0 | 0 | 0 |
| Lines of code | **~565** | 800+ | 400+ | 300+ |
| Maintained (2025+) | ✓ | ✓ | ✓ | **✗ abandoned** |

**tenacity** is the industry standard — and it's good. But its API is confusing (`retry_if_exception_type` vs `retry_if_not_exception_type` vs `retry_if_exception_message`...), it has no circuit breaker, and you'll need `pybreaker` or `circuitbreaker` as a separate package.

**tenaz** gives you retry + circuit breaker in one decorator with an API you can memorize in 60 seconds.

---

## Install

```bash
pip install tenaz
```

Or just copy `tenaz.py` into your project. It's one file.

**Requires:** Python 3.10+  
**Dependencies:** None (stdlib only)

---

## Quick Start

### Basic retry

```python
from tenaz import retry

@retry(max_attempts=3, backoff=1.0)
def fetch_data():
    return requests.get("https://api.example.com")
```

Fails → waits ~1s → retries → waits ~2s → retries → gives up.

### With jitter (default, recommended)

```python
@retry(max_attempts=5, backoff=0.5, jitter=True)
def fetch_data():
    ...
```

Full jitter randomizes delay between `[0, calculated_delay]`. This is the [AWS-recommended strategy](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) to prevent thundering herd.

### Async — same decorator

```python
@retry(max_attempts=3, backoff=0.5)
async def async_fetch():
    async with aiohttp.ClientSession() as session:
        return await session.get("https://api.example.com")
```

No separate import. No `@async_retry`. The same `@retry` detects `async def` and does the right thing.

---

## Core Features

### Selective retry by exception type

```python
@retry(
    retry_on=(ConnectionError, TimeoutError),   # Retry these
    abort_on=(AuthenticationError, ValueError),  # Never retry these
)
def smart_call():
    ...
```

`abort_on` overrides `retry_on` — if an `AuthenticationError` happens, it raises immediately. No retries. No wasted time.

### Retry on return value

```python
@retry(
    max_attempts=10,
    backoff=2.0,
    retry_on_result=lambda r: r is None,  # Retry while result is None
)
def poll_job_status(job_id: str):
    return api.get_status(job_id)  # Returns None until job completes
```

When `retry_on_result` returns `True`, the call is treated as a failure and retried. The `on_retry` hook receives `None` as the exception argument for result-based retries.

### Total timeout

```python
@retry(max_attempts=100, backoff=1.0, total_timeout=30.0)
def bounded_retry():
    return flaky_service.call()
```

Caps the total wall-clock time across all attempts. Raises `RetryTimeout` when exceeded. Sleep delays are automatically clamped to the remaining budget — no sleeping past the deadline.

```python
from tenaz import RetryTimeout

try:
    bounded_retry()
except RetryTimeout as e:
    print(e.elapsed)         # 30.1
    print(e.attempts)        # 12
    print(e.last_exception)  # ConnectionError(...)
```

### Circuit breaker

```python
@retry(
    max_attempts=3,
    circuit_threshold=5,    # Open after 5 consecutive failures
    circuit_timeout=30.0,   # Stay open for 30 seconds
)
def db_query():
    return database.execute("SELECT ...")
```

After 5 consecutive failures across any number of calls, the circuit **opens**. All subsequent calls raise `CircuitOpen` **instantly** — no execution, no waiting. After 30 seconds, the circuit enters **half-open** state and allows one test call through.

This prevents cascading failures when a downstream service is dead. No need for a separate `pybreaker` package.

```python
from tenaz import CircuitOpen

try:
    db_query()
except CircuitOpen as e:
    # e.until = timestamp when circuit will half-open
    return cached_response()
```

### Lifecycle hooks

```python
@retry(
    max_attempts=5,
    on_retry=lambda attempt, exc, delay: (
        logger.warning(f"Attempt {attempt} failed: {exc}. Retrying in {delay:.1f}s")
    ),
    on_fail=lambda exc, total: (
        alerting.send(f"CRITICAL: Failed after {total} attempts: {exc}")
    ),
    on_circuit_open=lambda: (
        metrics.increment("circuit_breaker.opened")
    ),
)
def critical_operation():
    ...
```

| Hook | Signature | When |
|------|-----------|------|
| `on_retry` | `(attempt: int, exception \| None, delay: float)` | Before each retry sleep. `exception` is `None` for result-based retries. |
| `on_fail` | `(last_exception, total_attempts: int)` | When all attempts exhausted |
| `on_circuit_open` | `()` | When circuit breaker trips |

### Max delay cap

```python
@retry(max_attempts=10, backoff=2.0, max_delay=30.0)
def long_retry():
    ...
```

Delays: 2s → 4s → 8s → 16s → 30s → 30s → 30s → ...

Without `max_delay`, attempt 10 would wait 1024 seconds. The cap prevents absurd waits.

### Context manager mode

When you can't use a decorator — inline retry with a per-attempt context manager:

```python
from tenaz import retrying

for attempt in retrying(max_attempts=3, backoff=0.5):
    with attempt:
        connection = establish_connection()
```

Each `attempt` is a context manager that catches retryable exceptions and suppresses them until the last attempt. On the final attempt, the exception propagates normally.

#### Async context manager

```python
from tenaz import async_retrying

async for attempt in async_retrying(max_attempts=3, backoff=0.5):
    with attempt:
        result = await fragile_async_call()
```

### RetryExhausted exception

When all attempts fail, `RetryExhausted` wraps the last exception:

```python
from tenaz import retry, RetryExhausted

@retry(max_attempts=3)
def will_fail():
    raise ConnectionError("down")

try:
    will_fail()
except RetryExhausted as e:
    print(e.last_exception)  # ConnectionError("down")
    print(e.attempts)         # 3
```

---

## API Reference

### `retry(**kwargs)`

The main decorator. All parameters are optional.

```python
@retry(
    max_attempts=3,          # Total attempts including first (1 = no retry)
    backoff=1.0,             # Base delay in seconds
    max_delay=60.0,          # Maximum delay cap
    jitter=True,             # Full jitter (randomize 0 to delay)
    retry_on=(Exception,),   # Exception types to retry
    abort_on=(),             # Exception types to abort immediately
    retry_on_result=None,    # Predicate fn(result) → bool; True = retry
    on_retry=None,           # Hook: fn(attempt, exception | None, delay)
    on_fail=None,            # Hook: fn(last_exception, total_attempts)
    total_timeout=0.0,       # Wall-clock cap in seconds (0 = unlimited)
    circuit_threshold=0,     # Failures to trip breaker (0 = disabled)
    circuit_timeout=30.0,    # Seconds breaker stays open
    on_circuit_open=None,    # Hook: fn() called when breaker trips
)
def your_function():
    ...
```

### Parameter Validation

All parameters are validated at construction time. Invalid values raise `ValueError`:

- `max_attempts` must be >= 1
- `backoff` must be >= 0
- `max_delay` must be >= 0
- `total_timeout` must be >= 0
- `circuit_threshold` must be >= 0
- `circuit_timeout` must be > 0 when circuit is enabled

### `retrying(**kwargs)` / `async_retrying(**kwargs)`

Iterables for inline retry. Each iteration yields an `_Attempt` context manager. On final failure, raises `RetryExhausted`.

```python
# Sync
for attempt in retrying(
    max_attempts=3,
    backoff=1.0,
    max_delay=60.0,
    jitter=True,
    retry_on=(Exception,),
    abort_on=(),              # exception types that abort immediately
    total_timeout=0.0,        # wall-clock cap (0 = unlimited)
    on_retry=None,            # hook: fn(attempt, exception, delay)
):
    with attempt:
        result = do_something()

# Async
async for attempt in async_retrying(max_attempts=3):
    with attempt:
        result = await do_something()
```

### Exceptions

| Exception | When | Attributes |
|-----------|------|------------|
| `RetryExhausted` | All attempts consumed | `.last_exception`, `.attempts` |
| `RetryTimeout` | Total timeout exceeded | `.last_exception`, `.elapsed`, `.attempts` |
| `CircuitOpen` | Circuit breaker is open | `.until` (monotonic timestamp) |

---

## Backoff Strategy Deep Dive

tenaz uses **exponential backoff with full jitter**, the same strategy recommended by AWS for distributed systems.

```
delay = min(backoff × 2^attempt, max_delay)
if jitter:
    delay = random(0, delay)
```

**Why full jitter?** Without jitter, when a service recovers from an outage, all clients retry at the exact same intervals — creating synchronized bursts that can take the service down again. Full jitter spreads retries uniformly across the delay window.

```
Without jitter (thundering herd):
  Client A: ──X──1s──X──2s──X──4s──✓
  Client B: ──X──1s──X──2s──X──4s──✓    ← all hit at same time
  Client C: ──X──1s──X──2s──X──4s──✓

With full jitter (spread):
  Client A: ──X──0.7s──X──1.3s──X──2.8s──✓
  Client B: ──X──0.2s──X──1.9s──X──0.5s──✓   ← distributed
  Client C: ──X──0.9s──X──0.4s──X──3.1s──✓
```

To disable jitter (not recommended in production):
```python
@retry(jitter=False)
```

---

## Circuit Breaker Deep Dive

The circuit breaker has 3 states:

```
 ┌─────────────────────────────────────────────────────────┐
 │                                                         │
 │   CLOSED ──(N failures)──→ OPEN ──(timeout)──→ HALF-OPEN
 │     ↑                                            │
 │     └──────────(success)─────────────────────────┘
 │                                            │
 │                              (failure)──→ OPEN
 └─────────────────────────────────────────────────────────┘
```

| State | Behavior |
|-------|----------|
| **Closed** | Normal operation. Failures counted. |
| **Open** | All calls rejected instantly with `CircuitOpen`. No execution. |
| **Half-Open** | Exactly one probe call allowed (atomic). Success → Closed. Failure → Open. All other callers rejected. |

The breaker is **per-decorated-function**, thread-safe, and shares state across all calls to that function.

```python
# Breaker trips after 5 failures, stays open 30s
@retry(max_attempts=2, circuit_threshold=5, circuit_timeout=30.0)
def service_call():
    return requests.get("https://fragile-service.com")

# After 5 consecutive failures across any caller:
# - service_call() raises CircuitOpen instantly for 30 seconds
# - After 30s, one call goes through as a test
# - If test succeeds → circuit closes, normal operation resumes
# - If test fails → circuit reopens for another 30s
```

---

## Real-World Examples

### HTTP client with full protection

```python
import requests
from tenaz import retry, RetryExhausted, CircuitOpen

@retry(
    max_attempts=4,
    backoff=0.5,
    max_delay=10.0,
    retry_on=(requests.ConnectionError, requests.Timeout),
    abort_on=(requests.HTTPError,),    # Don't retry 4xx/5xx
    circuit_threshold=10,
    circuit_timeout=60.0,
    total_timeout=30.0,
    on_retry=lambda a, e, d: logger.info(f"Retry {a}, wait {d:.1f}s"),
)
def api_get(url: str) -> dict:
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    return resp.json()
```

### Polling with retry_on_result

```python
@retry(
    max_attempts=20,
    backoff=1.0,
    max_delay=10.0,
    total_timeout=120.0,
    retry_on_result=lambda r: r["status"] == "pending",
)
def wait_for_deploy(deploy_id: str) -> dict:
    return api.get_deploy_status(deploy_id)
```

### Database reconnection

```python
@retry(
    max_attempts=5,
    backoff=2.0,
    max_delay=30.0,
    retry_on=(psycopg2.OperationalError,),
    on_fail=lambda e, n: alert(f"DB unreachable after {n} attempts"),
)
def get_user(user_id: int):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()
```

### Async task queue consumer

```python
@retry(max_attempts=3, backoff=1.0, circuit_threshold=20, circuit_timeout=120.0)
async def process_job(job_id: str):
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            "https://worker.internal/process",
            json={"job_id": job_id},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return await resp.json()
```

### Graceful degradation with circuit breaker

```python
from tenaz import retry, CircuitOpen, RetryExhausted

@retry(max_attempts=2, circuit_threshold=5, circuit_timeout=30.0)
def get_recommendations(user_id: str) -> list:
    return recommendation_service.fetch(user_id)

def handle_request(user_id: str):
    try:
        recs = get_recommendations(user_id)
    except CircuitOpen:
        recs = get_cached_recommendations(user_id)  # Fallback
    except RetryExhausted:
        recs = default_recommendations()             # Default
    return render(recs)
```

### Inline retry with context manager

```python
from tenaz import retrying

for attempt in retrying(max_attempts=3, backoff=1.0, retry_on=(IOError,)):
    with attempt:
        with open("/mnt/nfs/data.csv") as f:
            data = f.read()
```

### Context manager with timeout and hooks

```python
from tenaz import retrying, RetryExhausted

try:
    for attempt in retrying(
        max_attempts=10,
        backoff=0.5,
        total_timeout=30.0,
        abort_on=(PermissionError,),
        on_retry=lambda a, e, d: logger.info(f"Retry {a}, wait {d:.1f}s"),
    ):
        with attempt:
            data = fetch_from_nfs()
except RetryExhausted as e:
    logger.error(f"Failed after {e.attempts} attempts: {e.last_exception}")
```

---

## Thread Safety

tenaz is thread-safe:

- The circuit breaker uses `threading.Lock` for all state mutations
- The half-open state allows exactly one probe call through — concurrent callers are rejected atomically
- Each decorated function gets its own independent breaker instance
- Backoff delays use `time.sleep` (sync) or `asyncio.sleep` (async) — no shared timers

Safe to use in multi-threaded servers (Django, Flask with threads, etc.) and async frameworks (FastAPI, aiohttp, etc.).

---

## FAQ

**Q: Why "tenaz"?**  
A: Spanish for "tenacious". Because your code should be.

**Q: Can I just copy the file instead of pip install?**  
A: Yes. That's the intended primary use. It's one file, zero deps.

**Q: How is the circuit breaker different from `pybreaker`?**  
A: It's integrated into the retry decorator — no separate wrapper needed. It's simpler (3 states, threshold + timeout), which covers 95% of use cases. If you need advanced breaker features (listeners, storage backends, excluded exceptions), use `pybreaker`.

**Q: Does it work with Django/Flask/FastAPI?**  
A: Yes. It's a pure Python decorator with no framework dependencies.

**Q: What Python versions?**  
A: 3.10+ (uses `Union` type syntax from `__future__` annotations).

**Q: Is the circuit breaker per-function or global?**  
A: Per-function. Each `@retry(circuit_threshold=...)` creates its own breaker. Two different decorated functions have independent failure counts.

**Q: What's the difference between `RetryExhausted` and `RetryTimeout`?**  
A: `RetryExhausted` means all `max_attempts` were used. `RetryTimeout` means `total_timeout` seconds elapsed before all attempts could run. Both carry `.last_exception` and `.attempts`.

---

## License

MIT — see [LICENSE](LICENSE).
