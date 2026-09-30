"""세트가격조회 성능 최적화 — 일괄 데이터 로드"""
import json
import threading
import time
from collections import defaultdict

_REFERENCE_CACHE = {'ts': 0.0, 'data': None}
REFERENCE_CACHE_TTL_SEC = 120
_INDEXES_ENSURED = False
_CACHE_BUILD_LOCK = threading.Lock()


def _row_val(row, key, idx=0, default=None):
    try:
        if hasattr(row, 'keys') and callable(getattr(row, 'keys', None)):
            if key in row.keys():
                return row[key]
            return row[idx] if len(row) > idx else default
        return row[idx] if len(row) > idx else default
    except (KeyError, IndexError, TypeError, AttributeError):
        return default


def ensure_set_price_indexes(cursor):
    global _INDEXES_ENSURED
    if _INDEXES_ENSURED:
        return
    indexes = [
        'CREATE INDEX IF NOT EXISTS idx_selected_combinations_set_code ON selected_combinations(set_code)',
        'CREATE INDEX IF NOT EXISTS idx_option_prices_set_code ON option_prices(set_code)',
        'CREATE INDEX IF NOT EXISTS idx_option_base_prices_cat_opt ON option_base_prices(category_number, option_name)',
        'CREATE INDEX IF NOT EXISTS idx_export_products_code ON export_quotation_new_products(product_code)',
        'CREATE INDEX IF NOT EXISTS idx_export_products_name ON export_quotation_new_products(product_name)',
        'CREATE INDEX IF NOT EXISTS idx_option_items_cat_name ON option_items(category_id, item_name)',
    ]
    for sql in indexes:
        try:
            cursor.execute(sql)
        except Exception:
            pass
    cursor.connection.commit()
    _INDEXES_ENSURED = True


def invalidate_set_price_cache():
    _REFERENCE_CACHE['data'] = None
    _REFERENCE_CACHE['ts'] = 0.0


def get_set_price_reference_data(system, cursor, *, force_refresh=False):
    now = time.time()
    if (
        not force_refresh
        and _REFERENCE_CACHE['data'] is not None
        and now - _REFERENCE_CACHE['ts'] < REFERENCE_CACHE_TTL_SEC
    ):
        return _REFERENCE_CACHE['data']

    with _CACHE_BUILD_LOCK:
        now = time.time()
        if (
            not force_refresh
            and _REFERENCE_CACHE['data'] is not None
            and now - _REFERENCE_CACHE['ts'] < REFERENCE_CACHE_TTL_SEC
        ):
            return _REFERENCE_CACHE['data']

        ensure_set_price_indexes(cursor)

        cursor.execute('''
            SELECT set_code, selected_options, price, MIN(created_at) AS created_at
            FROM selected_combinations
            WHERE set_code IS NOT NULL AND set_code != ''
            GROUP BY set_code
            ORDER BY set_code
        ''')
        set_entries = {}
        for row in cursor.fetchall():
            set_code = _row_val(row, 'set_code', 0)
            if not set_code:
                continue
            selected_raw = _row_val(row, 'selected_options', 1, '{}')
            try:
                selected_options = json.loads(selected_raw) if selected_raw else {}
            except json.JSONDecodeError:
                selected_options = {}
            set_entries[set_code] = {
                'set_code': set_code,
                'selected_options': selected_options,
                'stored_price': _row_val(row, 'price', 2),
                'created_at': _row_val(row, 'created_at', 3),
            }

        option_prices_by_set = defaultdict(dict)
        cursor.execute('''
            SELECT set_code, category_number, option_name, price
            FROM option_prices
            WHERE set_code IS NOT NULL
        ''')
        for row in cursor.fetchall():
            set_code = _row_val(row, 'set_code', 0)
            cat_num = _row_val(row, 'category_number', 1)
            opt_name = _row_val(row, 'option_name', 2)
            price = _row_val(row, 'price', 3)
            if set_code and cat_num is not None and opt_name is not None:
                option_prices_by_set[set_code][f'{cat_num}_{opt_name}'] = price

        base_price_map = {}
        try:
            cursor.execute('SELECT category_number, option_name, price FROM option_base_prices')
            for row in cursor.fetchall():
                cat_num = _row_val(row, 'category_number', 0)
                opt_name = _row_val(row, 'option_name', 1)
                price = _row_val(row, 'price', 2)
                if cat_num is not None and opt_name is not None:
                    base_price_map[f'{cat_num}_{opt_name}'] = price
        except Exception:
            pass

        item_code_map = {}
        cursor.execute('''
            SELECT oc.category_number, oi.item_name, oi.item_code
            FROM option_items oi
            JOIN option_categories oc ON oi.category_id = oc.id
        ''')
        for row in cursor.fetchall():
            cat_num = _row_val(row, 'category_number', 0)
            item_name = _row_val(row, 'item_name', 1)
            item_code = _row_val(row, 'item_code', 2)
            if cat_num is not None and item_name:
                item_code_map[(int(cat_num), item_name)] = item_code or item_name

        products_by_code = {}
        products_by_name = {}
        try:
            cursor.execute('''
                SELECT product_code, product_name,
                       COALESCE(price_book_price, unit_price, 0) AS price_book_price,
                       COALESCE(purchase_price, 0) AS purchase_price,
                       updated_at
                FROM export_quotation_new_products
                ORDER BY updated_at DESC
            ''')
            for row in cursor.fetchall():
                code = (_row_val(row, 'product_code', 0) or '').strip()
                name = (_row_val(row, 'product_name', 1) or '').strip()
                info = {
                    'product_name': name,
                    'price_book_price': _row_val(row, 'price_book_price', 2),
                    'purchase_price': _row_val(row, 'purchase_price', 3),
                }
                if code and code not in products_by_code:
                    products_by_code[code] = info
                if name and name not in products_by_name:
                    products_by_name[name] = info
        except Exception:
            pass

        try:
            categories = system.get_all_categories()
            category_map = {cat['category_number']: cat['category_name'] for cat in categories}
        except Exception:
            category_map = {}

        data = {
            'set_entries': set_entries,
            'option_prices_by_set': dict(option_prices_by_set),
            'base_price_map': base_price_map,
            'item_code_map': item_code_map,
            'products_by_code': products_by_code,
            'products_by_name': products_by_name,
            'category_map': category_map,
            'registered_product_codes': set(products_by_code.keys()),
        }
        _REFERENCE_CACHE['data'] = data
        _REFERENCE_CACHE['ts'] = now
        return data


