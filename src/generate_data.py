"""Generate the SYNTHETIC KedaiKita dataset into SQLite (db/backlog.db).

Everything here is synthetic: KedaiKita, its customers, couriers and hubs are
fictional; names/addresses come from curated lists (Faker has no Malay locale).
Ticket text comes from src/ticket_text.py plus the optional hand-written
tickets in data/handwritten_tickets.csv. Fixed seed -> identical data every run.

Pipeline:
  1. customers, orders, shipments (with the planned Shah Alam hub incident), refunds
  2. tickets, each built from a REAL order in a state that fits the complaint,
     e.g. a refund-status ticket points at an order that has a refund, a spike
     ticket points at a shipment genuinely delayed at Shah Alam
  3. ground-truth labels go to `ticket_labels` (agent code can't read it, see db.py)
  4. validate(): an independent second pass re-checks every ticket against the data

Run:  python src/generate_data.py
"""
from __future__ import annotations

import csv
import random
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path

import config as C
import db
import state
import ticket_text

# ------------------------------------------------------------------ schema
def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


SCHEMA = f"""
CREATE TABLE customers (
    customer_id        TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    email              TEXT NOT NULL,
    phone              TEXT NOT NULL,
    address            TEXT NOT NULL,
    city               TEXT NOT NULL,
    state              TEXT NOT NULL,
    postcode           TEXT NOT NULL,
    preferred_language TEXT NOT NULL CHECK (preferred_language IN ({_in(C.LANGUAGES)})),
    created_at         TEXT NOT NULL
);

CREATE TABLE orders (
    order_id       TEXT PRIMARY KEY,
    customer_id    TEXT NOT NULL REFERENCES customers(customer_id),
    order_date     TEXT NOT NULL,
    items          TEXT NOT NULL,
    total_rm       REAL NOT NULL,
    payment_method TEXT NOT NULL,
    ship_state     TEXT NOT NULL,
    ship_postcode  TEXT NOT NULL,
    cancelled_at   TEXT
);

-- Timestamps, not a status: state.tracking_state() derives the status "as of" any moment.
CREATE TABLE shipments (
    tracking_no      TEXT PRIMARY KEY,
    order_id         TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
    courier          TEXT NOT NULL,
    hub              TEXT NOT NULL,
    shipped_at       TEXT NOT NULL,
    hub_arrival_at   TEXT NOT NULL,
    delay_flagged_at TEXT,
    hub_release_at   TEXT,
    delivered_at     TEXT,
    promised_date    TEXT NOT NULL,
    delay_days       INTEGER NOT NULL DEFAULT 0,
    delay_reason     TEXT CHECK (delay_reason IN ('hub_congestion', 'courier_delay', 'lost'))
);

CREATE TABLE refunds (
    refund_id    TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
    amount_rm    REAL NOT NULL,
    reason       TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_at   TEXT NOT NULL,
    paid_at      TEXT
);

-- order_id is only filled when the customer's text quotes the order ID.
CREATE TABLE tickets (
    ticket_id   TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    order_id    TEXT REFERENCES orders(order_id),
    channel     TEXT NOT NULL CHECK (channel IN ({_in(C.CHANNELS)})),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ({_in(C.TICKET_STATUSES)})),
    lane        TEXT CHECK (lane IN ({_in(C.LANES)})),
    source      TEXT NOT NULL CHECK (source IN ({_in(C.SOURCES)}))
);

-- GROUND TRUTH for evaluation only. Agent connections are blocked from this table (src/db.py).
CREATE TABLE ticket_labels (
    ticket_id       TEXT PRIMARY KEY REFERENCES tickets(ticket_id),
    language        TEXT NOT NULL CHECK (language IN ({_in(C.LANGUAGES)})),
    intent          TEXT NOT NULL CHECK (intent IN ({_in(C.INTENTS)})),
    cluster         TEXT NOT NULL,
    expected_lane   TEXT NOT NULL CHECK (expected_lane IN ({_in(C.LANES)})),
    urgency         TEXT CHECK (urgency IN ('low', 'medium', 'high')),
    is_tricky       INTEGER,
    linked_order_id TEXT REFERENCES orders(order_id),
    note            TEXT
);

CREATE TABLE audit_log (
    action_id   TEXT PRIMARY KEY,
    ticket_id   TEXT REFERENCES tickets(ticket_id),
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL CHECK (actor IN ('agent', 'human', 'system')),
    action_type TEXT NOT NULL,
    tool_name   TEXT,
    input_json  TEXT,
    output_json TEXT,
    lane        TEXT,
    reason      TEXT,
    confidence  REAL,
    outcome     TEXT
);

CREATE INDEX idx_orders_customer  ON orders(customer_id);
CREATE INDEX idx_tickets_created  ON tickets(created_at);
CREATE INDEX idx_tickets_customer ON tickets(customer_id);
CREATE INDEX idx_audit_ticket     ON audit_log(ticket_id);
"""

# ------------------------------------------------- curated Malaysian lists
MALAY_MALE = ["Ahmad", "Muhammad", "Aiman", "Farhan", "Irfan", "Syafiq", "Zulkifli", "Hakim", "Amirul", "Razif",
              "Faizal", "Shahrul", "Danial", "Nazri", "Azlan", "Hisham", "Khairul", "Firdaus", "Izzat", "Hafiz"]
MALAY_FEMALE = ["Nur Aisyah", "Aina", "Farah", "Liyana", "Suraya", "Nadia", "Aishah", "Husna", "Sabrina", "Zulaikha",
                "Balqis", "Atiqah", "Hidayah", "Sofea", "Dayana", "Maisarah", "Syazwani", "Nabilah", "Aliyah", "Wardina"]
