# Kết quả thí nghiệm Indexing

Môi trường: MacBook Pro (Apple Silicon), Docker Desktop, MongoDB 8.0.34 (Atlas Local, WiredTiger), pymongo 4.19.
Dữ liệu: `images` 40.562 doc, `images_big` 405.620 doc (nhân bản ×10), `patients` 575 doc.
Thời gian `wall_ms` là trung vị của 6 lần chạy phía client. Số liệu đầy đủ ở `indexing_results.csv`, explain ở `explain/`.

## exp1: Không index vs secondary index
Query: `{patient_id: "0001"}` (trả về 54 doc)

| Collection | Plan | keys | docs | wall_ms |
|---|---|---|---|---|
| images (40k) | COLLSCAN | 0 | 40.562 | 26.6 |
| images (40k) | IXSCAN → FETCH (`pid_1`) | 54 | 54 | 3.5 |
| images_big (405k) | COLLSCAN | 0 | 405.620 | 151.1 |
| images_big (405k) | IXSCAN → FETCH (`pid_1`) | 54 | 54 | **1.2** |
| tra theo `_id` | EXPRESS_IXSCAN (`_id_`) | 1 | 1 | 0.4–1.0 |

→ Dữ liệu ×10 thì COLLSCAN chậm ×5.7, còn IXSCAN không đổi (O(n) so với O(log n)).

## exp2: Compound index
Query UC2: `{patient_id: "0001", sequence: "T2", plane: "SAGITTAL"}` + sort `{series_folder, instance_number}` (15 kết quả)

| Case | Index | Plan | keys | docs |
|---|---|---|---|---|
| 2a | (không) | COLLSCAN → SORT | 0 | 40.562 |
| 2b | `{patient_id}` | IXSCAN → FETCH → SORT | 54 | 54 |
| 2c | `{patient_id, sequence, plane}` | IXSCAN → FETCH → **SORT** | 15 | 15 |
| 2d | `{patient_id, sequence, plane, series_folder, instance_number}` (ESR) | IXSCAN → FETCH (**không SORT**) | 15 | 15 |
| 2e | `{plane, sequence, patient_id}` | IXSCAN → FETCH → SORT | 15 | 15 |
| 2f | prefix đúng: query `{patient_id}` trên `{patient_id, sequence, plane}` | IXSCAN → FETCH | 54 | 54 |
| 2g | prefix sai: query `{patient_id}` trên `{plane, sequence, patient_id}` | IXSCAN → FETCH | **40.562** | **40.562** |
| 2h | range trước: `{patient_id, sequence}` với patient_id ∈ [0100, 0150), sequence = T2 | IXSCAN → FETCH | 1.630 | 1.586 |
| 2i | equality trước: `{sequence, patient_id}` | IXSCAN → FETCH | 1.586 | 1.586 |
| 2j | planner tự chọn | chọn ESR (2d), loại 5 plan khác | 15 | 15 |

→ Khi chỉ có equality thì thứ tự field không ảnh hưởng (2c và 2e như nhau). Thứ tự quan trọng ở 3 chỗ: **prefix** (2f/2g), **sort** (2c/2d) và **range** (2h/2i).
Ở 2h, range đứng trước chỉ tốn thêm 44 key vì MongoDB *seek* qua các đoạn key không khớp, chứ không quét tuần tự như lý thuyết giả định.

## exp3: Cấu trúc B-tree (WiredTiger, page 16KB)

| Index | Entries | Size | Byte/entry | Leaf page (100%) | Fan-out | Số tầng (công thức) | **Độ sâu thật (WT)** |
|---|---|---|---|---|---|---|---|
| `_id_` | 40.562 | 5.00 MB | 123.2 | 308 | 124 | 3 | 3 |
| `pid_1` | 40.562 | 0.35 MB | 8.7 | 22 | 981 | 2 | 3 |
| `pid_seq_plane_sort` | 40.562 | 0.43 MB | 10.5 | 27 | 885 | 2 | 3 |
| `_id_` | 405.620 | 15.28 MB | 37.7 | 935 | 358 | 3 | 3 |
| `pid_1` | 405.620 | 4.25 MB | 10.5 | 260 | 886 | 2 | 3 |
| `pid_seq_plane_sort` | 405.620 | 4.29 MB | 10.6 | 262 | 882 | 2 | 3 |

→ Số entry ×10, số leaf page ×12, nhưng **độ sâu vẫn 3**. Mỗi lần tra key mất khoảng 3 lần đọc page index cộng 1 lần đọc document (t + 1).
Công thức trong slide ra 2 tầng vì giả định page đầy 100% và không tính root. Thực tế page chỉ đầy một phần sau khi split, và WiredTiger đếm cả root.
Byte/entry nhỏ hơn nhiều so với kích thước key thật vì WiredTiger dùng **prefix compression**.

## exp4: Chi phí index (insert 100k doc, trung vị 3 lần)

