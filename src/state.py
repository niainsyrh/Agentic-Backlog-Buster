"""Point-in-time state of shipments, orders and refunds (all SYNTHETIC data).

The database stores TIMESTAMPS (picked up, arrived at hub, flagged delayed,
delivered ...), not a single status. The status at any moment is derived here
from `as_of`. That keeps a replay of 30 days honest: on day 12 a delayed parcel
really looks delayed, even though by day 30 it has been delivered.

Used by the mock API (to answer queries) and by generate_data.py (to check
that every ticket matches the data at the moment it was written).

Functions accept dict-like rows; convert sqlite3.Row with dict(row) first.
Run `python src/state.py` for the self-test.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

IN_TRANSIT = ("label_created", "picked_up", "at_hub", "left_hub", "out_for_delivery")

_DELAY_TEXT = {
    "hub_congestion": "Delayed at {hub} hub due to high parcel volume",
    "courier_delay": "Delayed in transit, delivery will be rescheduled",
    "lost": "Shipment exception, under investigation",
}


def to_dt(value: str | datetime | None) -> datetime | None:
    """Parse a stored timestamp (or pass through None / datetime)."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.strptime(value, TS_FORMAT)


def to_str(value: datetime | None) -> str | None:
    """Format a datetime the way the database stores it."""
    return value.strftime(TS_FORMAT) if value else None


def tracking_state(shipment: Mapping[str, Any], as_of: datetime) -> dict[str, Any]:
    """What the tracking page would show at `as_of`.

    status is one of: label_created, picked_up, at_hub, delayed, left_hub,
    out_for_delivery, delivered.
    """
    s = shipment
    events: list[tuple[datetime, str, str]] = [
        (to_dt(s["shipped_at"]), "picked_up", f"Picked up by {s['courier']}"),
        (to_dt(s["hub_arrival_at"]), "at_hub", f"Arrived at {s['hub']} hub"),
    ]
    flagged = to_dt(s["delay_flagged_at"])
    if flagged:
        events.append((flagged, "delayed", _DELAY_TEXT[s["delay_reason"]].format(hub=s["hub"])))
    release = to_dt(s["hub_release_at"])
    if release:
        events.append((release, "left_hub", f"Departed {s['hub']} hub"))
    delivered = to_dt(s["delivered_at"])
    if delivered:
        events.append((delivered.replace(hour=8, minute=0, second=0), "out_for_delivery", "Out for delivery"))
        events.append((delivered, "delivered", "Delivered"))

    seen = sorted((e for e in events if e[0] <= as_of), key=lambda e: e[0])
    if seen:
        last_ts, status, description = seen[-1]
    else:
        last_ts, status, description = None, "label_created", "Shipment information received"

    revised_eta = delivered.date().isoformat() if (status == "delayed" and delivered) else None
    return {
        "tracking_no": s["tracking_no"],
        "order_id": s["order_id"],
        "courier": s["courier"],
        "hub": s["hub"],
        "status": status,
        "last_event": description,
        "last_event_at": to_str(last_ts),
        "promised_date": s["promised_date"],
        "revised_eta": revised_eta,
        "history": [{"at": to_str(t), "status": c, "description": d} for t, c, d in seen],
    }


def order_status(order: Mapping[str, Any], shipment: Mapping[str, Any] | None, as_of: datetime) -> str:
    """One of: placed, shipped, delivered, cancelled."""
    if to_dt(order["order_date"]) > as_of:
        raise ValueError("order does not exist yet at as_of")
    cancelled = to_dt(order["cancelled_at"])
    if cancelled and cancelled <= as_of:
        return "cancelled"
    if shipment is None:
        return "placed"
    code = tracking_state(shipment, as_of)["status"]
    if code == "delivered":
        return "delivered"
    return "placed" if code == "label_created" else "shipped"


def refund_state(refund: Mapping[str, Any], as_of: datetime) -> dict[str, Any] | None:
    """Refund as of `as_of`, or None if it had not been requested yet.

    status is one of: requested, rejected, approved, paid.
    """
    requested = to_dt(refund["requested_at"])
    if requested > as_of:
        return None
    decided, paid = to_dt(refund["decided_at"]), to_dt(refund["paid_at"])
    if decided is None or decided > as_of:
        status = "requested"
    elif refund["decision"] == "rejected":
        status = "rejected"
    elif paid is not None and paid <= as_of:
        status = "paid"
    else:
        status = "approved"
    return {
        "refund_id": refund["refund_id"],
        "order_id": refund["order_id"],
        "amount_rm": refund["amount_rm"],
        "reason": refund["reason"],
        "status": status,
        "requested_at": to_str(requested),
        "decided_at": to_str(decided) if decided and decided <= as_of else None,
        "paid_at": to_str(paid) if paid and paid <= as_of else None,
    }


