# Tote & Trolley Optimisation Project — CVRP Version 1.0

This file is the **clean baseline** going forward. CVRP v1.0 is the current
production candidate. Future algorithm trials should be A/B'd against the
numbers and configuration captured here.

---

## CVRP Version 1.0 — current production candidate

**Paradigm:** pure CVRP/TSP over the symmetric walkable-distance matrix.
NN initialisation + 2-opt + or-opt with a precomputed local distance table.
Same engine drives tote builder F8 polish, trolley fill marginal scoring,
SA refinement, and validation reporting. Outputs are byte-identical to the
legacy std/non-std routing heuristic on a Freezer matrix-only baseline.

**Pipeline (per zone, executed by `build_trolleys_production`):**

```
1. v2.build_totes_for_zone           per-order; agglomerative merge + F7 LB-pack + F8 trip_cost polish
2. v2.build_trolleys_rolling_pat...  PAT alpha-anchor seeds; two-phase picker (cheap aff rank -> marginal trip_cost on top-K)
3. v4._reassign_pass                 x3 - every tote vs top-8 aisle-Jaccard targets, accept on delta < 0
4. v2._sa_refine_trolleys_spt        best-of-5 seeds, steepest descent, portfolio:
                                       G1 smart pairwise swap
                                       G2 3-way rotation
                                       G3 worst-tote relocate / directed swap
                                       G6 random-tote relocate (top-K targets)
                                       G8 2-tote Lin-Kernighan chain
                                       G9 CP-SAT LNS partition over K=3 trolleys
                                       legacy relocate / block-swap
5. v2._build_trolley_result          materialise TrolleyResult (walk, uturns, cc_time)
```

**Headline flags (StoreConfig):**

  - `enable_sa_g6_random_relocate: True`  + `sa_g6_top_k_targets: 8`
  - `enable_sa_g8_lk_chain: True`  + `sa_g8_top_k_y: 4` + `sa_g8_top_k_z: 4`
  - `enable_sa_g9_cpsat_lns: True` + `sa_g9_lns_k: 3` + `sa_g9_top_k_swaps: 5` + `sa_g9_time_limit_s: 1.5`
  - `enable_sa_adaptive_weights: True` + `sa_adaptive_segment_iters: 100` + `sa_adaptive_reaction: 0.3` + `sa_adaptive_floor: 0.05`
  - `enable_trip_cost_merge: True`  (trip_cost-marginal agglomerative merge)
  - `margin_topk: 12` (Phase-2 picker MARGIN_TOPK; was 6)
  - `enforce_cold_chain: True` on `cold_chain_zones = {"Chilled"}`
  - `f7_min_count_pack: {"Ambient": False, "Chilled": True, "Freezer": True, "Security": True}`
  - `respect_max_out:   {"Ambient": True,  "Chilled": False, "Freezer": True, "Security": True}`
  - `frozen_max_orders_per_tote: 2`  (same-TID consolidation cap for Freezer)
  - `security_max_orders_per_tote: 6` (same-TID consolidation cap for Security)

**Runtime budget:** 10 minutes wall-clock per store per zone. CVRP v1.0
finishes under budget on every zone (Ambient ~5m 30s, Chilled ~3m 20s,
Freezer ~2s, Security ~2s).

**Decision rule for proposed algorithm changes:**

  - Promote (flag default-ON) only if cost <= current baseline AND runtime <= 10 min.
  - Refute and revert if cost worsens OR runtime breaches 10 min.
  - All flags gated in `StoreConfig` so we can toggle per zone (cold-zone
    re-validation is mandatory before promoting globally).
  - Stack only what won — re-baseline after each promotion.

**Adaptive LNS portfolio weights — promoted (2026-05-12)**

`enable_sa_adaptive_weights: True` is now the default. Pisinger & Ropke
(2007) style EWMA reallocation of SA move shares (G1/G2/G3/G6/G8/G9 +
legacy) based on score = `improvement_m / max(1, calls_in_segment)`.
Reaction r=0.3, segment 100 iters, 5% floor per active move. Initial
weights mirror the fixed-share cascade so iter-0 behaviour matches the
pre-adaptive baseline. Phase A profile motivation: per-move impr/call
ratio measured at 51x (Ambient) and 109x (Chilled) under fixed shares,
with G3 contributing 33-46% of improvement on its 15% slot; floor-only
moves (relocate/block2) contributed ~0%. Phase B A/B on 1419
(`bench_adaptive_weights.py`):

| zone    | metric    | OFF       | ON        | delta              |
|---------|-----------|-----------|-----------|--------------------|
| Ambient | cost      | 31,514.7  | 31,253.0  | **-261.7 (-0.83%)**|
| Ambient | walk / U  | 31,187 / 82 | 30,941 / 78 | -246m / -4 U   |
| Ambient | runtime   | 162.5s    | 205.0s    | +42.5s (+26.2%)    |
| Chilled | cost      | 17,120.1  | 17,079.2  | **-40.9 (-0.24%)** |
| Chilled | walk / U  | 17,044 / 19 | 17,003 / 19 | -41m / 0 U     |
| Chilled | runtime   | 152.4s    | 181.2s    | +28.7s (+18.9%)    |

