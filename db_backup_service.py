#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite DB 일일/수동 백업 및 복원 서비스"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_ROOT = os.path.join(BASE_DIR, 'db_backups')
DAILY_DIR = os.path.join(BACKUP_ROOT, 'daily')
MANUAL_DIR = os.path.join(BACKUP_ROOT, 'manual')
CONFIG_PATH = os.path.join(BACKUP_ROOT, 'config.json')

DEFAULT_CONFIG = {
    'enabled': True,
    'hour': 2,
    'minute': 0,
    'retention_days': 30,
    'last_success_at': None,
    'last_file': None,
    'last_error': None,
    'last_run_date': None,
}

_config_lock = threading.RLock()
_scheduler_started = False
_scheduler_lock = threading.Lock()
_db_path_resolver: Callable[[], str] | None = None
_after_restore: Callable[[], None] | None = None


def ensure_backup_dirs() -> None:
    os.makedirs(DAILY_DIR, exist_ok=True)
    os.makedirs(MANUAL_DIR, exist_ok=True)


def set_db_path_resolver(fn: Callable[[], str]) -> None:
    global _db_path_resolver
    _db_path_resolver = fn


def set_after_restore_hook(fn: Callable[[], None]) -> None:
    global _after_restore
    _after_restore = fn


def resolve_source_db_path() -> str:
    if _db_path_resolver:
        path = _db_path_resolver()
        if path:
            return path
    preferred = os.path.join(BASE_DIR, 'welding_options_data.db')
    legacy = os.path.join(BASE_DIR, 'welding_options.db')
    return preferred if os.path.exists(preferred) else legacy


def _load_config_unlocked() -> dict:
    ensure_backup_dirs()
    if not os.path.isfile(CONFIG_PATH):
        cfg = dict(DEFAULT_CONFIG)
        _save_config_unlocked(cfg)
        return cfg
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: data.get(k, cfg[k]) for k in DEFAULT_CONFIG})
    try:
        cfg['hour'] = max(0, min(23, int(cfg.get('hour', 2))))
        cfg['minute'] = max(0, min(59, int(cfg.get('minute', 0))))
        cfg['retention_days'] = max(1, min(3650, int(cfg.get('retention_days', 30))))
        cfg['enabled'] = bool(cfg.get('enabled', True))
    except (TypeError, ValueError):
        cfg.update(DEFAULT_CONFIG)
    return cfg


def _save_config_unlocked(cfg: dict) -> None:
    ensure_backup_dirs()
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def get_config() -> dict:
    with _config_lock:
        return _load_config_unlocked()


def update_config(*, enabled=None, hour=None, minute=None, retention_days=None) -> dict:
    with _config_lock:
        cfg = _load_config_unlocked()
        if enabled is not None:
            cfg['enabled'] = bool(enabled)
        if hour is not None:
            cfg['hour'] = max(0, min(23, int(hour)))
        if minute is not None:
            cfg['minute'] = max(0, min(59, int(minute)))
        if retention_days is not None:
            cfg['retention_days'] = max(1, min(3650, int(retention_days)))
        _save_config_unlocked(cfg)
        return dict(cfg)


def _now() -> datetime:
    return datetime.now()


def next_scheduled_at(cfg: dict | None = None) -> datetime | None:
    cfg = cfg or get_config()
    if not cfg.get('enabled'):
        return None
    now = _now()
    candidate = now.replace(hour=int(cfg['hour']), minute=int(cfg['minute']), second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def today_backup_done(cfg: dict | None = None) -> bool:
    cfg = cfg or get_config()
    return (cfg.get('last_run_date') or '') == _now().strftime('%Y-%m-%d')


def _format_size(num: int) -> str:
    if num < 1024:
        return f'{num} B'
    if num < 1024 * 1024:
        return f'{num / 1024:.1f} KB'
    return f'{num / (1024 * 1024):.1f} MB'


def _safe_relpath(rel: str) -> str | None:
    if not rel:
        return None
    rel = rel.replace('\\', '/').lstrip('/')
    if '..' in rel.split('/'):
        return None
    if not re.match(r'^(daily|manual)/[A-Za-z0-9._\-]+$', rel):
        return None
    full = os.path.normpath(os.path.join(BACKUP_ROOT, rel))
    root = os.path.normpath(BACKUP_ROOT)
    if not full.startswith(root + os.sep):
        return None
    return rel


def resolve_backup_path(rel: str) -> str | None:
    safe = _safe_relpath(rel)
    if not safe:
        return None
    full = os.path.join(BACKUP_ROOT, *safe.split('/'))
    if not os.path.isfile(full):
        return None
    return full


def _sqlite_backup(src_path: str, dest_path: str) -> None:
    ensure_backup_dirs()
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if not os.path.isfile(src_path):
        raise FileNotFoundError(f'원본 DB를 찾을 수 없습니다: {src_path}')
    # 임시 파일에 백업 후 교체 (부분 파일 방지)
    tmp_path = dest_path + '.tmp'
    try:
        src = sqlite3.connect(src_path, timeout=60.0)
        try:
            dst = sqlite3.connect(tmp_path, timeout=60.0)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp_path, dest_path)
    finally:
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def purge_old_backups(retention_days: int | None = None) -> int:
    cfg = get_config()
    days = int(retention_days if retention_days is not None else cfg.get('retention_days') or 30)
    cutoff = _now() - timedelta(days=days)
    removed = 0
    for folder in (DAILY_DIR, MANUAL_DIR):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if not name.endswith('.db'):
                continue
            path = os.path.join(folder, name)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path))
                if mtime < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    return removed


