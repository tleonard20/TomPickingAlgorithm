"""tote_trolley_optimizer_v4.py — joint (non-greedy) trolley builder.

This module is a SIBLING of `tote_trolley_optimizer_v2.py`. It reuses v2's tote
builder (`build_totes_for_zone`), trip_cost engine, distance matrix, and the
post-hoc SA refinement (G1+G2+G3) untouched. The ONLY thing v4 changes is the
trolley-construction phase.

------------------------------------------------------------
v2 trolley build (greedy)        |   v4 trolley build (joint)
---------------------------------|--------------------------------
1. While pool non-empty:         |   1. Determine K = ceil(n/6).
   pick BEST trolley from pool;  |   2. Seed K trolleys SIMULTANEOUSLY with
   remove its 6 totes; loop.     |      diverse PAT-anchor totes.
2. Greedy locks in early picks   |   3. Joint fill: every remaining tote
   - last trolleys see depleted  |      evaluates marginal trip_cost across
   pool.                         |      ALL K trolleys, assigns to lowest-
3. Phase 2 SA permutes already-  |      cost feasible.
   formed trolleys.              |   4. Iterated reassignment: each tote
                                 |      can move to a different trolley if
                                 |      total cost drops.
                                 |   5. Same SA polish as v2 (reused).
------------------------------------------------------------

Honest expectation: the v3 attempt at "trolley-first" failed catastrophically
(+4,701m vs v2) because it used a min-walk PROXY at the item level. v4 avoids
that trap by:
  - keeping v2's tote boundaries (1 order per tote, capacity-respecting)
  - using ACTUAL trip_cost (not pairwise-walk) for every assignment decision
  - filtering candidates by aisle Jaccard to keep runtime tractable
    (~793 totes × ~10 candidates × ~5ms trip_cost = ~40s per pass)

PAT/HC11/zone constraints enforced throughout — same safety assertions as v2.

Run:
    python deep_dive_ambient_v4.py    # 3-way validator: PROD vs v2 vs v4
"""
from __future__ import annotations

import math
import os
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

# Import v2 wholesale — we reuse everything except build_trolleys_*.
import tote_trolley_optimizer_v2 as v2  # type: ignore

# Re-export v2 symbols at module level so callers can use either v2 or v4 without
# tracking which exposed what.
DistanceMatrix = v2.DistanceMatrix
Item = v2.Item
ToteResult = v2.ToteResult
TrolleyResult = v2.TrolleyResult
StoreConfig = v2.StoreConfig
load_orders = v2.load_orders
build_totes_for_zone = v2.build_totes_for_zone
analyse_baseline = v2.analyse_baseline
trip_cost = v2.trip_cost
ORDERS_CSV = v2.ORDERS_CSV
DIST_MATRIX_CSV = v2.DIST_MATRIX_CSV


# ============================================================================
# Helpers reused from v2
# ============================================================================

_path_cost = v2._path_cost
_trolley_distinct_tids = v2._trolley_distinct_tids
_trolley_zones = v2._trolley_zones
_tote_aisle_int_set = v2._tote_aisle_int_set
_trolley_aisle_int_set = v2._trolley_aisle_int_set
_aisle_jaccard = v2._aisle_jaccard
_tote_anchor_transit = v2._tote_anchor_transit
_build_trolley_result = v2._build_trolley_result
_sa_refine_trolleys_spt = v2._sa_refine_trolleys_spt


def _trip(items: List[Item], matrix: DistanceMatrix, cfg: StoreConfig) -> float:
    d, u, _ = trip_cost(items, matrix, cfg)
    return _path_cost(d, u, cfg)


def _trolley_trip(totes: Sequence[ToteResult], matrix: DistanceMatrix,
                   cfg: StoreConfig) -> float:
    items = [it for t in totes for it in t.items]
    return _trip(items, matrix, cfg)


def _tote_tids(t: ToteResult) -> Set[str]:
    return {it.transit_id for it in t.items if it.transit_id}


def _tote_zones(t: ToteResult) -> Set[str]:
    return {t.zone} if t.zone else set()


