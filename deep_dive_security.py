"""Deep-dive Security comparison: NEW (v4.2 warm-start, PROD candidate) vs PRODUCTION.

Security is a back-room pick zone — pickers are stationary, so there are no
walk or U-turn metrics. The key operational win is same-TID consolidation:
multiple Security orders sharing a TID can be merged into a single physical tote
(up to security_max_orders_per_tote=6, bounded in practice by the 12.5kg /
capacity_max_volume_cm3 weight/volume cap).

Run:
    python deep_dive_security.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

import tote_trolley_optimizer_v2 as v2  # type: ignore
import tote_trolley_optimizer_v4 as v4  # type: ignore

ZONE = "Security"


def _summary_counts(totes):
    """Return (logical_totes, physical_totes, orders) for a list of ToteResult."""
    logical = len(totes)
    physical = sum(1 for t in totes if len(t.order_nos) >= 1)
    orders = sum(len(t.order_nos) for t in totes)
    return logical, physical, orders


def main() -> None:
    print("=" * 78)
    print(f" {ZONE} deep-dive: NEW (v4.2 warm-start, PROD candidate) vs PRODUCTION")
    print("=" * 78)
    print("  (Security is back-room pick — no walk/U-turn metrics)")

    cfg = v2.StoreConfig()
    print(f"\n[1/4] Building NEW {ZONE} totes & trolleys via v4.2 ...", flush=True)
    matrix = v2.DistanceMatrix.load_from_csv(v2.DIST_MATRIX_CSV, unit_to_m=cfg.matrix_unit_to_m)
    items = v2.load_orders(v2.ORDERS_CSV)
    items = [it for it in items if matrix.has(it.location_key)]
    items_by_order = defaultdict(list)
    for it in items:
        items_by_order[it.order_no].append(it)
    zone_items = [it for it in items if it.zone == ZONE]
    n_orders = len({it.order_no for it in zone_items})
    print(f"        zone items: {len(zone_items)} across {n_orders} orders")

    new_totes = v2.build_totes_for_zone(zone_items, ZONE, cfg, matrix, items_by_order)
    new_trolleys = v4.build_trolleys_production(new_totes, ZONE, cfg, matrix, items_by_order)
    print(f"        new {ZONE}: {len(new_totes)} logical totes, {len(new_trolleys)} trolleys")

    print(f"\n[2/4] Building PRODUCTION baseline (planned, excl Label) ...", flush=True)
    base_trolleys = v2.analyse_baseline(items, matrix, cfg, items_by_order, exclude_label=True)
    base_z = [tr for tr in base_trolleys if tr.zone == ZONE]
    base_z_totes = [tote for tr in base_z for tote in tr.totes]
    print(f"        prod {ZONE}: {len(base_z_totes)} totes, {len(base_z)} trolleys")

    # --- NEW tote details ---
    print(f"\n[3/4] NEW tote breakdown (pre-consolidation logical totes):")
    new_tids = Counter()
    for t in new_totes:
        for it in t.items:
            new_tids[it.transit_id] += 1
    print(f"  logical totes: {len(new_totes)}")
    print(f"  distinct TIDs across all totes: {len(new_tids)}")

    # Post-consolidation (physical totes inside built trolleys)
    new_physical = [tote for tr in new_trolleys for tote in tr.totes]
    new_log, new_phys, new_orders = _summary_counts(new_physical)
    print(f"\n  Post-consolidation (inside built trolleys):")
    print(f"    physical totes: {new_phys}")
    print(f"    logical orders carried: {new_orders}")
    new_orders_per_phys = Counter(len(t.order_nos) for t in new_physical)
    print(f"    orders-per-physical-tote histogram:")
    for k in sorted(new_orders_per_phys):
        print(f"      {k} order(s): {new_orders_per_phys[k]} tote(s)")

    # TID breakdown per trolley
    print(f"\n  NEW trolley breakdown ({len(new_trolleys)} trolleys):")
    for i, tr in enumerate(new_trolleys, 1):
        phys_totes = tr.totes
        tids = sorted({it.transit_id for tote in phys_totes for it in tote.items})
        order_nos = sorted({o for tote in phys_totes for o in tote.order_nos})
        total_g = sum(
            it.unit_weight_g * it.quantity
            for tote in phys_totes for it in tote.items
        )
        total_cm3 = sum(
            it.unit_volume_cm3 * it.quantity
            for tote in phys_totes for it in tote.items
        )
        print(f"    trolley {i}: {len(phys_totes)} physical tote(s), "
              f"{len(order_nos)} order(s), TIDs={tids}, "
              f"weight={total_g:.0f}g, vol={total_cm3:.0f}cm3")

    # --- PROD tote details ---
    print(f"\n[4/4] PROD tote breakdown:")
    print(f"  trolleys: {len(base_z)}")
    print(f"  totes: {len(base_z_totes)}")
    prod_tids = Counter()
    for t in base_z_totes:
        for it in t.items:
            prod_tids[it.transit_id] += 1
    print(f"  distinct TIDs: {len(prod_tids)}")
    for i, tr in enumerate(base_z, 1):
        tids = sorted({it.transit_id for tote in tr.totes for it in tote.items})
        order_nos = sorted({o for tote in tr.totes for o in tote.order_nos})
        total_g = sum(
            it.unit_weight_g * it.quantity
            for tote in tr.totes for it in tote.items
        )
        total_cm3 = sum(
            it.unit_volume_cm3 * it.quantity
            for tote in tr.totes for it in tote.items
        )
        print(f"    trolley {i}: {len(tr.totes)} tote(s), "
              f"{len(order_nos)} order(s), TIDs={tids}, "
              f"weight={total_g:.0f}g, vol={total_cm3:.0f}cm3")

    # --- Summary ---
    print("\n" + "=" * 78)
    print(f" {ZONE} summary")
    print("=" * 78)
    print(f"  trolleys:       new={len(new_trolleys)}   prod={len(base_z)}   "
          f"delta={len(new_trolleys)-len(base_z):+d}")
    print(f"  logical totes:  new={len(new_totes)}   prod={len(base_z_totes)}   "
          f"delta={len(new_totes)-len(base_z_totes):+d}")
    print(f"  physical totes: new={new_phys}   "
          f"(PROD does not consolidate same-TID orders)")
    print(f"  orders carried: new={new_orders}   prod={len(base_z_totes)}")
    print(f"  (No walk/U-turn metrics — back-room stationary pick)")

    # TID histogram
    new_tid_hist = Counter(m for tr in new_trolleys
                           for m in [len({it.transit_id
                                          for tote in tr.totes
                                          for it in tote.items})])
    prod_tid_hist = Counter(m for tr in base_z
                            for m in [len({it.transit_id
                                           for tote in tr.totes
                                           for it in tote.items})])
    print("\n  Distinct-TIDs histogram per trolley:")
    print(f"    {'tids':>6}  {'new':>5}  {'prod':>5}")
    for k in sorted(set(new_tid_hist) | set(prod_tid_hist)):
        print(f"    {k:>6}  {new_tid_hist.get(k,0):>5}  {prod_tid_hist.get(k,0):>5}")

    print(f"\n  PAT compliance: max TIDs/trolley new={max(new_tid_hist or {0})}  "
          f"prod={max(prod_tid_hist or {0})}  (cap={cfg.pick_across_trucks})")


if __name__ == "__main__":
    main()
