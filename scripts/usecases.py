"""Use case 1 & 2 của assignment.

UC1 - Cho patient ID -> lấy text (notes) + ảnh
    python scripts/usecases.py uc1 --patient 0001
    python scripts/usecases.py uc1 --patient 0001 --export 4      # xuất 4 ảnh PNG / series

UC2 - Cho patient ID + điều kiện (vd T2 sagittal) -> lấy text + ảnh thoả điều kiện
    python scripts/usecases.py uc2 --patient 0001 --sequence T2 --plane SAGITTAL
    python scripts/usecases.py uc2 --patient 0001 --sequence T2 --plane SAGITTAL --export 5 --explain
    python scripts/usecases.py uc2 --patient 0001 --sequence T1 --plane AXIAL --finding disc_bulge

--explain : in execution stats (stage, keys/docs examined, thời gian) -> dùng cho demo index/query.
Ảnh PNG được ghi vào thư mục out/<patient>/.
"""
import argparse
import io
import struct
import sys
import time
import zlib
from pathlib import Path

import gridfs
import numpy as np
import pydicom

from common import get_db

OUT_DIR = Path(__file__).resolve().parent.parent / "out"


# ---------- image helpers ----------------------------------------------------
def write_png(path: Path, img: np.ndarray):
    """Ghi ảnh grayscale uint8 ra PNG (không cần Pillow)."""
    h, w = img.shape
    raw = b"".join(b"\x00" + img[i].tobytes() for i in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def _first(v):
    if v is None:
        return None
    try:
        return float(v[0])
    except (TypeError, IndexError):
        return float(v)


def dicom_to_uint8(ds) -> np.ndarray:
    arr = ds.pixel_array.astype(np.float32)
    arr = arr * float(getattr(ds, "RescaleSlope", 1) or 1) + float(getattr(ds, "RescaleIntercept", 0) or 0)
    wc, ww = _first(getattr(ds, "WindowCenter", None)), _first(getattr(ds, "WindowWidth", None))
    if wc is not None and ww:
        lo, hi = wc - ww / 2, wc + ww / 2
    else:
        lo, hi = np.percentile(arr, [1, 99])
    out = np.clip((arr - lo) / max(hi - lo, 1e-6), 0, 1) * 255
    return out.astype(np.uint8)


def pick_evenly(items, n):
    if n <= 0 or not items:
        return []
    if n >= len(items):
        return items
    idx = np.linspace(0, len(items) - 1, n).round().astype(int)
    return [items[i] for i in sorted(set(idx))]


def export_pngs(db, images, per_series: int, patient_id: str):
    bucket = gridfs.GridFSBucket(db, bucket_name="mri")
    outdir = OUT_DIR / patient_id
    outdir.mkdir(parents=True, exist_ok=True)
    by_series = {}
    for im in images:
        by_series.setdefault(im["series_folder"], []).append(im)
    written = []
    for series, ims in by_series.items():
        for im in pick_evenly(ims, per_series):
            data = bucket.open_download_stream(im["file_id"]).read()
            ds = pydicom.dcmread(io.BytesIO(data), force=True)
            path = outdir / f"{series}_{im.get('instance_number') or 0:03d}.png"
            write_png(path, dicom_to_uint8(ds))
            written.append(path)
    return written


# ---------- explain helper --------------------------------------------------
def _stages(node, acc):
    if isinstance(node, dict):
        if "stage" in node:
            acc.append(node["stage"])
        for k, v in node.items():
            if k in ("inputStage", "inputStages", "queryPlan", "winningPlan", "outerStage", "innerStage"):
                _stages(v, acc)
    elif isinstance(node, list):
        for x in node:
            _stages(x, acc)
    return acc


def print_explain(db, filt, sort):
    exp = db.command("explain", {"find": "images", "filter": filt, "sort": sort}, verbosity="executionStats")
    es = exp["executionStats"]
    plan = _stages(exp["queryPlanner"]["winningPlan"], [])
    print("\n--- explain (executionStats) ---")
    print(f"winning plan     : {' <- '.join(plan)}")
    print(f"nReturned        : {es['nReturned']}")
    print(f"keysExamined     : {es['totalKeysExamined']}")
    print(f"docsExamined     : {es['totalDocsExamined']}")
    print(f"executionTimeMs  : {es['executionTimeMillis']}")
    print(f"rejected plans   : {len(exp['queryPlanner'].get('rejectedPlans', []))}")


# ---------- printing --------------------------------------------------------
def print_patient(p):
    print(f"\n=== Patient {p['_id']} | sex={p.get('sex')} | images={p.get('image_count', 0)} ===")
    print("Notes:")
    print("  " + (p.get("notes") or "(không có notes)").replace("\n", "\n  "))
    print(f"Levels  : {p.get('levels')}")
    print(f"Findings: {p.get('findings')}")
    for st in p.get("studies", []):
        print(f"\nStudy {st['date']}  (age {st.get('age')})  {st['name']}")
        for s in st["series"]:
            print(f"  - {s['name']:<32} {s['sequence']:<5} {s['plane']:<9} {s['slices']:>3} slices")


def print_images(images, limit=8):
    print(f"\nImages trả về: {len(images)}")
    for im in images[:limit]:
        print(f"  {im['series_folder']:<28} #{im.get('instance_number')!s:<3} "
              f"{im['sequence']} {im['plane']:<9} slice_loc={im.get('slice_location')}  file_id={im['file_id']}")
    if len(images) > limit:
        print(f"  ... (+{len(images) - limit})")


# ---------- use cases -------------------------------------------------------
IMAGE_FIELDS = {"series_folder": 1, "sequence": 1, "plane": 1, "instance_number": 1,
                "slice_location": 1, "file_id": 1}


def uc1(db, args):
    """Một aggregation: patients JOIN images bằng $lookup."""
    pipeline = [
        {"$match": {"_id": args.patient}},
        {"$lookup": {
            "from": "images",
            "localField": "_id",
            "foreignField": "patient_id",
            "as": "images",
            "pipeline": [
                {"$match": {"series_type": "PRIMARY"}},
                {"$sort": {"series_folder": 1, "instance_number": 1}},
                {"$project": IMAGE_FIELDS},
            ],
        }},
    ]
    t = time.perf_counter()
    res = list(db.patients.aggregate(pipeline))
    ms = (time.perf_counter() - t) * 1000
    if not res:
        sys.exit(f"Không tìm thấy patient {args.patient}")
    p = res[0]
    print_patient(p)
    print_images(p["images"])
    print(f"\nQuery time: {ms:.1f} ms")
    return p["images"]


def uc2(db, args):
    """2 query: lấy patient (text) + find images theo điều kiện."""
    p = db.patients.find_one({"_id": args.patient}, {"studies": 0})
    if not p:
        sys.exit(f"Không tìm thấy patient {args.patient}")
    if args.finding and args.finding not in p.get("findings", []):
        print(f"Patient {args.patient} KHÔNG có finding '{args.finding}' -> không trả ảnh.")
        print(f"Findings của patient: {p.get('findings')}")
        return []

    filt = {"patient_id": args.patient, "series_type": "PRIMARY"}
    if args.sequence:
        filt["sequence"] = args.sequence.upper()
    if args.plane:
        filt["plane"] = args.plane.upper()
    sort = {"series_folder": 1, "instance_number": 1}

    t = time.perf_counter()
    images = list(db.images.find(filt, IMAGE_FIELDS).sort(list(sort.items())))
    ms = (time.perf_counter() - t) * 1000

    print(f"\n=== Patient {p['_id']} ===")
    print("Notes:\n  " + (p.get("notes") or "(không có notes)").replace("\n", "\n  "))
    print(f"Findings: {p.get('findings')}")
    print(f"\nFilter: {filt}")
    print_images(images)
    print(f"\nQuery time: {ms:.1f} ms")
    if args.explain:
        print_explain(db, filt, sort)
    return images


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("case", choices=["uc1", "uc2"])
    ap.add_argument("--patient", required=True, help="vd 0001 hoặc 1")
    ap.add_argument("--sequence", choices=["T1", "T2", "PD", "t1", "t2", "pd"])
    ap.add_argument("--plane", choices=["SAGITTAL", "AXIAL", "CORONAL", "sagittal", "axial", "coronal"])
    ap.add_argument("--finding", help="điều kiện trên notes, vd disc_bulge, annular_tear")
    ap.add_argument("--export", type=int, default=0, help="xuất N ảnh PNG / series")
    ap.add_argument("--explain", action="store_true")
    args = ap.parse_args()
    args.patient = args.patient.zfill(4)

    db = get_db()
    images = uc1(db, args) if args.case == "uc1" else uc2(db, args)
    if args.export and images:
        files = export_pngs(db, images, args.export, args.patient)
        print(f"\nĐã xuất {len(files)} PNG vào {OUT_DIR / args.patient}")


if __name__ == "__main__":
    main()
