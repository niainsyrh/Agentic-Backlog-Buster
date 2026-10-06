"""Agent tools: typed read-only wrappers around the mock order API (src/mock_api.py).

    get_order(order_id, customer_id=..., as_of=...)
    get_tracking(tracking_no, customer_id=..., as_of=...)
    get_refund_status(order_id, customer_id=..., as_of=...)

These are the only way agent code touches order data; it never queries SQLite directly. Each call:

* passes as_of=<the ticket's created_at>, so a replayed ticket sees exactly what the customer would
  have seen at that moment, never "now". as_of is a required argument on purpose: there is no
  default, so a caller cannot forget it and silently get today's state for an old ticket.
* checks IDENTITY in code before returning anything: the order's customer_id must match the
  customer_id the caller passed in, or the tool returns identity_mismatch and no data at all.
  get_tracking and get_refund_status don't get a customer_id back from their own endpoint, so they
  verify ownership via a second call to /order/{order_id}.
* goes through a CIRCUIT BREAKER: after CIRCUIT_BREAKER_FAILURES consecutive transport failures or
  5xx responses, every further call returns circuit_open immediately (no network call) until
  breaker.reset() runs or a call succeeds. A 404 or 422 is a healthy, correct answer from the
  server, so it does NOT count as a breaker failure and resets any failure streak.

This module has no side effects besides the HTTP call: it never writes audit_log. The caller
(router/pipeline, week 3) is responsible for logging what was asked, what came back and why.

Self-test (no network, uses the real generated DB via an in-process TestClient + a fake transport
for breaker/failure cases):   python src/agent/tools.py
Against a real running server (start `uvicorn src.mock_api:app` first):   python src/agent/tools.py --live
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # so `import config` / `state` work

import config as C  # noqa: E402
import state  # noqa: E402


class _Transport(Protocol):
    """What this module needs from an HTTP client: httpx.Client and FastAPI's TestClient both fit."""
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...


@dataclass(frozen=True)
class ToolResult:
    """ok=False means: stop and send the ticket to a human. `data`, when ok, is the mock API's own
    JSON shape unchanged (no re-typing it into a parallel schema that could drift from the real one).
    """
    ok: bool
    data: dict | None = None
    error: str | None = None   # not_found | identity_mismatch | circuit_open | timeout | http_error


class CircuitBreaker:
    """Tracks consecutive order-API failures; opens after `threshold` in a row."""

    def __init__(self, threshold: int = C.CIRCUIT_BREAKER_FAILURES) -> None:
        self.threshold = threshold
        self.consecutive_failures = 0
        self.open = False

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.open = False

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.open = True

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.open = False


_default_breaker = CircuitBreaker()
_default_client: httpx.Client | None = None


def _get_default_client() -> httpx.Client:
    global _default_client
    if _default_client is None:
        _default_client = httpx.Client(base_url=C.MOCK_API_URL, timeout=C.API_TIMEOUT_S)
    return _default_client


def _get(client: _Transport, breaker: CircuitBreaker, path: str, as_of: datetime) -> ToolResult:
    if breaker.open:
        return ToolResult(ok=False, error="circuit_open")
    try:
        response = client.get(path, params={"as_of": state.to_str(as_of)})
    except httpx.TimeoutException:
        breaker.record_failure()
        return ToolResult(ok=False, error="timeout")
    except httpx.TransportError:
        breaker.record_failure()
        return ToolResult(ok=False, error="http_error")
    if response.status_code == 404:
        breaker.record_success()   # the server answered correctly: "no such record" is not "down"
        return ToolResult(ok=False, error="not_found")
    if response.status_code >= 500:
        breaker.record_failure()
        return ToolResult(ok=False, error="http_error")
    if response.status_code != 200:
        breaker.record_success()   # e.g. 422 bad as_of: a caller bug, not evidence the service is down
        return ToolResult(ok=False, error=f"http_{response.status_code}")
    breaker.record_success()
    return ToolResult(ok=True, data=response.json())


