#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""사용자 활동 추적 (고객/수출부/관리자)"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

USER_TYPES = {
    'admin': '관리자(PNS)',
    'export_dept': '수출부',
    'customer': '고객',
}

# username -> (password, user_type)
TRACKING_USERS = {
    'pns': ('pns@123', 'admin'),
    'trade': ('trade@123', 'export_dept'),
    'customer': ('customer@123', 'customer'),
}

# 역할별 접근 권한 (메뉴/페이지)
ROLE_PERMISSIONS = {
    'admin': {
        'nav_admin',           # 코드관리/로직/세트가격/관리
        'nav_export_staff',    # 수출견적서(스태프)
        'nav_export_customer', # 수출견적서(고객)
        'option_select_pns',   # 옵션선택(PNS용)
        'tracking_banner',     # 활동 추적 배너
        'activity_dashboard',
        'export_staff_page',
        'export_customer_page',
    },
    'export_dept': {
        'nav_export_staff',
        'export_staff_page',
    },
    'customer': {
        'nav_export_customer',
        'export_customer_page',
    },
}


def user_has_permission(user_type: str, permission: str) -> bool:
    perms = ROLE_PERMISSIONS.get(user_type) or set()
    return permission in perms


def can_view_export_cost(user_type: str | None) -> bool:
    """수출견적서 제품 원가/마진 노출 — PNS(관리자), 수출부(trade)만 허용"""
    return user_type in ('admin', 'export_dept')


def authenticate_tracking_user(username: str, password: str):
    username = (username or '').strip().lower()
    password = (password or '').strip()
    cred = TRACKING_USERS.get(username)
    if not cred or cred[0] != password:
        return None
    return {
        'user_id': username,
        'user_type': cred[1],
        'user_type_label': USER_TYPES.get(cred[1], cred[1]),
        'permissions': sorted(ROLE_PERMISSIONS.get(cred[1], set())),
    }


def now_kst() -> str:
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


