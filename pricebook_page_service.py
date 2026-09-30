"""프라이스북 — 메인 공통 1페이지 / 마지막페이지 + 파워소스 이미지"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone, timedelta

from werkzeug.utils import secure_filename

KST = timezone(timedelta(hours=9))
POWER_SOURCE_CATEGORY_NUMBER = 1
PAGE_TYPES = {'first', 'last', 'content', 'accessory'}
PAGE_TYPE_LABELS = {
    'first': '1페이지',
    'last': '마지막페이지',
    'content': '이미지',
    'accessory': '하단악세서리',
}
VARIANTS = {'std', 'W'}
ALLOWED_EXT = {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp'}


def now_kst() -> str:
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


def upload_base_dir() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads', 'pricebook_pages')
    os.makedirs(path, exist_ok=True)
    return path


def page_dir(variant: str, page_type: str, option_item_id: int | None = None) -> str:
    if option_item_id:
        path = os.path.join(upload_base_dir(), str(option_item_id), variant, page_type)
    else:
        path = os.path.join(upload_base_dir(), 'global', variant, page_type)
    os.makedirs(path, exist_ok=True)
    return path


def normalize_page_type(value: str | None) -> str | None:
    if value is None:
        return None
    key = str(value).strip().lower()
    aliases = {
        'first': 'first',
        '1': 'first',
        '1page': 'first',
        'page1': 'first',
        'first_page': 'first',
        'last': 'last',
        'lastpage': 'last',
        'last_page': 'last',
        'final': 'last',
        'content': 'content',
        'image': 'content',
        'images': 'content',
        'middle': 'content',
        'accessory': 'accessory',
        'accessories': 'accessory',
        'bottom_accessory': 'accessory',
        '하단악세서리': 'accessory',
    }
    return aliases.get(key)


def normalize_variant(value: str | None) -> str:
    if value is None or str(value).strip() == '':
        return 'std'
    key = str(value).strip().upper()
    if key in {'STD', 'STANDARD', 'BASE', 'DEFAULT', 'N', 'NONE'}:
        return 'std'
    if key in {'W', 'WATER'}:
        return 'W'
    return 'std'


def ensure_pricebook_page_tables(cursor) -> None:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pricebook_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            option_item_id INTEGER,
            variant TEXT DEFAULT 'std',
            page_type TEXT NOT NULL,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            uploaded_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (option_item_id) REFERENCES option_items(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('PRAGMA table_info(pricebook_pages)')
    columns = {row[1] for row in cursor.fetchall()}
    if 'option_item_id' not in columns:
        cursor.execute('ALTER TABLE pricebook_pages ADD COLUMN option_item_id INTEGER')
    if 'variant' not in columns:
        cursor.execute("ALTER TABLE pricebook_pages ADD COLUMN variant TEXT DEFAULT 'std'")
        cursor.execute("UPDATE pricebook_pages SET variant = 'std' WHERE variant IS NULL OR TRIM(variant) = ''")
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_pricebook_pages_page_type
        ON pricebook_pages(page_type)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_pricebook_pages_option_item
        ON pricebook_pages(option_item_id)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_pricebook_pages_global_variant_type
        ON pricebook_pages(variant, page_type)
    ''')


def _get_power_source_category_id(cursor) -> int | None:
    cursor.execute(
        '''
        SELECT id FROM option_categories
        WHERE category_number = ?
        LIMIT 1
        ''',
        (POWER_SOURCE_CATEGORY_NUMBER,),
    )
    row = cursor.fetchone()
    return int(row[0]) if row else None


def _extract_model_name(item_name: str) -> str:
    name = (item_name or '').strip()
    if not name:
        return ''
    start = name.find('(')
    if start < 0:
        return name
    depth = 0
    for i in range(start, len(name)):
        ch = name[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                inner = name[start + 1:i].strip()
                return inner or name
    return name


def _folder_label(item_name: str, item_code: str) -> str:
    model = _extract_model_name(item_name)
    if model:
        return model
    code = (item_code or '').strip()
    return code or '이름 없음'


def list_power_source_folders(cursor) -> list[dict]:
    """옵션조회 [1] 파워소스 목록 + 이미지 등록 수"""
    ensure_pricebook_page_tables(cursor)
    category_id = _get_power_source_category_id(cursor)
    if not category_id:
        return []

    cursor.execute(
        '''
        SELECT oi.id, oi.item_name, oi.item_code, oi.description,
               SUM(CASE WHEN pp.page_type = 'content' AND COALESCE(pp.variant, 'std') IN ('std', '') THEN 1 ELSE 0 END) AS std_count,
               SUM(CASE WHEN pp.page_type = 'content' AND UPPER(COALESCE(pp.variant, '')) = 'W' THEN 1 ELSE 0 END) AS w_count,
               SUM(CASE WHEN pp.page_type = 'accessory' THEN 1 ELSE 0 END) AS accessory_count
        FROM option_items oi
        LEFT JOIN pricebook_pages pp ON pp.option_item_id = oi.id
        WHERE oi.category_id = ?
        GROUP BY oi.id, oi.item_name, oi.item_code, oi.description
        ORDER BY oi.id
        ''',
        (category_id,),
    )
    folders = []
    for row in cursor.fetchall():
        base_label = _folder_label(row[1], row[2])
        folders.append({
            'option_item_id': row[0],
            'item_name': row[1] or '',
            'item_code': row[2] or '',
            'description': row[3] or '',
            'std_count': row[4] or 0,
            'w_count': row[5] or 0,
            'accessory_count': row[6] or 0,
            'image_count': (row[4] or 0) + (row[5] or 0),
            'folder_label': base_label,
            'folder_label_w': f'{base_label} W',
        })
    return folders


def _safe_filename(filename: str) -> str:
    name = os.path.basename((filename or '').strip()).replace('\x00', '')
    name = re.sub(r'[\*\?"<>|]', '_', name)
    root, ext = os.path.splitext(name)
    if not ext:
        return secure_filename(name) or 'page.pdf'
    safe_root = secure_filename(root) or 'page'
    return safe_root + ext.lower()


def _row_to_dict(row) -> dict:
    page_type = row[3] or ''
    variant = normalize_variant(row[2])
    return {
        'id': row[0],
        'option_item_id': row[1],
        'variant': variant,
        'page_type': page_type,
        'page_label': PAGE_TYPE_LABELS.get(page_type, page_type),
        'filename': row[4],
        'filepath': row[5],
        'file_size': row[6] or 0,
        'size_kb': round((row[6] or 0) / 1024, 1),
        'uploaded_by': row[7] or '',
        'created_at': row[8],
    }


def _variant_clause(variant: str) -> tuple[str, list]:
    normalized = normalize_variant(variant)
    if normalized == 'std':
        return "(COALESCE(variant, 'std') IN ('std', ''))", []
    return "UPPER(COALESCE(variant, '')) = ?", [normalized]


def list_pages(
    cursor,
    page_type: str | None = None,
    variant: str | None = None,
    option_item_id: int | None = None,
) -> list[dict]:
    ensure_pricebook_page_tables(cursor)
    normalized_type = normalize_page_type(page_type) if page_type else None
    clauses = []
    params: list = []

    if option_item_id is None:
        clauses.append('option_item_id IS NULL')
    else:
        clauses.append('option_item_id = ?')
        params.append(int(option_item_id))

    if variant is not None:
        v_clause, v_params = _variant_clause(variant)
        clauses.append(v_clause)
        params.extend(v_params)
    if normalized_type:
        clauses.append('page_type = ?')
        params.append(normalized_type)

    where = 'WHERE ' + ' AND '.join(clauses)
    cursor.execute(
        f'''
        SELECT id, option_item_id, variant, page_type, filename, filepath, file_size, uploaded_by, created_at
        FROM pricebook_pages
        {where}
        ORDER BY
            CASE page_type WHEN 'first' THEN 1 WHEN 'last' THEN 2 WHEN 'content' THEN 3 WHEN 'accessory' THEN 4 ELSE 5 END,
            created_at DESC,
            id DESC
        ''',
        params,
    )
    return [_row_to_dict(row) for row in cursor.fetchall()]


def get_latest_by_type(cursor, page_type: str, variant: str = 'std') -> dict | None:
    pages = list_pages(cursor, page_type=page_type, variant=variant, option_item_id=None)
    return pages[0] if pages else None


def get_global_latest(cursor, variant: str = 'std') -> dict:
    return {
        'first': get_latest_by_type(cursor, 'first', variant=variant),
        'last': get_latest_by_type(cursor, 'last', variant=variant),
    }


def list_content_images(cursor, option_item_id: int, variant: str = 'std') -> list[dict]:
    return list_pages(
        cursor,
        page_type='content',
        variant=variant,
        option_item_id=option_item_id,
    )


def list_accessory_images(cursor, option_item_id: int) -> list[dict]:
    """파워소스별 하단악세서리 이미지"""
    return list_pages(
        cursor,
        page_type='accessory',
        variant=None,
        option_item_id=option_item_id,
    )


def get_page_record(cursor, page_id: int) -> dict | None:
    ensure_pricebook_page_tables(cursor)
    cursor.execute(
        '''
        SELECT id, option_item_id, variant, page_type, filename, filepath, file_size, uploaded_by, created_at
        FROM pricebook_pages
        WHERE id = ?
        ''',
        (page_id,),
    )
    row = cursor.fetchone()
    return _row_to_dict(row) if row else None


def save_uploaded_page(
    cursor,
    page_type: str,
    file,
    uploaded_by: str = '',
    variant: str = 'std',
    option_item_id: int | None = None,
) -> tuple[dict | None, str | None]:
    ensure_pricebook_page_tables(cursor)

    normalized = normalize_page_type(page_type)
    # option_item_id 가 있으면 first/last/accessory 외에는 content
    if option_item_id and normalized not in {'first', 'last', 'accessory'}:
        normalized = 'content'
    if not normalized or normalized not in PAGE_TYPES:
        return None, '페이지 타입이 올바르지 않습니다. (1페이지/마지막페이지/이미지/하단악세서리)'

    # 하단악세서리는 variant 고정(std) — 파워소스 폴더에 귀속
    if normalized == 'accessory':
        normalized_variant = 'std'
    else:
        normalized_variant = normalize_variant(variant)
        if normalized_variant not in VARIANTS:
            return None, '변형 타입이 올바르지 않습니다.'

    # 공통 1/마지막 페이지
    if normalized in {'first', 'last'}:
        if option_item_id is not None:
            return None, '1페이지/마지막페이지는 메인에서만 등록합니다.'
    # 파워소스별 이미지 / 하단악세서리
    elif normalized in {'content', 'accessory'}:
        if not option_item_id:
            return None, '이미지 등록에는 파워소스 선택이 필요합니다.'
        category_id = _get_power_source_category_id(cursor)
        if not category_id:
            return None, '파워소스 카테고리를 찾을 수 없습니다.'
        try:
            option_item_id = int(option_item_id)
        except (TypeError, ValueError):
            return None, '유효하지 않은 파워소스입니다.'
        cursor.execute(
            'SELECT id FROM option_items WHERE id = ? AND category_id = ?',
            (option_item_id, category_id),
        )
        if not cursor.fetchone():
            return None, '유효하지 않은 파워소스 옵션입니다.'

    if not file or not getattr(file, 'filename', None) or not file.filename.strip():
        return None, '파일이 선택되지 않았습니다.'

    raw_name = file.filename.strip()
    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in ALLOWED_EXT:
        return None, '허용 형식: .pdf, .png, .jpg, .jpeg, .gif, .webp'

    filename = _safe_filename(raw_name)
    target_dir = page_dir(normalized_variant, normalized, option_item_id=option_item_id)
    filepath = os.path.join(target_dir, filename)
    if os.path.isfile(filepath):
        base, extension = os.path.splitext(filename)
        counter = 1
        while os.path.isfile(filepath):
            filename = f'{base}_{counter}{extension}'
            filepath = os.path.join(target_dir, filename)
            counter += 1

    file.save(filepath)
    file_size = os.path.getsize(filepath)
    created_at = now_kst()

    cursor.execute(
        '''
        INSERT INTO pricebook_pages (
            option_item_id, variant, page_type, filename, filepath, file_size, uploaded_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            option_item_id,
            normalized_variant,
            normalized,
            filename,
            filepath,
            file_size,
            uploaded_by or '',
            created_at,
        ),
    )
    page_id = cursor.lastrowid
    return {
        'id': page_id,
        'option_item_id': option_item_id,
        'variant': normalized_variant,
        'page_type': normalized,
        'page_label': PAGE_TYPE_LABELS[normalized],
        'filename': filename,
        'filepath': filepath,
        'file_size': file_size,
        'size_kb': round(file_size / 1024, 1),
        'uploaded_by': uploaded_by or '',
        'created_at': created_at,
    }, None


def delete_page(cursor, page_id: int) -> bool:
    record = get_page_record(cursor, page_id)
    if not record:
        return False
    try:
        if os.path.isfile(record['filepath']):
            os.remove(record['filepath'])
    except OSError:
        pass
    cursor.execute('DELETE FROM pricebook_pages WHERE id = ?', (page_id,))
    return True
