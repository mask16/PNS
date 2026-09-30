#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""COOLANT 이름 확인 스크립트"""

import sqlite3
import json

OLD_NAME = "COOLANT(SUPER COOLANT 2.5L)"
NEW_NAME = "COOLANT(SUPER COOLANT 2.0L)"

db_path = "welding_options.db"
con = sqlite3.connect(db_path)
cur = con.cursor()

# 1. selected_combinations에서 OLD_NAME이 남아있는지 확인
cur.execute(
    "SELECT id, selected_options FROM selected_combinations WHERE selected_options LIKE ? LIMIT 10",
    (f"%{OLD_NAME}%",)
)
rows = cur.fetchall()
print(f"[1] selected_combinations with OLD_NAME: {len(rows)}")
for r in rows[:3]:
    print(f"  id: {r[0]}")
    try:
        d = json.loads(r[1])
        for k, v in d.items():
            if isinstance(v, str) and OLD_NAME in v:
                print(f"    cat[{k}]: {v}")
    except:
        print("    parse_error")

# 2. option_items 확인
cur.execute("SELECT item_name FROM option_items WHERE item_name LIKE ?", ("%COOLANT%",))
rows = cur.fetchall()
print(f"\n[2] option_items with COOLANT: {len(rows)}")
for r in rows:
    print(f"  - {r[0]}")

# 3. option_prices 확인
cur.execute("SELECT option_name FROM option_prices WHERE option_name LIKE ?", ("%COOLANT%",))
rows = cur.fetchall()
print(f"\n[3] option_prices with COOLANT: {len(rows)}")
for r in rows[:5]:
    print(f"  - {r[0]}")

con.close()








































