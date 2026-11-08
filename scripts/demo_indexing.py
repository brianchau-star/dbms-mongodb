"""Demo tiêu chí 1 – "Show how DBMS works with indexing" (so sánh lý thuyết Chương 2 vs MongoDB).

Mỗi experiment độc lập (tự drop / tạo index nó cần), in bảng số liệu, ghi:
    results/indexing_results.csv     – 1 dòng / phép đo (dán vào report)
    results/explain/<exp>_<case>.json – explain đầy đủ (phụ lục / chụp màn hình)

    python scripts/demo_indexing.py list                 # danh sách experiment
    python scripts/demo_indexing.py exp1                 # chạy 1 experiment
    python scripts/demo_indexing.py all                  # chạy tất cả (exp1..exp11) rồi tạo bộ index khuyến nghị
    python scripts/demo_indexing.py make-big --factor 10 # tạo images_big (~400k doc) cho thí nghiệm "big data"
    python scripts/demo_indexing.py exp1 --coll images_big
    python scripts/demo_indexing.py reset                # xoá index/collection do demo tạo ra
"""
import argparse
import csv
import json
import math
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

from bson import json_util

from common import get_db

ROOT = Path(__file__).resolve().parent.parent
RES_DIR = ROOT / "results"
CSV_PATH = RES_DIR / "indexing_results.csv"
PATIENT = "0001"
NO_EMB = {"embedding": 0}
SCRATCH = ["bench_index_cost", "bench_src", "images_plain", "images_clustered"]

EXPERIMENTS = {}


def experiment(key, title, theory):
    def deco(fn):
        EXPERIMENTS[key] = (title, theory, fn)
        return fn
    return deco


# =============================================================================
# helpers
# =============================================================================
def banner(key):
    title, theory, _ = EXPERIMENTS[key]
    print("\n" + "=" * 100)
    print(f"{key.upper()} – {title}")
    print(f"Lý thuyết (Chương 2): {theory}")
    print("=" * 100)


def drop_secondary(coll):
    for name in list(coll.index_information()):
        if name != "_id_":
            coll.drop_index(name)


def create_index(coll, keys, **kw):
    t = time.perf_counter()
    name = coll.create_index(keys, **kw)
    ms = (time.perf_counter() - t) * 1000
    print(f"  + createIndex {name:<28} {str(keys):<70} {ms:8.0f} ms")
    return name


def _walk(node, key, acc):
    if isinstance(node, dict):
        if key in node:
            acc.append(node[key])
        for v in node.values():
            _walk(v, key, acc)
    elif isinstance(node, list):
        for x in node:
            _walk(x, key, acc)
    return acc


def plan_stages(winning):
    """Trả về chuỗi stage từ dưới lên, vd 'IXSCAN -> FETCH -> SORT'."""
    acc = []

    def rec(n):
        if isinstance(n, dict):
            if "stage" in n:
                acc.append(n["stage"])
            for k in ("queryPlan", "inputStage", "outerStage", "innerStage"):
                if k in n:
                    rec(n[k])
            for x in n.get("inputStages", []):
                rec(x)

    rec(winning)
    return " -> ".join(reversed(acc))


def explain_find(coll, filt, sort=None, proj=None, hint=None):
    cmd = {"find": coll.name, "filter": filt}
    if sort:
        cmd["sort"] = sort
    if proj:
        cmd["projection"] = proj
    if hint:
        cmd["hint"] = hint
    exp = coll.database.command("explain", cmd, verbosity="executionStats")
    es = exp["executionStats"]
    win = exp["queryPlanner"]["winningPlan"]
    return {
        "plan": plan_stages(win),
        "index": ",".join(sorted(set(_walk(win, "indexName", [])))) or "-",
        "keys": es["totalKeysExamined"],
        "docs": es["totalDocsExamined"],
        "returned": es["nReturned"],
        "exec_ms": es["executionTimeMillis"],
        "rejected": len(exp["queryPlanner"].get("rejectedPlans", [])),
        "multikey": any(_walk(win, "isMultiKey", [])),
        "_raw": exp,
    }


def wall_ms(coll, filt, sort=None, proj=None, hint=None, runs=7):
    ts = []
    for _ in range(runs):
        cur = coll.find(filt, proj or NO_EMB)
        if sort:
            cur = cur.sort(list(sort.items()))
        if hint:
            cur = cur.hint(hint if isinstance(hint, str) else list(hint.items()))
        t = time.perf_counter()
        list(cur)
        ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts[1:])  # bỏ lần đầu (warm-up)


ROWS = []


