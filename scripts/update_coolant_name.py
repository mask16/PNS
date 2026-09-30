#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
COOLANT(SUPER COOLANT 2.5L) -> COOLANT(SUPER COOLANT 2.0L) 업데이트 스크립트
- selected_combinations.selected_options JSON 업데이트
- option_items.item_name 업데이트
- option_prices.option_name 업데이트
- option_base_prices.option_name 업데이트
"""

import sqlite3
import json
import os
import sys

OLD_NAME = "COOLANT(SUPER COOLANT 2.5L)"
NEW_NAME = "COOLANT(SUPER COOLANT 2.0L)"

DB_PATHS = [
    "welding_options.db",
    "pns.db"
]


def table_exists(cur: sqlite3.Cursor, table_name: str) -> bool:
    """테이블 존재 여부 확인"""
    cur.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name=?
    """, (table_name,))
    return cur.fetchone() is not None


def update_selected_combinations(cur: sqlite3.Cursor) -> tuple[int, int]:
    """selected_combinations.selected_options JSON의 옵션명 업데이트"""
    if not table_exists(cur, "selected_combinations"):
        return 0, 0
    
    scanned = 0
    updated = 0
    
    cur.execute("SELECT id, selected_options FROM selected_combinations")
    rows = cur.fetchall()
    
    for row_id, selected_options_json in rows:
        scanned += 1
        if not selected_options_json:
            continue
        
        try:
            data = json.loads(selected_options_json)
            if not isinstance(data, dict):
                continue
            
            changed = False
            # 모든 카테고리에서 OLD_NAME을 찾아 NEW_NAME으로 변경
            for key in list(data.keys()):
                val = data[key]
                if isinstance(val, str) and val == OLD_NAME:
                    data[key] = NEW_NAME
                    changed = True
            
            if changed:
                updated_json = json.dumps(data, ensure_ascii=False)
                cur.execute(
                    "UPDATE selected_combinations SET selected_options = ? WHERE id = ?",
                    (updated_json, row_id)
                )
                updated += 1
        except Exception as e:
            print(f"  [WARN] row {row_id}: {e}")
            continue
    
    return scanned, updated


def update_option_items(cur: sqlite3.Cursor) -> int:
    """option_items.item_name 업데이트"""
    if not table_exists(cur, "option_items"):
        return 0
    cur.execute(
        "UPDATE option_items SET item_name = ? WHERE item_name = ?",
        (NEW_NAME, OLD_NAME)
    )
    return cur.rowcount


def update_option_prices(cur: sqlite3.Cursor) -> int:
    """option_prices.option_name 업데이트"""
    if not table_exists(cur, "option_prices"):
        return 0
    cur.execute(
        "UPDATE option_prices SET option_name = ? WHERE option_name = ?",
        (NEW_NAME, OLD_NAME)
    )
    return cur.rowcount


def update_option_base_prices(cur: sqlite3.Cursor) -> int:
    """option_base_prices.option_name 업데이트"""
    # 테이블 존재 여부 확인
    cur.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name='option_base_prices'
    """)
    if not cur.fetchone():
        return 0
    
    cur.execute(
        "UPDATE option_base_prices SET option_name = ? WHERE option_name = ?",
        (NEW_NAME, OLD_NAME)
    )
    return cur.rowcount


def main():
    print(f"[INFO] Updating '{OLD_NAME}' -> '{NEW_NAME}'")
    print()
    
    for db_path in DB_PATHS:
        if not os.path.exists(db_path):
            print(f"[SKIP] {db_path} (not found)")
            continue
        
        print(f"[OK] {db_path}")
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        
        try:
            # 1. selected_combinations 업데이트
            scanned, combo_updates = update_selected_combinations(cur)
            print(f"  - selected_combinations scanned: {scanned}, updated: {combo_updates}")
            
            # 2. option_items 업데이트
            item_updates = update_option_items(cur)
            print(f"  - option_items updated: {item_updates}")
            
            # 3. option_prices 업데이트
            price_updates = update_option_prices(cur)
            print(f"  - option_prices updated: {price_updates}")
            
            # 4. option_base_prices 업데이트
            base_price_updates = update_option_base_prices(cur)
            print(f"  - option_base_prices updated: {base_price_updates}")
            
            con.commit()
            print(f"  [OK] Committed")
            
        except Exception as e:
            con.rollback()
            print(f"  [ERROR] {e}")
            import traceback
            traceback.print_exc()
        finally:
            con.close()
        
        print()
    
    print("[DONE] Update completed")


if __name__ == "__main__":
    main()

