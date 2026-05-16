"""
tote_trolley_optimizer_v2.py
Framework-compliant tote and trolley optimiser.

Implements the hard requirements (T-HR, TR-HR, DA-HR) and hard constraints
(HC1-HC16) defined in Corefiles/AlgorithmValidationFramework.md.

Key differences vs v1 (tote_trolley_optimizer.py):
  - Multi-zone (Ambient / Chilled / Freezer / Security) per HC4/HC5
  - Frozen up-to-2-order tote pooling per T-HR-1 / HC3
  - Real distance matrix as primary distance source (geometric fallback)
  - Pick Across Trucks staging gate using alpha-sequential TransitID
  - Goal-time calculation per Trolley Goal time calculation.md
  - Cold-chain 30-min cap enforced via build-time trolley split (HC12)
  - uturn_count emitted on every trolley (TR-HR-8)
  - Structured AlgorithmException for exceeded capacities and invariant breaches
  - Single-source ingest from TestStore/1419Orders.csv

Run:
    python tote_trolley_optimizer_v2.py
"""
from __future__ import annotations

import csv
import json
import math
import os
import random
import sys
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

try:  # G9 CP-SAT LNS (optional dep)
    from ortools.sat.python import cp_model as _cp_model
    _HAVE_CPSAT = True
except ImportError:
    _HAVE_CPSAT = False
    _cp_model = None  # type: ignore

# ----------------------------------------------------------------------------
# 0. Paths and global defaults
# ----------------------------------------------------------------------------

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(THIS_DIR)
TEST_STORE_DIR = os.path.join(PROJECT_ROOT, "TestStore")
ORDERS_CSV = os.path.join(TEST_STORE_DIR, "1419Orders.csv")
DIST_MATRIX_CSV = os.path.join(TEST_STORE_DIR, "1419_dist_mat_onlineaisles_core.csv")
OUTPUT_JSON = os.path.join(THIS_DIR, "1419_run_output.json")

# Goal-time constants (seconds) per Trolley Goal time calculation.md
SETUP_TIME_S = 45.0
DOWNTIME_S = 0.1
PICK_TIME_PER_LINE_S = 10.7
PACK_TIME_PER_LINE_S = 0.1
PICK_TIME_PER_ITEM_S = 13.0
PACK_TIME_PER_ITEM_S = 0.1
LINES_OVER_60_BONUS_S = 0.1

# Walking / U-turn. WALK_SPEED_SPM and UTURN_PENALTY_S are time constants and
# remain module-level; UTURN_PENALTY_M (metre-equivalent weighting in trip_cost)
# now lives on StoreConfig.uturn_penalty_m so it can be calibrated per-store.
WALK_SPEED_SPM = 0.72   # seconds per metre
UTURN_PENALTY_S = 2.88  # seconds per U-turn

# All previously module-level geometric constants (BAY_GAP_M, AISLE_GAP_M,
# AISLE_LENGTH_M, MID_BAY_THRESHOLD, NON_STD_AISLE_THRESHOLD, MAX_STORE_DISTANCE_M,
# DEAD_WALK_NORM_M) were 1419-draft assumptions baked in before the real
# distance matrix existed. They now live on StoreConfig and are calibrated from
# the matrix + loaded items via StoreConfig.calibrate(matrix, items). The
# geometric_distance fallback was deleted — pre-filter quarantines any items
# missing from the matrix, so the matrix is the only distance source.

# ----------------------------------------------------------------------------
# 1. Exceptions and dataclasses
# ----------------------------------------------------------------------------


class AlgorithmException(Exception):
    """Structured failure carrying error_code, severity and reason per T-HR-7."""

    def __init__(self, error_code: str, severity: str, reason: str, context: Optional[dict] = None):
        super().__init__(f"[{error_code}|{severity}] {reason}")
        self.error_code = error_code
        self.severity = severity  # 'ERROR' | 'WARNING'
        self.reason = reason
        self.context = context or {}

    def to_dict(self) -> dict:
        return {
            "error_code": self.error_code,
            "severity": self.severity,
            "reason": self.reason,
            "context": self.context,
        }


@dataclass
class Item:
    """Single picked line. One Item == one (order, sku) line at a fixed location."""

    order_no: str
    line_no: str
    stock_code: str
    quantity: int
    unit_weight_g: float
    unit_volume_cm3: float
    aisle_location: str
    bay_location: str
    location_key: str        # Ailse_Bay_Concat, matches matrix labels
    zone: str                # Ambient | Chilled | Freezer | Security
    transit_id: str          # alpha-sequential, e.g. BB, BC...
    truck: str
    trolley_id_baseline: str
    delivery_start: str
    is_max_out: bool         # MaxOutTote=True -> alone in tote
    splittable: bool         # Splittable=True -> may be split across totes if needed
    is_special: bool         # IsSpecialPick
    picking_type: str = "Normal"  # Normal | Ugly | Bulk | Label (Label = volumetric overflow added on the fly)
    tray_header_id: str = ""      # Production TrayHeaderID (used for baseline parity)


@dataclass
class ToteResult:
    tote_id: str
    zone: str
    order_nos: List[str]
    items: List[Item]
    total_weight_g: float
    total_volume_cm3: float
    aisles: List[str]
    location_keys: List[str]
    transit_ids: List[str]
    is_ugly: bool = False    # exceeds capacity but cannot be split
    notes: List[str] = field(default_factory=list)
    # Production emits 1 TrayHeaderID per (order, trolley). For frozen pooled totes
    # (multiple orders in one physical tote) production therefore reports MULTIPLE trays.
    # tray_label_count mirrors that convention so reporting matches: physical totes still
    # = len(items), HC11 still uses physical, but the "tote count" metric uses tray_label_count.
    tray_label_count: int = 1


@dataclass
class TrolleyResult:
    trolley_id: str
    zone: str
    totes: List[ToteResult]
    transit_ids_covered: List[str]
    walk_distance_m: float
    uturn_count: int                # TR-HR-8 mandatory
    walk_time_s: float
    goal_time_s: float
    unique_skus: int
    total_lines: int
    pat_window_start: str           # earliest TransitID in trolley
    pat_window_end: str             # latest TransitID in trolley
    pat_wave_index: int = 0         # 1-based wave number under rolling PAT
    pat_wave_window: List[str] = field(default_factory=list)  # the N TransitIDs allowed in this wave
    notes: List[str] = field(default_factory=list)
    cold_chain_time_s: float = 0.0  # NEW (2026-05-11): HC12 compliance timer; see cold_chain_compliance_time_s


@dataclass
class StoreConfig:
    """Per-store configuration. 1419 defaults shown here; HC2/T-HR-3 require per-store override."""

    store_no: str = "1419"

    # Capacity limits per zone (HC8 / T-HR-3). Store 1419 uses 48L volume; 12.5kg weight strict.
    capacity_max_volume_cm3: Dict[str, float] = field(
        default_factory=lambda: {
            "Ambient": 48000.0,
            "Chilled": 48000.0,
            "Freezer": 48000.0,
            "Security": 48000.0,
        }
    )
    capacity_max_weight_g: Dict[str, float] = field(
        default_factory=lambda: {
            "Ambient": 12500.0,
            "Chilled": 12500.0,
            "Freezer": 12500.0,
            "Security": 12500.0,
        }
    )
    trolley_max_totes: int = 6  # HC11
    trolley_max_totes_soft: int = 6  # set higher (e.g. 7) to allow tail-fill overflow when HC11 strict
    target_totes_per_trolley: int = 6  # fill target used by PAT look-ahead window selector

    # Frozen multi-order ceiling (T-HR-1 / HC3)
    frozen_max_orders_per_tote: int = 2

    # Security multi-order ceiling. Security is picked from the back-room
    # (no in-store walking), so there is no PAT-walk penalty for combining
    # multiple same-TID orders into a single physical tote. Cap is generous;
    # in practice capacity (12.5kg / 48L) and same-TID grouping bind first.
    security_max_orders_per_tote: int = 6

    # Cold-chain pick-time cap (TR-HR-3 / HC12)
    cold_chain_cap_min: float = 30.0
    cold_chain_zones: Tuple[str, ...] = ("Chilled", "Freezer")
    # Cold-chain cap check mode:
    #   "compliance_time" - our model: pick-walk + uturns + per-line + per-item + over-60
    #                       (cold time = "first cold item touched -> staged"). STRICTER.
    #   "prod_goal_time"  - PROD's CalculateTrolleyGoalTimeSeconds: setup + downtime +
    #                       sum(per-item) + (PickPerLine+PackPerLine)*distinct_skus.
    #                       NO walk component. MORE PERMISSIVE.
    cc_check_mode: str = "compliance_time"

    # MaxOutTote (is_max_out) flag enforcement per zone. When False, items flagged
    # MaxOutTote=True in source are treated as regular items (no "alone in tote"
    # constraint, can merge into multi-line totes). Chilled defaults to OFF as a
    # workaround to match PROD behaviour — see CLAUDE.md "Key callouts". TODO:
    # investigate true PROD behaviour of MaxOutTote and re-enable when understood.
    respect_max_out: Dict[str, bool] = field(
        default_factory=lambda: {
            "Ambient": True,
            "Chilled": False,
            "Freezer": True,
            "Security": True,
        }
    )

    # Pick Across Trucks staging gate
    pick_across_trucks: int = 3

    # Distance / start-end anchors
    start_anchor: str = "picking_start_location_multi-pick"
    end_anchor: str = "staging_location_1"
    end_anchor_alt: str = "staging_location_2"  # optimiser picks cheaper of the two

    # Build behaviour
    sa_seeds: int = 5                 # multi-seed best-of-N (lever A)
    sa_iterations: int = 600
    enable_sa: bool = True
    enable_zone_security: bool = True
    geometric_fallback_enabled: bool = True

    # Phase B: zones that use multi-component affinity scoring + SPT-scored SA refinement
    # for trolley building. Frozen is excluded - its pooled multi-order totes complicate
    # cross-trolley swaps and the win is smaller relative to risk.
    affinity_zones: Tuple[str, ...] = ("Ambient", "Chilled")

    # Tested-and-refuted levers preserved as flags. Default OFF (levAB baseline).
    # Lever D: F7 multi-target item-quantity split. Saves totes but regresses walk
    #   per trolley (32,613 -> 32,906m). Refuted 2026-05-10.
    # Lever H: trip_cost-marginal scoring in _agglomerative_merge. Tightens tote
    #   shapes but the resulting compositions degrade trolley walk (32,613 -> 33,192m
    #   combined with D). Refuted 2026-05-10.
    enable_qty_split_f7: bool = False
    # H (2026-05-12 RETRY): trip_cost-marginal scoring in _agglomerative_merge.
    # Original H refuted 2026-05-10 with the OLD std/non-std-aware trip_cost engine
    # (combined with D regressed walk +579m vs levAB on Ambient). Retrying now that
    # the engine is pure CVRP — the merge objective and the trolley walk objective
    # are the same engine, so the proxy/objective mismatch that may have driven
    # the original regression is gone.
    enable_trip_cost_merge: bool = True

    # F7 lower-bound bin-pack (2026-05-11). When True for a zone, F7 first attempts a
    # multi-strategy FFD/BFD pack into the per-order volumetric lower bound. If the
    # pack succeeds and gives FEWER totes than the current greedy donor-dissolution
    # produces, that packing is used and F8 then recovers distance. Falls back to
    # the existing greedy F7 if no strategy hits the LB. Diagnostic on Chilled
    # (store 1419, `diag_tote_delta_fast.py`) showed 9 of 16 over-PROD orders
    # have FFD-feasible packings into PROD's tote count that greedy F7 misses.
    f7_min_count_pack: Dict[str, bool] = field(
        default_factory=lambda: {
            "Ambient": False,
            "Chilled": True,
            "Freezer": True,
            "Security": True,
        }
    )

    # Trolley-side T1 levers (greedy paradigm, post-hoc local search).
    # G1 (smart pairwise swap): enumerate PAT-feasible (ai, bj) swaps, pre-score
    #   by aisle overlap, evaluate trip_cost on top-K, take best improving.
    # G2 (3-way rotation): cycle (a -> b -> c -> a) across 3 trolleys.
    # G3 (worst-tote swap/relocate): targeted move on the highest-cost tote.
    enable_sa_g1_smart_swap: bool = True
    enable_sa_g2_three_way: bool = True
    enable_sa_g3_worst_tote: bool = True
    sa_g1_top_k: int = 4         # top-K feasible swaps to evaluate per (i, j) pair
    sa_g2_top_k: int = 6         # top-K feasible 3-way rotations to evaluate per (i, j, k) triple
    sa_g3_top_k_targets: int = 3 # top-K target trolleys for worst-tote swap by aisle overlap
    # G4 (refuted 2026-05-10): construction-order best-of-N via top-K perturbation in
    # Phase 2 pick. Every perturbed run regressed +400 to +1000 cost. The picker's local
    # optimum is too strong; sampling from top-3 candidates just degrades early picks.
    # Code preserved behind flag for future revisit with a different perturbation mechanism.
    enable_g4_construction_best_of_n: bool = False
    g4_construction_seeds: int = 5
    g4_perturbation_rank: int = 3
    sa_g3_swap_per_target: int = 2  # top-K swap candidates per target trolley
    # G5 (refuted 2026-05-10): trolley-count slack via split moves. Sort totes by
    # aisle centroid; try every cut k in 1..n-1; accept if cost(left)+cost(right) <
    # cost(original). Result: 0/147 split acceptances across 5 seeds. Reason:
    # splitting a 6-tote trolley doubles the dead-walk (return-to-start traverse for
    # each side), and with steepest-descent acceptance no split clears the breakeven.
    # The merge counterpart has ~zero candidates anyway (130/133 trolleys at max size 6).
    # Code preserved behind flag — re-enable only if a future tote/trolley shape
    # produces meaningfully bimodal trolleys (max_aisle_gap p90 > 8 or so).
    enable_sa_g5_split: bool = False
    # G6 (2026-05-12): random-tote relocate inside SA. Diagnostic-driven by the
    # v4.2 reassign pass which found 25 improving single-tote moves on pass 1 from
    # v2's "final" SA state — proving SA's G1/G2/G3 portfolio leaves a residual gap
    # of single-tote relocations on NON-WORST totes (G3 only targets the single
    # highest-cost-contributing tote per trolley). G6 picks a RANDOM tote in
    # trolley i, ranks top-K targets j by aisle Jaccard with that tote, and
    # accepts the best feasible relocate. Internalises the v4 reassign signal
    # into SA so we get the steepest-descent acceptance + multi-seed best-of-5
    # treatment automatically.
    enable_sa_g6_random_relocate: bool = True
    sa_g6_top_k_targets: int = 8  # top-K target trolleys by aisle Jaccard
    # G8 (2026-05-12): 2-tote Lin-Kernighan chain (A: X->Y, B: Y->Z, Z != X).
    # Structural gap not covered by existing moves: 1-relocate needs Y to have slack;
    # block-swap forces B back to X; 3-way rotation closes the cycle (returns to X);
    # G8 is the open displacement chain. Combinatorial bound: K_Y * K_Z chains per
    # tote A, with B picked as the lowest aisle-Jaccard tote in Y (most likely to
    # benefit from a different home).
    enable_sa_g8_lk_chain: bool = True
    sa_g8_top_k_y: int = 4  # top-K Y trolleys (destination for A)
    sa_g8_top_k_z: int = 4  # top-K Z trolleys (destination for displaced B)
    # G9 (2026-05-12): CP-SAT LNS — pick K random trolleys, dissolve their totes,
    # set-partition them optimally via CP-SAT. Co-evaluates multiple moves that
    # G1/G2/G3/G6/G8 can only accept one at a time. Sense check on whether v4.2 +
    # heuristic SA is leaving global structure on the table that a small ILP can
    # catch.
    enable_sa_g9_cpsat_lns: bool = True
    sa_g9_lns_k: int = 3  # number of trolleys to dissolve per LNS move
    sa_g9_top_k_swaps: int = 5  # outsider swap candidates per position
    sa_g9_time_limit_s: float = 1.5  # CP-SAT time limit per LNS solve

    # Matrix unit scaling. 1419 matrix is in centimetres -> 0.01 to convert to metres.
    matrix_unit_to_m: float = 0.01

    # ----- Data-driven geometry (replaces the v1 draft constants) -----
    # All set by StoreConfig.calibrate(matrix, items). Default sentinels of 0.0
    # / 0 mean "not yet calibrated"; the picker/affinity/SA code asserts these
    # are populated before use.
    uturn_penalty_m: float = 4.0  # time-equivalent weight of a U-turn in trip_cost
    # Calibrated from the actual matrix (max pairwise distance).
    max_store_distance_m: float = 0.0   # affinity denominator
    _calibrated: bool = False           # set True by calibrate(); guards build entry points

    def calibrate(self, matrix: "DistanceMatrix", items: Sequence["Item"]) -> None:
        """Populate data-driven geometry from the matrix + loaded items.

        Must be called after DistanceMatrix.load_from_csv and load_orders, and
        before any build_totes / build_trolleys / analyse_baseline call. The
        build entry points assert _calibrated is True so missed calls surface
        loudly rather than silently using zero-denominators.
        """
        if not items:
            raise AlgorithmException(
                "CFG_CALIBRATE_EMPTY", "ERROR",
                "calibrate(): items list is empty — nothing to calibrate from")
        # Matrix-derived max pairwise distance. Linear scan over labels — O(n^2)
        # over ~few-thousand labels is sub-second on 1419's matrix.
        max_d = 0.0
        n = len(matrix.labels)
        u = matrix.unit_to_m
        for i in range(n):
            row = matrix.matrix[i]
            for j in range(i + 1, n):
                v = row[j]
                if v != float("inf") and v * u > max_d:
                    max_d = v * u
        if max_d <= 0.0:
            raise AlgorithmException(
                "CFG_CALIBRATE_NO_DIST", "ERROR",
                "calibrate(): matrix yielded no positive distance — corrupt matrix?")
        self.max_store_distance_m = max_d
        self._calibrated = True

    def _assert_calibrated(self) -> None:
        if not self._calibrated:
            raise AlgorithmException(
                "CFG_NOT_CALIBRATED", "ERROR",
                "StoreConfig.calibrate(matrix, items) must be called before build entry points")


# ----------------------------------------------------------------------------
# 2. Distance matrix
# ----------------------------------------------------------------------------


@dataclass
class DistanceMatrix:
    """Symmetric pairwise walkable distance lookup, indexed by location label.

    All `lookup` results are returned in METRES; the raw matrix is multiplied by `unit_to_m`.
    """

    labels: List[str]
    label_to_idx: Dict[str, int]
    matrix: List[List[float]]
    unit_to_m: float = 0.01

    @classmethod
    def load_from_csv(cls, path: str, unit_to_m: float = 0.01) -> "DistanceMatrix":
        if not os.path.exists(path):
            raise AlgorithmException(
                error_code="DM_NOT_FOUND",
                severity="ERROR",
                reason=f"Distance matrix CSV not found: {path}",
                context={"path": path},
            )
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader)
            labels = [h.strip() for h in header[1:]]
            n = len(labels)
            mat: List[List[float]] = []
            row_labels: List[str] = []
            for row in reader:
                if not row:
                    continue
                row_labels.append(row[0].strip())
                vals = [float(v) if v else float("inf") for v in row[1:]]
                if len(vals) != n:
                    raise AlgorithmException(
                        error_code="DM_SHAPE",
                        severity="ERROR",
                        reason="Distance matrix row width mismatch",
                        context={"row": row[0], "expected": n, "got": len(vals)},
                    )
                mat.append(vals)
            if row_labels != labels:
                missing_in_rows = set(labels) - set(row_labels)
                missing_in_cols = set(row_labels) - set(labels)
                raise AlgorithmException(
                    error_code="DM_NOT_SQUARE",
                    severity="ERROR",
                    reason="Distance matrix row labels do not match column labels",
                    context={
                        "rows_only": sorted(missing_in_cols)[:10],
                        "cols_only": sorted(missing_in_rows)[:10],
                    },
                )
        return cls(labels=labels, label_to_idx={lab: i for i, lab in enumerate(labels)},
                   matrix=mat, unit_to_m=unit_to_m)

    def has(self, label: str) -> bool:
        return label in self.label_to_idx

    def lookup(self, a: str, b: str) -> Tuple[float, str]:
        """Return (distance_m, source). source in {'matrix','self'}.

        Pure matrix lookup — geometric fallback was deleted (2026-05-11, Option B).
        The pre-filter quarantines any items missing from the matrix, so every
        lookup is guaranteed to hit a matrix entry or be a self-pair.
        """
        if a == b:
            return 0.0, "self"
        if a in self.label_to_idx and b in self.label_to_idx:
            return self.matrix[self.label_to_idx[a]][self.label_to_idx[b]] * self.unit_to_m, "matrix"
        raise AlgorithmException(
            error_code="DM_LOOKUP_MISS",
            severity="ERROR",
            reason=f"No matrix entry for ({a}, {b}) — pre-filter should have quarantined",
            context={"a": a, "b": b},
        )


# ----------------------------------------------------------------------------
# 3. Order ingest
# ----------------------------------------------------------------------------


def _bool(s: str) -> bool:
    return str(s).strip().upper() in ("TRUE", "1", "Y", "YES")


def load_orders(path: str) -> List[Item]:
    if not os.path.exists(path):
        raise AlgorithmException("ORDERS_NOT_FOUND", "ERROR", f"Orders CSV not found: {path}")
    items: List[Item] = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                qty = int(float(row.get("quantity") or row.get("Ordered_Qty") or 0))
            except ValueError:
                qty = 0
            if qty <= 0:
                continue
            try:
                weight = float(row.get("UnitWeightGrams") or 0)
                volume = float(row.get("UnitVolumeCubicCm") or 0)
            except ValueError:
                weight, volume = 0.0, 0.0
            zone = (row.get("PickingZone") or "").strip() or "Ambient"
            location_key = (row.get("Ailse_Bay_Concat") or "").strip()
            aisle_loc = (row.get("Aisle_Location") or "").strip()
            bay_loc = (row.get("Bay_Location") or "").strip()
            transit_id = (row.get("TransitID") or "").strip()
            items.append(Item(
                order_no=(row.get("Order_NBR") or "").strip(),
                line_no=(row.get("LineNumber") or "").strip(),
                stock_code=(row.get("StockCode") or "").strip(),
                quantity=qty,
                unit_weight_g=weight,
                unit_volume_cm3=volume,
                aisle_location=aisle_loc,
                bay_location=bay_loc,
                location_key=location_key,
                zone=zone,
                transit_id=transit_id,
                truck=(row.get("Truck") or "").strip(),
                trolley_id_baseline=(row.get("TrolleyID") or "").strip(),
                delivery_start=(row.get("DeliveryStartDateTime_local") or "").strip(),
                is_max_out=_bool(row.get("MaxOutTote") or "FALSE"),
                splittable=_bool(row.get("Splittable") or "FALSE"),
                is_special=_bool(row.get("IsSpecialPick") or "FALSE"),
                picking_type=(row.get("PickingType") or "Normal").strip() or "Normal",
                tray_header_id=(row.get("TrayHeaderID") or "").strip(),
            ))
    return items