def measure(exp, case, coll, filt, sort=None, proj=None, hint=None, note="", timing=True):
    r = explain_find(coll, filt, sort, proj, hint)
    r["wall_ms"] = wall_ms(coll, filt, sort, proj, hint) if timing else None
    row = {"time": datetime.now().isoformat(timespec="seconds"), "exp": exp, "case": case,
           "collection": coll.name, "filter": json_util.dumps(filt), "hint": json_util.dumps(hint) if hint else "",
           **{k: r[k] for k in ("plan", "index", "keys", "docs", "returned", "exec_ms", "rejected", "multikey")},
           "wall_ms_median": round(r["wall_ms"], 2) if r["wall_ms"] is not None else "", "note": note}
    ROWS.append(row)
    (RES_DIR / "explain").mkdir(parents=True, exist_ok=True)
    (RES_DIR / "explain" / f"{exp}_{case}.json").write_text(json_util.dumps(r["_raw"], indent=2))
    return dict(row)


def print_table(rows, cols):
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  " + " | ".join(c.ljust(widths[c]) for c in cols))
    print("  " + "-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  " + " | ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def show(exp_rows):
    print()
    print_table(exp_rows, ["case", "plan", "index", "keys", "docs", "returned", "exec_ms", "wall_ms_median", "note"])


def flush_csv():
    if not ROWS:
        return
    RES_DIR.mkdir(exist_ok=True)
    new = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ROWS[0].keys()))
        if new:
            w.writeheader()
        w.writerows(ROWS)
    print(f"\n→ đã ghi {len(ROWS)} dòng vào {CSV_PATH.relative_to(ROOT)}  (explain JSON trong results/explain/)")
    ROWS.clear()


def storage_stats(coll):
    return next(coll.aggregate([{"$collStats": {"storageStats": {}}}]))["storageStats"]


def mb(x):
    return f"{x / 1e6:.2f} MB"


# =============================================================================
# EXP1 – no index vs single-field secondary index
# =============================================================================
@experiment("exp1", "Không index (linear search) vs secondary index 1 field",
            "linear search ~b block accesses vs secondary index (dense) ~log2(bi)+1; primary index trên _id")
def exp1(db, args):
    coll = db[args.coll]
    drop_secondary(coll)
    filt = {"patient_id": PATIENT}
    rows = []
    rows.append(measure("exp1", "1a_no_index", coll, filt, note="chỉ có _id index"))
    create_index(coll, [("patient_id", 1)], name="pid_1")
    rows.append(measure("exp1", "1b_pid_index", coll, filt, note="secondary index trên patient_id"))
    some_id = coll.find_one(filt, {"_id": 1})["_id"]
    rows.append(measure("exp1", "1c_by_id", coll, {"_id": some_id}, note="primary key _id (unique)"))
    show(rows)
    n = coll.estimated_document_count()
    print(f"\n  Collection có {n} document. Không index: phải đọc {rows[0]['docs']} doc để trả {rows[0]['returned']};"
          f" có index: đọc {rows[1]['keys']} key + {rows[1]['docs']} doc.")


# =============================================================================
# EXP2 – compound index, thứ tự field, prefix, ESR, sort
# =============================================================================
@experiment("exp2", "Compound index: thứ tự field, prefix, Equality-Sort-Range, tránh SORT",
            "index on multiple keys (composite key <k1,k2,...> sắp theo thứ tự từ điển)")
