"""Tạo database `spine_mri` và các collection kèm JSON Schema validator.

Collections
-----------
patients          : 1 document / patient: radiologist notes + tóm tắt studies/series (embed)
images            : 1 document / slice MRI (metadata DICOM + tham chiếu GridFS + embedding)
mri.files/.chunks : GridFS bucket "mri" chứa file .ima gốc.
                    mri.chunks dùng block compressor zstd (WiredTiger) thay cho snappy mặc định:
                    đo trên mẫu dataset, file DICOM nén còn ~56% kích thước.

Script này CHỈ tạo index bắt buộc (_id mặc định; GridFS tự tạo {files_id, n} khi upload).
Secondary index (patient_id, sequence, plane, ...) để dành cho phần demo Indexing.

    python scripts/init_db.py           # giữ data, cập nhật validator
    python scripts/init_db.py --drop    # xoá sạch DB và tạo lại (nhớ chạy lại load_notes.py)
"""
import argparse

from common import DB_NAME, get_client

PATIENT_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id"],
        "properties": {
            "_id": {"bsonType": "string", "description": "patient id, vd '0001'"},
            "patient_no": {"bsonType": "int"},
            "notes": {"bsonType": ["string", "null"], "description": "radiologist notes nguyên văn"},
            "has_notes": {"bsonType": "bool"},
            "levels": {"bsonType": "array", "items": {"bsonType": "string"}},
            "findings": {"bsonType": "array", "items": {"bsonType": "string"}},
            "sex": {"bsonType": ["string", "null"]},
            "studies": {"bsonType": "array"},
            "image_count": {"bsonType": ["int", "long"]},
        },
    }
}

IMAGE_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "patient_id", "file_id", "series_type", "sequence", "plane"],
        "properties": {
            "_id": {"bsonType": "string", "description": "SOPInstanceUID"},
            "patient_id": {"bsonType": "string"},
            "study_name": {"bsonType": "string"},
            "study_date": {"bsonType": ["string", "null"]},
            "series_folder": {"bsonType": "string"},
            "series_description": {"bsonType": ["string", "null"]},
            "series_type": {"enum": ["PRIMARY", "LOCALIZER", "DERIVED"]},
            "sequence": {"enum": ["T1", "T2", "PD", "OTHER"]},
            "plane": {"enum": ["SAGITTAL", "AXIAL", "CORONAL", "UNKNOWN"]},
            "fat_suppressed": {"bsonType": "bool"},
            "contrast": {"bsonType": "bool"},
            "instance_number": {"bsonType": ["int", "null"]},
            "slice_location": {"bsonType": ["double", "null"]},
            "rows": {"bsonType": ["int", "null"]},
            "columns": {"bsonType": ["int", "null"]},
            "patient_sex": {"bsonType": ["string", "null"]},
            "patient_age": {"bsonType": ["int", "null"]},
            "file_id": {"bsonType": "objectId", "description": "_id trong mri.files"},
            "embedding": {"bsonType": "array", "items": {"bsonType": "double"}},
        },
    }
}

COLLECTIONS = {"patients": PATIENT_SCHEMA, "images": IMAGE_SCHEMA}
ZSTD = {"wiredTiger": {"configString": "block_compressor=zstd"}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true", help="drop database trước khi tạo")
    args = ap.parse_args()

    client = get_client()
    db = client[DB_NAME]
    if args.drop:
        client.drop_database(DB_NAME)
        print(f"Dropped database {DB_NAME}")

    existing = set(db.list_collection_names())
    for name, schema in COLLECTIONS.items():
        if name in existing:
            db.command("collMod", name, validator=schema, validationLevel="moderate")
            print(f"Updated validator: {name}")
        else:
            db.create_collection(name, validator=schema, validationLevel="moderate")
            print(f"Created: {name}")

    # GridFS bucket "mri"
    if "mri.files" not in existing:
        db.create_collection("mri.files")
        print("Created: mri.files")
    if "mri.chunks" in existing and db["mri.chunks"].estimated_document_count() == 0:
        db["mri.chunks"].drop()  # tạo lại để áp dụng zstd
        existing.discard("mri.chunks")
    if "mri.chunks" not in existing:
        db.create_collection("mri.chunks", storageEngine=ZSTD)
        print("Created: mri.chunks (zstd)")

    try:
        st = next(db["mri.chunks"].aggregate([{"$collStats": {"storageStats": {}}}]))
        cfg = st["storageStats"].get("wiredTiger", {}).get("creationString", "")
        print("mri.chunks compressor:", "zstd" if "block_compressor=zstd" in cfg else "snappy/other")
    except Exception as e:
        print("Không đọc được storageStats:", e)
    print("\nCollections:", sorted(db.list_collection_names()))


if __name__ == "__main__":
    main()
