"""Deep-dive Freezer comparison: NEW (v4.2 warm-start, PROD candidate) vs PRODUCTION.

Mirrors deep_dive_chilled.py but for the Freezer zone. Freezer is:
  - A cold_chain_zone (HC12 30-min cap, _split_for_cold_chain may fire)
  - An affinity_zone (Phase B v4.2 warm-start applies on top)
  - Unique in allowing 2 orders per tote (frozen_max_orders_per_tote=2)

Run:
    python deep_dive_freezer.py
"""
from __future__ import annotations

import os
import statistics
import sys
from collections import Counter, defaultdict
from typing import Dict

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ORDERS_CSV = os.path.join(THIS_DIR, "1052Orders.csv")

sys.path.insert(0, THIS_DIR)
import tote_trolley_optimizer_v2 as v2  # type: ignore
import tote_trolley_optimizer_v4 as v4  # type: ignore  # v4.2 warm-start = PROD candidate

ZONE = "Freezer"


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
    distinct_orders = len({it[5] for it in items if len(it) > 5 and it[5]})
    return {
        "n_items": len(items),
        "distinct_aisles": len(distinct_aisles),
        "aisle_range": aisle_range,
        "max_bay_range_in_aisle": max_bay_range_in_aisle,
        "sum_bay_range": sum_bay_range,
        "distinct_orders": distinct_orders,
    }


def _items_metrics_trolley(items, matrix=None, cfg=None, walk_known: float = None,
                            uturn_known: int = None):
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
    print(f"  {label:<28} n={s['n']:4d}  mean={s['mean']:7.2f}  median={s['median']:6.2f}  p90={s['p90']:6.2f}  max={s['max']:6.2f}")