| Số index phụ | Tốc độ insert từ client | Tốc độ insert phía server ($merge) | Dung lượng index | Index / data |
|---|---|---|---|---|
| 0 | 100% | 100% | 2.25 MB | 3% |
| 1 | 106% | 91% | 4.42 MB | 7% |
| 3 | 82% | 96% | 8.38 MB | 13% |
| 5 | 74% | 88% | 12.98 MB | 20% |
| 8 | **58%** | **62%** | 22.22 MB | **34%** |

→ Dung lượng index tăng tuyến tính theo số index. Tốc độ ghi giảm rõ từ 5 index trở lên. Ở mức 1–3 index, chênh lệch nằm trong sai số đo (±10–15%).
Chỉ nên so sánh % trong cùng một cột: `$merge` phải tra `_id` của từng document trước khi chèn, nên số tuyệt đối của nó thấp hơn.

## exp5: Multikey index trên mảng `patients.findings`

| Query | Không index (docs) | Có `findings_1` (keys / docs) | Trả về |
|---|---|---|---|
| `{findings: "annular_tear"}` | 575 | 29 / 29 | 29 |
| `{findings: {$all: ["disc_bulge", "annular_tear"]}}` | 575 | 29 / 29 | 26 |

→ 575 document nhưng index có **1.689 key** (1.568 phần tử mảng cộng key cho mảng rỗng). Đây là secondary index trên non-key field.

## exp6: Covered query

| Projection | Plan | keys | docs |
|---|---|---|---|
| chỉ field có trong index | IXSCAN → PROJECTION_COVERED | 15 | **0** |
| thêm `file_id` | IXSCAN → FETCH → PROJECTION_SIMPLE | 15 | 15 |

## exp7: Clustered collection (Index-Organized) vs collection thường

| Collection | Range 2.000 `_id` | Point | Trên đĩa | Index `_id` riêng |
|---|---|---|---|---|
| thường | IXSCAN → FETCH, 2.000 keys + 2.000 docs | EXPRESS_IXSCAN | 5.80 MB | 2.74 MB |
| clustered | CLUSTERED_IXSCAN, 0 key + 2.001 docs | EXPRESS_CLUSTERED_IXSCAN | **2.91 MB** | **không có** |

## exp8: Sparse & partial index

| Case | Kết quả |
|---|---|
| `image_count`: index thường vs sparse | 575 key vs **515 key** (bỏ 60 doc thiếu field) |
| tìm doc thiếu field `{image_count: null}` | sparse không dùng được → COLLSCAN |
| partial `patient_age` với điều kiện `>= 60` | chỉ 5.003 / 40.562 key |
| query `age >= 70` (nằm trong điều kiện partial) | IXSCAN, 3.070 keys |
| query `age >= 50` (ra ngoài điều kiện partial) | COLLSCAN 40.562 docs |

→ "Sparse" trong MongoDB nghĩa là bỏ qua document thiếu field. Trong sách, "sparse/nondense" nghĩa là chỉ có entry cho một số giá trị (ví dụ block anchor). Hai khái niệm khác nhau.

## exp9: Text index vs regex trên `notes`

| Case | Plan | keys | docs | Trả về |
|---|---|---|---|---|
| `/annular/i`, không index | COLLSCAN | 0 | 575 | 32 |
| `/^LSS MRI/` trên B-tree `notes_1` | IXSCAN (có bound) | 305 | 304 | 304 |
| `/annular/i` trên B-tree `notes_1` | IXSCAN (quét cả index) | 515 | 32 | 32 |
| `$text: "annular"` | IXSCAN → FETCH → TEXT_MATCH | **32** | 32 | 32 |

## exp10: Hashed vs B-tree

| Query | Hashed | B-tree |
|---|---|---|
| equality `patient_id = 0001` | IXSCAN, 54 keys | IXSCAN, 54 keys |
| range `0001..0005` | **COLLSCAN 40.562 docs** | IXSCAN, 293 keys |

## exp11: Vector index HNSW (`$vectorSearch`, top-10, 512 chiều, cosine)

| Phạm vi | Exact | ANN nc=10 | nc=20 | nc=50 | nc=100 | nc=500 | nc=1000 |
|---|---|---|---|---|---|---|---|
| có filter T2 SAG (~9.5k vector), ms | 18.2 | 7.0 | 6.9 | 7.2 | 7.0 | 10.0 | 10.4 |
| có filter, recall@10 | 1.00 | 0.80 | 0.80 | 1.00 | 1.00 | 1.00 | 1.00 |
| không filter (~40k vector), ms | 10.3 | 3.3 | 5.9 | 4.2 | 4.9 | 7.2 | 8.5 |
| không filter, recall@10 | 1.00 | 0.90 | 0.90 | 1.00 | 1.00 | 1.00 | 1.00 |

→ numCandidates ≥ 50 thì recall đạt 1.0. Tăng numCandidates thì thời gian tăng dần. ANN nhanh hơn exact khoảng 1.5–3 lần ở quy mô này.
