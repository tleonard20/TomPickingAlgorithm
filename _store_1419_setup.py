"""Store 1419 setup shim — side-effect import.

Monkey-patches v2 + v4 with 1419-specific paths pointing to the repo root.
StoreConfig defaults are already 1419 (PAT=3, 48L, two staging locations)
so no StoreConfig patch is needed.

On first run:
  - Extracts 1419_dist_mat_onlineaisles_core.zip -> 1419_dist_mat_onlineaisles_core.csv
  - Converts 1419.xlsx -> 1419Orders.csv

Subsequent runs reuse the cached files.

Usage:
    import _store_1419_setup  # must be first import in the script
"""
from __future__ import annotations

import csv as _csv
import os
import sys
import zipfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

import tote_trolley_optimizer_v2 as v2
import tote_trolley_optimizer_v4 as v4

_ORDERS_CSV = os.path.join(THIS_DIR, "1419Orders.csv")
_DIST_MATRIX_CSV = os.path.join(THIS_DIR, "1419_dist_mat_onlineaisles_core.csv")


def _ensure_dist_matrix() -> None:
    if os.path.exists(_DIST_MATRIX_CSV):
        return
    zip_path = os.path.join(THIS_DIR, "1419_dist_mat_onlineaisles_core.zip")
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"Dist matrix zip not found: {zip_path}")
    print(f"[setup-1419] Extracting dist matrix from zip ...", flush=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(THIS_DIR)
    print(f"[setup-1419] Extracted -> {_DIST_MATRIX_CSV}", flush=True)


def _ensure_orders_csv() -> None:
    if os.path.exists(_ORDERS_CSV):
        return
    src = os.path.join(THIS_DIR, "1419.xlsx")
    if not os.path.exists(src):
        raise FileNotFoundError(f"Source xlsx not found: {src}")
    try:
        import openpyxl
    except ImportError:
        raise ImportError("openpyxl required for xlsx conversion: pip install openpyxl")

    print(f"[setup-1419] Converting {src} -> {_ORDERS_CSV} ...", flush=True)
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    col = {name: i for i, name in enumerate(header) if name is not None}

    out_fields = [
        "Order_NBR", "LineNumber", "StockCode", "MaxOutTote", "Splittable",
        "IsSpecialPick", "PickingType", "Ordered_Qty", "quantity",
        "UnitWeightGrams", "UnitVolumeCubicCm", "PickingZone",
        "Aisle_Location", "Bay_Location", "Ailse_Bay_Concat",
        "TransitID", "Truck", "TrolleyID", "TrayHeaderID",
        "DeliveryStartDateTime_local",
    ]

    def _int(v):
        try:
            return str(int(float(v))) if v is not None else ""
        except (ValueError, TypeError):
            return str(v) if v is not None else ""

    def _bool(v):
        if v is True or str(v).strip().upper() in ("TRUE", "1", "YES"):
            return "TRUE"
        return "FALSE"

    def _str(v):
        return str(v).strip() if v is not None else ""

    with open(_ORDERS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for row in rows:
            a = _int(row[col["Aisle_Location"]])
            b = _int(row[col["Bay_Location"]])
            writer.writerow({
                "Order_NBR": _int(row[col["Order_NBR"]]),
                "LineNumber": _int(row[col["LineNumber"]]),
                "StockCode": _int(row[col["StockCode"]]),
                "MaxOutTote": _bool(row[col["MaxOutTote"]]),
                "Splittable": _bool(row[col["Splittable"]]),
                "IsSpecialPick": _bool(row[col["IsSpecialPick"]]),
                "PickingType": _str(row[col["PickingType"]]) or "Normal",
                "Ordered_Qty": _str(row[col["Ordered_Qty"]]),
                "quantity": _str(row[col["quantity"]]),
                "UnitWeightGrams": _str(row[col["UnitWeightGrams"]]),
                "UnitVolumeCubicCm": _str(row[col["UnitVolumeCubicCm"]]),
                "PickingZone": _str(row[col["PickingZone"]]),
                "Aisle_Location": a,
                "Bay_Location": b,
                "Ailse_Bay_Concat": f"{a}_{b}" if a and b else "",
                "TransitID": _str(row[col["Truck"]]),
                "Truck": _str(row[col["Truck"]]),
                "TrolleyID": _int(row[col["TrolleyID"]]),
                "TrayHeaderID": _int(row[col["TrayHeaderID"]]),
                "DeliveryStartDateTime_local": _str(row[col["DeliveryStartDateTime_local"]]),
            })
    wb.close()
    print(f"[setup-1419] Done.", flush=True)


_ensure_dist_matrix()
_ensure_orders_csv()

v2.ORDERS_CSV = _ORDERS_CSV
v2.DIST_MATRIX_CSV = _DIST_MATRIX_CSV
v4.ORDERS_CSV = _ORDERS_CSV
v4.DIST_MATRIX_CSV = _DIST_MATRIX_CSV
# StoreConfig unchanged — defaults are already 1419
