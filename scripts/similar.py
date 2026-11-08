"""Use case 3 – bước 2: tìm ảnh MRI tương tự 1 ảnh của patient cho trước ($vectorSearch).

Ảnh truy vấn mặc định = lát giữa của series (sequence, plane) của patient đó.
Kết quả lọc cùng sequence + plane, khác patient, mỗi patient lấy 1 ảnh tốt nhất.

    python scripts/similar.py --patient 0001                                  # T2 sagittal, top 5
    python scripts/similar.py --patient 0001 --sequence T1 --plane AXIAL --k 8
    python scripts/similar.py --patient 0001 --exact                          # ENN (quét hết) để so với ANN
    python scripts/similar.py --patient 0001 --export                         # xuất ảnh ghép out/similar_0001.png
"""
import argparse
import io
import sys
import time

import gridfs
import numpy as np
import pydicom

from common import get_db
from embed_images import INDEX_NAME, resize_nearest
from usecases import OUT_DIR, dicom_to_uint8, write_png


def load_png_tile(bucket, file_id, size=256):
    ds = pydicom.dcmread(io.BytesIO(bucket.open_download_stream(file_id).read()), force=True)
    return resize_nearest(dicom_to_uint8(ds), size)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patient", required=True)
    ap.add_argument("--sequence", default="T2", choices=["T1", "T2", "PD"])
    ap.add_argument("--plane", default="SAGITTAL", choices=["SAGITTAL", "AXIAL", "CORONAL"])
    ap.add_argument("--slice", type=int, help="instance_number của ảnh truy vấn (mặc định: lát giữa)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--exact", action="store_true", help="exact nearest neighbour thay vì HNSW (ANN)")
    ap.add_argument("--same-patient", action="store_true", help="cho phép trả ảnh của chính patient đó")
    ap.add_argument("--export", action="store_true")
    args = ap.parse_args()
    pid = args.patient.zfill(4)
    db = get_db()

    series = list(db.images.find(
        {"patient_id": pid, "series_type": "PRIMARY", "sequence": args.sequence, "plane": args.plane,
         "embedding": {"$exists": True}},
        {"embedding": 1, "instance_number": 1, "series_folder": 1, "file_id": 1, "embedding_model": 1},
    ).sort("instance_number", 1))
    if not series:
        sys.exit(f"Patient {pid} không có ảnh {args.sequence} {args.plane} đã có embedding "
                 f"(chạy embed_images.py trước).")
    if args.slice:
        query = next((s for s in series if s.get("instance_number") == args.slice), None)
        if not query:
            sys.exit(f"Không có slice #{args.slice}. Có: {[s.get('instance_number') for s in series]}")
    else:
        query = series[len(series) // 2]
    print(f"Ảnh truy vấn: patient {pid}  {query['series_folder']} #{query.get('instance_number')}  "
          f"(model={query.get('embedding_model')}, dim={len(query['embedding'])})")

    vfilter = {"sequence": args.sequence, "plane": args.plane, "series_type": "PRIMARY"}
    if not args.same_patient:
        vfilter["patient_id"] = {"$ne": pid}
    fetch = args.k * 10  # lấy dư rồi gom mỗi patient 1 ảnh
    vs = {"index": INDEX_NAME, "path": "embedding", "queryVector": query["embedding"],
          "filter": vfilter, "limit": fetch}
    if args.exact:
        vs["exact"] = True
    else:
        vs["numCandidates"] = max(fetch * 10, 200)

    pipeline = [
        {"$vectorSearch": vs},
        {"$project": {"patient_id": 1, "series_folder": 1, "instance_number": 1, "file_id": 1,
                      "score": {"$meta": "vectorSearchScore"}}},
    ]
    t = time.perf_counter()
    hits = list(db.images.aggregate(pipeline))
    ms = (time.perf_counter() - t) * 1000

    results, seen = [], set()
    for h in hits:
        if args.same_patient or h["patient_id"] not in seen:
            seen.add(h["patient_id"])
            results.append(h)
        if len(results) == args.k:
            break

    notes = {p["_id"]: p for p in db.patients.find({"_id": {"$in": [r["patient_id"] for r in results] + [pid]}},
                                                   {"findings": 1})}
    mode = "EXACT (ENN)" if args.exact else f"ANN/HNSW numCandidates={vs['numCandidates']}"
    print(f"$vectorSearch [{mode}] -> {len(hits)} hits, {ms:.1f} ms\n")
    print(f"Query  {pid}: findings={notes.get(pid, {}).get('findings')}")
    for i, r in enumerate(results, 1):
        print(f"#{i} score={r['score']:.4f}  patient {r['patient_id']}  {r['series_folder']} "
              f"#{r.get('instance_number')}  findings={notes.get(r['patient_id'], {}).get('findings')}")

    if args.export:
        bucket = gridfs.GridFSBucket(db, bucket_name="mri")
        tiles = [load_png_tile(bucket, query["file_id"])] + [load_png_tile(bucket, r["file_id"]) for r in results]
        sep = np.full((256, 8), 255, np.uint8)
        row = np.hstack([x for tile in tiles for x in (tile, sep)][:-1])
        OUT_DIR.mkdir(exist_ok=True)
        path = OUT_DIR / f"similar_{pid}_{args.sequence}_{args.plane}.png"
        write_png(path, row)
        print(f"\nẢnh ghép (trái nhất = ảnh truy vấn): {path}")


if __name__ == "__main__":
    main()
