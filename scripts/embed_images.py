"""Use case 3 – bước 1: tạo embedding cho ảnh MRI và tạo Vector Search index.

Mỗi slice PRIMARY -> đọc file DICOM từ GridFS -> vector đặc trưng -> lưu vào images.embedding.
Sau đó tạo index "img_vec" (type vectorSearch, cosine, HNSW trong mongot) + các filter field
(patient_id, sequence, plane, series_type) để $vectorSearch có thể lọc trước.

Hai loại model:
  --model resnet18 : CNN pretrained ImageNet (torchvision), bỏ lớp fc -> 512 chiều.  (khuyên dùng)
                     cần:  pip install torch torchvision
  --model thumb    : không cần thư viện thêm. Ảnh thu nhỏ 32x32, chuẩn hoá z-score -> 1024 chiều.
                     dùng khi không cài được torch; độ "hiểu" ảnh kém hơn nhưng vẫn demo được.

Ví dụ:
    python scripts/embed_images.py --model resnet18 --limit 200     # chạy thử
    python scripts/embed_images.py --model resnet18                 # full ~40k ảnh
    python scripts/embed_images.py --model resnet18 --index-only    # chỉ tạo lại index
Chạy lại được: ảnh đã có embedding của đúng model sẽ được bỏ qua (trừ khi --redo).
"""
import argparse
import io
import sys
import time

import gridfs
import numpy as np
import pydicom
from pymongo import UpdateOne
from pymongo.operations import SearchIndexModel

from common import get_db
from usecases import dicom_to_uint8

INDEX_NAME = "img_vec"
FILTER_FIELDS = ["patient_id", "sequence", "plane", "series_type"]


# ---------- models -----------------------------------------------------------
def resize_nearest(img: np.ndarray, size: int) -> np.ndarray:
    h, w = img.shape
    ys = ((np.arange(size) + 0.5) * h / size).astype(int)
    xs = ((np.arange(size) + 0.5) * w / size).astype(int)
    return img[ys][:, xs]


class ThumbModel:
    name, dim = "thumb", 1024

    def embed(self, imgs):
        out = []
        for img in imgs:
            # nearest lên 128 rồi average-pool 4x4 -> 32x32 (giảm aliasing)
            t = resize_nearest(img, 128).astype(np.float32).reshape(32, 4, 32, 4).mean(axis=(1, 3))
            v = (t - t.mean()) / (t.std() + 1e-6)
            out.append(v.ravel())
        return np.stack(out)


class ResNetModel:
    name, dim = "resnet18", 512

    def __init__(self):
        try:
            import torch
            import torchvision
        except ImportError:
            sys.exit("Chưa cài torch. Chạy: pip install torch torchvision   (hoặc dùng --model thumb)")
        self.torch = torch
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        weights = torchvision.models.ResNet18_Weights.IMAGENET1K_V1
        m = torchvision.models.resnet18(weights=weights)
        m.fc = torch.nn.Identity()
        self.model = m.eval().to(self.device)
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=self.device).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=self.device).view(1, 3, 1, 1)
        print(f"ResNet18 trên device: {self.device}")

    def embed(self, imgs):
        torch = self.torch
        F = torch.nn.functional
        batch = []
        for img in imgs:
            t = torch.from_numpy(img).float().div(255).unsqueeze(0).unsqueeze(0)  # 1x1xHxW
            t = F.interpolate(t, size=(224, 224), mode="bilinear", align_corners=False)
            batch.append(t.repeat(1, 3, 1, 1))  # grayscale -> 3 kênh
        x = (torch.cat(batch).to(self.device) - self.mean) / self.std
        with torch.no_grad():
            return self.model(x).cpu().numpy()


def l2norm(m: np.ndarray) -> np.ndarray:
    return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-12)


