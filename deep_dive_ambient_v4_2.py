"""4-way deep-dive validator: PROD vs v2 vs v4.1 (joint) vs v4.2 (warmstart hybrid).

Extends `deep_dive_ambient_v4.py` to add v4.2 — option (b) from CLAUDE.md:
take v2's final TrolleyResult list, run v4's joint reassignment + SA polish on
top. Guaranteed >= v2 by construction (steepest descent only accepts
improvements).

All four sides scored with the SAME `trip_cost` engine so walk/U-turn
comparisons are apples-to-apples.

Run:
    python deep_dive_ambient_v4_2.py            (prints to stdout)
    python deep_dive_ambient_v4_2.py > out.txt  (capture)
"""
from __future__ import annotations

import os
import statistics
import sys
from collections import Counter, defaultdict
from typing import Dict, List

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(THIS_DIR)

sys.path.insert(0, THIS_DIR)
import tote_trolley_optimizer_v2 as v2  # type: ignore
import tote_trolley_optimizer_v4 as v4  # type: ignore


# ----------------------------------------------------------------------------
# Helpers (lifted from deep_dive_ambient_v4.py)
# ----------------------------------------------------------------------------


def _items_metrics_tote(items):
    aisles = [it[0] for it in items if it[0]]
    bays_by_aisle: Dict[int, list] = defaultdict(list)
    for it in items:
        a, b = it[0], it[1]
        if a:
            bays_by_aisle[a].append(b)
    distinct_aisles = sorted(set(aisles))
    aisle_range = (max(distinct_aisles) - min(distinct_aisles)) if distinct_aisles else 0
    max_bay_range_in_aisle = max(
        (max(bs) - min(bs) for bs in bays_by_aisle.values()), default=0
    )
    sum_bay_range = sum(max(bs) - min(bs) for bs in bays_by_aisle.values())
    return {
        "n_items": len(items),
        "distinct_aisles": len(distinct_aisles),
        "aisle_range": aisle_range,
        "max_bay_range_in_aisle": max_bay_range_in_aisle,
        "sum_bay_range": sum_bay_range,
    }


def _items_metrics_trolley(items, walk_known=None, uturn_known=None):
    base = _items_metrics_tote(items)
    transits = sorted({it[2] for it in items if it[2]})
    aisles = sorted({it[0] for it in items if it[0]})
    aisle_gaps = [aisles[i + 1] - aisles[i] for i in range(len(aisles) - 1)]
    max_aisle_gap = max(aisle_gaps) if aisle_gaps else 0
    base.update({
        "distinct_trucks": len(transits),
        "max_aisle_gap": max_aisle_gap,
        "walk_m": walk_known,
        "uturns": uturn_known,
    })
    return base


def _summary(values):
    if not values:
        return {"n": 0, "mean": 0.0, "median": 0.0, "p90": 0.0, "max": 0}
    s = sorted(values)
    p90 = s[max(0, int(0.9 * len(s)) - 1)]
    return {
        "n": len(s),
        "mean": round(sum(s) / len(s), 2),
        "median": round(statistics.median(s), 2),
        "p90": p90,
        "max": s[-1],
    }


def _print_dist(label, vals):
    s = _summary(vals)
    print(f"  {label:<28} n={s['n']:4d}  mean={s['mean']:7.2f}  median={s['median']:6.2f}"
          f"  p90={s['p90']:6.2f}  max={s['max']:6.2f}")


def _trolley_metrics(trolleys):
    out = []
    for tr in trolleys:
        items = [
            (v2.aisle_int(it.aisle_location), v2.bay_int(it.bay_location),
             it.transit_id, it.stock_code, it.quantity)
            for tote in tr.totes for it in tote.items
        ]
        m = _items_metrics_trolley(items, walk_known=tr.walk_distance_m,
                                    uturn_known=tr.uturn_count)
        m["tote_count"] = len(tr.totes)
        out.append(m)
    return out