# ============================================================================
# Phase 1+2 — Priority-ordered bin-packing into K=ceil(n/6) empty bins.
#
# v4.0 (refuted) used upfront alpha-anchor seeding (1 tote per anchor TID) which
# pre-committed each bin's TID set before fill started → 19 overflow totes.
#
# v4.1 (current) drops upfront seeding entirely. It uses K empty bins and
# processes totes in PAT-difficulty descending order. The first K totes naturally
# seed the bins; later totes are placed by lowest marginal trip_cost. If a tote
# can't fit anywhere, eviction repair (2-step swap chain) frees room. The
# trolley count is hard-capped at K — never spawn an overflow trolley.
# ============================================================================


def _bin_marginal_cost(tote_items: List[Item], bin_items: List[Item], bin_cost: float,
                        matrix: DistanceMatrix, cfg: StoreConfig) -> float:
    """Marginal trip_cost of adding `tote_items` to a bin currently with `bin_items`."""
    if not bin_items:
        # Empty bin: marginal cost = trip_cost of just the new tote's items.
        return _trip(list(tote_items), matrix, cfg)
    new_cost = _trip(bin_items + list(tote_items), matrix, cfg)
    return new_cost - bin_cost


def _evict_and_place(tote_idx: int, totes: List[ToteResult],
                      tids_cache: Dict[int, Set[str]],
                      aisles_cache: Dict[int, Set[int]],
                      trolleys: List[List[ToteResult]],
                      t_aisles: List[Set[int]],
                      t_tids: List[Set[str]],
                      t_zones: List[Set[str]],
                      t_items: List[List[Item]],
                      t_costs: List[float],
                      cfg: StoreConfig, matrix: DistanceMatrix,
                      pat_n: int, max_size: int) -> bool:
    """Repair attempt when tote can't directly fit any of K bins.

    Strategy: find a 2-step swap (T, Y, U) such that
        (a) removing Y from T frees PAT/capacity room for the new tote in T,
        (b) Y can move to U feasibly, and
        (c) total cost-delta is finite.
    Picks the cheapest such (T, Y, U). Returns True if applied, False if no swap exists.
    """
    tote_i = totes[tote_idx]
    new_tids = tids_cache[tote_idx]
    new_aisles = aisles_cache[tote_idx]
    new_zone = tote_i.zone

    best = None  # (delta, T, y_pos, U, new_T_cost, new_U_cost, T_items_after, U_items_after, Y_tids, Y_aisles)

    for T in range(len(trolleys)):
        if not trolleys[T]:
            continue  # empty bins were already feasible — must be PAT/zone block
        if t_zones[T] and new_zone and new_zone not in t_zones[T]:
            continue
        # Try evicting each tote Y from T
        for y_pos, Y in enumerate(trolleys[T]):
            Y_tids = {it.transit_id for it in Y.items if it.transit_id}
            # Compute T's TIDs without Y (iterate other totes, never use set subtraction).
            T_minus_y_tids: Set[str] = set()
            for k_pos, other in enumerate(trolleys[T]):
                if k_pos != y_pos:
                    T_minus_y_tids.update(it.transit_id for it in other.items if it.transit_id)
            # Can the new tote fit in T \ {Y}?
            if len(T_minus_y_tids | new_tids) > pat_n:
                continue
            # Capacity: |T \ {Y} ∪ {new}| = len(T_list), already ≤ max_size.
            # Now find U for Y.
            for U in range(len(trolleys)):
                if U == T:
                    continue
                if len(trolleys[U]) >= max_size:
                    continue
                if t_zones[U] and Y.zone and Y.zone not in t_zones[U]:
                    continue
                if len(t_tids[U] | Y_tids) > pat_n:
                    continue
                # Both moves feasible. Compute cost delta.
                T_items_after = [it for k_pos, t in enumerate(trolleys[T])
                                 if k_pos != y_pos for it in t.items] + list(tote_i.items)
                U_items_after = list(t_items[U]) + list(Y.items)
                new_T_cost = _trip(T_items_after, matrix, cfg) if T_items_after else 0.0
                new_U_cost = _trip(U_items_after, matrix, cfg)
                delta = (new_T_cost + new_U_cost) - (t_costs[T] + t_costs[U])
                if best is None or delta < best[0]:
                    Y_aisles = _tote_aisle_int_set(Y)
                    best = (delta, T, y_pos, U, new_T_cost, new_U_cost,
                            T_items_after, U_items_after, Y_tids, Y_aisles)

    if best is None:
        return False

    delta, T, y_pos, U, new_T_cost, new_U_cost, T_items_after, U_items_after, Y_tids, Y_aisles = best
    Y = trolleys[T][y_pos]

    # Apply: remove Y from T, append new tote to T, append Y to U.
    trolleys[T] = [t for k_pos, t in enumerate(trolleys[T]) if k_pos != y_pos] + [tote_i]
    trolleys[U] = list(trolleys[U]) + [Y]

    # Recompute caches for T (Y removed, new tote added).
    new_T_aisles: Set[int] = set()
    new_T_tids: Set[str] = set()
    new_T_zones: Set[str] = set()
    for tt in trolleys[T]:
        new_T_aisles.update(_tote_aisle_int_set(tt))
        new_T_tids.update(it.transit_id for it in tt.items if it.transit_id)
        if tt.zone:
            new_T_zones.add(tt.zone)
    t_aisles[T] = new_T_aisles
    t_tids[T] = new_T_tids
    t_zones[T] = new_T_zones
    t_items[T] = T_items_after
    t_costs[T] = new_T_cost

    # Update U (Y added).
    t_aisles[U] = t_aisles[U] | Y_aisles
    t_tids[U] = t_tids[U] | Y_tids
    if Y.zone:
        t_zones[U] = t_zones[U] | {Y.zone}
    t_items[U] = U_items_after
    t_costs[U] = new_U_cost

    return True