# ---------- vector index -------------------------------------------------------
def ensure_index(db, dim: int):
    coll = db.images
    existing = {i["name"]: i for i in coll.list_search_indexes()}
    if INDEX_NAME in existing:
        fields = existing[INDEX_NAME].get("latestDefinition", {}).get("fields", [])
        cur_dim = next((f.get("numDimensions") for f in fields if f.get("type") == "vector"), None)
        if cur_dim == dim:
            print(f"Index '{INDEX_NAME}' đã tồn tại ({dim} chiều).")
            return wait_queryable(coll)
        print(f"Index '{INDEX_NAME}' đang {cur_dim} chiều -> drop và tạo lại {dim} chiều")
        coll.drop_search_index(INDEX_NAME)
        while any(i["name"] == INDEX_NAME for i in coll.list_search_indexes()):
            time.sleep(2)
    definition = {"fields": [{"type": "vector", "path": "embedding", "numDimensions": dim,
                              "similarity": "cosine"}]
                  + [{"type": "filter", "path": f} for f in FILTER_FIELDS]}
    coll.create_search_index(SearchIndexModel(definition=definition, name=INDEX_NAME, type="vectorSearch"))
    print(f"Đang build index '{INDEX_NAME}' ...")
    wait_queryable(coll)


def wait_queryable(coll, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        idx = next(iter(coll.list_search_indexes(INDEX_NAME)), None)
        if idx and idx.get("queryable"):
            print(f"Index sẵn sàng (status={idx.get('status')}), {time.time() - t0:.0f}s")
            return
        time.sleep(5)
    print("Index chưa queryable sau 15 phút – kiểm tra `docker compose logs mongodb`.")


# ---------- main ------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["resnet18", "thumb"], default="resnet18")
    ap.add_argument("--limit", type=int, default=0, help="chỉ xử lý N ảnh (chạy thử)")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--redo", action="store_true", help="tính lại cả ảnh đã có embedding")
    ap.add_argument("--index-only", action="store_true")
    args = ap.parse_args()

    model = ResNetModel() if args.model == "resnet18" else ThumbModel()
    db = get_db()

    if not args.index_only:
        q = {"series_type": "PRIMARY"}
        if not args.redo:
            q["embedding_model"] = {"$ne": model.name}
        ids = [d["_id"] for d in db.images.find(q, {"_id": 1}).sort("_id", 1)]
        if args.limit:
            ids = ids[: args.limit]
        print(f"Cần tính embedding cho {len(ids)} ảnh (model={model.name}, dim={model.dim})")

        bucket = gridfs.GridFSBucket(db, bucket_name="mri")
        t0, done, failed = time.time(), 0, 0
        for i in range(0, len(ids), args.batch):
            docs = list(db.images.find({"_id": {"$in": ids[i: i + args.batch]}}, {"file_id": 1}))
            imgs, keep = [], []
            for d in docs:
                try:
                    ds = pydicom.dcmread(io.BytesIO(bucket.open_download_stream(d["file_id"]).read()), force=True)
                    imgs.append(dicom_to_uint8(ds))
                    keep.append(d["_id"])
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    if failed <= 3:
                        print(f"  lỗi đọc {d['_id']}: {e}")
            if not imgs:
                continue
            vecs = l2norm(model.embed(imgs).astype(np.float64))
            db.images.bulk_write([
                UpdateOne({"_id": _id}, {"$set": {"embedding": v.tolist(), "embedding_model": model.name}})
                for _id, v in zip(keep, vecs)
            ], ordered=False)
            done += len(keep)
            el = time.time() - t0
            if (i // args.batch) % 10 == 0 or done == len(ids):
                eta = el / done * (len(ids) - done) if done else 0
                print(f"  {done}/{len(ids)}  {done / el:.0f} ảnh/s  ETA {eta / 60:.1f} phút")
        print(f"Xong: {done} ảnh, lỗi {failed}, {time.time() - t0:.0f}s")

    ensure_index(db, model.dim)
    total = db.images.count_documents({"embedding_model": model.name})
    print(f"Tổng ảnh có embedding ({model.name}): {total}")


if __name__ == "__main__":
    main()