def _print_trolley_dists(label, trolleys, metrics):
    print(f"\n  -- {label} ({len(trolleys)}) --")
    _print_dist("totes_per_trolley", [m["tote_count"] for m in metrics])
    _print_dist("items_per_trolley", [m["n_items"] for m in metrics])
    _print_dist("distinct_aisles_per_trolley", [m["distinct_aisles"] for m in metrics])
    _print_dist("aisle_range_per_trolley", [m["aisle_range"] for m in metrics])
    _print_dist("max_aisle_gap", [m["max_aisle_gap"] for m in metrics])
    _print_dist("sum_per_aisle_bay_range", [m["sum_bay_range"] for m in metrics])
    _print_dist("max_bay_range_in_aisle", [m["max_bay_range_in_aisle"] for m in metrics])
    _print_dist("distinct_trucks_per_trolley", [m["distinct_trucks"] for m in metrics])
    _print_dist("walk_m_per_trolley", [m["walk_m"] for m in metrics])
    _print_dist("uturns_per_trolley", [m["uturns"] for m in metrics])


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def main() -> None:
    print("=" * 78)
    print(" Ambient deep-dive: 4-way (PROD vs v2 vs v4.1 joint vs v4.2 warmstart)")
    print("=" * 78)

    cfg = v2.StoreConfig()
    matrix = v2.DistanceMatrix.load_from_csv(v2.DIST_MATRIX_CSV, unit_to_m=cfg.matrix_unit_to_m)
    items = v2.load_orders(v2.ORDERS_CSV)
    items = [it for it in items if matrix.has(it.location_key)]
    items_by_order = defaultdict(list)
    for it in items:
        items_by_order[it.order_no].append(it)
    amb_items = [it for it in items if it.zone == "Ambient"]

    # ---- Build totes ONCE, reuse for v2/v4.1/v4.2. ----
    print("\n[1/5] Building Ambient totes (shared by v2 / v4.1 / v4.2) ...", flush=True)
    new_totes = v2.build_totes_for_zone(amb_items, "Ambient", cfg, matrix, items_by_order)
    print(f"        ambient totes: {len(new_totes)}")

    # ---- v2 trolleys ----
    print("\n[2/5] Building v2 trolleys (greedy + SA) ...", flush=True)
    v2_trolleys = v2.build_trolleys_rolling_pat_for_zone(
        new_totes, "Ambient", cfg, matrix, items_by_order)
    print(f"        v2 ambient: {len(new_totes)} totes, {len(v2_trolleys)} trolleys")

    # ---- v4.1 trolleys (joint) ----
    print("\n[3/5] Building v4.1 trolleys (joint bin-pack + SA) ...", flush=True)
    v4_trolleys = v4.build_trolleys_joint_v4(
        new_totes, "Ambient", cfg, matrix, items_by_order)
    print(f"        v4.1 ambient: {len(new_totes)} totes, {len(v4_trolleys)} trolleys")

    # ---- v4.2 trolleys (warm-start hybrid) ----
    print("\n[4/5] Building v4.2 trolleys (v2 warm-start + reassign + SA) ...", flush=True)
    v42_trolleys = v4.build_trolleys_warmstart_hybrid(
        v2_trolleys, "Ambient", cfg, matrix, items_by_order, reassign_passes=6)
    print(f"        v4.2 ambient: {len(new_totes)} totes, {len(v42_trolleys)} trolleys")

    # ---- PROD baseline ----
    print("\n[5/5] Building PROD baseline (planned, excl Label) ...", flush=True)
    base_trolleys = v2.analyse_baseline(items, matrix, cfg, items_by_order, exclude_label=True)
    base_amb = [tr for tr in base_trolleys if tr.zone == "Ambient"]
    base_amb_totes = [tote for tr in base_amb for tote in tr.totes]
    print(f"        prod ambient: {len(base_amb_totes)} totes, {len(base_amb)} trolleys")

    # ---- Trolley distributions ----
    print("\n" + "=" * 78)
    print(" Trolley-level distributions (walk/U-turns via same SPT engine)")
    print("=" * 78)
    v2_metrics = _trolley_metrics(v2_trolleys)
    v4_metrics = _trolley_metrics(v4_trolleys)
    v42_metrics = _trolley_metrics(v42_trolleys)
    prod_metrics = _trolley_metrics(base_amb)
    _print_trolley_dists("v2 trolleys (greedy+SA)", v2_trolleys, v2_metrics)
    _print_trolley_dists("v4.1 trolleys (joint+SA)", v4_trolleys, v4_metrics)
    _print_trolley_dists("v4.2 trolleys (warmstart+SA)", v42_trolleys, v42_metrics)
    _print_trolley_dists("PROD trolleys", base_amb, prod_metrics)

    # ---- Summary ----
    def _agg(metrics):
        walk = sum(m["walk_m"] for m in metrics)
        uturn = sum(m["uturns"] for m in metrics)
        n = len(metrics)
        return {
            "trolleys": n,
            "walk": walk,
            "uturn": uturn,
            "m_per_trolley": walk / n if n else 0.0,
            "cost": walk + uturn * cfg.uturn_penalty_m,
        }

    a_v2 = _agg(v2_metrics)
    a_v4 = _agg(v4_metrics)
    a_v42 = _agg(v42_metrics)
    a_prod = _agg(prod_metrics)

    print("\n" + "=" * 78)
    print(" Headline summary (Ambient)")
    print("=" * 78)
    print(f"  {'metric':<24} {'v2':>10} {'v4.1':>10} {'v4.2':>10} {'PROD':>10}"
          f"  {'v4.2 vs v2':>12} {'v4.2 vs PROD':>14}")
    fmt = ("  {label:<24} {v2:>10.1f} {v41:>10.1f} {v42:>10.1f} {prod:>10.1f}"
           "  {dv2:>+12.1f} {dprod:>+14.1f}")
    fmt_int = ("  {label:<24} {v2:>10d} {v41:>10d} {v42:>10d} {prod:>10d}"
               "  {dv2:>+12d} {dprod:>+14d}")
    print(fmt_int.format(label="trolleys", v2=a_v2["trolleys"], v41=a_v4["trolleys"],
                         v42=a_v42["trolleys"], prod=a_prod["trolleys"],
                         dv2=a_v42["trolleys"] - a_v2["trolleys"],
                         dprod=a_v42["trolleys"] - a_prod["trolleys"]))
    print(fmt.format(label="total walk (m)", v2=a_v2["walk"], v41=a_v4["walk"],
                     v42=a_v42["walk"], prod=a_prod["walk"],
                     dv2=a_v42["walk"] - a_v2["walk"],
                     dprod=a_v42["walk"] - a_prod["walk"]))
    print(fmt.format(label="walk per trolley (m)", v2=a_v2["m_per_trolley"],
                     v41=a_v4["m_per_trolley"], v42=a_v42["m_per_trolley"],
                     prod=a_prod["m_per_trolley"],
                     dv2=a_v42["m_per_trolley"] - a_v2["m_per_trolley"],
                     dprod=a_v42["m_per_trolley"] - a_prod["m_per_trolley"]))
    print(fmt_int.format(label="total U-turns", v2=a_v2["uturn"], v41=a_v4["uturn"],
                         v42=a_v42["uturn"], prod=a_prod["uturn"],
                         dv2=a_v42["uturn"] - a_v2["uturn"],
                         dprod=a_v42["uturn"] - a_prod["uturn"]))
    print(fmt.format(label="cost (walk + 4·U)", v2=a_v2["cost"], v41=a_v4["cost"],
                     v42=a_v42["cost"], prod=a_prod["cost"],
                     dv2=a_v42["cost"] - a_v2["cost"],
                     dprod=a_v42["cost"] - a_prod["cost"]))

    # PAT-compliance check on all sides.
    v2_max_tids = max((m["distinct_trucks"] for m in v2_metrics), default=0)
    v4_max_tids = max((m["distinct_trucks"] for m in v4_metrics), default=0)
    v42_max_tids = max((m["distinct_trucks"] for m in v42_metrics), default=0)
    print(f"\n  PAT compliance: v2 max TIDs={v2_max_tids}  v4.1 max TIDs={v4_max_tids}  "
          f"v4.2 max TIDs={v42_max_tids}  (cap = {cfg.pick_across_trucks})")

    # ---- Histograms ----
    print("\n  Aisles-per-trolley histogram:")
    h_v2 = Counter(m["distinct_aisles"] for m in v2_metrics)
    h_v4 = Counter(m["distinct_aisles"] for m in v4_metrics)
    h_v42 = Counter(m["distinct_aisles"] for m in v42_metrics)
    h_prod = Counter(m["distinct_aisles"] for m in prod_metrics)
    keys = sorted(set(h_v2) | set(h_v4) | set(h_v42) | set(h_prod))
    print(f"    {'aisles':>6}  {'v2':>5}  {'v4.1':>5}  {'v4.2':>5}  {'prod':>5}")
    for k in keys:
        print(f"    {k:>6}  {h_v2.get(k,0):>5}  {h_v4.get(k,0):>5}  "
              f"{h_v42.get(k,0):>5}  {h_prod.get(k,0):>5}")

    print("\n  Tote-count histogram per trolley:")
    h_v2 = Counter(m["tote_count"] for m in v2_metrics)
    h_v4 = Counter(m["tote_count"] for m in v4_metrics)
    h_v42 = Counter(m["tote_count"] for m in v42_metrics)
    h_prod = Counter(m["tote_count"] for m in prod_metrics)
    keys = sorted(set(h_v2) | set(h_v4) | set(h_v42) | set(h_prod))
    print(f"    {'totes':>5}  {'v2':>5}  {'v4.1':>5}  {'v4.2':>5}  {'prod':>5}")
    for k in keys:
        print(f"    {k:>5}  {h_v2.get(k,0):>5}  {h_v4.get(k,0):>5}  "
              f"{h_v42.get(k,0):>5}  {h_prod.get(k,0):>5}")

    print("\n  Distinct-trucks histogram per trolley:")
    h_v2 = Counter(m["distinct_trucks"] for m in v2_metrics)
    h_v4 = Counter(m["distinct_trucks"] for m in v4_metrics)
    h_v42 = Counter(m["distinct_trucks"] for m in v42_metrics)
    h_prod = Counter(m["distinct_trucks"] for m in prod_metrics)
    keys = sorted(set(h_v2) | set(h_v4) | set(h_v42) | set(h_prod))
    print(f"    {'trucks':>6}  {'v2':>5}  {'v4.1':>5}  {'v4.2':>5}  {'prod':>5}")
    for k in keys:
        print(f"    {k:>6}  {h_v2.get(k,0):>5}  {h_v4.get(k,0):>5}  "
              f"{h_v42.get(k,0):>5}  {h_prod.get(k,0):>5}")


if __name__ == "__main__":
    main()