def exp2(db, args):
    coll = db[args.coll]
    drop_secondary(coll)
    F = {"patient_id": PATIENT, "sequence": "T2", "plane": "SAGITTAL"}
    S = {"series_folder": 1, "instance_number": 1}
    create_index(coll, [("patient_id", 1)], name="pid_1")
    create_index(coll, [("patient_id", 1), ("sequence", 1), ("plane", 1)], name="pid_seq_plane")
    create_index(coll, [("patient_id", 1), ("sequence", 1), ("plane", 1), ("series_folder", 1),
                        ("instance_number", 1)], name="pid_seq_plane_sort")
    create_index(coll, [("plane", 1), ("sequence", 1), ("patient_id", 1)], name="plane_seq_pid")
    create_index(coll, [("patient_id", 1), ("sequence", 1)], name="pid_seq")
    create_index(coll, [("sequence", 1), ("patient_id", 1)], name="seq_pid")

    rows = []

    def m(case, filt, sort, hint, note):
        r = measure("exp2", case, coll, filt, sort, hint=hint, note=note)
        r["case"] = case
        r["note"] = note
        rows.append(r)

    print("\n  (A) UC2: patient + T2 + SAGITTAL, sort theo series/instance – ép từng index bằng hint")
    m("2a_collscan", F, S, {"$natural": 1}, "không dùng index")
    m("2b_pid_1", F, S, "pid_1", "chỉ lọc được patient_id")
    m("2c_pid_seq_plane", F, S, "pid_seq_plane", "lọc đủ 3 field, vẫn SORT trong RAM")
    m("2d_ESR", F, S, "pid_seq_plane_sort", "Equality + Sort trong index -> không SORT")
    m("2e_plane_seq_pid", F, S, "plane_seq_pid", "đảo thứ tự: equality vẫn chặt")
    print("\n  (B) Prefix rule: query chỉ có patient_id")
    m("2f_prefix_ok", {"patient_id": PATIENT}, None, "pid_seq_plane", "patient_id là prefix")
    m("2g_prefix_bad", {"patient_id": PATIENT}, None, "plane_seq_pid", "patient_id KHÔNG là prefix -> quét cả index")
    print("\n  (C) Equality trước Range (ESR): sequence = T2, patient_id trong khoảng 0100–0150")
    R = {"patient_id": {"$gte": "0100", "$lt": "0150"}, "sequence": "T2"}
    m("2h_range_first", R, None, "pid_seq", "Range field đứng trước")
    m("2i_equality_first", R, None, "seq_pid", "Equality field đứng trước")
    print("\n  (D) Không hint – để query planner tự chọn (cầu nối sang tiêu chí Query Optimization)")
    m("2j_planner_choice", F, S, None, "planner tự chọn")
    show(rows)
    print(f"\n  Planner chọn: {rows[-1]['index']}  (đã loại {rows[-1]['rejected']} plan khác)")


# =============================================================================
# EXP3 – B-tree thật trong WiredTiger vs tính toán multilevel index
# =============================================================================
def _parse_size(cs, key):
    m = re.search(rf"{key}=(\d+)([kKmM]?)", cs)
    if not m:
        return None
    v = int(m.group(1))
    return v * {"": 1, "k": 1024, "K": 1024, "m": 1 << 20, "M": 1 << 20}[m.group(2)]


@experiment("exp3", "Cấu trúc B-tree thật của index (WiredTiger) vs ước lượng multilevel index",
            "multilevel index / B+-tree: fan-out fo, số mức t = ceil(log_fo(b1)), truy cập t+1 block")
