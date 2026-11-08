# DBMS Assignment – MongoDB với dữ liệu MRI cột sống thắt lưng

Assignment nhóm môn Hệ quản trị Cơ sở dữ liệu (HCMUT): dùng MongoDB lưu và truy vấn dữ liệu text + ảnh MRI,
qua đó trình bày cách DBMS xử lý **indexing**, **query processing & optimization**, **transaction** và
**concurrency control**, có so sánh lý thuyết trên lớp với sản phẩm thực tế.

- Kết quả thí nghiệm Indexing: [`results/SUMMARY.md`](results/SUMMARY.md)
- Hướng dẫn chạy demo Indexing: [`DEMO_INDEXING.md`](DEMO_INDEXING.md)

## 1. Dữ liệu

| Dataset | Nội dung | Nguồn |
|---|---|---|
| Lumbar Spine MRI Dataset | 515 bệnh nhân, 48.345 lát cắt DICOM (`.ima`), ~13GB sau khi giải nén | [Mendeley k57fr854j2](https://data.mendeley.com/datasets/k57fr854j2/2) |
| Radiologists Notes | `Radiologists Report.xlsx`: 575 patient ID, 515 có ghi chú | [Mendeley s6bgczr8s2](https://data.mendeley.com/datasets/s6bgczr8s2/2) |

Cả hai dataset dùng license CC BY 4.0. **Repo không chứa dữ liệu gốc**, mỗi người tự tải về `~/Downloads`.

Cấu trúc thư mục ảnh: `01_MRI_Data/<patient>/<study>/<series>/*.ima`, ví dụ `0001/L-SPINE_LSS_20160309_.../T2_TSE_SAG_384_0002/`.

## 2. Kiến trúc

```
Docker: mongodb/mongodb-atlas-local:8.0   (cổng 27018 trên máy host)
  ├─ mongod (WiredTiger), single-node replica set  → có multi-document transaction
  └─ mongot                                        → có Search / $vectorSearch

database spine_mri
  ├─ patients     575 doc   notes, levels[], findings[], studies[] (embed), image_count
  ├─ images       40.562 doc metadata DICOM, sequence/plane/series_type, file_id, embedding[512]
  ├─ mri.files    40.562 doc  GridFS: thông tin file
  └─ mri.chunks   52.846 doc  GridFS: dữ liệu file .ima, nén zstd (9.59GB → 5.37GB trên đĩa)
```

**Vì sao dùng `mongodb-atlas-local` mà không dùng `mongo:7`:** image này chạy sẵn replica set (bắt buộc cho transaction)
và có mongot (bắt buộc cho `$vectorSearch` ở use case 3). Atlas free tier (M0) giới hạn 512MB nên không chứa nổi dữ liệu ảnh.

**Data model:**
```js
patients { _id: "0001", patient_no: 1, notes: "L4-5: degenerative annular disc bulge ...", has_notes: true,
           levels: ["L4-L5"], findings: ["annular_tear", "disc_bulge", ...], sex: "M", image_count: 54,
           studies: [{ name, date: "2016-03-09", age: 53, series: [{ name, sequence, plane, slices }] }] }

images   { _id: "<SOPInstanceUID>", patient_id: "0001", study_date, series_folder: "T2_TSE_SAG_384_0002",
           series_type: "PRIMARY", sequence: "T2", plane: "SAGITTAL", instance_number, slice_location,
           rows, columns, pixel_spacing, patient_age, patient_sex,
           file_id: ObjectId(→ mri.files), embedding: [512 số], embedding_model: "resnet18" }
```
- `levels` và `findings` được trích từ notes bằng regex (`load_notes.py`).
- `series_type`: chỉ nạp ảnh **PRIMARY**. Bỏ qua LOCALIZER (ảnh định vị) và DERIVED (POSDISP, ảnh dựng lại), tổng cộng 7.783 slice.
- `plane` được tính từ `ImageOrientationPatient` trong header DICOM, không dựa vào tên series.

## 3. Cài đặt (khoảng 30 phút, phần lớn là thời gian nạp ảnh)

Yêu cầu: Docker Desktop, Python 3.11+, khoảng 15GB đĩa trống (dataset giải nén ~13GB, DB ~6GB).

```bash
# 1. DB
docker compose up -d
docker compose ps                       # đợi STATUS = (healthy)

# 2. Python
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Kiểm tra + tạo schema
python scripts/check_connection.py     # phải có 4 dòng [OK]
python scripts/init_db.py --drop

# 4. Nạp dữ liệu
python scripts/load_notes.py  --file "$HOME/Downloads/Radiologists Notes for Lumbar Spine MRI Dataset/Radiologists Report.xlsx"
python scripts/load_images.py --root "$HOME/Downloads/01_MRI_Data" --limit-patients 3 --dry-run   # xem trước
python scripts/load_images.py --root "$HOME/Downloads/01_MRI_Data"                                 # ~10 phút

# 5. Embedding cho use case 3 (~7 phút trên Mac M-series)
pip install -r requirements-embed.txt
python scripts/embed_images.py --model resnet18
```

Kết nối Compass: `mongodb://admin:admin123@localhost:27018/?directConnection=true`

> Tài khoản `admin/admin123` chỉ dùng cho DB chạy local. Muốn đổi thì copy `.env.example` thành `.env` rồi sửa (file `.env` đã nằm trong `.gitignore`).

## 4. Ba use case của đề

```bash
# UC1: cho patient ID → lấy text + ảnh (1 aggregation $lookup patients ⨝ images)
python scripts/usecases.py uc1 --patient 0001 --export 3

# UC2: cho patient ID + điều kiện (sequence/plane, finding trong notes)
python scripts/usecases.py uc2 --patient 0001 --sequence T2 --plane SAGITTAL --export 5 --explain
python scripts/usecases.py uc2 --patient 0001 --sequence T1 --plane AXIAL --finding disc_bulge

# UC3: tìm ảnh MRI tương tự ảnh của một patient ($vectorSearch, ResNet18 512 chiều, cosine, HNSW)
python scripts/similar.py --patient 0001 --export
python scripts/similar.py --patient 0001 --exact      # exact search để so sánh với ANN
```
Ảnh PNG được ghi ra `out/` (không đưa lên git).

## 5. Demo các tiêu chí chấm điểm

| Tiêu chí | Script | Người phụ trách |
|---|---|---|
| Indexing | `scripts/demo_indexing.py` (11 experiment), xem [`DEMO_INDEXING.md`](DEMO_INDEXING.md) | Brian + teammate |
| Query processing & optimization | (chưa có) | |
| Transaction | (chưa có) | |
| Concurrency control | (chưa có) | |

```bash
python scripts/demo_indexing.py list
python scripts/demo_indexing.py all        # chạy exp1..exp11, cuối cùng tạo bộ index khuyến nghị
python scripts/demo_indexing.py final      # khôi phục bộ index khuyến nghị sau khi thử nghiệm
```

**Lưu ý khi dùng chung DB:** `demo_indexing.py` xoá và tạo lại index phụ trên `images`/`patients`.
Sau khi demo xong, chạy `final` để trả DB về bộ index chuẩn:
- `images.pid_seq_plane_sort`
- `patients.findings_1`
- `patients.notes_text`
- vector index `img_vec`

## 6. Cấu trúc repo

```
docker-compose.yml        MongoDB Atlas Local (mongod + mongot)
requirements.txt          pymongo, pydicom, numpy, openpyxl, python-dotenv
requirements-embed.txt    torch, torchvision (chỉ cần cho use case 3)
scripts/
  common.py               kết nối DB (đọc .env nếu có)
  check_connection.py     kiểm tra kết nối, replica set, transaction, vector search
  init_db.py              tạo collection + JSON Schema validator + GridFS nén zstd
  load_notes.py           Excel → patients (trích levels / findings)
  load_images.py          DICOM → GridFS + images, gộp studies vào patients ($merge)
  embed_images.py         ResNet18 embedding + vector index img_vec
  usecases.py             UC1, UC2 (+ xuất PNG, explain)
  similar.py              UC3 ($vectorSearch)
  demo_indexing.py        11 experiment cho tiêu chí Indexing
results/
  SUMMARY.md              tóm tắt kết quả Indexing (đọc file này trước)
  indexing_results.csv    số liệu đã làm sạch (1 dòng / phép đo)
  indexing_results_raw.csv  log gốc, gồm cả các lần chạy lỗi trước đó
  explain/*.json          output explain đầy đủ của từng phép đo
```

## 7. Xử lý sự cố

| Lỗi | Cách xử lý |
|---|---|
| `zsh: command not found: python` | Chưa `source .venv/bin/activate`, hoặc dùng `python3` |
| `Connection reset by peer` dù container vẫn `healthy` | Docker Desktop vừa restart. Chạy `docker compose down && docker compose up -d` (dữ liệu vẫn giữ) |
| Mất dữ liệu | **Không** chạy `docker compose down -v`, lệnh này xoá cả volume |
| Đường dẫn có `~` trong dấu `"..."` không chạy | Dùng `$HOME` thay cho `~` |
