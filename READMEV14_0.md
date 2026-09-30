# PNS V14.0 최종 소스 복구 가이드

**목적**: pns 폴더의 최종 소스를 전체 확인하여 기록. **READMEV14_0.md만 읽으면 현재와 동일한 로직·소스로 복구** 가능하도록 작성.

---

## 목차

1. 프로젝트 구조  
2. 실행·설정  
3. 데이터베이스 스키마  
4. 라우트 목록  
5. V14.0 반영 요구사항 및 복구 포인트  
6. 핵심 상수·경로  
7. 복구 체크리스트  

---

## 1. 프로젝트 구조

```
pns/
├── app.py                 # Flask 메인 앱, 모든 라우트·API
├── pns.py                 # WeldingOptionSystem 클래스, DB 초기화·옵션 로직
├── logic_rules_data.py    # 로직 제한조건 상수 (DETAILED_RULES)
├── requirements.txt       # 의존성
├── start.bat              # Windows 실행 스크립트
├── nginx.conf             # Nginx 리버스 프록시 설정
├── welding_options.db     # SQLite DB (실행 시 생성)
├── quote_templates/      # 계약서/팩킹리스트/인보이스 엑셀 템플릿 저장
├── static/
│   ├── css/style.css
│   └── js/main.js
├── templates/             # Jinja2 HTML 템플릿
│   ├── base.html
│   ├── index.html, index_en.html
│   ├── select_options.html, lookup.html, lookup_en.html
│   ├── select_options_view.html, select_options_view_en.html
│   ├── option_management.html, option_price_management.html
│   ├── set_price_*.html (view, view_pns, view_en, input, create)
│   ├── export_quotation.html, export_quotation_new.html, export_quotation_new_public.html
│   ├── export_price_dashboard.html, export_price_dashboard_new.html
│   ├── quote_template_edit.html, doc_template_upload.html
│   ├── logic_rules_dashboard.html, logic_rules_settings.html
│   ├── db_management.html, db_login.html, result.html
│   ├── part_price_*.html, trolley_spare_parts.html
│   ├── pns_login.html
│   └── partials/select_options_shared.html
├── scripts/               # check_coolant_name.py, update_coolant_name.py
├── delete_all_prices.py, update_all_prices.py
└── uploads/part_images/  # 파트 가격 이미지 업로드 저장
```

---

## 2. 실행·설정

### 2.1 의존성 (requirements.txt)

```
flask>=2.3.0
openpyxl>=3.1.0
reportlab>=4.0.0
```

### 2.2 앱 설정 (app.py 상단)

- `app.secret_key = 'welding_system_secret_key_2025'`
- `app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024`  # 50MB
- `app.config['UPLOAD_FOLDER']` = 동일 디렉터리 `uploads/part_images`
- `app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}`
- `app.config['JSON_AS_ASCII'] = False`
- CORS: `after_request`에서 `Access-Control-Allow-Origin: *` 등 추가

### 2.3 서버 기동 (app.py 마지막)

- `app.run(debug=True, host='0.0.0.0', port=5000, threaded=True, use_reloader=False)`
- 5000 사용 중이면 5001 시도

### 2.4 start.bat

- chcp 65001, cd /d "%~dp0"
- venv 있으면 activate
- pip install -r requirements.txt
- python app.py

### 2.5 Nginx (nginx.conf)

- server_name: pnsoptions.com www.pnsoptions.com
- client_max_body_size 50M
- location / → proxy_pass http://127.0.0.1:5000
- location /static/ → alias D:/Warehouse/wwwroot/pns/static/
- proxy 타임아웃 300s

---

## 3. 데이터베이스 스키마

### 3.1 pns.py에서 생성 (WeldingOptionSystem._initialize_tables)

- **option_categories**: id, category_name, category_number (UNIQUE) — 1~11만 시드
- **option_items**: id, category_id, item_name, item_code, description
- **selected_combinations**: id, combination_code, set_code, call_arc_code, selected_options, price, created_at
- **option_prices**: id, set_code, category_number, option_name, price, created_at, updated_at, UNIQUE(set_code, category_number, option_name)
- **logic_rules**: id, order_index, power_source_code, power_source_value, type_code, type_value, actions_json
- **quote_template**: id, company_name, company_address, company_phone, company_email, company_logo, quote_title, footer_note, terms_conditions, created_at, updated_at
- **export_quotation_products**: id, product_code, product_name, price_book_price, product_cost, created_at, updated_at
- **schema_migrations**: (마이그레이션 추적)

### 3.2 app.py에서 생성·사용 (option_base_prices, export_quotation_new_products)