def aisle_int(s: str) -> int:
    try:
        return int(str(s).strip())
    except ValueError:
        return 0


def bay_int(s: str) -> int:
    try:
        return int(str(s).strip())
    except ValueError:
        return 0


def location_pair(it: Item) -> Tuple[int, int]:
    return aisle_int(it.aisle_location), bay_int(it.bay_location)


# ----------------------------------------------------------------------------
# 4. Goal time calculation
# ----------------------------------------------------------------------------


def calculate_goal_time(trolley: TrolleyResult, items_by_order: Dict[str, List[Item]]) -> float:
    """Per Trolley Goal time calculation.md.

    Goal = Setup + Downtime + sum(TrayGoalTime) + (PickPerLine + PackPerLine) * unique_skus
    TrayGoalTime = sum(qty * (PickPerItem + PackPerItem)) for items in tray (=tote)
    Per-order >60 lines bonus added once per qualifying order.
    """
    tray_goal = 0.0
    skus: Set[str] = set()
    total_lines = 0
    for tote in trolley.totes:
        for it in tote.items:
            tray_goal += it.quantity * (PICK_TIME_PER_ITEM_S + PACK_TIME_PER_ITEM_S)
            skus.add(it.stock_code)
            total_lines += 1
    unique_skus = len(skus)
    base = SETUP_TIME_S + DOWNTIME_S + tray_goal
    line_cost = (PICK_TIME_PER_LINE_S + PACK_TIME_PER_LINE_S) * unique_skus
    over_60_bonus = 0.0
    orders_in_trolley = {it.order_no for tote in trolley.totes for it in tote.items}
    for order_no in orders_in_trolley:
        order_items = items_by_order.get(order_no, [])
        if len({it.line_no for it in order_items}) > 60:
            over_60_bonus += LINES_OVER_60_BONUS_S
    return base + line_cost + over_60_bonus


# ----------------------------------------------------------------------------
# 5. Trip cost: pure CVRP/TSP over the distance matrix.
#
# The matrix is the sole source of physical walking distance — no
# aisle-layout heuristic, no std vs non-std classification. The 4-variant
# serpentine portfolio it replaced was a 1419-draft assumption (assumed a
# linear A1..A12 layout with non-std items reachable via a fixed detour).
#
# Engine: Nearest-Neighbour init -> 2-opt -> Or-opt local search, evaluated
# against both end anchors. Returns the lowest-cost matrix path.
#
# U-turn count is a sequence-dependent ergonomic cost layered on top of
# matrix walk. A U-turn is detected when the picker reverses direction
# inside an aisle — i.e. consecutive bay numbers within the same aisle form
# a non-monotonic sequence. Cost objective: walk_m + cfg.uturn_penalty_m * U.
# ----------------------------------------------------------------------------


def _count_uturns(path: List[Tuple[str, Tuple[int, int]]]) -> int:
    """Count direction reversals within consecutive same-aisle bay sequences.

    Sequence-only — no MID_BAY_THRESHOLD, no entry/exit-end inference. The
    matrix prices physical walk; this function prices the ergonomic cost of
    physically turning the trolley around inside an aisle.

    Known under-count: monotonic bay sequence with same-side entry/exit
    (enter aisle, walk to deepest pick, exit same end) implies a turn-around
    that this counter misses. Bounded by 1 per aisle visit; accepted under
    Option B (pure CVRP) shipping debt.
    """
    n = len(path)
    if n < 2:
        return 0
    uturns = 0
    i = 0
    while i < n:
        a = path[i][1][0]  # aisle int
        j = i
        while j + 1 < n and path[j + 1][1][0] == a:
            j += 1
        if j > i:
            bays = [path[k][1][1] for k in range(i, j + 1)]
            direction = 0  # 0 = unset, 1 = ascending, -1 = descending
            for k in range(1, len(bays)):
                if bays[k] == bays[k - 1]:
                    continue
                cur = 1 if bays[k] > bays[k - 1] else -1
                if direction != 0 and cur != direction:
                    uturns += 1
                direction = cur
        i = j + 1
    return uturns


def _path_length(matrix: DistanceMatrix, start: str,
                 locs: List[Tuple[str, Tuple[int, int]]], end: str) -> float:
    """Sum matrix distance along start -> locs -> end."""
    if not locs:
        d_se, _ = matrix.lookup(start, end)
        return d_se
    total = 0.0
    d, _ = matrix.lookup(start, locs[0][0])
    total += d
    for i in range(len(locs) - 1):
        d, _ = matrix.lookup(locs[i][0], locs[i + 1][0])
        total += d
    d, _ = matrix.lookup(locs[-1][0], end)
    total += d
    return total


def _nn_path(items: List[Item], matrix: DistanceMatrix, start: str
             ) -> List[Tuple[str, Tuple[int, int]]]:
    """Nearest-neighbour init starting from `start` anchor."""
    remaining = list(items)
    path: List[Tuple[str, Tuple[int, int]]] = []
    cur_label = start
    while remaining:
        best_idx = 0
        best_d = float("inf")
        for i, it in enumerate(remaining):
            d, _ = matrix.lookup(cur_label, it.location_key)
            if d < best_d:
                best_d = d
                best_idx = i
        chosen = remaining.pop(best_idx)
        path.append((chosen.location_key, location_pair(chosen)))
        cur_label = chosen.location_key
    return path


def _build_local_dist_table(path: List[Tuple[str, Tuple[int, int]]],
                             matrix: DistanceMatrix, start: str, end: str
                             ) -> List[List[float]]:
    """Precompute a dense (n+2) x (n+2) distance table local to a path.

    Index layout:  0 = start anchor, 1..n = path stops in order, n+1 = end.

    Lifts every matrix.lookup() call out of the 2-opt / or-opt hot loops so
    those loops do plain array indexing (~30-100x faster than re-resolving
    label_to_idx + the unit_to_m multiply per edge).
    """
    n = len(path)
    locs = [start] + [p[0] for p in path] + [end]
    m = n + 2
    idx_map = matrix.label_to_idx
    unit = matrix.unit_to_m
    mat = matrix.matrix
    # Resolve every label to its global matrix index once.
    try:
        global_idx = [idx_map[lbl] for lbl in locs]
    except KeyError as exc:
        raise AlgorithmException(
            "DM_LOOKUP_MISS",
            f"label {exc.args[0]!r} not in distance matrix while building local table",
        ) from exc
    d = [[0.0] * m for _ in range(m)]
    for i in range(m):
        row = mat[global_idx[i]]
        di = d[i]
        for j in range(m):
            if i == j:
                continue
            di[j] = row[global_idx[j]] * unit
    return d


def _count_uturns_from_indices(tour: List[int], aisle_of: List[int],
                                bay_of: List[int]) -> int:
    """U-turn count over a tour given local aisle/bay arrays indexed by tour entry.

    aisle_of[k] / bay_of[k] are the aisle/bay of the stop at local index k
    (None for start/end anchors). Mirrors _count_uturns but operates on the
    tour index sequence so 2-opt / or-opt can evaluate candidate tours
    without materialising a new path list.
    """
    n = len(tour)
    if n < 2:
        return 0
    uturns = 0
    i = 0
    while i < n:
        a = aisle_of[tour[i]]
        if a is None:
            i += 1
            continue
        j = i
        while j + 1 < n and aisle_of[tour[j + 1]] == a:
            j += 1
        if j > i:
            direction = 0
            prev_bay = bay_of[tour[i]]
            for k in range(i + 1, j + 1):
                cur_bay = bay_of[tour[k]]
                if cur_bay == prev_bay:
                    continue
                cur = 1 if cur_bay > prev_bay else -1
                if direction != 0 and cur != direction:
                    uturns += 1
                direction = cur
                prev_bay = cur_bay
        i = j + 1
    return uturns


def _two_opt(path: List[Tuple[str, Tuple[int, int]]], matrix: DistanceMatrix,
             start: str, end: str, max_passes: int = 4,
             uturn_penalty_m: float = 0.0
             ) -> List[Tuple[str, Tuple[int, int]]]:
    """Cost-aware 2-opt over a precomputed local distance table.

    Objective: walk + uturn_penalty_m * uturns.
    Distance delta is O(1) from the precomputed table. U-turn delta requires
    re-evaluating the tour (sequence-dependent), so we accept on the FULL cost.
    The dist-table speedup eliminates per-edge matrix.lookup overhead — typically
    a 10-30x speedup vs the prior naive _path_length-based form.
    """
    n = len(path)
    if n < 4:
        return path
    d = _build_local_dist_table(path, matrix, start, end)
    m = n + 2
    # Local aisle/bay arrays, indexed by local tour index (0 = start, n+1 = end).
    aisle_of: List[int] = [None] * m  # type: ignore[list-item]
    bay_of: List[int] = [None] * m    # type: ignore[list-item]
    for k in range(n):
        aisle_of[k + 1] = path[k][1][0]
        bay_of[k + 1] = path[k][1][1]
    tour = list(range(m))
    # Baseline total walk for the identity tour.
    cur_walk = 0.0
    for k in range(m - 1):
        cur_walk += d[tour[k]][tour[k + 1]]
    cur_uturns = _count_uturns_from_indices(tour, aisle_of, bay_of)
    cur_cost = cur_walk + uturn_penalty_m * cur_uturns
    improved = True
    passes = 0
    while improved and passes < max_passes:
        improved = False
        passes += 1
        # OLD path-index loop `for i in range(len(best) - 1)` maps to tour indices
        # [1, n-1] (path index k -> tour index k+1). Matching that range keeps the
        # search neighbourhood byte-identical to the legacy implementation so the
        # downstream picker/SA/reassign decisions stay calibrated.
        for i in range(1, n):
            a = tour[i]
            b = tour[i + 1]
            dab = d[a][b]
            for j in range(i + 2, n + 1):
                c = tour[j]
                e = tour[j + 1]
                delta_walk = (d[a][c] + d[b][e]) - (dab + d[c][e])
                # Distance-only quick reject when penalty is zero.
                if uturn_penalty_m == 0.0:
                    if delta_walk + 1e-9 < 0.0:
                        tour[i + 1:j + 1] = tour[i + 1:j + 1][::-1]
                        cur_walk += delta_walk
                        b = tour[i + 1]
                        dab = d[a][b]
                        improved = True
                    continue
                # Evaluate candidate u-turn count by reversing the segment in place.
                tour[i + 1:j + 1] = tour[i + 1:j + 1][::-1]
                new_uturns = _count_uturns_from_indices(tour, aisle_of, bay_of)
                new_walk = cur_walk + delta_walk
                new_cost = new_walk + uturn_penalty_m * new_uturns
                if new_cost + 1e-9 < cur_cost:
                    cur_walk = new_walk
                    cur_uturns = new_uturns
                    cur_cost = new_cost
                    b = tour[i + 1]
                    dab = d[a][b]
                    improved = True
                else:
                    # Revert.
                    tour[i + 1:j + 1] = tour[i + 1:j + 1][::-1]
    return [path[k - 1] for k in tour[1:-1]]


def _or_opt(path: List[Tuple[str, Tuple[int, int]]], matrix: DistanceMatrix,
            start: str, end: str, max_passes: int = 3,
            uturn_penalty_m: float = 0.0
            ) -> List[Tuple[str, Tuple[int, int]]]:
    """Cost-aware or-opt over a precomputed local distance table.

    Relocates a contiguous 1/2/3-stop chain. Distance delta is O(1) per
    candidate position; u-turn delta requires re-evaluating after the move
    (sequence-dependent). Accepts on full cost = walk + uturn_penalty_m * uturns.
    """
    n = len(path)
    if n < 4:
        return path
    d = _build_local_dist_table(path, matrix, start, end)
    m = n + 2
    aisle_of: List[int] = [None] * m  # type: ignore[list-item]
    bay_of: List[int] = [None] * m    # type: ignore[list-item]
    for k in range(n):
        aisle_of[k + 1] = path[k][1][0]
        bay_of[k + 1] = path[k][1][1]
    tour = list(range(m))
    cur_walk = 0.0
    for k in range(m - 1):
        cur_walk += d[tour[k]][tour[k + 1]]
    cur_uturns = _count_uturns_from_indices(tour, aisle_of, bay_of)
    cur_cost = cur_walk + uturn_penalty_m * cur_uturns
    improved = True
    passes = 0
    while improved and passes < max_passes:
        improved = False
        passes += 1
        for seg_len in (1, 2, 3):
            if seg_len >= n:
                break
            i = 1
            while i + seg_len <= n + 1:
                a = tour[i - 1]
                seg_first = tour[i]
                seg_last = tour[i + seg_len - 1]
                b = tour[i + seg_len]
                gap_old = d[a][seg_first] + d[seg_last][b]
                gap_close = d[a][b]
                base_delta_walk = gap_close - gap_old

                best_delta_cost = 0.0
                best_insert = -1
                best_new_walk = cur_walk
                best_new_uturns = cur_uturns
                segment = tour[i:i + seg_len]
                trimmed = tour[:i] + tour[i + seg_len:]
                trimmed_len = m - seg_len
                for k in range(trimmed_len - 1):
                    li = k if k < i else k + seg_len
                    ri = (k + 1) if (k + 1) < i else (k + 1) + seg_len
                    L = tour[li]
                    R = tour[ri]
                    if li == i - 1 and ri == i + seg_len:
                        continue
                    ins_delta = (d[L][seg_first] + d[seg_last][R]) - d[L][R]
                    cand_delta_walk = base_delta_walk + ins_delta
                    if uturn_penalty_m == 0.0:
                        if cand_delta_walk + 1e-9 < best_delta_cost:
                            best_delta_cost = cand_delta_walk
                            best_new_walk = cur_walk + cand_delta_walk
                            best_insert = k + 1
                        continue
                    # Build candidate tour and recount u-turns.
                    cand_tour = trimmed[:k + 1] + segment + trimmed[k + 1:]
                    cand_uturns = _count_uturns_from_indices(cand_tour, aisle_of, bay_of)
                    cand_new_walk = cur_walk + cand_delta_walk
                    cand_delta_cost = cand_delta_walk + uturn_penalty_m * (cand_uturns - cur_uturns)
                    if cand_delta_cost + 1e-9 < best_delta_cost:
                        best_delta_cost = cand_delta_cost
                        best_new_walk = cand_new_walk
                        best_new_uturns = cand_uturns
                        best_insert = k + 1

                if best_insert >= 0:
                    tour = trimmed[:best_insert] + segment + trimmed[best_insert:]
                    cur_walk = best_new_walk
                    cur_uturns = best_new_uturns
                    cur_cost = cur_walk + uturn_penalty_m * cur_uturns
                    improved = True
                    continue
                i += 1
    return [path[k - 1] for k in tour[1:-1]]


# Memo for _select_best_path. Hot path on Chilled was double-calling SPT per SA
# move (once via trip_cost, once via cold_chain_compliance_time_s) and re-deriving
# the same SPT result across reassign passes and SA seeds. Keyed on the item-set
# identity (Item objects are not mutated in place during a build) plus cfg identity.
# Cleared at build entry points via clear_best_path_cache().
_BEST_PATH_CACHE: Dict[Tuple[int, frozenset], Tuple[
    List[Tuple[str, Tuple[int, int]]], str, float, float, int, str
]] = {}


def clear_best_path_cache() -> None:
    _BEST_PATH_CACHE.clear()


def _select_best_path(items: List[Item], matrix: DistanceMatrix, cfg: StoreConfig
                       ) -> Tuple[List[Tuple[str, Tuple[int, int]]], str, float, float, int, str]:
    """Pure TSP over the matrix. Returns (best_path, best_end_anchor,
    walk_distance_m, approach_distance_m, uturns, method).

    approach_distance_m = start_anchor -> first pick edge. Subtracted from
    total when computing cold-chain compliance walk (picker hasn't touched a
    cold item before the first pick).
    """
    if not items:
        return [], cfg.end_anchor, 0.0, 0.0, 0, "empty"
    key = (id(cfg), frozenset(id(it) for it in items))
    cached = _BEST_PATH_CACHE.get(key)
    if cached is not None:
        return cached

    start = cfg.start_anchor
    nn = _nn_path(items, matrix, start)

    candidates = []
    penalty = cfg.uturn_penalty_m
    for end in (cfg.end_anchor, cfg.end_anchor_alt):
        # Local search optimises pure walk distance (matches the prior
        # _path_length-based path-recompute form). U-turn penalty is applied
        # only when picking among candidates below — this preserves the
        # accept/reject behaviour the baseline runs were calibrated against.
        improved = _two_opt(nn, matrix, start, end, uturn_penalty_m=0.0)
        improved = _or_opt(improved, matrix, start, end, uturn_penalty_m=0.0)
        d = _path_length(matrix, start, improved, end)
        u = _count_uturns(improved)
        cost = d + u * penalty
        candidates.append((cost, d, u, improved, end))

    candidates.sort(key=lambda x: x[0])
    _, best_d, best_u, best_path, best_end = candidates[0]
    if best_path:
        approach_d, _ = matrix.lookup(start, best_path[0][0])
    else:
        approach_d, _ = matrix.lookup(start, best_end)
    result = (best_path, best_end, best_d, approach_d, best_u, "nn_2opt_oropt")
    _BEST_PATH_CACHE[key] = result
    return result


def trip_cost(items: List[Item], matrix: DistanceMatrix, cfg: StoreConfig
              ) -> Tuple[float, int, str]:
    """Return (distance_m, uturn_count, method). Hot path — signature preserved."""
    if not items:
        return 0.0, 0, "empty"
    _, _, d, _, u, method = _select_best_path(items, matrix, cfg)
    return d, u, method


def _prod_trolley_goal_time_s(items: List[Item],
                               items_by_order: Dict[str, List[Item]],
                               cfg: StoreConfig) -> float:
    """Mirror of PROD's CalculateTrolleyGoalTimeSeconds (ProdGoaltimecalc.txt).

    Returns:
      Setup + Downtime + sum(qty*per-item) + (PickPerLine+PackPerLine)*distinct_skus
      + over-60-lines-per-order bonus (stand-in for LargeOrdersExtraTime)

    NO walk component. NO travelTimeToShopFloor (single-store dataset).
    This is intentionally MORE PERMISSIVE than cold_chain_compliance_time_s —
    use it to test whether PROD's actual cap policy permits the trolley shapes
    PROD ships.
    """
    if not items:
        return 0.0
    tray_goal = sum(it.quantity * (PICK_TIME_PER_ITEM_S + PACK_TIME_PER_ITEM_S) for it in items)
    skus = {it.stock_code for it in items}
    line_cost = (PICK_TIME_PER_LINE_S + PACK_TIME_PER_LINE_S) * len(skus)
    over_60 = 0.0
    for order_no in {it.order_no for it in items}:
        if len({oit.line_no for oit in items_by_order.get(order_no, [])}) > 60:
            over_60 += LINES_OVER_60_BONUS_S
    return SETUP_TIME_S + DOWNTIME_S + tray_goal + line_cost + over_60


def cold_chain_compliance_time_s(items: List[Item],
                                  items_by_order: Dict[str, List[Item]],
                                  matrix: DistanceMatrix,
                                  cfg: StoreConfig) -> float:
    """HC12 cold-chain compliance timer (NEW model 2026-05-11).

    Dispatches on cfg.cc_check_mode:
      "compliance_time" (default): "first cold item touched -> staged" — includes
        pick-walk, uturns, per-line, per-item, over-60. STRICTER.
      "prod_goal_time": PROD's CalculateTrolleyGoalTimeSeconds formula — no walk.
        MORE PERMISSIVE. Used to validate whether PROD's cap policy explains
        PROD's trolley count.

    Includes (compliance_time mode):
      * Walk along the pick path (first item -> last item)
      * Return walk from last item to staging
      * U-turn time penalty along the pick path
      * Per-line pick + pack time   (PICK_TIME_PER_LINE_S + PACK_TIME_PER_LINE_S) * unique_skus
      * Per-item pick + pack time   sum( qty * (PICK_TIME_PER_ITEM_S + PACK_TIME_PER_ITEM_S) )
      * Over-60-lines-per-order bonus

    Excludes (compliance_time mode, vs the planning goal_time):
      * Setup time (45s)            — happens at staging before picker leaves; pre-cold-chain
      * Downtime  (0.1s)            — pre-pick allowance
      * Approach walk (start -> 1st pick) — picker hasn't touched a cold item yet

    Diagnostic vs PROD on Chilled store 1419 (2026-05-11): under this model PROD's
    Chilled trolleys cluster well below the 30-min cap, where the goal-time-based
    check was firing on 14/97 PROD trolleys (false breaches). See CLAUDE.md.
    """
    if not items:
        return 0.0
    if getattr(cfg, "cc_check_mode", "compliance_time") == "prod_goal_time":
        return _prod_trolley_goal_time_s(items, items_by_order, cfg)
    _, _, total_d, approach_d, uturns, _ = _select_best_path(items, matrix, cfg)
    cc_walk_m = max(0.0, total_d - approach_d)
    cc_walk_s = cc_walk_m * WALK_SPEED_SPM + uturns * UTURN_PENALTY_S
    tray_goal = sum(it.quantity * (PICK_TIME_PER_ITEM_S + PACK_TIME_PER_ITEM_S) for it in items)
    skus = {it.stock_code for it in items}
    line_cost = (PICK_TIME_PER_LINE_S + PACK_TIME_PER_LINE_S) * len(skus)
    over_60 = 0.0
    for order_no in {it.order_no for it in items}:
        if len({oit.line_no for oit in items_by_order.get(order_no, [])}) > 60:
            over_60 += LINES_OVER_60_BONUS_S
    return cc_walk_s + tray_goal + line_cost + over_60


def trolley_path_cost_and_cc_time(items: List[Item],
                                   items_by_order: Optional[Dict[str, List[Item]]],
                                   matrix: DistanceMatrix,
                                   cfg: StoreConfig,
                                   need_cc: bool = True) -> Tuple[float, float]:
    """Single-shot helper: returns (path_cost, cc_time_s) from ONE SPT call.

    Both the trolley-cost objective (walk + cfg.uturn_penalty_m*uturns) and the
    HC12 cold-chain compliance time share `_select_best_path`'s output. Without
    this helper, SA refinement and the picker C1 guard would call SPT twice per
    candidate (once via trip_cost, once via cold_chain_compliance_time_s). The
    SPT memo makes the second call O(1) anyway, but this helper short-circuits
    the bookkeeping entirely.

    `need_cc=False` skips the cc_time math (returns 0.0) for hot paths where
    cold-chain is disabled.
    """
    if not items:
        return 0.0, 0.0
    # prod_goal_time mode doesn't need SPT at all
    if need_cc and getattr(cfg, "cc_check_mode", "compliance_time") == "prod_goal_time":
        _, _, total_d, _approach_d, uturns, _ = _select_best_path(items, matrix, cfg)
        path_cost = total_d + uturns * cfg.uturn_penalty_m
        cc_time = _prod_trolley_goal_time_s(items, items_by_order or {}, cfg)
        return path_cost, cc_time
    _, _, total_d, approach_d, uturns, _ = _select_best_path(items, matrix, cfg)
    path_cost = total_d + uturns * cfg.uturn_penalty_m
    if not need_cc:
        return path_cost, 0.0
    cc_walk_m = max(0.0, total_d - approach_d)
    cc_walk_s = cc_walk_m * WALK_SPEED_SPM + uturns * UTURN_PENALTY_S
    tray_goal = sum(it.quantity * (PICK_TIME_PER_ITEM_S + PACK_TIME_PER_ITEM_S) for it in items)
    skus = {it.stock_code for it in items}
    line_cost = (PICK_TIME_PER_LINE_S + PACK_TIME_PER_LINE_S) * len(skus)
    over_60 = 0.0
    if items_by_order is not None:
        for order_no in {it.order_no for it in items}:
            if len({oit.line_no for oit in items_by_order.get(order_no, [])}) > 60:
                over_60 += LINES_OVER_60_BONUS_S
    cc_time = cc_walk_s + tray_goal + line_cost + over_60
    return path_cost, cc_time


