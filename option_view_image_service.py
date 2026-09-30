"""옵션조회 [1] 파워소스 × [2] 타입(AIR/WATER) 이미지 및 영역 매핑 관리"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta

from werkzeug.utils import secure_filename

KST = timezone(timedelta(hours=9))
POWER_SOURCE_CATEGORY_NUMBER = 1
TYPE_CATEGORY_NUMBER = 2
ALLOWED_IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
VALID_TYPE_CODES = {'AIR', 'WATER'}
TYPE_CODE_ALIASES = {
    'AIR': 'AIR',
    'WATER': 'WATER',
    'T001': 'AIR',
    'T002': 'WATER',
}


def now_kst() -> str:
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


def upload_base_dir() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads', 'option_view_images')
    os.makedirs(path, exist_ok=True)
    return path


def option_image_dir(option_item_id: int, type_code: str | None = None) -> str:
    path = os.path.join(upload_base_dir(), str(option_item_id))
    if type_code:
        path = os.path.join(path, type_code)
    os.makedirs(path, exist_ok=True)
    return path


def normalize_type_code(value: str | None) -> str | None:
    if value is None:
        return None
    key = str(value).strip().upper()
    if not key:
        return None
    return TYPE_CODE_ALIASES.get(key)


def infer_type_code_from_filename(filename: str | None) -> str | None:
    name = os.path.basename(str(filename or '')).upper()
    if not name:
        return None
    # WATER를 먼저 검사 (AIR 부분문자열 오탐 방지)
    if re.search(r'(^|[_\-\s])WATER([_\-\s\.]|$)', name):
        return 'WATER'
    if re.search(r'(^|[_\-\s])AIR([_\-\s\.]|$)', name):
        return 'AIR'
    return None


def ensure_option_view_image_tables(cursor) -> None:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS option_view_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            option_item_id INTEGER NOT NULL,
            type_code TEXT,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            uploaded_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (option_item_id) REFERENCES option_items(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute("PRAGMA table_info(option_view_images)")
    columns = {row[1] for row in cursor.fetchall()}
    if 'type_code' not in columns:
        cursor.execute('ALTER TABLE option_view_images ADD COLUMN type_code TEXT')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_option_view_images_option_item_id
        ON option_view_images(option_item_id)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_option_view_images_option_type
        ON option_view_images(option_item_id, type_code)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS option_view_image_regions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            category_number INTEGER NOT NULL,
            region_type TEXT NOT NULL DEFAULT 'rect',
            region_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (image_id) REFERENCES option_view_images(id) ON DELETE CASCADE,
            UNIQUE(image_id, category_number)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_option_view_image_regions_image_id
        ON option_view_image_regions(image_id)
    ''')
    _backfill_type_codes_from_filename(cursor)


def _backfill_type_codes_from_filename(cursor) -> None:
    cursor.execute(
        '''
        SELECT id, filename
        FROM option_view_images
        WHERE type_code IS NULL OR TRIM(COALESCE(type_code, '')) = ''
        '''
    )
    for image_id, filename in cursor.fetchall():
        inferred = infer_type_code_from_filename(filename)
        if inferred:
            cursor.execute(
                'UPDATE option_view_images SET type_code = ? WHERE id = ?',
                (inferred, image_id),
            )


def update_image_type_code(cursor, image_id: int, type_code: str) -> tuple[dict | None, str | None]:
    ensure_option_view_image_tables(cursor)
    normalized = normalize_type_code(type_code)
    if not normalized or normalized not in VALID_TYPE_CODES:
        return None, '타입은 AIR 또는 WATER 이어야 합니다.'
    record = get_image_record(cursor, image_id)
    if not record:
        return None, '이미지를 찾을 수 없습니다.'
    cursor.execute(
        'UPDATE option_view_images SET type_code = ? WHERE id = ?',
        (normalized, image_id),
    )
    record['type_code'] = normalized
    return record, None


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


def list_power_source_folders(cursor) -> list[dict]:
    """[1] 파워소스 옵션 목록 + AIR/WATER 이미지 개수"""
    ensure_option_view_image_tables(cursor)
    category_id = _get_power_source_category_id(cursor)
    if not category_id:
        return []

    cursor.execute(
        '''
        SELECT oi.id, oi.item_name, oi.item_code, oi.description,
               COUNT(ovi.id) AS image_count,
               SUM(CASE WHEN UPPER(COALESCE(ovi.type_code, '')) = 'AIR' THEN 1 ELSE 0 END) AS air_count,
               SUM(CASE WHEN UPPER(COALESCE(ovi.type_code, '')) = 'WATER' THEN 1 ELSE 0 END) AS water_count
        FROM option_items oi
        LEFT JOIN option_view_images ovi ON ovi.option_item_id = oi.id
        WHERE oi.category_id = ?
        GROUP BY oi.id, oi.item_name, oi.item_code, oi.description
        ORDER BY oi.id
        ''',
        (category_id,),
    )
    folders = []
    for row in cursor.fetchall():
        folders.append({
            'option_item_id': row[0],
            'item_name': row[1] or '',
            'item_code': row[2] or '',
            'description': row[3] or '',
            'image_count': row[4] or 0,
            'air_count': row[5] or 0,
            'water_count': row[6] or 0,
            'folder_label': _folder_label(row[1], row[2]),
        })
    return folders


def _folder_label(item_name: str, item_code: str) -> str:
    name = (item_name or '').strip()
    code = (item_code or '').strip()
    if name and code:
        return f'{name} ({code})'
    return name or code or '이름 없음'


def _row_to_image_dict(row) -> dict:
    return {
        'id': row[0],
        'option_item_id': row[1],
        'type_code': normalize_type_code(row[2]),
        'filename': row[3],
        'filepath': row[4],
        'file_size': row[5] or 0,
        'size_kb': round((row[5] or 0) / 1024, 1),
        'uploaded_by': row[6] or '',
        'created_at': row[7],
    }


def list_images_by_option(cursor, option_item_id: int, type_code: str | None = None) -> list[dict]:
    ensure_option_view_image_tables(cursor)
    normalized = normalize_type_code(type_code)
    if normalized:
        cursor.execute(
            '''
            SELECT id, option_item_id, type_code, filename, filepath, file_size, uploaded_by, created_at
            FROM option_view_images
            WHERE option_item_id = ?
              AND UPPER(COALESCE(type_code, '')) = ?
            ORDER BY created_at DESC, id DESC
            ''',
            (option_item_id, normalized),
        )
        return [_row_to_image_dict(row) for row in cursor.fetchall()]

    cursor.execute(
        '''
        SELECT id, option_item_id, type_code, filename, filepath, file_size, uploaded_by, created_at
        FROM option_view_images
        WHERE option_item_id = ?
        ORDER BY
            CASE UPPER(COALESCE(type_code, ''))
                WHEN 'AIR' THEN 1
                WHEN 'WATER' THEN 2
                ELSE 3
            END,
            created_at DESC,
            id DESC
        ''',
        (option_item_id,),
    )
    return [_row_to_image_dict(row) for row in cursor.fetchall()]


def get_image_record(cursor, image_id: int) -> dict | None:
    ensure_option_view_image_tables(cursor)
    cursor.execute(
        '''
        SELECT id, option_item_id, type_code, filename, filepath, file_size, uploaded_by, created_at
        FROM option_view_images
        WHERE id = ?
        ''',
        (image_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _row_to_image_dict(row)


def _safe_filename(filename: str) -> str:
    name = os.path.basename((filename or '').strip()).replace('\x00', '')
    name = re.sub(r'[\*\?"<>|]', '_', name)
    root, ext = os.path.splitext(name)
    if not ext:
        return secure_filename(name) or 'image.png'
    safe_root = secure_filename(root) or 'image'
    return safe_root + ext.lower()


def save_uploaded_images(
    cursor,
    option_item_id: int,
    files,
    uploaded_by: str = '',
    type_code: str | None = None,
) -> tuple[list[dict], str | None]:
    ensure_option_view_image_tables(cursor)
    category_id = _get_power_source_category_id(cursor)
    if not category_id:
        return [], '파워소스 카테고리를 찾을 수 없습니다.'

    normalized_type = normalize_type_code(type_code)
    if not normalized_type or normalized_type not in VALID_TYPE_CODES:
        return [], '타입(AIR 또는 WATER)을 선택해 주세요.'

    cursor.execute(
        'SELECT id FROM option_items WHERE id = ? AND category_id = ?',
        (option_item_id, category_id),
    )
    if not cursor.fetchone():
        return [], '유효하지 않은 파워소스 옵션입니다.'

    saved = []
    target_dir = option_image_dir(option_item_id, normalized_type)
    created_at = now_kst()

    for file in files or []:
        if not file or not getattr(file, 'filename', None):
            continue
        raw_name = file.filename.strip()
        if not raw_name:
            continue
        ext = os.path.splitext(raw_name)[1].lower()
        if ext not in ALLOWED_IMAGE_EXT:
            continue

        filename = _safe_filename(raw_name)
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

        cursor.execute(
            '''
            INSERT INTO option_view_images (
                option_item_id, type_code, filename, filepath, file_size, uploaded_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (option_item_id, normalized_type, filename, filepath, file_size, uploaded_by or '', created_at),
        )
        image_id = cursor.lastrowid
        saved.append({
            'id': image_id,
            'option_item_id': option_item_id,
            'type_code': normalized_type,
            'filename': filename,
            'file_size': file_size,
            'size_kb': round(file_size / 1024, 1),
            'uploaded_by': uploaded_by or '',
            'created_at': created_at,
        })

    if not saved:
        return [], '업로드할 수 있는 이미지 파일이 없습니다. (.png, .jpg, .jpeg, .gif, .webp)'

    return saved, None


def delete_image(cursor, image_id: int) -> bool:
    record = get_image_record(cursor, image_id)
    if not record:
        return False
    try:
        if os.path.isfile(record['filepath']):
            os.remove(record['filepath'])
    except OSError:
        pass
    cursor.execute('DELETE FROM option_view_images WHERE id = ?', (image_id,))
    return True


def _parse_region_json(raw: str) -> dict:
    try:
        data = json.loads(raw or '{}')
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _normalize_region_row(row) -> dict:
    region_json = _parse_region_json(row[4])
    return {
        'id': row[0],
        'image_id': row[1],
        'category_number': row[2],
        'region_type': row[3] or 'rect',
        'region_json': region_json,
        'created_at': row[5],
    }


def list_regions_by_image(cursor, image_id: int) -> list[dict]:
    ensure_option_view_image_tables(cursor)
    cursor.execute(
        '''
        SELECT id, image_id, category_number, region_type, region_json, created_at
        FROM option_view_image_regions
        WHERE image_id = ?
        ORDER BY category_number ASC, id ASC
        ''',
        (image_id,),
    )
    return [_normalize_region_row(row) for row in cursor.fetchall()]


def attach_regions_to_images(cursor, images: list[dict]) -> list[dict]:
    if not images:
        return images
    ensure_option_view_image_tables(cursor)
    image_ids = [img['id'] for img in images if img.get('id')]
    if not image_ids:
        return images
    placeholders = ','.join('?' for _ in image_ids)
    cursor.execute(
        f'''
        SELECT id, image_id, category_number, region_type, region_json, created_at
        FROM option_view_image_regions
        WHERE image_id IN ({placeholders})
        ORDER BY category_number ASC, id ASC
        ''',
        image_ids,
    )
    by_image: dict[int, list[dict]] = {}
    for row in cursor.fetchall():
        region = _normalize_region_row(row)
        by_image.setdefault(region['image_id'], []).append(region)
    for image in images:
        image['regions'] = by_image.get(image['id'], [])
    return images


def _normalize_polygon_points_list(points) -> list[list[float]] | None:
    if not isinstance(points, list) or len(points) < 3:
        return None
    normalized_points = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        try:
            px = float(point[0])
            py = float(point[1])
        except (TypeError, ValueError):
            return None
        if px < 0 or px > 1 or py < 0 or py > 1:
            return None
        normalized_points.append([px, py])
    return normalized_points if len(normalized_points) >= 3 else None


def _normalize_polygon_region_json(region_json) -> tuple[dict | None, str | None]:
    if isinstance(region_json, str):
        region_json = _parse_region_json(region_json)
    if not isinstance(region_json, dict):
        region_json = {}

    candidates = []
    polygons = region_json.get('polygons')
    if isinstance(polygons, list):
        candidates.extend(polygons)
    points = region_json.get('points')
    if isinstance(points, list) and points:
        candidates.append(points)

    normalized_polys = []
    for poly in candidates:
        normalized = _normalize_polygon_points_list(poly)
        if normalized:
            normalized_polys.append(normalized)

    if not normalized_polys:
        return None, 'polygon 영역에는 3개 이상의 points가 필요합니다.'

    if len(normalized_polys) == 1:
        return {'points': normalized_polys[0]}, None
    return {'polygons': normalized_polys}, None


def _validate_region_payload(region: dict) -> tuple[dict | None, str | None]:
    if not isinstance(region, dict):
        return None, '영역 데이터 형식이 올바르지 않습니다.'
    category_number = region.get('category_number')
    try:
        category_number = int(category_number)
    except (TypeError, ValueError):
        return None, 'category_number가 필요합니다.'
    if category_number < 1:
        return None, 'category_number가 올바르지 않습니다.'

    region_type = (region.get('region_type') or 'rect').strip().lower()
    if region_type not in {'rect', 'polygon'}:
        return None, 'region_type은 rect 또는 polygon이어야 합니다.'

    region_json = region.get('region_json')
    if isinstance(region_json, str):
        region_json = _parse_region_json(region_json)
    if not isinstance(region_json, dict):
        region_json = region.get('region') if isinstance(region.get('region'), dict) else {}
    if region_type == 'rect':
        for key in ('x', 'y', 'w', 'h'):
            if key not in region_json:
                return None, f'rect 영역에 {key} 값이 필요합니다.'
            try:
                val = float(region_json[key])
            except (TypeError, ValueError):
                return None, f'rect 영역 {key} 값이 올바르지 않습니다.'
            if val < 0 or val > 1:
                return None, f'rect 영역 {key} 값은 0~1 사이여야 합니다.'
    else:
        region_json, error = _normalize_polygon_region_json(region_json)
        if error:
            return None, f'[{category_number}] {error}'

    return {
        'category_number': category_number,
        'region_type': region_type,
        'region_json': region_json,
    }, None


def save_regions_for_image(cursor, image_id: int, regions: list[dict]) -> tuple[list[dict], str | None]:
    ensure_option_view_image_tables(cursor)
    record = get_image_record(cursor, image_id)
    if not record:
        return [], '이미지를 찾을 수 없습니다.'

    normalized: list[dict] = []
    seen_categories: set[int] = set()
    for region in regions or []:
        parsed, error = _validate_region_payload(region)
        if error:
            return [], error
        if parsed['category_number'] in seen_categories:
            return [], f"[{parsed['category_number']}] 카테고리 영역이 중복되었습니다."
        seen_categories.add(parsed['category_number'])
        normalized.append(parsed)

    cursor.execute('DELETE FROM option_view_image_regions WHERE image_id = ?', (image_id,))
    created_at = now_kst()
    for region in normalized:
        cursor.execute(
            '''
            INSERT INTO option_view_image_regions (
                image_id, category_number, region_type, region_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ''',
            (
                image_id,
                region['category_number'],
                region['region_type'],
                json.dumps(region['region_json'], ensure_ascii=False),
                created_at,
            ),
        )
    return list_regions_by_image(cursor, image_id), None


def list_option_categories(cursor) -> list[dict]:
    cursor.execute(
        '''
        SELECT category_number, category_name
        FROM option_categories
        ORDER BY category_number ASC
        '''
    )
    return [
        {'category_number': row[0], 'category_name': row[1] or ''}
        for row in cursor.fetchall()
    ]