def main() -> None:
    print("=" * 78)
    print(f" {ZONE} deep-dive: NEW (v4.2 warm-start, PROD candidate) vs PRODUCTION")
    print("=" * 78)

    cfg = v2.StoreConfig()
    print(f"\n[1/4] Building NEW {ZONE} totes & trolleys via v4.2 (warm-start hybrid) ...", flush=True)
    matrix = v2.DistanceMatrix.load_from_csv(v2.DIST_MATRIX_CSV, unit_to_m=cfg.matrix_unit_to_m)
    items = v2.load_orders(v2.ORDERS_CSV)
    items = [it for it in items if matrix.has(it.location_key)]
    items_by_order = defaultdict(list)
    for it in items:
        items_by_order[it.order_no].append(it)
    zone_items = [it for it in items if it.zone == ZONE]
    print(f"        zone items: {len(zone_items)} across {len({it.order_no for it in zone_items})} orders")
    new_totes = v2.build_totes_for_zone(zone_items, ZONE, cfg, matrix, items_by_order)
    new_trolleys = v4.build_trolleys_production(new_totes, ZONE, cfg, matrix, items_by_order)
    print(f"        new {ZONE}: {len(new_totes)} totes, {len(new_trolleys)} trolleys")

    print(f"\n[2/4] Building PRODUCTION baseline (planned, excl Label) via v2.analyse_baseline ...", flush=True)
    base_trolleys = v2.analyse_baseline(items, matrix, cfg, items_by_order, exclude_label=True)
    base_z = [tr for tr in base_trolleys if tr.zone == ZONE]
    base_z_totes = [tote for tr in base_z for tote in tr.totes]
    print(f"        prod {ZONE}: {len(base_z_totes)} totes, {len(base_z)} trolleys")

    # --- TOTE-LEVEL ---
    print("\n[3/4] TOTE-level distributions:")
    print(f"  -- NEW totes ({len(new_totes)}) --")
    new_tote_items = [
        [(v2.aisle_int(it.aisle_location), v2.bay_int(it.bay_location), it.transit_id, it.stock_code, it.quantity, it.order_no)
         for it in t.items]
        for t in new_totes
    ]
    new_tote_metrics = [_items_metrics_tote(its) for its in new_tote_items]
    _print_dist("items_per_tote", [m["n_items"] for m in new_tote_metrics])
    _print_dist("distinct_orders_per_tote", [m["distinct_orders"] for m in new_tote_metrics])
    _print_dist("distinct_aisles_per_tote", [m["distinct_aisles"] for m in new_tote_metrics])
    _print_dist("aisle_range_per_tote", [m["aisle_range"] for m in new_tote_metrics])
    _print_dist("max_bay_range_in_aisle", [m["max_bay_range_in_aisle"] for m in new_tote_metrics])
    _print_dist("sum_bay_range", [m["sum_bay_range"] for m in new_tote_metrics])

    print(f"\n  -- PRODUCTION totes ({len(base_z_totes)}) --")
    prod_tote_items = [
        [(v2.aisle_int(it.aisle_location), v2.bay_int(it.bay_location), it.transit_id, it.stock_code, it.quantity, it.order_no)
         for it in tote.items]
        for tote in base_z_totes
    ]
    prod_tote_metrics = [_items_metrics_tote(its) for its in prod_tote_items]
    _print_dist("items_per_tote", [m["n_items"] for m in prod_tote_metrics])
    _print_dist("distinct_orders_per_tote", [m["distinct_orders"] for m in prod_tote_metrics])
    _print_dist("distinct_aisles_per_tote", [m["distinct_aisles"] for m in prod_tote_metrics])
    _print_dist("aisle_range_per_tote", [m["aisle_range"] for m in prod_tote_metrics])
    _print_dist("max_bay_range_in_aisle", [m["max_bay_range_in_aisle"] for m in prod_tote_metrics])
    _print_dist("sum_bay_range", [m["sum_bay_range"] for m in prod_tote_metrics])

    # --- TROLLEY-LEVEL ---
    print("\n[4/4] TROLLEY-level distributions (walk/U-turns recomputed via same SPT engine):")

    print(f"\n  -- NEW trolleys ({len(new_trolleys)}) --")
    new_tr_items = [
        [(v2.aisle_int(it.aisle_location), v2.bay_int(it.bay_location), it.transit_id, it.stock_code, it.quantity, it.order_no)
         for tote in tr.totes for it in tote.items]
        for tr in new_trolleys
    ]
    new_tr_metrics = []
    for tr, its in zip(new_trolleys, new_tr_items):
        m = _items_metrics_trolley(its, matrix, cfg,
                                   walk_known=tr.walk_distance_m,
                                   uturn_known=tr.uturn_count)
        m["tote_count"] = len(tr.totes)
        new_tr_metrics.append(m)

    _print_dist("totes_per_trolley", [m["tote_count"] for m in new_tr_metrics])
    _print_dist("items_per_trolley", [m["n_items"] for m in new_tr_metrics])
    _print_dist("distinct_aisles_per_trolley", [m["distinct_aisles"] for m in new_tr_metrics])
    _print_dist("aisle_range_per_trolley", [m["aisle_range"] for m in new_tr_metrics])
    _print_dist("max_aisle_gap", [m["max_aisle_gap"] for m in new_tr_metrics])
    _print_dist("sum_per_aisle_bay_range", [m["sum_bay_range"] for m in new_tr_metrics])
    _print_dist("max_bay_range_in_aisle", [m["max_bay_range_in_aisle"] for m in new_tr_metrics])
    _print_dist("distinct_trucks_per_trolley", [m["distinct_trucks"] for m in new_tr_metrics])
    _print_dist("walk_m_per_trolley", [m["walk_m"] for m in new_tr_metrics])
    _print_dist("uturns_per_trolley", [m["uturns"] for m in new_tr_metrics])

    print(f"\n  -- PRODUCTION trolleys ({len(base_z)}) --")
    prod_tr_items = [
        [(v2.aisle_int(it.aisle_location), v2.bay_int(it.bay_location), it.transit_id, it.stock_code, it.quantity, it.order_no)
         for tote in tr.totes for it in tote.items]
        for tr in base_z
    ]
    prod_tr_metrics = []
    for tr, its in zip(base_z, prod_tr_items):
        m = _items_metrics_trolley(its, matrix, cfg,
                                   walk_known=tr.walk_distance_m,
                                   uturn_known=tr.uturn_count)
        m["tote_count"] = len(tr.totes)
        prod_tr_metrics.append(m)

    _print_dist("totes_per_trolley", [m["tote_count"] for m in prod_tr_metrics])
    _print_dist("items_per_trolley", [m["n_items"] for m in prod_tr_metrics])
    _print_dist("distinct_aisles_per_trolley", [m["distinct_aisles"] for m in prod_tr_metrics])
    _print_dist("aisle_range_per_trolley", [m["aisle_range"] for m in prod_tr_metrics])
    _print_dist("max_aisle_gap", [m["max_aisle_gap"] for m in prod_tr_metrics])
    _print_dist("sum_per_aisle_bay_range", [m["sum_bay_range"] for m in prod_tr_metrics])
    _print_dist("max_bay_range_in_aisle", [m["max_bay_range_in_aisle"] for m in prod_tr_metrics])
    _print_dist("distinct_trucks_per_trolley", [m["distinct_trucks"] for m in prod_tr_metrics])
    _print_dist("walk_m_per_trolley", [m["walk_m"] for m in prod_tr_metrics])
    _print_dist("uturns_per_trolley", [m["uturns"] for m in prod_tr_metrics])

    # --- DELTA HEADLINE ---
    new_walk = sum(m["walk_m"] for m in new_tr_metrics)
    prod_walk = sum(m["walk_m"] for m in prod_tr_metrics)
    new_uturn = sum(m["uturns"] for m in new_tr_metrics)
    prod_uturn = sum(m["uturns"] for m in prod_tr_metrics)
    new_cost = new_walk + 4.0 * new_uturn
    prod_cost = prod_walk + 4.0 * prod_uturn
    print("\n" + "=" * 78)
    print(f" {ZONE} summary")
    print("=" * 78)
    print(f"  trolleys:     new={len(new_tr_metrics)}    prod={len(prod_tr_metrics)}     delta={len(new_tr_metrics)-len(prod_tr_metrics):+d}")
    print(f"  totes:        new={len(new_tote_metrics)}   prod={len(prod_tote_metrics)}   delta={len(new_tote_metrics)-len(prod_tote_metrics):+d}")
    print(f"  total walk:   new={new_walk:8.0f} m  prod={prod_walk:8.0f} m  delta={new_walk-prod_walk:+.0f} m")
    if new_tr_metrics and prod_tr_metrics:
        print(f"  m / trolley:  new={new_walk/len(new_tr_metrics):6.1f}     prod={prod_walk/len(prod_tr_metrics):6.1f}     delta={new_walk/len(new_tr_metrics) - prod_walk/len(prod_tr_metrics):+.1f}")
    print(f"  total U-turn: new={new_uturn}  prod={prod_uturn}  delta={new_uturn-prod_uturn:+d}")
    print(f"  cost (w+4U):  new={new_cost:8.0f}    prod={prod_cost:8.0f}    delta={new_cost-prod_cost:+.0f}")

    print("\n  Aisles-per-trolley histogram:")
    new_hist = Counter(m["distinct_aisles"] for m in new_tr_metrics)
    prod_hist = Counter(m["distinct_aisles"] for m in prod_tr_metrics)
    keys = sorted(set(new_hist) | set(prod_hist))
    print(f"    {'aisles':>6}  {'new':>5}  {'prod':>5}")
    for k in keys:
        print(f"    {k:>6}  {new_hist.get(k,0):>5}  {prod_hist.get(k,0):>5}")

    print("\n  Tote-count histogram per trolley:")
    new_thist = Counter(m["tote_count"] for m in new_tr_metrics)
    prod_thist = Counter(m["tote_count"] for m in prod_tr_metrics)
    keys = sorted(set(new_thist) | set(prod_thist))
    print(f"    {'totes':>5}  {'new':>5}  {'prod':>5}")
    for k in keys:
        print(f"    {k:>5}  {new_thist.get(k,0):>5}  {prod_thist.get(k,0):>5}")

    print("\n  Distinct-TIDs histogram per trolley:")
    new_tk = Counter(m["distinct_trucks"] for m in new_tr_metrics)
    prod_tk = Counter(m["distinct_trucks"] for m in prod_tr_metrics)
    keys = sorted(set(new_tk) | set(prod_tk))
    print(f"    {'tids':>6}  {'new':>5}  {'prod':>5}")
    for k in keys:
        print(f"    {k:>6}  {new_tk.get(k,0):>5}  {prod_tk.get(k,0):>5}")

    print("\n  Orders-per-tote histogram — pre-consolidation NEW vs PROD (TrayHeaderID granularity):")
    new_ohist = Counter(m["distinct_orders"] for m in new_tote_metrics)
    prod_ohist = Counter(m["distinct_orders"] for m in prod_tote_metrics)
    keys = sorted(set(new_ohist) | set(prod_ohist))
    print(f"    {'orders':>6}  {'new':>5}  {'prod':>5}")
    for k in keys:
        print(f"    {k:>6}  {new_ohist.get(k,0):>5}  {prod_ohist.get(k,0):>5}")

    # Post-consolidation NEW totes (as they appear inside the built trolleys, after
    # _consolidate_same_tid_totes pooled same-TID singletons). PROD totes don't
    # consolidate — each TrayHeaderID is a physical tote — so PROD count remains
    # the pre-consolidation count.
    print("\n  Post-consolidation NEW physical tote counts (inside trolleys):")
    new_post_totes = [tote for tr in new_trolleys for tote in tr.totes]
    new_post_orders = Counter(len(tote.order_nos) for tote in new_post_totes)
    keys = sorted(new_post_orders)
    print(f"    {'orders':>6}  {'count':>5}")
    for k in keys:
        print(f"    {k:>6}  {new_post_orders.get(k,0):>5}")
    print(f"    total physical totes (post-consolidation) = {len(new_post_totes)}")
    print(f"    total logical totes (pre-consolidation) = {sum(len(tote.order_nos) for tote in new_post_totes)}"
          f"  (vs PROD {len(prod_tote_metrics)})")


if __name__ == "__main__":
    main()
