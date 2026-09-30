#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
웰딩 장비 옵션 선택 시스템 (PNS) - 핵심 시스템
"""

import sqlite3
import hashlib
import json
import os
import sys
import threading
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from logic_rules_data import DETAILED_RULES

# 한글 인코딩 설정
if sys.platform.startswith('win'):
    import locale
    try:
        locale.setlocale(locale.LC_ALL, 'ko_KR.UTF-8')
    except:
        pass

class WeldingOptionSystem:
    _schema_lock = threading.Lock()

    def __init__(self, db_path=None):
        """웰딩 옵션 시스템 초기화"""
        if db_path is None:
            # IIS/작업폴더와 무관하게 항상 프로젝트 루트의 쓰기 가능 DB 사용
            # (기존 welding_options.db 가 OS ACL로 읽기전용이 되는 경우 대비)
            base_dir = os.path.dirname(os.path.abspath(__file__))
            preferred = os.path.join(base_dir, "welding_options_data.db")
            legacy = os.path.join(base_dir, "welding_options.db")
            db_path = preferred if os.path.exists(preferred) else legacy
        self.db_path = db_path
        self.connection = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # WAL 모드 설정으로 동시 접근 개선
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA cache_size=10000")
        self.connection.execute("PRAGMA temp_store=MEMORY")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._initialize_tables()
    
    def _initialize_tables(self):
        """테이블 초기화"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                
                # 옵션 카테고리 테이블
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS option_categories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        category_name TEXT NOT NULL,
                        category_number INTEGER UNIQUE NOT NULL
                    )
                ''')
                
                # 옵션 아이템 테이블
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS option_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        category_id INTEGER NOT NULL,
                        item_name TEXT NOT NULL,
                        item_code TEXT,
                        description TEXT,
                        FOREIGN KEY (category_id) REFERENCES option_categories (id)
                    )
                ''')
                
                # 선택된 조합 테이블
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS selected_combinations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        combination_code TEXT UNIQUE NOT NULL,
                        set_code TEXT,
                        call_arc_code TEXT,
                        selected_options TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # 기존 테이블에 컬럼 추가 (마이그레이션)
                try:
                    cursor.execute('ALTER TABLE selected_combinations ADD COLUMN set_code TEXT')
                except sqlite3.OperationalError:
                    pass  # 컬럼이 이미 존재하는 경우
                
                try:
                    cursor.execute('ALTER TABLE selected_combinations ADD COLUMN call_arc_code TEXT')
                except sqlite3.OperationalError:
                    pass  # 컬럼이 이미 존재하는 경우
                
                try:
                    cursor.execute('ALTER TABLE selected_combinations ADD COLUMN price REAL')
                except sqlite3.OperationalError:
                    pass  # 컬럼이 이미 존재하는 경우
                
                # 옵션별 가격 테이블
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS option_prices (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        set_code TEXT NOT NULL,
                        category_number INTEGER NOT NULL,
                        option_name TEXT NOT NULL,
                        price REAL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(set_code, category_number, option_name)
                    )
                ''')
                
                # 로직 제한조건 테이블
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS logic_rules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_index INTEGER NOT NULL DEFAULT 0,
                        power_source_code TEXT,
                        power_source_value TEXT,
                        type_code TEXT,
                        type_value TEXT,
                        actions_json TEXT NOT NULL
                    )
                ''')
                
                # 견적서 양식 테이블
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS quote_template (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_name TEXT,
                        company_address TEXT,
                        company_phone TEXT,
                        company_email TEXT,
                        company_logo TEXT,
                        quote_title TEXT DEFAULT '견적서',
                        footer_note TEXT,
                        terms_conditions TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # 수출견적서 제품 정보 테이블
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS export_quotation_products (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        product_code TEXT,
                        product_name TEXT,
                        price_book_price REAL,
                        product_cost REAL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                self._ensure_export_quotation_new_products_schema(cursor)
                self._ensure_exchange_rates_schema(cursor)
                self._ensure_user_tracking_schema(cursor)
                self._ensure_export_label_schema(cursor)
                self._ensure_option_view_image_schema(cursor)
                
                # 기본 양식 데이터가 없으면 생성
                cursor.execute('SELECT COUNT(*) FROM quote_template')
                if cursor.fetchone()[0] == 0:
                    cursor.execute('''
                        INSERT INTO quote_template 
                        (company_name, quote_title, footer_note)
                        VALUES (?, ?, ?)
                    ''', ('회사명', '견적서', '본 견적서는 유효기간 30일입니다.'))
                
                cursor.execute("SELECT COUNT(*) FROM logic_rules")
                if cursor.fetchone()[0] == 0:
                    self._seed_logic_rules(cursor)
                
                # 기본 데이터 삽입 (이미 존재하는지 확인)
                cursor.execute("SELECT COUNT(*) FROM option_categories")
                if cursor.fetchone()[0] == 0:
                    self._insert_default_data(cursor)
                else:
                    # 기존 데이터베이스의 카테고리 번호 마이그레이션
                    self._migrate_category_numbers(cursor)

                # 최신 로직 제한조건 상태 보정
                self._apply_logic_rule_updates(cursor)
                    
        except sqlite3.Error as e:
            print(f"데이터베이스 초기화 오류: {e}")
    
    def _insert_default_data(self, cursor):
        """기본 데이터 삽입"""
        # 11개 카테고리 데이터
        categories = [
            (1, "파워소스", 1),
            (2, "타입", 2),
            (3, "와이어피더", 3),
            (4, "토치", 4),
            (5, "호스패키지", 5),
            (6, "어스", 6),
            (7, "트롤리", 7),
            (8, "쿨러", 8),
            (9, "쿨란트", 9),
            (10, "가스호스", 10),
            (11, "ELECTRODE HOLDER", 11)
        ]
        
        for category in categories:
            cursor.execute("INSERT INTO option_categories (id, category_name, category_number) VALUES (?, ?, ?)", category)
        
        # 각 카테고리의 옵션 아이템 데이터
        items = [
            # 1. 파워소스 (category_id=1)
            (1, "SUPER T400 DC", "P35001772", "DC TIG(SUPER T400 DC)"),
            (1, "SUPER T400 DC/AC", "P35002083", "DC TIG(SUPER T400 DC/AC)"),
            (1, "SUPER T270 DC", "P35001799", "DC TIG(SUPER T270 DC)"),
            (1, "SUPER T270 DC/AC", "P35002084", "DC TIG(SUPER T270 DC/AC)"),
            (1, "SUPER T220 DC", "P35002085", "DC TIG(SUPER T220 DC)"),
            (1, "SUPER T200 DC/AC", "P35002086", "DC TIG(SUPER T220 DC/AC)"),
            (1, "SUPER T200 DC", "P35002087", "DC TIG(SUPER T200 DC)"),
            (1, "SUPER T200 DC/AC", "P35002088", "DC TIG(SUPER T200 DC/AC)"),
            (1, "SUPER S400", "P35001725", "MMA(SUPER S400)"),
            (1, "SUPER S270", "P35001821", "MMA(SUPER S270)"),
            (1, "SUPER S220", "P35002089", "MMA(SUPER S220)"),
            (1, "SUPER S200", "P35002090", "MMA(SUPER S200)"),
            (1, "SUPER M500", "P35001822", "MAG(SUPER M500)"),
            (1, "SUPER M450", "P35001797", "MAG(SUPER M450)"),
            (1, "SUPER M350", "P35001796", "MAG(SUPER M350)"),
            (1, "SUPER C350", "P35001798", "MAG(SUPER C350)"),
            (1, "SUPER C300", "P35001850", "MAG(SUPER C300)"),
            
            # 2. 타입 (category_id=2)
            (2, "AIR", "T001", "AIR 타입"),
            (2, "WATER", "T002", "WATER 타입"),
            
            # 3. 와이어피더 (category_id=3)
            (3, "Wire Feeder XM4S W", "P35001733", "W/FEEDER(SUPER WF4S W)"),
            (3, "Wire Feeder XM4S", "P35001726", "W/FEEDER(SUPER WF4S)"),
            (3, "Wire Feeder XM4 W Pulse", "P35001775", "W/FEEDER(SUPER WF4 W)"),
            (3, "Wire Feeder XM4 Pulse", "P35001774", "W/FEEDER(SUPER WF4)"),
            
            # 4. 토치 (category_id=4)
            (4, "Torch, XT27 (4m)", "P35001734", "TORCH(SUPER T T27 4M)"),
            (4, "Torch, XT19 (4m)", "P35001818", "TORCH(SUPER T T19 W 4M)"),
            (4, "Torch, MX50 (Up-Down Remote,4m)", "P35001727", "TORCH(SUPER M T50 W 4M REMOTE)"),
            (4, "Torch, MX36 (Up-Down Remote, 4m)", "P35001819", "TORCH(SUPER M T36 4M REMOTE)"),
            (4, "Torch, MX25(Up-Down Remote, 4m)", "P35001820", "TORCH(SUPER M T25 4M REMOTE)"),
            
            # 5. 호스패키지 (category_id=5)
            (5, "Hose Package, 600 W 5m", "P35001823", "HOSE PACKAGE (SUPER HP 600 W 5M)"),
            (5, "Hose Package, 600 W 20m", "P35001824", "HOSE PACKAGE (SUPER HP 600 W 20M)"),
            (5, "Hose Package, 600 W 15m", "P35001825", "HOSE PACKAGE (SUPER HP 600 W 15M)"),
            (5, "Hose Package, 600 W 10m", "P35001826", "HOSE PACKAGE (SUPER HP 600 W 10M)"),
            (5, "Hose Package, 600 W 5m", "P35001872", "HOSE PACKAGE (SUPER HP 600 W 5M)"),
            (5, "Hose Package, 600 5m", "P35001827", "HOSE PACKAGE (SUPER HP 600 5M)"),
            (5, "Hose Package, 600 20m", "P35001828", "HOSE PACKAGE (SUPER HP 600 20M)"),
            (5, "Hose Package, 600 15m", "P35001829", "HOSE PACKAGE (SUPER HP 600 15M)"),
            (5, "Hose Package, 600 10m", "P35001830", "HOSE PACKAGE (SUPER HP 600 10M)"),
            (5, "Hose Package, 500 W 5m", "P35001831", "HOSE PACKAGE (SUPER HP 500 W 5M)"),
            (5, "Hose Package, 500 W 20m", "P35001832", "HOSE PACKAGE (SUPER HP 500 W 20M)"),
            (5, "Hose Package, 500 W 15m", "P35001833", "HOSE PACKAGE (SUPER HP 500 W 15M)"),
            (5, "Hose Package, 500 W 10m", "P35001834", "HOSE PACKAGE (SUPER HP 500 W 10M)"),
            (5, "Hose Package, 500 5m", "P35001835", "HOSE PACKAGE (SUPER HP 500 5M)"),
            (5, "Hose Package, 500 20m", "P35001803", "HOSE PACKAGE (SUPER HP 500 20M)"),
            (5, "Hose Package, 500 15m", "P35001802", "HOSE PACKAGE (SUPER HP 500 15M)"),
            (5, "Hose Package, 500 10m", "P35001801", "HOSE PACKAGE (SUPER HP 500 10M)"),
            (5, "Hose Package, 350 W 5m", "P35001836", "HOSE PACKAGE (SUPER HP 350 W 5M)"),
            (5, "Hose Package, 350 W 20m", "P35001837", "HOSE PACKAGE (SUPER HP 350 W 20M)"),
            (5, "Hose Package, 350 W 15m", "P35001838", "HOSE PACKAGE (SUPER HP 350 W 15M)"),
            (5, "Hose Package, 350 W 10m", "P35001839", "HOSE PACKAGE (SUPER HP 350 W 10M)"),
            (5, "Hose Package, 350 5m", "P35001840", "HOSE PACKAGE (SUPER HP 350 5M)"),
            (5, "Hose Package, 350 20m", "P35001841", "HOSE PACKAGE (SUPER HP 350 20M)"),
            (5, "Hose Package, 350 15m", "P35001842", "HOSE PACKAGE (SUPER HP 350 15M)"),
            (5, "Hose Package, 350 10m", "P35001843", "HOSE PACKAGE (SUPER HP 350 10M)"),
            
            # 6. 어스 (category_id=6)
            (6, "Ground Clamp (5m, 95mm2)", "P35001844", "GROUND CABLE (SUPER CLAMP 95SQ 5M)"),
            (6, "Ground Clamp (5m, 70mm2)", "P35001732", "GROUND CABLE (SUPER CLAMP 70SQ 5M)"),
            (6, "Ground Clamp (3m, 50mm2)", "P35001845", "GROUND CABLE (SUPER CLAMP 50SQ 3M)"),
            (6, "Ground Clamp (3m, 35mm2)", "P35001852", "GROUND CABLE (SUPER CLAMP 35SQ 3M)"),
            (6, "Ground Clamp (2m, 16mm2)", "P35001846", "GROUND CABLE (SUPER CLAMP 16SQ 2M)"),
            
            # 7. 트롤리 (category_id=7)
            (7, "Trolley, XT", "P35001730", "TROLLEY(SUPER TR T)"),
            (7, "Trolley, XMS", "P35001800", "TROLLEY(SUPER TR MS)"),
            (7, "Trolley, XM", "P35001777", "TROLLEY(SUPER TR M)"),
            (7, "Trolley, 2 side", "P35001857", "TROLLEY (SUPER 2 SIDE)"),
            
            # 8. 쿨러 (category_id=8)
            (8, "Cooling Unit, XWL", "P35001728", "WATER COOLER(SUPER COOLER L)"),
            (8, "Cooling Unit, XW", "P35001776", "WATER COOLER(SUPER COOLER)"),
            
            # 9. 쿨런트 (category_id=9)
            (9, "Coolant (2.5L)", "P35001729", "COOLANT(SUPER COOLANT 2.5L)"),
            
            # 10. 가스호스 (category_id=10)
            (10, "Gas Hose (1.5m)", "P35001847", "GAS HOSE(SUPER GAS HOSE 1.5M)"),
            
            # 11. ELECTRODE HOLDER (category_id=11)
            (11, "Electrode Holder (5m, 50mm2)", "P35001873", "HOLDER CABLE(SUPER HOLDER 50SQ 5M)"),
            (11, "Electrode Holder (5m, 35mm2)", "P35001848", "HOLDER CABLE(SUPER HOLDER 35SQ 5M)"),
            (11, "Electrode Holder (3m, 16mm2)", "P35001849", "HOLDER CABLE(SUPER HOLDER 16SQ 3M)")
        ]
        
        for item in items:
            cursor.execute("INSERT INTO option_items (category_id, item_name, item_code, description) VALUES (?, ?, ?, ?)", item)

    def _ensure_schema_meta(self, cursor):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                key TEXT PRIMARY KEY,
                value TEXT,
                applied_at TEXT
            )
        """)

    def _has_migration(self, cursor, key):
        self._ensure_schema_meta(cursor)
        cursor.execute("SELECT 1 FROM schema_migrations WHERE key = ?", (key,))
        return cursor.fetchone() is not None

    def _record_migration(self, cursor, key, value="1"):
        self._ensure_schema_meta(cursor)
        cursor.execute(
            """
            INSERT INTO schema_migrations (key, value, applied_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE
            SET value = excluded.value,
                applied_at = excluded.applied_at
            """,
            (key, value, datetime.utcnow().isoformat())
        )

    def _ensure_export_quotation_new_products_schema(self, cursor):
        """수출견적서(스태프) 제품 테이블 스키마 - 앱 시작 시 1회만 마이그레이션"""
        with WeldingOptionSystem._schema_lock:
            migration_key = "export_quotation_new_products_schema_v2"
            if self._has_migration(cursor, migration_key):
                return

            cursor.execute('''
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
            ''')

            for column_name, column_type in (
                ('purchase_price', 'REAL'),
                ('price_book_price', 'REAL'),
                ('category_number', 'INTEGER'),
            ):
                try:
                    cursor.execute(
                        f'ALTER TABLE export_quotation_new_products ADD COLUMN {column_name} {column_type}'
                    )
                except sqlite3.OperationalError:
                    pass

            cursor.execute("PRAGMA table_info(export_quotation_new_products)")
            column_names = [col[1] for col in cursor.fetchall()]

            if 'unit_price' in column_names:
                try:
                    cursor.execute('''
                        UPDATE export_quotation_new_products
                        SET price_book_price = unit_price
                        WHERE price_book_price IS NULL AND unit_price IS NOT NULL
                    ''')
                except sqlite3.OperationalError:
                    pass

            if 'product_cost' in column_names:
                try:
                    cursor.execute('''
                        UPDATE export_quotation_new_products
                        SET purchase_price = product_cost
                        WHERE purchase_price IS NULL AND product_cost IS NOT NULL
                    ''')
                except sqlite3.OperationalError:
                    pass

            if 'price' in column_names:
                try:
                    cursor.execute('''
                        UPDATE export_quotation_new_products
                        SET price_book_price = price
                        WHERE price_book_price IS NULL AND price IS NOT NULL
                    ''')
                except sqlite3.OperationalError:
                    pass

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS option_base_prices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_number INTEGER NOT NULL,
                    option_name TEXT NOT NULL,
                    price REAL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(category_number, option_name)
                )
            ''')

            self._record_migration(cursor, migration_key)

    def _ensure_exchange_rates_schema(self, cursor):
        """일일 환율 캐시 테이블"""
        with WeldingOptionSystem._schema_lock:
            migration_key = "exchange_rates_daily_schema_v1"
            if self._has_migration(cursor, migration_key):
                return
            from exchange_rates import ensure_exchange_rate_table
            ensure_exchange_rate_table(cursor)
            self._record_migration(cursor, migration_key)

    def _ensure_user_tracking_schema(self, cursor):
        with WeldingOptionSystem._schema_lock:
            migration_key = "user_tracking_schema_v1"
            if self._has_migration(cursor, migration_key):
                return
            from user_tracking import ensure_user_tracking_tables
            ensure_user_tracking_tables(cursor)
            self._record_migration(cursor, migration_key)

    def _ensure_export_label_schema(self, cursor):
        with WeldingOptionSystem._schema_lock:
            migration_key = "export_label_uploads_schema_v1"
            if self._has_migration(cursor, migration_key):
                return
            from export_label_service import ensure_export_label_tables
            ensure_export_label_tables(cursor)
            self._record_migration(cursor, migration_key)

    def _ensure_option_view_image_schema(self, cursor):
        with WeldingOptionSystem._schema_lock:
            migration_key = "option_view_images_schema_v1"
            if self._has_migration(cursor, migration_key):
                pass
            else:
                from option_view_image_service import ensure_option_view_image_tables
                ensure_option_view_image_tables(cursor)
                self._record_migration(cursor, migration_key)
            migration_key_v2 = "option_view_image_regions_schema_v1"
            if not self._has_migration(cursor, migration_key_v2):
                from option_view_image_service import ensure_option_view_image_tables
                ensure_option_view_image_tables(cursor)
                self._record_migration(cursor, migration_key_v2)
            migration_key_v3 = "option_view_images_type_code_v1"
            if self._has_migration(cursor, migration_key_v3):
                return
            from option_view_image_service import ensure_option_view_image_tables
            ensure_option_view_image_tables(cursor)
            self._record_migration(cursor, migration_key_v3)

    def _apply_logic_rule_updates(self, cursor):
        """필수 로직 제한조건 상태를 강제로 정렬"""
        migration_key = "20241114_logic_rule_12_super_s400_air"
        if self._has_migration(cursor, migration_key):
            return

        desired_actions = [
            {"category": 11, "allowCodes": ["P58800000"], "allowNone": False},
            {"category": 6, "allowCodes": ["P58600003"], "allowNone": False},
        ]
        actions_json = json.dumps(desired_actions, ensure_ascii=False)

        cursor.execute("SELECT id FROM logic_rules WHERE id = ?", (12,))
        row = cursor.fetchone()

        if row:
            cursor.execute(
                """
                UPDATE logic_rules
                SET order_index = ?,
                    power_source_code = ?,
                    power_source_value = ?,
                    type_code = ?,
                    type_value = ?,
                    actions_json = ?
                WHERE id = ?
                """,
                (12, "P58100008", None, None, "AIR", actions_json, 12)
            )
        else:
            cursor.execute(
                """
                INSERT INTO logic_rules (
                    id,
                    order_index,
                    power_source_code,
                    power_source_value,
                    type_code,
                    type_value,
                    actions_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (12, 12, "P58100008", None, None, "AIR", actions_json)
            )

        self._record_migration(cursor, migration_key)
    
    def _seed_logic_rules(self, cursor):
        """로직 제한조건 초기 데이터 삽입"""
        for idx, rule in enumerate(DETAILED_RULES):
            actions = rule.get("actions", [])
            try:
                actions_json = json.dumps(actions, ensure_ascii=False)
            except (TypeError, ValueError):
                actions_json = "[]"
            
            order_index = rule.get("orderIndex")
            if order_index is None:
                order_index = idx
            
            cursor.execute(
                '''
                INSERT INTO logic_rules (
                    order_index,
                    power_source_code,
                    power_source_value,
                    type_code,
                    type_value,
                    actions_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    order_index,
                    rule.get("powerSourceCode"),
                    rule.get("powerSourceValue"),
                    rule.get("typeCode"),
                    rule.get("typeValue"),
                    actions_json
                )
            )
    
    def _migrate_category_numbers(self, cursor):
        """카테고리 번호와 명칭을 최신 순서로 정렬"""
        try:
            mapping = {
                "파워소스옵션": (1, "파워소스"),
                "파워소스": (1, "파워소스"),
                "타입옵션": (2, "타입"),
                "타입": (2, "타입"),
                "와이어피더옵션": (3, "와이어피더"),
                "와이어피더": (3, "와이어피더"),
                "피더": (3, "와이어피더"),
                "토치옵션": (4, "토치"),
                "토치": (4, "토치"),
                "호스패키지옵션": (5, "호스패키지"),
                "호스패키지": (5, "호스패키지"),
                "그라운드옵션": (6, "어스"),
                "어스": (6, "어스"),
                "트롤리옵션": (7, "트롤리"),
                "트롤리": (7, "트롤리"),
                "쿨링옵션": (8, "쿨러"),
                "쿨링": (8, "쿨러"),
                "쿨러": (8, "쿨러"),
                "쿨란드옵션": (9, "쿨란트"),
                "쿨런트": (9, "쿨란트"),
                "가스호스옵션": (10, "가스호스"),
                "가스호스": (10, "가스호스"),
                "가스": (10, "가스호스"),
                "일렉트로홀더옵션": (11, "ELECTRODE HOLDER"),
                "ELECTRODE HOLDER": (11, "ELECTRODE HOLDER")
            }
            
            cursor.execute("SELECT id, category_name, category_number FROM option_categories")
            rows = cursor.fetchall()
            
            needs_item_update = False
            for row in rows:
                category_id = row["id"]
                current_name = row["category_name"]
                current_number = row["category_number"]
                target_number, target_name = mapping.get(current_name, (current_number, current_name))
                
                if current_number != target_number:
                    cursor.execute(
                        "UPDATE option_categories SET category_number = ? WHERE id = ?",
                        (target_number, category_id)
                    )
                    needs_item_update = True
                
                if current_name != target_name:
                    cursor.execute(
                        "UPDATE option_categories SET category_name = ? WHERE id = ?",
                        (target_name, category_id)
                    )
                    needs_item_update = True
            
            print("카테고리 번호 및 명칭 마이그레이션이 완료되었습니다.")
        except sqlite3.Error as e:
            print(f"카테고리 번호 마이그레이션 오류: {e}")
    
    def _normalize_category_row(self, row):
        alias_map = {
            "파워소스옵션": "파워소스",
            "파워소스": "파워소스",
            "타입옵션": "타입",
            "타입": "타입",
            "와이어피더옵션": "와이어피더",
            "와이어피더": "와이어피더",
            "피더": "와이어피더",
            "토치옵션": "토치",
            "토치": "토치",
            "호스패키지옵션": "호스패키지",
            "호스패키지": "호스패키지",
            "그라운드옵션": "어스",
            "어스": "어스",
            "트롤리옵션": "트롤리",
            "트롤리": "트롤리",
            "쿨링옵션": "쿨러",
            "쿨링": "쿨러",
            "쿨러": "쿨러",
            "쿨란드옵션": "쿨란트",
            "쿨런트": "쿨란트",
            "가스호스옵션": "가스호스",
            "가스호스": "가스호스",
            "가스": "가스호스",
            "일렉트로홀더옵션": "ELECTRODE HOLDER",
            "ELECTRODE HOLDER": "ELECTRODE HOLDER"
        }
        desired_order = {
            "파워소스": 1,
            "타입": 2,
            "와이어피더": 3,
            "토치": 4,
            "호스패키지": 5,
            "어스": 6,
            "트롤리": 7,
            "쿨러": 8,
            "쿨란트": 9,
            "가스호스": 10,
            "ELECTRODE HOLDER": 11
        }
        name = row["category_name"]
        final_name = alias_map.get(name, name)
        final_number = desired_order.get(final_name, row["category_number"])
        return {
            "id": row["id"],
            "category_name": final_name,
            "category_number": final_number
        }
    
    def get_all_categories(self):
        """모든 카테고리 조회"""
        cursor = self.connection.cursor()
        cursor.execute('''
            SELECT id, category_name, category_number 
            FROM option_categories 
            ORDER BY category_number
        ''')
        rows = cursor.fetchall()
        normalized = [self._normalize_category_row(row) for row in rows]
        normalized.sort(key=lambda c: c["category_number"])
        return normalized
    
    def get_category_options(self, category_id):
        """특정 카테고리의 옵션들 조회"""
        cursor = self.connection.cursor()
        cursor.execute('''
            SELECT id, item_name, item_code, description 
            FROM option_items 
            WHERE category_id = ? 
            ORDER BY id
        ''', (category_id,))
        return [dict(row) for row in cursor.fetchall()]
    
    # === 로직 제한조건 관리 ===
    
    def _row_to_logic_rule(self, row):
        """logic_rules 행을 애플리케이션에서 사용 가능한 딕셔너리로 변환"""
        raw_actions = row["actions_json"] if row["actions_json"] is not None else "[]"
        try:
            actions = json.loads(raw_actions)
        except (json.JSONDecodeError, TypeError, ValueError):
            actions = []
        
        return {
            "id": row["id"],
            "orderIndex": row["order_index"],
            "powerSourceCode": row["power_source_code"],
            "powerSourceValue": row["power_source_value"],
            "typeCode": row["type_code"],
            "typeValue": row["type_value"],
            "actions": actions
        }
    
    def get_logic_rule(self, rule_id):
        cursor = self.connection.cursor()
        cursor.execute('''
            SELECT id, order_index, power_source_code, power_source_value, type_code, type_value, actions_json
            FROM logic_rules
            WHERE id = ?
        ''', (rule_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_logic_rule(row)
    
    def get_logic_rules(self):
        cursor = self.connection.cursor()
        cursor.execute('''
            SELECT id, order_index, power_source_code, power_source_value, type_code, type_value, actions_json
            FROM logic_rules
            ORDER BY order_index ASC, id ASC
        ''')
        rows = cursor.fetchall()
        return [self._row_to_logic_rule(row) for row in rows]
    
    def create_logic_rule(self, data):
        actions = data.get("actions", [])
        try:
            actions_json = json.dumps(actions, ensure_ascii=False)
        except (TypeError, ValueError):
            raise ValueError("actions 필드는 JSON으로 직렬화 가능한 리스트여야 합니다.")
        
        order_index = data.get("orderIndex")
        cursor = self.connection.cursor()
        if order_index is None:
            cursor.execute('SELECT COALESCE(MAX(order_index), -1) + 1 FROM logic_rules')
            order_index = cursor.fetchone()[0] or 0
        
        with self.connection:
            cursor.execute('''
                INSERT INTO logic_rules (
                    order_index, power_source_code, power_source_value, type_code, type_value, actions_json
                ) VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                int(order_index),
                data.get("powerSourceCode"),
                data.get("powerSourceValue"),
                data.get("typeCode"),
                data.get("typeValue"),
                actions_json
            ))
            new_id = cursor.lastrowid
        return self.get_logic_rule(new_id)
    
    def update_logic_rule(self, rule_id, data):
        existing = self.get_logic_rule(rule_id)
        if not existing:
            return None
        
        actions = data.get("actions", existing["actions"])
        try:
            actions_json = json.dumps(actions, ensure_ascii=False)
        except (TypeError, ValueError):
            raise ValueError("actions 필드는 JSON으로 직렬화 가능한 리스트여야 합니다.")
        
        order_index = data.get("orderIndex", existing["orderIndex"])
        
        with self.connection:
            self.connection.execute('''
                UPDATE logic_rules
                SET order_index = ?, 
                    power_source_code = ?, 
                    power_source_value = ?, 
                    type_code = ?, 
                    type_value = ?, 
                    actions_json = ?
                WHERE id = ?
            ''', (
                int(order_index) if order_index is not None else existing["orderIndex"],
                data.get("powerSourceCode"),
                data.get("powerSourceValue"),
                data.get("typeCode"),
                data.get("typeValue"),
                actions_json,
                rule_id
            ))
        return self.get_logic_rule(rule_id)
    
    def delete_logic_rule(self, rule_id):
        with self.connection:   
            cursor = self.connection.execute('DELETE FROM logic_rules WHERE id = ?', (rule_id,))
        return cursor.rowcount > 0
    
    def generate_combination_code(self, selected_options):
        """선택된 옵션 조합으로 순차적 코드 생성 (50000001부터 시작)"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                
                # 마지막 생성된 코드 조회
                cursor.execute("SELECT combination_code FROM selected_combinations ORDER BY id DESC LIMIT 1")
                last_code = cursor.fetchone()
                
                if last_code:
                    # 마지막 코드에서 숫자 부분 추출하여 +1
                    last_code_str = last_code[0]
                    if last_code_str.startswith('50000'):
                        try:
                            # 50000 이후의 숫자 부분 추출``
                            number_part = last_code_str[5:]  # 50000 이후 부분
                            next_number = int(number_part) + 1
                            new_code = f"50000{next_number:03d}"
                        except ValueError:
                            # 숫자 변환 실패 시 50000001부터 시작
                            new_code = "50000001"
                    else:
                        # 50000으로 시작하지 않는 경우 50000001부터 시작
                        new_code = "50000001"
                else:
                    # 첫 번째 코드
                    new_code = "50000001"
                
                return new_code
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                # 락 문제 발생 시 재시도
                import time
                time.sleep(0.1)
                return self.generate_combination_code(selected_options)
            else:
                raise
    
    def save_combination(self, combination_code, selected_options, set_code=None, call_arc_code=None):
        """선택된 조합을 데이터베이스에 저장"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                
                # 중복 확인 순서:
                # 1. set_code로 먼저 확인 (세트코드 중복 체크)
                # 2. selected_options로 확인 (옵션 조합 중복 체크)
                # 3. combination_code로 확인 (코드 중복 체크)
                options_json = json.dumps(selected_options, ensure_ascii=False, sort_keys=True)
                
                # set_code로 중복 확인 (세트코드가 이미 존재하는지)
                if set_code:
                    cursor.execute("SELECT combination_code, set_code, call_arc_code FROM selected_combinations WHERE set_code = ?", (set_code,))
                    existing_by_set_code = cursor.fetchone()
                    
                    if existing_by_set_code:
                        return {
                            "is_new": False,
                            "is_duplicate_set_code": True,  # 세트코드 중복 플래그
                            "is_duplicate_options": False,
                            "existing_code": existing_by_set_code[0],
                            "set_code": existing_by_set_code[1],
                            "call_arc_code": existing_by_set_code[2]
                        }
                
                # selected_options로 중복 확인 (동일한 옵션 조합이 이미 저장되어 있는지)
                cursor.execute("SELECT combination_code, set_code, call_arc_code FROM selected_combinations WHERE selected_options = ?", (options_json,))
                existing_by_options = cursor.fetchone()
                
                if existing_by_options:
                    return {
                        "is_new": False,
                        "is_duplicate_set_code": False,
                        "is_duplicate_options": True,  # 옵션 중복 플래그
                        "existing_code": existing_by_options[0],
                        "set_code": existing_by_options[1],
                        "call_arc_code": existing_by_options[2]
                    }
                
                # combination_code로 확인 (같은 코드가 이미 있는지)
                cursor.execute("SELECT combination_code, set_code, call_arc_code FROM selected_combinations WHERE combination_code = ?", (combination_code,))
                existing_by_code = cursor.fetchone()
                
                if existing_by_code:
                    return {
                        "is_new": False,
                        "is_duplicate_set_code": False,
                        "is_duplicate_options": False,  # 코드 중복이지만 옵션은 다를 수 있음
                        "existing_code": existing_by_code[0],
                        "set_code": existing_by_code[1],
                        "call_arc_code": existing_by_code[2]
                    }
                
                # 새 조합 저장 (set_code, call_arc_code 포함)
                print(f"  - combination_code: '{combination_code}'")
                print(f"  - set_code: '{set_code}' (type: {type(set_code)})")
                print(f"  - call_arc_code: '{call_arc_code}' (type: {type(call_arc_code)})")
                
                cursor.execute('''
                    INSERT INTO selected_combinations (combination_code, set_code, call_arc_code, selected_options)
                    VALUES (?, ?, ?, ?)
                ''', (combination_code, set_code, call_arc_code, options_json))
                
                return {
                    "is_new": True, 
                    "new_code": combination_code,
                    "set_code": set_code,
                    "call_arc_code": call_arc_code
                }
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                # 락 문제 발생 시 재시도
                import time
                time.sleep(0.1)
                return self.save_combination(combination_code, selected_options, set_code=set_code, call_arc_code=call_arc_code)
            else:
                print(f"[ERROR] DB 오류 발생: {e}")
                raise
        except sqlite3.IntegrityError as e:
            # UNIQUE constraint 오류 처리 - with 블록 밖이므로 새로운 cursor 필요
            print(f"[ERROR] 무결성 제약 조건 오류: {e}")
            if "UNIQUE constraint failed: selected_combinations.combination_code" in str(e):
                # combination_code 중복 - 다시 조회
                try:
                    with self.connection:
                        cursor = self.connection.cursor()
                        cursor.execute("SELECT combination_code, set_code, call_arc_code FROM selected_combinations WHERE combination_code = ?", (combination_code,))
                        existing = cursor.fetchone()
                        if existing:
                            return {
                                "is_new": False,
                                "is_duplicate_options": False,
                                "existing_code": existing[0],
                                "set_code": existing[1],
                                "call_arc_code": existing[2]
                            }
                except:
                    pass
            raise
    
    def lookup_combination(self, combination_code):
        """코드로 조합 조회 (부분 검색 지원)"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                
                
                # 먼저 정확한 코드로 검색
                cursor.execute('''
                    SELECT combination_code, set_code, call_arc_code, selected_options, created_at 
                    FROM selected_combinations 
                    WHERE combination_code = ?
                ''', (combination_code,))
                
                result = cursor.fetchone()
                
                # 정확한 코드가 없으면 세트코드로 검색 (세트코드가 모든 것을 관리)
                if not result:
                    cursor.execute('''
                        SELECT combination_code, set_code, call_arc_code, selected_options, created_at 
                        FROM selected_combinations 
                        WHERE set_code = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                    ''', (combination_code,))
                    
                    result = cursor.fetchone()
                
                if result:
                    actual_code = result[0] if result[0] is not None else ""  # 실제 매칭된 코드
                    set_code_value = result[1] if result[1] is not None else ""  # 세트코드 (None 체크)
                    call_arc_code_value = result[2] if result[2] is not None else ""  # 콜아크코드 (None 체크)
                    selected_options = json.loads(result[3]) if result[3] is not None else {}  # JSON 문자열 파싱
                    created_at = result[4] if result[4] is not None else ""  # 생성일시
                    
                    # 각 카테고리의 상세 정보 조회
                    detailed_options = {}
                    for category_number, option_name in selected_options.items():
                        # category_number를 정수로 변환
                        cat_num = int(category_number) if isinstance(category_number, str) else category_number
                        
                        # 해당 옵션의 상세 정보 조회 (코드, 설명)
                        cursor.execute('''
                            SELECT oc.category_name, oi.item_code, oi.description
                            FROM option_items oi
                            JOIN option_categories oc ON oi.category_id = oc.id
                            WHERE oi.item_name = ? AND oc.category_number = ?
                        ''', (option_name, cat_num))
                        
                        option_detail = cursor.fetchone()
                        if option_detail:
                            category_name, item_code, description = option_detail
                            detailed_options[str(cat_num)] = {
                                "category_name": category_name,
                                "option_name": option_name,
                                "item_code": item_code if item_code else "-",
                                "description": description if description else "-"
                            }
                        else:
                            detailed_options[str(cat_num)] = {
                                "category_name": f"카테고리 {cat_num}",
                                "option_name": option_name,
                                "item_code": "-",
                                "description": "-"
                            }
                    
                    # 반환값 준비 (None 또는 빈 문자열 처리)
                    final_set_code = set_code_value if (set_code_value is not None and set_code_value != "") else ""
                    final_call_arc_code = call_arc_code_value if (call_arc_code_value is not None and call_arc_code_value != "") else ""
                    
                    return {
                        "found": True,
                        "code": actual_code,  # 실제 매칭된 코드 반환
                        "set_code": final_set_code,  # 세트코드 (None 처리)
                        "call_arc_code": final_call_arc_code,  # 콜아크코드 (None 처리)
                        "options": detailed_options,
                        "created_at": created_at
                    }
                return {"found": False}
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                # 락 문제 발생 시 재시도
                import time
                time.sleep(0.1)
                return self.lookup_combination(combination_code)
            else:
                raise

    def find_combination_by_options(self, selected_options, allow_partial=False):
        """선택된 옵션 조합으로 저장된 코드 조회

        allow_partial=True 인 경우 선택된 옵션이 저장된 조합의 부분집합이어도 매칭합니다.
        """
        if not selected_options:
            return None
        
        try:
            with self.connection:
                cursor = self.connection.cursor()
                # 1차: 동일한 JSON 문자열로 빠르게 조회 (신규 저장분 대응)
                options_json = json.dumps(selected_options, ensure_ascii=False, sort_keys=True)
                cursor.execute('''
                    SELECT combination_code, set_code, call_arc_code, selected_options, created_at
                    FROM selected_combinations
                    WHERE selected_options = ?
                ''', (options_json,))
                row = cursor.fetchone()
                stored_json_for_row = row[3] if row else None

                # 2차: 이전 버전에서 저장된 JSON 순서 차이를 보정하기 위해 전체 스캔
                if not row:
                    target_options = {str(k): v for k, v in selected_options.items()}
                    cursor.execute('''
                        SELECT combination_code, set_code, call_arc_code, selected_options, created_at
                        FROM selected_combinations
                    ''')
                    candidates = cursor.fetchall()
                    for candidate in candidates:
                        stored_json = candidate[3]
                        if not stored_json:
                            continue
                        try:
                            stored_options = json.loads(stored_json)
                        except json.JSONDecodeError:
                            continue
                        normalized_stored = {str(k): v for k, v in stored_options.items()}
                        if normalized_stored == target_options:
                            row = candidate
                            stored_json_for_row = stored_json
                            break

                # 3차: 부분 일치 허용 (옵션 부분집합 매칭)
                if (not row) and allow_partial:
                    target_options = {str(k): v for k, v in selected_options.items()}
                    cursor.execute('''
                        SELECT combination_code, set_code, call_arc_code, selected_options, created_at
                        FROM selected_combinations
                    ''')
                    candidates = cursor.fetchall()
                    best_candidate = None
                    best_candidate_json = None
                    best_match_length = -1
                    for candidate in candidates:
                        stored_json = candidate[3]
                        if not stored_json:
                            continue
                        try:
                            stored_options = json.loads(stored_json)
                        except json.JSONDecodeError:
                            continue
                        normalized_stored = {str(k): v for k, v in stored_options.items()}
                        # 부분집합 여부 확인
                        if all(normalized_stored.get(str(k)) == v for k, v in target_options.items()):
                            current_length = len(normalized_stored)
                            if current_length > best_match_length:
                                best_candidate = candidate
                                best_candidate_json = stored_json
                                best_match_length = current_length
                    if best_candidate:
                        row = best_candidate
                        stored_json_for_row = best_candidate_json

                if not row:
                    return None
                
                stored_options = json.loads(stored_json_for_row) if stored_json_for_row else {}
                return {
                    "combination_code": row[0],
                    "set_code": row[1] or "",
                    "call_arc_code": row[2] or "",
                    "selected_options": stored_options,
                    "created_at": row[4]
                }
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                import time
                time.sleep(0.1)
                return self.find_combination_by_options(selected_options, allow_partial=allow_partial)
            raise
    
    def get_all_combinations(self):
        """모든 저장된 조합 조회"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                cursor.execute('''
                    SELECT combination_code, set_code, call_arc_code, selected_options, created_at 
                    FROM selected_combinations 
                    ORDER BY 
                        CASE 
                            WHEN set_code IS NOT NULL AND set_code != '' THEN
                                CAST(SUBSTR(set_code, 3) AS INTEGER)
                            ELSE
                                CAST(combination_code AS INTEGER)
                        END ASC
                ''')
                
                results = cursor.fetchall()
                combinations = []
                
                for row in results:
                    selected_options = json.loads(row[3])
                    
                    # 각 카테고리의 상세 정보 조회
                    detailed_options = {}
                    for category_number, option_name in selected_options.items():
                        # category_number를 정수로 변환
                        cat_num = int(category_number) if isinstance(category_number, str) else category_number
                        
                        # 해당 옵션의 상세 정보 조회 (코드, 설명)
                        cursor.execute('''
                            SELECT oc.category_name, oi.item_code, oi.description
                            FROM option_items oi
                            JOIN option_categories oc ON oi.category_id = oc.id
                            WHERE oi.item_name = ? AND oc.category_number = ?
                        ''', (option_name, cat_num))
                        
                        option_detail = cursor.fetchone()
                        if option_detail:
                            category_name, item_code, description = option_detail
                            detailed_options[str(cat_num)] = {
                                "category_name": category_name,
                                "option_name": option_name,
                                "item_code": item_code if item_code else "-",
                                "description": description if description else "-"
                            }
                        else:
                            detailed_options[str(cat_num)] = {
                                "category_name": f"카테고리 {cat_num}",
                                "option_name": option_name,
                                "item_code": "-",
                                "description": "-"
                            }
                    
                    combinations.append({
                        "code": row[0],
                        "set_code": row[1],
                        "call_arc_code": row[2],
                        "options": detailed_options,
                        "created_at": row[4]
                    })
                
                return {
                    "success": True,
                    "total_count": len(combinations),
                    "combinations": combinations
                }
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                # 락 문제 발생 시 재시도
                import time
                time.sleep(0.1)
                return self.get_all_combinations()
            else:
                return {
                    "success": False,
                    "error": str(e),
                    "total_count": 0,
                    "combinations": []
                }

    def delete_combination(self, combination_code):
        """특정 조합 삭제"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                
                # 먼저 해당 코드가 존재하는지 확인
                cursor.execute('''
                    SELECT COUNT(*) FROM selected_combinations 
                    WHERE combination_code = ?
                ''', (combination_code,))
                
                if cursor.fetchone()[0] == 0:
                    return {
                        "success": False,
                        "error": f"코드 '{combination_code}'를 찾을 수 없습니다."
                    }
                
                # 코드 삭제
                cursor.execute('''
                    DELETE FROM selected_combinations 
                    WHERE combination_code = ?
                ''', (combination_code,))
                
                return {
                    "success": True,
                    "message": f"코드 '{combination_code}'가 성공적으로 삭제되었습니다."
                }
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                # 락 문제 발생 시 재시도
                import time
                time.sleep(0.1)
                return self.delete_combination(combination_code)
            else:
                return {
                    "success": False,
                    "error": str(e)
                }

    def get_table_info(self, table_name):
        """테이블 정보 조회"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                cursor.execute(f"PRAGMA table_info({table_name})")
                columns = cursor.fetchall()
                
                column_info = []
                for col in columns:
                    column_info.append({
                        "id": col[0],
                        "name": col[1],
                        "type": col[2],
                        "notnull": col[3],
                        "default": col[4],
                        "pk": col[5]
                    })
                
                return {
                    "success": True,
                    "columns": column_info
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def add_column(self, table_name, column_name, column_type="TEXT"):
        """테이블에 새로운 칼럼 추가"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                
                # 칼럼 이름 검증
                if not column_name.isidentifier():
                    return {
                        "success": False,
                        "error": "유효하지 않은 칼럼 이름입니다."
                    }
                
                # 칼럼 타입 검증
                valid_types = ["TEXT", "INTEGER", "REAL", "BLOB", "NULL"]
                if column_type.upper() not in valid_types:
                    return {
                        "success": False,
                        "error": "유효하지 않은 칼럼 타입입니다."
                    }
                
                # 칼럼 추가
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                
                return {
                    "success": True,
                    "message": f"칼럼 '{column_name}'이 성공적으로 추가되었습니다."
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def delete_column_data(self, table_name, column_name):
        """칼럼 삭제 (SQLite는 칼럼 삭제를 직접 지원하지 않음)"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                
                # SQLite는 ALTER TABLE DROP COLUMN을 지원하지 않으므로
                # 테이블을 다시 생성하는 방식 사용
                
                # 기존 칼럼 정보 조회
                cursor.execute(f"PRAGMA table_info({table_name})")
                columns = cursor.fetchall()
                
                # 삭제할 칼럼을 제외한 칼럼 목록
                remaining_columns = [col for col in columns if col[1] != column_name]
                
                if len(remaining_columns) == 0:
                    return {
                        "success": False,
                        "error": "최소한 하나의 칼럼은 남겨야 합니다."
                    }
                
                # 칼럼 정의 문자열
                column_defs = []
                for col in remaining_columns:
                    col_def = f"{col[1]} {col[2]}"
                    if col[5] == 1:  # Primary Key
                        col_def += " PRIMARY KEY"
                    column_defs.append(col_def)
                
                # 새 테이블 생성
                new_table_name = f"{table_name}_new"
                create_sql = f"CREATE TABLE {new_table_name} ({', '.join(column_defs)})"
                cursor.execute(create_sql)
                
                # 데이터 복사
                remaining_col_names = [col[1] for col in remaining_columns]
                copy_sql = f"INSERT INTO {new_table_name} ({', '.join(remaining_col_names)}) SELECT {', '.join(remaining_col_names)} FROM {table_name}"
                cursor.execute(copy_sql)
                
                # 기존 테이블 삭제
                cursor.execute(f"DROP TABLE {table_name}")
                
                # 새 테이블 이름 변경
                cursor.execute(f"ALTER TABLE {new_table_name} RENAME TO {table_name}")
                
                return {
                    "success": True,
                    "message": f"칼럼 '{column_name}'이 성공적으로 삭제되었습니다."
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def add_option_item(self, category_id, item_name, item_code=None, description=None):
        """옵션 항목 추가"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                
                # 카테고리 존재 확인
                cursor.execute("SELECT id FROM option_categories WHERE id = ?", (category_id,))
                if not cursor.fetchone():
                    return {
                        "success": False,
                        "error": f"카테고리 ID {category_id}를 찾을 수 없습니다."
                    }
                
                # 중복 항목 확인
                cursor.execute("""
                    SELECT COUNT(*) FROM option_items 
                    WHERE category_id = ? AND item_name = ?
                """, (category_id, item_name))
                
                if cursor.fetchone()[0] > 0:
                    return {
                        "success": False,
                        "error": f"'{item_name}' 항목이 이미 존재합니다."
                    }
                
                # 항목 추가
                cursor.execute("""
                    INSERT INTO option_items (category_id, item_name, item_code, description)
                    VALUES (?, ?, ?, ?)
                """, (category_id, item_name, item_code, description))
                
                return {
                    "success": True,
                    "message": f"'{item_name}' 항목이 성공적으로 추가되었습니다.",
                    "item_id": cursor.lastrowid
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def update_option_item(self, item_id, **fields):
        """옵션 항목 수정"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                
                # 기존 항목 확인
                cursor.execute("SELECT * FROM option_items WHERE id = ?", (item_id,))
                existing_item = cursor.fetchone()
                
                if not existing_item:
                    return {
                        "success": False,
                        "error": f"항목 ID {item_id}를 찾을 수 없습니다."
                    }
                
                allowed_fields = {"item_name", "item_code", "description"}
                update_fields = []
                update_values = []
                
                for field in allowed_fields:
                    if field in fields:
                        update_fields.append(f"{field} = ?")
                        update_values.append(fields[field])
                
                if not update_fields:
                    return {
                        "success": False,
                        "error": "수정할 필드가 없습니다."
                    }
                
                update_values.append(item_id)
                
                # 항목 수정
                cursor.execute(f"""
                    UPDATE option_items 
                    SET {', '.join(update_fields)}
                    WHERE id = ?
                """, update_values)
                
                return {
                    "success": True,
                    "message": f"항목 ID {item_id}가 성공적으로 수정되었습니다."
                }
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                import time
                time.sleep(0.1)
                return self.update_option_item(item_id, **fields)
            return {
                "success": False,
                "error": str(e)
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def delete_option_item(self, item_id):
        """옵션 항목 삭제"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                
                # 기존 항목 확인
                cursor.execute("SELECT item_name FROM option_items WHERE id = ?", (item_id,))
                existing_item = cursor.fetchone()
                
                if not existing_item:
                    return {
                        "success": False,
                        "error": f"항목 ID {item_id}를 찾을 수 없습니다."
                    }
                
                # 항목 삭제
                cursor.execute("DELETE FROM option_items WHERE id = ?", (item_id,))
                
                return {
                    "success": True,
                    "message": f"'{existing_item[0]}' 항목이 성공적으로 삭제되었습니다."
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def export_to_excel(self, combination_code, selected_options):
        """선택된 옵션을 Excel 파일로 내보내기"""
        try:
            # xlsx 폴더가 없으면 생성
            if not os.path.exists('xlsx'):
                os.makedirs('xlsx')
            
            filename = os.path.join('xlsx', f"welding_options_{combination_code}.xlsx")
            
            # Excel 워크북 생성
            wb = Workbook()
            ws = wb.active
            ws.title = "웰딩 장비 옵션"
            
            # 헤더 스타일 설정
            header_font = Font(bold=True, color="FFFFFF")
            header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
            header_alignment = Alignment(horizontal="center", vertical="center")
        
            # 헤더 작성
            ws['A1'] = '카테고리'
            ws['B1'] = '선택된 옵션'
            ws['C1'] = '코드'
            ws['D1'] = '생성일시'
            
            # 헤더 스타일 적용
            for cell in ws[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
            
            # 데이터 작성
            categories = self.get_all_categories()
            row = 2
        
            for category in categories:
                category_number = category["category_number"]
                option_name = selected_options.get(category_number, "선택없음")
                
                # 카테고리 정보
                ws[f'A{row}'] = f"[{category_number}] {category['category_name']}"
                ws[f'B{row}'] = option_name
                
                # 선택된 옵션의 코드 찾기
                if option_name != "선택없음":
                    options = self.get_category_options(category['id'])
                    for option in options:
                        if option['item_name'] == option_name:
                            ws[f'C{row}'] = option['item_code'] or ""
                            break
                    else:
                        ws[f'C{row}'] = ""
                else:
                    ws[f'C{row}'] = ""
                
                # 생성일시
                ws[f'D{row}'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                row += 1
            
            # 열 너비 자동 조정
            for column in ws.columns:
                max_length = 0
                column_letter = column[0].column_letter
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = min(max_length + 2, 50)
                ws.column_dimensions[column_letter].width = adjusted_width
            
            # 파일 저장
            wb.save(filename)
            return filename
        
        except Exception as e:
            print(f"Excel 파일 생성 오류: {e}")
            # 기본 파일명으로 재시도
            try:
                fallback_filename = os.path.join('xlsx', f"welding_options_{combination_code}.xlsx")
                wb = Workbook()
                ws = wb.active
                ws.title = "웰딩 장비 옵션"
                
                # 기본 헤더
                ws['A1'] = '카테고리'
                ws['B1'] = '선택된 옵션'
                ws['C1'] = '코드'
                ws['D1'] = '생성일시'
                
                # 기본 데이터
                ws['A2'] = f"코드: {combination_code}"
                ws['B2'] = "옵션 정보"
                ws['C2'] = combination_code
                ws['D2'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                wb.save(fallback_filename)
                return fallback_filename
            except Exception as fallback_error:
                print(f"Fallback Excel 파일 생성도 실패: {fallback_error}")
                return None
    
    def get_categories(self):
        """카테고리 목록 조회"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                cursor.execute("SELECT id, category_name, category_number FROM option_categories ORDER BY category_number")
                rows = cursor.fetchall()
                normalized = [self._normalize_category_row(row) for row in rows]
                normalized.sort(key=lambda c: c["category_number"])
                return [{"id": cat["id"], "name": cat["category_name"], "number": cat["category_number"]} for cat in normalized]
        except Exception as e:
            print(f"카테고리 조회 오류: {e}")
            return []
    
    def get_items_by_category(self, category_id):
        """특정 카테고리의 옵션 항목 조회"""
        try:
            with self.connection:
                cursor = self.connection.cursor()
                cursor.execute("""
                    SELECT id, item_name, item_code, description 
                    FROM option_items 
                    WHERE category_id = ? 
                    ORDER BY item_name
                """, (category_id,))
                items = cursor.fetchall()
                
                return [{"id": item[0], "name": item[1], "code": item[2], "description": item[3]} for item in items]
        except Exception as e:
            print(f"항목 조회 오류: {e}")
            return []

def main():
    """메인 함수 - 콘솔 버전"""
    system = WeldingOptionSystem()
    
    while True:
        print("\n" + "="*60)
        print("웰딩 장비 옵션 선택 시스템 (PNS)")
        print("="*60)
        print("1. 옵션 선택 및 코드 생성")
        print("2. 코드로 옵션 조회")
        print("3. 종료")
        print("="*60)
        
        choice = input("선택하세요 (1-3): ").strip()
        
        if choice == "1":
            select_options_and_generate_code(system)
        elif choice == "2":
            lookup_options_by_code(system)
        elif choice == "3":
            print("시스템을 종료합니다.")
            break
        else:
            print("잘못된 선택입니다. 1-3 중에서 선택해주세요.")

def select_options_and_generate_code(system):
    """옵션 선택 및 코드 생성"""
    print("\n" + "="*50)
    print("옵션 선택")
    print("="*50)
    
    selected_options = {}
    categories = system.get_all_categories()
    
    for category in categories:
        print(f"\n[{category['category_number']}] {category['category_name']}")
        print("-" * 30)
        
        options = system.get_category_options(category['id'])
        print("0. 선택없음")
        
        for i, option in enumerate(options, 1):
            print(f"{i}. {option['item_name']} ({option['item_code']}) - {option['description']}")
        
        while True:
            try:
                choice = int(input(f"\n{category['category_name']} 선택 (0-{len(options)}): "))
                if 0 <= choice <= len(options):
                    if choice == 0:
                        selected_options[category['category_number']] = "선택없음"
                    else:
                        selected_options[category['category_number']] = options[choice-1]['item_name']
                    break
                else:
                    print(f"0부터 {len(options)} 사이의 숫자를 입력해주세요.")
            except ValueError:
                print("숫자를 입력해주세요.")
    
    # 코드 생성
    combination_code = system.generate_combination_code(selected_options)
    
    # 중복 확인 및 저장
    save_result = system.save_combination(combination_code, selected_options)
    
    if save_result["is_new"]:
        print(f"\n✅ 새로운 코드가 생성되었습니다: {save_result['new_code']}")
        filename = system.export_to_excel(save_result['new_code'], selected_options)
        print(f"📄 Excel 파일이 생성되었습니다: {filename}")
    else:
        print(f"\n⚠️  같은 옵션 조합이 이미 존재합니다.")
        print(f"기존 코드: {save_result['existing_code']}")
        filename = system.export_to_excel(save_result['existing_code'], selected_options)
        print(f"📄 Excel 파일이 생성되었습니다: {filename}")
    
    # 선택된 옵션 표시
    print("\n선택된 옵션:")
    print("-" * 30)
    for category in categories:
        category_number = category['category_number']
        option_name = selected_options.get(category_number, "선택없음")
        print(f"[{category_number}] {category['category_name']}: {option_name}")

def lookup_options_by_code(system):
    """코드로 옵션 조회"""
    print("\n" + "="*50)
    print("코드로 옵션 조회")
    print("="*50)
    
    combination_code = input("조회할 코드를 입력하세요: ").strip()
    
    if not combination_code:
        print("코드를 입력해주세요.")
        return
    
    result = system.lookup_combination(combination_code)
    
    if result["found"]:
        print(f"\n✅ 코드 '{combination_code}' 조회 성공!")
        print(f"생성일시: {result['created_at']}")
        print("\n선택된 옵션:")
        print("-" * 30)
        
        categories = system.get_all_categories()
        for category in categories:
            category_number = category['category_number']
            option_name = result['options'].get(category_number, "선택없음")
            print(f"[{category_number}] {category['category_name']}: {option_name}")
    else:
        print(f"\n❌ 코드 '{combination_code}'를 찾을 수 없습니다.")

if __name__ == "__main__":
    main()