# ----------------------------------------------------------------------------
# 6. Tote builder
# ----------------------------------------------------------------------------


def _ugly_check(qty: int, unit_w: float, unit_v: float, cap_w: float, cap_v: float) -> bool:
    """An item is 'ugly' if EVEN ONE unit exceeds tote capacity (cannot be split usefully)."""
    return unit_w > cap_w or unit_v > cap_v


def _line_fits(qty: int, unit_w: float, unit_v: float, cap_w: float, cap_v: float) -> bool:
    return qty * unit_w <= cap_w and qty * unit_v <= cap_v


def _split_oversized_line(it: Item, cap_w: float, cap_v: float) -> List[Item]:
    """Split a line into multiple Items, each with quantity that fits one tote.

    Splittable=True is currently respected; non-splittable oversized lines are flagged ugly.
    """
    if _line_fits(it.quantity, it.unit_weight_g, it.unit_volume_cm3, cap_w, cap_v):
        return [it]
    if _ugly_check(it.quantity, it.unit_weight_g, it.unit_volume_cm3, cap_w, cap_v):
        return [it]  # caller marks as ugly
    if not it.splittable and it.quantity > 1:
        return [it]  # caller marks as ugly
    # max units per chunk that still fit
    max_w_units = int(cap_w // it.unit_weight_g) if it.unit_weight_g > 0 else it.quantity
    max_v_units = int(cap_v // it.unit_volume_cm3) if it.unit_volume_cm3 > 0 else it.quantity
    chunk = max(1, min(max_w_units, max_v_units))
    pieces = []
    remaining = it.quantity
    idx = 0
    while remaining > 0:
        take = min(chunk, remaining)
        pieces.append(Item(
            order_no=it.order_no,
            line_no=f"{it.line_no}.{idx}",
            stock_code=it.stock_code,
            quantity=take,
            unit_weight_g=it.unit_weight_g,
            unit_volume_cm3=it.unit_volume_cm3,
            aisle_location=it.aisle_location,
            bay_location=it.bay_location,
            location_key=it.location_key,
            zone=it.zone,
            transit_id=it.transit_id,
            truck=it.truck,
            trolley_id_baseline=it.trolley_id_baseline,
            delivery_start=it.delivery_start,
            is_max_out=it.is_max_out,
            splittable=it.splittable,
            is_special=it.is_special,
        ))
        remaining -= take
        idx += 1
    return pieces


@dataclass
class _Cluster:
    items: List[Item]
    weight_g: float = 0.0
    volume_cm3: float = 0.0
    order_set: Set[str] = field(default_factory=set)
    is_ugly: bool = False
    is_max_out: bool = False

    @property
    def aisles(self) -> Set[int]:
        return {aisle_int(it.aisle_location) for it in self.items}


def _cluster_representative(c: _Cluster) -> str:
    if not c.items:
        return ""
    sorted_items = sorted(c.items, key=lambda it: (aisle_int(it.aisle_location), bay_int(it.bay_location)))
    mid = sorted_items[len(sorted_items) // 2]
    return mid.location_key


def _cluster_distance(a: _Cluster, b: _Cluster, matrix: DistanceMatrix) -> float:
    """Walkable distance between two clusters' representative locations."""
    if not a.items or not b.items:
        return float("inf")
    ra = _cluster_representative(a)
    rb = _cluster_representative(b)
    d, _ = matrix.lookup(ra, rb)
    return d


def _can_merge(a: _Cluster, b: _Cluster, cap_w: float, cap_v: float,
               max_orders: int, alpha_idx: Optional[Dict[str, int]] = None,
               max_alpha_span: Optional[int] = None) -> bool:
    if a.is_max_out or b.is_max_out:
        return False
    if a.is_ugly or b.is_ugly:
        return False
    if a.weight_g + b.weight_g > cap_w:
        return False
    if a.volume_cm3 + b.volume_cm3 > cap_v:
        return False
    if len(a.order_set | b.order_set) > max_orders:
        return False
    # PAT-aware constraint: a tote's TransitIDs must fit within an N-position alpha span
    # so the tote can be picked inside any N-truck PAT window without being orphaned.
    if alpha_idx is not None and max_alpha_span is not None:
        union_tids = {it.transit_id for it in a.items + b.items if it.transit_id}
        positions = [alpha_idx[t] for t in union_tids if t in alpha_idx]
        if positions and (max(positions) - min(positions)) > max_alpha_span:
            return False
    return True


def _merge_cluster(a: _Cluster, b: _Cluster) -> _Cluster:
    return _Cluster(
        items=a.items + b.items,
        weight_g=a.weight_g + b.weight_g,
        volume_cm3=a.volume_cm3 + b.volume_cm3,
        order_set=a.order_set | b.order_set,
        is_ugly=a.is_ugly or b.is_ugly,
        is_max_out=a.is_max_out or b.is_max_out,
    )


def _seed_cluster(it: Item, cap_w: float, cap_v: float,
                  respect_max_out: bool = True) -> _Cluster:
    line_w = it.quantity * it.unit_weight_g
    line_v = it.quantity * it.unit_volume_cm3
    is_ugly = _ugly_check(it.quantity, it.unit_weight_g, it.unit_volume_cm3, cap_w, cap_v) or \
              (not it.splittable and (line_w > cap_w or line_v > cap_v))
    return _Cluster(
        items=[it],
        weight_g=line_w,
        volume_cm3=line_v,
        order_set={it.order_no},
        is_ugly=is_ugly,
        is_max_out=it.is_max_out if respect_max_out else False,
    )


def build_totes_for_zone(items_in_zone: List[Item], zone: str, cfg: StoreConfig,
                         matrix: DistanceMatrix,
                         alpha_idx: Optional[Dict[str, int]] = None) -> List[ToteResult]:
    if not cfg._calibrated and items_in_zone:
        cfg.calibrate(matrix, items_in_zone)
    cap_w = cfg.capacity_max_weight_g[zone]
    cap_v = cfg.capacity_max_volume_cm3[zone]
    max_orders = cfg.frozen_max_orders_per_tote if zone == "Freezer" else 1
    respect_max_out = cfg.respect_max_out.get(zone, True)
    enable_lb_pack = cfg.f7_min_count_pack.get(zone, False)

    # Step A: split oversized splittable lines
    expanded: List[Item] = []
    for it in items_in_zone:
        expanded.extend(_split_oversized_line(it, cap_w, cap_v))

    # Step B: seed clusters per item. When respect_max_out=False for this zone
    # (e.g. Chilled, see CLAUDE.md "Key callouts"), the is_max_out flag is zeroed
    # at seed time so _can_merge / _minimize_tote_count_per_order / _rebalance_distance
    # all treat the item as regular and free to merge.
    clusters: List[_Cluster] = [_seed_cluster(it, cap_w, cap_v, respect_max_out)
                                for it in expanded]

    # Step C: agglomerative merge — all zones build totes per-order (1 order per tote).
    # Freezer's 2-order-per-tote allowance is exploited at TROLLEY-BUILD time via
    # _consolidate_same_tid_totes — only same-TID orders are pooled, so a pooled tote
    # still counts as 1 TID and doesn't burn PAT slack.
    groups: Dict[str, List[_Cluster]] = {}
    for c in clusters:
        order = next(iter(c.order_set))
        groups.setdefault(order, []).append(c)
    merged: List[_Cluster] = []
    # Track F7 reductions for diagnostic
    f7_before = 0
    f7_after = 0
    for order_clusters in groups.values():
        ag = _agglomerative_merge(order_clusters, cap_w, cap_v, max_orders=1,
                                  matrix=matrix, alpha_idx=None, max_alpha_span=None,
                                  cfg=cfg, use_trip_cost=cfg.enable_trip_cost_merge)
        f7_before += len(ag)
        # F7: minimum-tote-count repacking
        packed = _minimize_tote_count_per_order(ag, cap_w, cap_v, matrix,
                                                enable_qty_split=cfg.enable_qty_split_f7,
                                                enable_lb_pack=enable_lb_pack)
        # F8: distance-aware reseat (preserves count)
        balanced = _rebalance_distance(packed, cap_w, cap_v, matrix, cfg)
        f7_after += len(balanced)
        merged.extend(balanced)
    if f7_before != f7_after:
        print(f"      [{zone} F7] reduced {f7_before} -> {f7_after} totes "
              f"({f7_before - f7_after} eliminated)")
    clusters = merged

    # Step D: package as ToteResult
    totes: List[ToteResult] = []
    for i, c in enumerate(clusters):
        tote_id = f"{zone[0]}T_{i+1:04d}"
        # Production reports 1 TrayHeaderID per (order, trolley). Mirror that for parity.
        n_orders = max(1, len(c.order_set))
        totes.append(ToteResult(
            tote_id=tote_id,
            zone=zone,
            order_nos=sorted(c.order_set),
            items=c.items,
            total_weight_g=c.weight_g,
            total_volume_cm3=c.volume_cm3,
            aisles=sorted({it.aisle_location for it in c.items}),
            location_keys=[it.location_key for it in c.items],
            transit_ids=sorted({it.transit_id for it in c.items}),
            is_ugly=c.is_ugly,
            notes=[],
            tray_label_count=n_orders,
        ))
    return totes


def _per_order_lower_bound(items: List[Item], cap_w: float, cap_v: float) -> int:
    """Volumetric/weight lower bound on the tote count for a set of single-order items.

    Lower bound = max(ceil(sum_w/cap_w), ceil(sum_v/cap_v), count of ugly/max-out items
    that must occupy a tote alone).
    """
    sum_w = sum(it.quantity * it.unit_weight_g for it in items)
    sum_v = sum(it.quantity * it.unit_volume_cm3 for it in items)
    forced_alone = sum(1 for it in items if it.is_max_out)
    # Also count items that as a single line exceed cap (will be ugly)
    for it in items:
        if it.is_max_out:
            continue
        line_w = it.quantity * it.unit_weight_g
        line_v = it.quantity * it.unit_volume_cm3
        if not it.splittable and (line_w > cap_w or line_v > cap_v):
            forced_alone += 1
    bound = max(
        math.ceil(sum_w / cap_w) if cap_w else 0,
        math.ceil(sum_v / cap_v) if cap_v else 0,
        forced_alone or 1,
    )
    return max(1, bound)


def _ffd_pack_items(items: List[Item], cap_w: float, cap_v: float,
                    n_target: int) -> Optional[List[List[Item]]]:
    """First Fit Decreasing bin-pack into exactly n_target bins, if feasible.

    Returns the bin assignment, or None if infeasible. Distance is ignored at this stage
    (will be the secondary objective handled by F8 distance-aware reseat).
    """
    # Sort items by descending volume (volume tends to be the binding constraint)
    sorted_items = sorted(items, key=lambda it: (it.quantity * it.unit_volume_cm3,
                                                 it.quantity * it.unit_weight_g),
                          reverse=True)
    bins: List[Tuple[List[Item], float, float]] = [([], 0.0, 0.0) for _ in range(n_target)]
    for it in sorted_items:
        w = it.quantity * it.unit_weight_g
        v = it.quantity * it.unit_volume_cm3
        # Place in first bin with capacity (FFD)
        placed = False
        for i, (bi, bw, bv) in enumerate(bins):
            if bw + w <= cap_w and bv + v <= cap_v:
                bi.append(it)
                bins[i] = (bi, bw + w, bv + v)
                placed = True
                break
        if not placed:
            return None  # n_target too small
    return [b[0] for b in bins if b[0]]


def _pack_items_strategy(items: List[Item], cap_w: float, cap_v: float,
                         n_target: int, sort_key: str,
                         strategy: str) -> Optional[List[List[Item]]]:
    """Generic decreasing bin-pack. sort_key in {volume, weight, max_frac, sum_frac}.
    strategy in {first_fit, best_fit, worst_fit}. Returns bin assignment or None.
    """
    def key(it: Item) -> float:
        w = it.quantity * it.unit_weight_g
        v = it.quantity * it.unit_volume_cm3
        if sort_key == "volume":
            return v
        if sort_key == "weight":
            return w
        if sort_key == "max_frac":
            return max(w / cap_w if cap_w else 0.0, v / cap_v if cap_v else 0.0)
        if sort_key == "sum_frac":
            return (w / cap_w if cap_w else 0.0) + (v / cap_v if cap_v else 0.0)
        return v
    sorted_items = sorted(items, key=key, reverse=True)
    bins: List[Tuple[List[Item], float, float]] = [([], 0.0, 0.0) for _ in range(n_target)]
    for it in sorted_items:
        w = it.quantity * it.unit_weight_g
        v = it.quantity * it.unit_volume_cm3
        chosen = -1
        chosen_score = None
        for i, (_bi, bw, bv) in enumerate(bins):
            if bw + w > cap_w or bv + v > cap_v:
                continue
            if strategy == "first_fit":
                chosen = i
                break
            # remaining capacity AFTER placement (normalised, smaller=tighter)
            rem_w = (cap_w - (bw + w)) / cap_w if cap_w else 0.0
            rem_v = (cap_v - (bv + v)) / cap_v if cap_v else 0.0
            score = max(rem_w, rem_v)  # tightest dimension dominates
            if strategy == "best_fit":
                if chosen_score is None or score < chosen_score:
                    chosen, chosen_score = i, score
            elif strategy == "worst_fit":
                if chosen_score is None or score > chosen_score:
                    chosen, chosen_score = i, score
        if chosen < 0:
            return None
        bi, bw, bv = bins[chosen]
        bi.append(it)
        bins[chosen] = (bi, bw + w, bv + v)
    return [b[0] for b in bins if b[0]]


def _multi_strategy_pack(items: List[Item], cap_w: float, cap_v: float,
                         n_target: int) -> Optional[List[List[Item]]]:
    """Try several decreasing bin-pack strategies; return the first feasible packing
    that hits n_target (or fewer). Strategies are cheap to enumerate (~12 attempts).
    """
    strategies = [
        ("max_frac", "best_fit"),
        ("volume", "best_fit"),
        ("weight", "best_fit"),
        ("sum_frac", "best_fit"),
        ("max_frac", "first_fit"),
        ("volume", "first_fit"),
        ("weight", "first_fit"),
        ("sum_frac", "first_fit"),
        ("max_frac", "worst_fit"),
        ("volume", "worst_fit"),
    ]
    for sort_key, strategy in strategies:
        bins = _pack_items_strategy(items, cap_w, cap_v, n_target, sort_key, strategy)
        if bins is not None and len(bins) <= n_target:
            return bins
    return None


def _split_item_qty(it: Item, take: int, suffix: str) -> Item:
    """Create a fragment of `it` with quantity=take. Used by F7 partial-quantity moves
    (lever D) and any future quantity-split logic. line_no gets a `.q{suffix}` marker
    so downstream telemetry can detect splits.
    """
    return Item(
        order_no=it.order_no,
        line_no=f"{it.line_no}.q{suffix}",
        stock_code=it.stock_code,
        quantity=take,
        unit_weight_g=it.unit_weight_g,
        unit_volume_cm3=it.unit_volume_cm3,
        aisle_location=it.aisle_location,
        bay_location=it.bay_location,
        location_key=it.location_key,
        zone=it.zone,
        transit_id=it.transit_id,
        truck=it.truck,
        trolley_id_baseline=it.trolley_id_baseline,
        delivery_start=it.delivery_start,
        is_max_out=it.is_max_out,
        splittable=it.splittable,
        is_special=it.is_special,
        picking_type=it.picking_type,
        tray_header_id=it.tray_header_id,
    )


def _minimize_tote_count_per_order(order_clusters: List[_Cluster], cap_w: float,
                                   cap_v: float, matrix: DistanceMatrix,
                                   enable_qty_split: bool = False,
                                   enable_lb_pack: bool = False) -> List[_Cluster]:
    """F7 (distance-aware): For a single order's clusters, iteratively dissolve the
    smallest mutable cluster by moving each of its items into the geographically-closest
    target cluster that has spare capacity. If all items can be redistributed the source
    is removed; otherwise the cluster is left in place and we move on.

    Lever D (enable_qty_split=True): when a splittable item (qty > 1) doesn't fit
    any single target whole, distribute its units across MULTIPLE targets in
    closest-first order. Each placement becomes a quantity-fragment Item with a
    distinct line_no suffix. Saves totes (-18 on Ambient) but the resulting trolley
    compositions regress walk per trolley (+7.9 m/trolley). Off by default.

    LB pack (enable_lb_pack=True, 2026-05-11): try a multi-strategy decreasing
    bin-pack into the volumetric lower bound before greedy donor-dissolution. If
    the pack succeeds with FEWER totes than greedy F7 would otherwise leave, use
    it (F8 then recovers distance via per-pair swaps, preserving count). Falls
    back to greedy on infeasibility.
    """
    if len(order_clusters) <= 1:
        return order_clusters
    immutable = [c for c in order_clusters if c.is_ugly or c.is_max_out]
    mutable = [c for c in order_clusters if not (c.is_ugly or c.is_max_out)]
    if len(mutable) <= 1:
        return order_clusters

    # LB pack path: attempt count-optimal bin-pack before greedy dissolution.
    if enable_lb_pack:
        all_mut_items = [it for c in mutable for it in c.items]
        sum_w = sum(it.quantity * it.unit_weight_g for it in all_mut_items)
        sum_v = sum(it.quantity * it.unit_volume_cm3 for it in all_mut_items)
        lb = max(1,
                 math.ceil(sum_w / cap_w) if cap_w else 1,
                 math.ceil(sum_v / cap_v) if cap_v else 1)
        # Only try if LB is strictly better than current cluster count
        if lb < len(mutable):
            packed = _multi_strategy_pack(all_mut_items, cap_w, cap_v, lb)
            if packed is not None and len(packed) <= len(mutable):
                order_set = mutable[0].order_set
                new_mutable: List[_Cluster] = []
                for bin_items in packed:
                    if not bin_items:
                        continue
                    bw = sum(it.quantity * it.unit_weight_g for it in bin_items)
                    bv = sum(it.quantity * it.unit_volume_cm3 for it in bin_items)
                    new_mutable.append(_Cluster(
                        items=bin_items, weight_g=bw, volume_cm3=bv,
                        order_set=set(order_set), is_ugly=False, is_max_out=False,
                    ))
                return immutable + new_mutable
    pool: List[_Cluster] = list(mutable)
    split_counter = 0
    safety = 0
    while safety < 1000:
        safety += 1
        if len(pool) <= 1:
            break
        # Smallest cluster (by max-of fill)
        pool.sort(key=lambda c: max(c.weight_g / cap_w if cap_w else 0,
                                    c.volume_cm3 / cap_v if cap_v else 0))
        donor = pool[0]
        targets = pool[1:]
        # Try to relocate every item in donor; for each item, plan WHOLE placement
        # first, falling back to multi-target quantity split for splittable qty>1 items.
        # Each plan entry: (item, [(target_cluster, units_taken), ...])
        plans: List[Tuple[Item, List[Tuple[_Cluster, int]]]] = []
        target_states: Dict[int, Tuple[float, float]] = {
            id(t): (t.weight_g, t.volume_cm3) for t in targets
        }
        feasible = True
        for it in donor.items:
            w = it.quantity * it.unit_weight_g
            v = it.quantity * it.unit_volume_cm3
            # Phase 1: closest target with capacity for the WHOLE quantity
            best = None
            best_d = float("inf")
            for t in targets:
                tw, tv = target_states[id(t)]
                if tw + w > cap_w or tv + v > cap_v:
                    continue
                rep_loc = _cluster_representative(t)
                d, _ = matrix.lookup(it.location_key, rep_loc)
                if d < best_d:
                    best_d = d
                    best = t
            if best is not None:
                tw, tv = target_states[id(best)]
                target_states[id(best)] = (tw + w, tv + v)
                plans.append((it, [(best, it.quantity)]))
                continue
            # Phase 2: partial-quantity split across closest-first targets (lever D).
            # Only valid for splittable items with qty > 1 and per-unit fits cap.
            if not enable_qty_split:
                feasible = False
                break
            if (not it.splittable) or it.quantity <= 1:
                feasible = False
                break
            if it.unit_weight_g > cap_w or it.unit_volume_cm3 > cap_v:
                feasible = False  # single unit doesn't fit any tote
                break
            ranked: List[Tuple[float, _Cluster]] = []
            for t in targets:
                rep_loc = _cluster_representative(t)
                d, _ = matrix.lookup(it.location_key, rep_loc)
                ranked.append((d, t))
            ranked.sort(key=lambda x: x[0])
            units_remaining = it.quantity
            placements: List[Tuple[_Cluster, int]] = []
            for _d, t in ranked:
                if units_remaining <= 0:
                    break
                tw, tv = target_states[id(t)]
                rem_w = cap_w - tw
                rem_v = cap_v - tv
                max_w_units = int(rem_w // it.unit_weight_g) if it.unit_weight_g > 0 else units_remaining
                max_v_units = int(rem_v // it.unit_volume_cm3) if it.unit_volume_cm3 > 0 else units_remaining
                avail = min(units_remaining, max_w_units, max_v_units)
                if avail <= 0:
                    continue
                placements.append((t, avail))
                target_states[id(t)] = (tw + avail * it.unit_weight_g,
                                        tv + avail * it.unit_volume_cm3)
                units_remaining -= avail
            if units_remaining > 0:
                feasible = False
                break
            plans.append((it, placements))
        if not feasible:
            break  # can't dissolve donor; stop the pass
        # Commit
        for it, placements in plans:
            if len(placements) == 1 and placements[0][1] == it.quantity:
                target, _units = placements[0]
                w = it.quantity * it.unit_weight_g
                v = it.quantity * it.unit_volume_cm3
                target.items.append(it)
                target.weight_g += w
                target.volume_cm3 += v
            else:
                for k, (target, units) in enumerate(placements):
                    split_counter += 1
                    fragment = _split_item_qty(it, units, f"{split_counter}_{k}")
                    target.items.append(fragment)
                    target.weight_g += units * it.unit_weight_g
                    target.volume_cm3 += units * it.unit_volume_cm3
        pool.remove(donor)
    return immutable + pool


def _cluster_trip_cost(c: _Cluster, matrix: DistanceMatrix, cfg: StoreConfig) -> float:
    """Time-equivalent path cost (m + U-turn penalty m-equiv) of picking the cluster's
    items as a stand-alone trip. Same objective as trolley-level trip_cost so the swap
    pass and the trolley builder both optimise to the same metric.
    """
    if not c.items:
        return 0.0
    d, u, _ = trip_cost(c.items, matrix, cfg)
    return d + u * cfg.uturn_penalty_m


def _rebalance_distance(clusters: List[_Cluster], cap_w: float, cap_v: float,
                        matrix: DistanceMatrix, cfg: StoreConfig) -> List[_Cluster]:
    """F8: Item-swap post-pass with trip-cost objective. For each pair of same-order
    clusters, try one-way moves AND two-way swaps; accept if the SUM of per-cluster
    time-equivalent trip_cost drops. This is the same objective the trolley builder
    uses, so tighter tote envelopes here translate directly into tighter trolley walks.

    Same cluster count in/out. Capacity-respecting. Hard items (ugly/max-out) pinned.
    """
    if len(clusters) <= 1:
        return clusters
    pool = [c for c in clusters]
    costs = [_cluster_trip_cost(c, matrix, cfg) for c in pool]
    safety = 0
    improved = True
    while improved and safety < 400:
        improved = False
        safety += 1
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                a, b = pool[i], pool[j]
                if a.is_ugly or b.is_ugly or a.is_max_out or b.is_max_out:
                    continue
                base = costs[i] + costs[j]
                best_delta = -1e-9  # require strict improvement
                best_move: Optional[Tuple[str, int, Optional[int], _Cluster, _Cluster]] = None

                # Move one item a -> b
                for ai in range(len(a.items)):
                    if len(a.items) == 1:
                        break
                    it = a.items[ai]
                    w = it.quantity * it.unit_weight_g
                    v = it.quantity * it.unit_volume_cm3
                    if b.weight_g + w > cap_w or b.volume_cm3 + v > cap_v:
                        continue
                    new_a = _Cluster(items=a.items[:ai] + a.items[ai+1:],
                                     weight_g=a.weight_g - w, volume_cm3=a.volume_cm3 - v,
                                     order_set=a.order_set, is_ugly=False, is_max_out=False)
                    new_b = _Cluster(items=b.items + [it],
                                     weight_g=b.weight_g + w, volume_cm3=b.volume_cm3 + v,
                                     order_set=b.order_set, is_ugly=False, is_max_out=False)
                    s = _cluster_trip_cost(new_a, matrix, cfg) + _cluster_trip_cost(new_b, matrix, cfg)
                    delta = s - base
                    if delta < best_delta:
                        best_delta = delta
                        best_move = ("a2b", ai, None, new_a, new_b)

                # Move one item b -> a
                for bj in range(len(b.items)):
                    if len(b.items) == 1:
                        break
                    it = b.items[bj]
                    w = it.quantity * it.unit_weight_g
                    v = it.quantity * it.unit_volume_cm3
                    if a.weight_g + w > cap_w or a.volume_cm3 + v > cap_v:
                        continue
                    new_a = _Cluster(items=a.items + [it],
                                     weight_g=a.weight_g + w, volume_cm3=a.volume_cm3 + v,
                                     order_set=a.order_set, is_ugly=False, is_max_out=False)
                    new_b = _Cluster(items=b.items[:bj] + b.items[bj+1:],
                                     weight_g=b.weight_g - w, volume_cm3=b.volume_cm3 - v,
                                     order_set=b.order_set, is_ugly=False, is_max_out=False)
                    s = _cluster_trip_cost(new_a, matrix, cfg) + _cluster_trip_cost(new_b, matrix, cfg)
                    delta = s - base
                    if delta < best_delta:
                        best_delta = delta
                        best_move = ("b2a", None, bj, new_a, new_b)

                # Two-way swap it_a <-> it_b
                for ai in range(len(a.items)):
                    it_a = a.items[ai]
                    wa = it_a.quantity * it_a.unit_weight_g
                    va = it_a.quantity * it_a.unit_volume_cm3
                    for bj in range(len(b.items)):
                        it_b = b.items[bj]
                        wb = it_b.quantity * it_b.unit_weight_g
                        vb = it_b.quantity * it_b.unit_volume_cm3
                        if a.weight_g - wa + wb > cap_w or a.volume_cm3 - va + vb > cap_v:
                            continue
                        if b.weight_g - wb + wa > cap_w or b.volume_cm3 - vb + va > cap_v:
                            continue
                        new_a = _Cluster(items=a.items[:ai] + [it_b] + a.items[ai+1:],
                                         weight_g=a.weight_g - wa + wb,
                                         volume_cm3=a.volume_cm3 - va + vb,
                                         order_set=a.order_set, is_ugly=False, is_max_out=False)
                        new_b = _Cluster(items=b.items[:bj] + [it_a] + b.items[bj+1:],
                                         weight_g=b.weight_g - wb + wa,
                                         volume_cm3=b.volume_cm3 - vb + va,
                                         order_set=b.order_set, is_ugly=False, is_max_out=False)
                        s = _cluster_trip_cost(new_a, matrix, cfg) + _cluster_trip_cost(new_b, matrix, cfg)
                        delta = s - base
                        if delta < best_delta:
                            best_delta = delta
                            best_move = ("swap", ai, bj, new_a, new_b)

                if best_move is not None:
                    _, _, _, new_a, new_b = best_move
                    pool[i] = new_a
                    pool[j] = new_b
                    costs[i] = _cluster_trip_cost(new_a, matrix, cfg)
                    costs[j] = _cluster_trip_cost(new_b, matrix, cfg)
                    improved = True
                    break
            if improved:
                break
    return pool


def _intra_spread(c: _Cluster, matrix: DistanceMatrix) -> float:
    """Legacy helper: max pairwise distance between item locations in a cluster."""
    if len(c.items) <= 1:
        return 0.0
    locs = list({it.location_key for it in c.items if it.location_key})
    if len(locs) <= 1:
        return 0.0
    mx = 0.0
    for i in range(len(locs)):
        for j in range(i + 1, len(locs)):
            d, _ = matrix.lookup(locs[i], locs[j])
            if d > mx:
                mx = d
    return mx


def _agglomerative_merge(clusters: List[_Cluster], cap_w: float, cap_v: float,
                         max_orders: int, matrix: DistanceMatrix,
                         alpha_idx: Optional[Dict[str, int]] = None,
                         max_alpha_span: Optional[int] = None,
                         cfg: Optional[StoreConfig] = None,
                         use_trip_cost: bool = False) -> List[_Cluster]:
    """Agglomerative cluster merge.

    Default scoring (legacy): closest-pair representative-rep walkable distance —
    cheap proxy, geographically intuitive but does NOT capture aisle entry/exit
    or U-turn trade-offs.

    Lever H (use_trip_cost=True, requires cfg): score by Δtrip_cost =
    trip_cost(a∪b) - trip_cost(a) - trip_cost(b). Picks the merge that produces
    the smallest end-to-end pick path increase, capturing aisle entries, U-turn
    decisions and dead-walk savings via the same engine that reports trolley
    walk. Per-cluster trip_cost is cached by id() and refreshed on merge.
    """
    pool = list(clusters)
    if len(pool) <= 1:
        return pool

    tc_cache: Dict[int, float] = {}
    def _tc(c: _Cluster) -> float:
        k = id(c)
        v = tc_cache.get(k)
        if v is not None:
            return v
        if not c.items or cfg is None:
            v = 0.0
        else:
            d, u, _ = trip_cost(c.items, matrix, cfg)
            v = d + u * cfg.uturn_penalty_m
        tc_cache[k] = v
        return v

    while True:
        best_pair = None
        best_score = float("inf")
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                if not _can_merge(pool[i], pool[j], cap_w, cap_v, max_orders,
                                  alpha_idx=alpha_idx, max_alpha_span=max_alpha_span):
                    continue
                if use_trip_cost and cfg is not None:
                    union_items = pool[i].items + pool[j].items
                    d, u, _ = trip_cost(union_items, matrix, cfg)
                    union_cost = d + u * cfg.uturn_penalty_m
                    score = union_cost - _tc(pool[i]) - _tc(pool[j])
                else:
                    score = _cluster_distance(pool[i], pool[j], matrix)
                if score < best_score:
                    best_score = score
                    best_pair = (i, j)
        if best_pair is None:
            break
        i, j = best_pair
        merged = _merge_cluster(pool[i], pool[j])
        tc_cache.pop(id(pool[i]), None)
        tc_cache.pop(id(pool[j]), None)
        pool = [c for k, c in enumerate(pool) if k not in (i, j)] + [merged]
    return pool


# ----------------------------------------------------------------------------
# 7. Pick Across Trucks staging gate
# ----------------------------------------------------------------------------


def pick_across_trucks_select(items: List[Item], cfg: StoreConfig) -> Tuple[List[Item], Dict[str, object]]:
    """Select first N alpha-sequential TransitIDs from the pool and filter items to that window.

    Mirrors the C# IncrementalStringHelper.GenerateStrings logic: take the earliest TransitID in
    the session pool and the next N-1 sequential ones (alphabetical = arrival order by convention).
    Filtering at the ITEM level ensures Frozen cross-order pooling cannot leak out-of-window codes.
    """
    n = cfg.pick_across_trucks
    if n <= 0:
        return list(items), {"pat_disabled": True}
    universe = sorted({it.transit_id for it in items if it.transit_id})
    if not universe:
        return list(items), {"pat_no_transit_ids": True}
    selected = universe[:n]
    selected_set = set(selected)
    in_window = [it for it in items if it.transit_id in selected_set]
    deferred = len(items) - len(in_window)
    return in_window, {
        "selected_transit_ids": selected,
        "deferred_item_count": deferred,
        "deferred_orders": sorted({it.order_no for it in items if it.transit_id not in selected_set}),
    }


# ----------------------------------------------------------------------------
# 8. Trolley builder
# ----------------------------------------------------------------------------


def _tote_representative(t: ToteResult) -> str:
    """Median-aisle / median-bay item as the tote's representative location for proxy distance."""
    if not t.items:
        return ""
    sorted_items = sorted(t.items, key=lambda it: (aisle_int(it.aisle_location), bay_int(it.bay_location)))
    mid = sorted_items[len(sorted_items) // 2]
    return mid.location_key


def _build_tote_distance_matrix(totes: List[ToteResult], matrix: DistanceMatrix
                                ) -> List[List[float]]:
    """Precomputed walkable distance between every pair of totes, using each tote's
    representative location. O(N^2) lookups vs O(N^2 * items^2) for mean-pairwise.
    """
    reps = [_tote_representative(t) for t in totes]
    n = len(totes)
    dm = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            ra = reps[i]
            rb = reps[j]
            if not ra or not rb:
                d = 0.0
            else:
                d, _ = matrix.lookup(ra, rb)
            dm[i][j] = d
            dm[j][i] = d
    return dm


def _proxy_trolley_score(idxs: List[int], dm: List[List[float]]) -> float:
    """Sum of intra-trolley pairwise tote-tote distances. Lower is better."""
    s = 0.0
    for i in range(len(idxs)):
        for j in range(i + 1, len(idxs)):
            s += dm[idxs[i]][idxs[j]]
    return s


def _path_cost(d: float, uturns: int, cfg: StoreConfig) -> float:
    """Time-equivalent path cost: metres + cfg.uturn_penalty_m per U-turn.
    Used everywhere a single optimisation target combining walk + U-turns is needed.
    Returning a single float lets pickers and SA accept walk/U-turn trade-offs that
    save TIME (the metric we actually care about) rather than raw distance.
    """
    return d + uturns * cfg.uturn_penalty_m


def _trolley_path_cost(items: List[Item], matrix: DistanceMatrix, cfg: StoreConfig) -> float:
    d, u, _ = trip_cost(items, matrix, cfg)
    return _path_cost(d, u, cfg)


# ============================================================
# Affinity scoring (Phase B): multi-component score used to
# select trolley composition before SPT-scored evaluation.
# Mirrors the v1 PathSegmentedTrolleyAlgorithmDesign formula
# but uses the real distance matrix for centroid proximity and
# dead-walk computation (instead of geometric estimates).
# ============================================================


def _consolidate_same_tid_totes(totes: List[ToteResult], cap_w: float, cap_v: float,
                                 max_orders: int = 2) -> List[ToteResult]:
    """Trolley-time tote consolidation for Freezer (2026-05-11) and Security.

    Operational rule: a tote may hold multiple distinct orders, but ONLY if all
    orders share the same TransitID. Building totes per-order keeps PAT
    accounting clean (1 TID per tote); this post-pass at trolley-assembly time
    merges same-TID totes into single physical totes so the trolley packs more
    orders per slot without burning PAT slack.

    Iterative greedy pair-merge within each (single-TID) group, sorted
    smallest-fill first. Repeats until no further merge is possible — when
    max_orders > 2, this allows a single physical tote to absorb 3+ same-TID
    orders capacity-permitting (used by Security back-room picking).
    Capacity-respecting. Returns a new list; merged totes get id "a+b[+c...]"
    and tray_label_count = combined order count.
    """
    def _pass(in_totes: List[ToteResult]) -> Tuple[List[ToteResult], bool]:
        by_tid: Dict[str, List[ToteResult]] = {}
        others: List[ToteResult] = []
        for t in in_totes:
            # Allow already-merged totes (order_nos count >= 1) back in if their
            # TID set is still a singleton and they haven't hit max_orders.
            if (len(t.transit_ids) == 1
                    and len(t.order_nos) < max_orders
                    and not t.is_ugly):
                by_tid.setdefault(t.transit_ids[0], []).append(t)
            else:
                others.append(t)
        out: List[ToteResult] = list(others)
        merged_any = False
        for tid, group in by_tid.items():
            # Sort smallest-fill first so we try to pair tight totes (better packing).
            group.sort(key=lambda t: t.total_weight_g / cap_w + t.total_volume_cm3 / cap_v)
            used = [False] * len(group)
            for i in range(len(group)):
                if used[i]:
                    continue
                a = group[i]
                best_j = -1
                best_slack = float("inf")
                for j in range(i + 1, len(group)):
                    if used[j]:
                        continue
                    b = group[j]
                    if a.total_weight_g + b.total_weight_g > cap_w:
                        continue
                    if a.total_volume_cm3 + b.total_volume_cm3 > cap_v:
                        continue
                    combined_orders = set(a.order_nos) | set(b.order_nos)
                    if len(combined_orders) > max_orders:
                        continue
                    # Prefer the pair that uses the most spare capacity.
                    slack = (cap_w - a.total_weight_g - b.total_weight_g) / cap_w + \
                            (cap_v - a.total_volume_cm3 - b.total_volume_cm3) / cap_v
                    if slack < best_slack:
                        best_slack = slack
                        best_j = j
                if best_j == -1:
                    out.append(a)
                    used[i] = True
                    continue
                b = group[best_j]
                merged_orders = sorted(set(a.order_nos) | set(b.order_nos))
                merged = ToteResult(
                    tote_id=f"{a.tote_id}+{b.tote_id}",
                    zone=a.zone,
                    order_nos=merged_orders,
                    items=a.items + b.items,
                    total_weight_g=a.total_weight_g + b.total_weight_g,
                    total_volume_cm3=a.total_volume_cm3 + b.total_volume_cm3,
                    aisles=sorted(set(a.aisles) | set(b.aisles)),
                    location_keys=list(a.location_keys) + list(b.location_keys),
                    transit_ids=sorted(set(a.transit_ids) | set(b.transit_ids)),
                    is_ugly=False,
                    notes=list(a.notes) + list(b.notes),
                    tray_label_count=len(merged_orders),
                )
                out.append(merged)
                used[i] = True
                used[best_j] = True
                merged_any = True
        return out, merged_any

    current = list(totes)
    safety = 0
    while safety < 32:
        safety += 1
        current, changed = _pass(current)
        if not changed:
            break
        if max_orders <= 2:
            # Pair-merging only — one pass suffices (already-merged totes have
            # order_nos == 2 == max_orders and are excluded from the next pass).
            break
    return current


def _compute_affinity(proxy_dist_m: float, cfg: StoreConfig) -> float:
    """Pure matrix-distance affinity (0..1, higher = better partners).

    Option B (Hybrid CVRP, 2026-05-11): the calibrated distance matrix is the sole
    physical-proximity signal. Two totes whose representative locations are close
    by matrix distance are likely to walk well together; aisle-sharing and
    bay-alignment are emergent properties of matrix-distance, not separate signals.

    Replaces the legacy multi-component score (aisle Jaccard + bay alignment +
    centroid proximity + start compatibility + non-std bonus), which baked in the
    1419-draft aisle-layout assumptions.
    """
    if cfg.max_store_distance_m <= 0.0:
        return 0.0
    return max(0.0, 1.0 - proxy_dist_m / cfg.max_store_distance_m)


def _build_affinity_matrix(totes: List[ToteResult],
                           dm_proxy: List[List[float]],
                           cfg: StoreConfig) -> List[List[float]]:
    n = len(totes)
    aff = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            a = _compute_affinity(dm_proxy[i][j], cfg)
            aff[i][j] = a
            aff[j][i] = a
    return aff


def _greedy_seed_trolleys(totes: List[ToteResult], cfg: StoreConfig,
                          dm: List[List[float]]) -> List[List[int]]:
    """Greedy bin-pack on tote indices using the precomputed distance matrix."""
    n = len(totes)
    remaining = set(range(n))
    trolleys: List[List[int]] = []
    while remaining:
        # Seed: tote with highest mean distance to others (likely on the periphery)
        seed = max(remaining, key=lambda i: sum(dm[i][j] for j in remaining if j != i))
        cur = [seed]
        remaining.remove(seed)
        while len(cur) < cfg.trolley_max_totes and remaining:
            best = None
            best_d = float("inf")
            for cand in remaining:
                avg = sum(dm[cand][k] for k in cur) / len(cur)
                if avg < best_d:
                    best_d = avg
                    best = cand
            if best is None:
                break
            cur.append(best)
            remaining.remove(best)
        trolleys.append(cur)
    return trolleys


def _swap_refine(trolleys: List[List[int]], cfg: StoreConfig,
                 dm: List[List[float]], max_passes: int = 2) -> List[List[int]]:
    best = [list(t) for t in trolleys]
    scores = [_proxy_trolley_score(t, dm) for t in best]
    for _ in range(max_passes):
        improved = False
        for i in range(len(best)):
            for j in range(i + 1, len(best)):
                for ai in range(len(best[i])):
                    for bj in range(len(best[j])):
                        cand_i = list(best[i])
                        cand_j = list(best[j])
                        cand_i[ai], cand_j[bj] = cand_j[bj], cand_i[ai]
                        new_i = _proxy_trolley_score(cand_i, dm)
                        new_j = _proxy_trolley_score(cand_j, dm)
                        if new_i + new_j + 1e-6 < scores[i] + scores[j]:
                            best[i] = cand_i
                            best[j] = cand_j
                            scores[i] = new_i
                            scores[j] = new_j
                            improved = True
        if not improved:
            break
    return best


def _sa_refine(trolleys: List[List[int]], cfg: StoreConfig,
               dm: List[List[float]], seed: int) -> List[List[int]]:
    rng = random.Random(seed)
    cur = [list(t) for t in trolleys]
    scores = [_proxy_trolley_score(t, dm) for t in cur]
    cur_total = sum(scores)
    best = [list(t) for t in cur]
    best_total = cur_total
    T = max(20.0, cur_total * 0.05)
    cooling = 0.995
    for _ in range(cfg.sa_iterations):
        if len(cur) < 2:
            break
        i = rng.randrange(len(cur))
        j = rng.randrange(len(cur))
        if i == j or not cur[i] or not cur[j]:
            T *= cooling
            continue
        ai = rng.randrange(len(cur[i]))
        bj = rng.randrange(len(cur[j]))
        cand_i = list(cur[i])
        cand_j = list(cur[j])
        cand_i[ai], cand_j[bj] = cand_j[bj], cand_i[ai]
        new_i = _proxy_trolley_score(cand_i, dm)
        new_j = _proxy_trolley_score(cand_j, dm)
        delta = (new_i + new_j) - (scores[i] + scores[j])
        if delta < 0 or rng.random() < math.exp(-delta / max(T, 1e-3)):
            cur[i] = cand_i
            cur[j] = cand_j
            scores[i] = new_i
            scores[j] = new_j
            cur_total += delta
            if cur_total < best_total:
                best = [list(t) for t in cur]
                best_total = cur_total
        T *= cooling
    return best


def _split_for_cold_chain(trolley_totes: List[ToteResult], items_by_order: Dict[str, List[Item]],
                          cfg: StoreConfig, matrix: DistanceMatrix) -> List[List[ToteResult]]:
    """Split a trolley until its HC12 cold-chain compliance time is within cap.

    Uses cold_chain_compliance_time_s (arrival-at-first-pick -> staging), NOT the
    full planning goal_time. Setup, downtime and the approach walk happen before
    the first cold item is touched and are therefore excluded.

    Recursive: while the trolley breaches the cap, pull the single tote whose
    removal yields the lowest residual cold-chain time, dump it into its own
    1-tote orphan, repeat on the remainder.
    """
    cap_s = cfg.cold_chain_cap_min * 60.0
    if not trolley_totes:
        return []
    all_items = [it for t in trolley_totes for it in t.items]
    cc_time = cold_chain_compliance_time_s(all_items, items_by_order, matrix, cfg)
    if cc_time <= cap_s or len(trolley_totes) == 1:
        return [trolley_totes]
    # Remove the tote whose removal reduces cold-chain compliance time the most.
    best_remove_idx = -1
    best_cc_after = float("inf")
    for i in range(len(trolley_totes)):
        rest = trolley_totes[:i] + trolley_totes[i + 1:]
        if not rest:
            continue
        rest_items = [it for t in rest for it in t.items]
        cc = cold_chain_compliance_time_s(rest_items, items_by_order, matrix, cfg)
        if cc < best_cc_after:
            best_cc_after = cc
            best_remove_idx = i
    if best_remove_idx < 0:
        return [trolley_totes]
    removed = trolley_totes[best_remove_idx]
    rest = trolley_totes[:best_remove_idx] + trolley_totes[best_remove_idx + 1:]
    return _split_for_cold_chain(rest, items_by_order, cfg, matrix) + [[removed]]


def _build_trolley_result(trolley_id: str, totes: List[ToteResult], matrix: DistanceMatrix,
                          cfg: StoreConfig, items_by_order: Dict[str, List[Item]]) -> TrolleyResult:
    distance, uturns, method = trip_cost([it for t in totes for it in t.items], matrix, cfg)
    walk_time = distance * WALK_SPEED_SPM + uturns * UTURN_PENALTY_S
    transit_ids = sorted({tid for t in totes for tid in t.transit_ids})
    skus = {it.stock_code for t in totes for it in t.items}
    total_lines = sum(len(t.items) for t in totes)
    zone = totes[0].zone if totes else ""
    tr = TrolleyResult(
        trolley_id=trolley_id,
        zone=zone,
        totes=totes,
        transit_ids_covered=transit_ids,
        walk_distance_m=round(distance, 2),
        uturn_count=uturns,
        walk_time_s=round(walk_time, 2),
        goal_time_s=0.0,
        unique_skus=len(skus),
        total_lines=total_lines,
        pat_window_start=transit_ids[0] if transit_ids else "",
        pat_window_end=transit_ids[-1] if transit_ids else "",
        notes=[f"path_method={method}"],
    )
    tr.goal_time_s = round(calculate_goal_time(tr, items_by_order), 2)
    all_items = [it for t in totes for it in t.items]
    tr.cold_chain_time_s = round(cold_chain_compliance_time_s(all_items, items_by_order, matrix, cfg), 2)
    return tr


def _tote_anchor_transit(t: ToteResult) -> str:
    """A tote's effective TransitID for PAT purposes is the earliest of its constituents."""
    return min(t.transit_ids) if t.transit_ids else ""


def _next_pat_window(pool: List[ToteResult], n: int) -> List[str]:
    """First N distinct anchor TransitIDs (alpha-sorted) with remaining totes in the pool.

    Window slides on tote-ANCHOR depletion: if no tote is anchored to BC, the window jumps to
    [BB, BD, BF] (whatever the next available anchors are). Per-zone evaluation.
    """
    if n <= 0 or not pool:
        return []
    distinct_anchors = sorted({_tote_anchor_transit(t) for t in pool if _tote_anchor_transit(t)})
    return distinct_anchors[:n]


def _eligible_count(pool: List[ToteResult], window: Set[str]) -> int:
    """Count totes whose anchor is in window AND whose full TransitID set is a subset of window."""
    return sum(1 for t in pool
               if _tote_anchor_transit(t) in window
               and set(t.transit_ids).issubset(window))


def _select_pat_window(pool: List[ToteResult], cfg: StoreConfig) -> List[str]:
    """Look-ahead PAT window selector.

    Default behaviour returns the first N distinct anchors (alpha-sorted). However if that
    default window does not have enough eligible totes to fill a full trolley AND there are
    further anchors available downstream, try single-anchor replacements (drop one, pull the
    next alpha anchor not in window). Bias: retain the earliest anchor (drop later ones first)
    so we never permanently skip an early truck.

    Returns the chosen N-anchor list (alpha-sorted). Falls back to default if no replacement
    yields >= target_totes_per_trolley eligible totes.
    """
    n = cfg.pick_across_trucks
    target = cfg.target_totes_per_trolley
    if n <= 0 or not pool:
        return []
    distinct_anchors = sorted({_tote_anchor_transit(t) for t in pool if _tote_anchor_transit(t)})
    if len(distinct_anchors) <= n:
        return distinct_anchors
    default_window = distinct_anchors[:n]
    default_set = set(default_window)
    default_count = _eligible_count(pool, default_set)
    if default_count >= target:
        return default_window
    # Tail-deferral: try dropping the LATEST anchor in the window and pulling the next alpha
    # anchor from the remaining pool. Keep iterating drops (latest first) and replacement
    # candidates (earliest next first) until target hit or all options exhausted.
    remaining_anchors = distinct_anchors[n:]  # alpha-sorted, earliest first
    best_window = default_window
    best_count = default_count
    # drop_idx iterates from latest in window (n-1) down to 1 (always keep earliest at index 0)
    for drop_idx in range(n - 1, 0, -1):
        for repl in remaining_anchors:
            candidate = list(default_window)
            candidate[drop_idx] = repl
            cand_set = set(candidate)
            cnt = _eligible_count(pool, cand_set)
            if cnt > best_count:
                best_count = cnt
                best_window = sorted(candidate)
                if best_count >= target:
                    return best_window
    return sorted(best_window)


def _pick_single_trolley(eligible: List[ToteResult], cfg: StoreConfig,
                         dm_eligible: List[List[float]]) -> List[int]:
    """Greedy single-trolley pick: seed with most-isolated tote, add closest while honouring
    trolley_max_totes and the constraint that the trolley's total distinct TransitIDs stay <= N.
    """
    n = len(eligible)
    if n == 0:
        return []
    if n == 1:
        return [0]
    seed = max(range(n), key=lambda i: sum(dm_eligible[i][j] for j in range(n) if j != i))
    chosen = [seed]
    chosen_tids: Set[str] = set(eligible[seed].transit_ids)
    remaining = set(range(n)) - {seed}
    while len(chosen) < cfg.trolley_max_totes and remaining:
        best = None
        best_d = float("inf")
        for cand in remaining:
            cand_tids = chosen_tids | set(eligible[cand].transit_ids)
            if len(cand_tids) > cfg.pick_across_trucks:
                continue
            avg = sum(dm_eligible[cand][k] for k in chosen) / len(chosen)
            if avg < best_d:
                best_d = avg
                best = cand
        if best is None:
            break
        chosen.append(best)
        chosen_tids |= set(eligible[best].transit_ids)
        remaining.discard(best)
    return chosen


def _pick_single_trolley_affinity(eligible: List[ToteResult], cfg: StoreConfig,
                                  matrix: DistanceMatrix,
                                  dm_eligible: List[List[float]],
                                  aff: List[List[float]],
                                  seed_indices: Optional[List[int]] = None,
                                  construction_rng: Optional[random.Random] = None,
                                  items_by_order: Optional[Dict[str, List[Item]]] = None,
                                  enforce_cold_chain: bool = False,
                                  ) -> List[int]:
    """Affinity-based single-trolley pick: try each seed, fill K-1 partners from the full eligible
    pool by affinity (respecting PAT TID ceiling and trolley_max_totes), score each candidate
    trolley by full SPT trip_cost, return the trolley indices with the lowest trip distance.

    seed_indices restricts which totes are tried as seeds (used to alpha-bias early trucks and
    to bound runtime when the eligible pool is large). If None, every tote is a candidate seed.
    Tiebreak on (trip_cost, min_anchor_transit_id) so earlier-truck seeds win on near-ties.

    construction_rng (G4): when provided, the Phase 2 pick is sampled uniformly from the top
    cfg.g4_perturbation_rank candidates by cost rather than always taking the absolute best.
    Enables construction-order best-of-N: run N greedy passes (run-0 unperturbed, runs 1..N-1
    perturbed via this RNG), keep the lowest cumulative cost. Run-0 unperturbed guarantees the
    G4 wrapper never regresses below baseline.

    enforce_cold_chain (C1, 2026-05-11): when True, candidate evaluations skip totes whose
    addition would push cold_chain_compliance_time_s over cfg.cold_chain_cap_min. Prevents
    the breach at composition time rather than splitting after the fact. Mirrors the
    PAT-feasibility guard. Requires items_by_order to be provided. Skipped totes remain in
    the caller's pool for the next trolley pick, so the rolling-PAT logic gets a chance to
    pair them with each other or with later-window totes.
    """
    n = len(eligible)
    if n == 0:
        return []
    if n == 1:
        return [0]
    cap = cfg.trolley_max_totes
    pat_n = cfg.pick_across_trucks
    seeds = seed_indices if seed_indices is not None else list(range(n))
    if not seeds:
        seeds = list(range(n))

    # IMPROVEMENT B: two-phase fill.
    # Phase 1 (cheap, all seeds): static affinity-rank fill, score by trip_cost.
    # Phase 2 (expensive, top-K seeds only): marginal trip-cost fill from the same seed,
    #   evaluating actual SPT cost of each candidate addition. The candidate that minimises
    #   time-equivalent path cost wins each fill slot.
    # This globalises the trolley at SPT level for the most promising seeds while keeping
    # runtime bounded by the seed shortlist.
    REFINE_TOP_SEEDS = 3
    MARGIN_TOPK = 12  # G7 (2026-05-12): bumped 6 -> 12, see CLAUDE.md next-trial backlog

    cc_cap_s = cfg.cold_chain_cap_min * 60.0 if enforce_cold_chain else float("inf")
    _need_cc_picker = enforce_cold_chain and items_by_order is not None

    def _cc_ok(items: List[Item]) -> bool:
        """C1 guard: would this item set still satisfy HC12 cold-chain compliance?"""
        if not _need_cc_picker:
            return True
        _pc, ct = trolley_path_cost_and_cc_time(items, items_by_order, matrix, cfg,
                                                 need_cc=True)
        return ct <= cc_cap_s + 1e-3

    def _aff_fill(seed: int) -> Tuple[List[int], float, str]:
        chosen = [seed]
        chosen_tids: Set[str] = set(eligible[seed].transit_ids)
        chosen_items: List[Item] = list(eligible[seed].items)
        order = sorted((i for i in range(n) if i != seed),
                       key=lambda i: -aff[seed][i])
        for cand in order:
            if len(chosen) >= cap:
                break
            cand_tids = chosen_tids | set(eligible[cand].transit_ids)
            if len(cand_tids) > pat_n:
                continue
            chosen.append(cand)
            chosen_tids = cand_tids
            chosen_items = chosen_items + list(eligible[cand].items)
        d, u, _ = trip_cost(chosen_items, matrix, cfg)
        c = _path_cost(d, u, cfg)
        seed_min_tid = min(eligible[seed].transit_ids) if eligible[seed].transit_ids else "ZZ"
        return chosen, c, seed_min_tid

    def _marginal_fill(seed: int) -> Tuple[List[int], float, str]:
        chosen = [seed]
        chosen_tids: Set[str] = set(eligible[seed].transit_ids)
        chosen_items: List[Item] = list(eligible[seed].items)
        remaining = [i for i in range(n) if i != seed]
        while len(chosen) < cap and remaining:
            def cluster_aff(i: int) -> float:
                return sum(aff[c][i] for c in chosen) / len(chosen)
            ranked = sorted(remaining, key=lambda i: -cluster_aff(i))
            shortlist = ranked[:MARGIN_TOPK]
            best_cand = -1
            best_cost = float("inf")
            for cand in shortlist:
                cand_tids = chosen_tids | set(eligible[cand].transit_ids)
                if len(cand_tids) > pat_n:
                    continue
                new_items = chosen_items + list(eligible[cand].items)
                if not _cc_ok(new_items):
                    continue
                d_, u_, _ = trip_cost(new_items, matrix, cfg)
                c_ = _path_cost(d_, u_, cfg)
                if c_ < best_cost:
                    best_cost = c_
                    best_cand = cand
            if best_cand < 0:
                for cand in ranked[MARGIN_TOPK:]:
                    cand_tids = chosen_tids | set(eligible[cand].transit_ids)
                    if len(cand_tids) > pat_n:
                        continue
                    new_items = chosen_items + list(eligible[cand].items)
                    if not _cc_ok(new_items):
                        continue
                    d_, u_, _ = trip_cost(new_items, matrix, cfg)
                    c_ = _path_cost(d_, u_, cfg)
                    if c_ < best_cost:
                        best_cost = c_
                        best_cand = cand
                if best_cand < 0:
                    break
            chosen.append(best_cand)
            chosen_tids |= set(eligible[best_cand].transit_ids)
            chosen_items.extend(eligible[best_cand].items)
            remaining.remove(best_cand)
        d, u, _ = trip_cost(chosen_items, matrix, cfg)
        c = _path_cost(d, u, cfg)
        seed_min_tid = min(eligible[seed].transit_ids) if eligible[seed].transit_ids else "ZZ"
        return chosen, c, seed_min_tid

    # Phase 1: cheap affinity-rank fill for every seed.
    phase1: List[Tuple[float, str, int, List[int]]] = []
    for seed in seeds:
        chosen, c, tid = _aff_fill(seed)
        phase1.append((c, tid, seed, chosen))
    phase1.sort()

    # Collect all candidates (phase-1 winner + phase-2 refinements) for ranking.
    # Each: (rounded_cost, tid, chosen). G4 may sample from top-K when perturbed.
    candidates: List[Tuple[float, str, List[int]]] = []
    if phase1:
        c, tid, _, chosen = phase1[0]
        candidates.append((round(c, 2), tid, chosen))

    for c1, _t1, seed, _chosen1 in phase1[:REFINE_TOP_SEEDS]:
        chosen, c, tid = _marginal_fill(seed)
        candidates.append((round(c, 2), tid, chosen))

    if not candidates:
        return []
    candidates.sort(key=lambda x: (x[0], x[1]))

    if construction_rng is not None and cfg.g4_perturbation_rank > 1 and len(candidates) > 1:
        topk = min(cfg.g4_perturbation_rank, len(candidates))
        return list(construction_rng.choice(candidates[:topk])[2])
    return list(candidates[0][2])


def _trolley_distinct_tids(totes: Sequence[ToteResult]) -> Set[str]:
    out: Set[str] = set()
    for t in totes:
        out.update(t.transit_ids)
    return out


def _trolley_zones(totes: Sequence[ToteResult]) -> Set[str]:
    return {t.zone for t in totes if t.zone}


def _tote_aisle_int_set(t: ToteResult) -> Set[int]:
    """Distinct aisle ints across a tote's items."""
    out: Set[int] = set()
    for it in t.items:
        a = aisle_int(it.aisle_location)
        if a > 0:
            out.add(a)
    return out


def _trolley_aisle_int_set(totes: Sequence[ToteResult]) -> Set[int]:
    out: Set[int] = set()
    for t in totes:
        out |= _tote_aisle_int_set(t)
    return out


def _aisle_jaccard(a: Set[int], b: Set[int]) -> float:
    if not a and not b:
        return 0.0
    u = a | b
    if not u:
        return 0.0
    return len(a & b) / len(u)


def _g9_cpsat_lns_partition(
    selected_trolleys: List[List[ToteResult]],
    cfg: StoreConfig,
    matrix: DistanceMatrix,
    items_by_order: Optional[Dict[str, List["Item"]]],
    enforce_cold_chain: bool,
    top_k_swaps: int,
    time_limit_s: float,
) -> Optional[Tuple[List[List[ToteResult]], float, int]]:
    """G9 LNS subproblem: optimally repartition the totes of K trolleys via CP-SAT.

    Generates candidate bins as (a) the originals and (b) single-tote-swap
    variants of each original where the swap-in tote comes from one of the
    OTHER selected trolleys. CP-SAT picks K bins such that each tote appears in
    exactly one bin and total trip_cost is minimised. Constraints (PAT<=3,
    size<=trolley_max_totes, single zone, cold-chain) are baked into the
    candidate filter, so the solver only sees feasible columns.

    Returns (new_partition, delta_cost, n_candidates) on strict improvement, or
    None otherwise. delta_cost is negative when accepted.
    """
    if not _HAVE_CPSAT or _cp_model is None:
        return None
    K = len(selected_trolleys)
    if K < 2:
        return None
    pat_n = cfg.pick_across_trucks
    max_size = cfg.trolley_max_totes
    cc_cap_s = cfg.cold_chain_cap_min * 60.0 if enforce_cold_chain else float("inf")

    # Collect the N totes that participate in this LNS subproblem.
    all_totes_flat: List[ToteResult] = [t for tr in selected_trolleys for t in tr]
    tote_ids = [t.tote_id for t in all_totes_flat]
    if len(set(tote_ids)) != len(tote_ids):
        return None  # duplicates shouldn't happen but guard anyway

    def _cost(tote_list: List[ToteResult]) -> float:
        items = [it for t in tote_list for it in t.items]
        if not items:
            return 0.0
        walk, uturns, _ = trip_cost(items, matrix, cfg)
        return walk + cfg.uturn_penalty_m * uturns

    def _feasible(tote_list: List[ToteResult]) -> bool:
        if not tote_list:
            return False
        if len(tote_list) > max_size:
            return False
        # PAT (correct residual via union, not set subtraction).
        tids: Set[str] = set()
        for t in tote_list:
            for it in t.items:
                if it.transit_id:
                    tids.add(it.transit_id)
        if len(tids) > pat_n:
            return False
        # Single zone.
        zones = {t.zone for t in tote_list if t.zone}
        if len(zones) > 1:
            return False
        # Cold chain.
        if enforce_cold_chain and items_by_order is not None:
            items = [it for t in tote_list for it in t.items]
            cc_s = cold_chain_compliance_time_s(items, items_by_order, matrix, cfg)
            if cc_s > cc_cap_s + 1e-3:
                return False
        return True

    # Build candidate pool: originals first (so warm-start hints map by index).
    candidates: List[List[ToteResult]] = []
    seen: Set[Tuple[str, ...]] = set()
    original_indices: List[int] = []
    for tr in selected_trolleys:
        key = tuple(sorted(t.tote_id for t in tr))
        if key in seen:
            # Two of the selected trolleys happen to be identical: degenerate; abort.
            return None
        seen.add(key)
        original_indices.append(len(candidates))
        candidates.append(list(tr))

    # Layer B: single-tote swap variants drawn from the K-selected totes only.
    for i_idx, tr in enumerate(selected_trolleys):
        if not tr:
            continue
        trolley_aisles = _trolley_aisle_int_set(tr)
        outsiders: List[Tuple[float, ToteResult]] = []
        for j_idx, other_tr in enumerate(selected_trolleys):
            if j_idx == i_idx:
                continue
            for ot in other_tr:
                ov = _aisle_jaccard(_tote_aisle_int_set(ot), trolley_aisles)
                outsiders.append((-ov, ot))
        outsiders.sort(key=lambda x: x[0])
        ranked_outsiders = [o[1] for o in outsiders[:top_k_swaps]]
        for pos in range(len(tr)):
            for cand_t in ranked_outsiders:
                variant = list(tr)
                variant[pos] = cand_t
                key = tuple(sorted(t.tote_id for t in variant))
                if key in seen:
                    continue
                if not _feasible(variant):
                    continue
                seen.add(key)
                candidates.append(variant)

    # Layer C (cheap): for any two originals, swap each position with the
    # opposite trolley's position (a 1-for-1 paired swap). Covers a different
    # neighbourhood than Layer B (forces a 2-move chain into one candidate set).
    # We already cover the marginal case via B; skip C for now to keep solve fast.

    n_cand = len(candidates)
    if n_cand <= K:
        return None  # nothing new generated

    # Cost cache.
    costs = [_cost(c) for c in candidates]
    old_cost_originals = sum(costs[i] for i in original_indices)

    # CP-SAT model.
    model = _cp_model.CpModel()
    x = [model.NewBoolVar(f"g9_x_{i}") for i in range(n_cand)]

    # Map tote_id -> candidate indices containing it.
    tote_to_cands: Dict[str, List[int]] = {tid: [] for tid in tote_ids}
    for i, c in enumerate(candidates):
        for t in c:
            if t.tote_id in tote_to_cands:
                tote_to_cands[t.tote_id].append(i)
    for tid, cand_idxs in tote_to_cands.items():
        if not cand_idxs:
            return None
        model.Add(sum(x[i] for i in cand_idxs) == 1)
    # Cardinality: must end up with exactly K bins (since each tote goes
    # exactly once and total totes = N = sum over K originals).
    model.Add(sum(x[i] for i in range(n_cand)) == K)

    int_costs = [int(round(c * 100)) for c in costs]
    model.Minimize(sum(int_costs[i] * x[i] for i in range(n_cand)))

    # Warm-start from the originals.
    orig_set = set(original_indices)
    for i in range(n_cand):
        model.AddHint(x[i], 1 if i in orig_set else 0)

    solver = _cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.num_search_workers = 1
    solver.parameters.log_search_progress = False
    status = solver.Solve(model)
    if status not in (_cp_model.OPTIMAL, _cp_model.FEASIBLE):
        return None

    selected_idx = [i for i in range(n_cand) if solver.Value(x[i]) == 1]
    new_cost = sum(costs[i] for i in selected_idx)
    delta = new_cost - old_cost_originals
    if delta >= -1e-6:
        return None
    return [candidates[i] for i in selected_idx], delta, n_cand


def _g1_enum_feasible_swaps(cur_i: List[ToteResult], cur_j: List[ToteResult],
                            tids_i_full: Set[str], tids_j_full: Set[str],
                            pat_n: int, top_k: int) -> List[Tuple[int, int, float]]:
    """G1: enumerate (ai, bj) feasible PAIRWISE swaps and rank by aisle-overlap pre-score.

    Pre-score (higher = better fit): how well does bj fit (i \\ ai) on aisles, plus
    how well does ai fit (j \\ bj). Pure aisle Jaccard, no trip_cost — that's evaluated
    afterward on the top_k shortlist by the SA loop.

    PAT feasibility checked here so the SA loop never sees infeasible candidates
    (eliminates the ~70% PAT-rejection waste that dominated random sampling).
    """
    out: List[Tuple[int, int, float]] = []
    aisles_per_tote_i = [_tote_aisle_int_set(t) for t in cur_i]
    aisles_per_tote_j = [_tote_aisle_int_set(t) for t in cur_j]
    tids_per_tote_i = [{it.transit_id for it in t.items if it.transit_id} for t in cur_i]
    tids_per_tote_j = [{it.transit_id for it in t.items if it.transit_id} for t in cur_j]
    for ai_idx in range(len(cur_i)):
        ai_tids = tids_per_tote_i[ai_idx]
        ai_aisles = aisles_per_tote_i[ai_idx]
        # Cheap rebuild of i \ ai TID/aisle sets (n is tiny, ~6).
        i_minus_ai_tids: Set[str] = set()
        i_minus_ai_aisles: Set[int] = set()
        for k in range(len(cur_i)):
            if k == ai_idx:
                continue
            i_minus_ai_tids |= tids_per_tote_i[k]
            i_minus_ai_aisles |= aisles_per_tote_i[k]
        for bj_idx in range(len(cur_j)):
            bj_tids = tids_per_tote_j[bj_idx]
            bj_aisles = aisles_per_tote_j[bj_idx]
            j_minus_bj_tids: Set[str] = set()
            j_minus_bj_aisles: Set[int] = set()
            for k in range(len(cur_j)):
                if k == bj_idx:
                    continue
                j_minus_bj_tids |= tids_per_tote_j[k]
                j_minus_bj_aisles |= aisles_per_tote_j[k]
            new_i_tids = i_minus_ai_tids | bj_tids
            new_j_tids = j_minus_bj_tids | ai_tids
            if len(new_i_tids) > pat_n or len(new_j_tids) > pat_n:
                continue
            score = (_aisle_jaccard(bj_aisles, i_minus_ai_aisles)
                     + _aisle_jaccard(ai_aisles, j_minus_bj_aisles))
            out.append((ai_idx, bj_idx, score))
    out.sort(key=lambda x: -x[2])
    return out[:top_k]


def _g2_enum_feasible_rotations(cur_i: List[ToteResult], cur_j: List[ToteResult],
                                cur_k: List[ToteResult], pat_n: int, top_k: int
                                ) -> List[Tuple[int, int, int, float]]:
    """G2: enumerate (ai, bj, ck) PAT-feasible 3-way rotations, rank by aisle Jaccard
    composition. Rotation: i: ai out, ck in. j: bj out, ai in. k: ck out, bj in.

    Returns top-K candidates (ai_idx, bj_idx, ck_idx, score) for trip_cost evaluation.
    PAT residuals computed correctly (iterating other totes), avoiding the
    set-subtraction under-count bug.
    """
    if not cur_i or not cur_j or not cur_k:
        return []
    tids_i_per = [{it.transit_id for it in t.items if it.transit_id} for t in cur_i]
    tids_j_per = [{it.transit_id for it in t.items if it.transit_id} for t in cur_j]
    tids_k_per = [{it.transit_id for it in t.items if it.transit_id} for t in cur_k]
    aisles_i_per = [_tote_aisle_int_set(t) for t in cur_i]
    aisles_j_per = [_tote_aisle_int_set(t) for t in cur_j]
    aisles_k_per = [_tote_aisle_int_set(t) for t in cur_k]
    out: List[Tuple[int, int, int, float]] = []
    for ai_idx in range(len(cur_i)):
        i_minus_tids: Set[str] = set()
        i_minus_aisles: Set[int] = set()
        for kk in range(len(cur_i)):
            if kk == ai_idx:
                continue
            i_minus_tids |= tids_i_per[kk]
            i_minus_aisles |= aisles_i_per[kk]
        ai_tids = tids_i_per[ai_idx]
        ai_aisles = aisles_i_per[ai_idx]
        for bj_idx in range(len(cur_j)):
            j_minus_tids: Set[str] = set()
            j_minus_aisles: Set[int] = set()
            for kk in range(len(cur_j)):
                if kk == bj_idx:
                    continue
                j_minus_tids |= tids_j_per[kk]
                j_minus_aisles |= aisles_j_per[kk]
            new_j_tids = j_minus_tids | ai_tids
            if len(new_j_tids) > pat_n:
                continue  # ai -> j infeasible regardless of ck
            bj_tids = tids_j_per[bj_idx]
            bj_aisles = aisles_j_per[bj_idx]
            for ck_idx in range(len(cur_k)):
                ck_tids = tids_k_per[ck_idx]
                new_i_tids = i_minus_tids | ck_tids
                if len(new_i_tids) > pat_n:
                    continue
                k_minus_tids: Set[str] = set()
                k_minus_aisles: Set[int] = set()
                for kk in range(len(cur_k)):
                    if kk == ck_idx:
                        continue
                    k_minus_tids |= tids_k_per[kk]
                    k_minus_aisles |= aisles_k_per[kk]
                new_k_tids = k_minus_tids | bj_tids
                if len(new_k_tids) > pat_n:
                    continue
                ck_aisles = aisles_k_per[ck_idx]
                sc = (_aisle_jaccard(ck_aisles, i_minus_aisles)
                      + _aisle_jaccard(ai_aisles, j_minus_aisles)
                      + _aisle_jaccard(bj_aisles, k_minus_aisles))
                out.append((ai_idx, bj_idx, ck_idx, sc))
    out.sort(key=lambda x: -x[3])
    return out[:top_k]


def _g3_worst_tote_idx(cur_i: List[ToteResult], score_i: float,
                       matrix: DistanceMatrix, cfg: StoreConfig) -> Tuple[int, float]:
    """G3 step 1: identify the tote in trolley i whose REMOVAL saves the most
    trip_cost. Returns (idx, savings). Computes len(cur_i) trip_cost calls.
    """
    if len(cur_i) <= 1:
        return -1, 0.0
    best_idx = -1
    best_save = -1.0
    for k in range(len(cur_i)):
        without = [t for idx, t in enumerate(cur_i) if idx != k]
        if not without:
            continue
        items = [it for t in without for it in t.items]
        d, u, _ = trip_cost(items, matrix, cfg)
        save = score_i - _path_cost(d, u, cfg)
        if save > best_save:
            best_save = save
            best_idx = k
    return best_idx, best_save


def _g5_best_split(cur_i: List[ToteResult], score_i: float,
                   matrix: DistanceMatrix, cfg: StoreConfig, pat_n: int
                   ) -> Optional[Tuple[List[ToteResult], List[ToteResult], float, float, float]]:
    """G5 split: try splitting trolley i into two trolleys at the aisle-centroid gap.

    Sort totes by mean std-aisle index; try every cut k in 1..n-1; check PAT/zone;
    score with full trip_cost; return best improving split or None.

    Returns (left, right, score_left, score_right, delta) where delta < 0.

    PAT/zone are guaranteed feasible per side (each is a subset of the original whole
    which already satisfied the caps), but checked defensively. Each side must have
    at least 1 tote — a 1+(n-1) split is allowed because that 1 tote may already span
    a costly cross-store traverse on its own; downstream relocate moves can still
    absorb it into another trolley if the lone tote is cheaper to co-locate.
    """
    n = len(cur_i)
    if n < 2:
        return None

    def _centroid(tote: ToteResult) -> float:
        aisles = []
        for it in tote.items:
            a = aisle_int(it.aisle_location)
            if a > 0:
                aisles.append(a)
        return sum(aisles) / len(aisles) if aisles else 0.0

    order = sorted(range(n), key=lambda idx: _centroid(cur_i[idx]))
    sorted_totes = [cur_i[idx] for idx in order]

    def _trip(totes_list: List[ToteResult]) -> float:
        items = [it for t in totes_list for it in t.items]
        d, u, _ = trip_cost(items, matrix, cfg)
        return _path_cost(d, u, cfg)

    best: Optional[Tuple[List[ToteResult], List[ToteResult], float, float, float]] = None
    for k in range(1, n):
        left = sorted_totes[:k]
        right = sorted_totes[k:]
        if (len(_trolley_distinct_tids(left)) > pat_n
                or len(_trolley_distinct_tids(right)) > pat_n):
            continue
        if len(_trolley_zones(left)) > 1 or len(_trolley_zones(right)) > 1:
            continue
        sl = _trip(left)
        sr = _trip(right)
        delta = (sl + sr) - score_i
        if delta < 0 and (best is None or delta < best[4]):
            best = (left, right, sl, sr, delta)
    return best


def _sa_refine_trolleys_spt(trolley_tote_lists: List[List[ToteResult]], cfg: StoreConfig,
                            matrix: DistanceMatrix, seed: int,
                            items_by_order: Optional[Dict[str, List[Item]]] = None,
                            enforce_cold_chain: bool = False) -> List[List[ToteResult]]:
    """Cross-trolley refinement scored by full SPT trip_cost (steepest descent).

    Move portfolio (T1 levers + legacy):
       - 30% G1 SMART pairwise swap (top-K feasible (ai, bj) ranked by aisle Jaccard,
         trip_cost evaluated on shortlist; eliminates ~70% PAT-rejection waste of
         random sampling)
       - 15% G2 3-way rotation (a -> b -> c -> a across trolleys i, j, k)
       - 15% G3 worst-tote targeted move (identify highest-cost-contributing tote in
         trolley i; relocate to underfilled trolley OR directed swap with top-K
         target trolleys ranked by aisle overlap)
       - 10% legacy 1-tote relocate (kept for underfilled-trolley cases)
       - 15% legacy 2-tote BLOCK-SWAP
       - 15% legacy 3-tote BLOCK-SWAP

    Each lever can be turned off via cfg.enable_sa_g1/g2/g3 — when off, that share of
    the dial routes to legacy random pairwise swap.

    Trolley pair (i, j) sampling: 70% TID-overlap-biased / 30% uniform.

    Acceptance: steepest-descent (delta < 0 only). Empirically validated 2026-05-10.

    C1 (2026-05-11): when `enforce_cold_chain=True` and `items_by_order` is supplied,
    any move that would push a recipient trolley's cold-chain compliance time above
    `cfg.cold_chain_cap_min` is rejected before acceptance. Mirrors the composition-
    time guard in the picker, so SA cannot re-introduce a cc-breach.
    """
    rng = random.Random(seed)
    cur = [list(t) for t in trolley_tote_lists]
    if not cur:
        return cur

    cc_cap_s = cfg.cold_chain_cap_min * 60.0 if enforce_cold_chain else float("inf")
    _need_cc = enforce_cold_chain and items_by_order is not None

    def trolley_cost(tote_list: List[ToteResult]) -> float:
        items = [it for t in tote_list for it in t.items]
        pc, _ct = trolley_path_cost_and_cc_time(items, items_by_order, matrix, cfg,
                                                 need_cc=False)
        return pc

    def _cc_ok(tote_list: List[ToteResult]) -> bool:
        """C1 guard: would this trolley's items still satisfy HC12 cold-chain compliance?"""
        if not _need_cc or not tote_list:
            return True
        items = [it for t in tote_list for it in t.items]
        _pc, ct = trolley_path_cost_and_cc_time(items, items_by_order, matrix, cfg,
                                                 need_cc=True)
        return ct <= cc_cap_s + 1e-3

    scores = [trolley_cost(t) for t in cur]
    cur_total = sum(scores)
    initial_total = cur_total
    best = [list(t) for t in cur]
    best_total = cur_total

    iters = max(200, cfg.sa_iterations)
    pat_n = cfg.pick_across_trucks
    max_size = cfg.trolley_max_totes

    tids_per_trolley: List[Set[str]] = [_trolley_distinct_tids(t) for t in cur]
    aisles_per_trolley: List[Set[int]] = [_trolley_aisle_int_set(t) for t in cur]

    # Diagnostic counters
    n_total = 0
    n_pat_reject = 0
    n_size_reject = 0
    n_zone_reject = 0
    n_cc_reject = 0
    n_improving = 0
    n_g1 = n_g1_acc = 0
    n_g2 = n_g2_acc = 0
    n_g3 = n_g3_acc = 0
    n_g5 = n_g5_acc = 0
    n_g6 = n_g6_acc = 0
    n_g8 = n_g8_acc = 0
    n_g9 = n_g9_acc = 0
    n_legacy = n_legacy_acc = 0

    def _pick_pair() -> Tuple[int, int]:
        if rng.random() < 0.7 and len(cur) >= 2:
            i = rng.randrange(len(cur))
            best_overlap = -1
            j_candidates: List[int] = []
            for k in range(len(cur)):
                if k == i:
                    continue
                ov = len(tids_per_trolley[i] & tids_per_trolley[k])
                if ov > best_overlap:
                    best_overlap = ov
                    j_candidates = [k]
                elif ov == best_overlap:
                    j_candidates.append(k)
            j = rng.choice(j_candidates) if j_candidates else (0 if i != 0 else 1)
            return i, j
        return tuple(rng.sample(range(len(cur)), 2))  # type: ignore[return-value]

    def _pick_triple_for_g2() -> Optional[Tuple[int, int, int]]:
        """Pick three distinct trolleys (i, j, k) biased toward TID-overlapping triples."""
        if len(cur) < 3:
            return None
        if rng.random() < 0.7:
            i = rng.randrange(len(cur))
            others = [m for m in range(len(cur)) if m != i]
            others.sort(key=lambda m: -len(tids_per_trolley[i] & tids_per_trolley[m]))
            top = others[:max(2, min(6, len(others)))]
            if len(top) < 2:
                return None
            j, k = rng.sample(top, 2)
            return i, j, k
        s = rng.sample(range(len(cur)), 3)
        return s[0], s[1], s[2]

    def _accept(i_idx: int, j_idx: int, new_i: List[ToteResult], new_j: List[ToteResult],
                new_si: float, new_sj: float, delta: float) -> None:
        nonlocal cur_total, best, best_total
        cur[i_idx] = new_i
        cur[j_idx] = new_j
        scores[i_idx] = new_si
        scores[j_idx] = new_sj
        tids_per_trolley[i_idx] = _trolley_distinct_tids(new_i)
        tids_per_trolley[j_idx] = _trolley_distinct_tids(new_j)
        aisles_per_trolley[i_idx] = _trolley_aisle_int_set(new_i)
        aisles_per_trolley[j_idx] = _trolley_aisle_int_set(new_j)
        cur_total += delta
        if cur_total < best_total:
            best = [list(t) for t in cur]
            best_total = cur_total

    for _ in range(iters):
        n_total += 1
        if len(cur) < 2:
            break
        # Move-type roulette. Lever shares re-route to legacy random pwise when off.
        r = rng.random()
        share_g1 = 0.30 if cfg.enable_sa_g1_smart_swap else 0.0
        share_g2 = 0.15 if (cfg.enable_sa_g2_three_way and len(cur) >= 3) else 0.0
        share_g3 = 0.15 if cfg.enable_sa_g3_worst_tote else 0.0
        share_g5 = 0.05 if cfg.enable_sa_g5_split else 0.0
        # G6 (15%) is carved from legacy_pwise (which had 10%) plus 5% from
        # relocate. Legacy random pwise has ~0% acceptance once G1 covers smart
        # swaps; relocate covered single-tote moves but only against a RANDOM
        # target (not top-K aisle-Jaccard ranked).
        share_g6 = 0.15 if cfg.enable_sa_g6_random_relocate else 0.0
        share_g8 = 0.10 if cfg.enable_sa_g8_lk_chain else 0.0
        # G9 (5%) carved from share_3block: CP-SAT LNS over K random trolleys.
        share_g9 = 0.05 if (cfg.enable_sa_g9_cpsat_lns and _HAVE_CPSAT
                             and len(cur) >= cfg.sa_g9_lns_k) else 0.0
        share_relocate = 0.05 if cfg.enable_sa_g6_random_relocate else 0.10
        share_2block = 0.15 if not cfg.enable_sa_g5_split else 0.10
        share_3block = (0.10 if not cfg.enable_sa_g5_split else 0.05) if share_g9 > 0.0 \
                       else (0.15 if not cfg.enable_sa_g5_split else 0.10)
        # share_legacy_pwise = remainder
        cum = 0.0
        cum += share_g1
        if r < cum:
            move = "g1"
        else:
            cum += share_g2
            if r < cum:
                move = "g2"
            else:
                cum += share_g3
                if r < cum:
                    move = "g3"
                else:
                    cum += share_g5
                    if r < cum:
                        move = "g5"
                    else:
                        cum += share_g6
                        if r < cum:
                            move = "g6"
                        else:
                            cum += share_g8
                            if r < cum:
                                move = "g8"
                            else:
                                cum += share_g9
                                if r < cum:
                                    move = "g9"
                                else:
                                    cum += share_relocate
                                    if r < cum:
                                        move = "relocate"
                                    else:
                                        cum += share_2block
                                        if r < cum:
                                            move = "block2"
                                        else:
                                            cum += share_3block
                                            if r < cum:
                                                move = "block3"
                                            else:
                                                move = "legacy_pwise"

        # ---------------- G1: smart pairwise swap ----------------
        if move == "g1":
            i, j = _pick_pair()
            if not cur[i] or not cur[j]:
                continue
            n_g1 += 1
            cands = _g1_enum_feasible_swaps(
                cur[i], cur[j], tids_per_trolley[i], tids_per_trolley[j],
                pat_n, cfg.sa_g1_top_k)
            if not cands:
                n_pat_reject += 1
                continue
            best_move = None
            for ai_idx, bj_idx, _sc in cands:
                new_i = list(cur[i]); new_j = list(cur[j])
                new_i[ai_idx], new_j[bj_idx] = new_j[bj_idx], new_i[ai_idx]
                if len(_trolley_zones(new_i)) > 1 or len(_trolley_zones(new_j)) > 1:
                    n_zone_reject += 1
                    continue
                if not (_cc_ok(new_i) and _cc_ok(new_j)):
                    n_cc_reject += 1
                    continue
                new_si = trolley_cost(new_i)
                new_sj = trolley_cost(new_j)
                delta = (new_si + new_sj) - (scores[i] + scores[j])
                if delta < 0 and (best_move is None or delta < best_move[4]):
                    best_move = (new_i, new_j, new_si, new_sj, delta)
            if best_move is not None:
                ni, nj, nsi, nsj, dlt = best_move
                _accept(i, j, ni, nj, nsi, nsj, dlt)
                n_improving += 1
                n_g1_acc += 1
            continue

        # ---------------- G2: 3-way rotation a -> b -> c -> a ----------------
        if move == "g2":
            triple = _pick_triple_for_g2()
            if triple is None:
                continue
            i, j, k = triple
            if not cur[i] or not cur[j] or not cur[k]:
                continue
            n_g2 += 1
            cands = _g2_enum_feasible_rotations(cur[i], cur[j], cur[k], pat_n,
                                                cfg.sa_g2_top_k)
            if not cands:
                n_pat_reject += 1
                continue
            best_move = None
            for ai_idx, bj_idx, ck_idx, _sc in cands:
                # Rotation: i: ai out, ck in. j: bj out, ai in. k: ck out, bj in.
                new_i = [t for idx, t in enumerate(cur[i]) if idx != ai_idx] + [cur[k][ck_idx]]
                new_j = [t for idx, t in enumerate(cur[j]) if idx != bj_idx] + [cur[i][ai_idx]]
                new_k = [t for idx, t in enumerate(cur[k]) if idx != ck_idx] + [cur[j][bj_idx]]
                if (len(new_i) > max_size or len(new_j) > max_size or len(new_k) > max_size):
                    n_size_reject += 1
                    continue
                if (len(_trolley_zones(new_i)) > 1 or len(_trolley_zones(new_j)) > 1
                        or len(_trolley_zones(new_k)) > 1):
                    n_zone_reject += 1
                    continue
                if not (_cc_ok(new_i) and _cc_ok(new_j) and _cc_ok(new_k)):
                    n_cc_reject += 1
                    continue
                new_si = trolley_cost(new_i)
                new_sj = trolley_cost(new_j)
                new_sk = trolley_cost(new_k)
                delta = ((new_si + new_sj + new_sk)
                         - (scores[i] + scores[j] + scores[k]))
                if delta < 0 and (best_move is None or delta < best_move[6]):
                    best_move = (new_i, new_j, new_k, new_si, new_sj, new_sk, delta)
            if best_move is not None:
                new_i, new_j, new_k, new_si, new_sj, new_sk, dlt = best_move
                cur[i], cur[j], cur[k] = new_i, new_j, new_k
                scores[i], scores[j], scores[k] = new_si, new_sj, new_sk
                tids_per_trolley[i] = _trolley_distinct_tids(new_i)
                tids_per_trolley[j] = _trolley_distinct_tids(new_j)
                tids_per_trolley[k] = _trolley_distinct_tids(new_k)
                aisles_per_trolley[i] = _trolley_aisle_int_set(new_i)
                aisles_per_trolley[j] = _trolley_aisle_int_set(new_j)
                aisles_per_trolley[k] = _trolley_aisle_int_set(new_k)
                cur_total += dlt
                if cur_total < best_total:
                    best = [list(t) for t in cur]
                    best_total = cur_total
                n_improving += 1
                n_g2_acc += 1
            continue

        # ---------------- G3: worst-tote targeted move ----------------
        if move == "g3":
            i = rng.randrange(len(cur))
            if len(cur[i]) < 2:
                continue
            n_g3 += 1
            worst_idx, _save = _g3_worst_tote_idx(cur[i], scores[i], matrix, cfg)
            if worst_idx < 0:
                continue
            worst_tote = cur[i][worst_idx]
            worst_tote_aisles = _tote_aisle_int_set(worst_tote)
            worst_tote_tids = {it.transit_id for it in worst_tote.items if it.transit_id}
            # Rank target trolleys by aisle overlap with worst tote.
            target_ranked: List[Tuple[float, int]] = []
            for j in range(len(cur)):
                if j == i:
                    continue
                if cur[j] and len(_trolley_zones(cur[j])) == 1:
                    z_i = next(iter(_trolley_zones(cur[i])), None)
                    z_j = next(iter(_trolley_zones(cur[j])), None)
                    if z_i and z_j and z_i != z_j:
                        continue
                ov = _aisle_jaccard(worst_tote_aisles, aisles_per_trolley[j])
                target_ranked.append((-ov, j))
            target_ranked.sort()
            best_move = None
            for _ov, j in target_ranked[:cfg.sa_g3_top_k_targets]:
                # Mode A: relocate worst -> j if room.
                if len(cur[j]) < max_size:
                    new_i = [t for idx, t in enumerate(cur[i]) if idx != worst_idx]
                    new_j = list(cur[j]) + [worst_tote]
                    new_j_tids = tids_per_trolley[j] | worst_tote_tids
                    if len(new_j_tids) > pat_n:
                        pass
                    elif (len(_trolley_zones(new_i)) > 1
                          or len(_trolley_zones(new_j)) > 1):
                        pass
                    elif not (_cc_ok(new_i) and _cc_ok(new_j)):
                        n_cc_reject += 1
                    else:
                        new_si = trolley_cost(new_i) if new_i else 0.0
                        new_sj = trolley_cost(new_j)
                        delta = (new_si + new_sj) - (scores[i] + scores[j])
                        if delta < 0 and (best_move is None or delta < best_move[5]):
                            best_move = (j, new_i, new_j, new_si, new_sj, delta)
                # Mode B: directed swap — worst out, top-K bj in j by aisle Jaccard.
                # Compute correct residual TID set for trolley i after removing worst_tote
                # (subtraction of worst_tote_tids from tids_per_trolley[i] under-counts when
                # a TID is shared across multiple totes — that's the PAT-breach bug).
                i_minus_worst_tids: Set[str] = set()
                for kk_idx, tt in enumerate(cur[i]):
                    if kk_idx == worst_idx:
                        continue
                    for it in tt.items:
                        if it.transit_id:
                            i_minus_worst_tids.add(it.transit_id)
                cand_bj: List[Tuple[float, int]] = []
                for bj_idx, bj_tote in enumerate(cur[j]):
                    bj_aisles = _tote_aisle_int_set(bj_tote)
                    bj_tids = {it.transit_id for it in bj_tote.items if it.transit_id}
                    new_i_tids = i_minus_worst_tids | bj_tids
                    if len(new_i_tids) > pat_n:
                        continue
                    j_minus_bj_tids: Set[str] = set()
                    for kk_idx, tt in enumerate(cur[j]):
                        if kk_idx == bj_idx:
                            continue
                        for it in tt.items:
                            if it.transit_id:
                                j_minus_bj_tids.add(it.transit_id)
                    new_j_tids = j_minus_bj_tids | worst_tote_tids
                    if len(new_j_tids) > pat_n:
                        continue
                    # Score: bj fits i (minus worst), worst fits j (minus bj). Aisle Jaccard.
                    i_minus = aisles_per_trolley[i] - worst_tote_aisles
                    j_minus = aisles_per_trolley[j] - bj_aisles
                    sc = _aisle_jaccard(bj_aisles, i_minus) + _aisle_jaccard(worst_tote_aisles, j_minus)
                    cand_bj.append((-sc, bj_idx))
                cand_bj.sort()
                for _s, bj_idx in cand_bj[:cfg.sa_g3_swap_per_target]:
                    new_i = list(cur[i]); new_j = list(cur[j])
                    new_i[worst_idx], new_j[bj_idx] = new_j[bj_idx], new_i[worst_idx]
                    if (len(_trolley_zones(new_i)) > 1
                            or len(_trolley_zones(new_j)) > 1):
                        continue
                    if not (_cc_ok(new_i) and _cc_ok(new_j)):
                        n_cc_reject += 1
                        continue
                    new_si = trolley_cost(new_i)
                    new_sj = trolley_cost(new_j)
                    delta = (new_si + new_sj) - (scores[i] + scores[j])
                    if delta < 0 and (best_move is None or delta < best_move[5]):
                        best_move = (j, new_i, new_j, new_si, new_sj, delta)
            if best_move is not None:
                j, ni, nj, nsi, nsj, dlt = best_move
                _accept(i, j, ni, nj, nsi, nsj, dlt)
                n_improving += 1
                n_g3_acc += 1
            continue

        # ---------------- G6: random-tote relocate to top-K aisle-Jaccard target ----------------
        # For a RANDOM tote in random trolley i (not just the worst), evaluate the top-K
        # target trolleys j ranked by aisle Jaccard with that tote. Accept the best
        # PAT/zone/size/cc-feasible relocate with delta < 0. Internalises v4 reassign's
        # single-tote-relocate signal into SA.
        if move == "g6":
            i = rng.randrange(len(cur))
            if len(cur[i]) < 2:
                continue
            n_g6 += 1
            t_idx = rng.randrange(len(cur[i]))
            t_tote = cur[i][t_idx]
            t_aisles = _tote_aisle_int_set(t_tote)
            t_tids = {it.transit_id for it in t_tote.items if it.transit_id}
            # Residual TID set in trolley i after removing this tote.
            i_residual_tids: Set[str] = set()
            for kk_idx, tt in enumerate(cur[i]):
                if kk_idx == t_idx:
                    continue
                for it in tt.items:
                    if it.transit_id:
                        i_residual_tids.add(it.transit_id)
            # Rank target trolleys by aisle Jaccard with the relocated tote.
            target_ranked: List[Tuple[float, int]] = []
            for j in range(len(cur)):
                if j == i:
                    continue
                if cur[j] and len(_trolley_zones(cur[j])) == 1:
                    z_i = next(iter(_trolley_zones(cur[i])), None)
                    z_j = next(iter(_trolley_zones(cur[j])), None)
                    if z_i and z_j and z_i != z_j:
                        continue
                if len(cur[j]) >= max_size:
                    continue
                # PAT pre-filter.
                if len(tids_per_trolley[j] | t_tids) > pat_n:
                    n_pat_reject += 1
                    continue
                ov = _aisle_jaccard(t_aisles, aisles_per_trolley[j])
                target_ranked.append((-ov, j))
            target_ranked.sort()
            best_move = None
            for _ov, j in target_ranked[:cfg.sa_g6_top_k_targets]:
                new_i = [t for idx, t in enumerate(cur[i]) if idx != t_idx]
                new_j = list(cur[j]) + [t_tote]
                if len(_trolley_zones(new_i)) > 1 or len(_trolley_zones(new_j)) > 1:
                    n_zone_reject += 1
                    continue
                if not (_cc_ok(new_i) and _cc_ok(new_j)):
                    n_cc_reject += 1
                    continue
                new_si = trolley_cost(new_i) if new_i else 0.0
                new_sj = trolley_cost(new_j)
                delta = (new_si + new_sj) - (scores[i] + scores[j])
                if delta < 0 and (best_move is None or delta < best_move[5]):
                    best_move = (j, new_i, new_j, new_si, new_sj, delta)
            if best_move is not None:
                j, ni, nj, nsi, nsj, dlt = best_move
                _accept(i, j, ni, nj, nsi, nsj, dlt)
                n_improving += 1
                n_g6_acc += 1
            continue

        # ---------------- G8: 2-tote LK chain (A: X->Y, B: Y->Z, Z != X) ----------------
        # Open displacement chain not covered by relocate / block-swap / 3-way rotation.
        # Pick a random tote A in random trolley X; rank top-K Y trolleys by aisle
        # Jaccard with A. For each candidate Y: pick the tote B in Y whose aisle
        # Jaccard with A is LOWEST (most likely to want a different home); rank Z
        # trolleys (Z != X, Z != Y) by aisle Jaccard with B; evaluate each chain
        # at trip_cost. Accept the best feasible delta < 0 chain.
        if move == "g8":
            if len(cur) < 3:
                continue
            x_idx = rng.randrange(len(cur))
            if not cur[x_idx]:
                continue
            n_g8 += 1
            a_idx = rng.randrange(len(cur[x_idx]))
            a_tote = cur[x_idx][a_idx]
            a_aisles = _tote_aisle_int_set(a_tote)
            a_tids = {it.transit_id for it in a_tote.items if it.transit_id}
            # Residual TID set of X after removing A.
            x_residual_tids: Set[str] = set()
            for kk_idx, tt in enumerate(cur[x_idx]):
                if kk_idx == a_idx:
                    continue
                for it in tt.items:
                    if it.transit_id:
                        x_residual_tids.add(it.transit_id)
            # Rank Y candidates by aisle Jaccard with A.
            y_ranked: List[Tuple[float, int]] = []
            for y in range(len(cur)):
                if y == x_idx or not cur[y]:
                    continue
                if len(cur[y]) == 0:
                    continue
                # Zone check.
                if cur[y] and len(_trolley_zones(cur[y])) == 1:
                    z_x = next(iter(_trolley_zones(cur[x_idx])), None)
                    z_y = next(iter(_trolley_zones(cur[y])), None)
                    if z_x and z_y and z_x != z_y:
                        continue
                ov = _aisle_jaccard(a_aisles, aisles_per_trolley[y])
                y_ranked.append((-ov, y))
            y_ranked.sort()
            best_chain = None
            for _ov_y, y in y_ranked[:cfg.sa_g8_top_k_y]:
                # Pick B in Y: tote whose aisle Jaccard with A is LOWEST (least similar
                # to A, so least valuable to keep adjacent to A).
                if len(cur[y]) == 0:
                    continue
                b_ranked: List[Tuple[float, int]] = []
                for b_i, b_tote in enumerate(cur[y]):
                    b_aisles = _tote_aisle_int_set(b_tote)
                    sc = _aisle_jaccard(b_aisles, a_aisles)
                    b_ranked.append((sc, b_i))
                b_ranked.sort()
                # Take top-2 displacement candidates per Y to keep chain count tight.
                for _sc, b_idx in b_ranked[:2]:
                    b_tote = cur[y][b_idx]
                    b_aisles = _tote_aisle_int_set(b_tote)
                    b_tids = {it.transit_id for it in b_tote.items if it.transit_id}
                    # Compute Y's residual TID set after B leaves.
                    y_residual_tids: Set[str] = set()
                    for kk_idx, tt in enumerate(cur[y]):
                        if kk_idx == b_idx:
                            continue
                        for it in tt.items:
                            if it.transit_id:
                                y_residual_tids.add(it.transit_id)
                    new_y_tids = y_residual_tids | a_tids
                    if len(new_y_tids) > pat_n:
                        continue
                    # Rank Z trolleys for B (Z != X, Z != Y).
                    z_ranked: List[Tuple[float, int]] = []
                    for z in range(len(cur)):
                        if z == x_idx or z == y or not cur[z]:
                            continue
                        if len(cur[z]) >= max_size:
                            continue
                        if cur[z] and len(_trolley_zones(cur[z])) == 1:
                            z_y = next(iter(_trolley_zones(cur[y])), None)
                            z_z = next(iter(_trolley_zones(cur[z])), None)
                            if z_y and z_z and z_y != z_z:
                                continue
                        new_z_tids = tids_per_trolley[z] | b_tids
                        if len(new_z_tids) > pat_n:
                            continue
                        ov = _aisle_jaccard(b_aisles, aisles_per_trolley[z])
                        z_ranked.append((-ov, z))
                    z_ranked.sort()
                    for _ov_z, z in z_ranked[:cfg.sa_g8_top_k_z]:
                        # Build new trolley states.
                        new_x = [t for idx, t in enumerate(cur[x_idx]) if idx != a_idx]
                        new_y = [t for idx, t in enumerate(cur[y]) if idx != b_idx] + [a_tote]
                        new_z = list(cur[z]) + [b_tote]
                        if (len(new_x) > max_size or len(new_y) > max_size
                                or len(new_z) > max_size):
                            continue
                        if (len(_trolley_zones(new_x)) > 1
                                or len(_trolley_zones(new_y)) > 1
                                or len(_trolley_zones(new_z)) > 1):
                            continue
                        if not (_cc_ok(new_x) and _cc_ok(new_y) and _cc_ok(new_z)):
                            n_cc_reject += 1
                            continue
                        new_sx = trolley_cost(new_x) if new_x else 0.0
                        new_sy = trolley_cost(new_y)
                        new_sz = trolley_cost(new_z)
                        delta = ((new_sx + new_sy + new_sz)
                                 - (scores[x_idx] + scores[y] + scores[z]))
                        if delta < 0 and (best_chain is None or delta < best_chain[7]):
                            best_chain = (y, z, new_x, new_y, new_z,
                                          new_sx, new_sy, delta, new_sz)
            if best_chain is not None:
                y, z, new_x, new_y, new_z, new_sx, new_sy, dlt, new_sz = best_chain
                cur[x_idx], cur[y], cur[z] = new_x, new_y, new_z
                scores[x_idx], scores[y], scores[z] = new_sx, new_sy, new_sz
                tids_per_trolley[x_idx] = _trolley_distinct_tids(new_x)
                tids_per_trolley[y] = _trolley_distinct_tids(new_y)
                tids_per_trolley[z] = _trolley_distinct_tids(new_z)
                aisles_per_trolley[x_idx] = _trolley_aisle_int_set(new_x)
                aisles_per_trolley[y] = _trolley_aisle_int_set(new_y)
                aisles_per_trolley[z] = _trolley_aisle_int_set(new_z)
                cur_total += dlt
                if cur_total < best_total:
                    best = [list(t) for t in cur]
                    best_total = cur_total
                n_improving += 1
                n_g8_acc += 1
            continue

        # ---------------- G9: CP-SAT LNS over K random trolleys ----------------
        if move == "g9":
            K = cfg.sa_g9_lns_k
            if len(cur) < K:
                continue
            n_g9 += 1
            # Pick K distinct trolley indices: anchor on a random i, then take
            # the K-1 highest TID-overlap partners (consistent with _pick_pair
            # / G2 triple heuristic).
            if rng.random() < 0.7:
                anchor = rng.randrange(len(cur))
                others = [m for m in range(len(cur)) if m != anchor]
                others.sort(key=lambda m: -len(tids_per_trolley[anchor] & tids_per_trolley[m]))
                top_pool = others[: max(K - 1, min(2 * (K - 1), len(others)))]
                if len(top_pool) < K - 1:
                    continue
                picked = [anchor] + rng.sample(top_pool, K - 1)
            else:
                picked = rng.sample(range(len(cur)), K)
            # Skip if any selected trolley is empty.
            if any(not cur[idx] for idx in picked):
                continue
            selected = [cur[idx] for idx in picked]
            res = _g9_cpsat_lns_partition(
                selected, cfg, matrix, items_by_order, _need_cc,
                cfg.sa_g9_top_k_swaps, cfg.sa_g9_time_limit_s,
            )
            if res is None:
                continue
            new_parts, dlt, _n_cand = res
            if len(new_parts) != K:
                continue
            # Apply: each picked index gets the corresponding new partition.
            for slot, idx in enumerate(picked):
                new_tr = new_parts[slot]
                cur[idx] = new_tr
                scores[idx] = trolley_cost(new_tr)
                tids_per_trolley[idx] = _trolley_distinct_tids(new_tr)
                aisles_per_trolley[idx] = _trolley_aisle_int_set(new_tr)
            cur_total += dlt
            if cur_total < best_total:
                best = [list(t) for t in cur]
                best_total = cur_total
            n_improving += 1
            n_g9_acc += 1
            continue

        # ---------------- G5: split trolley i into two trolleys ----------------
        if move == "g5":
            i = rng.randrange(len(cur))
            if len(cur[i]) < 2:
                continue
            n_g5 += 1
            split = _g5_best_split(cur[i], scores[i], matrix, cfg, pat_n)
            if split is None:
                continue
            left, right, sl, sr, dlt = split
            if not (_cc_ok(left) and _cc_ok(right)):
                n_cc_reject += 1
                continue
            cur[i] = left
            scores[i] = sl
            tids_per_trolley[i] = _trolley_distinct_tids(left)
            aisles_per_trolley[i] = _trolley_aisle_int_set(left)
            cur.append(right)
            scores.append(sr)
            tids_per_trolley.append(_trolley_distinct_tids(right))
            aisles_per_trolley.append(_trolley_aisle_int_set(right))
            cur_total += dlt
            if cur_total < best_total:
                best = [list(t) for t in cur]
                best_total = cur_total
            n_improving += 1
            n_g5_acc += 1
            continue

        # ---------------- Legacy moves ----------------
        i, j = _pick_pair()
        if not cur[i] or not cur[j]:
            continue
        n_legacy += 1
        if move == "relocate":
            if len(cur[i]) <= 1 or len(cur[j]) >= max_size:
                continue
            ai = rng.randrange(len(cur[i]))
            new_i = list(cur[i]); moved = new_i.pop(ai)
            new_j = list(cur[j]) + [moved]
        elif move == "block2" or move == "block3":
            k_block = 2 if move == "block2" else 3
            if len(cur[i]) < k_block or len(cur[j]) < k_block:
                continue
            i_pick = rng.sample(range(len(cur[i])), k_block)
            j_pick = rng.sample(range(len(cur[j])), k_block)
            i_set = set(i_pick); j_set = set(j_pick)
            i_kept = [t for idx, t in enumerate(cur[i]) if idx not in i_set]
            j_kept = [t for idx, t in enumerate(cur[j]) if idx not in j_set]
            i_moved = [cur[i][idx] for idx in i_pick]
            j_moved = [cur[j][idx] for idx in j_pick]
            new_i = i_kept + j_moved
            new_j = j_kept + i_moved
        else:  # legacy_pwise (random pairwise)
            ai = rng.randrange(len(cur[i]))
            bj = rng.randrange(len(cur[j]))
            new_i = list(cur[i]); new_j = list(cur[j])
            new_i[ai], new_j[bj] = new_j[bj], new_i[ai]
        if len(new_i) > max_size or len(new_j) > max_size:
            n_size_reject += 1
            continue
        if len(_trolley_zones(new_i)) > 1 or len(_trolley_zones(new_j)) > 1:
            n_zone_reject += 1
            continue
        new_i_tids = _trolley_distinct_tids(new_i)
        new_j_tids = _trolley_distinct_tids(new_j)
        if len(new_i_tids) > pat_n or len(new_j_tids) > pat_n:
            n_pat_reject += 1
            continue
        if not (_cc_ok(new_i) and _cc_ok(new_j)):
            n_cc_reject += 1
            continue
        new_si = trolley_cost(new_i)
        new_sj = trolley_cost(new_j)
        delta = (new_si + new_sj) - (scores[i] + scores[j])
        if delta < 0:
            _accept(i, j, new_i, new_j, new_si, new_sj, delta)
            n_improving += 1
            n_legacy_acc += 1

    print(f"          [SA seed={seed}] iters={n_total} pat_rej={n_pat_reject} "
          f"size_rej={n_size_reject} zone_rej={n_zone_reject} cc_rej={n_cc_reject} "
          f"improving={n_improving} "
          f"[G1 {n_g1_acc}/{n_g1} G2 {n_g2_acc}/{n_g2} G3 {n_g3_acc}/{n_g3} "
          f"G5 {n_g5_acc}/{n_g5} G6 {n_g6_acc}/{n_g6} G8 {n_g8_acc}/{n_g8} "
          f"G9 {n_g9_acc}/{n_g9} leg {n_legacy_acc}/{n_legacy}] "
          f"best_delta={best_total - initial_total:+.1f}", flush=True)

    # Safety net: assert PAT cap is honoured by the returned configuration.
    # If a future bug allows a breach to slip past the per-move check, fail loudly
    # rather than silently shipping an infeasible plan.
    for t_idx, t_list in enumerate(best):
        actual = _trolley_distinct_tids(t_list)
        if len(actual) > pat_n:
            raise AssertionError(
                f"[SA seed={seed}] trolley {t_idx} breaches PAT cap: "
                f"{len(actual)} distinct TIDs > {pat_n} (TIDs: {sorted(actual)})"
            )

    return [b for b in best if b]


def build_trolleys_rolling_pat_for_zone(totes: List[ToteResult], zone: str, cfg: StoreConfig,
                                        matrix: DistanceMatrix,
                                        items_by_order: Dict[str, List[Item]]
                                        ) -> List[TrolleyResult]:
    """Per-trolley rolling PAT.

    At each step:
      1. Compute window = first N distinct ANCHOR TransitIDs with remaining totes (anchor =
         earliest TransitID inside a tote; for non-Frozen totes anchor == only TransitID).
         When BC is depleted (no remaining tote anchored to BC), window slides to e.g.
         [BB, BD, BF] without revisiting BC.
      2. Eligibility: a tote is eligible if its anchor is in window AND its full TransitID set
         is a subset of (window plus any TransitID already permitted by a Frozen tote merge).
      3. Build ONE trolley from that eligible subset (greedy, capped at trolley_max_totes and
         at <= N distinct TransitIDs across the trolley).
      4. Remove those totes from the pool. Repeat.

    A single trolley may legitimately mix TransitIDs from non-contiguous trucks in alpha order
    (e.g. BB+BD+BF) if intervening trucks were already depleted when the trolley was built.
    """
    if not totes:
        return []
    use_affinity = zone in cfg.affinity_zones

    def _construct_phase1(construction_rng: Optional[random.Random]
                          ) -> List[Tuple[List[str], List[ToteResult]]]:
        """Run Phase 1 greedy: pick one trolley at a time from remaining pool, until empty.
        construction_rng=None: fully deterministic best-pick (matches pre-G4 behaviour).
        construction_rng set: each pick is sampled uniformly from top-K Phase 2 candidates.
        """
        pool: List[ToteResult] = list(totes)
        groups: List[Tuple[List[str], List[ToteResult]]] = []
        while pool:
            if use_affinity:
                distinct_anchors = sorted({_tote_anchor_transit(t) for t in pool
                                           if _tote_anchor_transit(t)})
                seed_window = set(distinct_anchors[:cfg.pick_across_trucks])
                eligible_idx = list(range(len(pool)))
                seed_idx_local = [i for i, t in enumerate(pool)
                                  if _tote_anchor_transit(t) in seed_window]
                if not seed_idx_local:
                    seed_idx_local = list(range(len(pool)))
                window = distinct_anchors[:cfg.pick_across_trucks]
            else:
                window = _select_pat_window(pool, cfg)
                if not window:
                    break
                wset = set(window)
                eligible_idx = [i for i, t in enumerate(pool)
                                if _tote_anchor_transit(t) in wset
                                and set(t.transit_ids).issubset(wset)]
                if not eligible_idx:
                    eligible_idx = [i for i, t in enumerate(pool) if _tote_anchor_transit(t) in wset]
                    if not eligible_idx:
                        break
                seed_idx_local = None
            eligible = [pool[i] for i in eligible_idx]
            dm = _build_tote_distance_matrix(eligible, matrix)
            is_cold = zone in cfg.cold_chain_zones
            if use_affinity and len(eligible) >= 2:
                aff = _build_affinity_matrix(eligible, dm, cfg)
                chosen_local = _pick_single_trolley_affinity(
                    eligible, cfg, matrix, dm,
                    aff, seed_indices=seed_idx_local,
                    construction_rng=construction_rng,
                    items_by_order=items_by_order,
                    enforce_cold_chain=is_cold,
                )
            else:
                chosen_local = _pick_single_trolley(eligible, cfg, dm)
            chosen_originals = [eligible[i] for i in chosen_local]
            chosen_totes = list(chosen_originals)
            # Same-TID tote consolidation + refill (2026-05-11). Build totes
            # per-order then pool same-TID orders into a single physical tote at
            # trolley-assembly time. Freeing tote slots lets us refill with more
            # eligible totes. Applies to Freezer (cap 2) and Security (cap 6,
            # back-room pick — no walk constraint, capacity-bound).
            consolidate_cap = 0
            if zone == "Freezer" and cfg.frozen_max_orders_per_tote > 1:
                consolidate_cap = cfg.frozen_max_orders_per_tote
            elif zone == "Security" and cfg.security_max_orders_per_tote > 1:
                consolidate_cap = cfg.security_max_orders_per_tote
            if consolidate_cap > 1 and len(chosen_totes) > 0:
                cap_w_z = cfg.capacity_max_weight_g[zone]
                cap_v_z = cfg.capacity_max_volume_cm3[zone]
                taken_idx = set(chosen_local)
                chosen_local_list = list(chosen_local)
                safety = 0
                while safety < 24:
                    safety += 1
                    chosen_totes = _consolidate_same_tid_totes(
                        chosen_originals, cap_w_z, cap_v_z,
                        consolidate_cap)
                    if len(chosen_totes) >= cfg.trolley_max_totes:
                        break
                    # Look for an eligible tote to add (closest avg, PAT-feasible).
                    cur_tids: Set[str] = set()
                    for t in chosen_totes:
                        cur_tids.update(t.transit_ids)
                    best_i = -1
                    best_d = float("inf")
                    for ri in range(len(eligible)):
                        if ri in taken_idx:
                            continue
                        cand = eligible[ri]
                        new_tids = cur_tids | set(cand.transit_ids)
                        if len(new_tids) > cfg.pick_across_trucks:
                            continue
                        avg = (sum(dm[ri][cj] for cj in chosen_local_list)
                               / max(1, len(chosen_local_list)))
                        if avg < best_d:
                            best_d = avg
                            best_i = ri
                    if best_i == -1:
                        break
                    taken_idx.add(best_i)
                    chosen_local_list.append(best_i)
                    chosen_originals.append(eligible[best_i])
                # Final consolidation pass after last add (if any).
                chosen_totes = _consolidate_same_tid_totes(
                    chosen_originals, cap_w_z, cap_v_z,
                    consolidate_cap)
            # C1 (2026-05-11): the affinity picker enforces cold-chain compliance during
            # composition for cold-chain zones, so a breach should not be possible here.
            # _split_for_cold_chain remains as a safety net for the non-affinity path and
            # is a no-op when the picker has already capped the composition.
            if is_cold:
                split = _split_for_cold_chain(chosen_totes, items_by_order, cfg, matrix)
            else:
                split = [chosen_totes]
            # Only totes that actually end up in a constructed trolley leave the pool.
            # If the cold-chain split drops totes (only possible from the non-affinity
            # path now), each dropped tote becomes its own 1-tote group — kept for
            # backwards compatibility. With C1 enforced the picker naturally returns
            # an undersize trolley rather than oversizing and splitting, so undersize
            # only happens when no eligible candidate in the current PAT window is
            # both PAT-feasible AND cold-chain-feasible.
            for sub in split:
                if not sub:
                    continue
                groups.append((list(window), sub))
            # Pool removal must use ORIGINAL (pre-consolidation) tote IDs.
            chosen_ids = {t.tote_id for t in chosen_originals}
            pool = [t for t in pool if t.tote_id not in chosen_ids]
        return groups

    def _phase1_total_cost(groups: List[Tuple[List[str], List[ToteResult]]]) -> float:
        return sum(_trolley_path_cost([it for t in tl for it in t.items], matrix, cfg)
                   for _, tl in groups)

    # G4 — construction-order best-of-N: run N greedy passes, pick the lowest cumulative
    # trip_cost as the SA seed. Run-0 is unperturbed (matches baseline exactly), runs 1..N-1
    # sample from top-K Phase 2 candidates per pick to explore alternative greedy paths.
    # Run-0 unperturbed guarantees no regression vs baseline.
    if (use_affinity and cfg.enable_g4_construction_best_of_n
            and cfg.g4_construction_seeds > 1):
        trolley_groups: List[Tuple[List[str], List[ToteResult]]] = []
        best_cost = float("inf")
        seed_costs: List[float] = []
        for n in range(cfg.g4_construction_seeds):
            crng = None if n == 0 else random.Random(8101 + n * 31)
            cand_groups = _construct_phase1(crng)
            cand_cost = _phase1_total_cost(cand_groups)
            seed_costs.append(cand_cost)
            if cand_cost < best_cost:
                best_cost = cand_cost
                trolley_groups = cand_groups
        seed_str = ", ".join(f"{x:.1f}" for x in seed_costs)
        print(f"        [{zone}] G4 phase-1 seeds (cost): "
              f"unperturbed={seed_costs[0]:.1f}  perturbed=[{seed_str}]  best={best_cost:.1f}",
              flush=True)
    else:
        trolley_groups = _construct_phase1(None)

    # Cross-trolley SA refinement (time-equivalent path cost) for affinity zones only.
    if use_affinity and cfg.enable_sa and len(trolley_groups) >= 2:
        tote_lists = [g[1] for g in trolley_groups]
        windows = [g[0] for g in trolley_groups]
        baseline = sum(_trolley_path_cost([it for t in tl for it in t.items], matrix, cfg)
                       for tl in tote_lists)
        best_lists = tote_lists
        best_score = baseline
        seed_scores: List[float] = []
        is_cold = zone in cfg.cold_chain_zones
        for s in range(cfg.sa_seeds):
            cand = _sa_refine_trolleys_spt(tote_lists, cfg, matrix, seed=s * 17 + 7919,
                                           items_by_order=items_by_order,
                                           enforce_cold_chain=is_cold)
            score = sum(_trolley_path_cost([it for t in tl for it in t.items], matrix, cfg)
                        for tl in cand)
            seed_scores.append(score)
            if score < best_score:
                best_score = score
                best_lists = cand
        # Diagnostic: report SA seed sweep so we can see variance even when none improve.
        seed_str = ", ".join(f"{x:.1f}" for x in seed_scores)
        print(f"        [{zone}] SA seeds (cost): baseline={baseline:.1f}  seeds=[{seed_str}]"
              f"  best={best_score:.1f}", flush=True)
        if best_score < baseline:
            print(f"        [{zone}] SA refinement: {baseline:.1f} -> {best_score:.1f} cost"
                  f" ({(baseline - best_score):.1f} saved)", flush=True)
            # Trolley count may have dropped if SA emptied a trolley; pad windows accordingly.
            if len(best_lists) < len(windows):
                windows = windows[:len(best_lists)]
            elif len(best_lists) > len(windows):
                windows.extend([[]] * (len(best_lists) - len(windows)))
            trolley_groups = list(zip(windows, best_lists))

    # Materialise final TrolleyResult objects in order.
    results: List[TrolleyResult] = []
    seq = 0
    for window, sub in trolley_groups:
        if not sub:
            continue
        seq += 1
        tid = f"{zone[:2].upper()}TR_{seq:04d}"
        tr = _build_trolley_result(tid, sub, matrix, cfg, items_by_order)
        tr.pat_wave_window = window
        results.append(tr)
    return results


def build_trolleys_for_zone(totes: List[ToteResult], zone: str, cfg: StoreConfig,
                            matrix: DistanceMatrix, items_by_order: Dict[str, List[Item]]) -> List[TrolleyResult]:
    if not totes:
        return []
    print(f"        [{zone}] precomputing tote-tote distance matrix ({len(totes)} totes)", flush=True)
    dm = _build_tote_distance_matrix(totes, matrix)
    seeded_idx = _greedy_seed_trolleys(totes, cfg, dm)
    refined_idx = _swap_refine(seeded_idx, cfg, dm)
    if cfg.enable_sa:
        best_idx = refined_idx
        best_score = sum(_proxy_trolley_score(t, dm) for t in best_idx)
        for s in range(cfg.sa_seeds):
            cand = _sa_refine(refined_idx, cfg, dm, seed=s * 17 + 1)
            score = sum(_proxy_trolley_score(t, dm) for t in cand)
            if score < best_score:
                best_idx = cand
                best_score = score
        refined_idx = best_idx

    refined: List[List[ToteResult]] = [[totes[i] for i in idxs] for idxs in refined_idx]

    # Cold-chain cap (HC12)
    if zone in cfg.cold_chain_zones:
        split: List[List[ToteResult]] = []
        for trolley_totes in refined:
            split.extend(_split_for_cold_chain(trolley_totes, items_by_order, cfg, matrix))
        refined = split

    results: List[TrolleyResult] = []
    for k, trolley_totes in enumerate(refined):
        if not trolley_totes:
            continue
        tid = f"{zone[:2].upper()}TR_{k+1:04d}"
        results.append(_build_trolley_result(tid, trolley_totes, matrix, cfg, items_by_order))
    return results


# ----------------------------------------------------------------------------
# 9. HC validator
# ----------------------------------------------------------------------------


def _reconcile_global(all_items: List[Item], trolleys: List[TrolleyResult]) -> List[AlgorithmException]:
    """HC1 across the whole session: every input item must appear in some trolley."""
    issues: List[AlgorithmException] = []
    placed_pairs: Set[Tuple[str, str]] = set()
    for tr in trolleys:
        for tote in tr.totes:
            for it in tote.items:
                placed_pairs.add((it.order_no, it.stock_code))
    for it in all_items:
        if (it.order_no, it.stock_code) not in placed_pairs:
            issues.append(AlgorithmException(
                "HC1_ITEM_LOST", "ERROR",
                f"Input item not placed: {it.order_no}/{it.line_no}/{it.stock_code}",
                {"order": it.order_no, "line": it.line_no, "stock": it.stock_code,
                 "transit_id": it.transit_id},
            ))
    return issues


def validate_hard_constraints(trolleys: List[TrolleyResult], cfg: StoreConfig,
                              all_items: List[Item],
                              pat_selected: Set[str]) -> List[AlgorithmException]:
    issues: List[AlgorithmException] = []

    for tr in trolleys:
        # HC4/HC5/TR-HR-1: single-zone trolley
        zones = {t.zone for t in tr.totes}
        if len(zones) > 1:
            issues.append(AlgorithmException(
                "HC4_TROLLEY_MIXED_ZONES", "ERROR",
                f"Trolley {tr.trolley_id} mixes zones: {zones}",
                {"trolley": tr.trolley_id, "zones": list(zones)},
            ))

        # HC11: max totes per trolley
        if len(tr.totes) > cfg.trolley_max_totes:
            issues.append(AlgorithmException(
                "HC11_TROLLEY_OVERFILL", "ERROR",
                f"Trolley {tr.trolley_id} has {len(tr.totes)} totes > {cfg.trolley_max_totes}",
                {"trolley": tr.trolley_id, "n": len(tr.totes)},
            ))

        # HC12: cold-chain cap (NEW 2026-05-11: uses cold_chain_compliance_time_s — arrival
        # at first pick -> staging only; excludes setup/downtime/approach walk.)
        if tr.zone in cfg.cold_chain_zones:
            cap_s = cfg.cold_chain_cap_min * 60.0
            if tr.cold_chain_time_s > cap_s + 1e-3:
                issues.append(AlgorithmException(
                    "HC12_COLD_CHAIN_BREACH", "ERROR",
                    f"Trolley {tr.trolley_id} ({tr.zone}) cold_chain_time={tr.cold_chain_time_s:.1f}s > cap {cap_s:.1f}s",
                    {"trolley": tr.trolley_id, "cold_chain_s": tr.cold_chain_time_s},
                ))

        # T-HR-8 / TR-HR-8: uturn_count must be set
        if tr.uturn_count < 0:
            issues.append(AlgorithmException(
                "TRHR8_UTURN_MISSING", "ERROR",
                f"Trolley {tr.trolley_id} has invalid uturn_count",
                {"trolley": tr.trolley_id},
            ))

        for tote in tr.totes:
            # HC8: capacity per zone
            cap_w = cfg.capacity_max_weight_g[tote.zone]
            cap_v = cfg.capacity_max_volume_cm3[tote.zone]
            if not tote.is_ugly:
                if tote.total_weight_g > cap_w + 1e-3:
                    issues.append(AlgorithmException(
                        "HC8_WEIGHT_OVER", "ERROR",
                        f"Tote {tote.tote_id} weight {tote.total_weight_g:.0f}g > {cap_w:.0f}g",
                        {"tote": tote.tote_id},
                    ))
                if tote.total_volume_cm3 > cap_v + 1e-3:
                    issues.append(AlgorithmException(
                        "HC8_VOLUME_OVER", "ERROR",
                        f"Tote {tote.tote_id} volume {tote.total_volume_cm3:.0f}cm3 > {cap_v:.0f}cm3",
                        {"tote": tote.tote_id},
                    ))

            # HC3 / T-HR-1: 1 order per tote (Frozen up to 2)
            max_orders = cfg.frozen_max_orders_per_tote if tote.zone == "Freezer" else 1
            if len(tote.order_nos) > max_orders:
                issues.append(AlgorithmException(
                    "HC3_TOO_MANY_ORDERS_IN_TOTE", "ERROR",
                    f"Tote {tote.tote_id} has {len(tote.order_nos)} orders, max {max_orders}",
                    {"tote": tote.tote_id, "orders": tote.order_nos},
                ))

        # PAT (per-trolley rolling): trolley's distinct TransitID count must be <= N.
        n_distinct = len([t for t in tr.transit_ids_covered if t])
        if n_distinct > cfg.pick_across_trucks:
            issues.append(AlgorithmException(
                "PAT_TRUCK_COUNT_OVER", "ERROR",
                f"Trolley {tr.trolley_id} spans {n_distinct} TransitIDs > limit {cfg.pick_across_trucks}",
                {"trolley": tr.trolley_id, "transit_ids": tr.transit_ids_covered,
                 "limit": cfg.pick_across_trucks},
            ))
    return issues


# ----------------------------------------------------------------------------
# 10. Baseline (production TrolleyID column)
# ----------------------------------------------------------------------------


def analyse_baseline(items: List[Item], matrix: DistanceMatrix, cfg: StoreConfig,
                     items_by_order: Dict[str, List[Item]],
                     exclude_label: bool = False) -> List[TrolleyResult]:
    """Re-derive production trolleys from the TrolleyID column.

    Each TrolleyID -> a TrolleyResult. ToteResult granularity = TrayHeaderID (one tote per
    distinct production tray label) so the tray/tote count matches production exactly. This
    is critical for parity because production may split a single order across multiple trays
    on one trolley.

    exclude_label=True drops items where PickingType=='Label'. These are post-plan volumetric
    overflow additions; excluding them yields the "production planned" baseline. Including
    them yields "production actual".
    """
    if not cfg._calibrated and items:
        cfg.calibrate(matrix, items)
    by_trolley: Dict[str, List[Tuple[str, Item]]] = {}
    for it in items:
        if not it.trolley_id_baseline:
            continue
        if exclude_label and (it.picking_type or "").lower() == "label":
            continue
        by_trolley.setdefault(it.trolley_id_baseline, []).append(it)
    results: List[TrolleyResult] = []
    for tid, its in sorted(by_trolley.items()):
        zones = sorted({it.zone for it in its})
        zone_label = zones[0] if len(zones) == 1 else "Mixed"
        # One ToteResult per TrayHeaderID. Items without a TrayHeaderID grouped per order.
        by_tray: Dict[str, List[Item]] = {}
        unlabeled: Dict[str, List[Item]] = {}
        for it in its:
            tray = it.tray_header_id or ""
            if tray:
                by_tray.setdefault(tray, []).append(it)
            else:
                unlabeled.setdefault(it.order_no, []).append(it)
        groups: List[Tuple[str, List[Item]]] = list(by_tray.items()) + [(f"O_{k}", v) for k, v in unlabeled.items()]
        totes: List[ToteResult] = []
        for label, grp in sorted(groups):
            totes.append(ToteResult(
                tote_id=f"BASE_{tid}_{label}",
                zone=zone_label,
                order_nos=sorted({it.order_no for it in grp}),
                items=grp,
                total_weight_g=sum(it.quantity * it.unit_weight_g for it in grp),
                total_volume_cm3=sum(it.quantity * it.unit_volume_cm3 for it in grp),
                aisles=sorted({it.aisle_location for it in grp}),
                location_keys=[it.location_key for it in grp],
                transit_ids=sorted({it.transit_id for it in grp}),
                is_ugly=False,
                notes=["baseline"],
                tray_label_count=1,
            ))
        tr = _build_trolley_result(f"BASE_{tid}", totes, matrix, cfg, items_by_order)
        results.append(tr)
    return results


# ----------------------------------------------------------------------------
# 11. main()
# ----------------------------------------------------------------------------


def _summarise(trolleys: List[TrolleyResult]) -> dict:
    """Summary metrics. Reports both physical totes (HC11 unit) and logical trays (production
    parity unit: 1 TrayHeaderID per (order, trolley)).
    """
    if not trolleys:
        return {"trolleys": 0}
    total_d = sum(t.walk_distance_m for t in trolleys)
    total_t = sum(t.walk_time_s + t.goal_time_s for t in trolleys)
    by_zone: Dict[str, dict] = {}
    for t in trolleys:
        z = by_zone.setdefault(t.zone, {"trolleys": 0, "totes_phys": 0, "trays_logical": 0,
                                        "walk_m": 0.0, "uturns": 0, "goal_s": 0.0, "walk_s": 0.0})
        z["trolleys"] += 1
        z["totes_phys"] += len(t.totes)
        z["trays_logical"] += sum(getattr(tt, "tray_label_count", 1) for tt in t.totes)
        z["walk_m"] += t.walk_distance_m
        z["uturns"] += t.uturn_count
        z["goal_s"] += t.goal_time_s
        z["walk_s"] += t.walk_time_s
    return {
        "trolleys": len(trolleys),
        "totes_phys": sum(len(t.totes) for t in trolleys),
        "trays_logical": sum(sum(getattr(tt, "tray_label_count", 1) for tt in t.totes) for t in trolleys),
        "total_walk_m": round(total_d, 1),
        "total_time_s": round(total_t, 1),
        "by_zone": {k: {kk: round(vv, 1) if isinstance(vv, float) else vv for kk, vv in v.items()}
                    for k, v in by_zone.items()},
    }


def _trolley_to_dict(tr: TrolleyResult) -> dict:
    return {
        "trolley_id": tr.trolley_id,
        "zone": tr.zone,
        "tote_count": len(tr.totes),  # physical (HC11 unit)
        "tray_count": sum(getattr(t, "tray_label_count", 1) for t in tr.totes),  # logical (per-order)
        "tote_ids": [t.tote_id for t in tr.totes],
        "transit_ids": tr.transit_ids_covered,
        "walk_distance_m": tr.walk_distance_m,
        "uturn_count": tr.uturn_count,
        "walk_time_s": tr.walk_time_s,
        "goal_time_s": tr.goal_time_s,
        "unique_skus": tr.unique_skus,
        "total_lines": tr.total_lines,
        "pat_window_start": tr.pat_window_start,
        "pat_window_end": tr.pat_window_end,
        "pat_wave_index": tr.pat_wave_index,
        "pat_wave_window": tr.pat_wave_window,
        "notes": tr.notes,
    }


def main() -> int:
    print("=" * 70)
    print(" tote_trolley_optimizer_v2 - framework-compliant build")
    print("=" * 70)

    cfg = StoreConfig()

    # Load matrix
    print(f"[1/6] Loading distance matrix {DIST_MATRIX_CSV} ...")
    try:
        matrix = DistanceMatrix.load_from_csv(DIST_MATRIX_CSV, unit_to_m=cfg.matrix_unit_to_m)
    except AlgorithmException as e:
        print(f"  FAILED: {e}")
        return 2
    print(f"      labels={len(matrix.labels)}  start={cfg.start_anchor in matrix.label_to_idx}  end={cfg.end_anchor in matrix.label_to_idx}")

    # Load items
    print(f"[2/6] Loading orders {ORDERS_CSV} ...")
    try:
        items = load_orders(ORDERS_CSV)
    except AlgorithmException as e:
        print(f"  FAILED: {e}")
        return 2
    print(f"      items={len(items)}  orders={len({it.order_no for it in items})}  zones={sorted({it.zone for it in items})}")

    # Pre-flight: location coverage in matrix
    quarantined: List[Item] = []
    valid: List[Item] = []
    for it in items:
        if matrix.has(it.location_key):
            valid.append(it)
        else:
            quarantined.append(it)
    if quarantined:
        print(f"      WARN: {len(quarantined)} items at locations missing from matrix - quarantined for fallback")
    items = valid

    items_by_order: Dict[str, List[Item]] = {}
    for it in items:
        items_by_order.setdefault(it.order_no, []).append(it)

    # Compute the session-wide TransitID alpha-index used by Frozen tote merge guard.
    distinct_tids = sorted({it.transit_id for it in items if it.transit_id})
    alpha_idx: Dict[str, int] = {tid: i for i, tid in enumerate(distinct_tids)}
    print(f"      session TransitID universe ({len(distinct_tids)}): {distinct_tids}")

    # Phase A: build totes for the FULL session pool (per zone). Totes carry their TransitID(s).
    print("[3/6] Building totes per zone (full session) ...")
    zone_items: Dict[str, List[Item]] = {}
    for it in items:
        zone_items.setdefault(it.zone, []).append(it)
    all_totes: List[ToteResult] = []
    for zone, zits in sorted(zone_items.items()):
        ts = build_totes_for_zone(zits, zone, cfg, matrix, alpha_idx=alpha_idx)
        print(f"      {zone:<10} items={len(zits):>4}  totes={len(ts):>3}  ugly={sum(1 for t in ts if t.is_ugly)}")
        all_totes.extend(ts)

    # Phase B: per-trolley rolling PAT. At each step the window is the next N TransitIDs that
    # still have remaining totes; depleted IDs are skipped. A trolley can mix BB+BD+BF if BC was
    # already exhausted when that trolley was built.
    print("[4/6] Building trolleys per zone with per-trolley rolling PAT ...")
    new_trolleys: List[TrolleyResult] = []
    totes_by_zone: Dict[str, List[ToteResult]] = {}
    for t in all_totes:
        totes_by_zone.setdefault(t.zone, []).append(t)
    for zone, ztotes in sorted(totes_by_zone.items()):
        ztrs = build_trolleys_rolling_pat_for_zone(ztotes, zone, cfg, matrix, items_by_order)
        print(f"      {zone:<10} totes={len(ztotes):>3}  trolleys={len(ztrs):>3}")
        new_trolleys.extend(ztrs)

    # Diagnostics: count distinct TransitIDs touched per trolley
    trolleys_with_n_truck = {}
    for tr in new_trolleys:
        k = len(tr.transit_ids_covered)
        trolleys_with_n_truck[k] = trolleys_with_n_truck.get(k, 0) + 1
    print(f"      trolleys by TransitID count: {dict(sorted(trolleys_with_n_truck.items()))}")

    # Baseline. Two flavours:
    #   - planned: production trolleys WITHOUT PickingType=Label items (= the original plan
    #     before volumetric overflow forced manual additions). This is the apples-to-apples
    #     comparison target for v2, which is itself a planner.
    #   - actual: production trolleys INCLUDING Label items (= what the picker actually
    #     walked). Reported for completeness.
    print("[5/6] Analysing production baseline (full session) ...")
    baseline_planned = analyse_baseline(items, matrix, cfg, items_by_order, exclude_label=True)
    baseline_actual = analyse_baseline(items, matrix, cfg, items_by_order, exclude_label=False)
    n_label_items = sum(1 for it in items if (it.picking_type or "").lower() == "label")
    print(f"      baseline planned trolleys={len(baseline_planned)} (excl {n_label_items} Label items)")
    print(f"      baseline actual  trolleys={len(baseline_actual)}")
    # Primary baseline = planned (used for headline saving comparison)
    baseline_trolleys = baseline_planned

    # Validate per-trolley against its own pat_wave_window (the window AT THE TIME OF BUILD)
    print("[6/6] Validating hard constraints ...")
    issues: List[AlgorithmException] = []
    for tr in new_trolleys:
        window_set = set(tr.pat_wave_window)
        issues.extend(validate_hard_constraints([tr], cfg, [it for tote in tr.totes for it in tote.items], window_set))
    # Global reconciliation: every input item must appear somewhere
    issues.extend(_reconcile_global(items, new_trolleys))
    wave_diagnostics: List[dict] = []  # legacy field kept for JSON compatibility
    if issues:
        for iss in issues[:10]:
            print(f"      {iss}")
        if len(issues) > 10:
            print(f"      ... +{len(issues)-10} more")
    else:
        print("      OK - no HC violations")

    # Summary
    print()
    print("=" * 70)
    print(" Summary")
    print("=" * 70)
    new_summary = _summarise(new_trolleys)
    base_planned_summary = _summarise(baseline_planned)
    base_actual_summary = _summarise(baseline_actual)
    print("New (v2):", json.dumps(new_summary, indent=2))
    print("Baseline (production planned, excl Label):", json.dumps(base_planned_summary, indent=2))
    print("Baseline (production actual, incl Label) :", json.dumps(base_actual_summary, indent=2))
    if base_planned_summary.get("total_walk_m") and new_summary.get("total_walk_m") is not None:
        saving = base_planned_summary["total_walk_m"] - new_summary["total_walk_m"]
        pct = 100.0 * saving / base_planned_summary["total_walk_m"] if base_planned_summary["total_walk_m"] else 0.0
        verb = "Walk saving" if saving >= 0 else "Walk increase"
        print(f"{verb} vs PLANNED: {saving:+.1f} m  ({pct:+.1f}% vs baseline)  [trolleys new={new_summary['trolleys']} base={base_planned_summary['trolleys']}]")
    if base_actual_summary.get("total_walk_m"):
        saving = base_actual_summary["total_walk_m"] - new_summary["total_walk_m"]
        pct = 100.0 * saving / base_actual_summary["total_walk_m"] if base_actual_summary["total_walk_m"] else 0.0
        verb = "Walk saving" if saving >= 0 else "Walk increase"
        print(f"{verb} vs ACTUAL : {saving:+.1f} m  ({pct:+.1f}% vs baseline)  [trolleys new={new_summary['trolleys']} base={base_actual_summary['trolleys']}]")

    # JSON dump
    out = {
        "store": cfg.store_no,
        "config": {
            "pick_across_trucks": cfg.pick_across_trucks,
            "trolley_max_totes": cfg.trolley_max_totes,
            "frozen_max_orders_per_tote": cfg.frozen_max_orders_per_tote,
            "cold_chain_cap_min": cfg.cold_chain_cap_min,
            "start_anchor": cfg.start_anchor,
            "end_anchor": cfg.end_anchor,
        },
        "waves": wave_diagnostics,
        "matrix_meta": {
            "labels": len(matrix.labels),
        },
        "summary_new": new_summary,
        "summary_baseline_planned": base_planned_summary,
        "summary_baseline_actual": base_actual_summary,
        "label_items_count": n_label_items,
        "trolleys_new": [_trolley_to_dict(t) for t in new_trolleys],
        "trolleys_baseline_planned": [_trolley_to_dict(t) for t in baseline_planned],
        "trolleys_baseline_actual": [_trolley_to_dict(t) for t in baseline_actual],
        "violations": [e.to_dict() for e in issues],
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"\nOutput dumped to {OUTPUT_JSON}")
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
