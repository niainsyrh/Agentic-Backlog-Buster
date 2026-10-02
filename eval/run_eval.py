"""Evaluate the understand step against the hidden ground-truth labels.

This is the only place (besides the data generator) allowed to read the answer key. The agent code
cannot: db.connect_agent blocks it. The script runs understand() on tickets, compares each answer
with the label, and prints accuracy overall and by language / source / tricky, a confusion list,
order-ID extraction, prompt-injection detection and a look at confidence.

    python eval/run_eval.py --sample 30      small, seeded, stratified sample (good while developing)
    python eval/run_eval.py --all            every ticket (~700; check the quota estimate first)
    python eval/run_eval.py --selftest       offline checks of the sampler and the metrics

Options: --seed N (default 42)   --show-errors N (default 10)

Cheap re-runs: LLM answers are cached on disk (src/agent/llm.py), so a repeated run, or a bigger
--sample that contains earlier tickets, only pays for the new tickets. Stop with Ctrl+C at any time
and run the same command again: it resumes from the cache.

Results are written to eval/results/ (git-ignored): last_run.csv (per ticket) and last_run_summary.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config as C  # noqa: E402
import db  # noqa: E402
from agent.llm import DailyCapReached, LLMClient  # noqa: E402
from agent.understand import (MAX_TEXT_CHARS, PROMPT_VERSION, SCHEMA, SYSTEM_PROMPT, Understanding,  # noqa: E402
                              build_prompt, understand)

RESULTS_DIR = ROOT / "eval" / "results"


# ------------------------------------------------------------------ data
def load_tickets(conn) -> list[dict]:
    """Every ticket with its ground truth (admin connection: this script may read the answer key)."""
    query = """SELECT t.ticket_id, t.text, t.source, t.order_id AS quoted_order_id,
                      l.language, l.intent, l.urgency, l.is_tricky, l.note, l.cluster
               FROM tickets t JOIN ticket_labels l USING (ticket_id) ORDER BY t.ticket_id"""
    return [dict(r) for r in conn.execute(query)]


def select_sample(rows: list[dict], n: int, seed: int = 42) -> list[dict]:
    """A seeded sample that covers every intent, language, source, tricky and incident tickets.

    Round-robin over intents; inside an intent, round-robin over (language, incident, tricky, source)
    groups. So it is NOT proportional to the population: it favours coverage, which is what you want
    while developing. Use --all for headline numbers. Same seed -> same sample; a bigger n only adds tickets.
    """
    rng = random.Random(seed)
    by_intent: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["ticket_id"]):
        by_intent[r["intent"]].append(r)
    queues: dict[str, list[dict]] = {}
    for intent in sorted(by_intent):
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for r in by_intent[intent]:
            groups[(r["language"], r["cluster"] == C.INCIDENT_CLUSTER, bool(r["is_tricky"]), r["source"])].append(r)
        keys = sorted(groups)
        rng.shuffle(keys)
        for k in keys:
            rng.shuffle(groups[k])
        queue: list[dict] = []
        while any(groups.values()):
            for k in keys:
                if groups[k]:
                    queue.append(groups[k].pop())
        queues[intent] = queue
    order = sorted(queues)
    rng.shuffle(order)
    picked: list[dict] = []
    while len(picked) < n and any(queues.values()):
        for intent in order:
            if queues[intent] and len(picked) < n:
                picked.append(queues[intent].pop(0))
    return picked


# ------------------------------------------------------------------ running
def _request_text(row: dict) -> str:
    return row["text"].strip()[:MAX_TEXT_CHARS]


def preflight(rows: list[dict], client: LLMClient) -> tuple[int, int]:
    """(tickets already cached, tickets that need a real API call)."""
    cached = sum(client.is_cached(build_prompt(_request_text(r)), system=SYSTEM_PROMPT, schema=SCHEMA) for r in rows)
    return cached, len(rows) - cached


def run(rows: list[dict], client: LLMClient, verbose: bool = True) -> tuple[list[dict], bool]:
    """Run understand() on each row. Returns (results, completed); completed is False if the daily cap stopped it."""
    results: list[dict] = []
    for i, row in enumerate(rows, 1):
        calls_before = client.stats["api_calls"]
        try:
            pred = understand(row["text"], client)
        except DailyCapReached as exc:
            print(f"\nStopped: {exc}\nResults so far are kept; run again tomorrow, cached tickets stay free.")
            return results, False
        results.append({**row, "pred": pred})
        if verbose:
            hit = pred.ok and pred.intent == row["intent"]
            source = "api" if client.stats["api_calls"] > calls_before else "cache"
            print(f"[{i:>3}/{len(rows)}] {row['ticket_id']} {'ok  ' if hit else 'MISS'} "
                  f"{row['intent']} -> {pred.intent or 'FAILED'}  ({source})", flush=True)
    return results, True


# ------------------------------------------------------------------ metrics
def _acc(hits: int, total: int) -> float | None:
    return hits / total if total else None


def compute_metrics(results: list[dict]) -> dict:
    """All scores from a list of {row fields..., 'pred': Understanding}. A failed prediction counts as wrong."""
    def correct(r: dict) -> bool:
        return r["pred"].ok and r["pred"].intent == r["intent"]

    m: dict = {"n": len(results), "failed": sum(not r["pred"].ok for r in results)}
    m["intent"] = _acc(sum(map(correct, results)), len(results))
    for key, values in (("by_language", C.LANGUAGES), ("by_source", C.SOURCES)):
        field = "language" if key == "by_language" else "source"
        m[key] = {}
        for v in values:
            subset = [r for r in results if r[field] == v]
            m[key][v] = {"n": len(subset), "acc": _acc(sum(map(correct, subset)), len(subset))}
    for name, flag in (("normal", 0), ("tricky", 1)):
        subset = [r for r in results if r["is_tricky"] == flag]
        m[f"intent_{name}"] = {"n": len(subset), "acc": _acc(sum(map(correct, subset)), len(subset))}

    m["by_intent"] = {}
    for intent in C.INTENTS:
        subset = [r for r in results if r["intent"] == intent]
        if subset:
            m["by_intent"][intent] = {"n": len(subset), "acc": _acc(sum(map(correct, subset)), len(subset))}

    ok = [r for r in results if r["pred"].ok]
    m["language_acc"] = _acc(sum(r["pred"].language == r["language"] for r in ok), len(ok))
    rated = [r for r in ok if r["urgency"] is not None]
    m["urgency"] = {"n": len(rated), "acc": _acc(sum(r["pred"].urgency == r["urgency"] for r in rated), len(rated))}
    with_id = [r for r in results if r["quoted_order_id"]]
    without_id = [r for r in results if not r["quoted_order_id"]]
    m["order_id_found"] = {"n": len(with_id), "acc": _acc(sum(r["pred"].order_id == r["quoted_order_id"] for r in with_id), len(with_id))}
    m["order_id_absent"] = {"n": len(without_id), "acc": _acc(sum(r["pred"].order_id is None for r in without_id), len(without_id))}

    injected = [r for r in results if r["note"] == "trick_injection"]
    others = [r for r in results if r["note"] != "trick_injection"]
    flag = lambda r: "prompt_injection" in r["pred"].flags  # noqa: E731
    m["injection"] = {"n": len(injected), "recall": _acc(sum(map(flag, injected)), len(injected)),
                      "false_alarms": sum(map(flag, others)), "others": len(others)}

    right = [r["pred"].confidence for r in ok if correct(r)]
    wrong = [r["pred"].confidence for r in ok if not correct(r)]
    hi = [r for r in ok if r["pred"].confidence >= C.CONFIDENCE_THRESHOLD]
    lo = [r for r in ok if r["pred"].confidence < C.CONFIDENCE_THRESHOLD]
    m["confidence"] = {
        "mean_when_right": sum(right) / len(right) if right else None,
        "mean_when_wrong": sum(wrong) / len(wrong) if wrong else None,
        "threshold": C.CONFIDENCE_THRESHOLD,
        "share_below_threshold": _acc(len(lo), len(ok)),
        "acc_at_or_above": _acc(sum(map(correct, hi)), len(hi)),
        "acc_below": _acc(sum(map(correct, lo)), len(lo)),
    }
    confusions = Counter((r["intent"], r["pred"].intent or "FAILED") for r in results if not correct(r))
    m["confusions"] = [{"true": t, "predicted": p, "count": c} for (t, p), c in confusions.most_common(10)]
    return m


# ------------------------------------------------------------------ output
def _p(x: float | None) -> str:
    return "  n/a" if x is None else f"{100 * x:5.1f}%"


def _r(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def print_report(m: dict, results: list[dict], show_errors: int) -> None:
    print(f"\n=== Understand step: {m['n']} tickets, model {C.LLM_MODEL}, prompt {PROMPT_VERSION} ===")
    print(f"Intent accuracy        {_p(m['intent'])}   ({m['failed']} failed calls counted as wrong)")
    for lang, v in m["by_language"].items():
        label = {"ms": "Malay", "en": "English", "mixed": "Manglish"}[lang]
        print(f"  {label:<10}           {_p(v['acc'])}   (n={v['n']})")
    for src, v in m["by_source"].items():
        print(f"  {src:<12}         {_p(v['acc'])}   (n={v['n']})")
    print(f"  normal tickets       {_p(m['intent_normal']['acc'])}   (n={m['intent_normal']['n']})")
    print(f"  deliberately tricky  {_p(m['intent_tricky']['acc'])}   (n={m['intent_tricky']['n']})")
    print("By intent: " + "  ".join(f"{k} {_p(v['acc']).strip()} ({v['n']})" for k, v in m["by_intent"].items()))
    print(f"Language accuracy      {_p(m['language_acc'])}")
    print(f"Urgency accuracy       {_p(m['urgency']['acc'])}   (n={m['urgency']['n']}, hand-written tickets have no urgency label)")
    print(f"Order ID found         {_p(m['order_id_found']['acc'])}   (n={m['order_id_found']['n']} tickets quote one)")
    print(f"Order ID absent        {_p(m['order_id_absent']['acc'])}   (n={m['order_id_absent']['n']} tickets quote none; no invented IDs)")
    inj = m["injection"]
    print(f"Injection flagged      {_p(inj['recall'])}   (n={inj['n']}); false alarms on other tickets: {inj['false_alarms']}/{inj['others']}")
    c = m["confidence"]
    print(f"Confidence             mean {_r(c['mean_when_right'])} when right, {_r(c['mean_when_wrong'])} when wrong; "
          f"{_p(c['share_below_threshold']).strip()} below the {c['threshold']} threshold would go to a human")
    print(f"  accuracy at/above threshold {_p(c['acc_at_or_above'])}, below {_p(c['acc_below'])}")
    if m["confusions"]:
        print("Most common mix-ups (true -> predicted):")
        for x in m["confusions"]:
            print(f"  {x['count']:>3} x {x['true']} -> {x['predicted']}")
    misses = [r for r in results if not (r["pred"].ok and r["pred"].intent == r["intent"])]
    if misses and show_errors:
        print(f"\nFirst {min(show_errors, len(misses))} misclassified tickets:")
        for r in misses[:show_errors]:
            note = f" [{r['note']}]" if r["note"] else ""
            err = r["pred"].error or ""
            print(f"  {r['ticket_id']} true={r['intent']} pred={r['pred'].intent} conf={r['pred'].confidence}{note} {err}\n      {r['text']}")


def save_results(results: list[dict], m: dict, args: argparse.Namespace) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["ticket_id", "source", "note", "true_language", "pred_language", "true_intent", "pred_intent", "true_urgency",
            "pred_urgency", "confidence", "quoted_order_id", "pred_order_id", "flags", "ok", "error", "text"]
    with (RESULTS_DIR / "last_run.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in results:
            p: Understanding = r["pred"]
            w.writerow([r["ticket_id"], r["source"], r["note"], r["language"], p.language, r["intent"], p.intent, r["urgency"],
                        p.urgency, p.confidence, r["quoted_order_id"], p.order_id, "|".join(p.flags), p.ok, p.error, r["text"]])
    summary = {"when": datetime.now().isoformat(timespec="seconds"), "model": C.LLM_MODEL, "prompt_version": PROMPT_VERSION,
               "mode": "all" if args.all else f"sample {args.sample}", "seed": args.seed, "metrics": m}
    (RESULTS_DIR / "last_run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSaved eval/results/last_run.csv and last_run_summary.json")


# ------------------------------------------------------------------ self-test
def _selftest() -> None:
    langs = C.LANGUAGES
    rows = []
    for intent in C.INTENTS:
        for i in range(12):
            rows.append({"ticket_id": f"T-{intent[:4]}-{i:02d}", "text": f"{intent} sample {i}", "source": "handwritten" if i == 0 else "template",
                         "quoted_order_id": "KK-000001" if i % 2 else None, "language": langs[i % 3], "intent": intent,
                         "urgency": "low" if i else None, "is_tricky": 1 if i == 5 else (None if i == 0 else 0),
                         "note": "trick_injection" if (intent == "refund_request" and i == 5) else None,
                         "cluster": C.INCIDENT_CLUSTER if (intent == "delivery_delay" and i in (1, 2)) else f"bg_{intent}"})
    s1, s2 = select_sample(rows, 30, seed=1), select_sample(rows, 30, seed=1)
    assert [r["ticket_id"] for r in s1] == [r["ticket_id"] for r in s2] and len({r["ticket_id"] for r in s1}) == 30
    assert {r["intent"] for r in s1} == set(C.INTENTS) and {r["language"] for r in s1} == set(langs)
    assert [r["ticket_id"] for r in select_sample(rows, 30, seed=2)] != [r["ticket_id"] for r in s1]
    assert len(select_sample(rows, 10_000)) == len(rows) and len(select_sample(rows, 0)) == 0
    bigger = {r["ticket_id"] for r in select_sample(rows, 60, seed=1)}
    assert {r["ticket_id"] for r in s1} <= bigger, "a bigger sample must contain the smaller one (cache reuse)"
    print("ok  sampler: seeded, covers all 11 intents and 3 languages at n=30, bigger sample contains smaller one")

    class Echo:   # fake LLM: predicts the intent named by the first word of the ticket, so accuracy is known
        stats = {"api_calls": 0}
        def generate_json(self, prompt, *, system=None, schema=None, label=""):
            text = prompt.split("<ticket>\n", 1)[1].split("\n</ticket>", 1)[0]
            word = text.split()[0]
            return {"intent": word, "language": "ms", "urgency": "low", "confidence": 0.9 if word != "general_question" else 0.4,
                    "injection_suspected": False}
    subset = rows[:24]                                   # first two intents
    fake_rows = [dict(r, text=(r["text"] if i % 4 else r["text"].replace(r["intent"], "wrong_item", 1))) for i, r in enumerate(subset)]
    results, done = run(fake_rows, Echo(), verbose=False)
    assert done and len(results) == 24
    m = compute_metrics(results)
    expected_hits = sum(1 for i, r in enumerate(subset) if i % 4 != 0 or r["intent"] == "wrong_item")   # text was swapped to "wrong_item" on every 4th
    assert abs(m["intent"] - expected_hits / 24) < 1e-9, (m["intent"], expected_hits)
    assert m["failed"] == 0 and m["by_language"]["ms"]["n"] + m["by_language"]["en"]["n"] + m["by_language"]["mixed"]["n"] == 24
    print(f"ok  metrics: known accuracy {expected_hits}/24 reproduced exactly")

    failing = [{**rows[0], "pred": Understanding(ok=False, error="x")}, {**rows[1], "pred": Understanding(ok=True, intent=rows[1]["intent"], language="ms", urgency="low", confidence=0.9)}]
    mf = compute_metrics(failing)
    assert mf["failed"] == 1 and mf["intent"] == 0.5 and mf["confusions"][0]["predicted"] == "FAILED"
    inj = [{**r, "pred": Understanding(ok=True, intent="refund_request", language="en", urgency="low", confidence=0.7, flags=("prompt_injection",))}
           for r in rows if r["note"] == "trick_injection"]
    mi = compute_metrics(inj)
    assert mi["injection"]["recall"] == 1.0 and mi["injection"]["false_alarms"] == 0
    print("ok  a failed call counts as wrong; injection recall and false alarms are computed")
    print("run_eval.py self-test passed (no real API calls were made).")


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, metavar="N", help="evaluate a seeded, stratified sample of N tickets")
    ap.add_argument("--all", action="store_true", help="evaluate every ticket")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--show-errors", type=int, default=10)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if not args.all and not args.sample:
        ap.error("give --sample N (e.g. --sample 30) or --all")
    if not C.DB_PATH.exists():
        sys.exit(f"{C.DB_PATH} not found. Run: python src/generate_data.py")

    conn = db.connect_admin()
    rows = load_tickets(conn)
    conn.close()
    if args.sample:
        rows = select_sample(rows, args.sample, args.seed)
    client = LLMClient()
    cached, needed = preflight(rows, client)
    minutes = needed * 60 / C.LLM_REQUESTS_PER_MINUTE / 60
    print(f"{len(rows)} tickets: {cached} already cached, {needed} need the API "
          f"(~{needed} requests, ~{minutes:.0f} min at {C.LLM_REQUESTS_PER_MINUTE}/min; {client.remaining_today} left in today's cap)")
    if needed > client.remaining_today:
        sys.exit("Not enough of today's request cap for this run. Use a smaller --sample, or run part now and the rest tomorrow.")
    results, completed = run(rows, client)
    if results:
        m = compute_metrics(results)
        print_report(m, results, args.show_errors)
        save_results(results, m, args)
    print(f"\nReal API calls this run: {client.stats['api_calls']} (retries {client.stats['retries']}), cache hits: {client.stats['cache_hits']}")
    if not completed:
        sys.exit(1)


if __name__ == "__main__":
    main()
