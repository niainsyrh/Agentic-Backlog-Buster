"""Central configuration for Backlog Buster.

Every tunable number lives here so nothing is scattered across modules.
All data described here is SYNTHETIC (KedaiKita is a fictional company).

Run `python src/config.py` to print the settings and run sanity checks.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------- paths
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "db" / "backlog.db"))
HANDWRITTEN_CSV = ROOT / "data" / "handwritten_tickets.csv"
MOCK_API_URL = os.getenv("MOCK_API_URL", "http://127.0.0.1:8000")

# ------------------------------------------------------- data generation
SEED = 42                        # fixed seed -> identical data every run
START_DATE = date(2026, 1, 1)    # day_index 1 == this date
N_DAYS = 30
N_ORDERS = 5000                  # orders placed inside the 30-day window
WARMUP_DAYS = 14                 # extra days of orders BEFORE day 1, so day-1 ticket volume is already steady
N_CUSTOMERS = 5000
CANCEL_RATE = 0.03               # share of orders cancelled before shipping
REFUND_RATE = 0.035              # share of delivered orders that get a refund
BASELINE_DELAY_RATE = 0.04       # normal courier delays outside the incident
LOST_PARCEL_RATE = 0.004
HUB_DWELL_DAYS = (1, 2, 3)       # days a parcel normally sits at a sorting hub
HUB_DWELL_WEIGHTS = (5, 3, 2)
BASELINE_TICKET_RATE = 0.08      # share of orders that raise a normal ticket
MENTION_ORDER_RATE = 0.65        # share of tickets whose text quotes the order ID
TRICKY_RATE = 0.10               # share of baseline tickets that are deliberately tricky
STALE_WAITING_RATE = 0.06        # share of baseline tickets left "waiting_customer"

# Planned incident: Shah Alam hub delay
INCIDENT_HUB = "Shah Alam"
INCIDENT_DAY = 12                # day_index the delay starts
INCIDENT_LAST_ARRIVAL_DAY = 14   # parcels reaching the hub after this day are not held
INCIDENT_TICKETS = 300
INCIDENT_TICKET_SPLIT = {12: 0.60, 13: 0.30, 14: 0.10}   # must sum to 1.0
INCIDENT_DELAY_DAYS = (3, 6)     # extra days added to affected shipments
INCIDENT_CLUSTER = "shah_alam_delay_d12"

# What "now" means for the mock API when a request has no ?as_of=. Default: end of day 30.
# Set MOCK_API_AS_OF="2026-01-12 10:00:00" in .env to pin the API to a moment (e.g. the spike).
DEFAULT_AS_OF = (datetime.fromisoformat(os.environ["MOCK_API_AS_OF"]) if os.getenv("MOCK_API_AS_OF")
                 else datetime.combine(START_DATE, time.min) + timedelta(days=N_DAYS))

# --------------------------------------------------- agent safety limits
# These are enforced in Python (tools/router), never only in a prompt.
REFUND_AUTO_LIMIT_RM = 50.0      # above this a refund is never automatic
CONFIDENCE_THRESHOLD = 0.85      # below this -> human queue
MAX_STEPS_PER_TICKET = 6
DAILY_LLM_BUDGET_USD = 5.0
API_TIMEOUT_S = 5.0
CIRCUIT_BREAKER_FAILURES = 3     # consecutive order-API failures before autopilot stops

# ------------------------------------------------------- shared vocab
LANES = ("autopilot", "cluster_fix", "chaser", "human_queue")
LANGUAGES = ("ms", "en", "mixed")            # mixed == Manglish / code-switched
CHANNELS = ("chat", "email", "telegram", "social", "form")
SOURCES = ("template", "handwritten")        # how a ticket's text was produced
TICKET_STATUSES = ("open", "waiting_customer", "resolved", "closed")
INTENTS = (
    "where_is_parcel",   # status question
    "delivery_delay",    # complaint that it is late
    "refund_status",     # where is my refund
    "refund_request",    # please refund me
    "cancel_order",
    "wrong_item",
    "damaged_item",
    "change_address",
    "payment_issue",
    "fraud_or_legal",    # never automated
    "general_question",  # policy / how-to
)

# ---------------------------------------------- fictional logistics network
HUBS = ("Shah Alam", "Penang", "Johor Bahru", "Kuching", "Kota Kinabalu")
COURIERS = ("LajuGo", "PantasPos", "SwiftMY", "KilatExpress")   # all fictional

# state -> (sorting hub, list of inclusive postcode ranges). Postcodes are
# realistic ranges but the addresses built from them are made up.
STATES: dict[str, tuple[str, list[tuple[int, int]]]] = {
    "Selangor":         ("Shah Alam",       [(40000, 48999)]),
    "Kuala Lumpur":     ("Shah Alam",       [(50000, 60000)]),
    "Putrajaya":        ("Shah Alam",       [(62000, 62999)]),
    "Pahang":           ("Shah Alam",       [(25000, 28999)]),
    "Terengganu":       ("Shah Alam",       [(20000, 24999)]),
    "Kelantan":         ("Shah Alam",       [(15000, 18999)]),
    "Pulau Pinang":     ("Penang",          [(10000, 14999)]),
    "Kedah":            ("Penang",          [(5000, 9999)]),
    "Perlis":           ("Penang",          [(1000, 2999)]),
    "Perak":            ("Penang",          [(30000, 36999)]),
    "Johor":            ("Johor Bahru",     [(79000, 86999)]),
    "Melaka":           ("Johor Bahru",     [(75000, 78999)]),
    "Negeri Sembilan":  ("Johor Bahru",     [(70000, 73999)]),
    "Sarawak":          ("Kuching",         [(93000, 98999)]),
    "Sabah":            ("Kota Kinabalu",   [(87000, 91999)]),
}
# Relative order volume per state (roughly follows population / e-commerce use).
STATE_WEIGHTS: dict[str, float] = {
    "Selangor": 22, "Kuala Lumpur": 12, "Johor": 12, "Pulau Pinang": 8, "Perak": 7,
    "Sabah": 6, "Sarawak": 6, "Kedah": 5, "Pahang": 4, "Kelantan": 3,
    "Terengganu": 3, "Negeri Sembilan": 4, "Melaka": 4, "Putrajaya": 2, "Perlis": 1,
}


def _sanity_check() -> None:
    assert abs(sum(INCIDENT_TICKET_SPLIT.values()) - 1.0) < 1e-9, "incident split must sum to 1"
    assert INCIDENT_DAY in INCIDENT_TICKET_SPLIT, "incident day must be in the split"
    assert 1 <= INCIDENT_DAY <= N_DAYS
    assert INCIDENT_HUB in HUBS
    assert set(h for h, _ in STATES.values()) <= set(HUBS)
    assert set(STATES) == set(STATE_WEIGHTS)
    for _, ranges in STATES.values():
        for lo, hi in ranges:
            assert 0 < lo < hi <= 99999
    assert WARMUP_DAYS >= 0
    assert 0 < REFUND_AUTO_LIMIT_RM <= 50, "CLAUDE.md caps automatic refunds at RM50"


if __name__ == "__main__":
    _sanity_check()
    print("ROOT               :", ROOT)
    print("DB_PATH            :", DB_PATH)
    print("START_DATE / DAYS  :", START_DATE, "/", N_DAYS)
    print("N_ORDERS / SEED    :", N_ORDERS, "/", SEED)
    print("API default as_of  :", DEFAULT_AS_OF)
    print("Incident           :", INCIDENT_HUB, "day", INCIDENT_DAY, "~", INCIDENT_TICKETS, "tickets")
    print("Refund auto limit  : RM", REFUND_AUTO_LIMIT_RM)
    print("Confidence thresh. :", CONFIDENCE_THRESHOLD)
    print("Sanity checks passed.")