- **option_base_prices**  
  `CREATE TABLE IF NOT EXISTS option_base_prices (id INTEGER PRIMARY KEY AUTOINCREMENT, category_number INTEGER NOT NULL, option_name TEXT NOT NULL, price REAL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(category_number, option_name))`  
  - 1~11: 카테고리별 기본가. 12(기타): 코드관리에서만 등록·삭제.

- **export_quotation_new_products**  
  `CREATE TABLE IF NOT EXISTS export_quotation_new_products (id INTEGER PRIMARY KEY AUTOINCREMENT, product_code TEXT, product_name TEXT, price_book_price REAL, purchase_price REAL, category_number INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)`  
  - ALTER로 price, unit_price, product_cost 등 마이그레이션 가능하도록 처리.

### 3.3 기타 app.py에서 생성하는 테이블

- part_prices, part_price_folders, part_price_subfolders, part_price_images, part_price_image_parts, part_price_image_matches (파트 가격·이미지)
- export_quotation_products (구 수출견적서 제품)

---

## 4. 라우트 목록 (app.py)

| 메서드 | 경로 | 용도 |
|--------|------|------|
| GET | / | 메인 (index.html / index_en.html) |
| GET | /test, /api/test, /test-simple, /api/option/test, /api/option/test2 | 테스트 |
| GET | /api/all-options | 전체 옵션 목록 |
| GET/POST | /api/logic_rules | 로직 규칙 목록·생성 |
| PUT/DELETE | /api/logic_rules/<id> | 로직 규칙 수정·삭제 |
| GET | /select_options | 옵션선택(PNS용) |
| GET | /lookup | 조회 |
| POST | /api/select_options, /api/select_options/by-options | 옵션 선택 제출·코드 조회 |
| POST | /api/lookup | 조회 API |
| GET | /api/code_list, /api/options | 코드/옵션 목록 |
| POST | /api/delete_code | 코드 삭제 |
| GET | /db_management, /option_management, /option_price_management | DB/코드/옵션가격 관리 페이지 |
| GET | /logic_rules, /logic_rules/settings | 로직 규칙 페이지 |
| GET | /api/categories | 카테고리 목록 (12 기타 포함) |
| GET | /api/options/category/<id> | 카테고리별 옵션 (12번은 option_base_prices) |
| POST | /api/db/login, /api/pns/login, /api/pns/logout, /api/db/logout | 로그인/로그아웃 |
| GET | /api/db/tables, /api/db/table_info/<name>, /api/db/add_column, /api/db/delete_column, /api/db/dashboard_info | DB 관리 API |
| POST | /api/generate_excel | 엑셀 생성 |
| GET | /download/<filename> | 파일 다운로드 |
| POST | /api/option/add, /api/option/update, /api/option/delete | 옵션 추가/수정/삭제 (12번 기타는 option_base_prices) |
| GET | /result, /set_price_view, /set_price_view_pns, /set_price_input, /set_price_create | 세트가격·결과 페이지 |
| GET | /export_quotation, /export_price_dashboard, /export_price_dashboard_new | 수출견적서·대시보드 |
| GET | /export_quotation_new | 수출견적서(스태프) |
| GET | /export_quotation_new_public | 수출견적서(고객) |
| POST | /api/export_quotation/upload, /api/export_quotation/new/upload | 수출 견적 엑셀 업로드 |
| POST | /api/part_price/upload | 파트 가격 업로드 |
| GET | /part_price_dashboard, /part_price_image_upload, /part_price_image_price_match, /part_price/trolley, /part_price/cooling_unit | 파트 가격 페이지 |
| GET/POST | /api/part_price/* | 파트 가격·이미지·폴더·매칭 API (다수) |
| GET | /quote_template | 견적서 양식 편집 페이지 |
| GET | /doc_template/<doc_type> | 계약서/팩킹리스트/인보이스(엑셀) 업로드 페이지 |
| POST | /api/doc_template/<doc_type>/excel/fill | 서류 엑셀 채우기 (제품 데이터 반영) |
| GET/POST | /api/doc_template/<doc_type>/excel | 서류 템플릿 다운로드/업로드 |
| GET/POST | /api/quote_template/excel | 견적서 양식 엑셀 다운로드/업로드 |
| GET/POST | /api/quote_template | 견적서 양식 API |
| POST | /api/export_quotation/products, /api/export_quotation/download_excel | 수출 견적 제품·다운로드 |
| GET | /api/export_price/dashboard, /api/export_price/dashboard_new | 수출 가격 대시보드 |
| POST | /api/export_price/update, /api/export_price/add_new, /api/export_price/update_new | 수출 가격 수정·추가 |
| POST | /api/export_price/delete, /api/export_price/delete_new, /api/export_price/bulk_delete, /api/export_price/bulk_delete_new | 수출 가격 삭제 |
| POST | /api/export_price/excel, /api/export_quotation/new/download_excel, /api/export_price/excel_new | 수출 견적 엑셀 |
| GET | /api/set_codes, /api/set_price/list, /api/set_price/update, /api/set_price/options | 세트코드·세트가격 |
| GET | /api/set_price/options/excel, /api/set_price/quote | 세트가격 옵션 엑셀·견적 |
| POST | /api/set_price/option_price/update | 세트 옵션 가격 수정 |
| POST | /api/option/base_price/update, /api/option/base_price/batch_update | 옵션 기본가 수정·일괄 |
| GET | /api/option/base_price/list | 옵션 기본가 목록 |
| GET/POST | /api/option/base_price/excel/download, /api/option/base_price/excel/upload | 옵션 기본가 엑셀 |
| POST | /api/set_price/delete_all | 세트가격 전부 삭제 |
| POST/GET | /api/background_image/upload, /api/background_image/test, /api/background_image | 배경 이미지 |
| OPTIONS | /api/delete_code | CORS |

---

## 5. V14.0 반영 요구사항 및 복구 포인트

### 5.1 [12] 기타 카테고리

- **요구**: 옵션선택(PNS/조회)·코드관리에서 [12] 기타 표시. 옵션추가/삭제 시 12번 선택 가능. 12번은 `option_base_prices`(category_number=12)만 사용. 수출견적·엑셀 업로드 시 12번은 option_base_prices에 자동 등록하지 않음.
- **app.py**:  
  - `render_select_options_page`: 12번 없으면 option_base_prices에서 category_number=12 로 synthetic 카테고리 추가.  
  - `/api/categories`: 12번 없으면 `{"id":12,"name":"기타","number":12}` 추가.  
  - `/api/options/category/<id>`: category_id==12 이면 option_items 대신 `SELECT DISTINCT option_name FROM option_base_prices WHERE category_number=12` 등.  
  - `/api/option/add`: category_id==12 이면 option_base_prices에 INSERT (category_number=12, option_name=?, price=NULL), ON CONFLICT DO NOTHING.  
  - `/api/option/delete`: category_id 12 또는 "12", option_code 있으면 `DELETE FROM option_base_prices WHERE category_number=12 AND option_name=?`. option_code 없으면 "기타 항목을 선택해주세요."  
  - 수출 제품 추가/수정/엑셀 업로드: category_number==12 인 경우 option_base_prices INSERT/UPDATE 하지 않음. option_base_prices 업데이트 시 `WHERE ... AND category_number != 12` 사용.
- **templates**: option_management.html — 12개 카테고리, catNum<=12, 삭제 시 value="cat12:CODE", body에 category_id:12, option_code. select_options.html, set_price_create.html, partials/select_options_shared.html — 0/12 항목, 12개 카테고리 문구.

### 5.2 수출견적서(고객) 조회 버튼

- 조회 버튼 색상 변경 (가독성). 해당 페이지 템플릿에서 버튼에 background-color, color 지정.

### 5.3 수출견적서(스태프) UI

- "엑셀 다운로드" → "견적서 다운로드" (번역 키 excelDownload).
- 서류 드롭다운: #docDropdownMenu, .dropdown-item 배경·글자 색 (예: #fff, #212529, hover #f8f9fa).
- 접기/펼치기: #toggleProductInfoBtn 배경 #e9ecef, color #212529, hover #dee2e6.

### 5.4 관리 메뉴 – 계약서/팩킹리스트/인보이스(엑셀)

- base.html 관리 드롭다운: 견적서 양식 편집 아래에 `/doc_template/contract`, `/doc_template/packing_list`, `/doc_template/invoice` 링크 (직접 경로).
- app.py: DOC_TEMPLATE_TYPES, /doc_template/<string:doc_type> → doc_template_upload.html.
- doc_template_upload.html: 제목·파일 선택·업로드·현재 양식 다운로드만. "견적서 양식 편집으로" 버튼 없음.

### 5.5 서류 템플릿 업로드 Permission denied

- POST /api/doc_template/<doc_type>/excel: tempfile.mkstemp(suffix='.xlsx', dir=templates_dir) → file.save(tmppath) → os.replace(tmppath, filepath) 최대 3회 재시도(0.4초 간격). 실패 시 기존 파일 삭제 후 shutil.move 시도. 계속 실패 시 409 + "대상 파일이 다른 프로그램(예: Excel)에서 열려 있을 수 있습니다. 파일을 닫은 후 다시 업로드해 주세요."

### 5.6 서류 채우기 다운로드 (계약서/인보이스/팩킹리스트)

- **엑셀 셀 매핑**
  - 계약서: start_row=21. A21=넘버링(1,2,3…), B21=SAP코드, C21=코드명, D21=수량, E21=최종 판매가 단가, F21=최종 판매가 합계.
  - 인보이스: start_row=17. B17=넘버링, C17=SAP코드, D17=코드명, G17=수량, H17=최종 판매가 단가, I17=최종 판매가 합계.
  - 팩킹리스트: start_row=17. C17=SAP코드, D17=코드명, G17=수량. 단가/합계·넘버링 없음.
- **단가/합계**: 계약서·인보이스는 request body의 finalSalesPrice, finalSalesPriceTotal 사용 (없으면 priceBookPrice, priceBookTotal).
- **API**: POST /api/doc_template/<doc_type>/excel/fill. 라우트는 /excel 보다 먼저 등록.  
  - load_workbook(filepath), ws.active 없으면 400.  
  - 병합 해제: ws.merged_cells.ranges 순회, 쓰기 영역과 겹치면 unmerge. min_col/max_col (또는 min_column/max_column) 호환.  
  - MergedCell 제거: ws._cells에서 쓰기 영역의 MergedCell만 del.  
  - col_no 있으면 ws.cell(row, col_no, value=i+1).  
  - wb.save(buf) 시 IndexError 나면 wb._alignments에 Alignment() 64개 추가 후 최대 5회 재시도.
- **프론트 (export_quotation_new.html)**: [data-doc-type] 클릭 시 products 있으면 discountRateSales, shippingSales로 totalPriceBookSum 계산 후 제품별 finalSalesPrice, finalSalesPriceTotal 계산해 payload에 포함. POST /api/doc_template/<docType>/excel/fill → blob 다운로드. 실패 시 r.text() 후 JSON 파싱 시도, HTML이면 메시지만 표시. 제품 없으면 GET /api/doc_template/<docType>/excel.

### 5.7 에러·호환

- merged_cells 순회·cells 삭제 시 try/except (IndexError, AttributeError, TypeError 등). products 항목 dict 아님 시 빈 dict로 처리. list(ranges_list) 시 IndexError 나면 ranges_copy=[].

---

## 6. 핵심 상수·경로

- **DOC_TEMPLATE_TYPES** (app.py):  
  `{'contract': {'title': '계약서(엑셀)', 'filename': 'contract_template.xlsx'}, 'packing_list': {...packing_list_template.xlsx}, 'invoice': {...invoice_template.xlsx}}`
- **템플릿 디렉터리**: `os.path.join(os.path.dirname(__file__), 'quote_templates')`
- **DB 파일**: 기본 `welding_options.db` (pns.py 동일 디렉터리)
- **get_welding_system()**: 스레드별 WeldingOptionSystem 싱글톤 (thread_local_systems, system_lock)

---

## 7. 복구 체크리스트

1. [12] 기타: 카테고리/옵션 API·옵션추가·삭제에서 12번·option_base_prices 처리. 수출/엑셀 업로드 시 12번 option_base_prices 미반영.
2. 관리 메뉴: 계약서/팩킹리스트/인보이스 링크가 `/doc_template/...` 직접 경로.
3. 서류 채우기: 계약서 A21~F21, 인보이스 B17~I17, 팩킹리스트 C17,D17,G17 매핑. 계약서·인보이스는 finalSalesPrice/finalSalesPriceTotal 사용.
4. 업로드: 임시 파일 → replace → 재시도 → shutil.move 폴백, PermissionError 시 409 메시지.
5. 저장: 병합 해제 → MergedCell 제거 → 셀 기록 → save 시 IndexError 시 _alignments 패딩 재시도.
6. 프론트: 서류 클릭 시 payload에 finalSalesPrice/finalSalesPriceTotal 포함, 에러 시 r.text() 후 JSON fallback.
7. 라우트 순서: /api/doc_template/<doc_type>/excel/fill 가 /excel 보다 먼저 등록.

---

## 8. app.py 핵심 구간 참조 (복구 시 검색용)

- Flask 앱·설정·get_welding_system: 파일 상단 ~ 약 65행
- DOC_TEMPLATE_TYPES·doc_template 라우트·api_doc_template_excel_fill: 약 10423~10600행
- api_doc_template_excel (GET/POST): 약 10601~10680행
- [12] 기타: render_select_options_page(155~180, 518~566), /api/categories(1223~1263), /api/options/category(1265~1312), /api/option/add(1924~2002), /api/option/delete(2082~2120)
- export_quotation_new_products·option_base_prices CREATE/ALTER: 2574~2658, 2735(12번 스킵), 6026~, 6410(12번 스킵), 6447~6454
- 서류 채우기 병합 해제·MergedCell 제거·저장 재시도: 10484~10590

---

이 문서와 체크리스트를 따라 구현하면 현재 pns 폴더의 최종 로직·소스와 동일하게 복구할 수 있습니다.