MALAY_FATHERS = ["Ismail", "Abdullah", "Hassan", "Yusof", "Ibrahim", "Rahman", "Osman", "Salleh", "Kamarudin",
                 "Zainal", "Mahmud", "Ariffin", "Hamid", "Jaafar", "Mansor"]
CHINESE_SURNAMES = ["Tan", "Lim", "Lee", "Wong", "Chong", "Ng", "Teo", "Goh", "Chan", "Yap", "Ong", "Low", "Khoo", "Liew", "Foo"]
CHINESE_GIVEN = ["Wei Ming", "Mei Ling", "Jia Hui", "Zhi Hao", "Xin Yi", "Kah Wai", "Siew Ling", "Jun Kai", "Hui Min",
                 "Yong Sheng", "Pei Shan", "Kok Leong", "Shu Fen", "Chee Keong", "Li Ying"]
INDIAN_MALE = ["Kumar", "Rajesh", "Suresh", "Vijay", "Arun", "Prakash", "Ganesh", "Kavin", "Dinesh", "Thiru"]
INDIAN_FEMALE = ["Priya", "Kavitha", "Deepa", "Anita", "Shanti", "Meena", "Lakshmi", "Divya", "Revathi", "Nisha"]
INDIAN_FATHERS = ["Rajan", "Muthu", "Krishnan", "Subramaniam", "Ramasamy", "Selvam", "Nair", "Pillai"]

STREET_WORDS = ["Mawar", "Melati", "Kenanga", "Bunga Raya", "Setia", "Perdana", "Damai", "Utama", "Indah", "Cempaka", "Seroja", "Anggerik"]
TAMAN_WORDS = ["Harmoni", "Sentosa", "Mutiara", "Puteri", "Bahagia", "Saujana", "Bayu", "Permai", "Aman", "Murni"]
CITIES = {
    "Selangor": ["Shah Alam", "Petaling Jaya", "Klang", "Subang Jaya", "Kajang", "Rawang"],
    "Kuala Lumpur": ["Kuala Lumpur"], "Putrajaya": ["Putrajaya"],
    "Pahang": ["Kuantan", "Temerloh", "Bentong"], "Terengganu": ["Kuala Terengganu", "Dungun"],
    "Kelantan": ["Kota Bharu", "Pasir Mas"], "Pulau Pinang": ["George Town", "Bayan Lepas", "Butterworth"],
    "Kedah": ["Alor Setar", "Sungai Petani"], "Perlis": ["Kangar"], "Perak": ["Ipoh", "Taiping", "Teluk Intan"],
    "Johor": ["Johor Bahru", "Iskandar Puteri", "Batu Pahat", "Muar"], "Melaka": ["Melaka", "Ayer Keroh"],
    "Negeri Sembilan": ["Seremban", "Nilai"], "Sarawak": ["Kuching", "Miri", "Sibu"],
    "Sabah": ["Kota Kinabalu", "Sandakan", "Tawau"],
}
EAST_MALAYSIA = ("Sabah", "Sarawak")
PHONE_PREFIXES = ["010", "011", "012", "013", "014", "016", "017", "018", "019"]
EMAIL_DOMAINS = ["example.com", "example.org", "example.net"]   # reserved domains: never real mailboxes

PRODUCTS = [   # (name, min RM, max RM), generic products, no brands
    ("Baju Kurung Moden", 59, 159), ("Tudung Bawal Cotton", 15, 39), ("Kasut Sukan", 79, 229),
    ("Beg Sandang Wanita", 39, 149), ("Wireless Earbuds", 49, 199), ("Kipas Meja USB", 19, 59),
    ("Rice Cooker 1.8L", 89, 199), ("Air Fryer 4L", 139, 329), ("Botol Air Stainless 1L", 25, 69),
    ("Phone Case", 9, 39), ("Cadar Queen Set", 59, 139), ("Set Pinggan Mangkuk", 35, 99),
    ("Kerepek Pisang Set", 12, 35), ("Minyak Wangi 50ml", 29, 119), ("Lampu Meja LED", 25, 79),
    ("Power Bank 10000mAh", 39, 119), ("Set Alat Tulis", 9, 29), ("Mainan Kanak-kanak", 19, 89),
    ("Sarung Tangan Motor", 29, 89), ("Blender Mini", 49, 139),
]
PAYMENT_METHODS = ["Online banking (FPX)", "Credit/debit card", "E-wallet", "Cash on delivery"]
TRACKING_PREFIX = {"LajuGo": "LG", "PantasPos": "PP", "SwiftMY": "SM", "KilatExpress": "KE"}

# ------------------------------------------------------------ time helpers
START_DT = datetime.combine(C.START_DATE, time.min)
END_DT = START_DT + timedelta(days=C.N_DAYS)                          # exclusive: 'now' at the end of day 30
INCIDENT_START = START_DT + timedelta(days=C.INCIDENT_DAY - 1)         # 00:00 on the incident day
INCIDENT_ARRIVAL_CUTOFF = START_DT + timedelta(days=C.INCIDENT_LAST_ARRIVAL_DAY)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
_dt, _s = state.to_dt, state.to_str


def day_of(dt: datetime) -> int:
    """day_index (1..N_DAYS) of a datetime."""
    return (dt.date() - C.START_DATE).days + 1


def day_start(day_index: int) -> datetime:
    return START_DT + timedelta(days=day_index - 1)