def _assign_to_k_bins(totes: List[ToteResult], k: int, cfg: StoreConfig,
                       matrix: DistanceMatrix, top_k_candidates: int = 12
                       ) -> Tuple[List[List[ToteResult]], int, int]:
    """Priority-ordered bin-packing into K empty bins. Hard cap K — never spawns
    a (K+1)th trolley. Falls back to eviction repair on direct-fit failure.

    Sort key (descending difficulty):
      1. number of TIDs (more TIDs → more PAT-constrained)
      2. aisle span (wider → harder to nest tightly)
      3. anchor TID alpha (deterministic tiebreaker)

    Returns (trolleys, n_eviction_repairs, n_unplaceable).
    """
    pat_n = cfg.pick_across_trucks
    max_size = cfg.trolley_max_totes

    trolleys: List[List[ToteResult]] = [[] for _ in range(k)]
    t_aisles: List[Set[int]] = [set() for _ in range(k)]
    t_tids: List[Set[str]] = [set() for _ in range(k)]
    t_zones: List[Set[str]] = [set() for _ in range(k)]
    t_items: List[List[Item]] = [[] for _ in range(k)]
    t_costs: List[float] = [0.0 for _ in range(k)]

    indices = list(range(len(totes)))
    aisles_cache = {i: _tote_aisle_int_set(totes[i]) for i in indices}
    tids_cache = {i: _tote_tids(totes[i]) for i in indices}

    indices.sort(key=lambda i: (-len(tids_cache[i]),
                                 -len(aisles_cache[i]),
                                 _tote_anchor_transit(totes[i])))

    n_repairs = 0
    n_unplaceable = 0

    for i in indices:
        tote = totes[i]
        # Step 1: feasibility scan across all K bins.
        feasible: List[int] = []
        for j in range(k):
            if len(trolleys[j]) >= max_size:
                continue
            if t_zones[j] and tote.zone and tote.zone not in t_zones[j]:
                continue
            if len(t_tids[j] | tids_cache[i]) > pat_n:
                continue
            feasible.append(j)

        if not feasible:
            # Step 2: eviction repair.
            ok = _evict_and_place(i, totes, tids_cache, aisles_cache,
                                  trolleys, t_aisles, t_tids, t_zones,
                                  t_items, t_costs, cfg, matrix, pat_n, max_size)
            if ok:
                n_repairs += 1
                continue
            # If repair fails, this is a hard failure — but we still need to place
            # the tote somewhere. Last-resort: if any bin has slack capacity, place
            # there ignoring PAT (this should never trigger if v2 has a solution).
            n_unplaceable += 1
            # Pick the bin with the least PAT pressure (fewest TIDs already present).
            slack_bins = [(len(t_tids[j]), j) for j in range(k) if len(trolleys[j]) < max_size]
            if slack_bins:
                slack_bins.sort()
                j = slack_bins[0][1]
                trolleys[j].append(tote)
                t_aisles[j] |= aisles_cache[i]
                t_tids[j] |= tids_cache[i]
                t_zones[j] |= _tote_zones(tote)
                t_items[j].extend(tote.items)
                t_costs[j] = _trip(t_items[j], matrix, cfg)
            continue

        # Step 3: rank feasible bins.
        scored: List[Tuple[float, int]] = []
        for j in feasible:
            if not trolleys[j]:
                # Empty bin — neutral aisle-Jaccard rank; let the marginal-cost
                # step decide. Push slightly negative so it's not always last.
                ov = -0.5
            else:
                ov = _aisle_jaccard(aisles_cache[i], t_aisles[j])
            # Tiny size-balance bonus to encourage even fill.
            balance = (max_size - len(trolleys[j])) * 0.001
            scored.append((-(ov + balance), j))
        scored.sort()
        cands = [j for _, j in scored[:top_k_candidates]]

        # Step 4: marginal trip_cost evaluation.
        best_j = -1
        best_marg = float("inf")
        for j in cands:
            marg = _bin_marginal_cost(tote.items, t_items[j], t_costs[j], matrix, cfg)
            if marg < best_marg:
                best_marg = marg
                best_j = j

        if best_j < 0:
            best_j = feasible[0]
            best_marg = _bin_marginal_cost(tote.items, t_items[best_j],
                                            t_costs[best_j], matrix, cfg)

        # Place.
        trolleys[best_j].append(tote)
        t_aisles[best_j] |= aisles_cache[i]
        t_tids[best_j] |= tids_cache[i]
        t_zones[best_j] |= _tote_zones(tote)
        t_items[best_j].extend(tote.items)
        if not trolleys[best_j][:-1]:
            # First tote in bin — set bin cost to absolute trip cost.
            t_costs[best_j] = best_marg
        else:
            t_costs[best_j] += best_marg

    return trolleys, n_repairs, n_unplaceable