Cold-zone sanity (Freezer/Security): identical OFF vs ON — adaptive
logic is a no-op when SA portfolio doesn't engage (reassign converges
in 1-2 passes). Aggregate 1419 walk+chilled cost saving: **-302m
(-0.62%)**; max runtime 3m 25s (Ambient ON) — well under 10-min budget.

Multi-store re-validation under new default (1052, 1030) is pending —
expect similar 0.3-1% improvement band based on the Ambient impr/call
gradient.

---

## CVRP v1.0 vs PROD — store 1419 (all zones, 2026-05-12)

### Aggregate (Ambient + Chilled + Freezer; Security is back-room, no walk)

| metric                | CVRP v1.0  | PROD     | delta     | delta %  |
|-----------------------|------------|----------|-----------|----------|
| trolleys              | 243        | 251      | **-8**    | -3.2%    |
| totes (logical)       | 1,458      | 1,478    | -20       | -1.4%    |
| total walk (m)        | 49,704     | 52,544   | **-2,840**| **-5.4%**|
| total U-turns         | 112        | 199      | **-87**   | -43.7%   |
| cost (walk + 4*U)     | **50,152** | 53,340   | **-3,188**| **-6.0%**|

Aggregate excludes Security from walk metrics (back-room pick, stationary;
the operational win there is the -1 physical tote from same-TID
consolidation).

### Per-zone breakdown

**Ambient** (`/tmp/cvrp_v1_ambient.txt`, runtime 5m 33s):

| metric                | CVRP v1.0  | PROD     | delta     |
|-----------------------|------------|----------|-----------|
| trolleys              | 132        | 136      | -4        |
| totes                 | 788        | 793      | -5        |
| total walk (m)        | 31,186.7   | 32,888.6 | **-1,702**|
| walk per trolley (m)  | 236.3      | 241.8    | -5.5      |
| total U-turns         | 82         | 134      | -52       |
| cost (walk + 4*U)     | **31,514.7**| 33,424.6| **-1,910**|
| PAT max TIDs/trolley  | 3          | 3        | -         |

**Chilled** (`/tmp/cvrp_v1_chilled.txt`, runtime 3m 18s):

| metric                | CVRP v1.0  | PROD     | delta     |
|-----------------------|------------|----------|-----------|
| trolleys              | 96         | 97       | -1        |
| totes                 | 547        | 560      | -13       |
| total walk (m)        | 17,044     | 17,968   | **-924**  |
| walk per trolley (m)  | 177.5      | 185.2    | -7.7      |
| total U-turns         | 19         | 54       | -35       |
| cost (walk + 4*U)     | **17,120** | 18,184   | **-1,064**|
| cold-chain breaches   | 0          | n/a      | -         |
| PAT max TIDs/trolley  | 3          | 3        | -         |

**Freezer** (`/tmp/cvrp_v1_freezer.txt`, runtime 2.1s):

| metric                | CVRP v1.0  | PROD     | delta     |
|-----------------------|------------|----------|-----------|
| trolleys              | 14         | 17       | -3        |
| logical totes         | 123        | 125      | -2        |
| physical totes        | 79         | n/a      | -         |
| total walk (m)        | 1,473      | 1,687    | -214      |
| walk per trolley (m)  | 105.2      | 99.2     | +6.0      |
| total U-turns         | 11         | 11       | 0         |
| cost (walk + 4*U)     | **1,517**  | 1,731    | **-214**  |

Freezer physical-tote breakdown post same-TID consolidation: 35 singletons
+ 44 paired = 79 physical totes carrying 123 logical orders.

**Security** (`/tmp/cvrp_v1_security.txt`, runtime 2.0s, back-room pick):

| metric            | CVRP v1.0  | PROD     | delta |
|-------------------|------------|----------|-------|
| trolleys          | 1          | 1        | 0     |
| logical totes     | 2          | 2        | 0     |
| physical totes    | **1**      | 2        | **-1**|
| orders carried    | 2          | 2        | 0     |

NEW consolidates two same-TID (`BH`) orders into a single physical tote
(148g / 559cm3 / 1.2% fill). PROD ships them as two separate trays.

---

## CVRP v1.0 vs PROD — store 1052 (PAT=2, 45L totes, 2026-05-12)

