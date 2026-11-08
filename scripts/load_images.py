"""ETL: 01_MRI_Data (file .ima DICOM) -> MongoDB.

Cấu trúc dataset:
    01_MRI_Data/<patient 0001..0575>/<study L-SPINE_LSS_YYYYMMDD_hhmmss_xxx>/<series T2_TSE_SAG_384_0002>/*.ima
    515 patient, 48.345 slice (~13GB), 477 patient có 1 study, 38 patient có 2-3 study.

Header DICOM đã ẩn danh: PatientID / StudyDate rỗng -> lấy patient_id từ thư mục cấp 1,
study_date từ tên thư mục study. SeriesDescription dạng "t2_tse_sag_384", "PosDisp: [5] t1_tse_tra", "localizer".

Mỗi slice:
    file .ima gốc -> GridFS bucket "mri" (mri.chunks được nén zstd, xem init_db.py)
    metadata      -> collection `images`, _id = SOPInstanceUID (chạy lại không bị trùng)
Cuối cùng gộp thông tin study/series vào `patients.studies` bằng aggregation + $merge.

Mặc định chỉ nạp series PRIMARY (T1/T2/PD gốc, ~41k slice, ~9.7GB thô).
LOCALIZER (ảnh định vị, ~2.9GB) và DERIVED (POSDISP / NEW_SERIES, ~0.9GB) bỏ qua trừ khi --include-all.

    python scripts/load_images.py --root "$HOME/Downloads/01_MRI_Data" --limit-patients 3 --dry-run
    python scripts/load_images.py --root "$HOME/Downloads/01_MRI_Data" --limit-patients 3
    python scripts/load_images.py --root "$HOME/Downloads/01_MRI_Data"
"""
import argparse
import hashlib
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pydicom

BATCH = 200
STUDY_DATE_RE = re.compile(r"_(\d{8})_\d{6}")


# ---------- classification ---------------------------------------------------
def _str(v):
    return None if v is None or str(v).strip() == "" else str(v).strip()


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def series_type(desc: str, image_type) -> str:
    d = (desc or "").upper()
    it = [str(x).upper() for x in (image_type or [])]
    if "LOCALIZER" in d:
        return "LOCALIZER"
    if "DERIVED" in it or "SECONDARY" in it or "POSDISP" in d:
        return "DERIVED"
    return "PRIMARY"


def detect_sequence(desc: str) -> str:
    d = (desc or "").upper()
    if re.search(r"(^|[^A-Z])T1", d):
        return "T1"
    if re.search(r"(^|[^A-Z])T2", d):
        return "T2"  # gồm cả TIRM/STIR (T2 + xoá mỡ)
    if re.search(r"(^|[^A-Z])PD", d):
        return "PD"
    return "OTHER"


def detect_plane(desc: str, iop) -> str:
    # 1) Ưu tiên ImageOrientationPatient: pháp tuyến mặt cắt = row x col
    if iop is not None and len(iop) == 6:
        normal = np.cross(np.array(iop[:3], float), np.array(iop[3:], float))
        if np.linalg.norm(normal) > 0:
            return ["SAGITTAL", "CORONAL", "AXIAL"][int(np.argmax(np.abs(normal)))]
    # 2) Fallback theo tên series
    d = (desc or "").upper()
    if "SAG" in d:
        return "SAGITTAL"
    if "TRA" in d or "AX" in d:
        return "AXIAL"
    if "COR" in d:
        return "CORONAL"
    return "UNKNOWN"


def parse_age(v):
    m = re.match(r"(\d+)\s*Y", str(v or ""), re.I)
    return int(m.group(1)) if m else None


def study_date_from_folder(name: str):
    m = STUDY_DATE_RE.search(name)
    if not m:
        return None
    s = m.group(1)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


# ---------- io ----------------------------------------------------------------
def iter_files(root: Path):
    for dirpath, dirnames, files in os.walk(root):
        dirnames.sort()  # duyệt theo thứ tự patient 0001, 0002, ...
        for f in sorted(files):
            if f.lower().endswith((".ima", ".dcm")):
                yield Path(dirpath) / f


def read_header(path: Path):
    try:
        return pydicom.dcmread(path, stop_before_pixels=True, force=True)
    except Exception:
        return None


def build_doc(path: Path, root: Path, ds):
    rel_parts = path.relative_to(root).parts  # (patient, study, series, file)
    if len(rel_parts) < 4:
        return None
    patient, study, series_folder = rel_parts[0], rel_parts[1], rel_parts[2]
    desc = _str(getattr(ds, "SeriesDescription", None)) or series_folder
    iop = getattr(ds, "ImageOrientationPatient", None)
    desc_u = desc.upper()
    rel = "/".join(rel_parts)
    return {
        "_id": _str(getattr(ds, "SOPInstanceUID", None)) or hashlib.sha1(rel.encode()).hexdigest(),
        "patient_id": patient.zfill(4),
        "study_name": study,
        "study_uid": _str(getattr(ds, "StudyInstanceUID", None)),
        "study_date": study_date_from_folder(study),
        "series_folder": series_folder,
        "series_uid": _str(getattr(ds, "SeriesInstanceUID", None)),
        "series_number": _int(getattr(ds, "SeriesNumber", None)),
        "series_description": desc,
        "series_type": series_type(desc, getattr(ds, "ImageType", None)),
        "sequence": detect_sequence(desc),
        "plane": detect_plane(desc, list(iop) if iop is not None else None),
        "fat_suppressed": bool(re.search(r"_FS|TIRM|STIR", desc_u)),
        "contrast": "_CM" in desc_u,
        "instance_number": _int(getattr(ds, "InstanceNumber", None)),
        "slice_location": _float(getattr(ds, "SliceLocation", None)),
        "slice_thickness": _float(getattr(ds, "SliceThickness", None)),
        "rows": _int(getattr(ds, "Rows", None)),
        "columns": _int(getattr(ds, "Columns", None)),
        "pixel_spacing": [float(x) for x in getattr(ds, "PixelSpacing", [])] or None,
        "field_strength": _float(getattr(ds, "MagneticFieldStrength", None)),
        "patient_sex": _str(getattr(ds, "PatientSex", None)),
        "patient_age": parse_age(getattr(ds, "PatientAge", None)),
        "source_path": rel,
    }


