"""Run only the NEW algorithm and compare against stored baselines.

Skips rebuilding the PROD baseline — uses pre-computed numbers from
baselines/<store>.json. Each store/zone runs as a clean subprocess so
store-specific patches never bleed across runs.

Usage:
    python run_experiment.py                    # all stores, all zones
    python run_experiment.py 1052               # one store, all zones
    python run_experiment.py 1052 chilled       # one store, one zone
    python run_experiment.py --update-baseline  # after a win: promote
                                                # current run to new v1.0

Output:
    results/exp_YYYY-MM-DD_HHMM_summary.txt  — 3-column comparison table
    results/exp_YYYY-MM-DD_HHMM_<s>_<z>.txt — per-zone detail

Approximate runtimes (NEW algorithm only, no PROD rebuild):
    1052:  Ambient ~4m  Chilled ~1m45s  Freezer ~2s
    1030:  Ambient ~3m30s  Chilled ~1m  Freezer ~4s
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, Optional

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(THIS_DIR, "results")
BASELINES_DIR = os.path.join(THIS_DIR, "baselines")

STORE_SETUPS = {
    "1052": "_store_1052_setup",
    "1030": "_store_1030_setup",
}

ZONE_MAP = {
    "ambient": "Ambient",
    "chilled": "Chilled",
    "freezer": "Freezer",
    "security": "Security",
}

ALL_ZONES = ["ambient", "chilled", "freezer"]
WALKING_ZONES = ["ambient", "chilled", "freezer"]


# ---------------------------------------------------------------------------
# Worker mode — called as subprocess for a single store/zone
# Prints a JSON line to stdout with the metrics.
# ---------------------------------------------------------------------------

def _worker(store: str, zone: str) -> None:
    import sys
    sys.path.insert(0, THIS_DIR)

    setup_mod = STORE_SETUPS[store]
    import importlib
    importlib.import_module(setup_mod)

    import tote_trolley_optimizer_v2 as v2
    import tote_trolley_optimizer_v4 as v4
    from collections import defaultdict

    cfg = v2.StoreConfig()
    matrix = v2.DistanceMatrix.load_from_csv(v2.DIST_MATRIX_CSV, unit_to_m=cfg.matrix_unit_to_m)
    all_items = v2.load_orders(v2.ORDERS_CSV)
    items = [it for it in all_items if matrix.has(it.location_key)]
    items_by_order = defaultdict(list)
    for it in items:
        items_by_order[it.order_no].append(it)

    z = ZONE_MAP[zone]
    all_zone_items = [it for it in all_items if it.zone == z]
    zone_items = [it for it in items if it.zone == z]

    # Abort if matrix coverage for this zone is critically low (< 10%) — results would be garbage.
    # Normal "online aisles" filtering legitimately removes 30-40% of items; this catches
    # the case where the wrong matrix is paired with the zone.
    if all_zone_items and len(zone_items) / len(all_zone_items) < 0.10:
        error = {
            "error": "low_matrix_coverage",
            "zone_items": len(all_zone_items),
            "in_matrix": len(zone_items),
        }
        print("__RESULT__:" + json.dumps(error))
        return
    totes = v2.build_totes_for_zone(zone_items, z, cfg, matrix, items_by_order)
    trolleys = v4.build_trolleys_production(totes, z, cfg, matrix, items_by_order)

    walk = sum(tr.walk_distance_m for tr in trolleys)
    uturns = sum(tr.uturn_count for tr in trolleys)
    physical = [t for tr in trolleys for t in tr.totes]
    logical = sum(len(t.order_nos) for t in physical)

    result = {
        "trolleys": len(trolleys),
        "totes": logical,
        "physical_totes": len(physical),
        "walk_m": round(walk, 1),
        "uturns": uturns,
        "cost": round(walk + 4.0 * uturns, 1),
    }
    # Print JSON on its own line so the parent can parse it
    print("__RESULT__:" + json.dumps(result))


# ---------------------------------------------------------------------------
# Baseline loading / saving
# ---------------------------------------------------------------------------

def load_baseline(store: str) -> Dict:
    path = os.path.join(BASELINES_DIR, f"{store}.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_baseline(store: str, data: Dict) -> None:
    os.makedirs(BASELINES_DIR, exist_ok=True)
    with open(os.path.join(BASELINES_DIR, f"{store}.json"), "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Run one store/zone as a subprocess
# ---------------------------------------------------------------------------

def run_zone(store: str, zone: str, out_path: str):
    """Returns (metrics_dict, elapsed_s, ok, full_output)."""
    cmd = [sys.executable, __file__, "--worker", store, zone]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=THIS_DIR)
    elapsed = time.time() - t0

    output = proc.stdout
    metrics = None
    for line in output.splitlines():
        if line.startswith("__RESULT__:"):
            try:
                metrics = json.loads(line[len("__RESULT__:"):])
            except json.JSONDecodeError:
                pass

    # A result with "error" key means the worker ran but the zone is unrunnable (e.g. matrix gap).
    is_coverage_error = (metrics is not None and "error" in metrics)
    ok = proc.returncode == 0 and metrics is not None and not is_coverage_error
    detail = output
    if proc.returncode != 0:
        detail += f"\n--- STDERR ---\n{proc.stderr}"

    with open(out_path, "w") as f:
        f.write(detail)

    return metrics, elapsed, ok


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _d(new, ref, is_float=True):
    """Delta string."""
    if ref is None:
        return "   n/a"
    d = new - ref
    return f"{d:>+8.1f}" if is_float else f"{d:>+8d}"


def _pct(new, ref):
    if not ref:
        return "    n/a"
    return f"{(new - ref) / abs(ref) * 100:>+6.1f}%"


def _verdict(new_cost, v1_cost):
    if v1_cost is None:
        return ""
    gap = new_cost - v1_cost
    if gap < -0.5:
        return f"BETTER by {abs(gap):.0f} ({_pct(new_cost, v1_cost).strip()})"
    if gap > 0.5:
        return f"WORSE by {gap:.0f} ({_pct(new_cost, v1_cost).strip()})"
    return "same as v1.0"


def print_zone_block(store, zone, new, prod, v1, elapsed, file=sys.stdout):
    def p(*a, **kw):
        print(*a, **kw, file=file)

    p(f"\n{'='*70}")
    p(f"  store {store}  |  {zone.upper()}  |  {elapsed:.1f}s")
    p(f"{'='*70}")

    if zone == "security":
        p(f"  {'metric':<20} {'PROD':>8} {'v1.0':>8} {'THIS RUN':>10}")
        p("  " + "-" * 48)
        rows = [
            ("trolleys",       "trolleys",      "trolleys",      "trolleys"),
            ("logical_totes",  "logical totes", "totes",         "totes"),
            ("physical_totes", "physical totes","physical_totes","physical_totes"),
        ]
        for bk, label, nk, _ in rows:
            pv = prod.get(bk, "-") if prod else "-"
            vv = v1.get(bk, "-") if v1 else "-"
            nv = new.get(nk, "-")
            p(f"  {label:<20} {str(pv):>8} {str(vv):>8} {str(nv):>10}")
        return

    p(f"  {'metric':<18} {'PROD':>10} {'v1.0':>10} {'THIS RUN':>10}"
      f"  {'vs v1.0':>9} {'%':>7}  {'vs PROD':>9} {'%':>7}")
    p("  " + "-" * 82)
    rows = [
        ("trolleys", "trolleys", False),
        ("totes",    "totes",    False),
        ("walk_m",   "walk (m)", True),
        ("uturns",   "U-turns",  False),
        ("cost",     "cost",     True),
    ]
    for key, label, is_f in rows:
        pv = prod.get(key) if prod else None
        vv = v1.get(key) if v1 else None
        nv = new.get(key)
        fmt = f"{nv:>10.1f}" if is_f else f"{nv:>10d}"
        pv_s = (f"{pv:>10.1f}" if is_f else f"{pv:>10d}") if pv is not None else f"{'—':>10}"
        vv_s = (f"{vv:>10.1f}" if is_f else f"{vv:>10d}") if vv is not None else f"{'—':>10}"
        p(f"  {label:<18} {pv_s} {vv_s} {fmt}"
          f"  {_d(nv, vv, is_f):>9} {_pct(nv, vv):>7}"
          f"  {_d(nv, pv, is_f):>9} {_pct(nv, pv):>7}")

    p(f"\n  >> {_verdict(new['cost'], v1.get('cost') if v1 else None)}")


def print_summary(all_results: Dict, file=sys.stdout):
    def p(*a, **kw):
        print(*a, **kw, file=file)

    p("\n" + "=" * 95)
    p("  EXPERIMENT SUMMARY — THIS RUN vs CVRP v1.0 vs PROD")
    p("=" * 95)
    p(f"  {'store':<6} {'zone':<10} {'THIS RUN':>10} {'v1.0':>10}"
      f" {'vs v1.0':>9} {'%':>7}  {'PROD':>10} {'vs PROD':>9} {'%':>7}  verdict")
    p("  " + "-" * 91)

    totals = {"new": 0.0, "v1": 0.0, "prod": 0.0, "have_v1": 0, "have_prod": 0}

    for store in ["1052", "1030"]:
        for zone in WALKING_ZONES:
            key = (store, zone)
            if key not in all_results:
                continue
            new, prod, v1, elapsed, ok = all_results[key]
            if not ok:
                p(f"  {store:<6} {zone:<10}  FAILED")
                continue
            nc = new["cost"]
            vc = v1.get("cost") if v1 else None
            pc = prod.get("cost") if prod else None
            totals["new"] += nc
            if vc:
                totals["v1"] += vc
                totals["have_v1"] += 1
            if pc:
                totals["prod"] += pc
                totals["have_prod"] += 1
            verdict = _verdict(nc, vc).split(" ")[0]  # BETTER / WORSE / same
            p(f"  {store:<6} {zone:<10} {nc:>10.0f}"
              f" {str(round(vc) if vc else '—'):>10}"
              f" {_d(nc, vc):>9} {_pct(nc, vc):>7}"
              f"  {str(round(pc) if pc else '—'):>10}"
              f" {_d(nc, pc):>9} {_pct(nc, pc):>7}"
              f"  {verdict}")

    p("  " + "-" * 91)
    p(f"  {'TOTAL':<6} {'(walk zones)':10} {totals['new']:>10.0f}"
      f" {totals['v1']:>10.0f}"
      f" {_d(totals['new'], totals['v1']):>9} {_pct(totals['new'], totals['v1']):>7}"
      f"  {totals['prod']:>10.0f}"
      f" {_d(totals['new'], totals['prod']):>9} {_pct(totals['new'], totals['prod']):>7}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]

    # Worker mode — called by subprocess
    if "--worker" in args:
        idx = args.index("--worker")
        _worker(args[idx + 1], args[idx + 2])
        return

    positional = [a for a in args if not a.startswith("-")]
    flags = [a for a in args if a.startswith("-")]
    update_mode = "--update-baseline" in flags

    filter_store = positional[0] if len(positional) >= 1 else None
    filter_zone = positional[1].lower() if len(positional) >= 2 else None

    if filter_store and filter_store not in STORE_SETUPS:
        print(f"Unknown store: {filter_store}. Options: {list(STORE_SETUPS)}")
        sys.exit(1)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")

    stores = [filter_store] if filter_store else list(STORE_SETUPS)
    zones = [filter_zone] if filter_zone else ALL_ZONES
    total = len(stores) * len(zones)
    done = 0

    all_results: Dict = {}
    store_results: Dict = {}  # for --update-baseline

    for store in stores:
        baseline = load_baseline(store)
        store_results[store] = {}

        for zone in zones:
            done += 1
            prod = baseline.get("zones", {}).get(zone, {}).get("prod")
            v1 = baseline.get("zones", {}).get(zone, {}).get("cvrp_v1")
            out_path = os.path.join(RESULTS_DIR, f"exp_{ts}_{store}_{zone}.txt")

            print(f"[{done}/{total}] store={store} zone={zone} ...", end=" ", flush=True)
            new, elapsed, ok = run_zone(store, zone, out_path)

            # Detect matrix coverage error
            if new and "error" in new:
                in_m = new.get("in_matrix", "?")
                total_z = new.get("zone_items", "?")
                print(f"{elapsed:.1f}s  SKIP (matrix coverage {in_m}/{total_z} items)")
                all_results[(store, zone)] = (None, prod, v1, elapsed, False)
                store_results[store][zone] = all_results[(store, zone)]
                continue

            print(f"{elapsed:.1f}s  {'OK' if ok else 'FAIL'}")

            if ok:
                print_zone_block(store, zone, new, prod, v1, elapsed)  # stdout
                with open(out_path, "a") as f:
                    print_zone_block(store, zone, new, prod, v1, elapsed, file=f)

            all_results[(store, zone)] = (new or {}, prod, v1, elapsed, ok)
            store_results[store][zone] = all_results[(store, zone)]

    # Summary
    summary_path = os.path.join(RESULTS_DIR, f"exp_{ts}_summary.txt")
    with open(summary_path, "w") as f:
        f.write(f"Experiment: {datetime.now().isoformat()}\n")
        print_summary(all_results, file=f)

    print_summary(all_results)
    print(f"\nDetailed results: results/exp_{ts}_<store>_<zone>.txt")
    print(f"Summary:          {os.path.relpath(summary_path)}")

    if update_mode:
        print("\n[--update-baseline] Promoting current results to new v1.0 baseline ...")
        for store in stores:
            data = load_baseline(store)
            if not data:
                data = {"store": store, "zones": {}}
            for zone, (new, prod, v1, elapsed, ok) in store_results[store].items():
                if not ok:
                    continue
                if zone == "security":
                    entry = {"trolleys": new["trolleys"],
                             "logical_totes": new["totes"],
                             "physical_totes": new["physical_totes"]}
                else:
                    entry = {k: new[k] for k in
                             ("trolleys","totes","walk_m","uturns","cost")}
                data.setdefault("zones", {}).setdefault(zone, {})["cvrp_v1"] = entry
            data["captured_at"] = datetime.now().strftime("%Y-%m-%d")
            save_baseline(store, data)
            print(f"  Updated baselines/{store}.json")


if __name__ == "__main__":
    main()