def exp3(db, args):
    coll = db[args.coll]
    existing = coll.index_information()
    if "pid_1" not in existing:
        create_index(coll, [("patient_id", 1)], name="pid_1")
    if "pid_seq_plane_sort" not in existing:
        create_index(coll, [("patient_id", 1), ("sequence", 1), ("plane", 1), ("series_folder", 1),
                            ("instance_number", 1)], name="pid_seq_plane_sort")
    # duyệt từng index 1 lần để WiredTiger ghi nhận "maximum tree depth" (index chưa dùng -> 0)
    for name, spec in coll.index_information().items():
        if not any(v in ("text", "hashed", "2dsphere") for _, v in spec["key"]):
            list(coll.find({}, {"_id": 1}).hint(name).limit(200))
            list(coll.find({}, {"_id": 1}).hint(name).sort([(spec["key"][0][0], -1)]).limit(200))
    db.client.admin.command("fsync")  # ép checkpoint để số liệu dung lượng chính xác
    n = coll.estimated_document_count()
    st = storage_stats(coll)
    print(f"\n  Collection {coll.name}: {n} doc, data {mb(st['size'])}, trên đĩa {mb(st['storageSize'])}, "
          f"avgObjSize {st.get('avgObjSize', 0)} B")

    def levels(entries, bpe, ipm, lpm, fill):
        leaf_cap = max(int(lpm * fill // max(bpe, 1)), 2)
        leaves = math.ceil(entries / leaf_cap)
        fo = max(int(ipm * fill // (max(bpe, 1) + 8)), 2)
        lv = 1 + (math.ceil(math.log(leaves, fo)) if leaves > 1 else 0)
        return leaves, fo, lv

    rows = []
    for name, size in st["indexSizes"].items():
        det = st.get("indexDetails", {}).get(name, {})
        cs = det.get("creationString", "")
        ipm = _parse_size(cs, "internal_page_max") or 16384
        lpm = _parse_size(cs, "leaf_page_max") or 16384
        bpe = size / max(n, 1)  # byte / entry trên đĩa (đã nén prefix)
        l100, fo100, lv100 = levels(n, bpe, ipm, lpm, 1.0)
        l69, fo69, lv69 = levels(n, bpe, ipm, lpm, 0.69)
        rows.append({
            "index": name, "entries": n, "size": mb(size), "bytes/entry": f"{bpe:.1f}", "page": f"{lpm // 1024}KB",
            "leaf_pages(100%)": l100, "fanout(100%)": fo100, "levels(100%)": lv100,
            "leaf_pages(69%)": l69, "levels(69%)": lv69,
            "WT_max_depth": det.get("btree", {}).get("maximum tree depth", "n/a"),
        })
        ROWS.append({"time": datetime.now().isoformat(timespec="seconds"), "exp": "exp3", "case": name,
                     "collection": coll.name, "filter": "", "hint": "", "plan": "", "index": name, "keys": n,
                     "docs": "", "returned": "", "exec_ms": "", "rejected": "", "multikey": "",
                     "wall_ms_median": "", "note": json.dumps({k: v for k, v in rows[-1].items() if k != "index"})})
    print()
    print_table(rows, list(rows[0].keys()))
    print("\n  levels(100%): công thức trong slide, giả sử mọi page đầy (bfr = page_size / entry_size).")
    print("  levels(69%) : B-tree thực tế chỉ đầy ~69% (ln 2) do split -> nhiều page hơn, có thể thêm 1 tầng.")
    print("  WT_max_depth: độ sâu thật WiredTiger báo cáo (tính cả root page).")
    print("  Tra 1 key = đi từ root xuống leaf (≈ số tầng page) + 1 lần đọc document (FETCH).")


# =============================================================================
# EXP4 – chi phí index: dung lượng + tốc độ ghi
# =============================================================================
@experiment("exp4", "Chi phí của index: tốc độ insert & dung lượng khi tăng số index",
            "guidelines: index tăng tốc đọc nhưng tốn chỗ và mỗi insert/update phải cập nhật mọi index")
def exp4(db, args):
    n = args.bench_docs
    base = list(db.images.find({}, {"embedding": 0, "_id": 0}))
    src = (base * math.ceil(n / len(base)))[:n]
    specs = [
        [("patient_id", 1)],
        [("patient_id", 1), ("sequence", 1), ("plane", 1)],
        [("series_description", 1)],
        [("slice_location", 1)],
        [("study_date", 1), ("patient_age", 1)],
        [("source_path", 1)],
        [("instance_number", 1), ("slice_location", 1)],
        [("sequence", 1), ("plane", 1), ("slice_location", 1)],
    ]
    # nguồn cho phép đo phía server: bản sao metadata (không embedding) -> đọc nhanh, cố định
    db.drop_collection("bench_src")
    db.images.aggregate([{"$project": {"embedding": 0, "_id": 0}}, {"$out": "bench_src"}])
    n_srv = db.bench_src.estimated_document_count() * 2

    def setup(k):
        db.drop_collection("bench_index_cost")
        b = db.bench_index_cost
        for sp in specs[:k]:
            b.create_index(sp)
        return b

    rows, base_c, base_s = [], None, None
    for k in [0, 1, 3, 5, 8]:
        c_secs, s_secs = [], []
        for rep in range(args.reps):
            # (A) client: Python -> insert_many
            b = setup(k)
            docs = [dict(d) for d in src]
            t = time.perf_counter()
            for i in range(0, len(docs), 1000):
                b.insert_many(docs[i:i + 1000], ordered=False)
            c_secs.append(time.perf_counter() - t)
            # (B) server: $merge từ bench_src (2 lượt), không qua mạng / BSON encode phía client
            b = setup(k)
            t = time.perf_counter()
            for _ in range(2):
                db.bench_src.aggregate([{"$project": {"_id": 0}},
                                        {"$merge": {"into": "bench_index_cost", "whenNotMatched": "insert"}}])
            s_secs.append(time.perf_counter() - t)
        c_sec, s_sec = statistics.median(c_secs), statistics.median(s_secs)
        db.client.admin.command("fsync")  # checkpoint -> dung lượng index chính xác
        st = storage_stats(db.bench_index_cost)
        c_rate, s_rate = n / c_sec, n_srv / s_sec
        base_c, base_s = base_c or c_rate, base_s or s_rate
        rows.append({"secondary_indexes": k,
                     "client_docs/s": f"{c_rate:.0f}", "client_vs_0": f"{c_rate / base_c:.0%}",
                     "server_docs/s": f"{s_rate:.0f}", "server_vs_0": f"{s_rate / base_s:.0%}",
                     "data": mb(st["size"]), "total_index_size": mb(st["totalIndexSize"]),
                     "index/data": f"{st['totalIndexSize'] / max(st['size'], 1):.0%}"})
        print(f"  {k} index: client {c_sec:.2f}s [{', '.join(f'{x:.2f}' for x in c_secs)}]  "
              f"server {s_sec:.2f}s [{', '.join(f'{x:.2f}' for x in s_secs)}]")
        ROWS.append({"time": datetime.now().isoformat(timespec="seconds"), "exp": "exp4", "case": f"{k}_indexes",
                     "collection": "bench_index_cost", "filter": "", "hint": "", "plan": "", "index": k, "keys": "",
                     "docs": n, "returned": "", "exec_ms": round(s_sec * 1000), "rejected": "", "multikey": "",
                     "wall_ms_median": round(c_sec * 1000), "note": json.dumps(rows[-1])})
    db.drop_collection("bench_src")
    print(f"\n  client: insert {n} doc từ Python;  server: $merge {n_srv} doc ngay trong mongod")
    db.drop_collection("bench_index_cost")
    print()
    print_table(rows, list(rows[0].keys()))
    db.client.admin.command("fsync")
    st = storage_stats(db.images)
    print(f"\n  images thật: data {mb(st['size'])}, index {mb(st['totalIndexSize'])}: "
          + ", ".join(f"{k}={mb(v)}" for k, v in st["indexSizes"].items()))


# =============================================================================
# EXP5 – multikey index trên mảng findings
# =============================================================================
@experiment("exp5", "Multikey index trên mảng (patients.findings)",
            "secondary index trên non-key field, option 3: 1 entry / giá trị + danh sách con trỏ")
def exp5(db, args):
    coll = db.patients
    for name in list(coll.index_information()):
        if name not in ("_id_",) and "text" not in str(coll.index_information()[name]["key"]):
            coll.drop_index(name)
    q1 = {"findings": "annular_tear"}
    q2 = {"findings": {"$all": ["disc_bulge", "annular_tear"]}}
    rows = []
    for case, f in [("5a_no_index_one", q1), ("5b_no_index_all", q2)]:
        r = measure("exp5", case, coll, f)
        rows.append(dict(r, case=case, note="COLLSCAN"))
    create_index(coll, [("findings", 1)], name="findings_1")
    for case, f, note in [("5c_multikey_one", q1, "1 bound trên index"),
                          ("5d_multikey_all", q2, "IXSCAN 1 giá trị + lọc phần còn lại")]:
        r = measure("exp5", case, coll, f)
        rows.append(dict(r, case=case, note=note))
    show(rows)
    v = db.command("validate", "patients")
    keys = v.get("keysPerIndex", {}).get("findings_1")
    total_vals = next(coll.aggregate([{"$group": {"_id": None, "n": {"$sum": {"$size": {"$ifNull": ["$findings", []]}}}}}]))["n"]
    distinct = len(coll.distinct("findings"))
    print(f"\n  patients: {coll.estimated_document_count()} doc | tổng số phần tử findings: {total_vals} | "
          f"giá trị khác nhau: {distinct} | số key trong findings_1: {keys}")
    print("  -> index có NHIỀU entry hơn số document (mỗi phần tử mảng = 1 key; mảng rỗng = 1 key undefined).")


# =============================================================================
# EXP6 – covered query
# =============================================================================
@experiment("exp6", "Covered query: trả kết quả chỉ từ index, không đọc document",
            "index chứa đủ field cần thiết -> không cần truy cập block dữ liệu (chỉ x block index)")
def exp6(db, args):
    coll = db[args.coll]
    if "pid_seq_plane_sort" not in coll.index_information():
        create_index(coll, [("patient_id", 1), ("sequence", 1), ("plane", 1), ("series_folder", 1),
                            ("instance_number", 1)], name="pid_seq_plane_sort")
    F = {"patient_id": PATIENT, "sequence": "T2", "plane": "SAGITTAL"}
    covered = {"_id": 0, "patient_id": 1, "sequence": 1, "plane": 1, "series_folder": 1, "instance_number": 1}
    fetch = {"_id": 0, "patient_id": 1, "series_folder": 1, "instance_number": 1, "file_id": 1}
    rows = []
    for case, proj, note in [("6a_covered", covered, "projection chỉ gồm field trong index"),
                             ("6b_needs_fetch", fetch, "cần file_id (không có trong index)")]:
        r = measure("exp6", case, coll, F, proj=proj, hint="pid_seq_plane_sort", note=note)
        rows.append(dict(r, case=case, note=note))
    show(rows)


# =============================================================================
# EXP7 – clustered collection (index-organized) vs collection thường
# =============================================================================
@experiment("exp7", "Clustered collection (dữ liệu sắp theo _id) vs collection thường + _id index",
            "primary/clustering index trên file có thứ tự; Index-Organized Table của Oracle")
def exp7(db, args):
    for c in ("images_plain", "images_clustered"):
        db.drop_collection(c)
    db.create_collection("images_plain")
    db.create_collection("images_clustered", clusteredIndex={"key": {"_id": 1}, "unique": True})
    for c in ("images_plain", "images_clustered"):
        t = time.perf_counter()
        db.images.aggregate([{"$project": {"embedding": 0}}, {"$merge": {"into": c}}])
        print(f"  copy images -> {c}: {(time.perf_counter() - t):.1f}s")
    ids = [d["_id"] for d in db.images_plain.find({}, {"_id": 1}).sort("_id", 1).skip(10000).limit(2000)]
    R = {"_id": {"$gte": ids[0], "$lte": ids[-1]}}
    E = {"_id": ids[500]}
    rows = []
    for c in ("images_plain", "images_clustered"):
        for case, f in [("range_2000", R), ("point", E)]:
            r = measure("exp7", f"7_{c}_{case}", db[c], f)
            rows.append(dict(r, case=f"{c}:{case}"))
    show(rows)
    srows = []
    for c in ("images_plain", "images_clustered"):
        st = storage_stats(db[c])
        srows.append({"collection": c, "data": mb(st["size"]), "on_disk": mb(st["storageSize"]),
                      "indexes": ", ".join(st["indexSizes"]) or "(không có index riêng)",
                      "total_index_size": mb(st["totalIndexSize"])})
    print()
    print_table(srows, list(srows[0].keys()))
    if not args.keep:
        for c in ("images_plain", "images_clustered"):
            db.drop_collection(c)
        print("  (đã xoá images_plain / images_clustered – dùng --keep để giữ lại)")


# =============================================================================
# EXP8 – sparse & partial index (dense vs sparse theo nghĩa của MongoDB)
# =============================================================================
@experiment("exp8", "Sparse index & partial index",
            "dense vs nondense (sparse) index – chú ý: 'sparse' của MongoDB mang nghĩa KHÁC sách")
def exp8(db, args):
    p = db.patients
    for name in ("img_cnt_dense", "img_cnt_sparse"):
        if name in p.index_information():
            p.drop_index(name)
    create_index(p, [("image_count", 1)], name="img_cnt_dense")
    create_index(p, [("image_count", 1)], name="img_cnt_sparse", sparse=True)
    keys = db.command("validate", "patients").get("keysPerIndex", {})
    have = p.count_documents({"image_count": {"$exists": True}})
    print(f"\n  patients: {p.estimated_document_count()} doc, có image_count: {have}")
    print(f"  số key: img_cnt_dense={keys.get('img_cnt_dense')}  img_cnt_sparse={keys.get('img_cnt_sparse')}")
    rows = []
    rows.append(dict(measure("exp8", "8a_sparse_ok", p, {"image_count": {"$gte": 100}}, hint="img_cnt_sparse"),
                     case="8a_sparse_range", note="sparse dùng được"))
    p.drop_index("img_cnt_dense")
    rows.append(dict(measure("exp8", "8b_missing_field", p, {"image_count": None}),
                     case="8b_find_missing", note="tìm doc THIẾU field: sparse không dùng được"))
    p.drop_index("img_cnt_sparse")

    im = db.images
    if "age_partial" in im.index_information():
        im.drop_index("age_partial")
    create_index(im, [("patient_age", 1)], name="age_partial",
                 partialFilterExpression={"patient_age": {"$gte": 60}})
    n_part = explain_find(im, {"patient_age": {"$gte": 60}}, hint="age_partial")["keys"]
    print(f"  images: {im.estimated_document_count()} doc, age_partial chỉ có {n_part} key (patient_age >= 60)")
    rows.append(dict(measure("exp8", "8c_partial_ok", im, {"patient_age": {"$gte": 70}}),
                     case="8c_age>=70", note="nằm trong partial filter -> dùng index"))
    rows.append(dict(measure("exp8", "8d_partial_no", im, {"patient_age": {"$gte": 50}}),
                     case="8d_age>=50", note="vượt ngoài partial filter -> COLLSCAN"))
    im.drop_index("age_partial")
    show(rows)


# =============================================================================
# EXP9 – text index vs regex
# =============================================================================
@experiment("exp9", "Text index (inverted index) vs regex trên notes",
            "function-based / domain index; B-tree chỉ hỗ trợ prefix, không tìm từ ở giữa chuỗi")
def exp9(db, args):
    p = db.patients
    for name, spec in list(p.index_information().items()):
        if name != "_id_" and (name in ("notes_1",) or "text" in str(spec["key"])):
            p.drop_index(name)
    rows = []
    rows.append(dict(measure("exp9", "9a_regex_noindex", p, {"notes": {"$regex": "annular", "$options": "i"}}),
                     case="9a_regex_contains", note="không index"))
    create_index(p, [("notes", 1)], name="notes_1")
    rows.append(dict(measure("exp9", "9b_regex_prefix", p, {"notes": {"$regex": "^LSS MRI"}}),
                     case="9b_regex_^prefix", note="B-tree: prefix -> có bound"))
    rows.append(dict(measure("exp9", "9c_regex_contains", p, {"notes": {"$regex": "annular", "$options": "i"}}),
                     case="9c_regex_contains", note="B-tree: không bound -> quét cả index"))
    p.drop_index("notes_1")
    create_index(p, [("notes", "text")], name="notes_text")
    rows.append(dict(measure("exp9", "9d_text", p, {"$text": {"$search": "annular"}}),
                     case="9d_$text", note="inverted index, có stemming"))
    show(rows)


# =============================================================================
# EXP10 – hashed index
# =============================================================================
@experiment("exp10", "Hashed index: equality nhanh, không hỗ trợ range",
            "hashing (static/extendible): tìm theo khoá = O(1), không giữ thứ tự nên không range scan")
def exp10(db, args):
    coll = db[args.coll]
    drop_secondary(coll)
    create_index(coll, [("patient_id", "hashed")], name="pid_hashed")
    create_index(coll, [("patient_id", 1)], name="pid_1")
    rows = []
    E = {"patient_id": PATIENT}
    R = {"patient_id": {"$gte": "0001", "$lte": "0005"}}
    rows.append(dict(measure("exp10", "10a_hash_eq", coll, E, hint="pid_hashed"), case="10a_hashed_equality"))
    rows.append(dict(measure("exp10", "10b_btree_eq", coll, E, hint="pid_1"), case="10b_btree_equality"))
    rows.append(dict(measure("exp10", "10c_btree_range", coll, R, hint="pid_1"), case="10c_btree_range"))
    coll.drop_index("pid_1")
    rows.append(dict(measure("exp10", "10d_hash_range", coll, R), case="10d_range_only_hashed",
                     note="hashed không dùng được cho range"))
    show(rows)
    sizes = storage_stats(coll)["indexSizes"]
    print(f"\n  index size: pid_hashed={mb(sizes.get('pid_hashed', 0))}")
    coll.drop_index("pid_hashed")


# =============================================================================
# EXP11 – vector index (HNSW): ANN vs exact, numCandidates vs recall
# =============================================================================
@experiment("exp11", "Vector index (HNSW) – ANN vs exact: thời gian & recall theo numCandidates",
            "không có trong B+-tree: index cho không gian nhiều chiều (application domain index)")
def exp11(db, args):
    q = list(db.images.find({"patient_id": PATIENT, "sequence": "T2", "plane": "SAGITTAL",
                             "embedding": {"$exists": True}}, {"embedding": 1}).sort("instance_number", 1))
    if not q:
        print("  Chưa có embedding – chạy embed_images.py trước.")
        return
    qv = q[len(q) // 2]["embedding"]
    K = 10

    def run(filt, nc=None, exact=False, runs=5):
        vs = {"index": "img_vec", "path": "embedding", "queryVector": qv, "limit": K}
        if filt:
            vs["filter"] = filt
        if exact:
            vs["exact"] = True
        else:
            vs["numCandidates"] = nc
        pipe = [{"$vectorSearch": vs}, {"$project": {"_id": 1}}]
        list(db.images.aggregate(pipe))  # warm-up
        ts, ids = [], None
        for _ in range(runs):
            t = time.perf_counter()
            ids = [d["_id"] for d in db.images.aggregate(pipe)]
            ts.append((time.perf_counter() - t) * 1000)
        return statistics.median(ts), ids

    rows = []
    for label, filt in [("filtered T2 SAG, khác patient", {"sequence": "T2", "plane": "SAGITTAL",
                                                            "patient_id": {"$ne": PATIENT}}),
                        ("không filter (toàn bộ ~40k)", None)]:
        ex_ms, ex_ids = run(filt, exact=True)
        rows.append({"scope": label, "mode": "EXACT", "numCandidates": "-", "median_ms": f"{ex_ms:.1f}",
                     "recall@10": "1.00"})
        for nc in [10, 20, 50, 100, 200, 500, 1000]:
            ms, ids = run(filt, nc=nc)
            rec = len(set(ids) & set(ex_ids)) / K
            rows.append({"scope": label, "mode": "ANN", "numCandidates": nc, "median_ms": f"{ms:.1f}",
                         "recall@10": f"{rec:.2f}"})
    for r in rows:
        ROWS.append({"time": datetime.now().isoformat(timespec="seconds"), "exp": "exp11",
                     "case": f"{r['mode']}_{r['numCandidates']}", "collection": "images", "filter": r["scope"],
                     "hint": "", "plan": "$vectorSearch", "index": "img_vec", "keys": "", "docs": "",
                     "returned": K, "exec_ms": "", "rejected": "", "multikey": "",
                     "wall_ms_median": r["median_ms"], "note": f"recall@10={r['recall@10']}"})
    print()
    print_table(rows, list(rows[0].keys()))


# =============================================================================
# utilities: make-big, final, reset
# =============================================================================
def make_big(db, factor):
    db.drop_collection("images_big")
    t = time.perf_counter()
    for k in range(factor):
        pid = "$patient_id" if k == 0 else {"$concat": ["$patient_id", f"_{k}"]}
        _id = "$_id" if k == 0 else {"$concat": ["$_id", f"#{k}"]}
        db.images.aggregate([{"$project": {"embedding": 0}}, {"$set": {"_id": _id, "patient_id": pid}},
                             {"$merge": {"into": "images_big"}}])
        print(f"  bản sao {k + 1}/{factor}: {db.images_big.estimated_document_count()} doc")
    print(f"images_big: {db.images_big.estimated_document_count()} doc trong {time.perf_counter() - t:.0f}s")
    print("Chạy:  python scripts/demo_indexing.py exp1 --coll images_big   (tương tự exp2, exp3, exp6, exp10)")


def final_indexes(db):
    print("\nTạo bộ index khuyến nghị cho ứng dụng (giữ lại sau demo):")
    drop_secondary(db.images)
    create_index(db.images, [("patient_id", 1), ("sequence", 1), ("plane", 1), ("series_folder", 1),
                             ("instance_number", 1)], name="pid_seq_plane_sort")
    p = db.patients
    for name in list(p.index_information()):
        if name != "_id_":
            p.drop_index(name)
    create_index(p, [("findings", 1)], name="findings_1")
    create_index(p, [("notes", "text")], name="notes_text")


def reset(db, drop_big):
    for c in SCRATCH + (["images_big"] if drop_big else []):
        db.drop_collection(c)
    drop_secondary(db.images)
    drop_secondary(db.patients)
    print("Đã xoá index phụ trên images/patients và các collection tạm "
          f"({', '.join(SCRATCH + (['images_big'] if drop_big else []))}). Vector index img_vec giữ nguyên.")


# =============================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", help="list | all | exp1..exp11 | make-big | final | reset")
    ap.add_argument("--coll", default="images", help="images hoặc images_big (exp1/2/3/6/10)")
    ap.add_argument("--factor", type=int, default=10, help="make-big: số bản sao")
    ap.add_argument("--bench-docs", type=int, default=100000, help="exp4: số doc insert mỗi lượt")
    ap.add_argument("--reps", type=int, default=3, help="exp4: số lần lặp mỗi cấu hình (lấy trung vị)")
    ap.add_argument("--keep", action="store_true", help="exp7: giữ collection images_plain/clustered")
    ap.add_argument("--all", action="store_true", help="reset: xoá cả images_big")
    args = ap.parse_args()
    db = get_db()

    if args.cmd == "list":
        for k, (title, theory, _) in EXPERIMENTS.items():
            print(f"{k:<6} {title}\n       ↳ {theory}")
        return
    if args.cmd == "make-big":
        return make_big(db, args.factor)
    if args.cmd == "final":
        return final_indexes(db)
    if args.cmd == "reset":
        return reset(db, args.all)

    keys = list(EXPERIMENTS) if args.cmd == "all" else [args.cmd]
    for k in keys:
        if k not in EXPERIMENTS:
            raise SystemExit(f"Không có experiment '{k}'. Dùng: list")
        banner(k)
        t = time.perf_counter()
        EXPERIMENTS[k][2](db, args)
        print(f"\n  ({k} xong trong {time.perf_counter() - t:.1f}s)")
        flush_csv()
    if args.cmd == "all":
        final_indexes(db)


if __name__ == "__main__":
    main()