def ensure_user_tracking_tables(cursor) -> None:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_tracking_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            user_type TEXT NOT NULL,
            user_agent TEXT,
            ip_address TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            total_seconds INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_page_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            user_type TEXT NOT NULL,
            page_path TEXT,
            page_title TEXT,
            entered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            left_at TIMESTAMP,
            duration_seconds INTEGER DEFAULT 0,
            is_interest INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES user_tracking_sessions(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS quote_download_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            user_id TEXT NOT NULL,
            user_type TEXT NOT NULL,
            quote_source TEXT,
            quote_category TEXT,
            product_count INTEGER DEFAULT 0,
            product_codes TEXT,
            file_name TEXT,
            downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_page_views_session ON user_page_views(session_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_page_views_user ON user_page_views(user_id, user_type)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_quote_downloads_user ON quote_download_logs(user_id, user_type)')


def create_tracking_session(cursor, user_id: str, user_type: str, user_agent=None, ip_address=None) -> str:
    session_id = str(uuid.uuid4())
    cursor.execute('''
        INSERT INTO user_tracking_sessions (id, user_id, user_type, user_agent, ip_address, started_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (session_id, user_id, user_type, user_agent, ip_address, now_kst(), now_kst()))
    return session_id


def update_session_activity(cursor, session_id: str, total_seconds=None):
    if total_seconds is not None:
        cursor.execute('''
            UPDATE user_tracking_sessions
            SET last_seen_at = ?, total_seconds = ?
            WHERE id = ?
        ''', (now_kst(), int(total_seconds), session_id))
    else:
        cursor.execute('''
            UPDATE user_tracking_sessions
            SET last_seen_at = ?
            WHERE id = ?
        ''', (now_kst(), session_id))


def log_page_view(cursor, session_id, user_id, user_type, page_path, page_title, duration_seconds=0):
    is_interest = 1 if duration_seconds >= 30 else 0
    cursor.execute('''
        INSERT INTO user_page_views
            (session_id, user_id, user_type, page_path, page_title, entered_at, left_at, duration_seconds, is_interest)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        session_id, user_id, user_type, page_path or '', page_title or '',
        now_kst(), now_kst(), int(duration_seconds or 0), is_interest
    ))
    return cursor.lastrowid


def classify_quote_category(products) -> str:
    """견적 다운로드 분류: set / welder / spare / mixed"""
    if not products:
        return 'general'
    categories = set()
    for p in products:
        code = str(p.get('code') or p.get('productCode') or '').strip().upper()
        cat = p.get('categoryNumber') or p.get('category_number')
        if re.match(r'^P5[1-4]\d{6}$', code):
            categories.add('set')
        elif cat == 14 or code.startswith('P58') or 'SPARE' in code:
            categories.add('spare')
        elif cat == 1 or code.startswith('P35') or code.startswith('P58') is False and re.match(r'^P5', code):
            if cat == 1:
                categories.add('welder')
            elif re.match(r'^P35', code):
                categories.add('welder')
        else:
            categories.add('other')
    if len(categories) == 1:
        return categories.pop()
    if 'set' in categories:
        return 'set'
    if 'welder' in categories:
        return 'welder'
    if 'spare' in categories:
        return 'spare'
    return 'mixed'


def log_quote_download(cursor, session_id, user_id, user_type, quote_source, products, file_name):
    codes = []
    for p in (products or []):
        c = p.get('code') or p.get('productCode') or ''
        if c:
            codes.append(str(c))
    category = classify_quote_category(products or [])
    cursor.execute('''
        INSERT INTO quote_download_logs
            (session_id, user_id, user_type, quote_source, quote_category, product_count, product_codes, file_name, downloaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        session_id, user_id, user_type, quote_source or '', category,
        len(products or []), json.dumps(codes, ensure_ascii=False), file_name or '', now_kst()
    ))


def get_dashboard_data(cursor, user_type_filter=None, user_id_filter=None, limit=200):
    """고객(customer) / 수출부(export_dept) 활동 조회"""
    clauses = []
    params = []

    if user_type_filter and user_type_filter in ('customer', 'export_dept'):
        clauses.append('user_type = ?')
        params.append(user_type_filter)
    else:
        clauses.append("user_type IN ('customer', 'export_dept')")

    if user_id_filter:
        clauses.append('user_id = ?')
        params.append(user_id_filter.strip().lower())

    type_clause = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''

    cursor.execute(f'''
        SELECT id, user_id, user_type, started_at, last_seen_at, total_seconds, ip_address
        FROM user_tracking_sessions
        {type_clause}
        ORDER BY last_seen_at DESC
        LIMIT ?
    ''', params + [limit])
    sessions = [dict(row) if hasattr(row, 'keys') else {
        'id': row[0], 'user_id': row[1], 'user_type': row[2],
        'started_at': row[3], 'last_seen_at': row[4], 'total_seconds': row[5], 'ip_address': row[6]
    } for row in cursor.fetchall()]

    interest_clauses = ["is_interest = 1"]
    interest_params = []
    if user_type_filter and user_type_filter in ('customer', 'export_dept'):
        interest_clauses.append('user_type = ?')
        interest_params.append(user_type_filter)
    else:
        interest_clauses.append("user_type IN ('customer', 'export_dept')")
    if user_id_filter:
        interest_clauses.append('user_id = ?')
        interest_params.append(user_id_filter.strip().lower())

    cursor.execute(f'''
        SELECT user_id, user_type, page_path, page_title, duration_seconds, entered_at
        FROM user_page_views
        WHERE {' AND '.join(interest_clauses)}
        ORDER BY duration_seconds DESC, entered_at DESC
        LIMIT 100
    ''', interest_params)
    interests = []
    for row in cursor.fetchall():
        if hasattr(row, 'keys'):
            interests.append(dict(row))
        else:
            interests.append({
                'user_id': row[0], 'user_type': row[1], 'page_path': row[2],
                'page_title': row[3], 'duration_seconds': row[4], 'entered_at': row[5]
            })

    cursor.execute(f'''
        SELECT user_id, user_type, quote_source, quote_category, product_count, file_name, downloaded_at, product_codes
        FROM quote_download_logs
        {type_clause}
        ORDER BY downloaded_at DESC
        LIMIT 100
    ''', params)
    downloads = []
    for row in cursor.fetchall():
        if hasattr(row, 'keys'):
            downloads.append(dict(row))
        else:
            downloads.append({
                'user_id': row[0], 'user_type': row[1], 'quote_source': row[2],
                'quote_category': row[3], 'product_count': row[4], 'file_name': row[5],
                'downloaded_at': row[6], 'product_codes': row[7]
            })

    # summary stats (고객/수출부만)
    cursor.execute('''
        SELECT user_type, COUNT(*) as cnt, COALESCE(SUM(total_seconds),0) as total_sec
        FROM user_tracking_sessions
        WHERE user_type IN ('customer', 'export_dept')
        GROUP BY user_type
    ''')
    summary = {}
    for row in cursor.fetchall():
        ut = row[0] if not hasattr(row, 'keys') else row['user_type']
        cnt = row[1] if not hasattr(row, 'keys') else row['cnt']
        sec = row[2] if not hasattr(row, 'keys') else row['total_sec']
        summary[ut] = {'sessions': cnt, 'total_seconds': sec}

    cursor.execute('''
        SELECT quote_category, COUNT(*) as cnt
        FROM quote_download_logs
        WHERE user_type IN ('customer', 'export_dept')
        GROUP BY quote_category
    ''')
    quote_stats = {}
    for row in cursor.fetchall():
        cat = row[0] if not hasattr(row, 'keys') else row['quote_category']
        cnt = row[1] if not hasattr(row, 'keys') else row['cnt']
        quote_stats[cat or 'general'] = cnt

    return {
        'sessions': sessions,
        'interests': interests,
        'downloads': downloads,
        'summary': summary,
        'quote_stats': quote_stats,
        'user_type_labels': USER_TYPES,
    }
