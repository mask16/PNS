"""SUPER M SERIES 콜아크코드 채번·코드명"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

TYPE_OPTIONS = ("A", "W")
FLAG_OPTIONS = ("1", "0")
FEEDER_OPTIONS = ("4", "4S")
HOSE_OPTIONS = ("2", "5", "10", "15", "20")
TORCH_OPTIONS = ("0", "4")
GROUND_OPTIONS = ("0", "3", "5")

SHARED_OPTIONS = {
    "type_code": TYPE_OPTIONS,
    "water_cooler": FLAG_OPTIONS,
    "coolant": FLAG_OPTIONS,
    "trolley": FLAG_OPTIONS,
    "feeder": FEEDER_OPTIONS,
    "hose": HOSE_OPTIONS,
    "torch": TORCH_OPTIONS,
    "ground": GROUND_OPTIONS,
}

_COMMON_DEFAULTS = {
    "type_code": "W",
    "water_cooler": "1",
    "coolant": "1",
    "trolley": "1",
    "feeder": "4",
    "hose": "5",
    "ground": "5",
    "call_arc_code": "",
}

SHEET_SPECS = {
    "M": {
        "prefix": "M",
        "series": "SUPER M SERIES",
        "gap_start": 51000000,
        "capacity": ("35", "45", "50", "60", "50_R", "50_SH"),
        "defaults": {**_COMMON_DEFAULTS, "capacity": "50", "torch": "4"},
    },
    "M2": {
        "prefix": "MX",
        "series": "SUPER M SERIES",
        "gap_start": 51500000,
        "capacity": ("35", "45", "50", "50_S", "50_S_6C", "60", "50_R", "50_SH"),
        "hose": ("0", "2", "5", "10", "15", "20"),
        "defaults": {**_COMMON_DEFAULTS, "capacity": "50_S", "torch": "0"},
    },
    "C": {
        "prefix": "C",
        "series": "SUPER C SERIES",
        "gap_start": 52000000,
        "layout": "c",
        "target_length": 30,
        "capacity": ("30", "30_S", "30_SH", "30_T", "30_15_T", "35"),
        "torch": ("0", "4"),
        "ground": ("0", "3", "5"),
        "gas": ("1.5",),
        "defaults": {
            "capacity": "35",
            "type_code": "W",
            "water_cooler": "1",
            "coolant": "1",
            "trolley": "1",
            "torch": "4",
            "ground": "5",
            "gas": "1.5",
            "call_arc_code": "",
        },
    },
    "C2": {
        "prefix": "CX",
        "series": "SUPER C SERIES",
        "gap_start": 52500000,
        "layout": "c",
        "target_length": 30,
        "capacity": ("22", "30", "35"),
        "torch": ("0", "4"),
        "ground": ("0", "3", "5"),
        "gas": ("1.5",),
        "defaults": {
            "capacity": "35",
            "type_code": "W",
            "water_cooler": "1",
            "coolant": "1",
            "trolley": "1",
            "torch": "0",
            "ground": "5",
            "gas": "1.5",
            "call_arc_code": "",
        },
    },
    "T": {
        "prefix": "T",
        "series": "SUPER T SERIES",
        "gap_start": 53000000,
        "layout": "c",
        "target_length": 30,
        "capacity": ("22", "27", "40"),
        "torch": ("0", "4"),
        "ground": ("0", "3", "5"),
        "gas": ("1.5",),
        "defaults": {
            "capacity": "40",
            "type_code": "W",
            "water_cooler": "1",
            "coolant": "1",
            "trolley": "1",
            "torch": "4",
            "ground": "5",
            "gas": "1.5",
            "call_arc_code": "",
        },
    },
    "T2": {
        "prefix": "TX",
        "series": "SUPER T SERIES",
        "gap_start": 53500000,
        "layout": "c",
        "target_length": 30,
        "capacity": ("22", "27", "40"),
        "torch": ("0", "4"),
        "ground": ("0", "3", "5"),
        "gas": ("1.5",),
        "defaults": {
            "capacity": "27",
            "type_code": "A",
            "water_cooler": "0",
            "coolant": "0",
            "trolley": "0",
            "torch": "4",
            "ground": "5",
            "gas": "1.5",
            "call_arc_code": "",
        },
    },
    "S": {
        "prefix": "S",
        "prefix_label": "STICK",
        "series": "SUPER S SERIES",
        "gap_start": 54000000,
        "layout": "s",
        "target_length": 22,
        "capacity": ("20", "22", "27", "40"),
        "holder": ("5",),
        "ground": ("0", "3", "5"),
        "defaults": {
            "capacity": "27",
            "type_code": "A",
            "holder": "5",
            "ground": "3",
            "call_arc_code": "",
        },
    },
    "S2": {
        "prefix": "SX",
        "prefix_label": "STICK",
        "series": "SUPER S SERIES",
        "gap_start": 54500000,
        "layout": "s",
        "target_length": 22,
        "capacity": ("20", "22", "27", "40"),
        "holder": ("0", "5"),
        "ground": ("0", "3", "5"),
        "defaults": {
            "capacity": "27",
            "type_code": "A",
            "holder": "0",
            "ground": "3",
            "call_arc_code": "",
        },
    },
}


def now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def resolve_sheet(name) -> str:
    key = str(name or "M").strip().upper()
    return key if key in SHEET_SPECS else "M"


def ensure_call_arc_code_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS call_arc_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gap_code TEXT NOT NULL UNIQUE,
            capacity TEXT NOT NULL,
            type_code TEXT NOT NULL,
            water_cooler TEXT NOT NULL,
            coolant TEXT NOT NULL,
            trolley TEXT NOT NULL,
            feeder TEXT NOT NULL,
            hose TEXT NOT NULL,
            torch TEXT NOT NULL,
            ground TEXT NOT NULL,
            call_arc_code TEXT NOT NULL DEFAULT '',
            code_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(call_arc_codes)")}
    if "sheet" not in columns:
        cursor.execute("ALTER TABLE call_arc_codes ADD COLUMN sheet TEXT NOT NULL DEFAULT 'M'")
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(call_arc_codes)")}
    if "gas" not in columns:
        cursor.execute("ALTER TABLE call_arc_codes ADD COLUMN gas TEXT NOT NULL DEFAULT ''")
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(call_arc_codes)")}
    if "holder" not in columns:
        cursor.execute("ALTER TABLE call_arc_codes ADD COLUMN holder TEXT NOT NULL DEFAULT ''")


def build_code_name(fields: dict, sheet: str = "M") -> str:
    """접두 + 용량부터 콜아크코드까지 이어 붙인다."""
    spec = SHEET_SPECS[resolve_sheet(sheet)]
    if spec.get("layout") == "s":
        return (
            spec["prefix"]
            + fields["capacity"]
            + ","
            + fields["type_code"]
            + ",HO"
            + fields.get("holder", "")
            + ",E"
            + fields["ground"]
            + "_"
            + (fields.get("call_arc_code") or "")
        )
    if spec.get("layout") == "c":
        return (
            spec["prefix"]
            + fields["capacity"]
            + ","
            + fields["type_code"]
            + fields["water_cooler"]
            + fields["coolant"]
            + fields["trolley"]
            + ",T"
            + fields["torch"]
            + ",E"
            + fields["ground"]
            + ",G"
            + fields.get("gas", "")
            + "_"
            + (fields.get("call_arc_code") or "")
        )
    return (
        spec["prefix"]
        + fields["capacity"]
        + ","
        + fields["type_code"]
        + fields["water_cooler"]
        + fields["coolant"]
        + fields["trolley"]
        + ","
        + "WF"
        + fields["feeder"]
        + ","
        + "H"
        + fields["hose"]
        + ","
        + "T"
        + fields["torch"]
        + ","
        + "E"
        + fields["ground"]
        + "_"
        + (fields.get("call_arc_code") or "")
    )


def _clean_choice(name: str, value, default: str, sheet: str) -> str:
    text = "" if value is None else str(value).strip()
    spec = SHEET_SPECS[resolve_sheet(sheet)]
    allowed = spec.get(name) or SHARED_OPTIONS.get(name)
    if not allowed:
        return default
    if text in allowed:
        return text
    return default


def normalize_fields(payload: dict | None, sheet: str = "M") -> dict:
    payload = payload or {}
    sheet = resolve_sheet(sheet)
    fields = {}
    for key, default in SHEET_SPECS[sheet]["defaults"].items():
        if key == "call_arc_code":
            fields[key] = str(payload.get(key) or "").strip()
        else:
            fields[key] = _clean_choice(key, payload.get(key), default, sheet)
    return fields


def next_gap_code(cursor, sheet: str = "M") -> str:
    ensure_call_arc_code_table(cursor)
    sheet = resolve_sheet(sheet)
    start = SHEET_SPECS[sheet]["gap_start"]
    cursor.execute(
        "SELECT gap_code FROM call_arc_codes WHERE sheet = ?",
        (sheet,),
    )
    current = start - 1
    for (code,) in cursor.fetchall():
        text = str(code or "")
        if text.startswith("P") and text[1:].isdigit():
            current = max(current, int(text[1:]))
    if current < start:
        current = start - 1
    return f"P{current + 1}"


def row_to_dict(row) -> dict:
    keys = row.keys() if hasattr(row, "keys") else None
    if keys:
        data = {key: row[key] for key in keys}
    else:
        data = {
            "id": row[0],
            "gap_code": row[1],
            "capacity": row[2],
            "type_code": row[3],
            "water_cooler": row[4],
            "coolant": row[5],
            "trolley": row[6],
            "feeder": row[7],
            "hose": row[8],
            "torch": row[9],
            "ground": row[10],
            "call_arc_code": row[11],
            "code_name": row[12],
            "gas": row[13] if len(row) > 13 else "",
            "sheet": row[14] if len(row) > 14 else "M",
            "holder": row[15] if len(row) > 15 else "",
        }
    data["gas"] = data.get("gas") or ""
    data["holder"] = data.get("holder") or ""
    data["sheet"] = resolve_sheet(data.get("sheet"))
    data["code_length"] = len(data.get("code_name") or "")
    return data


def list_call_arc_codes(cursor, sheet: str = "M") -> list[dict]:
    ensure_call_arc_code_table(cursor)
    sheet = resolve_sheet(sheet)
    cursor.execute(
        """
        SELECT id, gap_code, capacity, type_code, water_cooler, coolant, trolley,
               feeder, hose, torch, ground, call_arc_code, code_name, gas, sheet, holder
        FROM call_arc_codes
        WHERE sheet = ?
        ORDER BY gap_code
        """,
        (sheet,),
    )
    return [row_to_dict(row) for row in cursor.fetchall()]


def create_call_arc_code(cursor, payload: dict | None) -> dict:
    ensure_call_arc_code_table(cursor)
    payload = payload or {}
    sheet = resolve_sheet(payload.get("sheet"))
    fields = normalize_fields(payload, sheet)
    gap_code = next_gap_code(cursor, sheet)
    code_name = build_code_name(fields, sheet)
    now = now_kst()
    cursor.execute(
        """
        INSERT INTO call_arc_codes (
            gap_code, capacity, type_code, water_cooler, coolant, trolley,
            feeder, hose, torch, ground, gas, holder, call_arc_code, code_name, sheet, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gap_code,
            fields["capacity"],
            fields["type_code"],
            fields.get("water_cooler", ""),
            fields.get("coolant", ""),
            fields.get("trolley", ""),
            fields.get("feeder", ""),
            fields.get("hose", ""),
            fields.get("torch", ""),
            fields.get("ground", ""),
            fields.get("gas", ""),
            fields.get("holder", ""),
            fields["call_arc_code"],
            code_name,
            sheet,
            now,
            now,
        ),
    )
    cursor.execute(
        """
        SELECT id, gap_code, capacity, type_code, water_cooler, coolant, trolley,
               feeder, hose, torch, ground, call_arc_code, code_name, gas, sheet, holder
        FROM call_arc_codes WHERE id = ?
        """,
        (cursor.lastrowid,),
    )
    return row_to_dict(cursor.fetchone())


