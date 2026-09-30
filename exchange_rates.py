#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""하나은행 고시환율 조회 및 캐시"""

from __future__ import annotations

import re
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Dict, Optional

KST = timezone(timedelta(hours=9))
HANA_RATE_URL = "https://www.kebhana.com/cms/rate/wpfxd651_01i_01.do"
HANA_REFERER = "https://www.kebhana.com/cms/rate/wpfxd651_01i.do"


class _HanaRateTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_row = False
        self.in_td = False
        self.current_cells = []
        self.current_text = []
        self.rows = []
        self.base_date = None
        self.announced_at = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "tr":
            self.in_row = True
            self.current_cells = []
        elif tag == "td" and self.in_row:
            self.in_td = True
            self.current_text = []

    def handle_endtag(self, tag):
        if tag == "td" and self.in_td:
            text = " ".join("".join(self.current_text).split())
            self.current_cells.append(text)
            self.in_td = False
            self.current_text = []
        elif tag == "tr" and self.in_row:
            if self.current_cells:
                self.rows.append(self.current_cells)
            self.in_row = False
            self.current_cells = []

    def handle_data(self, data):
        if self.in_td:
            self.current_text.append(data)


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _parse_number(value: str) -> Optional[float]:
    if not value:
        return None
    cleaned = value.replace(",", "").replace(" ", "")
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def _extract_currency_code(cell: str) -> Optional[str]:
    if not cell:
        return None
    match = re.search(r"\b([A-Z]{3})\b", cell.upper())
    return match.group(1) if match else None


def fetch_hana_exchange_rates(inquiry_date: Optional[str] = None) -> Dict:
    """하나은행 고시환율 조회 (매매기준율 기준)."""
    if inquiry_date:
        inq_dt = inquiry_date.replace("-", "")
    else:
        inq_dt = datetime.now(KST).strftime("%Y%m%d")

    payload = urllib.parse.urlencode({
        "ajax": "1",
        "curCd": "",
        "tmpInqDt": inq_dt,
        "pbldDvCd": "3",  # 현재/최종 고시
        "inqKindCd": "1",
        "requestTarget": "searchContentDiv",
    }).encode("utf-8")

    request = urllib.request.Request(
        HANA_RATE_URL,
        data=payload,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": HANA_REFERER,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="POST",
    )

    context = ssl.create_default_context()
    with urllib.request.urlopen(request, context=context, timeout=20) as response:
        html = response.read().decode("utf-8", errors="replace")

    parser = _HanaRateTableParser()
    parser.feed(html)

    date_match = re.search(r"기준일</em>\s*:\s*<strong>(\d{4})년(\d{2})월(\d{2})일</strong>", html)
    base_date = (
        f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
        if date_match else _today_kst()
    )

    announced_match = re.search(
        r"고시일시</em>\s*:\s*<strong>\s*(\d{4})년(\d{2})월(\d{2})일\s*</strong>\s*<strong>\s*(\d{1,2})시(\d{1,2})분",
        html,
        re.DOTALL,
    )
    announced_at = None
    if announced_match:
        announced_at = (
            f"{announced_match.group(1)}-{announced_match.group(2)}-{announced_match.group(3)} "
            f"{int(announced_match.group(4)):02d}:{int(announced_match.group(5)):02d}"
        )

    rates = {}
    for row in parser.rows:
        if len(row) < 9:
            continue
        code = _extract_currency_code(row[0])
        if not code:
            continue
        # 매매기준율: 뒤에서 3번째(외화수표, 매매기준율, 환가료율, 미화환산율)
        basic_rate = _parse_number(row[-3]) if len(row) >= 3 else None
        usd_conv = _parse_number(row[-1]) if len(row) >= 1 else None
        if basic_rate is None:
            continue
        rates[code] = {
            "currency": code,
            "basic_rate": basic_rate,       # KRW per 1 unit (JPY는 100엔 기준일 수 있음)
            "usd_conversion_rate": usd_conv,
            "name": row[0],
        }

    usd_krw = rates.get("USD", {}).get("basic_rate")
    eur_krw = rates.get("EUR", {}).get("basic_rate")
    eur_usd = rates.get("EUR", {}).get("usd_conversion_rate")
    if eur_usd is None and usd_krw and eur_krw:
        eur_usd = round(eur_krw / usd_krw, 4)

    return {
        "success": True,
        "source": "KEB Hana Bank",
        "base_date": base_date,
        "announced_at": announced_at,
        "fetched_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "usd_krw": usd_krw,
        "eur_krw": eur_krw,
        "eur_usd": eur_usd,
        "krw": 1.0,
        "rates": rates,
    }


def ensure_exchange_rate_table(cursor) -> None:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS exchange_rates_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            base_date TEXT NOT NULL UNIQUE,
            usd_krw REAL,
            eur_krw REAL,
            eur_usd REAL,
            announced_at TEXT,
            source TEXT,
            raw_json TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')


def get_cached_rates(cursor, base_date: Optional[str] = None) -> Optional[Dict]:
    ensure_exchange_rate_table(cursor)
    if base_date:
        cursor.execute('''
            SELECT base_date, usd_krw, eur_krw, eur_usd, announced_at, source, updated_at
            FROM exchange_rates_daily
            WHERE base_date = ?
        ''', (base_date,))
    else:
        cursor.execute('''
            SELECT base_date, usd_krw, eur_krw, eur_usd, announced_at, source, updated_at
            FROM exchange_rates_daily
            ORDER BY base_date DESC
            LIMIT 1
        ''')
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "success": True,
        "source": row[5] or "KEB Hana Bank",
        "base_date": row[0],
        "announced_at": row[4],
        "fetched_at": row[6],
        "usd_krw": row[1],
        "eur_krw": row[2],
        "eur_usd": row[3],
        "krw": 1.0,
        "cached": True,
    }


def save_rates(cursor, rates: Dict) -> None:
    ensure_exchange_rate_table(cursor)
    import json
    cursor.execute('''
        INSERT INTO exchange_rates_daily
            (base_date, usd_krw, eur_krw, eur_usd, announced_at, source, raw_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(base_date) DO UPDATE SET
            usd_krw = excluded.usd_krw,
            eur_krw = excluded.eur_krw,
            eur_usd = excluded.eur_usd,
            announced_at = excluded.announced_at,
            source = excluded.source,
            raw_json = excluded.raw_json,
            updated_at = CURRENT_TIMESTAMP
    ''', (
        rates.get("base_date"),
        rates.get("usd_krw"),
        rates.get("eur_krw"),
        rates.get("eur_usd"),
        rates.get("announced_at"),
        rates.get("source"),
        json.dumps({
            "usd_krw": rates.get("usd_krw"),
            "eur_krw": rates.get("eur_krw"),
            "eur_usd": rates.get("eur_usd"),
        }, ensure_ascii=False),
    ))