# ------------------------------------------------ 1. customers and the world
def build_customers(rng: random.Random) -> list[dict]:
    states, weights = list(C.STATE_WEIGHTS), list(C.STATE_WEIGHTS.values())
    customers = []
    for i in range(1, C.N_CUSTOMERS + 1):
        r = rng.random()
        if r < 0.58:
            if rng.random() < 0.5:
                name = f"{rng.choice(MALAY_MALE)} bin {rng.choice(MALAY_FATHERS)}"
            else:
                name = f"{rng.choice(MALAY_FEMALE)} binti {rng.choice(MALAY_FATHERS)}"
        elif r < 0.86:
            name = f"{rng.choice(CHINESE_SURNAMES)} {rng.choice(CHINESE_GIVEN)}"
        elif rng.random() < 0.5:
            name = f"{rng.choice(INDIAN_MALE)} a/l {rng.choice(INDIAN_FATHERS)}"
        else:
            name = f"{rng.choice(INDIAN_FEMALE)} a/p {rng.choice(INDIAN_FATHERS)}"
        tokens = re.sub(r"[^a-z ]", "", name.lower()).split()
        st = rng.choices(states, weights)[0]
        lo, hi = rng.choice(C.STATES[st][1])
        prefix = rng.choice(PHONE_PREFIXES)
        digits = 8 if prefix == "011" else 7
        street = f"Jalan {rng.choice(STREET_WORDS)} {rng.randint(1, 9)}/{rng.randint(1, 12)}"
        customers.append({
            "customer_id": f"CU-{i:05d}",
            "name": name,
            "email": f"{tokens[0]}.{tokens[-1]}{rng.randint(1, 999)}@{rng.choice(EMAIL_DOMAINS)}",
            "phone": f"{prefix}-{rng.randint(10 ** (digits - 1), 10 ** digits - 1)}",
            "address": f"No. {rng.randint(1, 120)}, {street}, Taman {rng.choice(TAMAN_WORDS)}",
            "city": rng.choice(CITIES[st]),
            "state": st,
            "postcode": f"{rng.randint(lo, hi):05d}",
            "preferred_language": rng.choices(C.LANGUAGES, [40, 30, 30])[0],
            "created_at": _s(START_DT - timedelta(days=rng.randint(30, 700))),
        })
    return customers


def _order_datetimes(rng: random.Random) -> list[datetime]:
    """~N_ORDERS timestamps inside the window plus WARMUP_DAYS of earlier orders.

    Slightly busier weekends and paydays (25th-28th). The warm-up orders exist so that
    tickets about parcels/refunds already in flight are present from day 1.
    """
    offsets = range(-C.WARMUP_DAYS, C.N_DAYS)          # day offsets relative to START_DATE
    weights = []
    for d in offsets:
        date = C.START_DATE + timedelta(days=d)
        w = rng.uniform(0.9, 1.1) * (1.15 if date.weekday() >= 5 else 1.0) * (1.2 if 25 <= date.day <= 28 else 1.0)
        weights.append(w)
    total = C.N_ORDERS + round(C.N_ORDERS * C.WARMUP_DAYS / C.N_DAYS)
    days = rng.choices(offsets, weights, k=total)
    stamps = [START_DT + timedelta(days=d, seconds=rng.randint(8 * 3600, 23 * 3600 + 3599)) for d in days]
    return sorted(stamps)