def update_call_arc_code(cursor, row_id: int, payload: dict | None) -> dict | None:
    ensure_call_arc_code_table(cursor)
    cursor.execute("SELECT sheet FROM call_arc_codes WHERE id = ?", (row_id,))
    found = cursor.fetchone()
    if not found:
        return None
    sheet = resolve_sheet(found["sheet"] if hasattr(found, "keys") else found[0])
    fields = normalize_fields(payload, sheet)
    code_name = build_code_name(fields, sheet)
    cursor.execute(
        """
        UPDATE call_arc_codes
        SET capacity = ?, type_code = ?, water_cooler = ?, coolant = ?, trolley = ?,
            feeder = ?, hose = ?, torch = ?, ground = ?, gas = ?, holder = ?, call_arc_code = ?,
            code_name = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            fields["capacity"],
            fields["type_code"],
            fields.get("water_cooler", ""),
            fields.get("coolant", ""),
            fields.get("trolley", ""),
            fields.get("feeder", ""),
            fields.get("hose", ""),
            fields.get("torch", ""),
            fields.get("ground", ""),
            fields.get("gas", ""),
            fields.get("holder", ""),
            fields["call_arc_code"],
            code_name,
            now_kst(),
            row_id,
        ),
    )
    cursor.execute(
        """
        SELECT id, gap_code, capacity, type_code, water_cooler, coolant, trolley,
               feeder, hose, torch, ground, call_arc_code, code_name, gas, sheet, holder
        FROM call_arc_codes WHERE id = ?
        """,
        (row_id,),
    )
    saved = row_to_dict(cursor.fetchone())
    publish_call_arc_to_staff_dashboard(cursor, saved)
    return saved


STAFF_DASHBOARD_CATEGORY = 13


def publish_call_arc_to_staff_dashboard(cursor, row: dict) -> None:
    """수출견적서(스태프) 대시보드에 코드만 추가한다.

    프라이스북단가, 구매가원가, 마진액, 마진율은 넣지 않는다.
    """
    code = (row.get("gap_code") or "").strip()
    if not code:
        return
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS export_quotation_new_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_code TEXT,
            product_name TEXT,
            price_book_price REAL,
            purchase_price REAL,
            category_number INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    now = now_kst()
    name = row.get("code_name") or ""
    cursor.execute(
        "SELECT id FROM export_quotation_new_products WHERE product_code = ?",
        (code,),
    )
    if cursor.fetchone():
        cursor.execute(
            """
            UPDATE export_quotation_new_products
            SET product_name = ?, updated_at = ?
            WHERE product_code = ?
            """,
            (name, now, code),
        )
        return
    cursor.execute(
        """
        INSERT INTO export_quotation_new_products (
            product_code, product_name, price_book_price, purchase_price,
            category_number, created_at, updated_at
        ) VALUES (?, ?, NULL, NULL, ?, ?, ?)
        """,
        (code, name, STAFF_DASHBOARD_CATEGORY, now, now),
    )


def remove_call_arc_from_staff_dashboard(cursor, gap_code: str) -> None:
    """SAP코드와 같은 제품코드를 수출견적서(스태프) 대시보드에서 삭제한다."""
    code = (gap_code or "").strip()
    if not code:
        return
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'export_quotation_new_products'"
    )
    if not cursor.fetchone():
        return
    cursor.execute(
        "DELETE FROM export_quotation_new_products WHERE product_code = ?",
        (code,),
    )


def delete_call_arc_code(cursor, row_id: int) -> bool:
    ensure_call_arc_code_table(cursor)
    cursor.execute("SELECT gap_code FROM call_arc_codes WHERE id = ?", (row_id,))
    found = cursor.fetchone()
    if not found:
        return False
    gap_code = found["gap_code"] if hasattr(found, "keys") else found[0]
    cursor.execute("DELETE FROM call_arc_codes WHERE id = ?", (row_id,))
    if cursor.rowcount <= 0:
        return False
    remove_call_arc_from_staff_dashboard(cursor, gap_code)
    return True