# ============================================================================
# Phase 3 — Iterated reassignment
# ============================================================================


def _reassign_pass(trolleys: List[List[ToteResult]], cfg: StoreConfig,
                    matrix: DistanceMatrix, top_k_candidates: int = 8,
                    items_by_order: Optional[Dict[str, List[Item]]] = None) -> int:
    """One pass: for each tote, evaluate moving to a different feasible trolley.
    If the move strictly reduces total trip_cost across both donor and recipient,
    apply it. Returns the number of moves accepted in this pass.

    items_by_order (C1, 2026-05-11): when provided, reject any recipient whose
    cold_chain_compliance_time_s would exceed cfg.cold_chain_cap_min after the
    move. Preserves cold-chain compliance for cold-chain zones. No-op when None.
    """
    pat_n = cfg.pick_across_trucks
    max_size = cfg.trolley_max_totes
    cc_cap_s = cfg.cold_chain_cap_min * 60.0
    cc_zones = set(cfg.cold_chain_zones)
    moves = 0

    def _refresh(idx: int) -> None:
        # Recompute cached features for trolley idx.
        scores[idx] = _trolley_trip(trolleys[idx], matrix, cfg) if trolleys[idx] else 0.0
        t_aisles[idx] = _trolley_aisle_int_set(trolleys[idx])
        t_tids[idx] = _trolley_distinct_tids(trolleys[idx])
        t_zones_set[idx] = _trolley_zones(trolleys[idx])

    scores = [_trolley_trip(t, matrix, cfg) if t else 0.0 for t in trolleys]
    t_aisles = [_trolley_aisle_int_set(t) for t in trolleys]
    t_tids = [_trolley_distinct_tids(t) for t in trolleys]
    t_zones_set = [_trolley_zones(t) for t in trolleys]

    # Iterate over (donor_idx, tote_pos_in_donor) snapshots — donor list mutates.
    iter_order: List[Tuple[int, str]] = []
    for di, t_list in enumerate(trolleys):
        for t in t_list:
            iter_order.append((di, t.tote_id))
    # Mild shuffle of evaluation order so we don't always favor early trolleys.
    rng = random.Random(91337)
    rng.shuffle(iter_order)

    for donor_idx, tote_id in iter_order:
        # Resolve current position (donor may have shrunk).
        cur_pos = -1
        for k, t in enumerate(trolleys[donor_idx]):
            if t.tote_id == tote_id:
                cur_pos = k
                break
        if cur_pos < 0:
            continue
        if len(trolleys[donor_idx]) <= 1:
            continue  # don't empty a trolley via reassignment (split is a separate move)
        tote = trolleys[donor_idx][cur_pos]
        tote_aisles = _tote_aisle_int_set(tote)
        tote_tids = _tote_tids(tote)
        tote_zone = tote.zone
        # Donor candidate: cost without this tote.
        donor_without = trolleys[donor_idx][:cur_pos] + trolleys[donor_idx][cur_pos + 1:]
        donor_score_no = _trolley_trip(donor_without, matrix, cfg)
        donor_save = scores[donor_idx] - donor_score_no
        # Look at top-K recipient candidates by aisle Jaccard.
        feasible: List[Tuple[float, int]] = []
        for j, t_list in enumerate(trolleys):
            if j == donor_idx:
                continue
            if len(t_list) >= max_size:
                continue
            if t_zones_set[j] and tote_zone and tote_zone not in t_zones_set[j]:
                continue
            if len(t_tids[j] | tote_tids) > pat_n:
                continue
            ov = _aisle_jaccard(tote_aisles, t_aisles[j])
            feasible.append((-ov, j))
        feasible.sort()
        best_j = -1
        best_delta = 0.0  # need strictly < 0
        for _ov, j in feasible[:top_k_candidates]:
            new_recipient = trolleys[j] + [tote]
            # C1 (2026-05-11): cold-chain feasibility check for cold-chain zones.
            if items_by_order is not None and t_zones_set[j] & cc_zones:
                new_items = [it for t in new_recipient for it in t.items]
                if v2.cold_chain_compliance_time_s(new_items, items_by_order, matrix, cfg) > cc_cap_s + 1e-3:
                    continue
            new_score = _trolley_trip(new_recipient, matrix, cfg)
            recipient_cost = new_score - scores[j]
            delta = recipient_cost - donor_save  # net change to total
            if delta < best_delta:
                best_delta = delta
                best_j = j
        if best_j >= 0:
            trolleys[donor_idx] = donor_without
            trolleys[best_j] = trolleys[best_j] + [tote]
            _refresh(donor_idx)
            _refresh(best_j)
            moves += 1
    return moves


