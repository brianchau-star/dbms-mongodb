"""ETL: Radiologists Report.xlsx -> collection `patients`.

File gốc: 1 sheet, 2 cột ("Patient ID", "Clinician's Notes"), 575 dòng, 60 dòng không có notes.

Document:
{
  _id: "0001",                 # zero-pad 4 chữ số cho khớp tên thư mục ảnh
  patient_no: 1,
  notes: "L4-5: degenerative annular disc bulge ...",   # nguyên văn (null nếu trống)
  has_notes: true,
  levels:   ["L4-L5", "L5-S1"],                         # derived: các tầng đĩa đệm được nhắc tới
  findings: ["disc_bulge", "thecal_sac_compression"],   # derived: từ khoá chuẩn hoá
}
`levels` và `findings` là mảng -> dùng để demo multikey index + điều kiện ở use case 2.

Chạy:
    python scripts/load_notes.py --file "~/Downloads/Radiologists Notes for Lumbar Spine MRI Dataset/Radiologists Report.xlsx" --dry-run
    python scripts/load_notes.py --file "<...>.xlsx"
"""
import argparse
import os
import re
import sys
from collections import Counter

import openpyxl

PAD = 4

# keyword -> regex (case-insensitive). Thứ tự không quan trọng.
FINDING_PATTERNS = {
    "disc_bulge": r"bul?ge",
    "disc_protrusion": r"protrusion",
    "disc_herniation": r"(?<!no evidence of )herniat",
    "disc_extrusion": r"extru",
    "annular_tear": r"annul\w* (tear|fissure)|torn annulus|annular tear",
    "thecal_sac_compression": r"(compress|indent|abut)\w*[^.]{0,40}thecal",
    "nerve_root_compression": r"(compress|encroach|abut)\w*[^.]{0,60}nerve ro",
    "foraminal_narrowing": r"foramin|exit (neural )?canal",
    "spinal_canal_stenosis": r"canal stenosis|spinal stenosis|canal narrowing",
    "muscle_spasm": r"muscle spasm",
    "disc_desiccation": r"desiccat|dehydrat",
    "modic_change": r"modic",
    "osteophyte": r"osteophyt",
    "ligamentum_flavum_hypertrophy": r"lig\w* ?flavum",
    "facet_hypertrophy": r"facet",
    "spondylolisthesis": r"listhesis",
    "scoliosis": r"scolio",
    "schmorl_node": r"schmorl",
    "hemangioma": r"hemangioma|haemangioma",
    "post_operative": r"operat|laminectomy|fenestration",
}
NEGATION = re.compile(r"\bno (evidence of |significant |definite )?", re.I)

LEVEL_RE = re.compile(r"\b(L\d|D\d{1,2}|T\d{1,2})\s*[-/]\s*(L?\d|S1|D?\d{1,2}|T?\d{1,2})\b", re.I)


def norm_level(a: str, b: str) -> str:
    a = a.upper().replace("D", "T")  # D11 (dorsal) = T11
    b = b.upper().replace("D", "T")
    if b.isdigit():
        b = a[0] + b  # "L4-5" -> L4-L5
    return f"{a}-{b}"


def extract(note: str):
    levels = sorted({norm_level(a, b) for a, b in LEVEL_RE.findall(note)})
    findings = set()
    for sentence in re.split(r"[.\n]", note):
        s = sentence.strip().lower()
        if not s:
            continue
        negated = bool(NEGATION.match(s))
        for key, pat in FINDING_PATTERNS.items():
            if re.search(pat, s, re.I) and not negated:
                findings.add(key)
    return levels, sorted(findings)


def read_rows(path):
    ws = openpyxl.load_workbook(path, read_only=True).active
    for pid, note in ws.iter_rows(min_row=2, max_col=2, values_only=True):
        if pid is None:
            continue
        note = str(note).strip() if note is not None and str(note).strip() else None
        yield int(pid), note


def build_doc(pid: int, note):
    levels, findings = extract(note) if note else ([], [])
    return {
        "_id": str(pid).zfill(PAD),
        "patient_no": pid,
        "notes": note,
        "has_notes": note is not None,
        "levels": levels,
        "findings": findings,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    path = os.path.expanduser(args.file)
    if not os.path.isfile(path):
        sys.exit(f"Không thấy file: {path}")

    docs = [build_doc(pid, note) for pid, note in read_rows(path)]

    fc, lc = Counter(), Counter()
    for d in docs:
        fc.update(d["findings"])
        lc.update(d["levels"])
    print(f"patients: {len(docs)}, có notes: {sum(d['has_notes'] for d in docs)}")
    print("top findings:", fc.most_common(12))
    print("top levels:  ", lc.most_common(8))
    print("ví dụ:", {k: docs[0][k] for k in ("_id", "levels", "findings")})

    if args.dry_run:
        return

    from pymongo import ReplaceOne

    from common import get_db

    db = get_db()
    # upsert theo _id -> chạy lại nhiều lần không bị trùng
    res = db.patients.bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs])
    print(f"upserted={res.upserted_count}, modified={res.modified_count}")
    print("patients count:", db.patients.count_documents({}))


if __name__ == "__main__":
    main()