def _self_test() -> None:
    at = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M")  # noqa: E731
    base = {"tracking_no": "LG0000000001MY", "order_id": "KK-000001", "courier": "LajuGo",
            "hub": "Shah Alam", "promised_date": "2026-01-15", "delay_reason": None,
            "delay_flagged_at": None, "delay_days": 0}
    order = {"order_id": "KK-000001", "order_date": "2026-01-09 10:00:00", "cancelled_at": None}

    # Hub-congestion shipment: at hub Jan 10, flagged Jan 12, released Jan 15, delivered Jan 17.
    hub = {**base, "shipped_at": "2026-01-09 17:05:00", "hub_arrival_at": "2026-01-10 06:30:00",
           "delay_reason": "hub_congestion", "delay_flagged_at": "2026-01-12 03:00:00", "delay_days": 3,
           "hub_release_at": "2026-01-15 06:30:00", "delivered_at": "2026-01-17 14:00:00"}
    assert tracking_state(hub, at("2026-01-09 12:00"))["status"] == "label_created"
    assert tracking_state(hub, at("2026-01-11 12:00"))["status"] == "at_hub"
    t = tracking_state(hub, at("2026-01-12 10:00"))
    assert t["status"] == "delayed" and t["revised_eta"] == "2026-01-17"
    assert "Shah Alam" in t["last_event"]
    assert tracking_state(hub, at("2026-01-16 12:00"))["status"] == "left_hub"
    assert tracking_state(hub, at("2026-01-17 09:00"))["status"] == "out_for_delivery"
    assert tracking_state(hub, at("2026-01-17 15:00"))["status"] == "delivered"
    print("ok  tracking: label -> at_hub -> delayed (Jan 12) -> left_hub -> out_for_delivery -> delivered")

    # Lost shipment never leaves the 'delayed' state and has no ETA.
    lost = {**base, "shipped_at": "2026-01-03 17:00:00", "hub_arrival_at": "2026-01-04 06:00:00",
            "delay_reason": "lost", "delay_flagged_at": "2026-01-06 06:00:00", "hub_release_at": None,
            "delivered_at": None}
    t = tracking_state(lost, at("2026-01-30 12:00"))
    assert t["status"] == "delayed" and t["revised_eta"] is None
    print("ok  lost parcel stays 'delayed' with no ETA")

    assert order_status(order, hub, at("2026-01-09 12:00")) == "placed"
    assert order_status(order, hub, at("2026-01-12 10:00")) == "shipped"
    assert order_status(order, hub, at("2026-01-18 10:00")) == "delivered"
    assert order_status({**order, "cancelled_at": "2026-01-09 12:00:00"}, None, at("2026-01-09 13:00")) == "cancelled"
    assert order_status({**order, "cancelled_at": "2026-01-09 12:00:00"}, None, at("2026-01-09 11:00")) == "placed"
    print("ok  order status derived from shipment and cancellation time")

    refund = {"refund_id": "RF-000001", "order_id": "KK-000001", "amount_rm": 42.5, "reason": "item_damaged",
              "requested_at": "2026-01-20 09:00:00", "decision": "approved",
              "decided_at": "2026-01-21 15:00:00", "paid_at": "2026-01-25 10:00:00"}
    assert refund_state(refund, at("2026-01-19 00:00")) is None
    assert refund_state(refund, at("2026-01-20 12:00"))["status"] == "requested"
    assert refund_state(refund, at("2026-01-22 12:00"))["status"] == "approved"
    assert refund_state(refund, at("2026-01-26 12:00"))["status"] == "paid"
    rejected = {**refund, "decision": "rejected", "paid_at": None}
    assert refund_state(rejected, at("2026-01-22 12:00"))["status"] == "rejected"
    print("ok  refund: none -> requested -> approved -> paid, and rejected")
    print("state.py self-test passed.")


if __name__ == "__main__":
    _self_test()