def _check_owns_order(order_id: str, customer_id: str, as_of: datetime,
                      client: _Transport, breaker: CircuitBreaker) -> ToolResult | None:
    """None means ownership confirmed; otherwise the ToolResult the caller should return as-is."""
    order = _get(client, breaker, f"/order/{order_id}", as_of)
    if not order.ok:
        return order
    if order.data["customer_id"] != customer_id:
        return ToolResult(ok=False, error="identity_mismatch")
    return None


def get_order(order_id: str, *, customer_id: str, as_of: datetime,
              client: _Transport | None = None, breaker: CircuitBreaker | None = None) -> ToolResult:
    client, breaker = client or _get_default_client(), breaker or _default_breaker
    result = _get(client, breaker, f"/order/{order_id}", as_of)
    if not result.ok:
        return result
    if result.data["customer_id"] != customer_id:
        return ToolResult(ok=False, error="identity_mismatch")
    return result


def get_tracking(tracking_no: str, *, customer_id: str, as_of: datetime,
                 client: _Transport | None = None, breaker: CircuitBreaker | None = None) -> ToolResult:
    client, breaker = client or _get_default_client(), breaker or _default_breaker
    result = _get(client, breaker, f"/tracking/{tracking_no}", as_of)
    if not result.ok:
        return result
    mismatch = _check_owns_order(result.data["order_id"], customer_id, as_of, client, breaker)
    return mismatch if mismatch is not None else result


def get_refund_status(order_id: str, *, customer_id: str, as_of: datetime,
                      client: _Transport | None = None, breaker: CircuitBreaker | None = None) -> ToolResult:
    client, breaker = client or _get_default_client(), breaker or _default_breaker
    result = _get(client, breaker, f"/refund/{order_id}", as_of)
    if not result.ok:
        return result
    mismatch = _check_owns_order(result.data["order_id"], customer_id, as_of, client, breaker)
    return mismatch if mismatch is not None else result


# ---------------------------------------------------------------- tests
class _FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code, self._body = status_code, body

    def json(self) -> dict:
        return self._body


class _FlakyClient:
    """Fails with a transport error for the first `fail_times` calls, then returns 200 OK."""

    def __init__(self, fail_times: int, ok_body: dict) -> None:
        self.fail_times, self.ok_body, self.calls = fail_times, ok_body, 0

    def get(self, path: str, params: dict | None = None) -> _FakeResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ConnectError("simulated: order system unreachable")
        return _FakeResponse(200, self.ok_body)