# ============================================================================
# Phase 4 — SA polish (reuse v2)
# ============================================================================
# We call v2._sa_refine_trolleys_spt directly — already includes G1+G2+G3
# steepest-descent and the post-SA PAT safety assertion.


# ============================================================================
# Main entry point
# ============================================================================


def build_trolleys_joint_v4(totes: List[ToteResult], zone: str, cfg: StoreConfig,
                             matrix: DistanceMatrix,
                             items_by_order: Dict[str, List[Item]],
                             top_k_fill: int = 10,
                             reassign_passes: int = 3
                             ) -> List[TrolleyResult]:
    """Joint multi-trolley construction (non-greedy), hard-capped at K=ceil(n/6).

    Pipeline:
      1. K = ceil(len(totes) / cfg.trolley_max_totes).
      2. Priority-ordered bin-packing into K empty bins (no upfront seeding).
         First K hardest-to-place totes naturally seed the bins; rest go into
         the bin with lowest marginal trip_cost. Eviction repair on direct-fit
         failure. Hard cap K — never spawns a (K+1)th trolley.
      3. Iterated reassignment: tote moves between trolleys if total drops.
      4. SA polish (reuse v2 _sa_refine_trolleys_spt with G1+G2+G3).
    """
    if not totes:
        return []
    use_affinity = zone in cfg.affinity_zones
    n = len(totes)
    k = max(1, math.ceil(n / cfg.trolley_max_totes))

    # Phase 1+2: priority-ordered bin-packing with eviction repair.
    trolleys, n_repairs, n_unplaceable = _assign_to_k_bins(
        totes, k, cfg, matrix, top_k_candidates=top_k_fill)

    # Drop empty bins (shouldn't happen unless len(totes) < k).
    trolleys = [t for t in trolleys if t]

    print(f"        [{zone}] v4 bin-pack: {len(trolleys)} trolleys (K={k}, "
          f"eviction_repairs={n_repairs}, unplaceable={n_unplaceable})", flush=True)
    if n_unplaceable > 0:
        print(f"        [{zone}] v4 WARNING: {n_unplaceable} tote(s) placed in PAT-relaxed "
              f"slack bins (would breach PAT cap; SA must repair)", flush=True)

    # Phase 3
    if reassign_passes > 0:
        for p in range(reassign_passes):
            moves = _reassign_pass(trolleys, cfg, matrix, top_k_candidates=8,
                                    items_by_order=items_by_order)
            total = sum(_trolley_trip(t, matrix, cfg) if t else 0.0 for t in trolleys)
            print(f"        [{zone}] v4 reassign pass {p+1}: {moves} moves, total cost={total:.1f}",
                  flush=True)
            if moves == 0:
                break

    # Drop empty trolleys (reassignment never empties one, but defensive).
    trolleys = [t for t in trolleys if t]

    pre_sa_total = sum(_trolley_trip(t, matrix, cfg) for t in trolleys)
    print(f"        [{zone}] v4 pre-SA total cost: {pre_sa_total:.1f} ({len(trolleys)} trolleys)",
          flush=True)

    # Phase 4 — SA polish (reuse v2). Same multi-seed best-of-N as v2.
    if use_affinity and cfg.enable_sa and len(trolleys) >= 2:
        baseline = pre_sa_total
        best_lists = trolleys
        best_score = baseline
        seed_scores: List[float] = []
        is_cold = zone in cfg.cold_chain_zones
        for s in range(cfg.sa_seeds):
            cand = _sa_refine_trolleys_spt(trolleys, cfg, matrix, seed=s * 17 + 7919,
                                            items_by_order=items_by_order,
                                            enforce_cold_chain=is_cold)
            score = sum(_trolley_trip(t, matrix, cfg) for t in cand)
            seed_scores.append(score)
            if score < best_score:
                best_score = score
                best_lists = cand
        seed_str = ", ".join(f"{x:.1f}" for x in seed_scores)
        print(f"        [{zone}] v4 SA seeds (cost): baseline={baseline:.1f}  seeds=[{seed_str}]"
              f"  best={best_score:.1f}", flush=True)
        if best_score < baseline:
            print(f"        [{zone}] v4 SA refinement: {baseline:.1f} -> {best_score:.1f} cost"
                  f" ({(baseline - best_score):.1f} saved)", flush=True)
        trolleys = best_lists

    # PAT safety assertion (mirrors v2's post-SA check).
    pat_n = cfg.pick_across_trucks
    for t_idx, t_list in enumerate(trolleys):
        actual = _trolley_distinct_tids(t_list)
        if len(actual) > pat_n:
            raise AssertionError(
                f"[v4 {zone}] trolley {t_idx} breaches PAT cap: "
                f"{len(actual)} distinct TIDs > {pat_n} (TIDs: {sorted(actual)})"
            )

    # Materialise TrolleyResult objects.
    results: List[TrolleyResult] = []
    seq = 0
    for sub in trolleys:
        if not sub:
            continue
        seq += 1
        tid = f"{zone[:2].upper()}TR_v4_{seq:04d}"
        tr = _build_trolley_result(tid, sub, matrix, cfg, items_by_order)
        results.append(tr)
    return results