Second-store validation. Store 1052 differs from 1419 on two material
parameters: **PAT cap = 2** (vs 1419's 3) and **per-tote volume cap = 45L**
(vs 1419's 48L). Same code path, same flags, same SOTA-grade SA portfolio;
the only changes are paths + `pick_across_trucks=2` +
`capacity_max_volume_cm3=45000` injected via `_store_1052_setup.py`.
Store 1052 also has only one staging location (`staging_location_1`); the
setup helper sets `end_anchor_alt` to the same so the optimiser falls back
to a single anchor.

### Aggregate (Ambient + Chilled + Freezer; Security is back-room, no walk)

| metric                | CVRP v1.0  | PROD     | delta     | delta %  |
|-----------------------|------------|----------|-----------|----------|
| trolleys              | 130        | 131      | **-1**    | -0.8%    |
| totes (logical)       | 785        | 780      | +5        | +0.6%    |
| total walk (m)        | 21,119     | 22,664   | **-1,545**| **-6.8%**|
| total U-turns         | 83         | 155      | **-72**   | -46.5%   |
| cost (walk + 4*U)     | **21,451** | 23,284   | **-1,833**| **-7.9%**|

Aggregate excludes Security from walk metrics. The store-1052 saving is
**larger in % terms** than 1419's (-7.9% vs -6.0%) despite the tighter
PAT=2 cap — the more restrictive PAT constraint cuts cross-truck noise,
and PROD's aisle-sequence heuristic appears to handle the smaller store
layout less well than the dense 1419 floorplan.

### Per-zone breakdown

**Ambient** (`/tmp/cvrp_v1_1052_ambient.txt`, runtime 4m 19s):

| metric                | CVRP v1.0  | PROD     | delta     |
|-----------------------|------------|----------|-----------|
| trolleys              | 78         | 77       | +1        |
| totes                 | 461        | 450      | +11       |
| total walk (m)        | 15,360.3   | 16,105.8 | **-745**  |
| walk per trolley (m)  | 196.9      | 209.2    | -12.2     |
| total U-turns         | 52         | 92       | -40       |
| cost (walk + 4*U)     | **15,568.3**| 16,473.8| **-905**  |
| PAT max TIDs/trolley  | 2          | 2        | -         |

NEW shows +1 trolley and +11 totes against PROD here. The tighter PAT=2
cap forces more partition fragments than 1419's PAT=3, but per-trolley
walk drops 12.2 m and U-turns nearly halve — net cost still wins by 905.

**Chilled** (`/tmp/cvrp_v1_1052_chilled.txt`, runtime 1m 45s):

| metric                | CVRP v1.0  | PROD     | delta     |
|-----------------------|------------|----------|-----------|
| trolleys              | 43         | 43       | 0         |
| totes                 | 242        | 247      | -5        |
| total walk (m)        | 4,340      | 4,871    | **-531**  |
| walk per trolley (m)  | 100.9      | 113.3    | -12.3     |
| total U-turns         | 19         | 45       | -26       |
| cost (walk + 4*U)     | **4,416**  | 5,051    | **-635**  |
| cold-chain breaches   | 0          | n/a      | -         |
| PAT max TIDs/trolley  | 2          | 2        | -         |

Largest % win of the store: -12.6% cost. Same trolley count, fewer totes,
materially lower walk + U-turns.

**Freezer** (`/tmp/cvrp_v1_1052_freezer.txt`, runtime 1.6s):

| metric                | CVRP v1.0  | PROD     | delta     |
|-----------------------|------------|----------|-----------|
| trolleys              | 9          | 11       | -2        |
| logical totes         | 82         | 83       | -1        |
| physical totes        | 49         | n/a      | -         |
| total walk (m)        | 1,419      | 1,687    | -267      |
| walk per trolley (m)  | 157.7      | 153.3    | +4.4      |
| total U-turns         | 12         | 18       | -6        |
| cost (walk + 4*U)     | **1,467**  | 1,759    | **-291**  |

Freezer physical-tote breakdown post same-TID consolidation: 16 singletons
+ 33 paired = 49 physical totes carrying 82 logical orders.

**Security** (`/tmp/cvrp_v1_1052_security.txt`, runtime 1.5s, back-room pick):

| metric            | CVRP v1.0  | PROD     | delta |
|-------------------|------------|----------|-------|
| trolleys          | 1          | 1        | 0     |
| logical totes     | 1          | 1        | 0     |
| physical totes    | 1          | 1        | 0     |
| orders carried    | 1          | 1        | 0     |

Only one Security line item in the 1052 export, so no consolidation
opportunity (no same-TID partner to merge with). Identical to PROD.

### 1052 vs 1419 — multi-store deltas summary

| zone     | 1419 cost win | 1052 cost win | 1419 walk win | 1052 walk win |
|----------|---------------|---------------|---------------|---------------|
| Ambient  | -5.7%         | **-5.5%**     | -5.2%         | **-4.6%**     |
| Chilled  | -5.9%         | **-12.6%**    | -5.1%         | **-10.9%**    |
| Freezer  | -12.4%        | **-16.5%**    | -12.7%        | **-15.9%**    |
| Aggregate| -6.0%         | **-7.9%**     | -5.4%         | **-6.8%**     |

CVRP v1.0 generalises cleanly to a second store with different attributes
(smaller floor, PAT=2, 45L totes, single staging location). The Chilled
and Freezer wins both grew. The Ambient win held within the same band,
confirming `f7_min_count_pack["Ambient"] = False` carries over correctly
to PAT=2 layouts.

---

## CVRP v1.0 vs PROD — store 1030 (PAT=2, 45L totes, 2026-05-12)

Third-store validation. Store 1030 shares 1052's parameters (**PAT cap = 2**,
**per-tote volume cap = 45L**) but retains two staging locations
(`staging_location_1` + `_2`), so the default `end_anchor_alt` is kept.
Setup via `_store_1030_setup.py`. Smaller volume than 1052 (2,313 line
items vs 1052's 3,921) — operationally a mid-tier store.

### Aggregate (Ambient + Chilled + Freezer; Security back-room, no walk)

| metric                | CVRP v1.0  | PROD     | delta     | delta %   |
|-----------------------|------------|----------|-----------|-----------|
| trolleys              | 77         | 78       | **-1**    | -1.3%     |
| totes (logical)       | 466        | 458      | +8        | +1.7%     |
| total walk (m)        | 12,980     | 14,651   | **-1,671**| **-11.4%**|
| total U-turns         | 66         | 91       | **-25**   | -27.5%    |
| cost (walk + 4*U)     | **13,244** | 15,015   | **-1,771**| **-11.8%**|

**Largest cost saving of the three stores tested.** Store 1030's PROD
solution is notably weaker on Ambient (walk-per-trolley 211.0 m vs CVRP
v1.0's 186.1 m, a 25 m/trolley gap that's 4-5x what we saw on 1419/1052).
The algorithm extracts a bigger absolute win because there's more PROD
inefficiency to mine on this store's layout.

### Per-zone breakdown

**Ambient** (`/tmp/cvrp_v1_1030_ambient.txt`, runtime 3m 40s):

| metric                | CVRP v1.0  | PROD     | delta     |
|-----------------------|------------|----------|-----------|
| trolleys              | 50         | 49       | +1        |
| totes                 | 295        | 286      | +9        |
| total walk (m)        | 9,306.6    | 10,340.6 | **-1,034**|
| walk per trolley (m)  | 186.1      | 211.0    | **-24.9** |
| total U-turns         | 61         | 82       | -21       |
| cost (walk + 4*U)     | **9,550.6**| 10,668.6 | **-1,118**|
| PAT max TIDs/trolley  | 2          | 2        | -         |

Walk-per-trolley delta of -24.9 m is the largest seen across all stores
and zones to date. CVRP v1.0 settles on +1 trolley vs PROD but shaves a
quarter-football-field of walking off each one.

**Chilled** (`/tmp/cvrp_v1_1030_chilled.txt`, runtime 1m 05s):

| metric                | CVRP v1.0  | PROD     | delta     |
|-----------------------|------------|----------|-----------|
| trolleys              | 22         | 23       | -1        |
| totes                 | 129        | 131      | -2        |
| total walk (m)        | 3,187      | 3,766    | **-579**  |
| walk per trolley (m)  | 144.9      | 163.7    | -18.9     |
| total U-turns         | 5          | 9        | -4        |
| cost (walk + 4*U)     | **3,207**  | 3,802    | **-595**  |
| cold-chain breaches   | 0          | n/a      | -         |
| PAT max TIDs/trolley  | 2          | 2        | -         |

**Freezer** (`/tmp/cvrp_v1_1030_freezer.txt`, runtime 3.9s):

| metric                | CVRP v1.0  | PROD     | delta     |
|-----------------------|------------|----------|-----------|
| trolleys              | 5          | 6        | -1        |
| logical totes         | 42         | 41       | +1        |
| physical totes        | 25         | n/a      | -         |
| total walk (m)        | 486        | 544      | -58       |
| walk per trolley (m)  | 97.3       | 90.6     | +6.6      |
| total U-turns         | 0          | 0        | 0         |
| cost (walk + 4*U)     | **486**    | 544      | **-58**   |

Freezer physical-tote breakdown post same-TID consolidation: 8 singletons
+ 17 paired = 25 physical totes carrying 42 logical orders.

**Security** (`/tmp/cvrp_v1_1030_security.txt`, runtime 1.3s, back-room):

| metric            | CVRP v1.0  | PROD     | delta |
|-------------------|------------|----------|-------|
| trolleys          | 1          | 1        | 0     |
| logical totes     | 2          | 2        | 0     |
| physical totes    | 2          | 2        | 0     |
| orders carried    | 1          | 1        | 0     |

Three same-TID (`BD`), same-order (303062120) items totalling ~14.3 kg —
capacity-bound by the 12.5 kg/tote weight cap, not consolidation logic,
so both NEW and PROD ship 2 totes. NEW's split is 1+2 items (5,764 g /
8,544 g); PROD's split is 2+1 items (7,108 g / 7,200 g). Cost-equivalent.

### Three-store summary

| store | PAT | tote vol | trolleys delta | walk delta % | cost delta % |
|-------|-----|----------|----------------|--------------|--------------|
| 1419  | 3   | 48L      | -8 (-3.2%)     | -5.4%        | -6.0%        |
| 1052  | 2   | 45L      | -1 (-0.8%)     | -6.8%        | -7.9%        |
| 1030  | 2   | 45L      | -1 (-1.3%)     | **-11.4%**   | **-11.8%**   |

CVRP v1.0 wins consistently across all three stores on every walking
zone. The % savings vary by a factor of 2 between best and worst store,
which is expected — PROD's heuristic is closer to optimal on dense,
well-laid-out stores (1419) than on smaller/awkward layouts (1030).
**This variance is itself a signal:** for per-store auto-tuning
(SMAC/irace, backlog item #5), the heuristic-vs-optimum gap is the
target — stores with large gaps benefit most from the v1.0 portfolio.

---

## Key zone-specific design rules (do not regress)

### Cold chain — Chilled (HC12, 30-min cap)

Cold-chain compliance time formula is `cold_chain_compliance_time_s`
(`tote_trolley_optimizer_v2.py:~642`): pick-path walk (1st item -> last
item -> staging) + U-turn penalty + per-line + per-item + over-60 bonus.
**Excludes** setup, downtime, and approach walk (start -> 1st pick). Cap
stays at 30 min (`cfg.cold_chain_cap_min = 30.0`).

The C1 composition-time guard rejects candidate totes whose addition would
breach cc-time in:

  - `_pick_single_trolley_affinity` Phase 2 (picker)
  - `_reassign_pass` (v4 single-tote moves)
  - `_sa_refine_trolleys_spt` (every SA move type)

`_split_for_cold_chain` remains as a safety net but is effectively a no-op
on the affinity path because compositions never breach by construction.

### Same-TID multi-order consolidation

**Freezer** (`frozen_max_orders_per_tote = 2`): domain rule
T-HR-1/HC3 — picker physically pushes the tote through cold zones, so the
operational cost of mis-routing a multi-order tote grows with order count.
2-order cap.

**Security** (`security_max_orders_per_tote = 6`): back-room pick, no
walk penalty; cap is purely capacity-driven and mirrors the trolley-max-totes
slot count.

Both are implemented through `_consolidate_same_tid_totes` (iterates while
`max_orders > 2`, supports 3+ same-TID order merges) and the consolidation
+ refill loop in `_construct_phase1`. Same-TID detection: orders consolidate
only when they share a TID.

### `respect_max_out` Chilled workaround

`StoreConfig.respect_max_out["Chilled"] = False`. The source-data
`MaxOutTote=True` flag is enforced for Ambient/Freezer/Security but **not**
for Chilled. Reason: PROD evidently does not honour this flag the same way
on Chilled — root cause analysis on store 1419 (orders 303158845 and
303193212, stock codes 969723 and 268603) found PROD ships single totes
where strict enforcement would split into 5-6.

**TODO:** investigate true PROD semantics of `MaxOutTote`. Candidates:
stale flag, advisory-only, only enforced when also `Splittable=False`, or
different semantic ("max qty per tote" not "alone in tote"). Once confirmed,
either correct the flag at source, re-interpret in `load_orders`, or
re-enable enforcement.

### LB-pack F7 — zone-conditional

`f7_min_count_pack["Ambient"] = False`; True for Chilled / Freezer /
Security. The multi-strategy decreasing bin-pack F7 path wins on Chilled
but regresses Ambient walk on the wide-aisle layout (every cross-aisle
merge costs >= 24.84m, U-turn-bounded intra-aisle walk caps savings).
Ambient stays on greedy donor-dissolution F7 + F8 distance polish.

### IncrementalRoute screen — refuted (default OFF, kept wired)

`enable_sa_incremental_route` (default **False**). Port of Shaun's PathHelper
pattern from `Shaunsmodeldraft/swap_utils.py:518` — adds a cheapest-insertion
"cheap delta" screen in SA candidate eval, intended to skip the full
NN+2opt+oropt re-solve when cheap delta >= 0. Targets the 45-55% miss rate
on `_BEST_PATH_CACHE` measured during SA on Ambient (~50k misses per build).

A/B on 1419 Ambient (`bench_ir_screen_ambient.py`, 2026-05-12):

| metric         | OFF       | ON        | delta            |
|----------------|-----------|-----------|------------------|
| trolleys       | 132       | 132       | 0                |
| walk (m)       | 31,186.7  | 32,011.6  | +824.9           |
| U-turns        | 82        | 113       | +31              |
| cost (+4*U)    | 31,514.7  | 32,463.6  | **+948.9 (+3.0%)** |
| runtime (s)    | 157.7     | 105.9     | **-51.8 (-32.9%)** |

Cost gate FAILS by ~3%, runtime gate passes by ~33%. The cheap-insertion
estimate has ~+5% mean noise but ~+31% tail excess vs `trip_cost`
(`verify_incremental_route.py`: 9/160 cases where cheap < true), and on
2-trolley deltas the noise easily flips the sign of small (-1 to -20m)
improvements. The screen rejects ~75% of true improvers (per-seed
`improving` count: 88->23 on the v2 SA pass, 34->11 on the v4.2 pass).

Verdict: REJECT under v1.0 gate. Code path retained behind the flag for:
  - Future tighter cheap estimators (e.g. cheapest-insertion + 1-sweep
    2-opt on the changed segment).
  - Per-instance re-trial on stores / zones where the cheap-true gap
    might be smaller (e.g. cold zones have fewer aisles, possibly less
    noise).
The `IncrementalRoute` class remains importable as a building block for
future SA experiments.

---

## Active files

**Code (Corefiles/):**
  - `tote_trolley_optimizer_v2.py` — main optimiser module (tote builder,
    trolley builder, picker, SA, CVRP engine, F7/F8, consolidation).
  - `tote_trolley_optimizer_v4.py` — reassign + warm-start hybrid
    orchestrator. Reuses v2's SA + result builder.
  - `build_trolleys_production` — top-level entry point that routes each
    zone through the warm-start hybrid pipeline.

**Validators (Corefiles/):**
  - `deep_dive_ambient_v4_2.py` — Ambient 4-way validator (still emits
    v2/v4.1 columns alongside CVRP v1.0; the v4.2 column IS CVRP v1.0).
  - `deep_dive_chilled.py` — Chilled validator.
  - `deep_dive_freezer.py` — Freezer validator (pre/post-consolidation diagnostics).
  - `deep_dive_security.py` — Security back-room validator.

**Per-store shims (Corefiles/):**
  - `_store_1052_setup.py` — side-effect import that monkey-patches v2 +
    v4 with the 1052 paths and a StoreConfig factory pre-filled with
    `store_no="1052"`, `pick_across_trucks=2`,
    `capacity_max_volume_cm3 = 45000` per zone, and
    `end_anchor_alt = "staging_location_1"` (1052 has a single staging
    location).
  - `_store_1030_setup.py` — same pattern for store 1030
    (`store_no="1030"`, `pick_across_trucks=2`,
    `capacity_max_volume_cm3 = 45000`). Two staging locations, so
    `end_anchor_alt` is left at default. Pattern is reusable: per new
    store, add a sibling `_store_<id>_setup.py`.
  - `deep_dive_{ambient,chilled,freezer,security}_1052.py` and
    `_1030.py` — thin shims that import the relevant setup helper then
    delegate to the canonical validators. No business logic duplicated.

**Data (TestStore/):**
  - `1419Orders.csv` — full per-line-item data for store 1419.
  - `1419_dist_mat_onlineaisles_core.csv` — precomputed walkable distance
    matrix (cm) between all online-aisle locations.
  - `1052Orders.csv` / `1052_dist_mat_onlineaisles_core.csv` — store 1052
    (smaller layout, single staging location).
  - `1030Orders.csv` / `1030_dist_mat_onlineaisles_core.csv` — store 1030
    (mid-tier, two staging locations).

**Reference docs:**
  - `Overviewofcurrentsystem.md` — PROD algorithm description (aisle-sequence
    greedy + greedy swapper). Use for PROD comparisons and any regression
    discussions.
  - `PickAcrossTrucks.md` — PAT semantics (staging cap with alpha priority,
    NOT a geographic prior).
  - `Trolley Goal time calculation.md` — goal-time math for the operational
    scheduler.
  - `Storelayout.png` — store floor plan.

**File-rename note:** the underlying code files keep their existing names
(`_v2.py`, `_v4.py`) for now — renaming them mass-changes import paths
across all validators. The product-level version label going forward is
"CVRP Version 1.0"; if a code file rename is wanted, do it as a single
controlled step alongside `from tote_trolley_optimizer_v2 import` updates
in all validators.

---

## SOTA comparison

Mapping CVRP v1.0 to the literature on **JOBPRP** (Joint Order Batching +
Picker Routing Problem) for rectangular cross-aisle warehouses.

### Where CVRP v1.0 sits

CVRP v1.0 is squarely in the academic SOTA family for in-store JOBPRP:

  - **Construction:** PAT-anchored greedy + two-phase trip_cost-aware picker
    — analogous to seed-based clustering then assignment in
    Scholz & Wäscher (2017) VNS.
  - **Local search:** SA portfolio (G1/G2/G3/G6/G8 + legacy) — standard
    LNS/VNS neighbourhood library.
  - **Global LNS:** G9 CP-SAT partition over K=3 trolleys — recognised
    Large Neighbourhood Search pattern in the Pisinger & Ropke (2007) sense,
    using OR-Tools as the inner solver.
  - **Multi-start:** best-of-5 SA seeds.

### Three concrete SOTA upgrades that remain viable

1. **Ratliff & Rosenthal (1983) exact picker routing.** Polynomial DP for
   rectangular cross-aisle layouts. Could replace `trip_cost`'s NN+2-opt+or-opt
   for genuinely optimal in-trolley sequencing. Expected: small (<2%) cost
   delta on top of v1.0 because 2-opt+or-opt is already near-optimal on
   this graph topology, but eliminates the residual heuristic gap and
   would lower CP-SAT objective bound for G9.

2. **Adaptive LNS weights (Pisinger & Ropke, 2007).** Replace fixed SA
   move-share (G1 30%, G2 15%, etc.) with online weight adaptation based
   on each move's recent acceptance rate. Self-tunes the portfolio per
   problem instance. Expected: 0.5-1.5% additional cost on hard cases.

3. **Branch-and-Price column generation (Briant et al., 2020) as oracle.**
   Pricing subproblem = generate improving trolley columns; master = set
   partition. Better lower bound than G9's flat CP-SAT (Briant reports
   <0.5% optimality gap). Use as offline benchmark / certificate, not
   online — single-store solve is hours, infeasible in the 10-min budget.

### 1000-site frontier — per-store configuration, not better single-store algorithm

The real frontier across 1k sites with different attributes is **algorithm
configuration**, not finding a better single-instance algorithm:

  - **SMAC (Hutter et al.)** or **irace (López-Ibáñez)** — automated
    per-store hyperparameter tuning over the StoreConfig flag/threshold
    space.
  - **Per-Instance Algorithm Selection (Kerschke & Hoos, 2019)** — train
    a classifier on store features (zone counts, aisle widths, cold-chain
    pressure) to predict which config is best.
  - **Learning-augmented algorithms (Mitzenmacher & Vassilvitskii, 2020)**
    — warm-start SA from historical solutions on the same store.

### Operational alternatives (outside the CVRP paradigm)

These change the picking *system*, not the algorithm:

  - **Goods-to-Person** — fulfilment centre with robotic shelving brings
    SKUs to a stationary picker. Eliminates picker walking entirely.
    Capex-heavy; only viable for high-volume sites.
  - **AMR-assist (autonomous mobile robots)** — robots carry the trolley,
    picker only picks. Reduces picker dead-walk; algorithm reframes around
    robot-coordination rather than single-picker-trip-cost.
  - **Zone picking with merge** — split a trolley across multiple pickers
    by store zone, merge totes at staging. Trades extra handling for
    parallelism. Algorithm becomes a job-shop scheduling problem.

### Three-way comparison

| dimension            | PROD (aisle-seq + swapper)  | CVRP v1.0 (this baseline)        | SOTA reachable                |
|----------------------|------------------------------|-----------------------------------|--------------------------------|
| objective            | aisle range minimisation     | true walk + U-turn cost           | true walk (R-R exact)          |
| trolley composition  | greedy aisle overlap         | trip_cost-aware two-phase picker  | column generation oracle       |
| polish               | post-hoc pairwise swapper    | reassign + SA (7 moves) + CP-SAT LNS | adaptive LNS weights        |
| cold-chain           | reactive split               | composition-time guard (C1)       | same                           |
| multi-store          | one-size-fits-all            | per-store flags (manual)          | SMAC/irace auto-tuned          |
| ambient store 1419   | 32,889m walk / cost 33,425   | **31,187m / cost 31,515 (-5.7%)** | est. -1-2% further (R-R+ALNS)  |

**Conclusion:** CVRP v1.0 captures the majority of the algorithmic SOTA
headroom on this problem class under PAT=3. Remaining material gains live
in: (a) PAT relaxation (business decision), (b) operational paradigm
change (Goods-to-Person, AMR), or (c) per-store auto-configuration across
the 1k-site network.

---

## Forward backlog (proposed CVRP v1.x candidates)

Ranked by ROI. Stack on top of v1.0; each must beat v1.0 on the per-zone
cost AND stay under the 10-minute runtime budget.

### Algorithmic refinements

1. ~~**Ratliff-Rosenthal exact routing** as the in-trolley sequencer.~~
   **Refuted on generalisation grounds.** R-R DP is exact only for
   strictly rectangular cross-aisle layouts with two cross-aisles. Store
   1419 Ambient aisles 1-13 fit, but every other aisle (perimeter, off-
   location barges, intermediate cross-aisles in aisles 9/10) does not.
   Per-store topology classification cannot scale across 1k sites; the
   non-rectangular fallback would have to be `trip_cost` anyway. Kept in
   the backlog only as an offline benchmark / certificate for stores
   that fit the topology exactly.

2. ~~**Adaptive LNS portfolio weights** in `_sa_refine_trolleys_spt`.~~
   **Promoted 2026-05-12** — see Adaptive LNS section in v1.0 candidate
   header. Saved -0.83% Ambient / -0.24% Chilled cost on 1419, runtime
   well under 10-min budget. Now default-ON.

3. **TID-homogeneity bias in picker seed selection** (Chilled focus).
   PROD averages 2.00 TIDs/trolley vs v1.0's 2.88 — single-TID
   composition operationally absorbs cold-chain headroom. Bias the
   alpha-anchor seed phase toward single-TID seeds for cold zones.
   Expected: closes residual cost gap vs PROD on Chilled cost-headline
   without regressing walk-per-trolley advantage. Effort: ~half day.

### Validation / generalisation

4. **Multi-store validation.** Run 3-5 stores' Easter exports through
   CVRP v1.0 and confirm the -5.4% walk / -6.0% cost saving generalises.
   This is the gate for production rollout, not an algorithmic lever.
   Status: 3/N done.
     - Store 1419 (PAT=3, 48L, two staging): -5.4% walk / -6.0% cost.
     - Store 1052 (PAT=2, 45L, single staging): -6.8% walk / -7.9% cost.
     - Store 1030 (PAT=2, 45L, two staging): -11.4% walk / -11.8% cost.
   All three stores win on every walking zone. Magnitude scales inversely
   with how "tight" PROD's solution is — 1030's PROD solution is slowest
   per trolley (211 m vs 1419's 242 m and 1052's 209 m), so v1.0 has more
   headroom to mine there. This variance is the auto-tuning signal for
   backlog item #5.

5. **Per-store auto-tuning spike (SMAC/irace).** Even a 10-store pilot of
   automated per-store flag tuning would surface which knobs matter at
   scale. Effort: ~3 days for a credible spike.

### Business / operational (outside algorithm scope)

6. **PAT relaxation 3 -> 4.** Quantify the walk saving if business allowed
   4 TIDs/trolley. Every joint construction paradigm previously refuted
   was bound by PAT=3. Run v1.0 with `pat_n=4` and report the delta to
   business.

7. **U-turn penalty calibration.** Current `UTURN_PENALTY_M = 4.0` is an
   estimate (2.88s at 1.389 m/s). Time actual U-turns in-store. If real
   penalty is higher, the optimiser will bias more toward through-traverses.

8. **Operational reframe of trolley-count savings.** v1.0 saves -8 trolleys
   vs PROD across zones — that is -8 dispatch events + -8 setup operations
   per day per store. Quantify operational $/min and re-headline the saving.

### What is explicitly NOT in the backlog

  - **Anything that pre-shapes totes via a proxy other than `trip_cost`**
    — every such attempt (bay-spread, mean-pairwise, aisle-penalty merge,
    LB-pack on Ambient, item-centroid clustering) has regressed.
    `trip_cost` is the only objective that captures aisle-entry/exit +
    intra-aisle walk + U-turn trade-offs continuously.
  - **Set-partition CP-SAT as a global replacement** — runtime breaches
    the 10-min budget and the candidate-pool bottleneck dominates.
    G9's K=3 in-SA LNS captures the win without the cost.
  - **Joint k-anchored construction (k-means style).** PAT=3 makes upfront
    seed pre-commitment fragile — every attempt produced overflow trolleys
    or composition gaps.
  - **Item-level trolley-first clustering with min-walk proxy.** Same root
    cause as the proxy ban above.

---

## Validation protocol

When running A/B for any v1.x candidate:

```
# Per-zone deep-dive run (store 1419)
cd Corefiles
python deep_dive_ambient_v4_2.py > /tmp/<step>_ambient.txt   # ~5m 30s
python deep_dive_chilled.py        > /tmp/<step>_chilled.txt # ~3m 20s
python deep_dive_freezer.py        > /tmp/<step>_freezer.txt # ~2s
python deep_dive_security.py       > /tmp/<step>_security.txt # ~2s

# Per-zone deep-dive run (store 1052 — PAT=2, 45L, single staging)
python deep_dive_ambient_1052.py   > /tmp/<step>_1052_ambient.txt   # ~4m 20s
python deep_dive_chilled_1052.py   > /tmp/<step>_1052_chilled.txt   # ~1m 45s
python deep_dive_freezer_1052.py   > /tmp/<step>_1052_freezer.txt   # ~2s
python deep_dive_security_1052.py  > /tmp/<step>_1052_security.txt  # ~2s

# Per-zone deep-dive run (store 1030 — PAT=2, 45L, two staging)
python deep_dive_ambient_1030.py   > /tmp/<step>_1030_ambient.txt   # ~3m 40s
python deep_dive_chilled_1030.py   > /tmp/<step>_1030_chilled.txt   # ~1m 05s
python deep_dive_freezer_1030.py   > /tmp/<step>_1030_freezer.txt   # ~4s
python deep_dive_security_1030.py  > /tmp/<step>_1030_security.txt  # ~2s
```

For each zone:
  - Confirm cost <= v1.0 baseline (per-zone numbers above).
  - Confirm runtime <= 10 min wall clock.
  - Inspect per-trolley histograms (aisles/totes/TIDs) for regressions.
  - For Chilled: re-verify zero cold-chain breaches and the cc_rej counter
    in the SA diagnostic line.

Promote to default-ON only if all zones pass.

---

## Environmental notes

  - **Constants** (top of `tote_trolley_optimizer_v2.py`):
    `BAY_GAP_M = 0.92`, `AISLE_GAP_M = 2.5`, `AISLE_LENGTH_M = 24.84`,
    `WALK_SPEED_SPM = 0.72`, `UTURN_PENALTY_S = 2.88`, `UTURN_PENALTY_M = 4.0`,
    `TOTE_MAX_VOLUME_CM3 = 45000`, `TOTE_MAX_WEIGHT_G = 12500`.
  - **PAT** = staging-space cap (`per-trolley distinct TIDs <= 3`), NOT a
    geographic prior. Alpha-bias seed selection lets early trolleys lean
    toward early-alpha trucks but every trolley sees the full live pool.
  - **HC11** = 1 order per tote (cross-order moves forbidden, but same-TID
    multi-order consolidation lifts this at the *physical* tote level for
    Freezer and Security only).
  - **HC12** = 30-min cold-chain cap (Chilled only).
  - **trip_cost** returns `(distance_m, uturns, sequence)`. Time-equivalent
    cost = `distance_m + 4.0 * uturns`. This is the single objective used
    everywhere (F8, picker fill, SA, CP-SAT LNS, validation).