def _test_happy_path_and_identity() -> None:
    import warnings

    import db

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import mock_api
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # starlette warns about httpx; harmless
        from fastapi.testclient import TestClient

    if not C.DB_PATH.exists():
        sys.exit(f"{C.DB_PATH} not found. Run: python src/generate_data.py")
    client = TestClient(mock_api.app)
    breaker = CircuitBreaker(threshold=3)
    conn = db.connect_admin()
    at = datetime(2026, 1, 30)

    order = dict(conn.execute("SELECT order_id, customer_id FROM orders LIMIT 1").fetchone())
    r = get_order(order["order_id"], customer_id=order["customer_id"], as_of=at, client=client, breaker=breaker)
    assert r.ok and r.data["order_id"] == order["order_id"]
    r = get_order(order["order_id"], customer_id="CU-99999", as_of=at, client=client, breaker=breaker)
    assert not r.ok and r.error == "identity_mismatch" and r.data is None
    r = get_order("KK-999999", customer_id=order["customer_id"], as_of=at, client=client, breaker=breaker)
    assert not r.ok and r.error == "not_found"
    print("ok  get_order: correct owner returns data; wrong customer -> identity_mismatch, no data leaked; unknown -> not_found")

    ship = dict(conn.execute("SELECT tracking_no, order_id FROM shipments LIMIT 1").fetchone())
    owner = conn.execute("SELECT customer_id FROM orders WHERE order_id = ?", (ship["order_id"],)).fetchone()[0]
    r = get_tracking(ship["tracking_no"], customer_id=owner, as_of=at, client=client, breaker=breaker)
    assert r.ok and r.data["tracking_no"] == ship["tracking_no"]
    r = get_tracking(ship["tracking_no"], customer_id="CU-99999", as_of=at, client=client, breaker=breaker)
    assert not r.ok and r.error == "identity_mismatch"
    print("ok  get_tracking: identity enforced via the order it belongs to (tracking has no customer_id of its own)")

    refund_row = conn.execute("SELECT order_id FROM refunds LIMIT 1").fetchone()
    assert refund_row, "need at least one refund in the generated data for this check"
    refund_order_id = refund_row[0]
    owner = conn.execute("SELECT customer_id FROM orders WHERE order_id = ?", (refund_order_id,)).fetchone()[0]
    r = get_refund_status(refund_order_id, customer_id=owner, as_of=at, client=client, breaker=breaker)
    assert r.ok and r.data["order_id"] == refund_order_id
    r = get_refund_status(refund_order_id, customer_id="CU-99999", as_of=at, client=client, breaker=breaker)
    assert not r.ok and r.error == "identity_mismatch"
    print("ok  get_refund_status: happy path + identity enforced the same way")

    assert breaker.consecutive_failures == 0 and not breaker.open
    print("ok  not_found / identity_mismatch along the way never tripped the circuit breaker")
    conn.close()


def _test_circuit_breaker() -> None:
    at = datetime(2026, 1, 1)

    breaker = CircuitBreaker(threshold=3)
    flaky = _FlakyClient(fail_times=3, ok_body={"order_id": "KK-000001", "customer_id": "CU-1"})
    for _ in range(3):
        r = get_order("KK-000001", customer_id="CU-1", as_of=at, client=flaky, breaker=breaker)
        assert not r.ok and r.error == "http_error"
    assert breaker.open
    r = get_order("KK-000001", customer_id="CU-1", as_of=at, client=flaky, breaker=breaker)
    assert not r.ok and r.error == "circuit_open" and flaky.calls == 3, "open breaker must not hit the network"
    print("ok  3 consecutive transport failures open the breaker; further calls skip the network entirely")

    breaker.reset()
    r = get_order("KK-000001", customer_id="CU-1", as_of=at, client=flaky, breaker=breaker)
    assert r.ok and flaky.calls == 4 and breaker.consecutive_failures == 0
    print("ok  breaker.reset() allows calls again; a healthy response closes it")

    breaker2 = CircuitBreaker(threshold=3)
    flaky2 = _FlakyClient(fail_times=1, ok_body={"order_id": "KK-000001", "customer_id": "CU-1"})
    r1 = get_order("KK-000001", customer_id="CU-1", as_of=at, client=flaky2, breaker=breaker2)
    assert not r1.ok and not breaker2.open
    r2 = get_order("KK-000001", customer_id="CU-1", as_of=at, client=flaky2, breaker=breaker2)
    assert r2.ok and breaker2.consecutive_failures == 0
    print("ok  a single failure does not trip the breaker, and a later success resets the streak")


def _self_test() -> None:
    _test_happy_path_and_identity()
    _test_circuit_breaker()
    print("tools.py self-test passed.")


def _live_demo() -> None:
    import db

    conn = db.connect_admin()
    order = dict(conn.execute("SELECT order_id, customer_id FROM orders LIMIT 1").fetchone())
    conn.close()
    at = datetime(2026, 1, 30)
    print("get_order (real owner):     ", get_order(order["order_id"], customer_id=order["customer_id"], as_of=at))
    print("get_order (wrong customer): ", get_order(order["order_id"], customer_id="CU-99999", as_of=at))
    print("get_order (unknown id):     ", get_order("KK-999999", customer_id=order["customer_id"], as_of=at))


if __name__ == "__main__":
    _live_demo() if "--live" in sys.argv else _self_test()