# ============================================================================
# Convenience: same signature as v2.build_trolleys_rolling_pat_for_zone so a
# caller can swap engines via a flag.
# ============================================================================


def build_trolleys_rolling_pat_for_zone(totes: List[ToteResult], zone: str,
                                         cfg: StoreConfig, matrix: DistanceMatrix,
                                         items_by_order: Dict[str, List[Item]]
                                         ) -> List[TrolleyResult]:
    """v4 entry point with the same signature as v2's. Lets validators swap easily."""
    return build_trolleys_joint_v4(totes, zone, cfg, matrix, items_by_order)


# ============================================================================
# v4.2 — Warm-start hybrid (option b)
#
# Take v2's final TrolleyResult list as a starting state and run v4's joint
# reassignment + v2's SA polish on top. Guaranteed >= v2 by construction
# (steepest descent never accepts a worse total).
#
# Pipeline:
#   1. Accept prebuilt v2 TrolleyResult list.
#   2. Unwrap to List[List[ToteResult]] (tr.totes).
#   3. Run _reassign_pass up to `reassign_passes` times (stops early if 0 moves).
#   4. Run v2._sa_refine_trolleys_spt with cfg.sa_seeds best-of-N.
#   5. Repackage via _build_trolley_result.
#
# All existing PAT/HC/zone constraint logic is inherited from the underlying
# helpers — no duplicate enforcement needed here.
# ============================================================================