def _resolve_option_detail(cat_num, option_name, set_code, ref):
    key = f'{cat_num}_{option_name}'
    set_price = ref['option_prices_by_set'].get(set_code, {}).get(key)
    base_price = ref['base_price_map'].get(key)
    final_price = base_price if base_price is not None else set_price

    item_code = ref['item_code_map'].get((cat_num, option_name), option_name)
    updated_option_name = option_name
    price_book_price = None
    purchase_price = None

    product_code_to_check = None
    if isinstance(item_code, str) and item_code.startswith('P'):
        product_code_to_check = item_code
    elif isinstance(option_name, str) and option_name.startswith('P'):
        product_code_to_check = option_name

    product_info = None
    if product_code_to_check:
        product_info = ref['products_by_code'].get(product_code_to_check)
    if not product_info:
        product_info = ref['products_by_name'].get(option_name)

    if product_info:
        if product_info.get('product_name'):
            updated_option_name = product_info['product_name']
        pb = product_info.get('price_book_price')
        if pb is not None:
            try:
                price_book_price = float(pb)
                if price_book_price > 0:
                    final_price = price_book_price
            except (ValueError, TypeError):
                price_book_price = None
        pp = product_info.get('purchase_price')
        if pp is not None:
            try:
                purchase_price = float(pp)
            except (ValueError, TypeError):
                purchase_price = None

    return {
        'category_number': cat_num,
        'category_name': ref['category_map'].get(cat_num, f'카테고리 {cat_num}'),
        'option_name': updated_option_name,
        'item_code': item_code or option_name,
        'price': final_price,
        'base_price': base_price,
        'price_book_price': price_book_price,
        'purchase_price': purchase_price,
    }


def build_set_code_options(set_code, ref):
    entry = ref['set_entries'].get(set_code)
    if not entry:
        return None
    options_list = []
    for category_num, option_name in (entry.get('selected_options') or {}).items():
        try:
            cat_num = int(category_num)
        except (ValueError, TypeError):
            continue
        options_list.append(_resolve_option_detail(cat_num, option_name, set_code, ref))
    options_list.sort(key=lambda x: x['category_number'])
    return options_list


def _calc_list_price(set_code, ref):
    entry = ref['set_entries'][set_code]
    option_prices = ref['option_prices_by_set'].get(set_code, {})
    if option_prices:
        total = 0.0
        has_price = False
        for val in option_prices.values():
            if val is not None:
                try:
                    total += float(val)
                    has_price = True
                except (ValueError, TypeError):
                    continue
        if has_price:
            return total

    selected_options = entry.get('selected_options') or {}
    if not selected_options:
        stored = entry.get('stored_price')
        return float(stored) if stored is not None else None

    total = 0.0
    has_price = False
    for category_num, option_name in selected_options.items():
        try:
            cat_num = int(category_num)
        except (ValueError, TypeError):
            continue
        key = f'{cat_num}_{option_name}'
        price_val = ref['base_price_map'].get(key)
        if price_val is not None:
            try:
                total += float(price_val)
                has_price = True
            except (ValueError, TypeError):
                continue
    if has_price:
        return total
    stored = entry.get('stored_price')
    return float(stored) if stored is not None else None


def build_price_list(ref):
    price_list = []
    for set_code, entry in ref['set_entries'].items():
        price_list.append({
            'set_code': set_code,
            'price': _calc_list_price(set_code, ref),
            'created_at': entry.get('created_at'),
        })
    return price_list


def _option_totals(options, registered_codes, discount_rate):
    total_original = 0.0
    total_discounted = 0.0
    total_purchase = 0.0
    for option in options:
        sap_code = (option.get('item_code') or option.get('option_name') or '').strip().upper()
        if sap_code not in registered_codes:
            continue

        original_price = None
        pb = option.get('price_book_price')
        if pb is not None:
            try:
                if float(pb) > 0:
                    original_price = float(pb)
            except (ValueError, TypeError):
                pass
        if original_price is None and option.get('price') is not None:
            try:
                original_price = float(option['price'])
            except (ValueError, TypeError):
                original_price = None

        if original_price is not None:
            total_original += original_price
            total_discounted += original_price * (1 - discount_rate / 100.0)

        pp = option.get('purchase_price')
        if pp is not None:
            try:
                pp_val = float(pp)
                if pp_val > 0:
                    total_purchase += pp_val
            except (ValueError, TypeError):
                pass

    return {
        'original_total': round(total_original, 2),
        'discounted_total': round(total_discounted, 2),
        'purchase_total': round(total_purchase, 2),
    }


def build_all_summaries(ref, discount_rate=0.0):
    registered = {code.strip().upper() for code in ref['registered_product_codes'] if code}
    summaries = {}
    for set_code in ref['set_entries']:
        options = build_set_code_options(set_code, ref) or []
        totals = _option_totals(options, registered, discount_rate)
        if totals['original_total'] > 0:
            summaries[set_code] = totals
    return summaries