# ---------- summary into patients ---------------------------------------------
SUMMARY_PIPELINE = [
    {"$group": {
        "_id": {"patient_id": "$patient_id", "study_name": "$study_name", "series_folder": "$series_folder"},
        "study_date": {"$first": "$study_date"},
        "age": {"$first": "$patient_age"},
        "sex": {"$first": "$patient_sex"},
        "sequence": {"$first": "$sequence"},
        "plane": {"$first": "$plane"},
        "series_type": {"$first": "$series_type"},
        "slices": {"$sum": 1},
    }},
    {"$sort": {"_id.series_folder": 1}},
    {"$group": {
        "_id": {"patient_id": "$_id.patient_id", "study_name": "$_id.study_name"},
        "study_date": {"$first": "$study_date"},
        "age": {"$first": "$age"},
        "sex": {"$first": "$sex"},
        "series": {"$push": {"name": "$_id.series_folder", "sequence": "$sequence", "plane": "$plane",
                             "series_type": "$series_type", "slices": "$slices"}},
        "slices": {"$sum": "$slices"},
    }},
    {"$sort": {"study_date": 1}},
    {"$group": {
        "_id": "$_id.patient_id",
        "sex": {"$first": "$sex"},
        "studies": {"$push": {"name": "$_id.study_name", "date": "$study_date", "age": "$age",
                              "slices": "$slices", "series": "$series"}},
        "image_count": {"$sum": "$slices"},
    }},
    {"$merge": {"into": "patients", "on": "_id", "whenMatched": "merge", "whenNotMatched": "insert"}},
]


# ---------- main ------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="thư mục 01_MRI_Data")
    ap.add_argument("--limit-patients", type=int, default=0, help="chỉ nạp N patient đầu (0 = tất cả)")
    ap.add_argument("--include-all", action="store_true", help="nạp cả LOCALIZER và DERIVED")
    ap.add_argument("--dry-run", action="store_true", help="chỉ đọc header + thống kê, không ghi DB")
    args = ap.parse_args()

    root = Path(os.path.expanduser(args.root)).resolve()
    if not root.is_dir():
        sys.exit(f"Không thấy thư mục: {root}")

    if not args.dry_run:
        import gridfs
        from pymongo.errors import BulkWriteError

        from common import get_db

        db = get_db()
        fs = gridfs.GridFS(db, collection="mri")

    seen_patients = []
    stats = Counter()
    combos = Counter()
    batch = []
    t0 = time.time()

    def flush():
        if not batch:
            return
        ids = [d["_id"] for d, _ in batch]
        existing = {d["_id"] for d in db.images.find({"_id": {"$in": ids}}, {"_id": 1})}
        docs = []
        for doc, path in batch:
            if doc["_id"] in existing:
                stats["skipped_existing"] += 1
                continue
            with open(path, "rb") as fh:
                doc["file_id"] = fs.put(fh, filename=doc["source_path"],
                                        metadata={"patient_id": doc["patient_id"], "image_id": doc["_id"]})
            stats["bytes"] += path.stat().st_size
            docs.append(doc)
        if docs:
            try:
                db.images.insert_many(docs, ordered=False)
                stats["inserted"] += len(docs)
            except BulkWriteError as e:
                stats["inserted"] += e.details.get("nInserted", 0)
                stats["write_errors"] += len(e.details.get("writeErrors", []))
                print("  write error ví dụ:", e.details["writeErrors"][0].get("errmsg"))
        batch.clear()

    for path in iter_files(root):
        pid = path.relative_to(root).parts[0].zfill(4)
        if pid not in seen_patients:
            if args.limit_patients and len(seen_patients) >= args.limit_patients:
                break  # duyệt theo thứ tự nên có thể dừng luôn
            seen_patients.append(pid)
        stats["files"] += 1
        ds = read_header(path)
        doc = build_doc(path, root, ds) if ds is not None else None
        if doc is None:
            stats["unreadable"] += 1
            continue
        combos[(doc["series_type"], doc["sequence"], doc["plane"])] += 1
        if doc["series_type"] != "PRIMARY" and not args.include_all:
            stats["skipped_type"] += 1
            continue
        stats["selected"] += 1
        if not args.dry_run:
            batch.append((doc, path))
            if len(batch) >= BATCH:
                flush()
        if stats["files"] % 2000 == 0:
            el = time.time() - t0
            print(f"  ... {stats['files']} files, {len(seen_patients)} patients, {el:.0f}s")

    if not args.dry_run:
        flush()
        print("Gộp study/series vào patients ...")
        db.images.aggregate(SUMMARY_PIPELINE)

    print("\n=== Summary ===")
    print(f"patients: {len(seen_patients)}  (vd: {seen_patients[:5]})")
    for k in ("files", "selected", "skipped_type", "unreadable", "inserted", "skipped_existing", "write_errors"):
        print(f"{k}: {stats[k]}")
    if stats["bytes"]:
        print(f"bytes uploaded: {stats['bytes']/1e9:.2f} GB")
    print("\n(series_type, sequence, plane) -> số slice:")
    for k, v in combos.most_common(20):
        print(f"  {k}: {v}")
    print(f"\nTime: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
