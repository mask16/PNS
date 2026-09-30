"""EXPORT LABEL 업로드 이력 관리"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone, timedelta

from export_label_generator import parse_template_entries

KST = timezone(timedelta(hours=9))

EXPORT_LABEL_BASENAME = 'export_label_template'
EXPORT_LABEL_ALLOWED_EXT = {'.docx', '.doc'}


def now_kst() -> str:
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


def export_label_base_dir() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'export_label_templates')
    os.makedirs(path, exist_ok=True)
    return path


def export_label_history_dir() -> str:
    path = os.path.join(export_label_base_dir(), 'history')
    os.makedirs(path, exist_ok=True)
    return path


def active_template_path() -> str:
    directory = export_label_base_dir()
    for ext in ('.docx', '.doc'):
        filepath = os.path.join(directory, EXPORT_LABEL_BASENAME + ext)
        if os.path.isfile(filepath):
            return filepath
    return os.path.join(directory, EXPORT_LABEL_BASENAME + '.docx')


def sanitize_upload_filename(filename: str, default_ext: str = '.docx') -> str:
    """업로드 파일명 — 사용자 원본 이름 유지(한글 포함), 경로·위험 문자만 제거"""
    name = os.path.basename((filename or '').strip()).replace('\x00', '')
    name = name.replace('\\', '_').replace('/', '_').replace(':', '_')
    name = re.sub(r'[\*\?"<>|]', '_', name)
    name = name.strip().strip('.')
    if not name:
        return EXPORT_LABEL_BASENAME + default_ext
    root, ext = os.path.splitext(name)
    if not ext:
        return name + default_ext
    return name


def history_file_path(upload_id: str, original_filename: str) -> str:
    safe_name = sanitize_upload_filename(original_filename)
    return os.path.join(export_label_history_dir(), f'{upload_id}_{safe_name}')


def _legacy_history_file_path(upload_id: str, file_ext: str) -> str:
    ext = file_ext if file_ext.startswith('.') else f'.{file_ext}'
    return os.path.join(export_label_history_dir(), f'{upload_id}{ext}')


def ensure_export_label_tables(cursor) -> None:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS export_label_uploads (
            id TEXT PRIMARY KEY,
            original_filename TEXT NOT NULL,
            file_ext TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            stock_codes TEXT,
            stock_count INTEGER DEFAULT 0,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            uploaded_by TEXT,
            is_active INTEGER DEFAULT 0,
            notes TEXT
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_export_label_uploads_uploaded_at
        ON export_label_uploads(uploaded_at DESC)
    ''')


def _parse_stock_codes(filepath: str) -> list[str]:
    if not os.path.isfile(filepath):
        return []
    if filepath.lower().endswith('.doc') and not filepath.lower().endswith('.docx'):
        return []
    try:
        entries = parse_template_entries(filepath)
        codes = []
        seen = set()
        for entry in entries:
            code = (entry.get('stock_code') or '').strip()
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
        return codes
    except Exception:
        return []


def _clear_active_flags(cursor) -> None:
    cursor.execute('UPDATE export_label_uploads SET is_active = 0')


def _set_active_template_from_history(filepath: str, file_ext: str) -> None:
    """이력 파일을 현재 템플릿 경로로 복사. 대상 파일이 잠겨 있으면 상태 변경은 계속 진행."""
    import stat

    templates_dir = export_label_base_dir()
    ext = file_ext if str(file_ext).startswith('.') else f'.{file_ext or "docx"}'
    target_name = EXPORT_LABEL_BASENAME + ext
    target_path = os.path.join(templates_dir, target_name)
    for old_ext in EXPORT_LABEL_ALLOWED_EXT:
        old = os.path.join(templates_dir, EXPORT_LABEL_BASENAME + old_ext)
        if os.path.isfile(old) and os.path.normcase(old) != os.path.normcase(target_path):
            try:
                os.chmod(old, stat.S_IWRITE | stat.S_IREAD)
                os.remove(old)
            except OSError:
                pass
    try:
        if os.path.isfile(target_path):
            os.chmod(target_path, stat.S_IWRITE | stat.S_IREAD)
        shutil.copy2(filepath, target_path)
    except PermissionError:
        tmp_path = target_path + '.activating'
        try:
            shutil.copy2(filepath, tmp_path)
            os.replace(tmp_path, target_path)
        except OSError as err:
            print(f'[export_label] active template replace skipped: {err}')
            if os.path.isfile(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass



def register_upload(
    cursor,
    *,
    saved_filepath: str,
    original_filename: str,
    file_ext: str,
    size_bytes: int,
    uploaded_by: str | None = None,
    set_as_active: bool = True,
) -> dict:
    """업로드 파일을 이력에 등록하고 필요 시 현재 템플릿으로 활성화"""
    ensure_export_label_tables(cursor)

    upload_id = uuid.uuid4().hex
    history_path = history_file_path(upload_id, original_filename)
    shutil.copy2(saved_filepath, history_path)

    stock_codes = _parse_stock_codes(history_path)
    uploaded_at = now_kst()

    if set_as_active:
        _clear_active_flags(cursor)
        _set_active_template_from_history(history_path, file_ext)
        is_active = 1
    else:
        is_active = 0

    cursor.execute(
        '''
        INSERT INTO export_label_uploads (
            id, original_filename, file_ext, size_bytes,
            stock_codes, stock_count, uploaded_at, uploaded_by, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            upload_id,
            original_filename,
            file_ext,
            size_bytes,
            json.dumps(stock_codes, ensure_ascii=False),
            len(stock_codes),
            uploaded_at,
            uploaded_by or '',
            is_active,
        ),
    )

    return {
        'id': upload_id,
        'original_filename': original_filename,
        'file_ext': file_ext,
        'size_bytes': size_bytes,
        'size_kb': round(size_bytes / 1024, 1),
        'stock_codes': stock_codes,
        'stock_count': len(stock_codes),
        'uploaded_at': uploaded_at,
        'uploaded_by': uploaded_by or '',
        'is_active': bool(is_active),
    }


def _normalize_stock_code_search(value: str) -> str:
    text = (value or '').strip().upper().replace('|', 'I')
    return re.sub(r'[^A-Z0-9]', '', text)


def _stock_code_matches_query(code: str, query: str) -> bool:
    normalized_query = _normalize_stock_code_search(query)
    if not normalized_query:
        return True
    normalized_code = _normalize_stock_code_search(code)
    return normalized_query in normalized_code or normalized_code in normalized_query


def list_uploads(cursor, limit: int = 100, stock_code: str | None = None) -> list[dict]:
    ensure_export_label_tables(cursor)
    cursor.execute(
        '''
        SELECT id, original_filename, file_ext, size_bytes, stock_codes, stock_count,
               uploaded_at, uploaded_by, is_active
        FROM export_label_uploads
        ORDER BY uploaded_at DESC, id DESC
        LIMIT ?
        ''',
        (limit,),
    )
    rows = []
    query = (stock_code or '').strip()
    for row in cursor.fetchall():
        stock_codes_raw = row[4]
        try:
            stock_codes = json.loads(stock_codes_raw) if stock_codes_raw else []
        except (TypeError, json.JSONDecodeError):
            stock_codes = []
        if query and not any(_stock_code_matches_query(code, query) for code in stock_codes):
            continue
        rows.append({
            'id': row[0],
            'original_filename': row[1],
            'file_ext': row[2],
            'size_bytes': row[3],
            'size_kb': round((row[3] or 0) / 1024, 1),
            'stock_codes': stock_codes,
            'stock_count': row[5] or len(stock_codes),
            'uploaded_at': row[6],
            'uploaded_by': row[7] or '',
            'is_active': bool(row[8]),
        })
    return rows


def get_upload(cursor, upload_id: str) -> dict | None:
    ensure_export_label_tables(cursor)
    cursor.execute(
        '''
        SELECT id, original_filename, file_ext, size_bytes, stock_codes, stock_count,
               uploaded_at, uploaded_by, is_active
        FROM export_label_uploads
        WHERE id = ?
        ''',
        (upload_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    stock_codes_raw = row[4]
    try:
        stock_codes = json.loads(stock_codes_raw) if stock_codes_raw else []
    except (TypeError, json.JSONDecodeError):
        stock_codes = []

    filepath = history_file_path(row[0], row[1])
    if not os.path.isfile(filepath):
        legacy = _legacy_history_file_path(row[0], row[2])
        if os.path.isfile(legacy):
            filepath = legacy
    if not os.path.isfile(filepath):
        return None

    return {
        'id': row[0],
        'original_filename': row[1],
        'file_ext': row[2],
        'size_bytes': row[3],
        'size_kb': round((row[3] or 0) / 1024, 1),
        'stock_codes': stock_codes,
        'stock_count': row[5] or len(stock_codes),
        'uploaded_at': row[6],
        'uploaded_by': row[7] or '',
        'is_active': bool(row[8]),
        'filepath': filepath,
    }


def get_upload_row(cursor, upload_id: str) -> dict | None:
    """파일 존재 여부와 무관하게 DB 이력 행만 조회"""
    ensure_export_label_tables(cursor)
    cursor.execute(
        '''
        SELECT id, original_filename, file_ext, is_active
        FROM export_label_uploads
        WHERE id = ?
        ''',
        (upload_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        'id': row[0],
        'original_filename': row[1],
        'file_ext': row[2],
        'is_active': bool(row[3]),
    }


def activate_upload(cursor, upload_id: str) -> dict | None:
    record = get_upload(cursor, upload_id)
    if not record:
        return None

    _clear_active_flags(cursor)
    _set_active_template_from_history(record['filepath'], record['file_ext'])
    cursor.execute(
        'UPDATE export_label_uploads SET is_active = 1 WHERE id = ?',
        (upload_id,),
    )
    record['is_active'] = True
    return record


def delete_upload(cursor, upload_id: str) -> bool:
    row = get_upload_row(cursor, upload_id)
    if not row:
        return False

    candidates = [
        history_file_path(row['id'], row['original_filename']),
        _legacy_history_file_path(row['id'], row['file_ext'] or '.docx'),
    ]
    record = get_upload(cursor, upload_id)
    if record and record.get('filepath'):
        candidates.insert(0, record['filepath'])

    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass

    cursor.execute('DELETE FROM export_label_uploads WHERE id = ?', (upload_id,))
    return True


def resolve_template_filepath(cursor, upload_id: str | None = None) -> tuple[str | None, dict | None]:
    """upload_id가 있으면 해당 이력 파일, 없으면 현재 활성 템플릿"""
    ensure_export_label_tables(cursor)
    if upload_id:
        record = get_upload(cursor, upload_id)
        if not record:
            return None, None
        return record['filepath'], record

    cursor.execute(
        '''
        SELECT id FROM export_label_uploads
        WHERE is_active = 1
        ORDER BY uploaded_at DESC, id DESC
        LIMIT 1
        '''
    )
    row = cursor.fetchone()
    if row:
        record = get_upload(cursor, row[0])
        if record:
            return record['filepath'], record

    filepath = active_template_path()
    if os.path.isfile(filepath):
        return filepath, None
    return None, None


def migrate_existing_template(cursor) -> None:
    """기존 단일 템플릿 파일을 이력에 1회 등록"""
    ensure_export_label_tables(cursor)
    cursor.execute('SELECT COUNT(*) FROM export_label_uploads')
    if cursor.fetchone()[0] > 0:
        return

    filepath = active_template_path()
    if not os.path.isfile(filepath):
        return

    file_ext = os.path.splitext(filepath)[1].lower()
    original_name = os.path.basename(filepath)
    size_bytes = os.path.getsize(filepath)
    register_upload(
        cursor,
        saved_filepath=filepath,
        original_filename=original_name,
        file_ext=file_ext,
        size_bytes=size_bytes,
        uploaded_by='system',
        set_as_active=True,
    )
