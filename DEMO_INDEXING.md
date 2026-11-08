# Demo Indexing – hướng dẫn chạy & đọc kết quả

Script: `scripts/demo_indexing.py`. Mỗi experiment tự tạo/xoá index nó cần, nên chạy lại bao nhiêu lần cũng được.
Kết quả được ghi vào `results/indexing_results.csv` (mỗi phép đo một dòng), còn explain đầy đủ nằm trong `results/explain/*.json`.

```bash
source .venv/bin/activate
python scripts/demo_indexing.py list      # xem danh sách
python scripts/demo_indexing.py all       # chạy exp1..exp11 (~3–5 phút), cuối cùng tạo bộ index khuyến nghị
python scripts/demo_indexing.py exp2      # chạy riêng 1 experiment (dùng khi demo live)
```

Thêm phần "big data" (khuyến khích): nhân bản dữ liệu ×10 thành khoảng 405k document để thấy chênh lệch rõ hơn.
```bash
python scripts/demo_indexing.py make-big --factor 10
python scripts/demo_indexing.py exp1 --coll images_big
python scripts/demo_indexing.py exp2 --coll images_big
```

Dọn dẹp: `python scripts/demo_indexing.py reset` (thêm `--all` để xoá luôn `images_big`).

## Cột trong bảng kết quả

| Cột | Ý nghĩa | Liên hệ lý thuyết |
|---|---|---|
| `plan` | chuỗi stage, đọc từ trái sang phải (vd `IXSCAN -> FETCH -> SORT`) | access path / execution plan |
| `keys` | số index entry đã đọc (`totalKeysExamined`) | số block truy cập trên index |
| `docs` | số document đã đọc (`totalDocsExamined`) | số block truy cập trên data file |
| `returned` | số kết quả trả về | selection cardinality *s* |
| `exec_ms` | thời gian server đo | |
| `wall_ms_median` | thời gian phía client, lấy trung vị 6 lần chạy | |

Query lý tưởng: `keys ≈ docs ≈ returned`. Query tệ: `docs` gấp hàng nghìn lần `returned`.

## Experiment ↔ lý thuyết Chương 2

| Exp | MongoDB | Lý thuyết | Kết quả mong đợi |
|---|---|---|---|
| exp1 | Không index vs secondary index `patient_id`; tra theo `_id` | Linear search vs secondary index (dense); primary key | COLLSCAN 40.562 docs → IXSCAN 54 keys/54 docs |
| exp2 | Compound index, thứ tự field, prefix, ESR, SORT | Index on multiple keys | ESR bỏ được stage SORT; index không có prefix phù hợp thì phải quét toàn bộ index. Range đứng trước chỉ tốn hơn một chút (1.630 so với 1.586 keys) vì MongoDB **seek** qua các khoảng key không khớp chứ không quét tuần tự |
| exp3 | Độ sâu, page, fan-out của B-tree trong WiredTiger | Multilevel index, B+-tree, tính số block access | Công thức trong slide (page đầy 100%) cho 2–3 tầng, còn WiredTiger báo 3 tầng. Chênh lệch là do page thực tế chỉ đầy khoảng 69% và cách đếm root |
| exp4 | Tốc độ insert 100k doc khi có 0/1/3/5/8 index (trung vị 3 lần), dung lượng index | Guidelines: chi phí bảo trì | Càng nhiều index thì `docs_per_s` càng giảm và `total_index_size` càng tăng |
| exp5 | Multikey index trên mảng `findings` | Secondary index trên non-key field (option 3) | Số key nhiều hơn số document |
| exp6 | Covered query | Index chứa đủ dữ liệu, không cần đọc record | `docs = 0`, có stage `PROJECTION_COVERED` |
| exp7 | Clustered collection vs collection thường | Primary/clustering index, Index-Organized Table | Clustered không cần index `_id` riêng; range scan đọc dữ liệu liên tục |
| exp8 | Sparse & partial index | Dense vs nondense | "Sparse" của MongoDB nghĩa là **bỏ document thiếu field**, khác với sparse trong sách là **1 entry cho mỗi block** |
| exp9 | Text index vs `$regex` | Function-based / domain index | B-tree chỉ dùng được với regex dạng prefix `^...`; tìm từ nằm giữa chuỗi phải dùng inverted index |
| exp10 | Hashed index | Hashing | Equality chạy tốt; range thì COLLSCAN |
| exp11 | Vector index HNSW: ANN vs exact | Không có trong B+-tree | numCandidates tăng thì recall tăng lên 1.0 nhưng chậm hơn; với tập đã lọc nhỏ thì exact có thể nhanh hơn ANN |

## Nên chụp màn hình cho report/slide
1. exp1: bảng trước/sau. Đây là slide mở đầu phần Indexing.
2. exp2: toàn bộ bảng, nhất là cặp 2c/2d (SORT) và 2f/2g (prefix).
3. exp3: bảng ước lượng vs WiredTiger, để so sánh với Example 1–4 trong slide.
4. exp4: vẽ biểu đồ số index → docs/s.
5. exp11: vẽ biểu đồ numCandidates → recall và thời gian.
6. Compass: tab **Explain Plan** của query UC2 trước/sau khi có index, và tab **Indexes** (size, usage).

## Câu hỏi Q&A thường gặp
- **MongoDB dùng B-tree hay B+-tree?** WiredTiger lưu dữ liệu ở các leaf page, còn internal page chỉ giữ key để định hướng. Về bản chất đó là cấu trúc kiểu B+-tree, dù tài liệu MongoDB gọi chung là "B-tree".
- **Vì sao thứ tự field trong compound index quan trọng?** Key được sắp theo thứ tự từ điển (k1, k2, ...). Chỉ prefix liên tục mới tạo được khoảng quét gọn. Quy tắc ESR: Equality, rồi Sort, rồi Range.
- **Khác biệt so với Oracle?** MongoDB không có bitmap index. Thay vào đó có multikey, text, vector, hashed và wildcard index.
- **Index nhiều có hại không?** Có, xem exp4: insert chậm hơn và tốn thêm dung lượng, và mỗi index chiếm RAM trong cache.