def build_trolleys_warmstart_hybrid(prebuilt_v2_trolleys: List[TrolleyResult],
                                     zone: str, cfg: StoreConfig,
                                     matrix: DistanceMatrix,
                                     items_by_order: Dict[str, List[Item]],
                                     reassign_passes: int = 3
                                     ) -> List[TrolleyResult]:
    """Warm-start v4 reassignment + v2 SA from v2's final state.

    Returns a List[TrolleyResult] with the same trolley count as the input
    (reassignment never empties a trolley; SA may shrink the count only as a
    side effect, which is preserved).
    """
    if not prebuilt_v2_trolleys:
        return []
    use_affinity = zone in cfg.affinity_zones

    # Unwrap to inner List[List[ToteResult]].
    tote_lists: List[List[ToteResult]] = [list(tr.totes) for tr in prebuilt_v2_trolleys
                                          if tr.totes]

    pre_total = sum(_trolley_trip(t, matrix, cfg) for t in tote_lists)
    print(f"        [{zone}] v4.2 warmstart pre-reassign total cost: {pre_total:.1f} "
          f"({len(tote_lists)} trolleys)", flush=True)

    # Phase 3 — reassignment passes.
    if reassign_passes > 0:
        for p in range(reassign_passes):
            moves = _reassign_pass(tote_lists, cfg, matrix, top_k_candidates=8,
                                    items_by_order=items_by_order)
            total = sum(_trolley_trip(t, matrix, cfg) if t else 0.0 for t in tote_lists)
            print(f"        [{zone}] v4.2 reassign pass {p+1}: {moves} moves, "
                  f"total cost={total:.1f}", flush=True)
            if moves == 0:
                break

    tote_lists = [t for t in tote_lists if t]

    pre_sa_total = sum(_trolley_trip(t, matrix, cfg) for t in tote_lists)
    print(f"        [{zone}] v4.2 pre-SA total cost: {pre_sa_total:.1f} "
          f"({len(tote_lists)} trolleys)", flush=True)

    # Phase 4 — SA polish (reuse v2's _sa_refine_trolleys_spt).
    if use_affinity and cfg.enable_sa and len(tote_lists) >= 2:
        baseline = pre_sa_total
        best_lists = tote_lists
        best_score = baseline
        seed_scores: List[float] = []
        is_cold = zone in cfg.cold_chain_zones
        for s in range(cfg.sa_seeds):
            cand = _sa_refine_trolleys_spt(tote_lists, cfg, matrix, seed=s * 17 + 7919,
                                            items_by_order=items_by_order,
                                            enforce_cold_chain=is_cold)
            score = sum(_trolley_trip(t, matrix, cfg) for t in cand)
            seed_scores.append(score)
            if score < best_score:
                best_score = score
                best_lists = cand
        seed_str = ", ".join(f"{x:.1f}" for x in seed_scores)
        print(f"        [{zone}] v4.2 SA seeds (cost): baseline={baseline:.1f}  "
              f"seeds=[{seed_str}]  best={best_score:.1f}", flush=True)
        if best_score < baseline:
            print(f"        [{zone}] v4.2 SA refinement: {baseline:.1f} -> {best_score:.1f} "
                  f"cost ({(baseline - best_score):.1f} saved)", flush=True)
        tote_lists = best_lists

    # PAT safety assertion (mirrors v2 and v4).
    pat_n = cfg.pick_across_trucks
    for t_idx, t_list in enumerate(tote_lists):
        actual = _trolley_distinct_tids(t_list)
        if len(actual) > pat_n:
            raise AssertionError(
                f"[v4.2 {zone}] trolley {t_idx} breaches PAT cap: "
                f"{len(actual)} distinct TIDs > {pat_n} (TIDs: {sorted(actual)})"
            )

    # Materialise TrolleyResult objects.
    results: List[TrolleyResult] = []
    seq = 0
    for sub in tote_lists:
        if not sub:
            continue
        seq += 1
        tid = f"{zone[:2].upper()}TR_v4_2_{seq:04d}"
        tr = _build_trolley_result(tid, sub, matrix, cfg, items_by_order)
        results.append(tr)
    return results


