"""Mock KedaiKita order system: read-only FastAPI over the SYNTHETIC SQLite data.

    GET /order/{order_id}
    GET /tracking/{tracking_no}
    GET /refund/{order_id}
    GET /health

Every data endpoint takes an optional `?as_of=2026-01-12 10:00:00`. Status is derived from stored
timestamps (see state.py), so the API can answer "what would the customer have seen then?". With
no as_of it answers as of DEFAULT_AS_OF (config.py: end of day 30, or MOCK_API_AS_OF from .env).

Deliberate limits:
* Opens the DB read-only through db.connect_agent, so it cannot write or read the ground-truth answer key.
* Returns customer_id but no name, email, phone or street address. The identity check
  (does this order belong to the person on the ticket?) is done by the agent tools in week 3.
* Database problems return 503, which the agent's circuit breaker will treat as "order system down".

Run the server:  uvicorn src.mock_api:app --reload      (from the project root)
Run the self-test: python src/mock_api.py
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # lets `uvicorn src.mock_api:app` import config/db/state

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

import config as C  # noqa: E402
import db  # noqa: E402
import state  # noqa: E402

app = FastAPI(title="KedaiKita mock order API", description="Synthetic data only. KedaiKita is fictional.")

AS_OF_HELP = "Answer as of this moment, e.g. 2026-01-12 10:00:00 (local time, no timezone). Default: DEFAULT_AS_OF."


@app.exception_handler(sqlite3.Error)
def database_down(_request: Request, _exc: sqlite3.Error) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "order database unavailable"})


def _resolve_as_of(value: datetime | None) -> datetime:
    if value is None:
        return C.DEFAULT_AS_OF
    if value.tzinfo is not None:
        raise HTTPException(status_code=422, detail="as_of must be a local timestamp without a timezone")
    return value


def _one(sql: str, *params: object) -> dict | None:
    """Run a read-only query and return the first row as a dict (or None)."""
    with closing(db.connect_agent(readonly=True)) as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _stamp(at: datetime) -> str:
    return state.to_str(at)


@app.get("/health")
def health() -> dict:
    _one("SELECT 1")
    return {"status": "ok"}


@app.get("/order/{order_id}")
def get_order(order_id: str, as_of: datetime | None = Query(None, description=AS_OF_HELP)) -> dict:
    at = _resolve_as_of(as_of)
    order = _one("SELECT * FROM orders WHERE order_id = ?", order_id)
    if order is None or state.to_dt(order["order_date"]) > at:
        raise HTTPException(status_code=404, detail=f"order {order_id} not found")
    ship = _one("SELECT * FROM shipments WHERE order_id = ?", order_id)
    shipped = ship is not None and state.to_dt(ship["shipped_at"]) <= at
    cancelled = order["cancelled_at"] is not None and state.to_dt(order["cancelled_at"]) <= at
    return {
        "order_id": order["order_id"],
        "customer_id": order["customer_id"],
        "order_date": order["order_date"],
        "items": order["items"],
        "total_rm": order["total_rm"],
        "payment_method": order["payment_method"],
        "ship_state": order["ship_state"],
        "ship_postcode": order["ship_postcode"],
        "status": state.order_status(order, ship, at),
        "tracking_no": ship["tracking_no"] if shipped else None,
        "cancelled_at": order["cancelled_at"] if cancelled else None,
        "as_of": _stamp(at),
    }


@app.get("/tracking/{tracking_no}")
def get_tracking(tracking_no: str, as_of: datetime | None = Query(None, description=AS_OF_HELP)) -> dict:
    at = _resolve_as_of(as_of)
    ship = _one("SELECT s.*, o.order_date FROM shipments s JOIN orders o USING (order_id) WHERE s.tracking_no = ?",
                tracking_no)
    if ship is None or state.to_dt(ship["order_date"]) > at:
        raise HTTPException(status_code=404, detail=f"tracking number {tracking_no} not found")
    return {**state.tracking_state(ship, at), "as_of": _stamp(at)}


@app.get("/refund/{order_id}")
def get_refund(order_id: str, as_of: datetime | None = Query(None, description=AS_OF_HELP)) -> dict:
    at = _resolve_as_of(as_of)
    refund = _one("SELECT * FROM refunds WHERE order_id = ?", order_id)
    result = state.refund_state(refund, at) if refund else None
    if result is None:
        raise HTTPException(status_code=404, detail=f"no refund found for order {order_id}")
    return {**result, "as_of": _stamp(at)}


def _self_test() -> None:
    from datetime import timedelta

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # starlette warns about httpx; harmless
        from fastapi.testclient import TestClient

    if not C.DB_PATH.exists():
        sys.exit(f"{C.DB_PATH} not found. Run: python src/generate_data.py")
    client = TestClient(app)
    dt, st = state.to_dt, state.to_str

    def get(path: str, **params):
        return client.get(path, params=params)

    assert get("/health").json() == {"status": "ok"}
    print("ok  /health")

    # a parcel held by the Shah Alam incident
    ship = _one("SELECT * FROM shipments WHERE delay_reason = 'hub_congestion' ORDER BY tracking_no LIMIT 1")
    tn, oid = ship["tracking_no"], ship["order_id"]
    during = st(dt(ship["delay_flagged_at"]) + timedelta(hours=1))
    r = get(f"/tracking/{tn}", as_of=during).json()
    assert r["status"] == "delayed" and "Shah Alam" in r["last_event"] and r["revised_eta"] is not None, r
    assert r == {**state.tracking_state(ship, dt(during)), "as_of": during}
    r = get(f"/tracking/{tn}").json()
    assert r["status"] == "delivered" and r["revised_eta"] is None, r
    before = st(dt(ship["shipped_at"]) - timedelta(hours=1))
    assert get(f"/tracking/{tn}", as_of=before).json()["status"] == "label_created"
    print(f"ok  /tracking {tn}: label_created -> delayed at Shah Alam -> delivered (same parcel, three moments)")

    order = get(f"/order/{oid}").json()
    assert order["status"] == "delivered" and order["tracking_no"] == tn and order["customer_id"].startswith("CU-")
    assert not {"name", "email", "phone", "address"} & set(order), "personal data leaked"
    assert get(f"/order/{oid}", as_of=during).json()["status"] == "shipped"
    assert get(f"/order/{oid}", as_of=st(dt(ship["shipped_at"]) - timedelta(days=20))).status_code == 404
    print("ok  /order: status follows time, no name/email/phone/address, 404 before the order existed")

    approved = _one("SELECT * FROM refunds WHERE decision = 'approved' ORDER BY refund_id LIMIT 1")
    rejected = _one("SELECT * FROM refunds WHERE decision = 'rejected' ORDER BY refund_id LIMIT 1")
    o = approved["order_id"]
    assert get(f"/refund/{o}", as_of=st(dt(approved["requested_at"]) - timedelta(minutes=1))).status_code == 404
    for moment, expected in ((approved["requested_at"], "requested"), (approved["decided_at"], "approved"),
                             (approved["paid_at"], "paid")):
        assert get(f"/refund/{o}", as_of=moment).json()["status"] == expected, (moment, expected)
    assert get(f"/refund/{rejected['order_id']}", as_of=rejected["decided_at"]).json()["status"] == "rejected"
    print("ok  /refund: 404 -> requested -> approved -> paid, and rejected")

    for path in ("/order/KK-999999", "/tracking/NOPE", "/refund/KK-999999"):
        assert get(path).status_code == 404, path
    assert get(f"/order/{oid}", as_of="2026-01-12T10:00:00Z").status_code == 422
    assert get(f"/order/{oid}", as_of="not-a-date").status_code == 422
    print("ok  unknown ids -> 404, bad or timezone-aware as_of -> 422")

    problems = db.find_bypasses()
    assert not problems, problems
    print("ok  mock_api.py opens the DB only through db.connect_agent and never names the answer-key table")
    print("mock_api.py self-test passed.")


if __name__ == "__main__":
    _self_test()