def create_backup(kind: str = 'manual') -> dict:
    """kind: 'daily' | 'manual'"""
    kind = 'daily' if kind == 'daily' else 'manual'
    src = resolve_source_db_path()
    now = _now()
    if kind == 'daily':
        filename = f"pns_daily_{now.strftime('%Y%m%d')}.db"
        dest_dir = DAILY_DIR
        rel_prefix = 'daily'
    else:
        filename = f"pns_manual_{now.strftime('%Y%m%d_%H%M%S')}.db"
        dest_dir = MANUAL_DIR
        rel_prefix = 'manual'

    dest = os.path.join(dest_dir, filename)
    _sqlite_backup(src, dest)
    rel = f'{rel_prefix}/{filename}'
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')

    with _config_lock:
        cfg = _load_config_unlocked()
        cfg['last_success_at'] = stamp
        cfg['last_file'] = rel
        cfg['last_error'] = None
        if kind == 'daily':
            cfg['last_run_date'] = now.strftime('%Y-%m-%d')
        _save_config_unlocked(cfg)
        retention = int(cfg.get('retention_days') or 30)

    purge_old_backups(retention)
    size = os.path.getsize(dest)
    return {
        'success': True,
        'kind': kind,
        'relpath': rel,
        'filename': filename,
        'size': size,
        'size_label': _format_size(size),
        'saved_at': stamp,
        'message': '백업이 완료되었습니다.',
    }


def list_backups() -> list[dict]:
    ensure_backup_dirs()
    items: list[dict] = []
    for kind, folder, label in (
        ('daily', DAILY_DIR, '일일자동'),
        ('manual', MANUAL_DIR, '수동'),
    ):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if not name.endswith('.db'):
                continue
            path = os.path.join(folder, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            items.append({
                'kind': kind,
                'kind_label': label,
                'filename': name,
                'relpath': f'{kind}/{name}',
                'size': st.st_size,
                'size_label': _format_size(st.st_size),
                'saved_at': datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                'mtime': st.st_mtime,
            })
    items.sort(key=lambda x: x['mtime'], reverse=True)
    for item in items:
        item.pop('mtime', None)
    return items


def delete_backup(relpath: str) -> dict:
    path = resolve_backup_path(relpath)
    if not path:
        return {'success': False, 'error': '유효하지 않은 백업 파일입니다.'}
    os.remove(path)
    return {'success': True, 'message': '백업 파일이 삭제되었습니다.'}


def restore_backup(relpath: str) -> dict:
    path = resolve_backup_path(relpath)
    if not path:
        return {'success': False, 'error': '유효하지 않은 백업 파일입니다.'}
    dest = resolve_source_db_path()
    # 열린 연결이 있으면 파일 교체가 실패하므로 먼저 훅으로 닫음
    if _after_restore:
        try:
            _after_restore()
        except Exception:
            pass

    # 복원 직전 안전 백업
    safety_name = f"pns_pre_restore_{_now().strftime('%Y%m%d_%H%M%S')}.db"
    safety_path = os.path.join(MANUAL_DIR, safety_name)
    try:
        if os.path.isfile(dest):
            _sqlite_backup(dest, safety_path)
    except Exception as err:
        return {'success': False, 'error': f'복원 전 안전 백업 실패: {err}'}

    # WAL 부속 파일 정리 후 교체
    for suffix in ('-wal', '-shm'):
        side = dest + suffix
        if os.path.isfile(side):
            try:
                os.remove(side)
            except OSError:
                pass

    tmp_dest = dest + '.restore_tmp'
    try:
        shutil.copy2(path, tmp_dest)
        os.replace(tmp_dest, dest)
    finally:
        if os.path.isfile(tmp_dest):
            try:
                os.remove(tmp_dest)
            except OSError:
                pass

    return {
        'success': True,
        'message': 'DB가 복원되었습니다. 페이지를 새로고침하세요.',
        'safety_backup': f'manual/{safety_name}',
        'restored_from': relpath,
    }


def get_status() -> dict[str, Any]:
    cfg = get_config()
    nxt = next_scheduled_at(cfg)
    return {
        'success': True,
        'enabled': bool(cfg.get('enabled')),
        'hour': int(cfg.get('hour', 2)),
        'minute': int(cfg.get('minute', 0)),
        'retention_days': int(cfg.get('retention_days', 30)),
        'last_success_at': cfg.get('last_success_at'),
        'last_file': cfg.get('last_file'),
        'last_error': cfg.get('last_error'),
        'last_run_date': cfg.get('last_run_date'),
        'today_done': today_backup_done(cfg),
        'next_scheduled_at': nxt.strftime('%Y-%m-%d %H:%M') if nxt else None,
        'source_db': os.path.basename(resolve_source_db_path()),
        'backup_root': 'db_backups',
        'files': list_backups(),
    }


def _maybe_run_daily() -> None:
    with _config_lock:
        cfg = _load_config_unlocked()
        if not cfg.get('enabled'):
            return
        now = _now()
        if now.hour != int(cfg['hour']) or now.minute != int(cfg['minute']):
            return
        if (cfg.get('last_run_date') or '') == now.strftime('%Y-%m-%d'):
            return
    try:
        create_backup('daily')
    except Exception as err:
        with _config_lock:
            cfg = _load_config_unlocked()
            cfg['last_error'] = str(err)
            _save_config_unlocked(cfg)
        print(f'[db_backup] daily backup failed: {err}')


def _scheduler_loop() -> None:
    while True:
        try:
            _maybe_run_daily()
        except Exception as err:
            print(f'[db_backup] scheduler error: {err}')
        time.sleep(20)


def start_backup_scheduler() -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        ensure_backup_dirs()
        t = threading.Thread(target=_scheduler_loop, name='db-backup-scheduler', daemon=True)
        t.start()
        _scheduler_started = True
        print('[db_backup] daily backup scheduler started')