# ============================================================================
# Production entry point (2026-05-11) — v4.2 warm-start hybrid is PROD candidate.
#
# Drop-in replacement for v2.build_trolleys_rolling_pat_for_zone. Same signature,
# same return type. Internally:
#   1. Calls v2.build_trolleys_rolling_pat_for_zone for the composition + initial SA.
#   2. Wraps with build_trolleys_warmstart_hybrid for joint reassignment + 2nd SA.
#
# Validated 2026-05-11 on Ambient store 1419: 32,020 m / 240.8 m/trolley /
# cost 38,568. -3.3 m/trolley vs v2 alone, -6.3 m/trolley vs PROD.
# ============================================================================


PROD_REASSIGN_PASSES = 3  # pin: pass 4+ saturates per v4.2 6-pass test (2026-05-11)


def build_trolleys_production(totes: List[ToteResult], zone: str, cfg: StoreConfig,
                               matrix: DistanceMatrix,
                               items_by_order: Dict[str, List[Item]]
                               ) -> List[TrolleyResult]:
    """Production trolley builder (v4.2 warm-start hybrid). Drop-in replacement
    for v2.build_trolleys_rolling_pat_for_zone with the same signature.

    Pipeline:
      Phase A — v2 composition + SA polish (greedy two-phase picker + G1+G2+G3 SA).
      Phase B — v4 joint reassignment (PROD_REASSIGN_PASSES iters) + 2nd SA polish.

    Composition decisions (which 6 totes co-trolley) come from v2's
    trip_cost-in-the-loop two-phase picker, which is empirically the best
    composition strategy tested (joint paradigms all regressed — see v4.1).
    Polish decisions come from v4's exhaustive single-tote-across-all-trolleys
    reassignment, which catches non-worst-but-misplaced totes that v2's SA
    cannot reach via its G3-restricted-to-worst-tote portfolio.

    Non-affinity zones (no affinity_zones membership) skip Phase B and fall
    back to v2 directly — Phase B's reassignment relies on the joint-feasibility
    structure that the affinity-driven composition produces.
    """
    if not totes:
        return []
    v2_trolleys = v2.build_trolleys_rolling_pat_for_zone(
        totes, zone, cfg, matrix, items_by_order)
    if zone not in cfg.affinity_zones:
        return v2_trolleys
    return build_trolleys_warmstart_hybrid(
        v2_trolleys, zone, cfg, matrix, items_by_order,
        reassign_passes=PROD_REASSIGN_PASSES)