def build_world(rng: random.Random, customers: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Orders, shipments (incl. the Shah Alam incident) and refunds."""
    orders, shipments, used_tracking = [], [], set()
    for i, od in enumerate(_order_datetimes(rng), 1):
        cust = rng.choice(customers)
        lines, subtotal = [], 0.0
        for _ in range(rng.choices([1, 2, 3], [65, 27, 8])[0]):
            name, lo, hi = rng.choice(PRODUCTS)
            qty = rng.choices([1, 2], [85, 15])[0]
            price = float(round(rng.uniform(lo, hi))) - 0.10
            lines.append(f"{name} x{qty}")
            subtotal += price * qty
        fee = 12.0 if cust["state"] in EAST_MALAYSIA else 5.0
        cancelled_at = od + timedelta(minutes=rng.randint(10, 20 * 60)) if rng.random() < C.CANCEL_RATE else None
        orders.append({
            "order_id": f"KK-{i:06d}", "customer_id": cust["customer_id"], "order_date": _s(od),
            "items": ", ".join(lines), "total_rm": round(subtotal + fee, 2),
            "payment_method": rng.choices(PAYMENT_METHODS, [35, 25, 30, 10])[0],
            "ship_state": cust["state"], "ship_postcode": cust["postcode"], "cancelled_at": _s(cancelled_at),
        })
        if cancelled_at:
            continue
        hub = C.STATES[cust["state"]][0]
        shipped = datetime.combine(od.date(), time(17, rng.randint(0, 59))) + DAY * rng.choice([0, 1, 1, 2] if od.hour < 14 else [1, 1, 2])
        arrival = datetime.combine(shipped.date() + DAY, time(rng.randint(5, 9), rng.randint(0, 59)))
        normal_release = arrival + DAY * rng.choices(C.HUB_DWELL_DAYS, C.HUB_DWELL_WEIGHTS)[0]
        transit = rng.choice([2, 3, 4]) if cust["state"] in EAST_MALAYSIA else rng.choice([1, 1, 2])
        normal_delivery = datetime.combine(normal_release.date() + DAY * transit, time(rng.randint(10, 18), rng.randint(0, 59)))
        promised = normal_delivery.date() + DAY

        reason, delay_days, flagged = None, 0, None
        if hub == C.INCIDENT_HUB and normal_release >= INCIDENT_START and arrival < INCIDENT_ARRIVAL_CUTOFF:
            reason, delay_days = "hub_congestion", rng.randint(*C.INCIDENT_DELAY_DAYS)
            flagged = max(arrival + 2 * HOUR, INCIDENT_START + timedelta(minutes=rng.randint(60, 360)))
        else:
            r = rng.random()
            if r < C.LOST_PARCEL_RATE:
                reason, flagged = "lost", normal_release + 2 * DAY
            elif r < C.LOST_PARCEL_RATE + C.BASELINE_DELAY_RATE:
                reason, delay_days, flagged = "courier_delay", rng.randint(2, 4), normal_release
        lost = reason == "lost"
        tracking = None
        while tracking is None or tracking in used_tracking:
            courier = rng.choices(C.COURIERS, [35, 30, 20, 15])[0]
            tracking = f"{TRACKING_PREFIX[courier]}{rng.randint(10 ** 9, 10 ** 10 - 1)}MY"
        used_tracking.add(tracking)
        shipments.append({
            "tracking_no": tracking, "order_id": f"KK-{i:06d}", "courier": courier, "hub": hub,
            "shipped_at": _s(shipped), "hub_arrival_at": _s(arrival), "delay_flagged_at": _s(flagged),
            "hub_release_at": None if lost else _s(normal_release + DAY * delay_days),
            "delivered_at": None if lost else _s(normal_delivery + DAY * delay_days),
            "promised_date": promised.isoformat(), "delay_days": delay_days, "delay_reason": reason,
        })

    ships = {s["order_id"]: s for s in shipments}
    refunds = []
    for o in orders:
        ship = ships.get(o["order_id"])
        if o["cancelled_at"]:
            if rng.random() >= 0.9:
                continue
            requested, reason = _dt(o["cancelled_at"]) + timedelta(minutes=rng.randint(1, 60)), "order_cancelled"
            cancelled = True
        elif ship and ship["delivered_at"] and rng.random() < C.REFUND_RATE:
            reason = rng.choices(["item_damaged", "wrong_item", "not_as_described", "late_delivery"], [35, 25, 25, 15])[0]
            if reason == "late_delivery" and ship["delay_days"] < 2:
                reason = "not_as_described"
            requested = _dt(ship["delivered_at"]) + timedelta(minutes=rng.randint(120, 4 * 24 * 60))
            cancelled = False
        else:
            continue
        if requested >= END_DT:
            continue
        amount = o["total_rm"] if rng.random() < 0.7 else round(o["total_rm"] * rng.uniform(0.3, 0.8), 2)
        approved = cancelled or rng.random() >= 0.08
        decided = requested + timedelta(minutes=rng.randint(12 * 60, 96 * 60))
        refunds.append({
            "order_id": o["order_id"], "amount_rm": amount, "reason": reason, "requested_at": _s(requested),
            "decision": "approved" if approved else "rejected", "decided_at": _s(decided),
            "paid_at": _s(decided + timedelta(minutes=rng.randint(2 * 24 * 60, 6 * 24 * 60))) if approved else None,
        })
    refunds.sort(key=lambda r: r["requested_at"])
    for n, r in enumerate(refunds, 1):
        r["refund_id"] = f"RF-{n:06d}"
    return orders, shipments, refunds


# --------------------------------------- 2. pools of orders fit for a ticket
Item = tuple[str, datetime, datetime]   # (order_id, earliest, latest time a customer might write)

STALE_INTENTS = ("refund_request", "wrong_item", "damaged_item", "change_address", "payment_issue")
INTENT_POOL = {   # normal (non-tricky) intent -> pool of suitable orders; None = no order needed
    "where_is_parcel": "where_is_parcel", "delivery_delay": "courier_delay", "refund_status": "refund_status",
    "refund_request": "refund_request", "cancel_order": "cancel_order", "wrong_item": "wrong_item",
    "damaged_item": "damaged_item", "change_address": "change_address", "payment_issue": "payment_issue",
    "fraud_or_legal": "fraud_or_legal", "general_question": None,
}
BASELINE_INTENT_WEIGHTS = {
    "where_is_parcel": 24, "delivery_delay": 8, "refund_status": 12, "refund_request": 8, "cancel_order": 8,
    "wrong_item": 6, "damaged_item": 8, "change_address": 5, "payment_issue": 6, "fraud_or_legal": 2, "general_question": 13,
}
TRICKY = {   # kind -> (intent, pool). Expected lane is always human_queue.
    "trick_injection": ("refund_request", "refund_request"),
    "trick_legal": ("fraud_or_legal", "fraud_or_legal"),
    "trick_lost": ("where_is_parcel", "lost"),
    "trick_delivered": ("where_is_parcel", "delivered_not_received"),
    "trick_multi": ("damaged_item", "delayed_delivered"),
}
# (intent, lane) -> pools to draw from, for hand-written tickets.
HW_POOLS: dict[tuple[str, str], tuple[str, ...]] = {
    ("where_is_parcel", "autopilot"): ("where_is_parcel",),
    ("where_is_parcel", "human_queue"): ("lost", "delivered_not_received"),
    ("delivery_delay", "autopilot"): ("courier_delay",),
    ("delivery_delay", "cluster_fix"): ("incident",),
    ("delivery_delay", "human_queue"): ("delayed_delivered",),
    ("refund_status", "autopilot"): ("refund_status",),
    ("refund_status", "human_queue"): ("refund_status_rejected",),
    ("general_question", "autopilot"): (),
}
for _intent in ("refund_request", "cancel_order", "wrong_item", "damaged_item", "change_address", "payment_issue", "fraud_or_legal"):
    HW_POOLS[(_intent, "human_queue")] = (INTENT_POOL[_intent],)
for _intent in STALE_INTENTS:
    HW_POOLS[(_intent, "chaser")] = (INTENT_POOL[_intent],)


def build_pools(orders, ships, refunds_by_order) -> tuple[dict[str, list[Item]], dict[str, datetime]]:
    """For each kind of complaint, which orders could truthfully raise it and during which time window.

    Returns (pools, incident): incident maps order_id -> time the hub delay was flagged.
    """
    pools: dict[str, list[Item]] = defaultdict(list)
    incident: dict[str, datetime] = {}

    def add(name: str, oid: str, lo: datetime, hi: datetime) -> None:
        if hi - lo >= HOUR:      # windows may start before day 1 or end after day 30; take() drops those draws
            pools[name].append((oid, lo, hi))

    for o in orders:
        oid, od = o["order_id"], _dt(o["order_date"])
        ship, refund = ships.get(oid), refunds_by_order.get(oid)
        add("payment_issue", oid, od + 5 * MIN, od + 2 * DAY)
        add("fraud_or_legal", oid, od + 2 * HOUR, od + 20 * DAY)
        if ship is None:                                     # cancelled before shipping
            add("cancel_order", oid, od + 10 * MIN, _dt(o["cancelled_at"]) - 5 * MIN)
        else:
            shipped, delivered = _dt(ship["shipped_at"]), _dt(ship["delivered_at"])
            flagged, reason = _dt(ship["delay_flagged_at"]), ship["delay_reason"]
            add("cancel_order", oid, od + 10 * MIN, shipped - HOUR)
            add("change_address", oid, od + 10 * MIN, shipped - HOUR)
            if reason is None:
                add("where_is_parcel", oid, max(shipped + 3 * HOUR, od + HOUR), delivered - 2 * HOUR)
                add("delivered_not_received", oid, delivered + 2 * HOUR, delivered + 3 * DAY)
            elif reason == "courier_delay":
                add("courier_delay", oid, flagged + 2 * HOUR, _dt(ship["hub_release_at"]) - HOUR)   # 'delayed' ends when it leaves the hub
            elif reason == "lost":
                add("lost", oid, flagged + DAY, END_DT)
            else:
                incident[oid] = flagged
            if delivered:
                add("wrong_item", oid, delivered + HOUR, delivered + 5 * DAY)
                add("damaged_item", oid, delivered + HOUR, delivered + 5 * DAY)
                if refund is None:
                    add("refund_request", oid, delivered + 2 * HOUR, delivered + 10 * DAY)
                if reason in ("courier_delay", "hub_congestion"):
                    add("delayed_delivered", oid, delivered + HOUR, delivered + 5 * DAY)
        if refund:
            requested, decided = _dt(refund["requested_at"]), _dt(refund["decided_at"])
            if refund["decision"] == "rejected":
                add("refund_status_rejected", oid, decided + HOUR, decided + 10 * DAY)
            else:
                add("refund_status", oid, requested + DAY, requested + 12 * DAY)
    return pools, incident


# ---------------------------------------------------------- 3. tickets
def load_handwritten(path: Path = C.HANDWRITTEN_CSV) -> list[dict]:
    """Rows of data/handwritten_tickets.csv: text, language, intent, expected_lane.

    Text may use placeholders {order_id} {days} {item} {amount} {hub}; they are filled from
    the order the ticket is attached to, so numbers in the text can never contradict the data.
    """
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"text", "language", "intent", "expected_lane"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
        for n, row in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in row.items()}
            if not any(row.values()):
                continue
            where = f"{path.name} line {n}"
            if not row["text"]:
                raise ValueError(f"{where}: empty text")
            if row["language"] not in C.LANGUAGES:
                raise ValueError(f"{where}: language must be one of {C.LANGUAGES}, got {row['language']!r}")
            if row["intent"] not in C.INTENTS:
                raise ValueError(f"{where}: intent must be one of {C.INTENTS}, got {row['intent']!r}")
            if (row["intent"], row["expected_lane"]) not in HW_POOLS:
                ok = sorted(l for i, l in HW_POOLS if i == row["intent"])
                raise ValueError(f"{where}: lane {row['expected_lane']!r} not supported for intent {row['intent']!r}; use one of {ok}")
            rows.append(row)
    return rows


class TicketFactory:
    """Turns (kind, order, time) into ticket + label rows, consuming each order at most once."""

    def __init__(self, rng: random.Random, customers, orders, ships, refunds_by_order, pools, incident):
        self.rng = rng
        self.cust = {c["customer_id"]: c for c in customers}
        self.orders = {o["order_id"]: o for o in orders}
        self.ships, self.refunds = ships, refunds_by_order
        self.pools, self.incident = pools, incident
        self.used: set[str] = set()
        self.rows: list[dict] = []
        for name in sorted(self.pools):
            self.rng.shuffle(self.pools[name])

    def take(self, pool_names, latest: datetime | None = None) -> tuple[str, datetime] | None:
        """Pop a not-yet-used order from the first pool that has one and draw the ticket time.

        The time is uniform over the order's FULL window. A draw before day 1 or after `latest`
        (default: end of day 30) is dropped: that ticket falls outside the dataset. Clipping the
        window instead would pile tickets onto the first and last days.
        """
        latest = latest or END_DT - MIN
        for name in pool_names:
            pool = self.pools[name]
            while pool:
                oid, lo, hi = pool.pop()
                if oid in self.used:
                    continue
                when = lo + timedelta(seconds=self.rng.randint(0, int((hi - lo).total_seconds())))
                if when > latest or when < START_DT:
                    continue
                self.used.add(oid)
                return oid, when
        return None

    def add(self, *, kind: str, intent: str, lane: str, oid: str | None, when: datetime, tricky: int | None,
            source: str, language: str | None = None, hw_text: str | None = None,
            status: str = "open", note: str | None = None) -> None:
        rng = self.rng
        order = self.orders[oid] if oid else None
        cust = self.cust[order["customer_id"]] if order else rng.choice(list(self.cust.values()))
        if language is None:
            language = cust["preferred_language"] if rng.random() < 0.85 else rng.choice(C.LANGUAGES)
        cluster = C.INCIDENT_CLUSTER if kind == "incident_delay" else f"bg_{intent}"

        # facts the text may quote, taken from the data itself
        days = max(1, (when.date() - _dt(order["order_date"]).date()).days) if order else 1
        if kind == "refund_status" and oid in self.refunds:
            days = max(1, (when.date() - _dt(self.refunds[oid]["requested_at"]).date()).days)
        item = order["items"].split(" x")[0] if order else "item"
        amount = order["total_rm"] if order else 0.0

        urgency = None
        if source == "template":
            urgency = {"fraud_or_legal": "high", "trick_legal": "high", "trick_multi": "high", "trick_injection": "low",
                       "delivery_delay": "medium", "damaged_item": "medium", "wrong_item": "medium",
                       "payment_issue": "medium", "trick_lost": "medium", "trick_delivered": "medium"}.get(kind, "low")
            if kind == "incident_delay":
                urgency = rng.choices(["medium", "high"], [70, 30])[0]
            if urgency == "low" and days >= 7:
                urgency = "medium"

        mention = order is not None and rng.random() < C.MENTION_ORDER_RATE
        if hw_text is not None:
            mention = order is not None and "{order_id}" in hw_text
            text = (hw_text.replace("{order_id}", oid or "").replace("{days}", str(days)).replace("{item}", item)
                    .replace("{amount}", f"{amount:.2f}").replace("{hub}", C.INCIDENT_HUB))
        else:
            text = ticket_text.render(kind, language, rng, oid=oid if mention else None, days=days, item=item,
                                      amount=amount, hub=C.INCIDENT_HUB, urgent=urgency == "high")
        updated = when + timedelta(minutes=rng.randint(120, 480)) if status == "waiting_customer" else when
        self.rows.append({
            "customer_id": cust["customer_id"], "order_id": oid if mention else None,
            "channel": rng.choices(C.CHANNELS, [35, 25, 15, 15, 10])[0],
            "created_at": _s(when), "updated_at": _s(updated), "text": text, "status": status, "source": source,
            "language": language, "intent": intent, "cluster": cluster, "expected_lane": lane,
            "urgency": urgency, "is_tricky": tricky, "linked_order_id": oid, "note": note,
        })


def build_incident_tickets(tf: TicketFactory, hw_incident: list[dict]) -> None:
    """~INCIDENT_TICKETS tickets on days 12-14, each on a shipment genuinely delayed at Shah Alam."""
    rng = tf.rng
    quotas = {d: round(C.INCIDENT_TICKETS * f) for d, f in C.INCIDENT_TICKET_SPLIT.items()}
    last = max(quotas)
    quotas[last] += C.INCIDENT_TICKETS - sum(quotas.values())
    slots: dict[int, list[dict | None]] = {d: [None] * n for d, n in quotas.items()}
    for row in hw_incident:                          # hand-written incident tickets take some of the slots
        days = [d for d in slots if None in slots[d]]
        d = rng.choices(days, [slots[x].count(None) for x in days])[0]
        slots[d][slots[d].index(None)] = row
    remaining = sorted(tf.incident)
    for d in sorted(slots):
        day_end = day_start(d) + DAY
        eligible = [o for o in remaining if tf.incident[o] + HOUR < day_end - 30 * MIN]
        if len(eligible) < len(slots[d]):
            raise RuntimeError(f"day {d}: only {len(eligible)} delayed Shah Alam shipments for {len(slots[d])} tickets; "
                               "widen INCIDENT_LAST_ARRIVAL_DAY or lower INCIDENT_TICKETS in config.py")
        for oid, row in zip(rng.sample(eligible, len(slots[d])), slots[d]):
            remaining.remove(oid)
            tf.used.add(oid)
            lo = max(tf.incident[oid] + HOUR, day_start(d) + 7 * HOUR)
            hi = day_end - 30 * MIN
            when = lo + timedelta(seconds=rng.randint(0, int((hi - lo).total_seconds())))
            if row is None:
                tf.add(kind="incident_delay", intent="delivery_delay", lane="cluster_fix", oid=oid, when=when,
                       tricky=0, source="template", note="incident")
            else:
                tf.add(kind="incident_delay", intent="delivery_delay", lane="cluster_fix", oid=oid, when=when,
                       tricky=None, source="handwritten", language=row["language"], hw_text=row["text"], note="handwritten")


def build_handwritten_tickets(tf: TicketFactory, rows: list[dict]) -> None:
    for n, row in enumerate(rows, 1):
        intent, lane = row["intent"], row["expected_lane"]
        pools = HW_POOLS[(intent, lane)]
        stale = lane == "chaser"
        item = None
        if pools or intent != "general_question":
            item = tf.take(pools, latest=END_DT - 4 * DAY if stale else None)
            if item is None:
                raise RuntimeError(f"no order available for hand-written row {n} ({intent}/{lane})")
            oid, when = item
        else:
            oid, when = None, START_DT + timedelta(seconds=tf.rng.randint(0, int((END_DT - START_DT).total_seconds()) - 60))
        tf.add(kind=intent, intent=intent, lane=lane, oid=oid, when=when, tricky=None, source="handwritten",
               language=row["language"], hw_text=row["text"], note="handwritten",
               status="waiting_customer" if stale else "open")


def build_baseline_tickets(tf: TicketFactory, target: int) -> None:
    rng = tf.rng
    intents, weights = list(BASELINE_INTENT_WEIGHTS), list(BASELINE_INTENT_WEIGHTS.values())
    made = attempts = 0
    while made < target and attempts < target * 20:
        attempts += 1
        r = rng.random()
        if r < C.STALE_WAITING_RATE:                                  # waiting on the customer -> chaser lane
            intent = rng.choice(STALE_INTENTS)
            item = tf.take((INTENT_POOL[intent],), latest=END_DT - 4 * DAY)
            spec = dict(kind=intent, intent=intent, lane="chaser", tricky=0, status="waiting_customer", note="stale")
        elif r < C.STALE_WAITING_RATE + C.TRICKY_RATE:                # deliberately tricky -> human queue
            kind = rng.choice(sorted(TRICKY))
            intent, pool = TRICKY[kind]
            item = tf.take((pool,))
            spec = dict(kind=kind, intent=intent, lane="human_queue", tricky=1, note=kind)
        else:
            intent = rng.choices(intents, weights)[0]
            pool = INTENT_POOL[intent]
            item = tf.take((pool,)) if pool else None
            lane = "autopilot" if intent in ("where_is_parcel", "delivery_delay", "refund_status", "general_question") else "human_queue"
            spec = dict(kind=intent, intent=intent, lane=lane, tricky=0)
        if spec["intent"] != "general_question":
            if item is None:
                continue
            oid, when = item
        else:
            oid, when = None, START_DT + timedelta(seconds=rng.randint(0, int((END_DT - START_DT).total_seconds()) - 60))
        tf.add(oid=oid, when=when, source="template", **spec)
        made += 1


# ----------------------------------------------------------- 4. writing
def write_db(customers, orders, shipments, refunds, tickets) -> None:
    C.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if C.DB_PATH.exists():
        print(f"Overwriting existing database: {C.DB_PATH}")
        C.DB_PATH.unlink()
    conn = db.connect_admin()
    conn.executescript(SCHEMA)

    def insert(table: str, rows: list[dict], cols: list[str]) -> None:
        conn.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [[r[c] for c in cols] for r in rows])

    insert("customers", customers, ["customer_id", "name", "email", "phone", "address", "city", "state", "postcode", "preferred_language", "created_at"])
    insert("orders", orders, ["order_id", "customer_id", "order_date", "items", "total_rm", "payment_method", "ship_state", "ship_postcode", "cancelled_at"])
    insert("shipments", shipments, ["tracking_no", "order_id", "courier", "hub", "shipped_at", "hub_arrival_at", "delay_flagged_at", "hub_release_at", "delivered_at", "promised_date", "delay_days", "delay_reason"])
    insert("refunds", refunds, ["refund_id", "order_id", "amount_rm", "reason", "requested_at", "decision", "decided_at", "paid_at"])
    insert("tickets", tickets, ["ticket_id", "customer_id", "order_id", "channel", "created_at", "updated_at", "text", "status", "lane", "source"])
    insert("ticket_labels", tickets, ["ticket_id", "language", "intent", "cluster", "expected_lane", "urgency", "is_tricky", "linked_order_id", "note"])
    conn.commit()
    conn.close()


# ----------------------------------------------- 5. validation (second pass)
def validate(conn: sqlite3.Connection) -> list[str]:
    """Re-check every ticket against the stored data, independently of how it was generated."""
    orders = {r["order_id"]: dict(r) for r in conn.execute("SELECT * FROM orders")}
    ships = {r["order_id"]: dict(r) for r in conn.execute("SELECT * FROM shipments")}
    refunds = {r["order_id"]: dict(r) for r in conn.execute("SELECT * FROM refunds")}
    errors: list[str] = []
    seen_orders: set[str] = set()
    incident_count = 0
    query = """SELECT t.*, l.language, l.intent, l.cluster, l.expected_lane, l.linked_order_id, l.note
               FROM tickets t JOIN ticket_labels l USING (ticket_id)"""
    for row in conn.execute(query):
        t = dict(row)
        tid, intent, lane, oid = t["ticket_id"], t["intent"], t["expected_lane"], t["linked_order_id"]
        when = _dt(t["created_at"])
        bad = lambda msg: errors.append(f"{tid} ({intent}/{lane}): {msg}")  # noqa: E731

        if not (START_DT <= when < END_DT):
            bad("created outside the 30-day window")
        if (t["status"] == "waiting_customer") != (lane == "chaser"):
            bad("chaser lane and waiting_customer status disagree")
        if t["lane"] is not None:
            bad("lane must be empty until the agent routes the ticket")
        if oid is None:
            if intent != "general_question":
                bad("only general_question tickets may have no order")
            if t["order_id"] or re.search(r"KK-\d{6}", t["text"]):
                bad("ticket without an order quotes an order ID")
            continue
        if oid in seen_orders:
            bad(f"order {oid} used by more than one ticket")
        seen_orders.add(oid)
        order, ship, refund = orders.get(oid), ships.get(oid), refunds.get(oid)
        if order is None:
            bad(f"unknown order {oid}")
            continue
        if order["customer_id"] != t["customer_id"]:
            bad("customer does not own the linked order")
        if when < _dt(order["order_date"]):
            bad("ticket created before the order")
            continue
        quoted = re.findall(r"KK-\d{6}", t["text"])
        if t["order_id"]:
            if t["order_id"] != oid or quoted != [oid]:
                bad(f"quoted order ID {quoted} does not match linked order {oid}")
        elif quoted:
            bad("text quotes an order ID but tickets.order_id is empty")

        tr = state.tracking_state(ship, when) if ship else None
        status = tr["status"] if tr else None
        ostatus = state.order_status(order, ship, when)
        rs = state.refund_state(refund, when) if refund else None
        reason = ship["delay_reason"] if ship else None

        if t["cluster"] == C.INCIDENT_CLUSTER:
            incident_count += 1
            if not (ship and ship["hub"] == C.INCIDENT_HUB and reason == "hub_congestion" and status == "delayed"):
                bad("incident ticket is not on a shipment delayed at the Shah Alam hub at ticket time")
            if day_of(when) not in C.INCIDENT_TICKET_SPLIT:
                bad("incident ticket outside the incident days")
        elif intent == "where_is_parcel":
            if lane == "autopilot" and status not in state.IN_TRANSIT:
                bad(f"status question but shipment is {status}")
            if lane == "human_queue" and not (status == "delivered" or (status == "delayed" and reason == "lost")):
                bad(f"exception case needs a lost or delivered shipment, got {status}/{reason}")
        elif intent == "delivery_delay":
            if lane == "autopilot" and not (status == "delayed" and reason == "courier_delay"):
                bad(f"delay ticket but shipment is {status}/{reason}")
            if lane == "human_queue" and not (status == "delivered" and ship["delay_days"] >= 2):
                bad("late-and-delivered case needs a delayed delivered shipment")
        elif intent == "refund_status":
            if rs is None:
                bad("refund-status ticket but the order has no refund yet")
            elif (lane == "autopilot") == (rs["status"] == "rejected"):
                bad(f"lane {lane} does not fit refund status {rs['status']}")
        elif intent in ("refund_request", "wrong_item", "damaged_item"):
            if ostatus != "delivered":
                bad(f"order is {ostatus}, not delivered")
            if intent == "refund_request" and rs is not None:
                bad("refund already exists for this order")
        elif intent in ("cancel_order", "change_address"):
            if ostatus != "placed":
                bad(f"order is {ostatus}, must still be unshipped")
            if intent == "change_address" and ship is None:
                bad("change_address needs a shipment that has not left yet")
        # payment_issue and fraud_or_legal only need an existing order (checked above)

    if incident_count != C.INCIDENT_TICKETS:
        errors.append(f"expected {C.INCIDENT_TICKETS} incident tickets, found {incident_count}")
    n_t = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    n_l = conn.execute("SELECT COUNT(*) FROM ticket_labels").fetchone()[0]
    if n_t != n_l:
        errors.append(f"{n_t} tickets but {n_l} labels")
    return errors


def summarize(conn: sqlite3.Connection) -> None:
    in_window = conn.execute("SELECT COUNT(*) FROM orders WHERE order_date >= ?", (_s(START_DT),)).fetchone()[0]
    print(f"\nOrders inside the 30-day window: {in_window} (plus {C.WARMUP_DAYS} warm-up days of earlier orders)")
    print("Rows per table")
    for table in ("customers", "orders", "shipments", "refunds", "tickets", "ticket_labels", "audit_log"):
        print(f"  {table:14} {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>6}")
    joined = "FROM tickets t JOIN ticket_labels l USING (ticket_id)"
    for title, col in (("source", "t.source"), ("language", "l.language"), ("intent", "l.intent"),
                       ("expected lane", "l.expected_lane"), ("tricky", "l.is_tricky")):
        counts = conn.execute(f"SELECT {col}, COUNT(*) {joined} GROUP BY {col} ORDER BY 2 DESC").fetchall()
        print(f"\nTickets by {title}\n  " + "   ".join(f"{k}: {v}" for k, v in counts))
    print("\nTickets per day (# = 5 tickets; * = incident cluster)")
    per_day = defaultdict(lambda: [0, 0])
    for created, cluster in conn.execute(f"SELECT t.created_at, l.cluster {joined}"):
        per_day[day_of(_dt(created))][cluster == C.INCIDENT_CLUSTER] += 1
    for d in range(1, C.N_DAYS + 1):
        normal, inc = per_day[d][0], per_day[d][1]
        bar = "#" * round(normal / 5) + "*" * round(inc / 5)
        print(f"  day {d:>2} {(START_DT + timedelta(days=d - 1)):%a %d %b}  {normal + inc:>4}  {bar}")
    late = conn.execute("SELECT COUNT(*) FROM shipments WHERE delay_reason = 'hub_congestion'").fetchone()[0]
    print(f"\nShipments held by the Shah Alam incident: {late} (tickets on them: {C.INCIDENT_TICKETS})")


def main() -> None:
    rng = random.Random(C.SEED)
    hw_rows = load_handwritten()
    print(f"Hand-written tickets loaded: {len(hw_rows)}  ({C.HANDWRITTEN_CSV.name})")

    customers = build_customers(rng)
    orders, shipments, refunds = build_world(rng, customers)
    ships = {s["order_id"]: s for s in shipments}
    refunds_by_order = {r["order_id"]: r for r in refunds}
    pools, incident = build_pools(orders, ships, refunds_by_order)
    tf = TicketFactory(rng, customers, orders, ships, refunds_by_order, pools, incident)

    hw_incident = [r for r in hw_rows if r["expected_lane"] == "cluster_fix"]
    hw_other = [r for r in hw_rows if r["expected_lane"] != "cluster_fix"]
    build_incident_tickets(tf, hw_incident)
    build_handwritten_tickets(tf, hw_other)
    build_baseline_tickets(tf, max(0, round(C.N_ORDERS * C.BASELINE_TICKET_RATE) - len(hw_other)))

    tickets = sorted(tf.rows, key=lambda r: (r["created_at"], r["text"]))
    for n, t in enumerate(tickets, 1):
        t["ticket_id"], t["lane"] = f"TK-{n:05d}", None
    write_db(customers, orders, shipments, refunds, tickets)

    conn = db.connect_admin()
    errors = validate(conn)
    summarize(conn)
    conn.close()
    if errors:
        print(f"\nVALIDATION FAILED ({len(errors)} problems):")
        print("\n".join("  " + e for e in errors[:25]))
        sys.exit(1)
    print(f"\nValidation passed: all {len(tickets)} tickets match the data. Database: {C.DB_PATH}")


if __name__ == "__main__":
    main()
