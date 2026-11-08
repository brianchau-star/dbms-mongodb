"""Kiểm tra MongoDB đã sẵn sàng cho assignment chưa:
1. Kết nối + version
2. Đang chạy replica set (bắt buộc cho multi-document transaction)
3. Transaction commit / abort chạy được
4. mongot (Search / Vector Search) có hoạt động không
"""
import sys
import time

from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

from common import DB_NAME, get_client


def ok(msg):
    print(f"  [OK]   {msg}")


def fail(msg):
    print(f"  [FAIL] {msg}")


def main():
    client = get_client()
    print("1. Connection")
    try:
        client.admin.command("ping")
    except ServerSelectionTimeoutError as e:
        fail(f"Không kết nối được: {e}\n         -> container đã chạy chưa? `docker compose ps`")
        sys.exit(1)
    info = client.server_info()
    ok(f"MongoDB {info['version']}  (storage engine: "
       f"{client.admin.command('serverStatus')['storageEngine']['name']})")

    print("2. Replica set")
    hello = client.admin.command("hello")
    if hello.get("setName"):
        ok(f"replica set '{hello['setName']}', isWritablePrimary={hello.get('isWritablePrimary')}")
    else:
        fail("Không phải replica set -> multi-document transaction sẽ không chạy")

    print("3. Transaction")
    coll = client[DB_NAME]["_healthcheck"]
    coll.drop()
    coll.insert_one({"_id": "seed"})  # tạo collection trước (không tạo collection trong txn cho chắc)
    with client.start_session() as s:
        with s.start_transaction():
            coll.insert_one({"_id": "aborted"}, session=s)
            s.abort_transaction()
        with s.start_transaction():
            coll.insert_one({"_id": "committed"}, session=s)
    ids = sorted(d["_id"] for d in coll.find())
    if ids == ["committed", "seed"]:
        ok("abort -> rollback, commit -> persisted")
    else:
        fail(f"kết quả lạ: {ids}")

    print("4. Search / Vector Search (mongot)")
    try:
        coll.create_search_index(
            {
                "name": "hc_vec",
                "type": "vectorSearch",
                "definition": {
                    "fields": [{"type": "vector", "path": "v", "numDimensions": 3, "similarity": "cosine"}]
                },
            }
        )
        coll.insert_many([{"_id": i, "v": [1.0, float(i), 0.0]} for i in range(5)])
        for _ in range(30):
            idx = list(coll.list_search_indexes("hc_vec"))
            if idx and idx[0].get("queryable"):
                break
            time.sleep(2)
        res = list(coll.aggregate([
            {"$vectorSearch": {"index": "hc_vec", "path": "v", "queryVector": [1.0, 1.0, 0.0],
                               "numCandidates": 10, "limit": 2}},
            {"$project": {"score": {"$meta": "vectorSearchScore"}}},
        ]))
        if res:
            ok(f"$vectorSearch trả về {[r['_id'] for r in res]}")
        else:
            fail("index chưa queryable (thử chạy lại sau ~1 phút)")
    except OperationFailure as e:
        fail(f"Vector search không khả dụng: {e.details.get('errmsg', e)}")
    finally:
        coll.drop()

    print("\nDone.")


if __name__ == "__main__":
    main()
