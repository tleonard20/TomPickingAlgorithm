"""Run all zone validators across all stores and save results.

Usage:
    python run_validation.py              # all stores, all zones
    python run_validation.py 1052         # one store, all zones
    python run_validation.py 1052 chilled # one store, one zone

Output:
    results/YYYY-MM-DD_HHMM_<store>_<zone>.txt  — full validator output
    results/YYYY-MM-DD_HHMM_summary.txt          — headline metrics table

Runtime (sequential, approximate):
    Store 1052:  Ambient ~4m20s  Chilled ~1m45s  Freezer ~2s
    Store 1030:  Ambient ~3m40s  Chilled ~1m05s  Freezer ~4s
    Total all:   ~12 minutes
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(THIS_DIR, "results")

# ---------------------------------------------------------------------------
# Config: which stores / zones / setup modules / validator modules
# ---------------------------------------------------------------------------

STORES = {
    "1052": {
        "setup": "_store_1052_setup",
        "zones": {
            "ambient": "deep_dive_ambient_v4_2",
            "chilled": "deep_dive_chilled",
            "freezer": "deep_dive_freezer",
        },
    },
    "1030": {
        "setup": "_store_1030_setup",
        "zones": {
            "ambient": "deep_dive_ambient_v4_2",
            "chilled": "deep_dive_chilled",
            "freezer": "deep_dive_freezer",
        },
    },
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_validator(store: str, zone: str, setup_mod: str, validator_mod: str,
                  out_path: str) -> Tuple[bool, float, str]:
    """Run one validator as a subprocess. Returns (success, elapsed_s, output)."""
    script = (
        f"import sys; sys.path.insert(0, {repr(THIS_DIR)}); "
        f"import {setup_mod}; "
        f"import {validator_mod} as _v; "
        f"_v.main()"
    )
    t0 = time.time()
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=THIS_DIR,
    )
    elapsed = time.time() - t0
    output = result.stdout
    if result.returncode != 0:
        output += f"\n\n--- STDERR ---\n{result.stderr}"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)
    return result.returncode == 0, elapsed, output


# ---------------------------------------------------------------------------
# Parsers — extract headline metrics from validator output
# ---------------------------------------------------------------------------

def _find(pattern: str, text: str, cast=float) -> Optional[float]:
    m = re.search(pattern, text)
    if m:
        try:
            return cast(m.group(1).replace(",", ""))
        except (ValueError, IndexError):
            pass
    return None


def parse_ambient(text: str) -> Optional[Dict]:
    """Parse the 4-way Ambient summary block (v4.2 vs PROD columns)."""
    # Headline summary table lines look like:
    #   trolleys                   132         132          132          136
    #   total walk (m)         31187.0     31187.0      31187.0      32889.6
    #   total U-turns               82          82           82          134
    #   cost (walk + 4·U)      31515.0     31515.0      31515.0      33425.0
    trolleys_m = re.search(
        r"trolleys\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", text)
    walk_m = re.search(
        r"total walk \(m\)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", text)
    uturn_m = re.search(
        r"total U-turns\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", text)
    cost_m = re.search(
        r"cost \(walk \+ 4.U\)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", text)
    if not (trolleys_m and walk_m and cost_m):
        return None
    new_tr = int(trolleys_m.group(3))
    prod_tr = int(trolleys_m.group(4))
    new_walk = float(walk_m.group(3))
    prod_walk = float(walk_m.group(4))
    new_ut = int(uturn_m.group(3)) if uturn_m else 0
    prod_ut = int(uturn_m.group(4)) if uturn_m else 0
    new_cost = float(cost_m.group(3))
    prod_cost = float(cost_m.group(4))
    return dict(new_trolleys=new_tr, prod_trolleys=prod_tr,
                new_walk=new_walk, prod_walk=prod_walk,
                new_uturns=new_ut, prod_uturns=prod_ut,
                new_cost=new_cost, prod_cost=prod_cost)


def parse_zone(text: str) -> Optional[Dict]:
    """Parse Chilled / Freezer summary block (new vs prod two-column)."""
    tr_m = re.search(r"trolleys:\s+new=(\d+)\s+prod=(\d+)", text)
    walk_m = re.search(r"total walk:\s+new=\s*([\d.]+)\s*m\s+prod=\s*([\d.]+)\s*m", text)
    ut_m = re.search(r"total U-turn:\s+new=(\d+)\s+prod=(\d+)", text)
    cost_m = re.search(r"cost \(w\+4U\):\s+new=\s*([\d.]+)\s+prod=\s*([\d.]+)", text)
    if not tr_m:
        return None
    return dict(
        new_trolleys=int(tr_m.group(1)),
        prod_trolleys=int(tr_m.group(2)),
        new_walk=float(walk_m.group(1)) if walk_m else 0.0,
        prod_walk=float(walk_m.group(2)) if walk_m else 0.0,
        new_uturns=int(ut_m.group(1)) if ut_m else 0,
        prod_uturns=int(ut_m.group(2)) if ut_m else 0,
        new_cost=float(cost_m.group(1)) if cost_m else 0.0,
        prod_cost=float(cost_m.group(2)) if cost_m else 0.0,
    )


def parse_security(text: str) -> Optional[Dict]:
    """Parse Security summary block (trolleys + tote consolidation, no walk)."""
    tr_m = re.search(r"trolleys:\s+new=(\d+)\s+prod=(\d+)", text)
    log_m = re.search(r"logical totes:\s+new=(\d+)\s+prod=(\d+)", text)
    phys_m = re.search(r"physical totes:\s+new=(\d+)", text)
    if not tr_m:
        return None
    return dict(
        new_trolleys=int(tr_m.group(1)),
        prod_trolleys=int(tr_m.group(2)),
        new_logical=int(log_m.group(1)) if log_m else None,
        prod_logical=int(log_m.group(2)) if log_m else None,
        new_physical=int(phys_m.group(1)) if phys_m else None,
        new_walk=0.0, prod_walk=0.0,
        new_uturns=0, prod_uturns=0,
        new_cost=0.0, prod_cost=0.0,
    )


PARSERS = {
    "ambient": parse_ambient,
    "chilled": parse_zone,
    "freezer": parse_zone,
    "security": parse_security,
}


# ---------------------------------------------------------------------------
# Summary table printer
# ---------------------------------------------------------------------------

def _pct(new, prod):
    if prod and prod != 0:
        return f"{(new - prod) / abs(prod) * 100:+.1f}%"
    return "n/a"


def print_summary(results: Dict, file=None):
    def p(*args, **kwargs):
        print(*args, **kwargs)
        if file:
            print(*args, **kwargs, file=file)

    p("\n" + "=" * 90)
    p(" VALIDATION SUMMARY — NEW (CVRP v1.0) vs PRODUCTION")
    p("=" * 90)

    # Walking zones
    p(f"\n{'store':<6} {'zone':<10} {'new_tr':>6} {'prod_tr':>7} "
      f"{'new_walk':>10} {'prod_walk':>10} {'walk_δ':>8} {'walk_%':>7} "
      f"{'new_cost':>10} {'prod_cost':>10} {'cost_δ':>8} {'cost_%':>7}")
    p("-" * 90)

    agg = {"new_walk": 0, "prod_walk": 0, "new_cost": 0, "prod_cost": 0,
           "new_tr": 0, "prod_tr": 0}

    for store in ["1052", "1030"]:
        for zone in ["ambient", "chilled", "freezer"]:
            key = (store, zone)
            if key not in results:
                continue
            ok, elapsed, m = results[key]
            if not ok or m is None:
                p(f"{store:<6} {zone:<10}  {'ERROR':>6}")
                continue
            walk_d = m["new_walk"] - m["prod_walk"]
            cost_d = m["new_cost"] - m["prod_cost"]
            p(f"{store:<6} {zone:<10} "
              f"{m['new_trolleys']:>6d} {m['prod_trolleys']:>7d} "
              f"{m['new_walk']:>10.0f} {m['prod_walk']:>10.0f} "
              f"{walk_d:>+8.0f} {_pct(m['new_walk'], m['prod_walk']):>7} "
              f"{m['new_cost']:>10.0f} {m['prod_cost']:>10.0f} "
              f"{cost_d:>+8.0f} {_pct(m['new_cost'], m['prod_cost']):>7}")
            agg["new_walk"] += m["new_walk"]
            agg["prod_walk"] += m["prod_walk"]
            agg["new_cost"] += m["new_cost"]
            agg["prod_cost"] += m["prod_cost"]
            agg["new_tr"] += m["new_trolleys"]
            agg["prod_tr"] += m["prod_trolleys"]

    p("-" * 90)
    p(f"{'TOTAL':<6} {'(all zones)':10} "
      f"{agg['new_tr']:>6d} {agg['prod_tr']:>7d} "
      f"{agg['new_walk']:>10.0f} {agg['prod_walk']:>10.0f} "
      f"{agg['new_walk']-agg['prod_walk']:>+8.0f} "
      f"{_pct(agg['new_walk'], agg['prod_walk']):>7} "
      f"{agg['new_cost']:>10.0f} {agg['prod_cost']:>10.0f} "
      f"{agg['new_cost']-agg['prod_cost']:>+8.0f} "
      f"{_pct(agg['new_cost'], agg['prod_cost']):>7}")

    # Security (no walk)
    p(f"\n{'store':<6} {'zone':<10} {'new_tr':>6} {'prod_tr':>7} "
      f"{'new_logical':>12} {'prod_logical':>13} {'new_physical':>13}")
    p("-" * 65)
    for store in ["1052", "1030"]:
        key = (store, "security")
        if key not in results:
            continue
        ok, elapsed, m = results[key]
        if not ok or m is None:
            p(f"{store:<6} {'security':<10}  {'ERROR':>6}")
            continue
        p(f"{store:<6} {'security':<10} "
          f"{m['new_trolleys']:>6d} {m['prod_trolleys']:>7d} "
          f"{str(m.get('new_logical', '-')):>12} "
          f"{str(m.get('prod_logical', '-')):>13} "
          f"{str(m.get('new_physical', '-')):>13}")

    # Runtimes
    p(f"\n{'store':<6} {'zone':<10} {'elapsed':>10}")
    p("-" * 30)
    for store in ["1052", "1030"]:
        for zone in ["ambient", "chilled", "freezer", "security"]:
            key = (store, zone)
            if key not in results:
                continue
            ok, elapsed, _ = results[key]
            status = "OK" if ok else "FAIL"
            p(f"{store:<6} {zone:<10} {elapsed:>8.1f}s  {status}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Parse optional CLI args: [store] [zone]
    args = sys.argv[1:]
    filter_store = args[0] if len(args) >= 1 else None
    filter_zone = args[1] if len(args) >= 2 else None

    if filter_store and filter_store not in STORES:
        print(f"Unknown store: {filter_store}. Options: {list(STORES)}")
        sys.exit(1)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")

    results: Dict = {}
    total_jobs = sum(
        len(cfg["zones"])
        for s, cfg in STORES.items()
        if filter_store is None or s == filter_store
    )
    if filter_zone:
        total_jobs = sum(
            1 for s, cfg in STORES.items()
            if (filter_store is None or s == filter_store) and filter_zone in cfg["zones"]
        )
    done = 0

    for store, cfg in STORES.items():
        if filter_store and store != filter_store:
            continue
        for zone, validator_mod in cfg["zones"].items():
            if filter_zone and zone != filter_zone:
                continue
            done += 1
            out_path = os.path.join(RESULTS_DIR, f"{ts}_{store}_{zone}.txt")
            print(f"[{done}/{total_jobs}] Running store={store} zone={zone} ...",
                  end=" ", flush=True)
            ok, elapsed, output = run_validator(
                store, zone, cfg["setup"], validator_mod, out_path)
            status = "OK" if ok else "FAIL"
            print(f"{elapsed:.1f}s  {status}  -> {os.path.relpath(out_path)}")

            parsed = PARSERS[zone](output)
            results[(store, zone)] = (ok, elapsed, parsed)

    # Print + save summary
    summary_path = os.path.join(RESULTS_DIR, f"{ts}_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"Generated: {datetime.now().isoformat()}\n")
        print_summary(results, file=f)

    print_summary(results)
    print(f"\nResults saved to: {RESULTS_DIR}/")
    print(f"Summary:          {os.path.relpath(summary_path)}")


if __name__ == "__main__":
    main()
