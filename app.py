#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
웰딩 장비 옵션 선택 시스템 (PNS) - 웹 버전
"""

from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, Response
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
import threading
import os
import sys
import io
import re
import sqlite3
import time
from datetime import datetime
from pns import WeldingOptionSystem
from user_tracking import (
    authenticate_tracking_user,
    create_tracking_session,
    update_session_activity,
    log_page_view,
    log_quote_download,
    get_dashboard_data,
    ensure_user_tracking_tables,
    USER_TYPES,
    ROLE_PERMISSIONS,
    user_has_permission,
    can_view_export_cost,
)
from export_label_generator import build_labels_from_template, generate_label_docx
from export_label_service import (
    active_template_path,
    activate_upload,
    delete_upload,
    ensure_export_label_tables,
    export_label_base_dir,
    get_upload,
    get_upload_row,
    list_uploads,
    migrate_existing_template,
    register_upload,
    resolve_template_filepath,
    sanitize_upload_filename,
    EXPORT_LABEL_BASENAME,
    EXPORT_LABEL_ALLOWED_EXT,
)
from pricebook_page_service import (
    delete_page as delete_pricebook_page,
    ensure_pricebook_page_tables,
    get_global_latest as get_pricebook_global_latest,
    get_page_record as get_pricebook_page_record,
    list_accessory_images as list_pricebook_accessory_images,
    list_content_images as list_pricebook_content_images,
    list_pages as list_pricebook_pages,
    list_power_source_folders as list_pricebook_power_source_folders,
    save_uploaded_page as save_pricebook_page,
)
from pricebook_pdf_service import build_pricebook_pdf
from db_backup_service import (
    create_backup as create_db_backup,
    delete_backup as delete_db_backup,
    get_status as get_db_backup_status,
    resolve_backup_path,
    restore_backup as restore_db_backup,
    set_after_restore_hook,
    set_db_path_resolver,
    start_backup_scheduler,
    update_config as update_db_backup_config,
)
from option_view_image_service import (
    attach_regions_to_images,
    delete_image as delete_option_view_image,
    ensure_option_view_image_tables,
    get_image_record as get_option_view_image_record,
    list_images_by_option,
    list_option_categories,
    list_power_source_folders,
    list_regions_by_image,
    save_regions_for_image,
    save_uploaded_images,
    update_image_type_code as update_option_view_image_type_code,
)
from set_price_service import (
    build_all_summaries,
    build_price_list,
    build_set_code_options,
    get_set_price_reference_data,
    invalidate_set_price_cache,
)
from exchange_rates import (
    fetch_hana_exchange_rates,
    get_cached_rates,
    save_rates,
    ensure_exchange_rate_table,
    _today_kst,
)

# 한글 인코딩 설정
if sys.platform.startswith('win'):
    import locale
    locale.setlocale(locale.LC_ALL, 'ko_KR.UTF-8')

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False  # JSON 응답에서 한글 지원
app.secret_key = 'welding_system_secret_key_2025'  # 세션을 위한 비밀키
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB 파일 크기 제한
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads', 'part_images')
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def _session_user_type():
    return session.get('tracking_user_type')


def _session_can_view_export_cost():
    return can_view_export_cost(_session_user_type())


def _filter_export_dashboard_for_role(products, statistics, hide_cost):
    """고객(customer) 역할 — API 응답에서 원가/마진 필드 제거"""
    if not hide_cost:
        return products, statistics
    safe_products = []
    for product in products:
        safe = dict(product)
        safe['purchasePrice'] = 0
        safe.pop('margin', None)
        safe_products.append(safe)
    safe_stats = {
        'totalProducts': statistics.get('totalProducts', 0),
        'totalPriceBook': statistics.get('totalPriceBook', 0),
    }
    return safe_products, safe_stats


# CORS 헤더 추가 함수
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# OPTIONS 요청 처리
@app.route('/api/delete_code', methods=['OPTIONS'])
def handle_delete_code_options():
    return '', 200

# 파일 크기 제한 초과 에러 핸들러
@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    return jsonify({
        "success": False,
        "error": "파일 크기가 너무 큽니다. 최대 50MB까지 업로드 가능합니다."
    }), 413

# 스레드 로컬 스토리지를 위한 락
thread_local = threading.local()
thread_local_systems = {}
system_lock = threading.Lock()
db_write_lock = threading.Lock()

def run_db_with_retry(operation, max_retries=5):
    """SQLite database locked 오류 시 재시도"""
    last_error = None
    for attempt in range(max_retries):
        try:
            with db_write_lock:
                return operation()
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() or 'busy' in str(e).lower():
                last_error = e
                time.sleep(0.05 * (2 ** attempt))
                continue
            raise
    raise last_error

def get_welding_system():
    """스레드별 WeldingOptionSystem 인스턴스 반환"""
    thread_id = threading.get_ident()
    
    with system_lock:
        if thread_id not in thread_local_systems:
            thread_local_systems[thread_id] = WeldingOptionSystem()
        return thread_local_systems[thread_id]


@app.teardown_request
def _release_sqlite_transaction(exc):
    """요청 종료 시 미커밋 트랜잭션을 풀어 DB 잠금을 방지"""
    thread_id = threading.get_ident()
    with system_lock:
        system = thread_local_systems.get(thread_id)
    if not system:
        return
    try:
        if exc is None:
            system.connection.commit()
        else:
            system.connection.rollback()
    except Exception:
        try:
            system.connection.rollback()
        except Exception:
            pass


def _reset_welding_systems_after_restore():
    """DB 복원 후 열린 연결을 모두 닫고 캐시를 비운다."""
    with system_lock:
        for system in list(thread_local_systems.values()):
            try:
                system.connection.close()
            except Exception:
                pass
        thread_local_systems.clear()


def _resolve_live_db_path():
    preferred = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'welding_options_data.db')
    legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'welding_options.db')
    return preferred if os.path.exists(preferred) else legacy


set_db_path_resolver(_resolve_live_db_path)
set_after_restore_hook(_reset_welding_systems_after_restore)
start_backup_scheduler()


@app.route('/')
def index():
    """제품 시리즈 선택 포털 (최초 접속 메인)"""
    lang = request.args.get('lang', 'KOR').upper()
    return render_template('portal.html', lang=lang, portal_landing=True)


@app.route('/super-series')
def super_series():
    """SUPER SERIES — 기존 웰딩 장비 옵션 시스템 홈"""
    lang = request.args.get('lang', 'KOR').upper()
    if lang == 'ENG':
        return render_template('index_en.html')
    return render_template('index.html')

@app.route('/test')
def test():
    """테스트 엔드포인트"""
    return jsonify({"success": True, "message": "Test endpoint works"})

@app.route('/api/test')  
def api_test():
    """API 테스트 엔드포인트"""
    return jsonify({"success": True, "message": "API test endpoint works"})

@app.route('/test-simple')
def test_simple():
    """간단한 테스트"""
    return "Simple test works"

@app.route('/api/option/test', methods=['GET'])
def api_option_test():
    """옵션 API 테스트 엔드포인트"""
    return jsonify({
        "success": True, 
        "message": "옵션 API 엔드포인트가 정상적으로 작동합니다.",
        "available_endpoints": [
            "POST /api/option/add",
            "POST /api/option/update", 
            "POST /api/option/delete"
        ]
    })

@app.route('/api/option/test2', methods=['GET'])
def api_option_test2():
    """간단한 테스트 엔드포인트"""
    return "API Test OK"



@app.route('/api/all-options', methods=['GET'])
def api_all_options():
    """모든 옵션 목록 조회 - 간단한 버전"""
    try:
        system = get_welding_system()
        categories = system.get_all_categories()
        display_name_map = {
            1: "파워소스",
            2: "타입",
            3: "와이어피더",
            4: "토치",
            5: "호스패키지",
            6: "어스",
            7: "트롤리",
            8: "쿨러",
            9: "쿨란트",
            10: "가스호스",
            11: "용접봉 홀더",
            12: "기타"
        }
        
        options_by_category = {}
        
        for category in categories:
            category_num = category['category_number']
            category_id = category['id']
            
            options_by_category[category_num] = {
                'name': display_name_map.get(category_num, category['category_name']),
                'items': []
            }
            
            options = system.get_category_options(category_id)
            
            for option in options:
                try:
                    code_value = option['item_code']
                    description_value = option['description']
                    item_data = {
                        'id': option['id'],
                        'name': option['item_name'],
                        'code': code_value if code_value is not None else '',
                        'description': description_value if description_value is not None else ''
                    }
                    options_by_category[category_num]['items'].append(item_data)
                except:
                    continue
        
        # 12번 카테고리(기타)가 없으면 option_base_prices에서 추가 (코드관리용)
        if 12 not in options_by_category:
            options_by_category[12] = {'name': '기타', 'items': []}
            try:
                cursor = system.connection.cursor()
                cursor.execute('''
                    SELECT DISTINCT option_name FROM option_base_prices
                    WHERE category_number = 12 ORDER BY option_name
                ''')
                for row in cursor.fetchall():
                    option_name = row[0] if row else None
                    if option_name:
                        product_name = option_name
                        try:
                            cursor.execute('''
                                SELECT product_name FROM export_quotation_new_products
                                WHERE product_code = ? LIMIT 1
                            ''', (option_name,))
                            pr = cursor.fetchone()
                            if pr:
                                product_name = pr[0] if pr[0] else option_name
                        except Exception:
                            pass
                        options_by_category[12]['items'].append({
                            'id': None,
                            'name': product_name or option_name,
                            'code': option_name,
                            'description': ''
                        })
            except Exception:
                pass
        
        return jsonify({
            "success": True,
            "options": options_by_category
        })
    except Exception as e:
        print(f"Error in api_all_options: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

def build_option_lookup():
    """카테고리 및 옵션 정보를 조회하여 코드-이름 매핑을 생성"""
    system = get_welding_system()
    categories = system.get_all_categories()

    category_display_names = {
        1: "파워소스",
        2: "타입",
        3: "와이어피더",
        4: "토치",
        5: "호스패키지",
        6: "어스",
        7: "트롤리",
        8: "쿨러",
        9: "쿨란트",
        10: "가스호스",
        11: "용접봉 홀더"
    }

    option_lookup = {}
    category_name_map = {}

    for category in categories:
        category_number = category['category_number']
        display_name = category_display_names.get(category_number, category['category_name'])
        category_name_map[category_number] = display_name

        options = system.get_category_options(category['id'])
        for option in options:
            code = option.get('item_code')
            if code:
                option_lookup[code] = {
                    'name': option.get('item_name'),
                    'category_number': category_number,
                    'category_name': display_name
                }

    return category_name_map, option_lookup


POWER_FAMILY_ORDER = ["SUPER M", "SUPER C", "SUPER T", "SUPER S", "기타"]


def determine_power_family_from_label(label):
    label_upper = (label or "").upper()
    for family in POWER_FAMILY_ORDER:
        if family != "기타" and family in label_upper:
            return family
    return "기타"


def group_logic_rules(overview):
    grouped = {family: [] for family in POWER_FAMILY_ORDER}
    for rule in overview:
        family = rule.get('family') or determine_power_family_from_label(rule.get('power_source_label'))
        grouped.setdefault(family, []).append(rule)
    return grouped


def first_non_empty_family(grouped):
    for family in POWER_FAMILY_ORDER:
        if grouped.get(family):
            return family
    return POWER_FAMILY_ORDER[0]


def build_logic_rules_overview(rules=None):
    """대시보드 표시용 로직 제한 데이터 구성"""
    system = get_welding_system()
    if rules is None:
        rules = system.get_logic_rules()

    category_name_map, option_lookup = build_option_lookup()

    overview = []

    for idx, rule in enumerate(rules, start=1):
        power_source_code = rule.get('powerSourceCode')
        power_source_value = rule.get('powerSourceValue')

        if power_source_code:
            ps_info = option_lookup.get(power_source_code, {})
            ps_label = f"{ps_info.get('name', power_source_code)} ({power_source_code})"
        elif power_source_value:
            ps_label = power_source_value
        else:
            ps_label = "전체"

        type_label = rule.get('typeValue') or rule.get('typeCode') or '전체'
        power_family = determine_power_family_from_label(ps_label)

        actions = []
        for action in rule.get('actions', []):
            category_number = action.get('category')
            category_name = category_name_map.get(category_number, f"카테고리 {category_number}")

            if action.get('disableCategory'):
                actions.append({
                    'category_number': category_number,
                    'category_name': category_name,
                    'mode': 'disable'
                })
            elif 'allowCodes' in action:
                allowed_codes = action.get('allowCodes', [])
                allow_none = action.get('allowNone', True)
                option_labels = []
                for code in allowed_codes:
                    opt_info = option_lookup.get(code, {})
                    opt_label = f"{opt_info.get('name', code)} ({code})" if opt_info else code
                    option_labels.append(opt_label)
                actions.append({
                    'category_number': category_number,
                    'category_name': category_name,
                    'mode': 'allow',
                    'options': option_labels,
                    'allow_none': allow_none
                })

        overview.append({
            'id': rule.get('id', idx),
            'display_index': idx,
            'power_source_label': ps_label,
            'power_source_code': power_source_code,
            'type_label': type_label,
            'family': power_family,
            'actions': actions,
            'raw_rule': rule
        })

    return overview


def _to_optional_str(value):
    if value is None:
        return None
    value_str = str(value).strip()
    return value_str or None


def _coerce_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ('true', '1', 'y', 'yes', 'on'):
            return True
        if lowered in ('false', '0', 'n', 'no', 'off'):
            return False
    return bool(value)


def normalize_logic_rule_payload(data):
    if not isinstance(data, dict):
        raise ValueError("요청 데이터 형식이 올바르지 않습니다.")
    
    actions = data.get('actions')
    if actions is None:
        actions = []
    if not isinstance(actions, list):
        raise ValueError("actions 필드는 리스트여야 합니다.")
    
    normalized_actions = []
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("각 action 항목은 객체 형태여야 합니다.")
        
        if 'category' not in action:
            raise ValueError("각 action에는 category 필드가 필요합니다.")
        
        try:
            category_number = int(action.get('category'))
        except (TypeError, ValueError):
            raise ValueError("category 값은 정수여야 합니다.")
        
        normalized_action = {
            'category': category_number
        }
        
        if 'allowCodes' in action and action.get('allowCodes') is not None:
            allow_codes = action.get('allowCodes')
            if not isinstance(allow_codes, list):
                raise ValueError("allowCodes 필드는 리스트여야 합니다.")
            normalized_action['allowCodes'] = [str(code).strip() for code in allow_codes if str(code).strip()]
            normalized_action['allowNone'] = _coerce_bool(action.get('allowNone'), True)
        
        if action.get('disableCategory') is not None:
            normalized_action['disableCategory'] = _coerce_bool(action.get('disableCategory'), True)
        
        normalized_actions.append(normalized_action)
    
    order_index = data.get('orderIndex')
    if order_index is not None and order_index != '':
        try:
            order_index = int(order_index)
        except (TypeError, ValueError):
            raise ValueError("orderIndex 값은 정수여야 합니다.")
    else:
        order_index = None
    
    return {
        'powerSourceCode': _to_optional_str(data.get('powerSourceCode')),
        'powerSourceValue': _to_optional_str(data.get('powerSourceValue')),
        'typeCode': _to_optional_str(data.get('typeCode')),
        'typeValue': _to_optional_str(data.get('typeValue')),
        'orderIndex': order_index,
        'actions': normalized_actions
    }


@app.route('/api/logic_rules', methods=['GET', 'POST'])
def api_logic_rules():
    system = get_welding_system()
    
    if request.method == 'GET':
        rules = system.get_logic_rules()
        return jsonify({
            "success": True,
            "rules": rules
        })
    
    try:
        payload = request.get_json(silent=False)
    except Exception:
        payload = None
    
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "JSON 형식의 데이터가 필요합니다."}), 400
    
    try:
        normalized = normalize_logic_rule_payload(payload)
        rule = system.create_logic_rule(normalized)
        overview = build_logic_rules_overview()
        return jsonify({
            "success": True,
            "rule": rule,
            "overview": overview
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/logic_rules/<int:rule_id>', methods=['PUT', 'DELETE'])
def api_logic_rule_detail(rule_id):
    system = get_welding_system()
    
    if request.method == 'DELETE':
        try:
            deleted = system.delete_logic_rule(rule_id)
            if not deleted:
                return jsonify({"success": False, "error": "해당 ID의 제한조건을 찾을 수 없습니다."}), 404
            overview = build_logic_rules_overview()
            return jsonify({"success": True, "overview": overview})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
    
    # PUT
    try:
        payload = request.get_json(silent=False)
    except Exception:
        payload = None
    
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "JSON 형식의 데이터가 필요합니다."}), 400
    
    try:
        normalized = normalize_logic_rule_payload(payload)
        updated = system.update_logic_rule(rule_id, normalized)
        if not updated:
            return jsonify({"success": False, "error": "해당 ID의 제한조건을 찾을 수 없습니다."}), 404
        overview = build_logic_rules_overview()
        return jsonify({
            "success": True,
            "rule": updated,
            "overview": overview
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def render_select_options_page(template_name, view_mode=False, lang='KOR'):
    """옵션 선택/조회 페이지 렌더링 헬퍼"""
    system = get_welding_system()
    categories = system.get_all_categories()
    
    if lang == 'ENG':
        category_display_names = {
            1: "Power Source",
            2: "Type",
            3: "Wire Feeder",
            4: "Torch",
            5: "Hose Package",
            6: "Ground",
            7: "Trolley",
            8: "Cooler",
            9: "Coolant",
            10: "Gas Hose",
            11: "Electrode Holder",
            12: "Other"
        }
    else:
        category_display_names = {
            1: "파워소스",
            2: "타입",
            3: "와이어피더",
            4: "토치",
            5: "호스패키지",
            6: "어스",
            7: "트롤리",
            8: "쿨러",
            9: "쿨란트",
            10: "가스호스",
            11: "용접봉 홀더",
            12: "기타"
        }
    
    # 12번 카테고리(기타)가 DB에 없으면 목록에 추가
    if not any(c.get('category_number') == 12 for c in categories):
        categories.append({'id': 12, 'category_name': '기타', 'category_number': 12})
    
    for category in categories:
        overridden_name = category_display_names.get(category['category_number'])
        if overridden_name:
            category['category_name'] = overridden_name
    
    categories.sort(key=lambda c: c['category_number'])
    
    # 각 카테고리의 옵션들 가져오기
    category_options = {}
    for category in categories:
        if category.get('category_number') == 12:
            # 12번(기타) 옵션은 option_base_prices에서 로드
            cat12_items = []
            try:
                cursor = system.connection.cursor()
                cursor.execute('''
                    SELECT DISTINCT option_name FROM option_base_prices
                    WHERE category_number = 12 ORDER BY option_name
                ''')
                for row in cursor.fetchall():
                    option_name = row[0] if row else None
                    if option_name:
                        product_name = option_name
                        try:
                            cursor.execute('''
                                SELECT product_name FROM export_quotation_new_products
                                WHERE product_code = ? LIMIT 1
                            ''', (option_name,))
                            pr = cursor.fetchone()
                            if pr:
                                product_name = pr[0] if pr[0] else option_name
                        except Exception:
                            pass
                        cat12_items.append({
                            'id': None,
                            'item_name': product_name or option_name,
                            'item_code': option_name,
                            'description': ''
                        })
            except Exception:
                pass
            category_options[category['id']] = cat12_items
        else:
            category_options[category['id']] = system.get_category_options(category['id'])
    
    logic_rules = system.get_logic_rules()
    
    return render_template(
        template_name,
        categories=categories,
        category_options=category_options,
        view_mode=view_mode,
        detailed_rules=logic_rules
    )

@app.route('/select_options')
def select_options():
    """옵션 선택 페이지"""
    mode = request.args.get('mode', '').lower()
    if mode == 'view':
        return redirect(url_for('select_options', view='true'))
    view_flag = request.args.get('view', '').lower() == 'true'
    lang = request.args.get('lang', 'KOR').upper()
    
    if view_flag:
        template_name = 'select_options_view_en.html' if lang == 'ENG' else 'select_options_view.html'
    else:
        template_name = 'select_options.html'
    return render_select_options_page(template_name, view_mode=view_flag, lang=lang)

@app.route('/lookup')
def lookup():
    """코드 조회 페이지"""
    lang = request.args.get('lang', 'KOR').upper()
    if lang == 'ENG':
        return render_template('lookup_en.html')
    return render_template('lookup.html')

@app.route('/api/select_options', methods=['POST'])
def api_select_options():
    """옵션 선택 API"""
    try:
        print("=== 옵션 선택 API 호출 ===")
        print(f"받은 데이터: {request.json}")
        
        system = get_welding_system()
        selected_options = request.json.get('selected_options', {})
        
        # 문자열 키를 정수로 변환
        selected_options_int = {}
        for key, value in selected_options.items():
            selected_options_int[int(key)] = value
        
        # 사용자가 입력한 코드 사용
        combination_code = request.json.get('code', '').strip()
        set_code = request.json.get('set_code', '').strip()
        call_arc_code = request.json.get('call_arc_code', '').strip()
        
        print(f"입력받은 코드: '{combination_code}'")
        print(f"세트코드: '{set_code}'")
        print(f"콜아크코드: '{call_arc_code}'")
        
        if not combination_code:
            print("코드가 비어있음")
            return jsonify({
                "success": False,
                "error": "코드를 입력해주세요."
            }), 400
        
        # 세트코드 형식 검증: P5 + 7자리 숫자 (총 9자리)
        if not re.fullmatch(r'^P5\d{7}$', set_code):
            print(f"세트코드 형식 오류: '{set_code}'")
            return jsonify({
                "success": False,
                "error": "세트코드는 'P5'로 시작하는 9자리(뒤 7자리 숫자)여야 합니다. 예: P51000006"
            }), 400
        
        # 콜아크코드는 입력하는 경우 M으로 시작해야 함
        if call_arc_code:
            if not call_arc_code.startswith('M'):
                print(f"콜아크코드가 M으로 시작하지 않음: '{call_arc_code}'")
                return jsonify({
                    "success": False,
                    "error": "콜아크코드는 'M'으로 시작해야 합니다. 예: M2122I00008"
                }), 400
            if len(call_arc_code) != 11:
                print(f"콜아크코드 길이 오류: '{call_arc_code}' (길이: {len(call_arc_code)})")
                return jsonify({
                    "success": False,
                    "error": "콜아크코드는 총 11자리여야 합니다. 예: M2122I00008"
                }), 400
        
        # 중복 확인 및 저장 (set_code, call_arc_code 포함)
        save_result = system.save_combination(
            combination_code, 
            selected_options_int,
            set_code=set_code,
            call_arc_code=call_arc_code
        )
        
        # 결과 처리
        if save_result["is_new"]:
            # 새로운 코드인 경우
            code = save_result["new_code"]
            filename = system.export_to_excel(code, selected_options_int)
            result = {
                "success": True,
                "code": code,
                "set_code": set_code,
                "call_arc_code": call_arc_code,
                "is_new": True,
                "is_duplicate_options": False,
                "filename": filename,
                "selected_options": selected_options_int,
                "message": f"새로운 코드가 생성되었습니다: {code}"
            }
        else:
            # 기존 코드인 경우
            existing_code = save_result["existing_code"]
            existing_set_code = save_result.get("set_code", "")
            existing_call_arc_code = save_result.get("call_arc_code", "")
            is_duplicate_set_code = save_result.get("is_duplicate_set_code", False)
            is_duplicate_options = save_result.get("is_duplicate_options", False)
            
            # 세트코드 중복인 경우 미생성 처리
            if is_duplicate_set_code:
                result = {
                    "success": True,
                    "code": existing_code,
                    "set_code": existing_set_code,
                    "call_arc_code": existing_call_arc_code,
                    "is_new": False,
                    "is_duplicate_set_code": True,
                    "is_duplicate_options": False,
                    "filename": None,  # 엑셀 생성하지 않음
                    "selected_options": selected_options_int,
                    "message": f"세트코드 '{set_code}'가 이미 존재합니다. 기존 코드: {existing_code}"
                }
            # 옵션 중복인 경우 코드 생성하지 않고 기존 세트코드만 알림
            elif is_duplicate_options:
                result = {
                    "success": True,
                    "code": existing_code,
                    "set_code": existing_set_code,
                    "call_arc_code": existing_call_arc_code,
                    "is_new": False,
                    "is_duplicate_set_code": False,
                    "is_duplicate_options": True,
                    "filename": None,  # 엑셀 생성하지 않음
                    "selected_options": selected_options_int,
                    "message": "같은 옵션 조합이 이미 존재합니다"
                }
            else:
                # 코드만 중복인 경우 (옵션은 다를 수 있음)
                filename = system.export_to_excel(existing_code, selected_options_int)
                result = {
                    "success": True,
                    "code": existing_code,
                    "set_code": existing_set_code,
                    "call_arc_code": existing_call_arc_code,
                    "is_new": False,
                    "is_duplicate_options": False,
                    "filename": filename,
                    "selected_options": selected_options_int,
                    "message": f"같은 코드가 이미 존재합니다. 기존 코드: {existing_code}"
                }
        
        print(f"응답 데이터: {result}")
        print(f"응답의 set_code: {result.get('set_code')}")
        print(f"응답의 call_arc_code: {result.get('call_arc_code')}")
        
        return jsonify(result)
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/api/select_options/by-options', methods=['POST'])
def api_select_options_by_options():
    """선택된 옵션 조합으로 코드 조회"""
    try:
        system = get_welding_system()
        payload = request.get_json(silent=True) or {}
        selected_options = payload.get('selected_options')
        
        if not isinstance(selected_options, dict) or not selected_options:
            return jsonify({
                "success": False,
                "error": "선택된 옵션 정보가 필요합니다."
            }), 400
        
        normalized_options = {}
        for key, value in selected_options.items():
            if not value or value == '선택없음':
                continue
            try:
                category_number = int(key)
            except (ValueError, TypeError):
                return jsonify({
                    "success": False,
                    "error": f"잘못된 카테고리 번호입니다: {key}"
                }), 400
            normalized_options[category_number] = value
        
        if not normalized_options:
            return jsonify({
                "success": False,
                "error": "최소 하나 이상의 옵션을 선택해주세요."
            }), 400

        missing_categories = payload.get('missing_categories')
        if isinstance(missing_categories, list) and missing_categories:
            return jsonify({
                "success": False,
                "error": "모든 필수 옵션을 선택해주세요."
            }), 400
        
        result = system.find_combination_by_options(normalized_options, allow_partial=False)
        if result:
            return jsonify({
                "success": True,
                "found": True,
                "code": result["combination_code"],
                "set_code": result["set_code"],
                "call_arc_code": result["call_arc_code"],
                "selected_options": result["selected_options"],
                "created_at": result["created_at"]
            })
        else:
            return jsonify({
                "success": True,
                "found": False,
                "message": "미생성 코드입니다."
            })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/lookup', methods=['POST'])
def api_lookup():
    """코드 조회 API"""
    try:
        system = get_welding_system()
        combination_code = request.json.get('code', '').strip()
        
        if not combination_code:
            return jsonify({
                "success": False,
                "error": "코드를 입력해주세요."
            }), 400
        
        result = system.lookup_combination(combination_code)
        
        if result["found"]:
            # 실제 매칭된 코드 사용 (부분 검색인 경우 전체 코드 반환)
            actual_code = result.get("code", combination_code)
            set_code = result.get("set_code", "")
            call_arc_code = result.get("call_arc_code", "")
            
            return jsonify({
                "success": True,
                "found": True,
                "code": actual_code,
                "set_code": set_code,
                "call_arc_code": call_arc_code,
                "search_code": combination_code,  # 검색에 사용된 코드
                "options": result["options"],
                "created_at": result["created_at"]
            })
        else:
            return jsonify({
                "success": True,
                "found": False,
                "code": combination_code,
                "message": f"코드 '{combination_code}'를 찾을 수 없습니다."
            })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/code_list', methods=['GET'])
def api_code_list():
    """저장된 코드 리스트 조회 API"""
    try:
        system = get_welding_system()
        result = system.get_all_combinations()
        
        return jsonify({
            "success": True,
            "total_count": result["total_count"],
            "combinations": result["combinations"]
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/options', methods=['GET'])
def api_options():
    """모든 옵션 목록 조회 API"""
    try:
        system = get_welding_system()
        categories = system.get_all_categories()
        
        options_by_category = {}
        display_name_map = {
            1: "파워소스",
            2: "타입",
            3: "와이어피더",
            4: "토치",
            5: "호스패키지",
            6: "어스",
            7: "트롤리",
            8: "쿨러",
            9: "쿨란트",
            10: "가스호스",
            11: "용접봉 홀더",
            12: "기타"
        }
        
        for category in categories:
            category_num = category['category_number']
            category_id = category['id']
            
            options_by_category[category_num] = {
                'name': display_name_map.get(category_num, category['category_name']),
                'items': []
            }
            
            # 해당 카테고리의 옵션들 가져오기
            options = system.get_category_options(category_id)
            
            for option in options:
                try:
                    # sqlite3.Row 또는 딕셔너리 모두 처리
                    code_value = option['item_code']
                    description_value = option['description']
                    item_data = {
                        'id': option['id'],
                        'name': option['item_name'],
                        'code': code_value if code_value is not None else '',
                        'description': description_value if description_value is not None else ''
                    }
                    options_by_category[category_num]['items'].append(item_data)
                except (KeyError, TypeError) as e:
                    print(f"Error processing option: {option}, {e}")
                    continue
        
        # 12번 카테고리(기타)가 없으면 빈 카테고리로 추가
        if 12 not in options_by_category:
            options_by_category[12] = {
                'name': '기타',
                'items': []
            }
        
        # 12번 카테고리의 옵션들을 option_base_prices에서 가져오기
        try:
            cursor = system.connection.cursor()
            cursor.execute('''
                SELECT DISTINCT option_name, price
                FROM option_base_prices
                WHERE category_number = 12
                ORDER BY option_name
            ''')
            
            base_price_rows = cursor.fetchall()
            for row in base_price_rows:
                try:
                    if hasattr(row, 'keys') and callable(getattr(row, 'keys', None)):
                        option_name = row['option_name'] if 'option_name' in row.keys() else (row[0] if len(row) > 0 else None)
                        price = row['price'] if 'price' in row.keys() else (row[1] if len(row) > 1 else None)
                    else:
                        option_name = row[0] if len(row) > 0 else None
                        price = row[1] if len(row) > 1 else None
                    
                    if option_name:
                        # 제품코드로 제품명 조회
                        product_code = option_name
                        product_name = product_code  # 기본값은 제품코드
                        try:
                            cursor.execute('''
                                SELECT product_name
                                FROM export_quotation_new_products
                                WHERE product_code = ?
                                LIMIT 1
                            ''', (product_code,))
                            
                            product_row = cursor.fetchone()
                            if product_row:
                                if hasattr(product_row, 'keys') and callable(getattr(product_row, 'keys', None)):
                                    product_name = product_row['product_name'] if 'product_name' in product_row.keys() else (product_row[0] if len(product_row) > 0 else product_code)
                                else:
                                    product_name = product_row[0] if len(product_row) > 0 and product_row[0] else product_code
                        except Exception as e:
                            product_name = product_code
                        
                        # 이미 추가된 옵션인지 확인 (제품코드 기준)
                        existing_item = next((item for item in options_by_category[12]['items'] if item['code'] == product_code), None)
                        if not existing_item:
                            options_by_category[12]['items'].append({
                                'id': None,
                                'name': product_name or product_code,  # 제품명 사용, 없으면 제품코드
                                'code': product_code,  # 제품코드
                                'description': '',
                                'price': price
                            })
                except Exception as e:
                    continue
        except Exception as e:
            import traceback
            print(traceback.format_exc())
        
        return jsonify({
            "success": True,
            "options": options_by_category
        })
    
    except Exception as e:
        import traceback
        print(f"ERROR in api_options: {e}")
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/delete_code', methods=['POST'])
def api_delete_code():
    """코드 삭제 API"""
    try:
        data = request.json
        code = data.get('code', '').strip()
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        
        print(f"=== 코드 삭제 API 호출 ===")
        print(f"받은 데이터: {data}")
        print(f"코드: {code}")
        print(f"사용자명: {username}")
        print(f"비밀번호: {password}")
        
        if not code:
            return jsonify({
                "success": False,
                "error": "삭제할 코드를 입력해주세요."
            }), 400
        
        if not username or not password:
            print("사용자명 또는 비밀번호가 비어있음")
            return jsonify({
                "success": False,
                "error": "사용자명과 비밀번호를 입력해주세요."
            }), 400
        
        # 간단한 인증 (실제 환경에서는 더 안전한 인증 방식을 사용해야 함)
        if username != "pns" or password != "pns@123":
            print(f"인증 실패: username='{username}' (예상: 'pns'), password='{password}' (예상: 'pns@123')")
            return jsonify({
                "success": False,
                "error": "인증에 실패했습니다."
            }), 401
        
        print("인증 성공!")
        
        system = get_welding_system()
        result = system.delete_combination(code)
        
        if result["success"]:
            return jsonify({
                "success": True,
                "message": f"코드 '{code}'가 성공적으로 삭제되었습니다."
            })
        else:
            return jsonify({
                "success": False,
                "error": result["error"]
            }), 400
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/db_management')
def db_management():
    """데이터베이스 관리 페이지"""
    if 'db_admin_authenticated' not in session or not session.get('db_admin_authenticated'):
        return render_template('db_login.html', redirect_url=request.path)
    return render_template('db_management.html')

@app.route('/option_management')
def option_management():
    """옵션관리 페이지"""
    return render_template('option_management.html')

@app.route('/option_price_management', endpoint='option_price_management')
def option_price_management():
    """옵션별 가격 관리 페이지"""
    return render_template('option_price_management.html')


@app.route('/logic_rules')
def logic_rules_dashboard():
    """로직 제한조건 대시보드"""
    system = get_welding_system()
    rules = system.get_logic_rules()
    overview = build_logic_rules_overview(rules)
    grouped_rules = group_logic_rules(overview)
    default_family = first_non_empty_family(grouped_rules)
    rule_categories = sorted({action.get('category') for rule in rules for action in rule.get('actions', []) if isinstance(action.get('category'), int)})

    return render_template(
        'logic_rules_dashboard.html',
        logic_rules=overview,
        logic_rule_groups=grouped_rules,
        logic_rule_default_family=default_family,
        power_family_order=POWER_FAMILY_ORDER,
        detailed_rules=rules,
        rule_categories=rule_categories
    )


@app.route('/logic_rules/settings')
def logic_rules_settings():
    """로직 제한조건 설정 페이지"""
    view_mode = request.args.get('view', 'new')

    system = get_welding_system()
    rules = system.get_logic_rules()

    categories = system.get_all_categories()
    category_map = {c['category_number']: c for c in categories}

    category_options = {}
    category_option_codes = {}
    for category in categories:
        category_number = category['category_number']
        options = system.get_category_options(category['id'])
        category_options[str(category_number)] = [
            {
                "id": option.get('id'),
                "name": option.get('item_name'),
                "code": option.get('item_code'),
                "description": option.get('description')
            }
            for option in options
            if option.get('item_code')
        ]
        category_option_codes[category_number] = {
            opt.get('item_code') for opt in options if opt.get('item_code')
        }

    for rule in rules:
        power_code = rule.get('powerSourceCode')
        power_value = rule.get('powerSourceValue')
        if power_code:
            if 1 not in category_map:
                category_map[1] = {
                    'id': None,
                    'category_name': '파워소스',
                    'category_number': 1
                }
                categories.append(category_map[1])
                category_options.setdefault('1', [])
                category_option_codes.setdefault(1, set())
            if power_code not in category_option_codes.get(1, set()):
                category_options.setdefault('1', []).append({
                    "id": None,
                    "name": power_value or power_code,
                    "code": power_code,
                    "description": ''
                })
                category_option_codes.setdefault(1, set()).add(power_code)

        type_code = rule.get('typeCode')
        type_value = rule.get('typeValue')
        if type_code:
            if 2 not in category_map:
                category_map[2] = {
                    'id': None,
                    'category_name': '타입',
                    'category_number': 2
                }
                categories.append(category_map[2])
                category_options.setdefault('2', [])
                category_option_codes.setdefault(2, set())
            if type_code not in category_option_codes.get(2, set()):
                category_options.setdefault('2', []).append({
                    "id": None,
                    "name": type_value or type_code,
                    "code": type_code,
                    "description": ''
                })
                category_option_codes.setdefault(2, set()).add(type_code)

        for action in rule.get('actions', []):
            category_number = action.get('category')
            if not category_number:
                continue
            if category_number not in category_map:
                category_map[category_number] = {
                    'id': None,
                    'category_name': f'카테고리 {category_number}',
                    'category_number': category_number
                }
                categories.append(category_map[category_number])
            category_options.setdefault(str(category_number), [])
            category_option_codes.setdefault(category_number, set())

            allow_codes = action.get('allowCodes') or []
            for code in allow_codes:
                if not code:
                    continue
                if code not in category_option_codes[category_number]:
                    category_options[str(category_number)].append({
                        "id": None,
                        "name": code,
                        "code": code,
                        "description": ''
                    })
                    category_option_codes[category_number].add(code)

    categories.sort(key=lambda c: c['category_number'])

    def option_sort_key(option):
        name = (option.get('name') or '').strip()
        code = option.get('code') or ''
        bracket_match = re.match(r'\[(\d+)\]', name)
        if bracket_match:
            return (0, int(bracket_match.group(1)))
        return (1, name or code)

    for opts in category_options.values():
        opts.sort(key=option_sort_key)

    rule_categories = sorted({action.get('category') for rule in rules for action in rule.get('actions', []) if isinstance(action.get('category'), int)})

    overview = build_logic_rules_overview(rules)
    grouped_rules = group_logic_rules(overview)
    default_family = first_non_empty_family(grouped_rules)

    return render_template(
        'logic_rules_settings.html',
        logic_rules=overview,
        logic_rule_groups=grouped_rules,
        logic_rule_default_family=default_family,
        power_family_order=POWER_FAMILY_ORDER,
        detailed_rules=rules,
        rule_categories=rule_categories,
        categories=categories,
        category_options=category_options,
        view_mode=view_mode
    )

@app.route('/api/categories')
def api_categories():
    """카테고리 목록 조회 (코드관리·옵션추가 등 카테고리 선택용)"""
    try:
        system = get_welding_system()
        raw = system.get_categories()
        # 수정 가능한 새 리스트로 복사 (1~11 + 12 기타 포함)
        categories = [dict(c) for c in raw] if raw else []
        display_name_map = {
            1: "파워소스",
            2: "타입",
            3: "와이어피더",
            4: "토치",
            5: "호스패키지",
            6: "어스",
            7: "트롤리",
            8: "쿨러",
            9: "쿨란트",
            10: "가스호스",
            11: "용접봉 홀더",
            12: "기타"
        }
        for category in categories:
            number = category.get("number")
            if number is not None and int(number) in display_name_map:
                category["name"] = display_name_map[int(number)]
        # 12번 카테고리(기타)가 없으면 반드시 추가 (옵션추가/수정/삭제 시 카테고리 선택용)
        has_12 = any(c.get("number") is not None and int(c.get("number")) == 12 for c in categories)
        if not has_12:
            categories.append({"id": 12, "name": "기타", "number": 12})
        categories.sort(key=lambda c: (int(c.get("number", 0)) if c.get("number") is not None else 0))
        
        return jsonify({
            "success": True,
            "categories": categories
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/options/category/<int:category_id>')
def api_options_by_category(category_id):
    """특정 카테고리의 옵션 목록 조회"""
    try:
        system = get_welding_system()
        # 12번(기타)은 option_base_prices에서 조회 (코드관리용)
        if category_id == 12:
            items = []
            try:
                cursor = system.connection.cursor()
                cursor.execute('''
                    SELECT DISTINCT option_name FROM option_base_prices
                    WHERE category_number = 12 ORDER BY option_name
                ''')
                for row in cursor.fetchall():
                    option_name = row[0] if row else None
                    if option_name:
                        product_name = option_name
                        try:
                            cursor.execute('''
                                SELECT product_name FROM export_quotation_new_products
                                WHERE product_code = ? LIMIT 1
                            ''', (option_name,))
                            pr = cursor.fetchone()
                            if pr:
                                product_name = pr[0] if pr[0] else option_name
                        except Exception:
                            pass
                        items.append({
                            "id": None,
                            "name": product_name or option_name,
                            "code": option_name,
                            "description": ""
                        })
            except Exception:
                pass
        else:
            items = system.get_items_by_category(category_id)
        
        return jsonify({
            "success": True,
            "items": items
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/db/login', methods=['POST'])
def api_db_login():
    """데이터베이스 관리 페이지 로그인"""
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        
        # 인증 확인 (ID: it, Password: it@123)
        if username == "it" and password == "it@123":
            session['db_admin_authenticated'] = True
            return jsonify({
                "success": True,
                "message": "인증 성공"
            })
        else:
            return jsonify({
                "success": False,
                "error": "인증에 실패했습니다."
            }), 401
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/auth/login', methods=['POST'])
@app.route('/api/admin/login', methods=['POST'])
def api_auth_login():
    """통합 로그인: customer / trade / pns (역할별 권한 차등)"""
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or data.get('loginId') or '').strip().lower()
        password = (data.get('password') or data.get('loginPassword') or '').strip()

        user = authenticate_tracking_user(username, password)
        if not user:
            return jsonify({"success": False, "error": "ID 또는 비밀번호가 올바르지 않습니다."}), 401

        system = get_welding_system()
        cursor = system.connection.cursor()
        ensure_user_tracking_tables(cursor)
        tracking_session_id = create_tracking_session(
            cursor, user['user_id'], user['user_type'],
            request.headers.get('User-Agent'), request.remote_addr
        )
        system.connection.commit()

        # 세션 초기화 후 역할 저장
        session['pns_admin'] = (user['user_type'] == 'admin')
        session['pns_authenticated'] = (user['user_type'] == 'admin')
        session['tracking_user_id'] = user['user_id']
        session['tracking_user_type'] = user['user_type']
        session['tracking_session_id'] = tracking_session_id
        session['auth_logged_in'] = True

        return jsonify({
            "success": True,
            "message": "로그인에 성공했습니다.",
            "user_id": user['user_id'],
            "user_type": user['user_type'],
            "user_type_label": user.get('user_type_label') or USER_TYPES.get(user['user_type'], user['user_type']),
            "permissions": user.get('permissions') or sorted(ROLE_PERMISSIONS.get(user['user_type'], set())),
            "tracking_session_id": tracking_session_id,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/auth/logout', methods=['POST'])
@app.route('/api/admin/logout', methods=['POST'])
def api_auth_logout():
    session.pop('pns_admin', None)
    session.pop('pns_authenticated', None)
    session.pop('auth_logged_in', None)
    session.pop('tracking_user_id', None)
    session.pop('tracking_user_type', None)
    session.pop('tracking_session_id', None)
    return jsonify({"success": True, "message": "로그아웃되었습니다."})


@app.route('/api/auth/session', methods=['GET'])
@app.route('/api/admin/session', methods=['GET'])
def api_auth_session():
    user_type = session.get('tracking_user_type')
    logged_in = bool(session.get('auth_logged_in') or session.get('pns_admin') or session.get('tracking_user_id'))
    return jsonify({
        "success": True,
        "logged_in": logged_in,
        "user_id": session.get('tracking_user_id'),
        "user_type": user_type,
        "user_type_label": USER_TYPES.get(user_type, user_type) if user_type else None,
        "permissions": sorted(ROLE_PERMISSIONS.get(user_type, set())) if user_type else [],
        "tracking_session_id": session.get('tracking_session_id'),
        "is_admin": bool(session.get('pns_admin')),
    })


@app.route('/activity_tracking')
def activity_tracking_dashboard():
    """사용자 활동 추적 대시보드 (관리자 전용)"""
    if not session.get('pns_admin'):
        return redirect(url_for('index'))
    return render_template('activity_tracking.html')


@app.route('/db_backup')
def db_backup_dashboard():
    """DB 백업 현황 모니터링 (PNS 관리자)"""
    if not _is_pns_admin_session():
        return redirect(url_for('index'))
    return render_template('db_backup.html')


@app.route('/api/db_backup/status', methods=['GET'])
def api_db_backup_status():
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        return jsonify(get_db_backup_status())
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/db_backup/config', methods=['POST'])
def api_db_backup_config():
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        data = request.get_json(silent=True) or {}
        cfg = update_db_backup_config(
            enabled=data.get('enabled'),
            hour=data.get('hour'),
            minute=data.get('minute'),
            retention_days=data.get('retention_days'),
        )
        return jsonify({
            "success": True,
            "message": "설정이 저장되었습니다.",
            "config": cfg,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/db_backup/run', methods=['POST'])
def api_db_backup_run():
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        data = request.get_json(silent=True) or {}
        kind = (data.get('kind') or 'manual').strip().lower()
        if kind not in ('manual', 'daily'):
            kind = 'manual'
        result = create_db_backup(kind)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/db_backup/download', methods=['GET'])
def api_db_backup_download():
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        rel = (request.args.get('path') or '').strip()
        path = resolve_backup_path(rel)
        if not path:
            return jsonify({"success": False, "error": "파일을 찾을 수 없습니다."}), 404
        return send_file(
            path,
            as_attachment=True,
            download_name=os.path.basename(path),
            mimetype='application/octet-stream',
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/db_backup/delete', methods=['POST'])
def api_db_backup_delete():
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        data = request.get_json(silent=True) or {}
        result = delete_db_backup((data.get('path') or '').strip())
        status = 200 if result.get('success') else 400
        return jsonify(result), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/db_backup/restore', methods=['POST'])
def api_db_backup_restore():
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        data = request.get_json(silent=True) or {}
        result = restore_db_backup((data.get('path') or '').strip())
        status = 200 if result.get('success') else 400
        return jsonify(result), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/tracking/login', methods=['POST'])
def api_tracking_login():
    """고객/수출부 페이지 인증 (통합 로그인과 동일 계정)"""
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip().lower()
        password = (data.get('password') or '').strip()

        user = authenticate_tracking_user(username, password)
        if not user:
            return jsonify({"success": False, "error": "인증에 실패했습니다."}), 401

        system = get_welding_system()
        cursor = system.connection.cursor()
        ensure_user_tracking_tables(cursor)
        tracking_session_id = create_tracking_session(
            cursor, user['user_id'], user['user_type'],
            request.headers.get('User-Agent'), request.remote_addr
        )
        system.connection.commit()

        session['pns_admin'] = (user['user_type'] == 'admin')
        session['pns_authenticated'] = (user['user_type'] == 'admin')
        session['auth_logged_in'] = True
        session['tracking_user_id'] = user['user_id']
        session['tracking_user_type'] = user['user_type']
        session['tracking_session_id'] = tracking_session_id

        return jsonify({
            "success": True,
            "user_id": user['user_id'],
            "user_type": user['user_type'],
            "user_type_label": user.get('user_type_label') or USER_TYPES.get(user['user_type'], user['user_type']),
            "permissions": user.get('permissions') or sorted(ROLE_PERMISSIONS.get(user['user_type'], set())),
            "tracking_session_id": tracking_session_id,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/tracking/pageview', methods=['POST'])
def api_tracking_pageview():
    try:
        data = request.get_json(silent=True) or {}
        session_id = data.get('session_id') or session.get('tracking_session_id')
        user_id = data.get('user_id') or session.get('tracking_user_id')
        user_type = data.get('user_type') or session.get('tracking_user_type')
        if not session_id or not user_id or not user_type:
            return jsonify({"success": False, "error": "추적 세션이 없습니다."}), 400

        system = get_welding_system()
        cursor = system.connection.cursor()
        ensure_user_tracking_tables(cursor)
        duration = int(data.get('duration_seconds') or 0)
        log_page_view(
            cursor, session_id, user_id, user_type,
            data.get('page_path', ''), data.get('page_title', ''), duration
        )
        update_session_activity(cursor, session_id, data.get('total_seconds'))
        system.connection.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/tracking/heartbeat', methods=['POST'])
def api_tracking_heartbeat():
    try:
        data = request.get_json(silent=True) or {}
        session_id = data.get('session_id') or session.get('tracking_session_id')
        if not session_id:
            return jsonify({"success": False}), 400
        system = get_welding_system()
        cursor = system.connection.cursor()
        update_session_activity(cursor, session_id, data.get('total_seconds'))
        system.connection.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/tracking/quote_download', methods=['POST'])
def api_tracking_quote_download():
    try:
        data = request.get_json(silent=True) or {}
        session_id = data.get('session_id') or session.get('tracking_session_id')
        user_id = data.get('user_id') or session.get('tracking_user_id')
        user_type = data.get('user_type') or session.get('tracking_user_type')
        if not user_id or not user_type:
            return jsonify({"success": False, "error": "추적 세션이 없습니다."}), 400

        system = get_welding_system()
        cursor = system.connection.cursor()
        ensure_user_tracking_tables(cursor)
        log_quote_download(
            cursor, session_id, user_id, user_type,
            data.get('quote_source', ''), data.get('products', []), data.get('file_name', '')
        )
        system.connection.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/tracking/dashboard', methods=['GET'])
def api_tracking_dashboard():
    if not session.get('pns_admin'):
        return jsonify({"success": False, "error": "관리자 권한이 필요합니다."}), 403
    try:
        user_type = request.args.get('user_type')
        user_id = request.args.get('user_id')
        system = get_welding_system()
        cursor = system.connection.cursor()
        ensure_user_tracking_tables(cursor)
        data = get_dashboard_data(
            cursor,
            user_type_filter=user_type if user_type in ('customer', 'export_dept') else None,
            user_id_filter=user_id if user_id else None,
        )
        return jsonify({"success": True, **data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/pns/login', methods=['POST'])
def api_pns_login():
    """세트가격조회(PNS) 페이지 로그인"""
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        
        # 인증 확인 (ID: pns, Password: pns@123)
        if username == "pns" and password == "pns@123":
            session['pns_authenticated'] = True
            return jsonify({
                "success": True,
                "message": "인증 성공"
            })
        else:
            return jsonify({
                "success": False,
                "error": "인증에 실패했습니다."
            }), 401
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/pns/logout', methods=['POST'])
def api_pns_logout():
    """세트가격조회(PNS) 페이지 로그아웃"""
    session.pop('pns_authenticated', None)
    return jsonify({
        "success": True,
        "message": "로그아웃되었습니다."
    })

@app.route('/api/db/logout', methods=['POST'])
def api_db_logout():
    """데이터베이스 관리 페이지 로그아웃"""
    try:
        session.pop('db_admin_authenticated', None)
        return jsonify({
            "success": True,
            "message": "로그아웃 완료"
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/db/tables', methods=['GET'])
def api_db_tables():
    """데이터베이스 테이블 목록 조회"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        
        return jsonify({
            "success": True,
            "tables": tables
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/db/table_info/<table_name>', methods=['GET'])
def api_table_info(table_name):
    """테이블 정보 조회"""
    try:
        # 테이블 이름 검증
        if not table_name.isidentifier():
            return jsonify({
                "success": False,
                "error": "유효하지 않은 테이블 이름입니다."
            }), 400
        
        system = get_welding_system()
        result = system.get_table_info(table_name)
        return jsonify(result)
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/db/add_column', methods=['POST'])
def api_add_column():
    """칼럼 추가"""
    try:
        data = request.json
        table_name = data.get('table_name', '').strip()
        column_name = data.get('column_name', '').strip()
        column_type = data.get('column_type', 'TEXT').strip().upper()
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        
        # 인증
        if username != "admin" or password != "admin":
            return jsonify({
                "success": False,
                "error": "관리자 권한이 필요합니다."
            }), 401
        
        if not table_name or not column_name:
            return jsonify({
                "success": False,
                "error": "테이블 이름과 칼럼 이름을 입력해주세요."
            }), 400
        
        system = get_welding_system()
        result = system.add_column(table_name, column_name, column_type)
        
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/db/delete_column', methods=['POST'])
def api_delete_column():
    """칼럼 삭제"""
    try:
        data = request.json
        table_name = data.get('table_name', '').strip()
        column_name = data.get('column_name', '').strip()
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        
        # 인증
        if username != "admin" or password != "admin":
            return jsonify({
                "success": False,
                "error": "관리자 권한이 필요합니다."
            }), 401
        
        if not table_name or not column_name:
            return jsonify({
                "success": False,
                "error": "테이블 이름과 칼럼 이름을 입력해주세요."
            }), 400
        
        system = get_welding_system()
        result = system.delete_column_data(table_name, column_name)
        
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/db/dashboard_info', methods=['GET'])
def api_db_dashboard_info():
    """데이터베이스 대시보드 정보 조회"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 테이블 목록
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        table_count = len(tables)
        
        # 각 테이블의 필드 정보 수집
        table_details = []
        field_count = 0
        
        for table in tables:
            cursor.execute(f"PRAGMA table_info({table})")
            columns = cursor.fetchall()
            field_count += len(columns)
            
            # 각 테이블의 행 개수
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            row_count = cursor.fetchone()[0]
            
            # 필드 정보
            fields = []
            for col in columns:
                fields.append({
                    "name": col[1],
                    "type": col[2],
                    "notnull": col[3],
                    "default": col[4],
                    "pk": col[5]
                })
            
            table_details.append({
                "name": table,
                "row_count": row_count,
                "field_count": len(columns),
                "fields": fields
            })
        
        # 데이터베이스 파일 크기
        import os
        db_path = "welding_options.db"
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
            db_size_kb = db_size / 1024
            if db_size_kb < 1024:
                db_size_formatted = f"{db_size_kb:.2f} KB"
            else:
                db_size_mb = db_size_kb / 1024
                db_size_formatted = f"{db_size_mb:.2f} MB"
        else:
            db_size = 0
            db_size_formatted = "0 KB"
        
        # 저장된 코드 목록
        result = system.get_all_combinations()
        saved_codes = result.get("combinations", []) if result.get("success") else []
        saved_codes_count = len(saved_codes)
        
        return jsonify({
            "success": True,
            "database_size": f"{db_size:,} bytes",
            "database_size_formatted": db_size_formatted,
            "table_count": table_count,
            "field_count": field_count,
            "saved_codes_count": saved_codes_count,
            "saved_codes": saved_codes,
            "tables": tables,
            "table_details": table_details
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

CATEGORY_DISPLAY_NAMES_KOR = {
    1: "파워소스",
    2: "타입",
    3: "와이어피더",
    4: "토치",
    5: "호스패키지",
    6: "어스",
    7: "트롤리",
    8: "쿨러",
    9: "쿨란트",
    10: "가스호스",
    11: "용접봉 홀더",
    12: "기타"
}

CATEGORY_DISPLAY_NAMES_ENG = {
    1: "Power Source",
    2: "Type",
    3: "Wire Feeder",
    4: "Torch",
    5: "Hose Package",
    6: "Ground",
    7: "Trolley",
    8: "Cooler",
    9: "Coolant",
    10: "Gas Hose",
    11: "Electrode Holder",
    12: "Other"
}


@app.route('/api/generate_excel', methods=['POST'])
def api_generate_excel():
    """전달받은 데이터로 엑셀 파일 생성 및 다운로드"""
    try:
        print(f"=== 엑셀 생성 API 호출 시작 ===")
        print(f"요청 메서드: {request.method}")
        print(f"요청 헤더: {dict(request.headers)}")
        print(f"요청 데이터 타입: {type(request.json)}")
        
        data = request.json
        if not data:
            print("요청 데이터가 없습니다.")
            return jsonify({"error": "요청 데이터가 없습니다."}), 400
            
        code = data.get('code', '')
        options = data.get('options', {})
        created_at = data.get('created_at', '')
        lang = str(data.get('lang', 'kor')).lower()
        is_eng = lang == 'eng'
        category_display_names = CATEGORY_DISPLAY_NAMES_ENG if is_eng else CATEGORY_DISPLAY_NAMES_KOR
        
        print(f"코드: {code}")
        print(f"옵션 수: {len(options)}")
        print(f"옵션 데이터: {options}")
        
        if not code:
            return jsonify({"error": "코드가 필요합니다."}), 400
        
        # 임시 파일명 생성
        import tempfile
        import os
        temp_dir = tempfile.mkdtemp()
        temp_filename = os.path.join(temp_dir, f"welding_options_{code}.xlsx")
        
        # 엑셀 파일 생성
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from datetime import datetime
        
        wb = Workbook()
        ws = wb.active
        ws.title = "Welding Equipment Options" if is_eng else "웰딩 장비 옵션"
        
        # 헤더 스타일 설정
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center")
        
        # 헤더 작성 (4개 컬럼)
        if is_eng:
            ws['A1'] = 'Category'
            ws['B1'] = 'Selected Option'
            ws['C1'] = 'Code'
            ws['D1'] = 'Description'
        else:
            ws['A1'] = '카테고리'
            ws['B1'] = '선택된 옵션'
            ws['C1'] = '코드'
            ws['D1'] = '설명'
        
        # 헤더 스타일 적용
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
        
        # 전달받은 옵션 데이터로 엑셀 작성
        row = 2
        if options:
            for category_number, option_detail in options.items():
                try:
                    cat_num = int(category_number)
                except (TypeError, ValueError):
                    cat_num = None
                default_category_name = (
                    f"Category {category_number}" if is_eng else f"카테고리 {category_number}"
                )
                category_name = category_display_names.get(cat_num, default_category_name)

                # option_detail이 객체인지 확인
                if isinstance(option_detail, dict):
                    option_name = option_detail.get("option_name", "Option" if is_eng else "옵션")
                    item_code = option_detail.get("item_code", "-")
                    description = option_detail.get("description", "-")
                else:
                    # 단순 문자열인 경우
                    option_name = str(option_detail)
                    item_code = "-"
                    description = "-"
                
                ws[f'A{row}'] = f"[{category_number}] {category_name}"
                ws[f'B{row}'] = option_name
                ws[f'C{row}'] = item_code
                ws[f'D{row}'] = description
                row += 1
        else:
            # 옵션이 없는 경우 기본 데이터
            ws[f'A{row}'] = f"Code: {code}" if is_eng else f"코드: {code}"
            ws[f'B{row}'] = "No data" if is_eng else "데이터 없음"
            ws[f'C{row}'] = code
            ws[f'D{row}'] = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
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
        wb.save(temp_filename)
        
        # 파일을 메모리로 읽어서 응답
        with open(temp_filename, 'rb') as f:
            file_data = f.read()
        
        # 임시 파일 삭제
        os.remove(temp_filename)
        os.rmdir(temp_dir)
        
        # 응답 생성
        from flask import Response
        response = Response(
            file_data,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={
                'Content-Disposition': f'attachment; filename=welding_options_{code}.xlsx'
            }
        )
        
        print(f"엑셀 파일 생성 완료: {code}")
        return response
        
    except Exception as e:
        print(f"엑셀 생성 오류: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/download/<filename>')
def download_file(filename):
    """파일 다운로드 - welding_options_5xxxxxxx.xlsx 패턴의 모든 파일 자동 생성"""
    print(f"=== 다운로드 요청 시작: {filename} ===")
    
    try:
        # 파일 경로 검증
        if not filename.endswith('.xlsx'):
            print("오류: Excel 파일이 아닙니다.")
            return jsonify({"error": "Excel 파일만 다운로드 가능합니다."}), 400
        
        # welding_options_ 패턴 확인
        if not filename.startswith('welding_options_'):
            print("오류: 지원하지 않는 파일 형식입니다.")
            return jsonify({"error": "지원하지 않는 파일 형식입니다."}), 400
        
        print(f"파일 존재 확인: {filename}")
        print(f"현재 작업 디렉토리: {os.getcwd()}")
        print(f"파일 존재 여부: {os.path.exists(filename)}")
        
        # xlsx 폴더 경로로 파일 경로 수정
        xlsx_filename = os.path.join('xlsx', filename)
        
        # 파일이 존재하지 않으면 새로 생성
        if not os.path.exists(xlsx_filename):
            print(f"파일이 존재하지 않습니다: {xlsx_filename}. 새로 생성합니다.")
            
            # xlsx 폴더가 없으면 생성
            if not os.path.exists('xlsx'):
                os.makedirs('xlsx')
                print("xlsx 폴더를 생성했습니다.")
            
            # 파일명에서 코드 추출 (welding_options_5xxxxxxx.xlsx → 5xxxxxxx)
            code = filename.replace('welding_options_', '').replace('.xlsx', '')
            print(f"추출된 코드: {code}")
            
            # 코드 패턴 검증 (5로 시작하는 8자리 숫자)
            if not code.startswith('5') or not code.isdigit() or len(code) != 8:
                print(f"잘못된 코드 형식: {code}. 기본 코드로 대체합니다.")
                code = "50000001"  # 기본 코드
            
            print(f"최종 사용할 코드: {code}")
            
            # 바로 기본 파일 생성으로 진행 (더 안정적)
            print("기본 Excel 파일 생성으로 진행...")
            return create_default_excel(xlsx_filename, code)
        
        # 파일이 존재하는지 다시 확인
        print(f"최종 파일 존재 확인: {os.path.exists(xlsx_filename)}")
        if os.path.exists(xlsx_filename):
            print(f"파일 다운로드 시작: {xlsx_filename}")
            return send_file(
                xlsx_filename,
                as_attachment=True,
                download_name=filename,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
        else:
            print(f"파일 생성 후에도 존재하지 않음: {xlsx_filename}")
            return jsonify({"error": "파일을 생성할 수 없습니다."}), 500
            
    except Exception as e:
        print(f"다운로드 함수 오류: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

def create_default_excel(filename, code=None):
    """실제 조회된 옵션 데이터로 Excel 파일 생성"""
    print(f"=== Excel 파일 생성 시작: {filename} ===")
    
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from datetime import datetime
        
        print(f"Excel 파일 생성 시작: {filename}")
        print(f"현재 작업 디렉토리: {os.getcwd()}")
        
        wb = Workbook()
        ws = wb.active
        ws.title = "웰딩 장비 옵션"
        
        # 코드 추출 (매개변수가 없으면 파일명에서 추출)
        if code is None:
            code = filename.replace('welding_options_', '').replace('.xlsx', '')
        
        print(f"추출된 코드: {code}")
        
        # 코드 패턴 검증 및 정규화
        if not code.startswith('5') or not code.isdigit() or len(code) != 8:
            print(f"잘못된 코드 형식: {code}. 기본 코드로 대체합니다.")
            code = "50000001"
        
        print(f"최종 사용할 코드: {code}")
        
        # 헤더 스타일 설정
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center")
        
        # 헤더 작성 (4개 컬럼)
        ws['A1'] = '카테고리'
        ws['B1'] = '선택된 옵션'
        ws['C1'] = '코드'
        ws['D1'] = '설명'
        
        # 헤더 스타일 적용
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
        
        # 실제 조회된 옵션 데이터 가져오기
        system = get_welding_system()
        lookup_result = system.lookup_combination(code)
        
        row = 2
        if lookup_result.get("found") and lookup_result.get("options"):
            # 실제 조회된 옵션 데이터 사용
            print(f"실제 옵션 데이터 사용: {len(lookup_result['options'])}개 카테고리")
            
            for category_number, option_detail in lookup_result["options"].items():
                # option_detail이 객체인지 확인
                if isinstance(option_detail, dict):
                    category_name = option_detail.get("category_name", f"카테고리 {category_number}")
                    option_name = option_detail.get("option_name", "옵션")
                    item_code = option_detail.get("item_code", "-")
                    description = option_detail.get("description", "-")
                else:
                    # 단순 문자열인 경우
                    category_name = f"카테고리 {category_number}"
                    option_name = str(option_detail)
                    item_code = "-"
                    description = "-"
                
                ws[f'A{row}'] = f"[{category_number}] {category_name}"
                ws[f'B{row}'] = option_name
                ws[f'C{row}'] = item_code
                ws[f'D{row}'] = description
                row += 1
        else:
            # 조회된 데이터가 없는 경우 기본 데이터 사용
            print("조회된 데이터가 없습니다. 기본 데이터를 사용합니다.")
            
            categories = [
                "파워소스", "타입", "와이어피더", "토치", "호스패키지",
                "어스", "트롤리", "쿨러", "쿨란트",
                "가스호스", "용접봉 홀더"
            ]
            
            for i, category_name in enumerate(categories, 1):
                ws[f'A{row}'] = f"[{i}] {category_name}"
                ws[f'B{row}'] = "선택없음"
                ws[f'C{row}'] = "-"
                ws[f'D{row}'] = "-"
                row += 1
        
        # 코드 정보 추가
        ws[f'A{row}'] = f"코드: {code}"
        ws[f'B{row}'] = "자동 생성된 파일"
        ws[f'C{row}'] = code
        ws[f'D{row}'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
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
        print(f"파일 저장 시작: {filename}")
        wb.save(filename)
        print(f"파일 저장 완료: {filename}")
        
        # 파일 존재 확인
        if os.path.exists(filename):
            print(f"파일 생성 성공 확인: {filename}")
            file_size = os.path.getsize(filename)
            print(f"파일 크기: {file_size} bytes")
        else:
            print(f"파일 생성 실패: {filename}")
            return jsonify({"error": "파일 생성에 실패했습니다."}), 500
        
        print(f"기본 Excel 파일 생성 완료: {filename}")
        
        # 파일이 존재하는지 최종 확인
        if os.path.exists(filename):
            print(f"파일 다운로드 시작: {filename}")
            return send_file(
                filename,
                as_attachment=True,
                download_name=filename,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
        else:
            print(f"파일 생성 실패: {filename}")
            return jsonify({"error": "파일 생성에 실패했습니다."}), 500
    except Exception as e:
        print(f"기본 Excel 파일 생성 실패: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"기본 Excel 파일 생성 실패: {str(e)}"}), 500

@app.route('/api/option/add', methods=['POST'])
def api_add_option():
    """옵션 항목 추가 API"""
    try:
        data = request.json
        if not data:
            return jsonify({
                "success": False,
                "error": "요청 데이터가 없습니다."
            }), 400
            
        category_id = data.get('category_id')
        item_name = data.get('item_name')
        item_code = data.get('item_code')
        description = data.get('description')

        def clean_text(value, allow_empty=False):
            if value is None:
                return None
            if isinstance(value, str):
                stripped = value.strip()
                if stripped:
                    return stripped
                return '' if allow_empty else None
            return str(value)

        item_name = clean_text(item_name)
        item_code = clean_text(item_code)
        description = clean_text(description, allow_empty=True)
        
        if not category_id or not item_name:
            return jsonify({
                "success": False,
                "error": "카테고리 ID와 항목 이름을 입력해주세요."
            }), 400
        
        system = get_welding_system()
        # 12번(기타): option_categories에 없으므로 option_base_prices에만 등록 (코드관리 전용)
        try:
            cid = int(category_id) if category_id not in (None, '') else None
        except (TypeError, ValueError):
            cid = None
        if cid == 12:
            option_name = (item_code or item_name or '').strip() or item_name
            if not option_name:
                return jsonify({
                    "success": False,
                    "error": "SAP 코드 또는 코드명을 입력해주세요."
                }), 400
            try:
                cursor = system.connection.cursor()
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
                cursor.execute('''
                    INSERT INTO option_base_prices (category_number, option_name, price, updated_at)
                    VALUES (12, ?, NULL, CURRENT_TIMESTAMP)
                    ON CONFLICT(category_number, option_name) DO NOTHING
                ''', (option_name,))
                if cursor.rowcount == 0:
                    return jsonify({
                        "success": False,
                        "error": f"'{option_name}' 항목이 이미 존재합니다."
                    }), 400
                system.connection.commit()
                return jsonify({
                    "success": True,
                    "message": f"기타 항목 '{item_name}'이(가) 등록되었습니다."
                })
            except Exception as e:
                return jsonify({
                    "success": False,
                    "error": str(e)
                }), 500
        
        result = system.add_option_item(category_id, item_name, item_code, description)
        
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    
    except Exception as e:
        print(f"ERROR in api_add_option: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/option/update', methods=['POST'])
def api_update_option():
    """옵션 항목 수정 API"""
    try:
        data = request.json or {}
        item_id = data.get('item_id')
        
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            return jsonify({
                "success": False,
                "error": "올바른 항목 ID를 입력해주세요."
            }), 400
        
        def normalize_text(value, allow_empty=False):
            if value is None:
                return None
            if isinstance(value, str):
                stripped = value.strip()
                if stripped:
                    return stripped
                return '' if allow_empty else None
            return str(value)
        
        update_kwargs = {}
        
        if 'item_name' in data:
            item_name = normalize_text(data.get('item_name'))
            if item_name is not None:
                update_kwargs['item_name'] = item_name
        
        if 'item_code' in data:
            item_code = normalize_text(data.get('item_code'))
            update_kwargs['item_code'] = item_code
        
        if 'description' in data:
            description = normalize_text(data.get('description'), allow_empty=True)
            update_kwargs['description'] = description
        
        if not update_kwargs:
            return jsonify({
                "success": False,
                "error": "수정할 필드가 없습니다."
            }), 400
        
        system = get_welding_system()
        result = system.update_option_item(item_id, **update_kwargs)
        
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/option/delete', methods=['POST'])
def api_delete_option():
    """옵션 항목 삭제 API (1~11: option_items id, 12번 기타: option_base_prices 삭제)"""
    try:
        data = request.json or {}
        item_id = data.get('item_id')
        category_id = data.get('category_id')
        _opt = data.get('option_code')
        option_code = '' if _opt in (None, '') else str(_opt).strip()
        system = get_welding_system()

        # 12번(기타): option_base_prices에서 option_name(제품코드) 기준 삭제 (category_id는 12 또는 "12" 모두 허용)
        is_category_12 = category_id is not None and (category_id == 12 or str(category_id) == '12')
        if is_category_12:
            if not option_code:
                return jsonify({
                    "success": False,
                    "error": "기타 항목을 선택해주세요."
                }), 400
            try:
                cursor = system.connection.cursor()
                cursor.execute(
                    'DELETE FROM option_base_prices WHERE category_number = 12 AND option_name = ?',
                    (option_code.strip(),)
                )
                system.connection.commit()
                deleted = cursor.rowcount
                if deleted:
                    return jsonify({
                        "success": True,
                        "message": f"기타 항목 '{option_code}'이(가) 삭제되었습니다."
                    })
                return jsonify({
                    "success": False,
                    "error": f"기타 항목 '{option_code}'을(를) 찾을 수 없습니다."
                }), 400
            except Exception as e:
                return jsonify({
                    "success": False,
                    "error": str(e)
                }), 500

        # 1~11번: option_items id 기준 삭제
        if not item_id:
            return jsonify({
                "success": False,
                "error": "항목 ID를 입력해주세요."
            }), 400

        result = system.delete_option_item(item_id)
        if result["success"]:
            return jsonify(result)
        return jsonify(result), 400

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/result')
def result():
    """결과 페이지"""
    return render_template('result.html')

@app.route('/set_price_view')
def set_price_view():
    """세트가격조회 페이지 (로그인 불필요)"""
    lang = request.args.get('lang', 'KOR').upper()
    if lang == 'ENG':
        return render_template('set_price_view_en.html')
    return render_template('set_price_view.html')

@app.route('/set_price_view_pns')
def set_price_view_pns():
    """세트가격조회(PNS) 페이지 (로그인 불필요)"""
    lang = request.args.get('lang', 'KOR').upper()
    if lang == 'ENG':
        return render_template('set_price_view_pns_en.html')
    return render_template('set_price_view_pns.html')

@app.route('/set_price_input')
def set_price_input():
    """세트가격입력 페이지"""
    return render_template('set_price_input.html')

@app.route('/set_price_create')
def set_price_create():
    """세트가격신규생성 페이지"""
    return render_select_options_page('set_price_create.html', view_mode=False, lang='KOR')

@app.route('/export_quotation')
def export_quotation():
    """수출견적서 페이지"""
    return render_template('export_quotation.html')

@app.route('/export_price_dashboard')
def export_price_dashboard():
    """수출가격 대시보드 페이지"""
    return render_template('export_price_dashboard.html')

@app.route('/export_price_dashboard_new')
def export_price_dashboard_new():
    """수출견적서(스태프) 대시보드 페이지"""
    return render_template('export_price_dashboard_new.html')

@app.route('/export_quotation_new')
def export_quotation_new():
    """수출견적서(스태프) 페이지 — 수출부/관리자"""
    lang = request.args.get('lang', 'KOR').upper()
    user_type = _session_user_type()
    if user_type == 'customer':
        return redirect(url_for('export_quotation_new_public', lang=lang))
    return render_template(
        'export_quotation_new.html',
        lang=lang,
        can_view_export_cost=_session_can_view_export_cost(),
    )

@app.route('/export_quotation_new_public')
def export_quotation_new_public():
    """수출견적서(고객) 페이지 — 인증 없이 접근"""
    lang = request.args.get('lang', 'KOR').upper()
    return render_template(
        'export_quotation_new_public.html',
        lang=lang,
        can_view_export_cost=False,
    )


@app.route('/export_label', methods=['GET', 'POST'])
def export_label():
    """EXPORT LABEL — PNS 관리자 전용 (페이지/업로드/다운로드)"""
    if not session.get('pns_admin'):
        if request.method == 'POST' or request.args.get('download') or request.args.get('generate'):
            return jsonify({"success": False, "error": "PNS 관리자 권한이 필요합니다."}), 403
        return redirect(url_for('index'))

    if request.method == 'POST':
        return _handle_export_label_upload()

    if request.args.get('download'):
        return _handle_export_label_download()

    if request.args.get('generate'):
        return _handle_export_label_generate()

    lang = request.args.get('lang', 'KOR').upper()
    system = get_welding_system()
    template_info = None
    upload_history = []
    if system:
        cursor = system.connection.cursor()
        ensure_export_label_tables(cursor)
        migrate_existing_template(cursor)
        system.connection.commit()
        upload_history = list_uploads(cursor)
        active = next((item for item in upload_history if item.get('is_active')), None)
        if active:
            template_info = {
                'id': active['id'],
                'filename': active['original_filename'],
                'updated_at': active['uploaded_at'],
                'size_kb': active['size_kb'],
                'stock_codes': active.get('stock_codes') or [],
                'stock_count': active.get('stock_count') or 0,
            }
        else:
            filepath = active_template_path()
            if os.path.isfile(filepath):
                template_info = {
                    'id': None,
                    'filename': os.path.basename(filepath),
                    'updated_at': datetime.fromtimestamp(os.path.getmtime(filepath)).strftime('%Y-%m-%d %H:%M'),
                    'size_kb': round(os.path.getsize(filepath) / 1024, 1),
                    'stock_codes': [],
                    'stock_count': 0,
                }
    return render_template(
        'export_label.html',
        lang=lang,
        template_info=template_info,
        upload_history=upload_history,
    )


def _export_label_dir():
    return export_label_base_dir()


def _find_export_label_file():
    return active_template_path()


def _export_label_filepath():
    return active_template_path()


def _export_label_mimetype(filepath):
    if filepath.lower().endswith('.doc'):
        return 'application/msword'
    return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


def _is_pns_admin_session() -> bool:
    if session.get('pns_admin') or session.get('pns_authenticated'):
        return True
    if session.get('tracking_user_type') == 'admin':
        return True
    return (session.get('tracking_user_id') or '').strip().lower() == 'pns'


def _require_pns_admin_json():
    if not _is_pns_admin_session():
        return jsonify({"success": False, "error": "PNS 관리자 권한이 필요합니다."}), 403
    return None


def _get_export_label_upload_id(explicit_id=None):
    if explicit_id:
        return str(explicit_id).strip() or None
    upload_id = (request.args.get('upload_id') or request.form.get('upload_id') or '').strip()
    return upload_id or None


def _handle_export_label_download(upload_id=None):
    upload_id = _get_export_label_upload_id(upload_id)
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류가 발생했습니다."}), 500
        cursor = system.connection.cursor()
        ensure_export_label_tables(cursor)
        filepath, record = resolve_template_filepath(cursor, upload_id)
        if not filepath or not os.path.isfile(filepath):
            return jsonify({"success": False, "error": "등록된 워드 파일이 없습니다."}), 404
        download_name = (record or {}).get('original_filename') or os.path.basename(filepath)
        return send_file(
            filepath,
            as_attachment=True,
            download_name=download_name,
            mimetype=_export_label_mimetype(filepath),
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def _handle_export_label_generate(upload_id=None):
    upload_id = _get_export_label_upload_id(upload_id)
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류가 발생했습니다."}), 500

        cursor = system.connection.cursor()
        ensure_export_label_tables(cursor)
        filepath, record = resolve_template_filepath(cursor, upload_id)
        if not filepath or not os.path.isfile(filepath):
            return jsonify({"success": False, "error": "등록된 워드 템플릿이 없습니다. 먼저 워드 파일을 업로드해 주세요."}), 404
        if filepath.lower().endswith('.doc') and not filepath.lower().endswith('.docx'):
            return jsonify({
                "success": False,
                "error": "라벨 생성은 .docx 파일만 지원합니다. .docx 형식으로 업로드해 주세요."
            }), 400

        labels, error = build_labels_from_template(cursor, filepath)
        if error:
            return jsonify({"success": False, "error": error}), 404

        docx_bytes = generate_label_docx(labels, template_filepath=filepath)
        suffix = record['id'][:8] if record and record.get('id') else datetime.now().strftime('%Y%m%d_%H%M%S')
        download_name = f"export_labels_{suffix}.docx"
        return send_file(
            io.BytesIO(docx_bytes),
            as_attachment=True,
            download_name=download_name,
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )
    except Exception as e:
        import traceback
        print(f"Error in export_label generate: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500


def _handle_export_label_upload():
    try:
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "파일이 없습니다."}), 400
        file = request.files['file']
        if not file or file.filename == '':
            return jsonify({"success": False, "error": "파일이 선택되지 않았습니다."}), 400

        raw_filename = (file.filename or '').strip()
        file_ext = os.path.splitext(raw_filename)[1].lower()
        if file_ext not in EXPORT_LABEL_ALLOWED_EXT:
            return jsonify({
                "success": False,
                "error": "워드 파일(.docx, .doc)만 업로드 가능합니다."
            }), 400

        original_name = sanitize_upload_filename(raw_filename, file_ext or '.docx')

        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        if file_size > 50 * 1024 * 1024:
            return jsonify({
                "success": False,
                "error": "파일 크기가 너무 큽니다. 최대 50MB까지 업로드 가능합니다."
            }), 400

        templates_dir = _export_label_dir()
        target_name = EXPORT_LABEL_BASENAME + file_ext
        filepath = os.path.join(templates_dir, target_name)
        import tempfile
        import shutil
        fd, tmppath = tempfile.mkstemp(suffix=file_ext, dir=templates_dir)
        try:
            os.close(fd)
            file.save(tmppath)
            for ext in EXPORT_LABEL_ALLOWED_EXT:
                old = os.path.join(templates_dir, EXPORT_LABEL_BASENAME + ext)
                if os.path.isfile(old) and old != filepath:
                    try:
                        os.remove(old)
                    except PermissionError:
                        pass
            replaced = False
            for attempt in range(3):
                try:
                    if os.path.isfile(filepath):
                        try:
                            os.remove(filepath)
                        except PermissionError:
                            pass
                    os.replace(tmppath, filepath)
                    replaced = True
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(0.4)
            if not replaced:
                try:
                    if os.path.isfile(filepath):
                        try:
                            os.remove(filepath)
                        except PermissionError:
                            pass
                    shutil.move(tmppath, filepath)
                    replaced = True
                except Exception:
                    pass
                if not replaced:
                    try:
                        os.remove(tmppath)
                    except Exception:
                        pass
                    return jsonify({
                        "success": False,
                        "error": "파일이 다른 프로그램(예: Word)에서 열려 있을 수 있습니다. 파일을 닫은 후 다시 업로드해 주세요."
                    }), 409
        except Exception:
            if os.path.isfile(tmppath):
                try:
                    os.remove(tmppath)
                except Exception:
                    pass
            raise

        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류가 발생했습니다."}), 500

        cursor = system.connection.cursor()
        uploaded_by = session.get('pns_admin_user') or session.get('username') or 'pns'
        record = register_upload(
            cursor,
            saved_filepath=filepath,
            original_filename=original_name,
            file_ext=file_ext,
            size_bytes=file_size,
            uploaded_by=uploaded_by,
            set_as_active=True,
        )
        system.connection.commit()

        return jsonify({
            "success": True,
            "message": "EXPORT LABEL 워드 파일이 업로드되었습니다.",
            "filename": original_name,
            "original_name": original_name,
            "size_kb": round(file_size / 1024, 1),
            "upload": record,
        })
    except Exception as e:
        import traceback
        print(f"Error in export_label upload: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/export_label/word', methods=['GET', 'POST'])
def api_export_label_word():
    """EXPORT LABEL 워드 템플릿 다운로드(GET) / 업로드(POST)"""
    denied = _require_pns_admin_json()
    if denied:
        return denied

    if request.method == 'GET':
        return _handle_export_label_download()
    return _handle_export_label_upload()


@app.route('/api/export_label/generate', methods=['GET'])
def api_export_label_generate():
    """EXPORT LABEL 생성 다운로드 — Stock Code 기반 라벨 워드 생성"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    return _handle_export_label_generate()


@app.route('/api/export_label/history', methods=['GET'])
def api_export_label_history():
    """EXPORT LABEL 업로드 이력 목록"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        ensure_export_label_tables(cursor)
        migrate_existing_template(cursor)
        system.connection.commit()
        stock_code = (request.args.get('stock_code') or request.args.get('q') or '').strip()
        items = list_uploads(cursor, stock_code=stock_code or None)
        return jsonify({"success": True, "items": items, "stock_code": stock_code})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/export_label/history/<upload_id>', methods=['GET', 'DELETE'])
def api_export_label_history_item(upload_id):
    """EXPORT LABEL 이력 상세 / 삭제"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        ensure_export_label_tables(cursor)

        if request.method == 'DELETE':
            record = get_upload_row(cursor, upload_id)
            if not record:
                return jsonify({"success": False, "error": "이력을 찾을 수 없습니다."}), 404
            if record.get('is_active'):
                return jsonify({"success": False, "error": "현재 사용 중인 파일은 삭제할 수 없습니다. 다른 파일을 활성화한 후 삭제해 주세요."}), 400
            delete_upload(cursor, upload_id)
            system.connection.commit()
            return jsonify({"success": True, "message": "업로드 이력이 삭제되었습니다."})

        record = get_upload(cursor, upload_id)
        if not record:
            return jsonify({"success": False, "error": "이력을 찾을 수 없습니다."}), 404
        safe = dict(record)
        safe.pop('filepath', None)
        return jsonify({"success": True, "item": safe})
    except Exception as e:
        try:
            system = get_welding_system()
            if system:
                system.connection.rollback()
        except Exception:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/export_label/history/<upload_id>/download', methods=['GET'])
def api_export_label_history_download(upload_id):
    """EXPORT LABEL 이력 파일 다운로드"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    return _handle_export_label_download(upload_id)


@app.route('/api/export_label/history/<upload_id>/generate', methods=['GET'])
def api_export_label_history_generate(upload_id):
    """EXPORT LABEL 이력 파일로 라벨 생성 다운로드"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    return _handle_export_label_generate(upload_id)


@app.route('/api/export_label/history/<upload_id>/activate', methods=['POST'])
def api_export_label_history_activate(upload_id):
    """EXPORT LABEL 이력 파일을 현재 템플릿으로 재사용"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        ensure_export_label_tables(cursor)
        record = activate_upload(cursor, upload_id)
        if not record:
            row = get_upload_row(cursor, upload_id)
            if row:
                return jsonify({"success": False, "error": "이력 파일을 찾을 수 없어 현재 파일로 설정할 수 없습니다."}), 404
            return jsonify({"success": False, "error": "이력을 찾을 수 없습니다."}), 404
        system.connection.commit()
        safe = dict(record)
        safe.pop('filepath', None)
        return jsonify({
            "success": True,
            "message": "선택한 파일이 현재 템플릿으로 설정되었습니다.",
            "item": safe,
        })
    except Exception as e:
        try:
            system = get_welding_system()
            if system:
                system.connection.rollback()
        except Exception:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/pricebook_page_upload')
def pricebook_page_upload():
    """프라이스북 공통 1페이지 / 마지막페이지 등록 — PNS 관리자 전용"""
    if not _is_pns_admin_session():
        return redirect(url_for('index'))
    try:
        system = get_welding_system()
        if system:
            cursor = system.connection.cursor()
            ensure_pricebook_page_tables(cursor)
            system.connection.commit()
    except Exception:
        pass
    return render_template('pricebook_page_upload.html')


@app.route('/api/pricebook_pages/folders', methods=['GET'])
def api_pricebook_pages_folders():
    """옵션조회 [1] 파워소스 목록 (공통 페이지 적용 대상)"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        ensure_pricebook_page_tables(cursor)
        system.connection.commit()
        folders = list_pricebook_power_source_folders(cursor)
        return jsonify({"success": True, "folders": folders})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/pricebook_pages', methods=['GET'])
def api_pricebook_pages_list():
    """프라이스북 페이지 목록 — 공통(first/last) 또는 파워소스 이미지(content)"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        variant = request.args.get('variant') or 'std'
        option_item_id = request.args.get('option_item_id', type=int)
        page_type = request.args.get('page_type')
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        ensure_pricebook_page_tables(cursor)
        system.connection.commit()

        # 파워소스별 이미지 / 하단악세서리
        if option_item_id:
            req_page_type = (page_type or '').strip().lower()
            if req_page_type in ('accessory', 'accessories', 'bottom_accessory'):
                images = list_pricebook_accessory_images(cursor, option_item_id)
                resolved_type = 'accessory'
            else:
                images = list_pricebook_content_images(cursor, option_item_id, variant=variant)
                resolved_type = 'content'
            for page in images:
                page['url'] = url_for('api_pricebook_page_file', page_id=page['id'])
                page.pop('filepath', None)
            return jsonify({
                "success": True,
                "option_item_id": option_item_id,
                "variant": variant,
                "page_type": resolved_type,
                "images": images,
            })

        # 공통 1/마지막 페이지
        pages = list_pricebook_pages(cursor, variant=variant, option_item_id=None)
        if page_type:
            pages = [p for p in pages if p.get('page_type') == page_type]
        for page in pages:
            page['url'] = url_for('api_pricebook_page_file', page_id=page['id'])
            page.pop('filepath', None)
        latest = get_pricebook_global_latest(cursor, variant=variant)
        for key in ('first', 'last'):
            item = latest.get(key)
            if item:
                item['url'] = url_for('api_pricebook_page_file', page_id=item['id'])
                item.pop('filepath', None)
        return jsonify({
            "success": True,
            "variant": variant,
            "pages": pages,
            "latest": latest,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/pricebook_pages/upload', methods=['POST'])
def api_pricebook_pages_upload():
    """프라이스북 업로드 — 공통 first/last 또는 파워소스 content 이미지"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        raw_option_id = request.form.get('option_item_id') or request.args.get('option_item_id')
        option_item_id = None
        if raw_option_id not in (None, ''):
            try:
                option_item_id = int(raw_option_id)
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "option_item_id가 올바르지 않습니다."}), 400

        page_type = request.form.get('page_type') or request.form.get('type') or ''
        variant = request.form.get('variant') or 'std'
        file = request.files.get('file')

        # 파워소스 폴더 업로드: accessory 는 유지, 그 외는 content
        if option_item_id:
            normalized_pt = (page_type or '').strip().lower()
            if normalized_pt in ('accessory', 'accessories', 'bottom_accessory', '하단악세서리'):
                page_type = 'accessory'
            else:
                page_type = 'content'
        elif not page_type:
            return jsonify({"success": False, "error": "page_type이 필요합니다."}), 400

        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        uploaded_by = session.get('pns_admin_user') or session.get('username') or 'pns'
        saved, error = save_pricebook_page(
            cursor,
            page_type,
            file,
            uploaded_by=uploaded_by,
            variant=variant,
            option_item_id=option_item_id,
        )
        if error:
            return jsonify({"success": False, "error": error}), 400
        system.connection.commit()
        saved['url'] = url_for('api_pricebook_page_file', page_id=saved['id'])
        saved.pop('filepath', None)
        return jsonify({
            "success": True,
            "message": f"{saved.get('page_label')} 파일이 업로드되었습니다.",
            "page": saved,
        })
    except Exception as e:
        import traceback
        print(f"Error in api_pricebook_pages_upload: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/pricebook_pages/<int:page_id>', methods=['DELETE'])
def api_pricebook_pages_delete(page_id):
    """프라이스북 페이지 삭제"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        if not delete_pricebook_page(cursor, page_id):
            return jsonify({"success": False, "error": "파일을 찾을 수 없습니다."}), 404
        system.connection.commit()
        return jsonify({"success": True, "message": "삭제되었습니다."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/pricebook_pages/file/<int:page_id>', methods=['GET'])
def api_pricebook_page_file(page_id):
    """프라이스북 페이지 파일"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        record = get_pricebook_page_record(cursor, page_id)
        if not record or not os.path.isfile(record['filepath']):
            return jsonify({"success": False, "error": "파일을 찾을 수 없습니다."}), 404
        return send_file(record['filepath'], download_name=record['filename'])
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/export_quotation/pricebook_pdf', methods=['POST', 'OPTIONS'])
def api_export_quotation_pricebook_pdf():
    """수출견적서 PRICE BOOK PDF 다운로드 (1페이지 + 중간 이미지/가격표 + 마지막페이지)"""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        data = request.get_json(silent=True) or {}
        items = data.get('items') or data.get('products') or []
        if not isinstance(items, list) or not items:
            return jsonify({
                "success": False,
                "error": "다운로드할 제품이 없습니다. 제품을 먼저 추가해 주세요."
            }), 400

        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        buffer, error = build_pricebook_pdf(cursor, items)
        if error:
            return jsonify({"success": False, "error": error}), 400
        return _pdf_file_response(buffer, 'PNS_Price_Book.pdf')
    except Exception as e:
        import traceback
        print(f"Error in api_export_quotation_pricebook_pdf: {traceback.format_exc()}")
        return jsonify({"success": False, "error": f"PDF 생성 오류: {str(e)}"}), 500


@app.route('/api/export_quotation/upload', methods=['POST'])
def api_upload_export_quotation():
    """수출견적서 가격 엑셀 업로드 API"""
    try:
        if 'file' not in request.files:
            return jsonify({
                "success": False,
                "error": "파일이 없습니다."
            }), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({
                "success": False,
                "error": "파일이 선택되지 않았습니다."
            }), 400
        
        if not file.filename.endswith(('.xlsx', '.xls')):
            return jsonify({
                "success": False,
                "error": "엑셀 파일(.xlsx, .xls)만 업로드 가능합니다."
            }), 400
        
        # 엑셀 파일 읽기
        from openpyxl import load_workbook
        from io import BytesIO
        
        file_data = file.read()
        file_stream = BytesIO(file_data)
        wb = load_workbook(file_stream, data_only=True)
        
        # 첫 번째 시트 사용
        if len(wb.sheetnames) > 0:
            ws = wb[wb.sheetnames[0]]
        else:
            ws = wb.active
        
        # 헤더 행 찾기
        header_row = None
        max_row = ws.max_row if ws.max_row else 100
        max_col = ws.max_column if ws.max_column else 10
        
        for row_idx in range(1, min(20, max_row + 1)):
            for col_idx in range(1, max_col + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                if cell.value:
                    cell_str = str(cell.value).strip()
                    # 제품코드, 제품명, 프라이스북 가격 등의 키워드로 헤더 찾기
                    if any(keyword in cell_str for keyword in ['제품코드', '제품명', '프라이스북', 'Price Book', 'Product Code', 'Product Name', '원가', 'Cost']):
                        header_row = row_idx
                        break
            if header_row:
                break
        
        if not header_row:
            # 헤더를 찾지 못한 경우 첫 번째 행을 헤더로 간주
            header_row = 1
        
        # 헤더에서 컬럼 인덱스 찾기
        code_col = None
        name_col = None
        price_col = None
        quantity_col = None
        cost_col = None
        
        for col_idx in range(1, max_col + 1):
            cell = ws.cell(row=header_row, column=col_idx)
            if cell.value:
                cell_str = str(cell.value).strip().lower()
                if '제품코드' in cell_str or 'product code' in cell_str or ('code' in cell_str and 'price' not in cell_str):
                    code_col = col_idx
                elif '제품명' in cell_str or 'product name' in cell_str or ('name' in cell_str and 'product' in cell_str):
                    name_col = col_idx
                elif '프라이스북' in cell_str or 'price book' in cell_str or ('price' in cell_str and 'book' in cell_str):
                    price_col = col_idx
                elif '수량' in cell_str or 'quantity' in cell_str or 'qty' in cell_str:
                    quantity_col = col_idx
                elif '원가' in cell_str or ('cost' in cell_str and 'total' not in cell_str):
                    cost_col = col_idx
        
        # 데이터 읽기
        products = []
        for row_idx in range(header_row + 1, max_row + 1):
            try:
                code = None
                name = None
                price_book_price = None
                quantity = 1
                product_cost = None
                
                if code_col:
                    code_cell = ws.cell(row=row_idx, column=code_col)
                    code = str(code_cell.value).strip() if code_cell.value else None
                
                if name_col:
                    name_cell = ws.cell(row=row_idx, column=name_col)
                    name = str(name_cell.value).strip() if name_cell.value else None
                
                if price_col:
                    price_cell = ws.cell(row=row_idx, column=price_col)
                    if price_cell.value:
                        try:
                            price_book_price = float(price_cell.value)
                        except (ValueError, TypeError):
                            pass
                
                if quantity_col:
                    qty_cell = ws.cell(row=row_idx, column=quantity_col)
                    if qty_cell.value:
                        try:
                            quantity = int(qty_cell.value)
                        except (ValueError, TypeError):
                            quantity = 1
                
                if cost_col:
                    cost_cell = ws.cell(row=row_idx, column=cost_col)
                    if cost_cell.value:
                        try:
                            product_cost = float(cost_cell.value)
                        except (ValueError, TypeError):
                            pass
                
                # 제품코드나 제품명이 있어야 추가
                if code or name:
                    products.append({
                        'code': code or '',
                        'name': name or '',
                        'priceBookPrice': price_book_price or 0,
                        'quantity': quantity,
                        'productCost': product_cost or 0
                    })
            except Exception as e:
                print(f"행 {row_idx} 처리 오류: {e}")
                continue
        
        # 데이터베이스에 제품 정보 저장
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 테이블이 존재하는지 확인하고 없으면 생성
        try:
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
            system.connection.commit()
        except Exception as e:
            print(f"테이블 생성 확인 오류: {e}")
        
        saved_count = 0
        updated_count = 0
        for product in products:
            try:
                # 제품코드로 기존 제품 확인
                cursor.execute('''
                    SELECT id FROM export_quotation_products 
                    WHERE product_code = ?
                ''', (product['code'],))
                existing = cursor.fetchone()
                
                if existing:
                    # 기존 제품 업데이트
                    cursor.execute('''
                        UPDATE export_quotation_products
                        SET product_name = ?,
                            price_book_price = ?,
                            product_cost = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE product_code = ?
                    ''', (product['name'], product['priceBookPrice'], product['productCost'], product['code']))
                    updated_count += 1
                else:
                    # 새 제품 추가
                    cursor.execute('''
                        INSERT INTO export_quotation_products 
                        (product_code, product_name, price_book_price, product_cost)
                        VALUES (?, ?, ?, ?)
                    ''', (product['code'], product['name'], product['priceBookPrice'], product['productCost']))
                    saved_count += 1
            except Exception as e:
                print(f"제품 저장 오류: {e}")
                continue
        
        system.connection.commit()
        
        # 메시지 구성
        message = f"{len(products)}개의 제품이 업로드되었습니다."
        if saved_count > 0:
            message += f" (신규 등록: {saved_count}개"
        if updated_count > 0:
            if saved_count > 0:
                message += f", 업데이트: {updated_count}개)"
            else:
                message += f" (업데이트: {updated_count}개)"
        elif saved_count > 0:
            message += ")"
        
        return jsonify({
            "success": True,
            "message": message,
            "saved_count": saved_count,
            "updated_count": updated_count,
            "total_count": len(products),
            "products": products
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_upload_export_quotation: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_quotation/new/upload', methods=['POST'])
def api_upload_export_quotation_new():
    """수출견적서(스태프) 가격 엑셀 업로드 API - 제품코드, 제품명, 프라이스북 단가, 구매가 원가"""
    try:
        if 'file' not in request.files:
            return jsonify({
                "success": False,
                "error": "파일이 없습니다."
            }), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({
                "success": False,
                "error": "파일이 선택되지 않았습니다."
            }), 400
        
        if not file.filename.endswith(('.xlsx', '.xls')):
            return jsonify({
                "success": False,
                "error": "엑셀 파일(.xlsx, .xls)만 업로드 가능합니다."
            }), 400
        
        # 엑셀 파일 읽기
        from openpyxl import load_workbook
        from io import BytesIO
        
        file_data = file.read()
        file_stream = BytesIO(file_data)
        wb = load_workbook(file_stream, data_only=True)
        
        # 첫 번째 시트 사용
        if len(wb.sheetnames) > 0:
            ws = wb[wb.sheetnames[0]]
        else:
            ws = wb.active
        
        # 헤더 행 찾기
        header_row = None
        max_row = ws.max_row if ws.max_row else 100
        max_col = ws.max_column if ws.max_column else 10
        
        for row_idx in range(1, min(20, max_row + 1)):
            for col_idx in range(1, max_col + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                if cell.value:
                    cell_str = str(cell.value).strip()
                    # 제품코드, 제품명, 프라이스북 단가, 구매가 원가, 분류 등의 키워드로 헤더 찾기
                    if any(keyword in cell_str for keyword in ['제품코드', '제품명', '프라이스북', '구매가', '분류', 'Product Code', 'Product Name', 'Price Book', 'Purchase Cost', 'Purchase Price', 'Category']):
                        header_row = row_idx
                        break
            if header_row:
                break
        
        if not header_row:
            # 헤더를 찾지 못한 경우 첫 번째 행을 헤더로 간주
            header_row = 1
        
        # 헤더에서 컬럼 인덱스 찾기
        code_col = None
        name_col = None
        price_book_col = None
        purchase_price_col = None
        category_col = None
        
        for col_idx in range(1, max_col + 1):
            cell = ws.cell(row=header_row, column=col_idx)
            if cell.value:
                cell_str = str(cell.value).strip().lower()
                if '제품코드' in cell_str or 'product code' in cell_str or ('code' in cell_str and 'price' not in cell_str):
                    code_col = col_idx
                elif '제품명' in cell_str or 'product name' in cell_str or ('name' in cell_str and 'product' in cell_str):
                    name_col = col_idx
                elif '분류' in cell_str or 'category' in cell_str:
                    category_col = col_idx
                elif '프라이스북' in cell_str or 'price book' in cell_str or ('price' in cell_str and 'book' in cell_str):
                    price_book_col = col_idx
                elif '구매가' in cell_str or 'purchase cost' in cell_str or 'purchase price' in cell_str or ('purchase' in cell_str and ('cost' in cell_str or 'price' in cell_str)):
                    purchase_price_col = col_idx
        
        # 데이터 읽기
        products = []
        for row_idx in range(header_row + 1, max_row + 1):
            try:
                code = None
                name = None
                price_book_price = None
                purchase_price = None
                category_number = None
                
                if code_col:
                    code_cell = ws.cell(row=row_idx, column=code_col)
                    code = str(code_cell.value).strip() if code_cell.value else None
                
                if name_col:
                    name_cell = ws.cell(row=row_idx, column=name_col)
                    name = str(name_cell.value).strip() if name_cell.value else None
                
                if category_col:
                    category_cell = ws.cell(row=row_idx, column=category_col)
                    if category_cell.value:
                        category_str = str(category_cell.value).strip()
                        # "[1] 파워소스" 형식에서 숫자 추출
                        import re
                        match = re.search(r'\[(\d+)\]', category_str)
                        if match:
                            try:
                                category_number = int(match.group(1))
                            except (ValueError, TypeError):
                                pass
                        else:
                            # 숫자만 있는 경우
                            try:
                                category_number = int(category_str)
                            except (ValueError, TypeError):
                                pass
                
                if price_book_col:
                    price_book_cell = ws.cell(row=row_idx, column=price_book_col)
                    if price_book_cell.value:
                        try:
                            price_book_price = float(price_book_cell.value)
                        except (ValueError, TypeError):
                            pass
                
                if purchase_price_col:
                    purchase_price_cell = ws.cell(row=row_idx, column=purchase_price_col)
                    if purchase_price_cell.value:
                        try:
                            purchase_price = float(purchase_price_cell.value)
                        except (ValueError, TypeError):
                            pass
                
                # 제품코드나 제품명이 있어야 추가
                if code or name:
                    products.append({
                        'code': code or '',
                        'name': name or '',
                        'priceBookPrice': price_book_price or 0,
                        'purchasePrice': purchase_price or 0,
                        'categoryNumber': category_number
                    })
            except Exception as e:
                print(f"행 {row_idx} 처리 오류: {e}")
                continue
        
        # 데이터베이스에 제품 정보 저장
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 테이블이 존재하는지 확인하고 없으면 생성
        try:
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
            system.connection.commit()
            
            # 기존 테이블에 컬럼 추가 (없는 경우)
            try:
                cursor.execute('ALTER TABLE export_quotation_new_products ADD COLUMN purchase_price REAL')
                system.connection.commit()
            except Exception as e:
                pass
            
            try:
                cursor.execute('ALTER TABLE export_quotation_new_products ADD COLUMN category_number INTEGER')
                system.connection.commit()
            except Exception as e:
                # 컬럼이 이미 존재하는 경우 무시
                if 'duplicate column' not in str(e).lower() and 'already exists' not in str(e).lower():
                    print(f"category_number 컬럼 추가 오류 (무시 가능): {e}")
            
            try:
                cursor.execute('ALTER TABLE export_quotation_new_products ADD COLUMN price_book_price REAL')
                system.connection.commit()
                
                # 컬럼 존재 여부 확인
                cursor.execute("PRAGMA table_info(export_quotation_new_products)")
                columns = cursor.fetchall()
                column_names = [col[1] for col in columns]
                
                # price 컬럼이 있는 경우에만 마이그레이션
                if 'price' in column_names:
                    try:
                        cursor.execute('UPDATE export_quotation_new_products SET price_book_price = price WHERE price_book_price IS NULL AND price IS NOT NULL')
                        system.connection.commit()
                    except Exception:
                        pass
                
                # unit_price 컬럼이 있는 경우에만 마이그레이션
                if 'unit_price' in column_names:
                    try:
                        cursor.execute('UPDATE export_quotation_new_products SET price_book_price = unit_price WHERE price_book_price IS NULL AND unit_price IS NOT NULL')
                        system.connection.commit()
                    except Exception:
                        pass
            except Exception as e:
                pass
            
            # 기존 데이터 마이그레이션 (product_cost -> purchase_price)
            # 컬럼 존재 여부 확인 후 마이그레이션
            try:
                cursor.execute("PRAGMA table_info(export_quotation_new_products)")
                columns = cursor.fetchall()
                column_names = [col[1] for col in columns]
                
                # product_cost 컬럼이 있는 경우에만 마이그레이션
                if 'product_cost' in column_names:
                    try:
                        cursor.execute('UPDATE export_quotation_new_products SET purchase_price = product_cost WHERE purchase_price IS NULL AND product_cost IS NOT NULL')
                        system.connection.commit()
                    except Exception:
                        pass
            except Exception as e:
                pass
        except Exception as e:
            print(f"테이블 생성 확인 오류: {e}")
        
        # option_base_prices 테이블이 없으면 생성
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
        
        saved_count = 0
        updated_count = 0
        option_price_updated_count = 0
        
        for product in products:
            try:
                # 제품코드로 기존 제품 확인
                cursor.execute('''
                    SELECT id FROM export_quotation_new_products 
                    WHERE product_code = ?
                ''', (product['code'],))
                existing = cursor.fetchone()
                
                if existing:
                    # 기존 제품 업데이트
                    cursor.execute('''
                        UPDATE export_quotation_new_products
                        SET product_name = ?,
                            price_book_price = ?,
                            purchase_price = ?,
                            category_number = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE product_code = ?
                    ''', (product['name'], product['priceBookPrice'], product['purchasePrice'], product.get('categoryNumber'), product['code']))
                    updated_count += 1
                else:
                    # 새 제품 추가
                    cursor.execute('''
                        INSERT INTO export_quotation_new_products 
                        (product_code, product_name, price_book_price, purchase_price, category_number)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (product['code'], product['name'], product['priceBookPrice'], product['purchasePrice'], product.get('categoryNumber')))
                    saved_count += 1
                
                # 제품 코드에서 옵션 추출 및 가격 분배
                if product['priceBookPrice'] and product['priceBookPrice'] > 0:
                    # 제품 코드로 옵션 조회 (combination_code 또는 set_code로 검색)
                    import json
                    selected_options = {}
                    
                    # 먼저 combination_code로 검색
                    cursor.execute('''
                        SELECT selected_options
                        FROM selected_combinations
                        WHERE combination_code = ? OR set_code = ?
                        LIMIT 1
                    ''', (product['code'], product['code']))
                    
                    option_row = cursor.fetchone()
                    if option_row:
                        try:
                            if hasattr(option_row, 'keys') and callable(getattr(option_row, 'keys', None)):
                                selected_options_str = option_row['selected_options'] if 'selected_options' in option_row.keys() else (option_row[0] if len(option_row) > 0 else None)
                            else:
                                selected_options_str = option_row[0] if len(option_row) > 0 else None
                            
                            if selected_options_str:
                                selected_options = json.loads(selected_options_str) if selected_options_str else {}
                        except (json.JSONDecodeError, TypeError, AttributeError) as e:
                            print(f"옵션 파싱 오류 (제품코드: {product['code']}): {e}")
                    
                    # 옵션이 있으면 가격 분배
                    if selected_options:
                        # 유효한 옵션 개수 계산 (값이 있고 '선택없음'이 아닌 것)
                        valid_options = {k: v for k, v in selected_options.items() if v and v != '선택없음' and str(v).strip()}
                        option_count = len(valid_options)
                        
                        if option_count > 0:
                            # 제품 가격을 옵션 개수로 나누어 각 옵션의 기본 가격 계산
                            price_per_option = product['priceBookPrice'] / option_count
                            
                            # 각 옵션의 기본 가격을 option_base_prices에 저장/업데이트
                            for category_num_str, option_name in valid_options.items():
                                try:
                                    category_number = int(category_num_str)
                                    # 12번(기타)은 코드관리에서만 등록·표시. 엑셀 업로드 시 option_base_prices에 넣지 않음
                                    if category_number == 12:
                                        continue
                                    # 기존 가격 확인
                                    cursor.execute('''
                                        SELECT id, price FROM option_base_prices
                                        WHERE category_number = ? AND option_name = ?
                                    ''', (category_number, option_name))
                                    
                                    existing_price = cursor.fetchone()
                                    
                                    if existing_price:
                                        # 기존 가격 업데이트 (더 큰 값으로 업데이트 또는 평균 계산)
                                        # 여기서는 업로드된 가격으로 업데이트
                                        cursor.execute('''
                                            UPDATE option_base_prices
                                            SET price = ?, updated_at = CURRENT_TIMESTAMP
                                            WHERE category_number = ? AND option_name = ?
                                        ''', (price_per_option, category_number, option_name))
                                    else:
                                        # 새 가격 추가
                                        cursor.execute('''
                                            INSERT INTO option_base_prices (category_number, option_name, price, updated_at)
                                            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                                        ''', (category_number, option_name, price_per_option))
                                    
                                    option_price_updated_count += 1
                                except (ValueError, TypeError) as e:
                                    print(f"카테고리 번호 변환 오류: {category_num_str}, {e}")
                                    continue
            except Exception as e:
                print(f"제품 저장 오류: {e}")
                continue
        
        system.connection.commit()
        
        # 메시지 구성
        message = f"{len(products)}개의 제품이 업로드되었습니다."
        if saved_count > 0 or updated_count > 0 or option_price_updated_count > 0:
            message += " ("
            parts = []
            if saved_count > 0:
                parts.append(f"신규 등록: {saved_count}개")
            if updated_count > 0:
                parts.append(f"업데이트: {updated_count}개")
            if option_price_updated_count > 0:
                parts.append(f"옵션 가격 업데이트: {option_price_updated_count}개")
            message += ", ".join(parts) + ")"
        
        return jsonify({
            "success": True,
            "message": message,
            "saved_count": saved_count,
            "updated_count": updated_count,
            "option_price_updated_count": option_price_updated_count,
            "total_count": len(products),
            "products": products
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_upload_export_quotation_new: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/upload', methods=['POST'])
def api_upload_part_price():
    """파트가격조회 가격 엑셀 업로드 API - 자재코드, 설명, 가격"""
    try:
        print("=== 파트가격조회 가격 업로드 API 호출됨 ===")
        print(f"요청 메서드: {request.method}")
        print(f"요청 Content-Type: {request.content_type}")
        import sys
        sys.stdout.flush()
        
        if 'file' not in request.files:
            return jsonify({
                "success": False,
                "error": "파일이 없습니다."
            }), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({
                "success": False,
                "error": "파일이 선택되지 않았습니다."
            }), 400
        
        if not file.filename.endswith(('.xlsx', '.xls')):
            return jsonify({
                "success": False,
                "error": "엑셀 파일(.xlsx, .xls)만 업로드 가능합니다."
            }), 400
        
        # 엑셀 파일 읽기
        try:
            from openpyxl import load_workbook
            from io import BytesIO
            
            print(f"파일 읽기 시작: {file.filename}")
            file_data = file.read()
            print(f"파일 크기: {len(file_data)} bytes")
            sys.stdout.flush()
            
            if len(file_data) == 0:
                return jsonify({
                    "success": False,
                    "error": "파일이 비어있습니다."
                }), 400
            
            file_stream = BytesIO(file_data)
            print("워크북 로드 시작...")
            sys.stdout.flush()
            wb = load_workbook(file_stream, data_only=True)
            print("워크북 로드 완료")
            sys.stdout.flush()
        except ImportError as e:
            print(f"openpyxl 라이브러리 오류: {e}")
            import traceback
            print(traceback.format_exc())
            sys.stdout.flush()
            return jsonify({
                "success": False,
                "error": f"엑셀 파일 처리 라이브러리 오류: {str(e)}"
            }), 500
        except Exception as e:
            print(f"파일 읽기 오류: {e}")
            import traceback
            print(traceback.format_exc())
            sys.stdout.flush()
            return jsonify({
                "success": False,
                "error": f"파일 읽기 오류: {str(e)}"
            }), 500
        
        # 첫 번째 시트 사용
        try:
            if len(wb.sheetnames) > 0:
                ws = wb[wb.sheetnames[0]]
            else:
                ws = wb.active
            print(f"시트 선택 완료: {ws.title}")
            sys.stdout.flush()
        except Exception as e:
            print(f"시트 선택 오류: {e}")
            import traceback
            print(traceback.format_exc())
            sys.stdout.flush()
            return jsonify({
                "success": False,
                "error": f"시트 선택 오류: {str(e)}"
            }), 500
        
        # 헤더 행 찾기
        header_row = None
        max_row = ws.max_row if ws.max_row else 100
        max_col = ws.max_column if ws.max_column else 10
        
        for row_idx in range(1, min(20, max_row + 1)):
            for col_idx in range(1, max_col + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                if cell.value:
                    cell_str = str(cell.value).strip()
                    # 자재코드, 설명, 가격 등의 키워드로 헤더 찾기
                    if any(keyword in cell_str for keyword in ['자재코드', '설명', '가격', 'Material Code', 'Description', 'Price']):
                        header_row = row_idx
                        break
            if header_row:
                break
        
        if not header_row:
            # 헤더를 찾지 못한 경우 첫 번째 행을 헤더로 간주
            header_row = 1
        
        # 헤더에서 컬럼 인덱스 찾기
        material_code_col = None
        description_col = None
        price_col = None
        
        print(f"헤더 행 {header_row}에서 컬럼 찾기 시작...")
        for col_idx in range(1, max_col + 1):
            cell = ws.cell(row=header_row, column=col_idx)
            if cell.value:
                cell_str = str(cell.value).strip()
                cell_str_lower = cell_str.lower()
                print(f"  컬럼 {col_idx}: '{cell_str}'")
                
                # 자재코드 컬럼 찾기 (더 유연하게)
                if not material_code_col:
                    if any(keyword in cell_str_lower for keyword in ['자재코드', 'material code', 'materialcode', 'code', '자재', '재료코드']):
                        material_code_col = col_idx
                        print(f"    -> 자재코드 컬럼으로 인식: {col_idx}")
                
                # 설명 컬럼 찾기
                if not description_col:
                    if any(keyword in cell_str_lower for keyword in ['설명', 'description', 'desc', '품명', 'name', '이름']):
                        description_col = col_idx
                        print(f"    -> 설명 컬럼으로 인식: {col_idx}")
                
                # 가격 컬럼 찾기
                if not price_col:
                    if any(keyword in cell_str_lower for keyword in ['가격', 'price', '단가', '금액', 'cost']):
                        price_col = col_idx
                        print(f"    -> 가격 컬럼으로 인식: {col_idx}")
        
        sys.stdout.flush()
        
        # 자재코드 컬럼이 없으면 첫 번째 컬럼을 자재코드로 간주
        if not material_code_col:
            print("자재코드 컬럼을 찾지 못했습니다. 첫 번째 컬럼을 자재코드로 사용합니다.")
            material_code_col = 1
            sys.stdout.flush()
        
        # 설명 컬럼이 없으면 두 번째 컬럼을 설명으로 간주
        if not description_col and max_col >= 2:
            print("설명 컬럼을 찾지 못했습니다. 두 번째 컬럼을 설명으로 사용합니다.")
            description_col = 2
            sys.stdout.flush()
        
        # 가격 컬럼이 없으면 세 번째 컬럼을 가격으로 간주
        if not price_col and max_col >= 3:
            print("가격 컬럼을 찾지 못했습니다. 세 번째 컬럼을 가격으로 사용합니다.")
            price_col = 3
            sys.stdout.flush()
        
        # 데이터 읽기
        parts = []
        print(f"헤더 행: {header_row}, 자재코드 컬럼: {material_code_col}, 설명 컬럼: {description_col}, 가격 컬럼: {price_col}")
        print(f"최대 행: {max_row}, 최대 열: {max_col}")
        sys.stdout.flush()
        
        for row_idx in range(header_row + 1, max_row + 1):
            try:
                material_code = None
                description = None
                price = None
                
                if material_code_col:
                    code_cell = ws.cell(row=row_idx, column=material_code_col)
                    material_code = str(code_cell.value).strip() if code_cell.value else None
                
                if description_col:
                    desc_cell = ws.cell(row=row_idx, column=description_col)
                    description = str(desc_cell.value).strip() if desc_cell.value else None
                
                if price_col:
                    price_cell = ws.cell(row=row_idx, column=price_col)
                    if price_cell.value:
                        try:
                            price = float(price_cell.value)
                        except (ValueError, TypeError):
                            pass
                
                # 자재코드가 있어야 추가
                if material_code and material_code.lower() not in ['none', 'null', '']:
                    parts.append({
                        'material_code': material_code,
                        'description': description or '',
                        'price': price or 0
                    })
                    print(f"행 {row_idx} 추가됨: 자재코드={material_code}, 설명={description}, 가격={price}")
            except Exception as e:
                print(f"행 {row_idx} 처리 오류: {e}")
                import traceback
                print(traceback.format_exc())
                continue
        
        print(f"총 {len(parts)}개의 부품 데이터를 읽었습니다.")
        sys.stdout.flush()
        
        if len(parts) == 0:
            return jsonify({
                "success": False,
                "error": "엑셀 파일에서 유효한 부품 데이터를 찾을 수 없습니다. 자재코드 컬럼이 있는지 확인해주세요."
            }), 400
        
        # 데이터베이스에 부품 정보 저장
        try:
            print("데이터베이스 연결 시작...")
            system = get_welding_system()
            cursor = system.connection.cursor()
            print("데이터베이스 연결 완료")
            
            # 테이블이 존재하는지 확인하고 없으면 생성
            try:
                print("테이블 생성 확인...")
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS part_prices (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        material_code TEXT UNIQUE,
                        description TEXT,
                        price REAL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                system.connection.commit()
                print("테이블 생성 확인 완료")
            except Exception as e:
                print(f"테이블 생성 확인 오류: {e}")
                import traceback
                print(traceback.format_exc())
                return jsonify({
                    "success": False,
                    "error": f"데이터베이스 테이블 생성 오류: {str(e)}"
                }), 500
        except Exception as e:
            print(f"데이터베이스 연결 오류: {e}")
            import traceback
            print(traceback.format_exc())
            return jsonify({
                "success": False,
                "error": f"데이터베이스 연결 오류: {str(e)}"
            }), 500
        
        ensure_part_prices_price_source_column(cursor, system.connection)
        
        saved_count = 0
        updated_count = 0
        
        for part in parts:
            try:
                # 자재코드로 기존 부품 확인
                cursor.execute('''
                    SELECT id FROM part_prices 
                    WHERE material_code = ?
                ''', (part['material_code'],))
                existing = cursor.fetchone()
                
                if existing:
                    # 기존 부품 업데이트
                    cursor.execute('''
                        UPDATE part_prices
                        SET description = ?,
                            price = ?,
                            updated_at = CURRENT_TIMESTAMP,
                            price_source = 'excel'
                        WHERE material_code = ?
                    ''', (part['description'], part['price'], part['material_code']))
                    updated_count += 1
                else:
                    # 새 부품 추가
                    cursor.execute('''
                        INSERT INTO part_prices 
                        (material_code, description, price, price_source)
                        VALUES (?, ?, ?, 'excel')
                    ''', (part['material_code'], part['description'], part['price']))
                    saved_count += 1
            except Exception as e:
                print(f"부품 저장 오류 (자재코드: {part.get('material_code', 'N/A')}): {e}")
                import traceback
                print(traceback.format_exc())
                continue
        
        try:
            system.connection.commit()
            print(f"커밋 완료: 신규 {saved_count}개, 업데이트 {updated_count}개")
        except Exception as e:
            print(f"커밋 오류: {e}")
            import traceback
            print(traceback.format_exc())
            system.connection.rollback()
            return jsonify({
                "success": False,
                "error": f"데이터베이스 저장 오류: {str(e)}"
            }), 500
        
        # 이번 엑셀에 포함된 자재만 (매칭 화면 sessionStorage용, id 포함). 자재코드는 대소문자·앞뒤공백 무시 매칭
        def _json_safe_ts(val):
            if val is None:
                return None
            if hasattr(val, "isoformat"):
                try:
                    return val.isoformat(sep=" ", timespec="seconds")
                except TypeError:
                    try:
                        return val.isoformat(sep=" ")
                    except TypeError:
                        return val.isoformat()
            return str(val)

        def _json_safe_price(val):
            try:
                x = float(val)
                if x != x or x in (float("inf"), float("-inf")):
                    return 0.0
                return x
            except (TypeError, ValueError):
                return 0.0

        uploaded_parts_out = []
        if parts:
            unique_order_upper = []
            seen_upper = set()
            for p in parts:
                raw = p.get("material_code")
                if raw is None:
                    continue
                mc = str(raw).strip()
                if not mc:
                    continue
                u = mc.upper()
                if u in seen_upper:
                    continue
                seen_upper.add(u)
                unique_order_upper.append(u)
            if unique_order_upper:
                placeholders = ",".join("?" * len(unique_order_upper))
                cursor.execute(
                    f"""
                    SELECT id, material_code, description, price, created_at, updated_at
                    FROM part_prices
                    WHERE UPPER(TRIM(IFNULL(material_code, ''))) IN ({placeholders})
                    """,
                    unique_order_upper,
                )
                rows_by_upper = {}
                for row in cursor.fetchall():
                    db_mc = row["material_code"]
                    if db_mc is None:
                        continue
                    ku = str(db_mc).strip().upper()
                    rows_by_upper[ku] = row
                for u in unique_order_upper:
                    row = rows_by_upper.get(u)
                    if not row:
                        continue
                    uploaded_parts_out.append(
                        {
                            "id": int(row["id"]),
                            "material_code": str(row["material_code"] or "").strip(),
                            "description": str(row["description"] or "").strip(),
                            "price": _json_safe_price(row["price"]),
                            "created_at": _json_safe_ts(row["created_at"]),
                            "updated_at": _json_safe_ts(row["updated_at"]),
                        }
                    )
        
        # 메시지 구성
        message = f"{len(parts)}개의 부품이 업로드되었습니다."
        if saved_count > 0 or updated_count > 0:
            message += " ("
            parts_msg = []
            if saved_count > 0:
                parts_msg.append(f"신규 등록: {saved_count}개")
            if updated_count > 0:
                parts_msg.append(f"업데이트: {updated_count}개")
            message += ", ".join(parts_msg) + ")"
        
        print(f"업로드 완료: {len(parts)}개 부품 처리됨 (신규: {saved_count}, 업데이트: {updated_count})")
        import sys
        sys.stdout.flush()
        
        return jsonify(
            {
                "success": True,
                "message": message,
                "saved_count": saved_count,
                "updated_count": updated_count,
                "total_count": len(parts),
                "parts": parts,
                "uploaded_parts": uploaded_parts_out,
            }
        )
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print("=" * 50)
        print(f"Error in api_upload_part_price: {error_trace}")
        print("=" * 50)
        import sys
        sys.stdout.flush()
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/part_price_dashboard')
def part_price_dashboard():
    """파트가격조회 대시보드 페이지"""
    return render_template('part_price_dashboard.html')

@app.route('/part_price_image_upload')
def part_price_image_upload():
    """파트가격조회 이미지 업로드 페이지"""
    return render_template('part_price_image_upload.html')


@app.route('/option_view_image_upload')
def option_view_image_upload():
    """옵션조회 [1] 파워소스 × [2] 타입 이미지 등록 및 영역 매핑 — PNS 관리자 전용"""
    if not _is_pns_admin_session():
        return redirect(url_for('index'))
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        ensure_option_view_image_tables(cursor)
        categories = list_option_categories(cursor)
    except Exception:
        categories = []
    return render_template('option_view_image_upload.html', categories=categories)


@app.route('/api/option_view_images/folders', methods=['GET'])
def api_option_view_image_folders():
    """[1] 파워소스 옵션 폴더 목록"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        ensure_option_view_image_tables(cursor)
        system.connection.commit()
        folders = list_power_source_folders(cursor)
        return jsonify({"success": True, "folders": folders})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/option_view_images', methods=['GET'])
def api_option_view_images_list():
    """파워소스 옵션별 이미지 목록 (type_code 선택)"""
    try:
        option_item_id = request.args.get('option_item_id', type=int)
        if not option_item_id:
            return jsonify({"success": False, "error": "option_item_id가 필요합니다."}), 400
        type_code = request.args.get('type_code') or request.args.get('type')
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        ensure_option_view_image_tables(cursor)
        system.connection.commit()
        images = list_images_by_option(cursor, option_item_id, type_code=type_code)
        attach_regions_to_images(cursor, images)
        for image in images:
            image['url'] = url_for('api_option_view_image_file', image_id=image['id'])
        return jsonify({"success": True, "images": images})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/option_view_images/by_option/<int:option_item_id>', methods=['GET'])
def api_option_view_images_by_option(option_item_id):
    """옵션선택(조회용) — 파워소스 + 타입(AIR/WATER) 선택 시 이미지 조회"""
    try:
        type_code = request.args.get('type_code') or request.args.get('type')
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        ensure_option_view_image_tables(cursor)
        system.connection.commit()
        images = list_images_by_option(cursor, option_item_id, type_code=type_code)
        attach_regions_to_images(cursor, images)
        for image in images:
            image['url'] = url_for('api_option_view_image_file', image_id=image['id'])
        return jsonify({"success": True, "images": images, "type_code": type_code})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/option_view_images/<int:image_id>/regions', methods=['GET'])
def api_option_view_image_regions_get(image_id):
    """옵션조회 이미지 영역 매핑 조회"""
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        ensure_option_view_image_tables(cursor)
        record = get_option_view_image_record(cursor, image_id)
        if not record:
            return jsonify({"success": False, "error": "이미지를 찾을 수 없습니다."}), 404
        regions = list_regions_by_image(cursor, image_id)
        return jsonify({"success": True, "image_id": image_id, "regions": regions})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/option_view_images/<int:image_id>/regions', methods=['PUT'])
def api_option_view_image_regions_save(image_id):
    """옵션조회 이미지 영역 매핑 저장"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        payload = request.get_json(silent=True) or {}
        regions = payload.get('regions', [])
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        saved, error = save_regions_for_image(cursor, image_id, regions)
        if error:
            return jsonify({"success": False, "error": error}), 400
        system.connection.commit()
        return jsonify({
            "success": True,
            "message": "영역 매핑이 저장되었습니다.",
            "regions": saved,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/option_view_images/upload', methods=['POST'])
def api_option_view_images_upload():
    """옵션조회 파워소스 × 타입(AIR/WATER) 이미지 업로드"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        option_item_id = request.form.get('option_item_id', type=int)
        if not option_item_id:
            return jsonify({"success": False, "error": "option_item_id가 필요합니다."}), 400
        type_code = request.form.get('type_code') or request.form.get('type')
        files = request.files.getlist('files')
        if not files or all(not f or not f.filename for f in files):
            return jsonify({"success": False, "error": "파일이 선택되지 않았습니다."}), 400

        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        uploaded_by = session.get('pns_admin_user') or session.get('username') or 'pns'
        saved, error = save_uploaded_images(
            cursor,
            option_item_id,
            files,
            uploaded_by=uploaded_by,
            type_code=type_code,
        )
        if error:
            return jsonify({"success": False, "error": error}), 400
        system.connection.commit()
        for image in saved:
            image['url'] = url_for('api_option_view_image_file', image_id=image['id'])
        return jsonify({
            "success": True,
            "message": f"{len(saved)}개 이미지가 업로드되었습니다.",
            "images": saved,
        })
    except Exception as e:
        import traceback
        print(f"Error in api_option_view_images_upload: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/option_view_images/<int:image_id>', methods=['DELETE'])
def api_option_view_images_delete(image_id):
    """옵션조회 파워소스 이미지 삭제"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        if not delete_option_view_image(cursor, image_id):
            return jsonify({"success": False, "error": "이미지를 찾을 수 없습니다."}), 404
        system.connection.commit()
        return jsonify({"success": True, "message": "이미지가 삭제되었습니다."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/option_view_images/<int:image_id>/type', methods=['PUT'])
def api_option_view_images_set_type(image_id):
    """옵션조회 이미지 타입(AIR/WATER) 지정"""
    denied = _require_pns_admin_json()
    if denied:
        return denied
    try:
        payload = request.get_json(silent=True) or {}
        type_code = payload.get('type_code') or payload.get('type') or request.args.get('type_code')
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        record, error = update_option_view_image_type_code(cursor, image_id, type_code)
        if error:
            return jsonify({"success": False, "error": error}), 400
        system.connection.commit()
        return jsonify({
            "success": True,
            "message": f"{record.get('type_code')} 타입으로 지정되었습니다.",
            "image": record,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/option_view_images/file/<int:image_id>', methods=['GET'])
def api_option_view_image_file(image_id):
    """옵션조회 파워소스 이미지 파일"""
    try:
        system = get_welding_system()
        if not system:
            return jsonify({"success": False, "error": "시스템 초기화 오류"}), 500
        cursor = system.connection.cursor()
        record = get_option_view_image_record(cursor, image_id)
        if not record or not os.path.isfile(record['filepath']):
            return jsonify({"success": False, "error": "이미지를 찾을 수 없습니다."}), 404
        return send_file(record['filepath'], download_name=record['filename'])
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/part_price/trolley')
def trolley_spare_parts():
    """Trolley Spare Parts List 이미지 조회 페이지"""
    return render_template('trolley_spare_parts.html')

@app.route('/part_price/cooling_unit')
def cooling_unit_spare_parts():
    """Cooling Unit Spare Parts List 이미지 조회 페이지"""
    # trolley_spare_parts.html과 동일한 템플릿 사용하되 category만 다름
    return render_template('trolley_spare_parts.html')

@app.route('/part_price_image_price_match')
def part_price_image_price_match():
    """파트가격조회 이미지,가격 매칭 페이지"""
    return render_template('part_price_image_price_match.html')


def ensure_part_prices_price_source_column(cursor, connection):
    """part_prices.price_source: 'excel' | 'manual' — 이미지·가격 매칭에서는 excel만 목록 표시"""
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='part_prices'")
        if not cursor.fetchone():
            return
        cursor.execute("PRAGMA table_info(part_prices)")
        col_names = [row[1] for row in cursor.fetchall()]
        if 'price_source' not in col_names:
            cursor.execute("ALTER TABLE part_prices ADD COLUMN price_source TEXT")
            connection.commit()
    except Exception as e:
        print(f"ensure_part_prices_price_source_column: {e}")


@app.route('/api/part_price/dashboard', methods=['GET'])
def api_part_price_dashboard():
    """파트가격조회 대시보드 API - 부품 목록 조회"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 테이블이 존재하는지 확인하고 없으면 생성
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS part_prices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    material_code TEXT UNIQUE,
                    description TEXT,
                    price REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            system.connection.commit()
        except Exception as e:
            print(f"테이블 생성 확인 오류: {e}")
        
        ensure_part_prices_price_source_column(cursor, system.connection)
        
        excel_only = request.args.get("excel_only", "").strip().lower() in ("1", "true", "yes")
        
        # 모든 부품 조회 (excel_only=1 이면 가격 엑셀 업로드로 등록·갱신된 행만)
        if excel_only:
            cursor.execute('''
                SELECT id, material_code, description, price, created_at, updated_at
                FROM part_prices
                WHERE price_source = 'excel'
                ORDER BY created_at DESC
            ''')
        else:
            cursor.execute('''
                SELECT id, material_code, description, price, created_at, updated_at
                FROM part_prices
                ORDER BY created_at DESC
            ''')
        
        parts = []
        total_price = 0
        
        rows = cursor.fetchall()
        
        for row in rows:
            try:
                row_id = row[0]
                material_code = row[1] if len(row) > 1 else ''
                description = row[2] if len(row) > 2 else ''
                price_val = row[3] if len(row) > 3 else None
                created_at = row[4] if len(row) > 4 else None
                updated_at = row[5] if len(row) > 5 else None
                
                price = float(price_val) if price_val is not None else 0.0
                total_price += price
                
                parts.append({
                    'id': row_id,
                    'material_code': material_code or '',
                    'description': description or '',
                    'price': price,
                    'created_at': created_at,
                    'updated_at': updated_at
                })
            except Exception as e:
                print(f"부품 데이터 처리 오류: {e}")
                continue
        
        # 통계 계산
        total_parts = len(parts)
        average_price = (total_price / total_parts) if total_parts > 0 else 0
        
        statistics = {
            'totalParts': total_parts,
            'totalPrice': total_price,
            'averagePrice': average_price
        }
        
        return jsonify({
            "success": True,
            "parts": parts,
            "statistics": statistics,
            "count": total_parts
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_part_price_dashboard: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

def allowed_file(filename):
    """허용된 파일 확장자인지 확인"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

@app.route('/api/part_price/image/upload', methods=['POST'])
def api_upload_part_price_image():
    """파트가격조회 이미지 업로드 API"""
    try:
        category = request.form.get('category')
        
        folder_id = request.form.get('folder_id')
        if folder_id:
            try:
                folder_id = int(folder_id)
            except (ValueError, TypeError):
                folder_id = None
        
        subfolder_id = request.form.get('subfolder_id')
        if subfolder_id:
            try:
                subfolder_id = int(subfolder_id)
            except (ValueError, TypeError):
                subfolder_id = None
        
        # subfolder_id가 있으면 하위 폴더 정보를 조회하여 카테고리 자동 결정
        if subfolder_id and not category:
            system = get_welding_system()
            cursor = system.connection.cursor()
            
            # 하위 폴더 테이블 생성
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS part_price_subfolders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    folder_id INTEGER NOT NULL,
                    subfolder_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (folder_id) REFERENCES part_price_folders(id) ON DELETE CASCADE,
                    UNIQUE(folder_id, subfolder_name)
                )
            ''')
            system.connection.commit()
            
            # 하위 폴더 정보 조회
            cursor.execute('''
                SELECT subfolder_name
                FROM part_price_subfolders
                WHERE id = ?
            ''', (subfolder_id,))
            
            row = cursor.fetchone()
            if row:
                subfolder_name = row[0].lower()
                # 하위 폴더 이름에서 카테고리 추출
                if 'cooling unit' in subfolder_name or 'coolingunit' in subfolder_name:
                    category = 'cooling_unit'
                elif 'trolley' in subfolder_name:
                    category = 'trolley'
        
        # category가 없으면 기본값 사용 (하위 폴더 이름에서 추출할 수 없는 경우)
        if not category:
            # subfolder_id가 있으면 기본값으로 'trolley' 사용
            if subfolder_id:
                category = 'trolley'
            else:
                return jsonify({
                    "success": False,
                    "error": "카테고리가 필요합니다."
                }), 400
        
        if 'files' not in request.files:
            return jsonify({
                "success": False,
                "error": "파일이 없습니다."
            }), 400
        
        files = request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            return jsonify({
                "success": False,
                "error": "파일이 선택되지 않았습니다."
            }), 400
        
        # 업로드 폴더 생성
        upload_folder = os.path.join(app.config['UPLOAD_FOLDER'], category)
        os.makedirs(upload_folder, exist_ok=True)
        
        # 데이터베이스 연결
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 이미지 테이블 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_price_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                file_size INTEGER,
                folder_id INTEGER,
                subfolder_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # folder_id, subfolder_id 컬럼이 없으면 추가
        try:
            cursor.execute("PRAGMA table_info(part_price_images)")
            columns = cursor.fetchall()
            column_names = [col[1] for col in columns]
            if 'folder_id' not in column_names:
                cursor.execute('ALTER TABLE part_price_images ADD COLUMN folder_id INTEGER')
            if 'subfolder_id' not in column_names:
                cursor.execute('ALTER TABLE part_price_images ADD COLUMN subfolder_id INTEGER')
            system.connection.commit()
        except Exception as e:
            print(f"컬럼 추가 오류 (이미 존재할 수 있음): {e}")
        
        # 부품 정보 테이블 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_price_image_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL,
                part_number INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                description TEXT NOT NULL,
                position_x REAL,
                position_y REAL,
                FOREIGN KEY (image_id) REFERENCES part_price_images(id) ON DELETE CASCADE,
                UNIQUE(image_id, part_number)
            )
        ''')
        system.connection.commit()
        
        uploaded_files = []
        errors = []
        
        for file in files:
            if file.filename == '':
                continue
            
            if not allowed_file(file.filename):
                errors.append(f"{file.filename}: 허용되지 않은 파일 형식입니다.")
                continue
            
            try:
                filename = secure_filename(file.filename)
                # 중복 방지를 위해 타임스탬프 추가
                import time
                timestamp = int(time.time() * 1000)
                name, ext = os.path.splitext(filename)
                unique_filename = f"{name}_{timestamp}{ext}"
                filepath = os.path.join(upload_folder, unique_filename)
                
                file.save(filepath)
                file_size = os.path.getsize(filepath)
                
                # 데이터베이스에 저장 (subfolder_id 포함)
                cursor.execute('''
                    INSERT INTO part_price_images (category, filename, filepath, file_size, folder_id, subfolder_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (category, filename, filepath, file_size, folder_id, subfolder_id))
                
                uploaded_files.append({
                    'filename': filename,
                    'filepath': filepath
                })
            except Exception as e:
                errors.append(f"{file.filename}: {str(e)}")
                continue
        
        system.connection.commit()
        
        if uploaded_files:
            message = f"{len(uploaded_files)}개의 이미지가 업로드되었습니다."
            if errors:
                message += f" ({len(errors)}개 실패)"
            return jsonify({
                "success": True,
                "message": message,
                "uploaded_count": len(uploaded_files),
                "errors": errors
            })
        else:
            return jsonify({
                "success": False,
                "error": "업로드된 파일이 없습니다. " + ("; ".join(errors) if errors else "")
            }), 400
            
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_upload_part_price_image: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/image/list', methods=['GET'])
def api_list_part_price_images():
    """파트가격조회 이미지 목록 조회 API"""
    try:
        print(f"[API] /api/part_price/image/list 호출됨")
        category = request.args.get('category')
        print(f"[API] 카테고리 파라미터: {category}")
        
        folder_id = request.args.get('folder_id')
        subfolder_id = request.args.get('subfolder_id')
        
        # subfolder_id가 없고 category도 없으면 오류
        if not subfolder_id and not category:
            print(f"[API] 카테고리 또는 하위 폴더 ID가 필요함")
            return jsonify({
                "success": False,
                "error": "카테고리 또는 하위 폴더 ID가 필요합니다."
            }), 400
        
        # folder_id와 subfolder_id 파싱
        if folder_id:
            try:
                folder_id = int(folder_id)
            except (ValueError, TypeError):
                folder_id = None
        
        if subfolder_id:
            try:
                subfolder_id = int(subfolder_id)
            except (ValueError, TypeError):
                subfolder_id = None
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        print(f"[API] 데이터베이스 연결 성공")
        
        # 테이블이 없으면 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_price_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                file_size INTEGER,
                folder_id INTEGER,
                subfolder_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # folder_id, subfolder_id 컬럼이 없으면 추가
        try:
            cursor.execute("PRAGMA table_info(part_price_images)")
            columns = cursor.fetchall()
            column_names = [col[1] for col in columns]
            if 'folder_id' not in column_names:
                cursor.execute('ALTER TABLE part_price_images ADD COLUMN folder_id INTEGER')
            if 'subfolder_id' not in column_names:
                cursor.execute('ALTER TABLE part_price_images ADD COLUMN subfolder_id INTEGER')
            system.connection.commit()
        except Exception as e:
            print(f"컬럼 추가 오류 (이미 존재할 수 있음): {e}")
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_price_image_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL,
                part_number INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                description TEXT NOT NULL,
                position_x REAL,
                position_y REAL,
                FOREIGN KEY (image_id) REFERENCES part_price_images(id) ON DELETE CASCADE,
                UNIQUE(image_id, part_number)
            )
        ''')
        system.connection.commit()
        
        # 쿼리 조건 구성
        # subfolder_id가 있으면 subfolder_id로 필터링, 없으면 category로 필터링
        if subfolder_id:
            query = 'SELECT id, category, filename, filepath, file_size, created_at FROM part_price_images WHERE subfolder_id = ?'
            params = [subfolder_id]
            if folder_id:
                query += ' AND folder_id = ?'
                params.append(folder_id)
        else:
            query = 'SELECT id, category, filename, filepath, file_size, created_at FROM part_price_images WHERE category = ?'
            params = [category]
            if folder_id:
                query += ' AND folder_id = ?'
                params.append(folder_id)
        
        query += ' ORDER BY created_at DESC'
        
        cursor.execute(query, tuple(params))
        
        images = []
        rows = cursor.fetchall()
        print(f"[API] 조회된 이미지 수: {len(rows)}")
        
        # subfolder_id가 있으면 subfolder_name 조회
        subfolder_name = None
        if subfolder_id:
            try:
                cursor.execute('''
                    SELECT subfolder_name
                    FROM part_price_subfolders
                    WHERE id = ?
                ''', (subfolder_id,))
                subfolder_row = cursor.fetchone()
                if subfolder_row:
                    subfolder_name = subfolder_row[0]
                    print(f"[API] 하위 폴더 이름 조회: {subfolder_name}")
            except Exception as e:
                print(f"[API] 하위 폴더 이름 조회 오류: {e}")
        
        for row in rows:
            image_id = row[0]
            # 부품 정보 조회
            cursor.execute('''
                SELECT part_number, stock_code, description, position_x, position_y
                FROM part_price_image_parts
                WHERE image_id = ?
                ORDER BY part_number
            ''', (image_id,))
            
            parts = []
            part_rows = cursor.fetchall()
            for part_row in part_rows:
                parts.append({
                    'part_number': part_row[0],
                    'stock_code': part_row[1],
                    'description': part_row[2],
                    'position_x': part_row[3],
                    'position_y': part_row[4]
                })
            
            images.append({
                'id': image_id,
                'category': row[1],
                'filename': row[2],
                'filepath': row[3],
                'file_size': row[4],
                'created_at': row[5],
                'parts': parts
            })
        
        response_data = {
            "success": True,
            "images": images,
            "count": len(images)
        }
        
        # subfolder_name이 있으면 응답에 포함
        if subfolder_name:
            response_data["subfolder_name"] = subfolder_name
        print(f"[API] 응답 데이터 준비 완료: 이미지 {len(images)}개")
        print(f"[API] 응답 전송 시작...")
        
        response = jsonify(response_data)
        print(f"[API] 응답 전송 완료")
        return response
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_list_part_price_images: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/by_codes', methods=['POST'])
def api_get_part_prices_by_codes():
    """자재코드 목록으로 부품 가격 정보 조회 API"""
    try:
        data = request.get_json()
        if not data or 'material_codes' not in data:
            return jsonify({
                "success": False,
                "error": "자재코드 목록이 필요합니다."
            }), 400
        
        material_codes = data.get('material_codes', [])
        if not isinstance(material_codes, list) or len(material_codes) == 0:
            return jsonify({
                "success": False,
                "error": "자재코드 목록이 비어있습니다."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # part_prices 테이블이 없으면 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_code TEXT UNIQUE,
                description TEXT,
                price REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        system.connection.commit()
        
        # 자재코드로 가격 정보 조회 (공백 제거 및 정규화)
        print(f"조회할 자재코드 목록 (원본): {material_codes}")
        
        # 자재코드 정규화 (공백 제거, 대문자 변환)
        normalized_codes = [str(code).strip().upper() if code else '' for code in material_codes]
        print(f"정규화된 자재코드 목록: {normalized_codes}")
        
        # 모든 part_prices 데이터 조회 (매칭을 위해)
        cursor.execute('''
            SELECT material_code, description, price
            FROM part_prices
        ''')
        
        all_rows = cursor.fetchall()
        print(f"데이터베이스의 전체 자재코드 수: {len(all_rows)}")
        
        # 자재코드 매핑 생성 (정규화된 코드로)
        code_map = {}
        for row in all_rows:
            db_code = str(row[0]).strip().upper() if row[0] else ''
            code_map[db_code] = {
                'original_code': row[0],
                'description': row[1],
                'price': float(row[2]) if row[2] is not None else None
            }
            print(f"DB 자재코드: {row[0]} -> 정규화: {db_code}, 가격: {row[2]}")
        
        # 요청된 자재코드와 매칭
        price_map = {}
        for i, normalized_code in enumerate(normalized_codes):
            original_code = material_codes[i]
            if normalized_code in code_map:
                price_info = code_map[normalized_code]
                price_map[original_code] = {
                    'material_code': price_info['original_code'],
                    'description': price_info['description'],
                    'price': price_info['price']
                }
                print(f"매칭 성공: {original_code} -> {price_info['original_code']}, 가격: {price_info['price']}")
            else:
                print(f"매칭 실패: {original_code} (정규화: {normalized_code})")
        
        print(f"반환할 가격 맵: {price_map}")
        
        return jsonify({
            "success": True,
            "prices": price_map
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_get_part_prices_by_codes: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/image/match', methods=['POST'])
def api_save_image_part_match():
    """파트가격조회 이미지-부품 매칭 저장 API"""
    try:
        # JSON 데이터 확인
        if not request.is_json:
            return jsonify({
                "success": False,
                "error": "Content-Type이 application/json이어야 합니다."
            }), 400
        
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "error": "요청 데이터가 없습니다."
            }), 400
        
        image_id = data.get('image_id')
        part_ids = data.get('part_ids', [])
        
        if not image_id:
            return jsonify({
                "success": False,
                "error": "이미지 ID가 필요합니다."
            }), 400
        
        if not part_ids or len(part_ids) == 0:
            return jsonify({
                "success": False,
                "error": "부품 ID가 필요합니다."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 매칭 테이블 생성 (UNIQUE 제약조건 제거 - 중복 저장 허용)
        # 기존 테이블에 UNIQUE 제약조건이 있는지 확인하고 제거
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='part_price_image_matches'")
        table_exists = cursor.fetchone()
        
        if table_exists:
            # 기존 테이블에 UNIQUE 제약조건이 있는지 확인
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='part_price_image_matches'")
            table_sql = cursor.fetchone()
            if table_sql and 'UNIQUE' in table_sql[0]:
                # 기존 데이터 백업
                cursor.execute('SELECT * FROM part_price_image_matches')
                existing_data = cursor.fetchall()
                
                # 기존 테이블 삭제
                cursor.execute('DROP TABLE part_price_image_matches')
                
                # UNIQUE 제약조건 없이 테이블 재생성
                cursor.execute('''
                    CREATE TABLE part_price_image_matches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        image_id INTEGER NOT NULL,
                        part_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # 기존 데이터 복원
                for row in existing_data:
                    cursor.execute('''
                        INSERT INTO part_price_image_matches (id, image_id, part_id, created_at)
                        VALUES (?, ?, ?, ?)
                    ''', row)
        else:
            # 테이블이 없으면 UNIQUE 제약조건 없이 생성
            cursor.execute('''
                CREATE TABLE part_price_image_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    part_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        
        # part_ids를 정수로 변환
        part_ids = [int(pid) for pid in part_ids if pid]
        image_id = int(image_id)
        
        saved_count = 0
        for part_id in part_ids:
            try:
                # INSERT OR IGNORE 대신 INSERT 사용 (중복 허용)
                cursor.execute('''
                    INSERT INTO part_price_image_matches (image_id, part_id)
                    VALUES (?, ?)
                ''', (image_id, part_id))
                saved_count += 1
            except Exception as e:
                import traceback
                print(f"부품 {part_id} 매칭 저장 오류: {e}")
                print(traceback.format_exc())
                continue
        
        system.connection.commit()
        
        return jsonify({
            "success": True,
            "message": f"{saved_count}개의 매칭이 저장되었습니다.",
            "saved_count": saved_count
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_save_image_part_match: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/image/matches', methods=['GET'])
def api_get_image_matches():
    """파트가격조회 이미지의 매칭 정보 조회 API"""
    try:
        image_id = request.args.get('image_id')
        
        if not image_id:
            return jsonify({
                "success": False,
                "error": "이미지 ID가 필요합니다."
            }), 400
        
        image_id = int(image_id)
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 매칭 테이블이 없으면 생성 (UNIQUE 제약조건 없이 - 중복 저장 허용)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_price_image_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL,
                part_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            SELECT m.id, m.part_id, m.created_at,
                   p.material_code, p.description, p.price, p.created_at as part_created_at, p.updated_at
            FROM part_price_image_matches m
            JOIN part_prices p ON m.part_id = p.id
            WHERE m.image_id = ?
            ORDER BY m.id ASC
        ''', (image_id,))
        
        matches = []
        for row in cursor.fetchall():
            matches.append({
                'id': row[0],
                'part_id': row[1],
                'created_at': row[2],
                'part': {
                    'id': row[1],
                    'material_code': row[3],
                    'description': row[4],
                    'price': float(row[5]) if row[5] else 0.0,
                    'created_at': row[6],
                    'updated_at': row[7]
                }
            })
        
        return jsonify({
            "success": True,
            "matches": matches
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_get_image_matches: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/image/match/<int:match_id>', methods=['DELETE'])
def api_delete_image_match(match_id):
    """파트가격조회 이미지-부품 매칭 삭제 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        cursor.execute('DELETE FROM part_price_image_matches WHERE id = ?', (match_id,))
        system.connection.commit()
        
        if cursor.rowcount > 0:
            return jsonify({
                "success": True,
                "message": "매칭이 삭제되었습니다."
            })
        else:
            return jsonify({
                "success": False,
                "error": "매칭을 찾을 수 없습니다."
            }), 404
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_delete_image_match: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/image/<int:image_id>', methods=['GET'])
def api_get_part_price_image(image_id):
    """파트가격조회 이미지 조회 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        cursor.execute('''
            SELECT filepath, filename
            FROM part_price_images
            WHERE id = ?
        ''', (image_id,))
        
        row = cursor.fetchone()
        if not row:
            return jsonify({
                "success": False,
                "error": "이미지를 찾을 수 없습니다."
            }), 404
        
        filepath = row[0]
        filename = row[1]
        
        if not os.path.exists(filepath):
            return jsonify({
                "success": False,
                "error": "파일이 존재하지 않습니다."
            }), 404
        
        return send_file(filepath, mimetype='image/jpeg', download_name=filename)
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_get_part_price_image: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

def _spare_parts_catalog_version():
    """브로셔 PDF 첫/끝 페이지에 동일하게 넣는 버전 표기"""
    from datetime import datetime, timezone, timedelta
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).strftime('Ver. %Y.%m')


def _to_int_or_none(value):
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_PDF_FONT_READY = False
_PDF_FONT = 'Helvetica'
_PDF_FONT_BOLD = 'Helvetica-Bold'


def _ensure_pdf_fonts():
    """영문 Helvetica 기본, Windows 맑은 고딕이 있으면 한글 제목 지원"""
    global _PDF_FONT_READY, _PDF_FONT, _PDF_FONT_BOLD
    if _PDF_FONT_READY:
        return
    _PDF_FONT_READY = True
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        regular = r'C:\Windows\Fonts\malgun.ttf'
        bold = r'C:\Windows\Fonts\malgunbd.ttf'
        if os.path.exists(regular):
            pdfmetrics.registerFont(TTFont('PNSGothic', regular))
            _PDF_FONT = 'PNSGothic'
            if os.path.exists(bold):
                pdfmetrics.registerFont(TTFont('PNSGothic-Bold', bold))
                _PDF_FONT_BOLD = 'PNSGothic-Bold'
            else:
                _PDF_FONT_BOLD = 'PNSGothic'
    except Exception as font_err:
        print(f"PDF 폰트 등록 실패(Helvetica 사용): {font_err}")
        _PDF_FONT = 'Helvetica'
        _PDF_FONT_BOLD = 'Helvetica-Bold'


def _pdf_safe_text(text):
    if text is None:
        return ''
    s = str(text)
    if _PDF_FONT != 'Helvetica' and not _PDF_FONT.startswith('Helvetica'):
        return s
    try:
        s.encode('latin-1')
        return s
    except UnicodeEncodeError:
        return s.encode('latin-1', 'replace').decode('latin-1')


def _ascii_pdf_filename(name, fallback='Spare_Parts.pdf'):
    raw = (name or fallback).strip() or fallback
    if not raw.lower().endswith('.pdf'):
        raw += '.pdf'
    safe = ''.join(ch if (ch.isalnum() or ch in (' ', '-', '_', '.')) else '_' for ch in raw)
    safe = '_'.join(safe.split())
    return safe or fallback


def _pdf_file_response(buffer, file_name):
    payload = buffer.getvalue() if hasattr(buffer, 'getvalue') else buffer
    ascii_name = _ascii_pdf_filename(file_name)
    resp = Response(payload, mimetype='application/pdf')
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'attachment; filename="{ascii_name}"'
    resp.headers['Content-Length'] = str(len(payload))
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _resolve_spare_parts_titles(cursor, folder_id, subfolder_id, category):
    title1 = None
    title2 = None
    folder_id = _to_int_or_none(folder_id)
    subfolder_id = _to_int_or_none(subfolder_id)
    if folder_id:
        try:
            cursor.execute('SELECT folder_name FROM part_price_folders WHERE id = ?', (folder_id,))
            folder_row = cursor.fetchone()
            if folder_row:
                title1 = folder_row[0]
        except Exception:
            pass
    if subfolder_id:
        try:
            cursor.execute('SELECT subfolder_name FROM part_price_subfolders WHERE id = ?', (subfolder_id,))
            subfolder_row = cursor.fetchone()
            if subfolder_row:
                title2 = subfolder_row[0]
        except Exception:
            pass
    if not title1:
        title1 = 'Spare Parts'
    if not title2:
        category_lower = (category or '').lower()
        if 'cooling' in category_lower:
            title2 = 'Cooling Unit Spare Parts List'
        elif 'power' in category_lower:
            title2 = 'Power Unit Spare Parts List'
        else:
            title2 = 'Trolley Spare Parts List'
    return title1, title2


def _query_spare_parts_images(cursor, folder_id, subfolder_id, category):
    folder_id = _to_int_or_none(folder_id)
    subfolder_id = _to_int_or_none(subfolder_id)
    if subfolder_id:
        query = '''
            SELECT id, filename, filepath
            FROM part_price_images
            WHERE subfolder_id = ?
            ORDER BY created_at DESC
        '''
        params = [subfolder_id]
        if folder_id:
            query = '''
                SELECT id, filename, filepath
                FROM part_price_images
                WHERE subfolder_id = ? AND folder_id = ?
                ORDER BY created_at DESC
            '''
            params.append(folder_id)
    else:
        query = '''
            SELECT id, filename, filepath
            FROM part_price_images
            WHERE category = ?
            ORDER BY created_at DESC
        '''
        params = [category or 'trolley']
        if folder_id:
            query = '''
                SELECT id, filename, filepath
                FROM part_price_images
                WHERE category = ? AND folder_id = ?
                ORDER BY created_at DESC
            '''
            params.append(folder_id)
    cursor.execute(query, params)
    return cursor.fetchall()


def _query_image_matches(cursor, image_id):
    matches = []
    total_price = 0.0
    if image_id is None:
        return matches, total_price
    try:
        cursor.execute('''
            SELECT p.material_code, p.description, p.price
            FROM part_price_image_matches m
            JOIN part_prices p ON m.part_id = p.id
            WHERE m.image_id = ?
            ORDER BY m.id ASC
        ''', (image_id,))
        for mrow in cursor.fetchall():
            material_code = mrow[0] if len(mrow) > 0 else ''
            description = mrow[1] if len(mrow) > 1 else ''
            price_val = float(mrow[2]) if len(mrow) > 2 and mrow[2] is not None else 0.0
            total_price += price_val
            matches.append({
                "material_code": material_code or '-',
                "description": description or '-',
                "price": price_val,
            })
    except Exception as match_err:
        print(f"매칭 부품 조회 생략(image_id={image_id}): {match_err}")
    return matches, total_price


def _draw_spare_parts_version(pdf, page_width, version_text):
    """첫 페이지·마지막 페이지에 동일한 버전 문자열을 표시"""
    from reportlab.lib.pagesizes import A4
    page_height = A4[1]
    pdf.setFillColorRGB(0.50, 0.00, 0.13)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawRightString(page_width - 36, page_height - 22, version_text)
    pdf.drawRightString(page_width - 36, 18, version_text)
    pdf.setFillColorRGB(0, 0, 0)


def _prepare_pdf_image_source(filepath):
    """대용량/특수 PNG 등으로 PDF 생성이 실패하지 않도록 이미지를 정규화"""
    if not filepath or not os.path.exists(filepath):
        return filepath
    try:
        from PIL import Image, ImageOps
        from io import BytesIO

        with Image.open(filepath) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ('RGB', 'L'):
                im = im.convert('RGB')
            max_side = 2400
            w, h = im.size
            if max(w, h) > max_side:
                scale = max_side / float(max(w, h))
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
            buf = BytesIO()
            im.save(buf, format='JPEG', quality=88, optimize=True)
            buf.seek(0)
            from reportlab.lib.utils import ImageReader
            return ImageReader(buf)
    except Exception as prep_err:
        print(f"PDF 이미지 정규화 생략({filepath}): {prep_err}")
        from reportlab.lib.utils import ImageReader
        return ImageReader(filepath)


def _parse_spare_parts_batch_items():
    """JSON 본문 또는 form 필드(items_json)에서 일괄 PDF 항목 파싱"""
    import json as _json
    data = request.get_json(silent=True)
    if isinstance(data, dict) and data.get('items') is not None:
        return data.get('items') or []
    raw = request.form.get('items_json')
    if raw:
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return parsed.get('items') or []
        except Exception as parse_err:
            print(f"items_json 파싱 실패: {parse_err}")
    return []


def _draw_spare_parts_page(pdf, filepath, matches, total_price, title1, title2, version_text, show_version):
    from reportlab.lib.pagesizes import A4

    _ensure_pdf_fonts()
    page_width, page_height = A4
    pdf.setFont(_PDF_FONT_BOLD, 15)
    pdf.drawString(36, page_height - 36, _pdf_safe_text(title1 or 'Spare Parts'))
    pdf.setFont(_PDF_FONT_BOLD, 12)
    pdf.drawString(36, page_height - 56, _pdf_safe_text(title2 or ''))
    if show_version and version_text:
        _draw_spare_parts_version(pdf, page_width, version_text)

    try:
        img = _prepare_pdf_image_source(filepath)
        img_width, img_height = img.getSize()
        left_x = 36
        top_y = page_height - 90
        bottom_y = 36
        content_height = top_y - bottom_y
        left_width = (page_width - 72) * 0.58
        right_x = left_x + left_width + 14
        right_width = page_width - 36 - right_x

        max_width = left_width
        max_height = content_height
        if img_width <= 0 or img_height <= 0:
            raise ValueError('invalid image size')
        scale = min(max_width / img_width, max_height / img_height)
        draw_width = img_width * scale
        draw_height = img_height * scale
        x = left_x + (left_width - draw_width) / 2
        y = bottom_y + (content_height - draw_height) / 2
        pdf.drawImage(
            img, x, y,
            width=draw_width,
            height=draw_height,
            preserveAspectRatio=True,
            mask='auto'
        )

        pdf.setFont(_PDF_FONT_BOLD, 10)
        pdf.drawString(right_x, top_y, "Matched Parts")
        table_top = top_y - 14
        row_h = 12

        pdf.setFont(_PDF_FONT_BOLD, 8)
        col_no_w = 18
        col_code_w = right_width * 0.28
        col_desc_w = right_width * 0.44
        header_y = table_top
        pdf.drawString(right_x, header_y, "No")
        pdf.drawString(right_x + col_no_w, header_y, "Code")
        pdf.drawString(right_x + col_no_w + col_code_w, header_y, "Description")
        pdf.drawRightString(right_x + right_width, header_y, "Price")

        y_cursor = header_y - row_h
        pdf.setFont(_PDF_FONT, 7.5)
        max_rows = int((content_height - 40) / row_h)
        display_rows = matches[:max_rows] if max_rows > 0 else []

        for idx, part in enumerate(display_rows, start=1):
            code = _pdf_safe_text(part["material_code"])
            desc = _pdf_safe_text(part["description"])
            if len(code) > 15:
                code = code[:15] + "..."
            if len(desc) > 26:
                desc = desc[:26] + "..."
            pdf.drawString(right_x, y_cursor, str(idx))
            pdf.drawString(right_x + col_no_w, y_cursor, code)
            pdf.drawString(right_x + col_no_w + col_code_w, y_cursor, desc)
            pdf.drawRightString(right_x + right_width, y_cursor, f"€{part['price']:.2f}")
            y_cursor -= row_h

        if len(matches) > len(display_rows):
            pdf.setFont(_PDF_FONT, 7)
            pdf.drawString(right_x, y_cursor, f"... and {len(matches) - len(display_rows)} more")
            y_cursor -= row_h

        pdf.setFont(_PDF_FONT_BOLD, 8.5)
        pdf.drawString(right_x, bottom_y + 8, "Total")
        pdf.drawRightString(right_x + right_width, bottom_y + 8, f"€{total_price:.2f}")
    except Exception as img_err:
        pdf.setFont(_PDF_FONT, 11)
        pdf.drawString(36, page_height / 2, _pdf_safe_text(f"Image load failed: {img_err}"))

    pdf.showPage()


def _collect_spare_parts_pages(cursor, folder_id, subfolder_id, category):
    title1, title2 = _resolve_spare_parts_titles(cursor, folder_id, subfolder_id, category)
    rows = _query_spare_parts_images(cursor, folder_id, subfolder_id, category)
    pages = []
    for row in rows:
        image_id = row[0] if len(row) > 0 else None
        filepath = row[2] if len(row) > 2 else ''
        if not filepath:
            continue
        if not os.path.exists(filepath):
            alt = os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(filepath.replace('\\', '/')))
            if os.path.exists(alt):
                filepath = alt
            else:
                continue
        matches, total_price = _query_image_matches(cursor, image_id)
        pages.append({
            'filepath': filepath,
            'matches': matches,
            'total_price': total_price,
            'title1': title1,
            'title2': title2,
        })
    return title2, pages


def _build_spare_parts_pdf(all_pages):
    from io import BytesIO
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4

    _ensure_pdf_fonts()
    version_text = _spare_parts_catalog_version()
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    total = len(all_pages)
    for idx, page in enumerate(all_pages):
        is_edge = (idx == 0 or idx == total - 1)
        _draw_spare_parts_page(
            pdf,
            page['filepath'],
            page['matches'],
            page['total_price'],
            page['title1'],
            page['title2'],
            version_text,
            is_edge,
        )
    pdf.save()
    buffer.seek(0)
    return buffer, version_text


@app.route('/api/part_price/spare_parts_pdf', methods=['GET'])
def api_download_spare_parts_pdf():
    """스페어 파트 이미지 PDF 다운로드 API (단일 하위 폴더)"""
    try:
        folder_id = request.args.get('folder_id')
        subfolder_id = request.args.get('subfolder_id')
        category = request.args.get('category', 'trolley')

        system = get_welding_system()
        cursor = system.connection.cursor()
        title2, pages = _collect_spare_parts_pages(cursor, folder_id, subfolder_id, category)
        if not pages:
            return jsonify({
                "success": False,
                "error": "PDF로 내보낼 이미지가 없습니다."
            }), 400

        buffer, _version = _build_spare_parts_pdf(pages)
        file_name = _ascii_pdf_filename(title2 or 'Spare_Parts')
        return _pdf_file_response(buffer, file_name)
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_download_spare_parts_pdf: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"PDF 생성 오류: {str(e)}"
        }), 500


@app.route('/api/part_price/spare_parts_pdf_batch', methods=['POST', 'OPTIONS'])
def api_download_spare_parts_pdf_batch():
    """체크된 스페어 파트 하위 항목을 하나의 PDF로 일괄 다운로드"""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        items = _parse_spare_parts_batch_items()
        print(f"[PDF batch] items={len(items) if isinstance(items, list) else 0}")
        if not isinstance(items, list) or len(items) == 0:
            return jsonify({
                "success": False,
                "error": "다운로드할 항목을 선택해주세요."
            }), 400

        system = get_welding_system()
        cursor = system.connection.cursor()

        all_pages = []
        names = []
        for item in items:
            folder_id = item.get('folder_id')
            subfolder_id = item.get('subfolder_id')
            category = item.get('category') or 'trolley'
            title2, pages = _collect_spare_parts_pages(cursor, folder_id, subfolder_id, category)
            if pages:
                all_pages.extend(pages)
                if title2:
                    names.append(title2)

        if not all_pages:
            return jsonify({
                "success": False,
                "error": "선택한 항목에 PDF로 변환 가능한 이미지가 없습니다."
            }), 400

        buffer, _version = _build_spare_parts_pdf(all_pages)
        if len(names) == 1:
            file_name = _ascii_pdf_filename(names[0])
        else:
            file_name = 'Spare_Parts_Selected.pdf'
        payload_len = len(buffer.getvalue())
        print(f"[PDF batch] ok pages={len(all_pages)} bytes={payload_len} file={file_name}")
        return _pdf_file_response(buffer, file_name)
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_download_spare_parts_pdf_batch: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"PDF 생성 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/add', methods=['POST'])
def api_add_part_price():
    """파트가격조회 부품 추가 API"""
    import sys
    try:
        print(f"[API] /api/part_price/add 호출됨")
        print(f"[API] Content-Type: {request.content_type}")
        sys.stdout.flush()
        
        # JSON 데이터 파싱
        if request.content_type and 'application/json' in request.content_type:
            data = request.get_json(silent=True)
        else:
            # Content-Type이 JSON이 아니면 직접 파싱 시도
            try:
                import json
                data = json.loads(request.data.decode('utf-8')) if request.data else None
            except:
                data = None
        
        if not data:
            print(f"[API] 요청 데이터가 없거나 JSON 파싱 실패")
            print(f"[API] 요청 본문: {request.data[:200] if request.data else 'None'}")
            sys.stdout.flush()
            return jsonify({
                "success": False,
                "error": "요청 데이터가 없습니다."
            }), 400
        
        material_code = data.get('material_code', '').strip()
        description = data.get('description', '').strip()
        price = data.get('price')
        
        print(f"[API] 자재코드: {material_code}, 설명: {description}, 가격: {price}")
        sys.stdout.flush()
        
        if not material_code:
            return jsonify({
                "success": False,
                "error": "자재코드를 입력해주세요."
            }), 400
        
        if not description:
            return jsonify({
                "success": False,
                "error": "설명을 입력해주세요."
            }), 400
        
        if price is None or price < 0:
            return jsonify({
                "success": False,
                "error": "가격을 올바르게 입력해주세요."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 테이블이 없으면 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_code TEXT UNIQUE,
                description TEXT,
                price REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        system.connection.commit()
        
        ensure_part_prices_price_source_column(cursor, system.connection)
        
        try:
            print(f"[API] 데이터베이스에 삽입 시도: {material_code}, {description}, {price}")
            sys.stdout.flush()
            
            cursor.execute('''
                INSERT INTO part_prices (material_code, description, price, price_source)
                VALUES (?, ?, ?, 'manual')
            ''', (material_code, description, float(price)))
            system.connection.commit()
            
            print(f"[API] 부품 추가 성공")
            sys.stdout.flush()
            
            response = jsonify({
                "success": True,
                "message": "부품이 추가되었습니다."
            })
            return response
        except Exception as e:
            system.connection.rollback()
            error_msg = str(e)
            print(f"[API] 데이터베이스 삽입 오류: {error_msg}")
            sys.stdout.flush()
            
            if 'UNIQUE constraint' in error_msg or 'UNIQUE' in error_msg.upper():
                return jsonify({
                    "success": False,
                    "error": "이미 존재하는 자재코드입니다."
                }), 400
            raise
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_add_part_price: {error_trace}")
        sys.stdout.flush()
        
        # JSON 응답을 보장하기 위해 명시적으로 jsonify 사용
        try:
            return jsonify({
                "success": False,
                "error": f"서버 오류: {str(e)}"
            }), 500
        except:
            # jsonify가 실패하면 직접 JSON 문자열 반환
            from flask import Response
            return Response(
                f'{{"success": false, "error": "서버 오류: {str(e).replace(chr(34), chr(39))}"}}',
                mimetype='application/json',
                status=500
            )

@app.route('/api/part_price/update', methods=['POST'])
def api_update_part_price():
    """파트가격조회 부품 수정 API"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "error": "요청 데이터가 없습니다."
            }), 400
        
        part_id = data.get('id')
        description = data.get('description', '').strip()
        price = data.get('price')
        
        if not part_id:
            return jsonify({
                "success": False,
                "error": "부품 ID가 필요합니다."
            }), 400
        
        if not description:
            return jsonify({
                "success": False,
                "error": "설명을 입력해주세요."
            }), 400
        
        if price is None or price < 0:
            return jsonify({
                "success": False,
                "error": "가격을 올바르게 입력해주세요."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        cursor.execute('''
            UPDATE part_prices
            SET description = ?, price = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (description, float(price), int(part_id)))
        
        if cursor.rowcount == 0:
            return jsonify({
                "success": False,
                "error": "부품을 찾을 수 없습니다."
            }), 404
        
        system.connection.commit()
        
        return jsonify({
            "success": True,
            "message": "부품 정보가 수정되었습니다."
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_update_part_price: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/delete', methods=['POST'])
def api_delete_part_price():
    """파트가격조회 부품 삭제 API"""
    import sys
    try:
        print(f"[API] /api/part_price/delete 호출됨")
        print(f"[API] Content-Type: {request.content_type}")
        sys.stdout.flush()
        
        # JSON 데이터 파싱
        if request.content_type and 'application/json' in request.content_type:
            data = request.get_json(silent=True)
        else:
            try:
                import json
                data = json.loads(request.data.decode('utf-8')) if request.data else None
            except:
                data = None
        
        if not data:
            print(f"[API] 요청 데이터가 없거나 JSON 파싱 실패")
            sys.stdout.flush()
            return jsonify({
                "success": False,
                "error": "요청 데이터가 없습니다."
            }), 400
        
        part_id = data.get('id')
        print(f"[API] 삭제할 부품 ID: {part_id}")
        sys.stdout.flush()
        
        if not part_id:
            return jsonify({
                "success": False,
                "error": "부품 ID가 필요합니다."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        cursor.execute('''
            DELETE FROM part_prices
            WHERE id = ?
        ''', (int(part_id),))
        
        deleted_count = cursor.rowcount
        print(f"[API] 삭제된 행 수: {deleted_count}")
        sys.stdout.flush()
        
        if deleted_count == 0:
            return jsonify({
                "success": False,
                "error": "부품을 찾을 수 없습니다."
            }), 404
        
        system.connection.commit()
        
        print(f"[API] 부품 삭제 성공")
        sys.stdout.flush()
        
        return jsonify({
            "success": True,
            "message": "부품이 삭제되었습니다."
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_delete_part_price: {error_trace}")
        sys.stdout.flush()
        
        # JSON 응답을 보장하기 위해 명시적으로 jsonify 사용
        try:
            return jsonify({
                "success": False,
                "error": f"서버 오류: {str(e)}"
            }), 500
        except:
            # jsonify가 실패하면 직접 JSON 문자열 반환
            from flask import Response
            return Response(
                f'{{"success": false, "error": "서버 오류: {str(e).replace(chr(34), chr(39))}"}}',
                mimetype='application/json',
                status=500
            )

@app.route('/api/part_price/bulk_delete', methods=['POST'])
def api_bulk_delete_part_price():
    """파트가격조회 부품 일괄 삭제 API"""
    import sys
    try:
        print(f"[API] /api/part_price/bulk_delete 호출됨")
        print(f"[API] Content-Type: {request.content_type}")
        sys.stdout.flush()
        
        # JSON 데이터 파싱
        if request.content_type and 'application/json' in request.content_type:
            data = request.get_json(silent=True)
        else:
            try:
                import json
                data = json.loads(request.data.decode('utf-8')) if request.data else None
            except:
                data = None
        
        if not data:
            print(f"[API] 요청 데이터가 없거나 JSON 파싱 실패")
            sys.stdout.flush()
            return jsonify({
                "success": False,
                "error": "요청 데이터가 없습니다."
            }), 400
        
        part_ids = data.get('part_ids', [])
        print(f"[API] 삭제할 부품 ID 목록: {part_ids}")
        sys.stdout.flush()
        
        if not part_ids or not isinstance(part_ids, list):
            return jsonify({
                "success": False,
                "error": "삭제할 부품 ID 목록이 필요합니다."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        placeholders = ','.join(['?'] * len(part_ids))
        cursor.execute(f'''
            DELETE FROM part_prices
            WHERE id IN ({placeholders})
        ''', [int(pid) for pid in part_ids])
        
        deleted_count = cursor.rowcount
        system.connection.commit()
        
        print(f"[API] 일괄 삭제 성공: {deleted_count}개")
        sys.stdout.flush()
        
        return jsonify({
            "success": True,
            "message": f"{deleted_count}개의 부품이 삭제되었습니다.",
            "deleted_count": deleted_count
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_bulk_delete_part_price: {error_trace}")
        sys.stdout.flush()
        
        # JSON 응답을 보장하기 위해 명시적으로 jsonify 사용
        try:
            return jsonify({
                "success": False,
                "error": f"서버 오류: {str(e)}"
            }), 500
        except:
            # jsonify가 실패하면 직접 JSON 문자열 반환
            from flask import Response
            return Response(
                f'{{"success": false, "error": "서버 오류: {str(e).replace(chr(34), chr(39))}"}}',
                mimetype='application/json',
                status=500
            )

@app.route('/api/part_price/excel', methods=['POST'])
def api_export_part_price_excel():
    """파트가격조회 엑셀 다운로드 API"""
    import sys
    try:
        print(f"[API] /api/part_price/excel 호출됨")
        print(f"[API] Content-Type: {request.content_type}")
        sys.stdout.flush()
        
        # JSON 데이터 파싱
        if request.content_type and 'application/json' in request.content_type:
            data = request.get_json(silent=True)
        else:
            try:
                import json
                data = json.loads(request.data.decode('utf-8')) if request.data else None
            except:
                data = None
        
        if not data or 'parts' not in data:
            print(f"[API] 요청 데이터가 없거나 parts 필드가 없음")
            sys.stdout.flush()
            return jsonify({
                "success": False,
                "error": "부품 데이터가 필요합니다."
            }), 400
        
        parts = data.get('parts', [])
        print(f"[API] 다운로드할 부품 수: {len(parts)}")
        sys.stdout.flush()
        
        if not parts:
            return jsonify({
                "success": False,
                "error": "다운로드할 부품이 없습니다."
            }), 400
        
        import tempfile
        import os
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from datetime import datetime
        from flask import Response
        
        print(f"[API] 엑셀 파일 생성 시작")
        sys.stdout.flush()
        
        # 임시 파일 생성
        temp_dir = tempfile.mkdtemp()
        temp_filename = os.path.join(temp_dir, f"part_price_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        
        # 엑셀 파일 생성
        wb = Workbook()
        ws = wb.active
        ws.title = "파트가격조회"
        
        # 헤더 스타일 설정
        header_font = Font(bold=True, color="FFFFFF", size=12)
        header_fill = PatternFill(start_color="800020", end_color="800020", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center")
        
        # 헤더 작성
        headers = ['번호', '자재코드', '설명', '가격(EUR)']
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
        
        # 데이터 작성
        for row_idx, part in enumerate(parts, 2):
            ws.cell(row=row_idx, column=1, value=part.get('번호', ''))
            ws.cell(row=row_idx, column=2, value=part.get('자재코드', ''))
            ws.cell(row=row_idx, column=3, value=part.get('설명', ''))
            price = part.get('가격', 0)
            price_cell = ws.cell(row=row_idx, column=4, value=float(price) if price else 0)
            price_cell.number_format = '#,##0.00'
        
        # 열 너비 조정
        ws.column_dimensions['A'].width = 10
        ws.column_dimensions['B'].width = 20
        ws.column_dimensions['C'].width = 40
        ws.column_dimensions['D'].width = 15
        
        # 파일 저장
        print(f"[API] 엑셀 파일 저장 중: {temp_filename}")
        sys.stdout.flush()
        wb.save(temp_filename)
        
        # 파일 읽기
        print(f"[API] 엑셀 파일 읽기 중")
        sys.stdout.flush()
        with open(temp_filename, 'rb') as f:
            file_data = f.read()
        
        print(f"[API] 파일 크기: {len(file_data)} bytes")
        sys.stdout.flush()
        
        # 임시 파일 삭제
        try:
            os.remove(temp_filename)
            os.rmdir(temp_dir)
        except Exception as e:
            print(f"임시 파일 삭제 오류 (무시 가능): {e}")
        
        # 응답 생성
        date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"part_price_{date_str}.xlsx"
        
        print(f"[API] 응답 생성 완료: {filename}, 크기: {len(file_data)} bytes")
        sys.stdout.flush()
        
        response = Response(
            file_data,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'Content-Length': str(len(file_data)),
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0'
            }
        )
        
        return response
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_export_part_price_excel: {error_trace}")
        sys.stdout.flush()
        
        # JSON 응답을 보장하기 위해 명시적으로 jsonify 사용
        try:
            return jsonify({
                "success": False,
                "error": f"서버 오류: {str(e)}"
            }), 500
        except:
            # jsonify가 실패하면 직접 JSON 문자열 반환
            from flask import Response
            return Response(
                f'{{"success": false, "error": "서버 오류: {str(e).replace(chr(34), chr(39))}"}}',
                mimetype='application/json',
                status=500
            )

@app.route('/api/part_price/image/<int:image_id>', methods=['DELETE'])
def api_delete_part_price_image(image_id):
    """파트가격조회 이미지 삭제 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 이미지 테이블이 없으면 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_price_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                file_size INTEGER,
                folder_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        system.connection.commit()
        
        cursor.execute('''
            SELECT filepath, category
            FROM part_price_images
            WHERE id = ?
        ''', (image_id,))
        
        row = cursor.fetchone()
        if not row:
            return jsonify({
                "success": False,
                "error": "이미지를 찾을 수 없습니다."
            }), 404
        
        filepath = row[0]
        category = row[1] if len(row) > 1 else None
        
        # 파일 삭제
        deleted_file = False
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
                deleted_file = True
        except Exception as e:
            print(f"파일 삭제 오류: {e}")
            import traceback
            print(traceback.format_exc())
        
        # 데이터베이스에서 이미지 삭제 (CASCADE로 part_price_image_parts도 자동 삭제됨)
        cursor.execute('''
            DELETE FROM part_price_images
            WHERE id = ?
        ''', (image_id,))
        system.connection.commit()
        
        return jsonify({
            "success": True,
            "message": "이미지가 삭제되었습니다.",
            "deleted_file": deleted_file
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_delete_part_price_image: {error_trace}")
        import sys
        sys.stdout.flush()
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/folders', methods=['GET'])
def api_get_part_price_folders():
    """파트가격조회 폴더 목록 조회 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 폴더 테이블 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_price_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        system.connection.commit()
        
        # 폴더 목록 조회 (자동 생성 로직 제거 - 사용자가 직접 폴더를 추가하도록 함)
        cursor.execute('''
            SELECT id, folder_name, created_at
            FROM part_price_folders
            ORDER BY created_at DESC
        ''')
        
        folders = []
        rows = cursor.fetchall()
        for row in rows:
            folders.append({
                'id': row[0],
                'folder_name': row[1],
                'created_at': row[2]
            })
        
        return jsonify({
            "success": True,
            "folders": folders
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_get_part_price_folders: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/folders', methods=['POST'])
def api_add_part_price_folder():
    """파트가격조회 폴더 추가 API"""
    try:
        if not request.is_json:
            return jsonify({
                "success": False,
                "error": "Content-Type이 application/json이어야 합니다."
            }), 400
        
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "error": "요청 데이터가 없습니다."
            }), 400
        
        folder_name = data.get('folder_name', '').strip()
        
        if not folder_name:
            return jsonify({
                "success": False,
                "error": "폴더 이름이 필요합니다."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 폴더 테이블 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_price_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        system.connection.commit()
        
        # 중복 확인
        cursor.execute('SELECT id FROM part_price_folders WHERE folder_name = ?', (folder_name,))
        if cursor.fetchone():
            return jsonify({
                "success": False,
                "error": "이미 존재하는 폴더 이름입니다."
            }), 400
        
        # 폴더 추가
        cursor.execute('''
            INSERT INTO part_price_folders (folder_name)
            VALUES (?)
        ''', (folder_name,))
        system.connection.commit()
        
        folder_id = cursor.lastrowid
        
        return jsonify({
            "success": True,
            "message": "폴더가 추가되었습니다.",
            "folder": {
                "id": folder_id,
                "folder_name": folder_name
            }
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_add_part_price_folder: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/folders/<int:folder_id>', methods=['DELETE'])
def api_delete_part_price_folder(folder_id):
    """파트가격조회 폴더 삭제 API"""
    try:
        print(f"폴더 삭제 요청: folder_id={folder_id}")
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 폴더 존재 확인
        cursor.execute('SELECT id, folder_name FROM part_price_folders WHERE id = ?', (folder_id,))
        folder = cursor.fetchone()
        
        if not folder:
            print(f"폴더를 찾을 수 없음: folder_id={folder_id}")
            return jsonify({
                "success": False,
                "error": "폴더를 찾을 수 없습니다."
            }), 404
        
        folder_name = folder[1]
        print(f"폴더 삭제 시작: {folder_name} (ID: {folder_id})")
        
        # part_price_images 테이블에 folder_id 컬럼이 있는지 확인하고 없으면 추가
        try:
            cursor.execute("PRAGMA table_info(part_price_images)")
            columns = cursor.fetchall()
            column_names = [col[1] for col in columns]
            
            if 'folder_id' not in column_names:
                cursor.execute('ALTER TABLE part_price_images ADD COLUMN folder_id INTEGER')
                system.connection.commit()
        except Exception as e:
            print(f"컬럼 추가 오류 (이미 존재할 수 있음): {e}")
        
        # part_price_subfolders 테이블 생성 확인
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS part_price_subfolders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    folder_id INTEGER NOT NULL,
                    subfolder_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(folder_id, subfolder_name)
                )
            ''')
            system.connection.commit()
        except Exception as e:
            print(f"하위 폴더 테이블 확인 오류 (이미 존재할 수 있음): {e}")
        
        # 해당 폴더의 하위 폴더 조회
        cursor.execute('SELECT id FROM part_price_subfolders WHERE folder_id = ?', (folder_id,))
        subfolders = cursor.fetchall()
        
        deleted_subfolders_count = 0
        deleted_images_count = 0
        deleted_files_count = 0
        
        # 각 하위 폴더의 이미지 삭제
        for subfolder_row in subfolders:
            subfolder_id = subfolder_row[0]
            
            # 하위 폴더에 연결된 이미지 조회
            cursor.execute('''
                SELECT id, filepath FROM part_price_images 
                WHERE subfolder_id = ?
            ''', (subfolder_id,))
            subfolder_images = cursor.fetchall()
            
            # 하위 폴더의 이미지 파일 삭제 및 데이터베이스에서 삭제
            for image in subfolder_images:
                image_id = image[0]
                filepath = image[1]
                
                # 파일 삭제
                try:
                    if filepath and os.path.exists(filepath):
                        os.remove(filepath)
                        deleted_files_count += 1
                except Exception as e:
                    print(f"파일 삭제 오류: {filepath}, {e}")
                
                # 데이터베이스에서 이미지 삭제
                try:
                    cursor.execute('DELETE FROM part_price_images WHERE id = ?', (image_id,))
                    deleted_images_count += 1
                except Exception as e:
                    print(f"이미지 삭제 오류: {image_id}, {e}")
            
            # 하위 폴더 삭제
            try:
                cursor.execute('DELETE FROM part_price_subfolders WHERE id = ?', (subfolder_id,))
                deleted_subfolders_count += 1
            except Exception as e:
                print(f"하위 폴더 삭제 오류: {subfolder_id}, {e}")
        
        # 해당 폴더에 직접 연결된 이미지 조회 (folder_id로 직접 연결된 경우)
        cursor.execute('''
            SELECT id, filepath FROM part_price_images 
            WHERE folder_id = ?
        ''', (folder_id,))
        images = cursor.fetchall()
        
        # 폴더에 직접 연결된 이미지 파일 삭제 및 데이터베이스에서 삭제
        for image in images:
            image_id = image[0]
            filepath = image[1]
            
            # 파일 삭제
            try:
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
                    deleted_files_count += 1
            except Exception as e:
                print(f"파일 삭제 오류: {filepath}, {e}")
            
            # 데이터베이스에서 이미지 삭제 (CASCADE로 part_price_image_parts도 자동 삭제됨)
            try:
                cursor.execute('DELETE FROM part_price_images WHERE id = ?', (image_id,))
                deleted_images_count += 1
            except Exception as e:
                print(f"이미지 삭제 오류: {image_id}, {e}")
        
        # 폴더 삭제
        cursor.execute('DELETE FROM part_price_folders WHERE id = ?', (folder_id,))
        deleted_rows = cursor.rowcount
        system.connection.commit()
        
        # 삭제 확인
        if deleted_rows == 0:
            return jsonify({
                "success": False,
                "error": "폴더 삭제에 실패했습니다. 폴더가 존재하지 않거나 이미 삭제되었을 수 있습니다."
            }), 404
        
        print(f"폴더 삭제 완료: {folder_name} (ID: {folder_id}), 하위 폴더: {deleted_subfolders_count}개, 이미지: {deleted_images_count}개")
        
        return jsonify({
            "success": True,
            "message": f"폴더와 {deleted_subfolders_count}개의 하위 폴더, {deleted_images_count}개의 이미지가 삭제되었습니다.",
            "deleted_subfolders": deleted_subfolders_count,
            "deleted_images": deleted_images_count,
            "deleted_files": deleted_files_count
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_delete_part_price_folder: {error_trace}")
        import sys
        sys.stdout.flush()
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/subfolders', methods=['GET'])
def api_get_part_price_subfolders():
    """파트가격조회 하위 폴더 목록 조회 API"""
    try:
        folder_id = request.args.get('folder_id')
        if not folder_id:
            return jsonify({
                "success": False,
                "error": "폴더 ID가 필요합니다."
            }), 400
        
        try:
            folder_id = int(folder_id)
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "유효하지 않은 폴더 ID입니다."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 하위 폴더 테이블 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_price_subfolders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER NOT NULL,
                subfolder_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (folder_id) REFERENCES part_price_folders(id) ON DELETE CASCADE,
                UNIQUE(folder_id, subfolder_name)
            )
        ''')
        system.connection.commit()
        
        # 하위 폴더 자동 생성 제거 - 사용자가 직접 추가해야 함
        
        # 하위 폴더 목록 조회
        cursor.execute('''
            SELECT id, subfolder_name, created_at
            FROM part_price_subfolders
            WHERE folder_id = ?
            ORDER BY created_at DESC
        ''', (folder_id,))
        
        subfolders = []
        rows = cursor.fetchall()
        for row in rows:
            subfolders.append({
                'id': row[0],
                'subfolder_name': row[1],
                'created_at': row[2]
            })
        
        return jsonify({
            "success": True,
            "subfolders": subfolders
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_get_part_price_subfolders: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/subfolders', methods=['POST'])
def api_add_part_price_subfolder():
    """파트가격조회 하위 폴더 추가 API"""
    try:
        if not request.is_json:
            return jsonify({
                "success": False,
                "error": "Content-Type이 application/json이어야 합니다."
            }), 400
        
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "error": "요청 데이터가 없습니다."
            }), 400
        
        folder_id = data.get('folder_id')
        subfolder_name = data.get('subfolder_name', '').strip()
        
        if not folder_id:
            return jsonify({
                "success": False,
                "error": "폴더 ID가 필요합니다."
            }), 400
        
        if not subfolder_name:
            return jsonify({
                "success": False,
                "error": "하위 폴더 이름이 필요합니다."
            }), 400
        
        try:
            folder_id = int(folder_id)
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "유효하지 않은 폴더 ID입니다."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 하위 폴더 테이블 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS part_price_subfolders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER NOT NULL,
                subfolder_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (folder_id) REFERENCES part_price_folders(id) ON DELETE CASCADE,
                UNIQUE(folder_id, subfolder_name)
            )
        ''')
        system.connection.commit()
        
        # 중복 확인
        cursor.execute('SELECT id FROM part_price_subfolders WHERE folder_id = ? AND subfolder_name = ?', (folder_id, subfolder_name))
        if cursor.fetchone():
            return jsonify({
                "success": False,
                "error": "이미 존재하는 하위 폴더 이름입니다."
            }), 400
        
        # 하위 폴더 추가
        cursor.execute('''
            INSERT INTO part_price_subfolders (folder_id, subfolder_name)
            VALUES (?, ?)
        ''', (folder_id, subfolder_name))
        system.connection.commit()
        
        subfolder_id = cursor.lastrowid
        
        return jsonify({
            "success": True,
            "message": "하위 폴더가 추가되었습니다.",
            "subfolder": {
                "id": subfolder_id,
                "subfolder_name": subfolder_name
            }
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_add_part_price_subfolder: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/part_price/subfolders/<int:subfolder_id>', methods=['DELETE'])
def api_delete_part_price_subfolder(subfolder_id):
    """파트가격조회 하위 폴더 삭제 API"""
    try:
        folder_id = request.args.get('folder_id')
        if not folder_id:
            return jsonify({
                "success": False,
                "error": "폴더 ID가 필요합니다."
            }), 400
        
        try:
            folder_id = int(folder_id)
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "유효하지 않은 폴더 ID입니다."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 하위 폴더 존재 확인
        cursor.execute('SELECT id, subfolder_name FROM part_price_subfolders WHERE id = ? AND folder_id = ?', (subfolder_id, folder_id))
        subfolder = cursor.fetchone()
        
        if not subfolder:
            return jsonify({
                "success": False,
                "error": "하위 폴더를 찾을 수 없습니다."
            }), 404
        
        # 해당 하위 폴더에 연결된 이미지 조회
        cursor.execute('''
            SELECT id, filepath FROM part_price_images 
            WHERE subfolder_id = ?
        ''', (subfolder_id,))
        images = cursor.fetchall()
        
        deleted_images_count = 0
        deleted_files_count = 0
        
        # 이미지 파일 삭제 및 데이터베이스에서 삭제
        for image in images:
            image_id = image[0]
            filepath = image[1]
            
            # 파일 삭제
            try:
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
                    deleted_files_count += 1
            except Exception as e:
                print(f"파일 삭제 오류: {filepath}, {e}")
            
            # 데이터베이스에서 이미지 삭제
            try:
                cursor.execute('DELETE FROM part_price_images WHERE id = ?', (image_id,))
                deleted_images_count += 1
            except Exception as e:
                print(f"이미지 삭제 오류: {image_id}, {e}")
        
        # 하위 폴더 삭제
        cursor.execute('DELETE FROM part_price_subfolders WHERE id = ?', (subfolder_id,))
        system.connection.commit()
        
        return jsonify({
            "success": True,
            "message": f"하위 폴더와 {deleted_images_count}개의 이미지가 삭제되었습니다.",
            "deleted_images": deleted_images_count,
            "deleted_files": deleted_files_count
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_delete_part_price_subfolder: {error_trace}")
        import sys
        sys.stdout.flush()
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_quotation/products', methods=['GET'])
def api_get_export_quotation_products():
    """수출견적서 저장된 제품 정보 조회 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 테이블이 존재하는지 확인하고 없으면 생성
        try:
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
            system.connection.commit()
        except Exception as e:
            print(f"테이블 생성 확인 오류: {e}")
        
        # 검색어가 있으면 필터링
        search_term = request.args.get('search', '').strip()
        
        if search_term:
            cursor.execute('''
                SELECT id, product_code, product_name, price_book_price, product_cost
                FROM export_quotation_products
                WHERE product_code LIKE ? OR product_name LIKE ?
                ORDER BY product_code ASC
            ''', (f'%{search_term}%', f'%{search_term}%'))
        else:
            cursor.execute('''
                SELECT id, product_code, product_name, price_book_price, product_cost
                FROM export_quotation_products
                ORDER BY product_code ASC
            ''')
        
        products = []
        for row in cursor.fetchall():
            try:
                if hasattr(row, 'keys') and callable(getattr(row, 'keys', None)):
                    products.append({
                        'id': row['id'],
                        'code': row['product_code'] or '',
                        'name': row['product_name'] or '',
                        'priceBookPrice': float(row['price_book_price']) if row['price_book_price'] is not None else 0,
                        'productCost': float(row['product_cost']) if row['product_cost'] is not None else 0
                    })
                else:
                    products.append({
                        'id': row[0],
                        'code': row[1] or '',
                        'name': row[2] or '',
                        'priceBookPrice': float(row[3]) if row[3] is not None else 0,
                        'productCost': float(row[4]) if row[4] is not None else 0
                    })
            except Exception as e:
                print(f"제품 데이터 처리 오류: {e}")
                continue
        
        return jsonify({
            "success": True,
            "products": products,
            "count": len(products)
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_get_export_quotation_products: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_quotation/download_excel', methods=['POST'])
def api_download_export_quotation_excel():
    """수출견적서 엑셀 다운로드 API"""
    try:
        print("=== 수출견적서 엑셀 다운로드 API 호출됨 ===")
        data = request.get_json(silent=True)
        if not data:
            print("요청 데이터가 없습니다.")
            return jsonify({"error": "요청 데이터가 없습니다."}), 400
        
        products = data.get('products', [])
        settings = data.get('settings', {})
        totals = data.get('totals', {})
        
        print(f"제품 수: {len(products)}")
        print(f"설정: {settings}")
        
        if not products:
            print("제품 데이터가 없습니다.")
            return jsonify({"error": "제품 데이터가 없습니다."}), 400
        
        # 임시 파일 생성
        import tempfile
        import os
        temp_dir = tempfile.mkdtemp()
        temp_filename = os.path.join(temp_dir, "export_quotation.xlsx")
        
        # 엑셀 파일 생성
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from datetime import datetime
        
        wb = Workbook()
        ws = wb.active
        ws.title = "수출견적서"
        
        # 스타일 정의
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(start_color="800020", end_color="800020", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        # 제목 행
        ws.merge_cells('A1:N1')
        ws['A1'] = '수출견적서'
        ws['A1'].font = Font(bold=True, size=16)
        ws['A1'].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 30
        
        # 설정 정보 행 (2-5행)
        row = 2
        ws[f'A{row}'] = '청구DAP'
        ws[f'B{row}'] = settings.get('claimDAP', 0)
        ws[f'C{row}'] = '발생DAP'
        ws[f'D{row}'] = settings.get('occurredDAP', 0)
        ws[f'E{row}'] = '할인율'
        ws[f'F{row}'] = f"{settings.get('discountRate', 0)}%"
        ws[f'G{row}'] = '부대비용'
        ws[f'H{row}'] = settings.get('incidentalExpense', 0)
        ws[f'I{row}'] = 'KRW 환율'
        ws[f'J{row}'] = settings.get('krwExchangeRate', 1690)
        
        row = 3  # 빈 행
        
        # 헤더 행
        row = 4
        headers = [
            '제품코드', '제품명', '판매수량',
            '프라이스북 가격', 'DAP 포함가 판매단가', '할인가', '프라이스북 합계', 'DAP포함 판매합산',
            '제품원가', '발생 DAP', '부대비용', '총원가합산', '마진율'
        ]
        
        for col_idx, header in enumerate(headers, start=1):
            cell = ws.cell(row=row, column=col_idx)
            cell.value = header
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = border
        
        ws.row_dimensions[row].height = 40
        
        # 제품 데이터 행
        row = 5
        print(f"제품 데이터 작성 시작, 총 {len(products)}개")
        try:
            for idx, product in enumerate(products):
                if idx % 100 == 0:
                    print(f"제품 데이터 작성 중: {idx}/{len(products)}")
                
                # 안전하게 값 가져오기
                try:
                    ws.cell(row=row, column=1).value = str(product.get('code', ''))[:100]  # 제품코드 최대 100자
                    ws.cell(row=row, column=2).value = str(product.get('name', ''))[:200]  # 제품명 최대 200자
                    ws.cell(row=row, column=3).value = float(product.get('salesQuantity', 0) or 0)
                    ws.cell(row=row, column=4).value = float(product.get('priceBookPrice', 0) or 0)
                    ws.cell(row=row, column=5).value = float(product.get('dapIncludedUnitPrice', 0) or 0)
                    ws.cell(row=row, column=6).value = float(product.get('discountedPrice', 0) or 0)
                    ws.cell(row=row, column=7).value = float(product.get('priceBookTotal', 0) or 0)
                    ws.cell(row=row, column=8).value = float(product.get('dapIncludedSalesTotal', 0) or 0)
                    ws.cell(row=row, column=9).value = float(product.get('productCost', 0) or 0)
                    ws.cell(row=row, column=10).value = float(product.get('occurredDAP', 0) or 0)
                    ws.cell(row=row, column=11).value = float(product.get('incidentalExpenseIndividual', 0) or 0)
                    ws.cell(row=row, column=12).value = float(product.get('totalCost', 0) or 0)
                    margin_rate = product.get('marginRate', 0)
                    if isinstance(margin_rate, (int, float)):
                        ws.cell(row=row, column=13).value = f"{float(margin_rate):.2f}%"
                    else:
                        ws.cell(row=row, column=13).value = str(margin_rate)[:20]
                except Exception as cell_error:
                    print(f"제품 {idx} 데이터 작성 오류: {cell_error}")
                    # 기본값으로 계속 진행
                    ws.cell(row=row, column=1).value = ''
                    ws.cell(row=row, column=2).value = ''
                    for col in range(3, 13):
                        ws.cell(row=row, column=col).value = 0
                    ws.cell(row=row, column=13).value = '0%'
                
                # 숫자 형식 적용
                try:
                    for col in [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
                        cell = ws.cell(row=row, column=col)
                        if cell.value is not None:
                            cell.number_format = '#,##0.00'
                            cell.alignment = Alignment(horizontal="right", vertical="center")
                        cell.border = border
                    
                    # 텍스트 셀 정렬
                    ws.cell(row=row, column=1).alignment = Alignment(horizontal="left", vertical="center")
                    ws.cell(row=row, column=2).alignment = Alignment(horizontal="left", vertical="center")
                    ws.cell(row=row, column=13).alignment = Alignment(horizontal="right", vertical="center")
                except Exception as format_error:
                    print(f"제품 {idx} 형식 적용 오류: {format_error}")
                
                row += 1
        except Exception as data_error:
            print(f"제품 데이터 작성 중 오류: {data_error}")
            import traceback
            print(traceback.format_exc())
            raise
        
        print(f"제품 데이터 작성 완료, 총 {row - 5}개 행")
        
        # 합계 행
        ws.cell(row=row, column=1).value = '합계수량'
        ws.cell(row=row, column=3).value = totals.get('totalQuantity', 0)
        ws.cell(row=row, column=7).value = totals.get('totalPriceBook', 0)
        ws.cell(row=row, column=8).value = totals.get('totalDAPSales', 0)
        ws.cell(row=row, column=9).value = totals.get('totalProductCost', 0)
        ws.cell(row=row, column=10).value = totals.get('totalOccurredDAP', 0)
        ws.cell(row=row, column=11).value = totals.get('totalIncidentalExpense', 0)
        ws.cell(row=row, column=12).value = totals.get('totalCost', 0)
        ws.cell(row=row, column=13).value = f"{totals.get('totalMarginRate', 0)}%"
        
        # 합계 행 스타일
        for col in range(1, 14):
            cell = ws.cell(row=row, column=col)
            cell.font = Font(bold=True, color="FF0000")
            if col in [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
                cell.number_format = '#,##0'
                cell.alignment = Alignment(horizontal="right", vertical="center")
            cell.border = border
        
        # 열 너비 자동 조정
        column_widths = {
            'A': 15,  # 제품코드
            'B': 30,  # 제품명
            'C': 12,  # 판매수량
            'D': 18,  # 프라이스북 가격
            'E': 20,  # DAP 포함가 판매단가
            'F': 12,  # 할인가
            'G': 18,  # 프라이스북 합계
            'H': 18,  # DAP포함 판매합산
            'I': 15,  # 제품원가
            'J': 15,  # 발생 DAP
            'K': 12,  # 부대비용
            'L': 15,  # 총원가합산
            'M': 12   # 마진율
        }
        
        for col_letter, width in column_widths.items():
            ws.column_dimensions[col_letter].width = width
        
        # 파일 저장
        print(f"엑셀 파일 저장 중: {temp_filename}")
        try:
            wb.save(temp_filename)
            print("엑셀 파일 저장 완료")
        except Exception as save_error:
            print(f"엑셀 파일 저장 오류: {save_error}")
            import traceback
            print(traceback.format_exc())
            # 워크북 정리
            try:
                wb.close()
            except:
                pass
            raise
        
        # 워크북 정리 (메모리 해제)
        try:
            wb.close()
        except:
            pass
        
        # 파일을 메모리로 읽어서 응답
        print("파일 읽기 시작")
        file_data = None
        try:
            with open(temp_filename, 'rb') as f:
                file_data = f.read()
            print(f"파일 읽기 완료, 크기: {len(file_data)} bytes")
        except Exception as read_error:
            print(f"파일 읽기 오류: {read_error}")
            import traceback
            print(traceback.format_exc())
            raise
        finally:
            # 임시 파일 삭제 (메모리 확보)
            try:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
                if os.path.exists(temp_dir):
                    os.rmdir(temp_dir)
                print("임시 파일 삭제 완료")
            except Exception as cleanup_error:
                print(f"임시 파일 삭제 오류 (무시 가능): {cleanup_error}")
        
        if not file_data or len(file_data) == 0:
            raise Exception("생성된 파일이 비어있습니다.")
        
        # 응답 생성
        from flask import Response
        date_str = datetime.now().strftime("%Y%m%d")
        safe_filename = f"export_quotation_{date_str}.xlsx"
        
        print(f"응답 생성 시작, 파일명: {safe_filename}, 크기: {len(file_data)} bytes")
        
        try:
            response = Response(
                file_data,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={
                    'Content-Disposition': f'attachment; filename="{safe_filename}"',
                    'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'Content-Length': str(len(file_data)),
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Pragma': 'no-cache',
                    'Expires': '0'
                }
            )
            
            print(f"수출견적서 엑셀 파일 생성 완료: {len(products)}개 제품, {len(file_data)} bytes")
            return response
        except Exception as response_error:
            print(f"응답 생성 오류: {response_error}")
            import traceback
            print(traceback.format_exc())
            raise
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_download_export_quotation_excel: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"엑셀 생성 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/dashboard', methods=['GET'])
def api_export_price_dashboard():
    """수출가격 대시보드 API - 통계 및 제품 목록 조회"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 테이블이 존재하는지 확인하고 없으면 생성
        try:
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
            system.connection.commit()
        except Exception as e:
            print(f"테이블 생성 확인 오류: {e}")
        
        # 모든 제품 조회
        cursor.execute('''
            SELECT id, product_code, product_name, price_book_price, product_cost, created_at, updated_at
            FROM export_quotation_products
            ORDER BY created_at DESC
        ''')
        
        products = []
        total_price_book = 0
        total_cost = 0
        total_margin = 0
        
        for row in cursor.fetchall():
            try:
                if hasattr(row, 'keys') and callable(getattr(row, 'keys', None)):
                    price_book_price = float(row['price_book_price']) if row['price_book_price'] is not None else 0
                    product_cost = float(row['product_cost']) if row['product_cost'] is not None else 0
                    margin = price_book_price - product_cost
                    
                    products.append({
                        'id': row['id'],
                        'code': row['product_code'] or '',
                        'name': row['product_name'] or '',
                        'priceBookPrice': price_book_price,
                        'productCost': product_cost,
                        'createdAt': row['created_at'] if 'created_at' in row.keys() else None,
                        'updatedAt': row['updated_at'] if 'updated_at' in row.keys() else None
                    })
                    
                    total_price_book += price_book_price
                    total_cost += product_cost
                    total_margin += margin
                else:
                    price_book_price = float(row[3]) if row[3] is not None else 0
                    product_cost = float(row[4]) if row[4] is not None else 0
                    margin = price_book_price - product_cost
                    
                    products.append({
                        'id': row[0],
                        'code': row[1] or '',
                        'name': row[2] or '',
                        'priceBookPrice': price_book_price,
                        'productCost': product_cost,
                        'createdAt': row[5] if len(row) > 5 else None,
                        'updatedAt': row[6] if len(row) > 6 else None
                    })
                    
                    total_price_book += price_book_price
                    total_cost += product_cost
                    total_margin += margin
            except Exception as e:
                print(f"제품 데이터 처리 오류: {e}")
                continue
        
        # 통계 계산
        total_products = len(products)
        average_margin_rate = (total_margin / total_price_book * 100) if total_price_book > 0 else 0
        
        statistics = {
            'totalProducts': total_products,
            'totalPriceBook': total_price_book,
            'totalCost': total_cost,
            'totalMargin': total_margin,
            'averageMarginRate': average_margin_rate
        }
        
        return jsonify({
            "success": True,
            "products": products,
            "statistics": statistics,
            "count": total_products
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_export_price_dashboard: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/dashboard_new', methods=['GET'])
def api_export_price_dashboard_new():
    """수출견적서(스태프) 대시보드 API - 통계 및 제품 목록 조회"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 모든 제품 조회 - price_book_price, purchase_price, category_number 포함
        try:
            cursor.execute('''
                SELECT id, product_code, product_name, 
                       price_book_price,
                       purchase_price,
                       category_number,
                       created_at, updated_at
                FROM export_quotation_new_products
                ORDER BY created_at DESC
            ''')
        except Exception as e:
            print(f"쿼리 실행 오류: {e}")
            # 컬럼이 없는 경우 대체 쿼리
            try:
                cursor.execute('''
                    SELECT id, product_code, product_name, 
                           COALESCE(unit_price, 0) as price_book_price,
                           0 as purchase_price,
                           category_number,
                           created_at, updated_at
                    FROM export_quotation_new_products
                    ORDER BY created_at DESC
                ''')
            except Exception as e2:
                print(f"대체 쿼리 실행 오류: {e2}")
                return jsonify({
                    "success": False,
                    "error": f"데이터 조회 오류: {str(e2)}"
                }), 500
        
        products = []
        total_price_book = 0
        total_purchase = 0
        total_margin = 0
        
        rows = cursor.fetchall()
        print(f"조회된 행 수: {len(rows)}")
        
        for row in rows:
            try:
                # SQLite는 튜플을 반환하므로 인덱스로 접근
                # SELECT 순서: id, product_code, product_name, price_book_price, purchase_price, category_number, created_at, updated_at
                row_id = row[0]
                product_code = row[1] if len(row) > 1 else ''
                product_name = row[2] if len(row) > 2 else ''
                # COALESCE 결과이므로 직접 접근
                price_book_price_val = row[3] if len(row) > 3 else None
                purchase_price_val = row[4] if len(row) > 4 else None
                category_number_val = row[5] if len(row) > 5 else None
                
                # None 체크 및 float 변환
                price_book_price = float(price_book_price_val) if price_book_price_val is not None else 0.0
                purchase_price = float(purchase_price_val) if purchase_price_val is not None else 0.0
                
                # category_number 처리
                category_number = None
                if category_number_val is not None:
                    try:
                        category_number = int(category_number_val)
                    except (ValueError, TypeError):
                        pass
                
                created_at = row[6] if len(row) > 6 else None
                updated_at = row[7] if len(row) > 7 else None
                
                margin = price_book_price - purchase_price
                
                # 카테고리명 매핑 (한글명으로 표시)
                category_display_names = {
                    1: "파워소스",
                    2: "타입",
                    3: "와이어피더",
                    4: "토치",
                    5: "호스패키지",
                    6: "어스",
                    7: "트롤리",
                    8: "쿨러",
                    9: "쿨란트",
                    10: "가스호스",
                    11: "용접봉 홀더",
                    12: "기타",
                    13: "세트코드",
                    14: "스페어부품"
                }
                
                # 저장된 category_number가 있으면 우선 사용
                category_name = None
                if category_number is not None:
                    category_name = category_display_names.get(category_number, f"카테고리 {category_number}")
                elif product_code:
                    try:
                        # 방법 1: option_items 테이블에서 item_code로 조회
                        cursor.execute('''
                            SELECT oc.category_number, oc.category_name
                            FROM option_items oi
                            JOIN option_categories oc ON oi.category_id = oc.id
                            WHERE oi.item_code = ?
                            LIMIT 1
                        ''', (product_code,))
                        
                        category_row = cursor.fetchone()
                        if category_row:
                            if hasattr(category_row, 'keys') and callable(getattr(category_row, 'keys', None)):
                                category_number = category_row['category_number']
                                category_name = category_row['category_name']
                            else:
                                category_number = category_row[0] if len(category_row) > 0 else None
                                category_name = category_row[1] if len(category_row) > 1 else None
                            
                            if category_number in category_display_names:
                                category_name = category_display_names[category_number]
                        else:
                            # 방법 2: option_base_prices 테이블에서 제품코드(option_name)로 조회
                            cursor.execute('''
                                SELECT category_number
                                FROM option_base_prices
                                WHERE option_name = ?
                                LIMIT 1
                            ''', (product_code,))
                            
                            base_price_row = cursor.fetchone()
                            if base_price_row:
                                if hasattr(base_price_row, 'keys') and callable(getattr(base_price_row, 'keys', None)):
                                    category_number = base_price_row['category_number']
                                else:
                                    category_number = base_price_row[0] if len(base_price_row) > 0 else None
                                
                                if category_number is not None:
                                    category_name = category_display_names.get(category_number, f"카테고리 {category_number}")
                            
                            # 방법 3: option_base_prices 테이블에서 제품명(option_name)으로 조회
                            if not category_number and product_name:
                                cursor.execute('''
                                    SELECT category_number
                                    FROM option_base_prices
                                    WHERE option_name = ?
                                    LIMIT 1
                                ''', (product_name,))
                                
                                base_price_row2 = cursor.fetchone()
                                if base_price_row2:
                                    if hasattr(base_price_row2, 'keys') and callable(getattr(base_price_row2, 'keys', None)):
                                        category_number = base_price_row2['category_number']
                                    else:
                                        category_number = base_price_row2[0] if len(base_price_row2) > 0 else None
                                    
                                    if category_number is not None:
                                        category_name = category_display_names.get(category_number, f"카테고리 {category_number}")
                            
                            # 방법 4: option_items 테이블에서 item_name으로 조회 (제품명과 옵션명 매칭)
                            if not category_number and product_name:
                                cursor.execute('''
                                    SELECT oc.category_number, oc.category_name
                                    FROM option_items oi
                                    JOIN option_categories oc ON oi.category_id = oc.id
                                    WHERE oi.item_name = ?
                                    LIMIT 1
                                ''', (product_name,))
                                
                                name_row = cursor.fetchone()
                                if name_row:
                                    if hasattr(name_row, 'keys') and callable(getattr(name_row, 'keys', None)):
                                        category_number = name_row['category_number']
                                        category_name = name_row['category_name']
                                    else:
                                        category_number = name_row[0] if len(name_row) > 0 else None
                                        category_name = name_row[1] if len(name_row) > 1 else None
                                    
                                    if category_number in category_display_names:
                                        category_name = category_display_names[category_number]
                    except Exception as e:
                        print(f"카테고리 정보 조회 오류 (제품코드: {product_code}, 제품명: {product_name}): {e}")
                        import traceback
                        print(traceback.format_exc())
                
                print(f"제품 처리: id={row_id}, code={product_code}, name={product_name}, price_book={price_book_price}, purchase={purchase_price}, margin={margin}, category={category_number}")
                
                products.append({
                    'id': row_id,
                    'code': product_code or '',
                    'name': product_name or '',
                    'priceBookPrice': price_book_price,
                    'purchasePrice': purchase_price,
                    'margin': margin,
                    'createdAt': created_at,
                    'updatedAt': updated_at,
                    'categoryNumber': category_number,
                    'categoryName': category_name
                })
                
                total_price_book += price_book_price
                total_purchase += purchase_price
                total_margin += margin
            except Exception as e:
                import traceback
                print(f"제품 데이터 처리 오류: {e}")
                print(f"오류 상세: {traceback.format_exc()}")
                print(f"행 데이터: {row}")
                print(f"행 길이: {len(row) if row else 0}")
                continue
        
        # 통계 계산
        total_products = len(products)
        average_margin_rate = (total_margin / total_price_book * 100) if total_price_book > 0 else 0
        
        statistics = {
            'totalProducts': total_products,
            'totalPriceBook': total_price_book,
            'totalPurchase': total_purchase,
            'totalMargin': total_margin,
            'averageMarginRate': average_margin_rate
        }

        hide_cost = not _session_can_view_export_cost()
        products, statistics = _filter_export_dashboard_for_role(products, statistics, hide_cost)
        
        return jsonify({
            "success": True,
            "products": products,
            "statistics": statistics,
            "count": total_products,
            "canViewExportCost": not hide_cost,
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_export_price_dashboard_new: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/update', methods=['POST'])
def api_update_export_price():
    """수출가격 대시보드 - 제품 가격 및 원가 업데이트 API"""
    try:
        # JSON 데이터 가져오기
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({
                "success": False,
                "error": "JSON 형식의 데이터가 필요합니다."
            }), 400
        
        product_id = data.get('id')
        product_name = data.get('productName')
        price_book_price = data.get('priceBookPrice')
        product_cost = data.get('productCost')
        
        if not product_id:
            return jsonify({
                "success": False,
                "error": "제품 ID가 필요합니다."
            }), 400
        
        # 제품명 검증
        if product_name is not None:
            product_name = product_name.strip()
            if not product_name:
                return jsonify({
                    "success": False,
                    "error": "제품명을 입력해주세요."
                }), 400
        
        # 가격 검증
        try:
            if price_book_price is not None:
                price_book_price = float(price_book_price)
                if price_book_price < 0:
                    return jsonify({
                        "success": False,
                        "error": "프라이스북 가격은 0 이상이어야 합니다."
                    }), 400
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "올바른 프라이스북 가격을 입력해주세요."
            }), 400
        
        try:
            if product_cost is not None:
                product_cost = float(product_cost)
                if product_cost < 0:
                    return jsonify({
                        "success": False,
                        "error": "원가는 0 이상이어야 합니다."
                    }), 400
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "올바른 원가를 입력해주세요."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 테이블이 존재하는지 확인하고 없으면 생성
        try:
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
            system.connection.commit()
        except Exception as e:
            print(f"테이블 생성 확인 오류: {e}")
        
        # 제품 존재 여부 확인
        cursor.execute('''
            SELECT id, product_code, product_name FROM export_quotation_products
            WHERE id = ?
        ''', (product_id,))
        product = cursor.fetchone()
        
        if not product:
            return jsonify({
                "success": False,
                "error": "제품을 찾을 수 없습니다."
            }), 404
        
        # 가격 업데이트
        update_fields = []
        update_values = []
        
        if product_name is not None:
            update_fields.append('product_name = ?')
            update_values.append(product_name)
        
        if price_book_price is not None:
            update_fields.append('price_book_price = ?')
            update_values.append(price_book_price)
        
        if product_cost is not None:
            update_fields.append('product_cost = ?')
            update_values.append(product_cost)
        
        if not update_fields:
            return jsonify({
                "success": False,
                "error": "업데이트할 값이 없습니다."
            }), 400
        
        update_fields.append('updated_at = CURRENT_TIMESTAMP')
        update_values.append(product_id)
        
        update_query = f'''
            UPDATE export_quotation_products
            SET {', '.join(update_fields)}
            WHERE id = ?
        '''
        
        cursor.execute(update_query, update_values)
        system.connection.commit()
        
        # 업데이트된 제품 정보 조회
        cursor.execute('''
            SELECT id, product_code, product_name, price_book_price, product_cost, created_at, updated_at
            FROM export_quotation_products
            WHERE id = ?
        ''', (product_id,))
        
        updated_row = cursor.fetchone()
        
        if hasattr(updated_row, 'keys') and callable(getattr(updated_row, 'keys', None)):
            updated_product = {
                'id': updated_row['id'],
                'code': updated_row['product_code'] or '',
                'name': updated_row['product_name'] or '',
                'priceBookPrice': float(updated_row['price_book_price']) if updated_row['price_book_price'] is not None else 0,
                'productCost': float(updated_row['product_cost']) if updated_row['product_cost'] is not None else 0,
                'createdAt': updated_row['created_at'] if 'created_at' in updated_row.keys() else None,
                'updatedAt': updated_row['updated_at'] if 'updated_at' in updated_row.keys() else None
            }
        else:
            updated_product = {
                'id': updated_row[0],
                'code': updated_row[1] or '',
                'name': updated_row[2] or '',
                'priceBookPrice': float(updated_row[3]) if updated_row[3] is not None else 0,
                'productCost': float(updated_row[4]) if updated_row[4] is not None else 0,
                'createdAt': updated_row[5] if len(updated_row) > 5 else None,
                'updatedAt': updated_row[6] if len(updated_row) > 6 else None
            }
        
        return jsonify({
            "success": True,
            "message": "가격이 성공적으로 업데이트되었습니다.",
            "product": updated_product
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_update_export_price: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/add_new', methods=['POST'])
def api_add_export_price_new():
    """수출견적서(스태프) 대시보드 - 제품 추가 API"""
    try:
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({
                "success": False,
                "error": "JSON 형식의 데이터가 필요합니다."
            }), 400
        
        product_code = data.get('productCode')
        product_name = data.get('productName')
        price_book_price = data.get('priceBookPrice')
        purchase_price = data.get('purchasePrice')
        category_number = data.get('categoryNumber')
        
        # category_number 처리: None이거나 빈 문자열이면 None으로 설정
        if category_number is not None:
            try:
                category_number = int(category_number) if category_number != '' else None
            except (ValueError, TypeError):
                category_number = None
        
        # 제품코드 앞뒤 공백 제거
        if product_code:
            product_code = str(product_code).strip()
        
        if not product_code:
            return jsonify({
                "success": False,
                "error": "제품코드가 필요합니다."
            }), 400
        
        if price_book_price is None:
            return jsonify({
                "success": False,
                "error": "프라이스북 단가가 필요합니다."
            }), 400
        
        if purchase_price is None:
            return jsonify({
                "success": False,
                "error": "구매가 원가가 필요합니다."
            }), 400
        
        try:
            price_book_price = float(price_book_price)
            purchase_price = float(purchase_price)
            if price_book_price < 0:
                return jsonify({
                    "success": False,
                    "error": "프라이스북 단가는 0 이상이어야 합니다."
                }), 400
            if purchase_price < 0:
                return jsonify({
                    "success": False,
                    "error": "구매가 원가는 0 이상이어야 합니다."
                }), 400
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "올바른 형식의 데이터를 입력해주세요."
            }), 400
        
        system = get_welding_system()

        def add_product():
            cursor = system.connection.cursor()

            cursor.execute('''
                SELECT id, product_code FROM export_quotation_new_products
                WHERE TRIM(COALESCE(product_code, '')) = ? OR product_code = ?
            ''', (product_code.strip(), product_code.strip()))
            existing = cursor.fetchone()

            if existing:
                if hasattr(existing, 'keys') and callable(getattr(existing, 'keys', None)):
                    existing_code = existing['product_code'] if 'product_code' in existing.keys() else (existing[1] if len(existing) > 1 else '')
                else:
                    existing_code = existing[1] if len(existing) > 1 else ''
                return {
                    "status": 400,
                    "body": {
                        "success": False,
                        "error": f"제품코드 '{product_code}'가 이미 존재합니다. (기존 제품코드: '{existing_code}')"
                    }
                }

            cursor.execute('''
                INSERT INTO export_quotation_new_products 
                (product_code, product_name, price_book_price, purchase_price, category_number)
                VALUES (?, ?, ?, ?, ?)
            ''', (product_code.strip(), product_name or '', price_book_price, purchase_price, category_number))

            new_product_id = cursor.lastrowid
            cursor.execute('''
                SELECT id, product_code, product_name, 
                       COALESCE(price_book_price, 0) as price_book_price,
                       COALESCE(purchase_price, 0) as purchase_price,
                       category_number,
                       created_at, updated_at
                FROM export_quotation_new_products
                WHERE id = ?
            ''', (new_product_id,))

            new_row = cursor.fetchone()

            if hasattr(new_row, 'keys') and callable(getattr(new_row, 'keys', None)):
                new_price_book = float(new_row['price_book_price']) if new_row['price_book_price'] is not None else 0
                new_purchase = float(new_row['purchase_price']) if new_row['purchase_price'] is not None else 0
                category_num = new_row['category_number'] if 'category_number' in new_row.keys() else None
                new_product = {
                    'id': new_row['id'],
                    'code': new_row['product_code'] or '',
                    'name': new_row['product_name'] or '',
                    'priceBookPrice': new_price_book,
                    'purchasePrice': new_purchase,
                    'categoryNumber': category_num,
                    'margin': new_price_book - new_purchase,
                    'createdAt': new_row['created_at'] if 'created_at' in new_row.keys() else None,
                    'updatedAt': new_row['updated_at'] if 'updated_at' in new_row.keys() else None
                }
            else:
                new_price_book = float(new_row[3]) if new_row[3] is not None else 0
                new_purchase = float(new_row[4]) if len(new_row) > 4 and new_row[4] is not None else 0
                category_num = new_row[5] if len(new_row) > 5 else None
                new_product = {
                    'id': new_row[0],
                    'code': new_row[1] or '',
                    'name': new_row[2] or '',
                    'priceBookPrice': new_price_book,
                    'purchasePrice': new_purchase,
                    'categoryNumber': category_num,
                    'margin': new_price_book - new_purchase,
                    'createdAt': new_row[6] if len(new_row) > 6 else None,
                    'updatedAt': new_row[7] if len(new_row) > 7 else None
                }

            if product_code:
                cursor.execute('''
                    SELECT oi.item_name, oc.category_number
                    FROM option_items oi
                    JOIN option_categories oc ON oi.category_id = oc.id
                    WHERE oi.item_code = ?
                ''', (product_code,))

                option_info = cursor.fetchone()
                if option_info:
                    if hasattr(option_info, 'keys') and callable(getattr(option_info, 'keys', None)):
                        option_name = option_info['item_name']
                        option_category_number = option_info['category_number']
                    else:
                        option_name = option_info[0] if len(option_info) > 0 else None
                        option_category_number = option_info[1] if len(option_info) > 1 else None

                    if option_name and option_category_number is not None:
                        cursor.execute('''
                            INSERT INTO option_base_prices (category_number, option_name, price, updated_at)
                            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(category_number, option_name) 
                            DO UPDATE SET price = ?, updated_at = CURRENT_TIMESTAMP
                        ''', (option_category_number, option_name, price_book_price, price_book_price))

            system.connection.commit()
            return {
                "status": 200,
                "body": {
                    "success": True,
                    "message": "제품이 성공적으로 추가되었습니다.",
                    "product": new_product
                }
            }

        result = run_db_with_retry(add_product)
        return jsonify(result["body"]), result["status"]
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_add_export_price_new: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/update_new', methods=['POST'])
def api_update_export_price_new():
    """수출견적서(스태프) 대시보드 - 프라이스북 단가 및 구매가 원가 업데이트 API"""
    try:
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({
                "success": False,
                "error": "JSON 형식의 데이터가 필요합니다."
            }), 400
        
        product_id = data.get('id')
        product_code = data.get('productCode')
        product_name = data.get('productName')
        price_book_price = data.get('priceBookPrice')
        purchase_price = data.get('purchasePrice')
        category_number = data.get('categoryNumber')
        
        if product_id is None:
            return jsonify({
                "success": False,
                "error": "제품 ID가 필요합니다."
            }), 400
        
        if price_book_price is None:
            return jsonify({
                "success": False,
                "error": "프라이스북 단가가 필요합니다."
            }), 400
        
        if purchase_price is None:
            return jsonify({
                "success": False,
                "error": "구매가 원가가 필요합니다."
            }), 400
        
        # 제품명 검증 및 정규화 (제품명은 필수 필드)
        if product_name is None or not product_name.strip():
            return jsonify({
                "success": False,
                "error": "제품명을 입력해주세요."
            }), 400
        
        product_name = product_name.strip()
        
        try:
            product_id = int(product_id)
            price_book_price = float(price_book_price)
            purchase_price = float(purchase_price)
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "올바른 형식의 데이터를 입력해주세요."
            }), 400
        
        system = get_welding_system()

        def update_product():
            cursor = system.connection.cursor()

            # 제품 존재 여부 확인
            cursor.execute('''
                SELECT id, product_code, product_name FROM export_quotation_new_products
                WHERE id = ?
            ''', (product_id,))
            product = cursor.fetchone()

            if not product:
                return {
                    "status": 404,
                    "body": {
                        "success": False,
                        "error": "제품을 찾을 수 없습니다."
                    }
                }

            # 제품명, 프라이스북 단가 및 구매가 원가 업데이트
            update_fields = []
            update_values = []

            # 제품명은 항상 업데이트 (검증을 통과했으므로 항상 유효한 값)
            update_fields.append('product_name = ?')
            update_values.append(product_name)
            # 분류값 업데이트 (제공된 경우)
            local_category_number = category_number
            if local_category_number is not None:
                try:
                    local_category_number = int(local_category_number)
                    update_fields.append('category_number = ?')
                    update_values.append(local_category_number)
                except (ValueError, TypeError):
                    local_category_number = None
            else:
                # 분류값이 null로 전송된 경우 (선택안함)
                update_fields.append('category_number = ?')
                update_values.append(None)

            update_fields.append('price_book_price = ?')
            update_values.append(price_book_price)

            update_fields.append('purchase_price = ?')
            update_values.append(purchase_price)

            update_fields.append('updated_at = CURRENT_TIMESTAMP')
            update_values.append(product_id)

            update_query = f'''
                UPDATE export_quotation_new_products
                SET {', '.join(update_fields)}
                WHERE id = ?
            '''
            cursor.execute(update_query, update_values)

            # 업데이트 후 제품 정보 다시 조회 (업데이트된 제품명 포함)
            cursor.execute('''
                SELECT id, product_code, product_name FROM export_quotation_new_products
                WHERE id = ?
            ''', (product_id,))
            updated_product_row = cursor.fetchone()

            # 제품 코드 가져오기
            local_product_code = product_code
            local_product_name = product_name
            if not local_product_code:
                if updated_product_row:
                    local_product_code = updated_product_row[1] if len(updated_product_row) > 1 else None
                else:
                    local_product_code = product[1] if len(product) > 1 else None

            # 제품명은 이미 업데이트되었으므로 업데이트된 값 사용
            if updated_product_row and len(updated_product_row) > 2:
                local_product_name = updated_product_row[2] or local_product_name

            # 분류 정보가 지정된 경우 option_base_prices에 저장
            if local_category_number is not None and local_product_code:
                if local_category_number != 12:
                    cursor.execute('''
                        INSERT INTO option_base_prices (category_number, option_name, price, updated_at)
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(category_number, option_name)
                        DO UPDATE SET price = ?, updated_at = CURRENT_TIMESTAMP
                    ''', (local_category_number, local_product_code, price_book_price, price_book_price))

            # 제품 코드로 option_base_prices의 관련 레코드 가격 업데이트 (12번 제외)
            if local_product_code and price_book_price is not None:
                cursor.execute('''
                    UPDATE option_base_prices
                    SET price = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE option_name = ? AND category_number != 12
                ''', (price_book_price, local_product_code))

                cursor.execute('''
                    SELECT oi.item_name, oc.category_number
                    FROM option_items oi
                    JOIN option_categories oc ON oi.category_id = oc.id
                    WHERE oi.item_code = ?
                ''', (local_product_code,))

                option_info = cursor.fetchone()
                if option_info:
                    if hasattr(option_info, 'keys') and callable(getattr(option_info, 'keys', None)):
                        option_name = option_info['item_name']
                        found_category_number = option_info['category_number']
                    else:
                        option_name = option_info[0] if len(option_info) > 0 else None
                        found_category_number = option_info[1] if len(option_info) > 1 else None

                    if option_name and found_category_number is not None and int(found_category_number) != 12:
                        cursor.execute('''
                            INSERT INTO option_base_prices (category_number, option_name, price, updated_at)
                            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(category_number, option_name)
                            DO UPDATE SET price = ?, updated_at = CURRENT_TIMESTAMP
                        ''', (found_category_number, option_name, price_book_price, price_book_price))

            system.connection.commit()

            # 업데이트된 제품 정보 조회
            cursor.execute('''
                SELECT id, product_code, product_name,
                       COALESCE(price_book_price, 0) as price_book_price,
                       COALESCE(purchase_price, 0) as purchase_price,
                       created_at, updated_at
                FROM export_quotation_new_products
                WHERE id = ?
            ''', (product_id,))

            updated_row = cursor.fetchone()

            if hasattr(updated_row, 'keys') and callable(getattr(updated_row, 'keys', None)):
                updated_price_book = float(updated_row['price_book_price']) if updated_row['price_book_price'] is not None else 0
                updated_purchase = float(updated_row['purchase_price']) if updated_row['purchase_price'] is not None else 0
                updated_product = {
                    'id': updated_row['id'],
                    'code': updated_row['product_code'] or '',
                    'name': updated_row['product_name'] or '',
                    'priceBookPrice': updated_price_book,
                    'purchasePrice': updated_purchase,
                    'margin': updated_price_book - updated_purchase,
                    'createdAt': updated_row['created_at'] if 'created_at' in updated_row.keys() else None,
                    'updatedAt': updated_row['updated_at'] if 'updated_at' in updated_row.keys() else None
                }
            else:
                updated_price_book = float(updated_row[3]) if updated_row[3] is not None else 0
                updated_purchase = float(updated_row[4]) if len(updated_row) > 4 and updated_row[4] is not None else 0
                updated_product = {
                    'id': updated_row[0],
                    'code': updated_row[1] or '',
                    'name': updated_row[2] or '',
                    'priceBookPrice': updated_price_book,
                    'purchasePrice': updated_purchase,
                    'margin': updated_price_book - updated_purchase,
                    'createdAt': updated_row[5] if len(updated_row) > 5 else None,
                    'updatedAt': updated_row[6] if len(updated_row) > 6 else None
                }

            return {
                "status": 200,
                "body": {
                    "success": True,
                    "message": "가격이 성공적으로 업데이트되었습니다.",
                    "product": updated_product
                }
            }

        result = run_db_with_retry(update_product)
        return jsonify(result["body"]), result["status"]
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_update_export_price_new: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/delete', methods=['POST'])
def api_delete_export_price():
    """수출가격 대시보드 - 제품 삭제 API"""
    try:
        # JSON 데이터 가져오기
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({
                "success": False,
                "error": "JSON 형식의 데이터가 필요합니다."
            }), 400
        
        product_id = data.get('id')
        
        if not product_id:
            return jsonify({
                "success": False,
                "error": "제품 ID가 필요합니다."
            }), 400
        
        try:
            product_id = int(product_id)
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "올바른 제품 ID를 입력해주세요."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 테이블이 존재하는지 확인하고 없으면 생성
        try:
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
            system.connection.commit()
        except Exception as e:
            print(f"테이블 생성 확인 오류: {e}")
        
        # 제품 존재 여부 확인
        cursor.execute('''
            SELECT id, product_code, product_name FROM export_quotation_products
            WHERE id = ?
        ''', (product_id,))
        product = cursor.fetchone()
        
        if not product:
            return jsonify({
                "success": False,
                "error": "제품을 찾을 수 없습니다."
            }), 404
        
        # 제품 삭제
        cursor.execute('''
            DELETE FROM export_quotation_products
            WHERE id = ?
        ''', (product_id,))
        system.connection.commit()
        
        return jsonify({
            "success": True,
            "message": "제품이 성공적으로 삭제되었습니다."
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_delete_export_price: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/delete_new', methods=['POST'])
def api_delete_export_price_new():
    """수출견적서(스태프) 대시보드 - 제품 삭제 API"""
    try:
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({
                "success": False,
                "error": "JSON 형식의 데이터가 필요합니다."
            }), 400
        
        product_id = data.get('id')
        
        if product_id is None:
            return jsonify({
                "success": False,
                "error": "제품 ID가 필요합니다."
            }), 400
        
        try:
            product_id = int(product_id)
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "올바른 제품 ID를 입력해주세요."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 제품 존재 여부 확인
        cursor.execute('''
            SELECT id, product_code, product_name FROM export_quotation_new_products
            WHERE id = ?
        ''', (product_id,))
        product = cursor.fetchone()
        
        if not product:
            return jsonify({
                "success": False,
                "error": "제품을 찾을 수 없습니다."
            }), 404
        
        # 제품 삭제
        def delete_product():
            cursor.execute('''
                DELETE FROM export_quotation_new_products
                WHERE id = ?
            ''', (product_id,))
            system.connection.commit()

        run_db_with_retry(delete_product)
        
        return jsonify({
            "success": True,
            "message": "제품이 성공적으로 삭제되었습니다."
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_delete_export_price_new: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/bulk_delete', methods=['POST'])
def api_bulk_delete_export_price():
    """수출가격 대시보드 - 제품 일괄 삭제 API"""
    try:
        # JSON 데이터 가져오기
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({
                "success": False,
                "error": "JSON 형식의 데이터가 필요합니다."
            }), 400
        
        product_ids = data.get('ids')
        
        if not product_ids:
            return jsonify({
                "success": False,
                "error": "제품 ID 목록이 필요합니다."
            }), 400
        
        if not isinstance(product_ids, list):
            return jsonify({
                "success": False,
                "error": "제품 ID는 배열 형식이어야 합니다."
            }), 400
        
        if len(product_ids) == 0:
            return jsonify({
                "success": False,
                "error": "삭제할 제품을 선택해주세요."
            }), 400
        
        # ID 유효성 검증
        try:
            product_ids = [int(id) for id in product_ids]
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "올바른 제품 ID를 입력해주세요."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 테이블이 존재하는지 확인하고 없으면 생성
        try:
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
            system.connection.commit()
        except Exception as e:
            print(f"테이블 생성 확인 오류: {e}")
        
        # 삭제할 제품 존재 여부 확인
        placeholders = ','.join(['?'] * len(product_ids))
        cursor.execute(f'''
            SELECT id, product_code, product_name FROM export_quotation_products
            WHERE id IN ({placeholders})
        ''', product_ids)
        
        existing_products = cursor.fetchall()
        existing_ids = []
        
        for product in existing_products:
            if hasattr(product, 'keys') and callable(getattr(product, 'keys', None)):
                existing_ids.append(product['id'])
            else:
                existing_ids.append(product[0])
        
        if len(existing_ids) == 0:
            return jsonify({
                "success": False,
                "error": "삭제할 제품을 찾을 수 없습니다."
            }), 404
        
        # 제품 일괄 삭제
        placeholders = ','.join(['?'] * len(existing_ids))
        cursor.execute(f'''
            DELETE FROM export_quotation_products
            WHERE id IN ({placeholders})
        ''', existing_ids)
        system.connection.commit()
        
        deleted_count = cursor.rowcount
        
        return jsonify({
            "success": True,
            "message": f"{deleted_count}개의 제품이 성공적으로 삭제되었습니다.",
            "deletedCount": deleted_count
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_bulk_delete_export_price: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/bulk_delete_new', methods=['POST'])
def api_bulk_delete_export_price_new():
    """수출견적서(스태프) 대시보드 - 제품 일괄 삭제 API"""
    try:
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({
                "success": False,
                "error": "JSON 형식의 데이터가 필요합니다."
            }), 400
        
        product_ids = data.get('ids', [])
        
        if not product_ids or not isinstance(product_ids, list):
            return jsonify({
                "success": False,
                "error": "제품 ID 목록이 필요합니다."
            }), 400
        
        if len(product_ids) == 0:
            return jsonify({
                "success": False,
                "error": "삭제할 제품을 선택해주세요."
            }), 400
        
        try:
            product_ids = [int(id) for id in product_ids]
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "올바른 제품 ID 목록을 입력해주세요."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 제품 삭제
        placeholders = ','.join(['?'] * len(product_ids))

        def bulk_delete_products():
            cursor.execute(f'''
                DELETE FROM export_quotation_new_products
                WHERE id IN ({placeholders})
            ''', product_ids)
            deleted_count = cursor.rowcount
            system.connection.commit()
            return deleted_count

        deleted_count = run_db_with_retry(bulk_delete_products)
        
        return jsonify({
            "success": True,
            "message": f"{deleted_count}개의 제품이 성공적으로 삭제되었습니다.",
            "deletedCount": deleted_count
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_bulk_delete_export_price_new: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/excel', methods=['POST'])
def api_export_price_excel():
    """수출가격 대시보드 - 엑셀 다운로드 API (필터링된 제품만)"""
    try:
        print("=== 엑셀 다운로드 API 호출됨 ===")
        import tempfile
        import os
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from datetime import datetime
        from flask import Response
        
        # POST 요청에서 필터링된 제품 데이터 받기
        data = request.get_json(silent=True)
        if data and 'products' in data:
            # 클라이언트에서 전달된 필터링된 제품 사용
            products_data = data.get('products', [])
            print(f"클라이언트에서 전달된 제품 수: {len(products_data)}")
            
            # 데이터 형식 변환
            products = []
            for item in products_data:
                products.append({
                    '번호': item.get('번호', 0),
                    'code': item.get('제품코드', ''),
                    'name': item.get('제품명', ''),
                    'priceBookPrice': float(item.get('판매가', 0)) if item.get('판매가') is not None else 0,
                    'productCost': float(item.get('원가', 0)) if item.get('원가') is not None else 0
                })
        else:
            # GET 요청이거나 데이터가 없으면 빈 배열 반환
            print("요청 데이터가 없거나 products 필드가 없습니다.")
            products = []
        
        print(f"엑셀에 포함될 제품 수: {len(products)}")
        
        if len(products) == 0:
            return jsonify({
                "success": False,
                "error": "다운로드할 제품이 없습니다."
            }), 400
        
        # 임시 파일명 생성
        temp_dir = tempfile.mkdtemp()
        temp_filename = os.path.join(temp_dir, f"수출가격대시보드_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        
        # 엑셀 파일 생성
        wb = Workbook()
        ws = wb.active
        ws.title = "수출견적서(DAP) 대시보드"
        
        # 헤더 스타일 설정
        header_font = Font(bold=True, color="FFFFFF", size=12)
        header_fill = PatternFill(start_color="800020", end_color="800020", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center")
        
        # 헤더 작성 (제품코드, 제품명, 판매가, 원가만)
        headers = ['번호', '제품코드', '제품명', '판매가 (EUR)', '원가 (EUR)']
        for col_idx, header in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx)
            cell.value = header
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
        
        # 데이터 작성
        for idx, product in enumerate(products, start=2):
            ws.cell(row=idx, column=1, value=product.get('번호', idx - 1))  # 번호
            ws.cell(row=idx, column=2, value=product.get('code', ''))  # 제품코드
            ws.cell(row=idx, column=3, value=product.get('name', ''))  # 제품명
            ws.cell(row=idx, column=4, value=product.get('priceBookPrice', 0))  # 판매가
            ws.cell(row=idx, column=5, value=product.get('productCost', 0))  # 원가
        
        # 열 너비 자동 조정
        column_widths = {
            'A': 10,  # 번호
            'B': 20,  # 제품코드
            'C': 40,  # 제품명
            'D': 18,  # 판매가
            'E': 18   # 원가
        }
        
        for col_letter, width in column_widths.items():
            ws.column_dimensions[col_letter].width = width
        
        # 숫자 형식 적용
        for row in range(2, len(products) + 2):
            ws.cell(row=row, column=4).number_format = '#,##0.00'  # 판매가
            ws.cell(row=row, column=5).number_format = '#,##0.00'  # 원가
        
        # 파일 저장
        print(f"엑셀 파일 저장 중: {temp_filename}")
        wb.save(temp_filename)
        print("엑셀 파일 저장 완료")
        
        # 파일을 메모리로 읽어서 응답
        with open(temp_filename, 'rb') as f:
            file_data = f.read()
        
        print(f"파일 크기: {len(file_data)} bytes")
        
        # 임시 파일 삭제
        try:
            os.remove(temp_filename)
            os.rmdir(temp_dir)
            print("임시 파일 삭제 완료")
        except Exception as e:
            print(f"임시 파일 삭제 오류 (무시 가능): {e}")
        
        # 응답 생성
        date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"export_price_{date_str}.xlsx"
        print(f"응답 생성: {filename}, 파일 크기: {len(file_data)} bytes")
        
        # 간단한 파일명 사용 (한글 제거)
        safe_filename = f"export_price_{date_str}.xlsx"
        
        response = Response(
            file_data,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={
                'Content-Disposition': f'attachment; filename="{safe_filename}"',
                'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'Content-Length': str(len(file_data)),
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0'
            }
        )
        
        print("엑셀 다운로드 API 완료")
        return response
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_export_price_excel: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_quotation/new/download_excel', methods=['POST'])
def api_download_export_quotation_new_excel():
    """수출견적서(스태프) 엑셀 다운로드 API"""
    try:
        print("=" * 50)
        print("=== 수출견적서(스태프) 엑셀 다운로드 API 호출됨 ===")
        print(f"요청 메서드: {request.method}")
        print(f"요청 URL: {request.url}")
        print(f"요청 헤더: {dict(request.headers)}")
        print("=" * 50)
        
        # 요청 본문 확인
        print(f"요청 Content-Type: {request.content_type}")
        print(f"요청 Content-Length: {request.content_length}")
        import sys
        sys.stdout.flush()
        
        data = request.get_json(silent=True)
        if not data:
            print("요청 데이터가 없습니다.")
            print(f"요청 본문 (raw): {request.get_data(as_text=True)[:500]}")
            return jsonify({"error": "요청 데이터가 없습니다."}), 400
        
        products = data.get('products', [])
        settings = data.get('settings', {})
        totals = data.get('totals', {})
        client_public = data.get('isPublicUser')
        if client_public is True:
            isPublicUser = True
        else:
            isPublicUser = not _session_can_view_export_cost()
        lang = data.get('lang', 'KOR').upper()  # 언어 정보 (기본값 KOR)
        
        print(f"제품 수: {len(products)}")
        print(f"설정: {settings}")
        print(f"합계: {totals}")
        print(f"일반유저 여부(서버 역할): {isPublicUser}, user_type={_session_user_type()}")
        print(f"언어: {lang}")
        import sys
        sys.stdout.flush()
        
        if not products:
            print("제품 데이터가 없습니다.")
            return jsonify({"error": "제품 데이터가 없습니다."}), 400
        
        print("엑셀 파일 생성 시작...")
        sys.stdout.flush()
        
        # 임시 파일 생성
        import tempfile
        import os
        temp_dir = tempfile.mkdtemp()
        temp_filename = os.path.join(temp_dir, "export_quotation_new.xlsx")
        
        # 엑셀 파일 생성
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from datetime import datetime
        
        # 언어별 텍스트 정의
        is_eng = (lang == 'ENG')
        
        if is_eng:
            title_public = "Export Quotation (Customer)"
            title_admin = "Export Quotation (Staff)"
            sheet_title_public = "Export Quotation (Customer)"
            sheet_title_admin = "Export Quotation (Staff)"
            discount_rate_label = "Discount Rate"
            shipping_sales_label = "Shipping (Sales)"
            shipping_purchase_label = "Shipping (Purchase)"
            incidental_expense_label = "Incidental Expense"
            krw_exchange_rate_label = "KRW Exchange Rate"
            product_info_label = "Product Information"
            sales_price_label = "Sales Price"
            cost_label = "Cost"
            margin_label = "Margin"
            total_sum_label = "Total Sum"
            headers_public = [
                'SAP Code', 'Product Name', 'Quantity',
                'Price Book Unit Price', 'Price Book Total', 'Discounted Unit Price', 'Discounted Total',
                'Shipping (Sales) Unit Price', 'Shipping (Sales) Total', 'Final Sales Unit Price', 'Final Sales Total'
            ]
            headers_admin = [
                'SAP Code', 'Product Name', 'Quantity',
                'Price Book Unit Price', 'Price Book Total', 'Discounted Unit Price', 'Discounted Total',
                'Shipping (Sales) Unit Price', 'Shipping (Sales) Total', 'Final Sales Unit Price', 'Final Sales Total',
                'Purchase Price', 'Purchase Total', 'Shipping (Purchase) Unit Price', 'Shipping (Purchase) Total',
                'Incidental Expense Unit Price', 'Incidental Expense Total', 'Final Cost Unit Price', 'Final Cost Total', 'Margin Rate'
            ]
            filename_prefix = "Export_Quotation_Staff"
        else:
            title_public = "수출견적서(고객)"
            title_admin = "수출견적서(스태프)"
            sheet_title_public = "수출견적서(고객)"
            sheet_title_admin = "수출견적서(스태프)"
            discount_rate_label = "할인율"
            shipping_sales_label = "운임(판매)"
            shipping_purchase_label = "운임(구매)"
            incidental_expense_label = "부대비용"
            krw_exchange_rate_label = "KRW 환율"
            product_info_label = "제품정보"
            sales_price_label = "판매가"
            cost_label = "원가"
            margin_label = "마진"
            total_sum_label = "총 합계"
            headers_public = [
                'SAP 코드', '코드명', '수량',
                '프라이스북 단가', '프라이스북 합계', '할인가 단가', '할인가 합계',
                '운임(판매) 단가', '운임(판매) 합계', '최종 판매가 단가', '최종 판매가 합계'
            ]
            headers_admin = [
                'SAP 코드', '코드명', '수량',
                '프라이스북 단가', '프라이스북 합계', '할인가 단가', '할인가 합계',
                '운임(판매) 단가', '운임(판매) 합계', '최종 판매가 단가', '최종 판매가 합계',
                '구매가 원가', '구매가 합계', '운임(구매) 단가', '운임(구매) 합계',
                '부대비용 단가', '부대비용 합계', '최종 원가 단가', '최종 원가 합계', '마진율'
            ]
            filename_prefix = "수출견적서(스태프)"
        
        wb = Workbook()
        ws = wb.active
        ws.title = sheet_title_public if isPublicUser else sheet_title_admin
        
        # 스타일 정의
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(start_color="800020", end_color="800020", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        # 제목 행
        if isPublicUser:
            ws.merge_cells('A1:K1')
            ws['A1'] = title_public
        else:
            ws.merge_cells('A1:U1')
            ws['A1'] = title_admin
        ws['A1'].font = Font(bold=True, size=16)
        ws['A1'].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 30
        
        # 설정 정보 행 (2행)
        row = 2
        ws[f'A{row}'] = discount_rate_label
        ws[f'B{row}'] = f"{settings.get('discountRate', 0)}%"
        ws[f'C{row}'] = shipping_sales_label
        ws[f'D{row}'] = settings.get('shippingSales', 0)
        if not isPublicUser:
            ws[f'E{row}'] = shipping_purchase_label
            ws[f'F{row}'] = settings.get('shippingPurchase', 0)
            ws[f'G{row}'] = incidental_expense_label
            ws[f'H{row}'] = settings.get('incidentalExpensePurchase', 0)
            ws[f'I{row}'] = krw_exchange_rate_label
            ws[f'J{row}'] = settings.get('krwExchangeRate', 1700)
        
        row = 3  # 빈 행
        
        # 섹션 헤더 행 (제품정보, 판매가)
        row = 4
        if isPublicUser:
            # 제품정보 섹션 (A-C)
            ws.merge_cells(f'A{row}:C{row}')
            cell = ws.cell(row=row, column=1)
            cell.value = product_info_label
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = border
            
            # 판매가 섹션 (D-K)
            ws.merge_cells(f'D{row}:K{row}')
            cell = ws.cell(row=row, column=4)
            cell.value = sales_price_label
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = border
        else:
            # 관리자용: 제품정보, 판매가, 원가, 마진
            ws.merge_cells(f'A{row}:C{row}')
            cell = ws.cell(row=row, column=1)
            cell.value = product_info_label
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = border
            
            ws.merge_cells(f'D{row}:K{row}')
            cell = ws.cell(row=row, column=4)
            cell.value = sales_price_label
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = border
            
            ws.merge_cells(f'L{row}:S{row}')
            cell = ws.cell(row=row, column=12)
            cell.value = cost_label
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = border
            
            ws.cell(row=row, column=20).value = margin_label
            ws.cell(row=row, column=20).font = header_font
            ws.cell(row=row, column=20).fill = header_fill
            ws.cell(row=row, column=20).alignment = header_alignment
            ws.cell(row=row, column=20).border = border
        
        ws.row_dimensions[row].height = 30
        
        # 상세 헤더 행
        row = 5
        if isPublicUser:
            headers = headers_public
        else:
            headers = headers_admin
        
        for col_idx, header in enumerate(headers, start=1):
            cell = ws.cell(row=row, column=col_idx)
            cell.value = header
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = border
        
        ws.row_dimensions[row].height = 40
        
        # 제품 데이터 행
        row = 6
        print(f"제품 데이터 작성 시작, 총 {len(products)}개")
        try:
            for idx, product in enumerate(products):
                if idx % 100 == 0:
                    print(f"제품 데이터 작성 중: {idx}/{len(products)}")
                
                # 안전하게 값 가져오기
                try:
                    ws.cell(row=row, column=1).value = str(product.get('code', ''))[:100]  # 제품코드
                    ws.cell(row=row, column=2).value = str(product.get('name', ''))[:200]  # 제품명
                    ws.cell(row=row, column=3).value = float(product.get('quantity', 0) or 0)  # 수량
                    ws.cell(row=row, column=4).value = float(product.get('priceBookPrice', 0) or 0)  # 프라이스북 단가
                    ws.cell(row=row, column=5).value = float(product.get('priceBookTotal', 0) or 0)  # 프라이스북 합계
                    ws.cell(row=row, column=6).value = float(product.get('discountedPrice', 0) or 0)  # 할인가 단가
                    ws.cell(row=row, column=7).value = float(product.get('discountedTotal', 0) or 0)  # 할인가 합계
                    ws.cell(row=row, column=8).value = float(product.get('shippingSalesPerProduct', 0) or 0)  # 운임(판매) 단가
                    ws.cell(row=row, column=9).value = float(product.get('shippingSalesTotal', 0) or 0)  # 운임(판매) 합계
                    ws.cell(row=row, column=10).value = float(product.get('finalSalesPrice', 0) or 0)  # 최종 판매가 단가
                    ws.cell(row=row, column=11).value = float(product.get('finalSalesPriceTotal', 0) or 0)  # 최종 판매가 합계
                    
                    if not isPublicUser:
                        # 관리자용: 원가 관련 데이터 포함
                        ws.cell(row=row, column=12).value = float(product.get('purchasePrice', 0) or 0)  # 구매가 원가
                        ws.cell(row=row, column=13).value = float(product.get('purchaseTotal', 0) or 0)  # 구매가 합계
                        ws.cell(row=row, column=14).value = float(product.get('shippingPurchasePerProduct', 0) or 0)  # 운임(구매) 단가
                        ws.cell(row=row, column=15).value = float(product.get('shippingPurchaseTotal', 0) or 0)  # 운임(구매) 합계
                        ws.cell(row=row, column=16).value = float(product.get('incidentalPurchasePerProduct', 0) or 0)  # 부대비용 단가
                        ws.cell(row=row, column=17).value = float(product.get('incidentalTotal', 0) or 0)  # 부대비용 합계
                        ws.cell(row=row, column=18).value = float(product.get('finalCost', 0) or 0)  # 최종 원가 단가
                        ws.cell(row=row, column=19).value = float(product.get('finalCostTotal', 0) or 0)  # 최종 원가 합계
                    margin_rate = product.get('marginRate', 0)
                    if isinstance(margin_rate, (int, float)):
                        ws.cell(row=row, column=20).value = f"{float(margin_rate):.1f}%"
                    else:
                        ws.cell(row=row, column=20).value = str(margin_rate)[:20]
                except Exception as cell_error:
                    print(f"제품 {idx} 데이터 작성 오류: {cell_error}")
                    # 기본값으로 계속 진행
                    ws.cell(row=row, column=1).value = ''
                    ws.cell(row=row, column=2).value = ''
                    max_col = 11 if isPublicUser else 20
                    for col in range(3, max_col):
                        ws.cell(row=row, column=col).value = 0
                    if not isPublicUser:
                        ws.cell(row=row, column=20).value = '0.0%'
                
                # 숫자 형식 적용
                try:
                    if isPublicUser:
                        num_cols = [3, 4, 5, 6, 7, 8, 9, 10, 11]
                    else:
                        num_cols = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
                    
                    for col in num_cols:
                        cell = ws.cell(row=row, column=col)
                        if cell.value is not None:
                            cell.number_format = '#,##0.00'
                            cell.alignment = Alignment(horizontal="right", vertical="center")
                        cell.border = border
                    
                    # 텍스트 셀 정렬
                    ws.cell(row=row, column=1).alignment = Alignment(horizontal="left", vertical="center")
                    ws.cell(row=row, column=2).alignment = Alignment(horizontal="left", vertical="center")
                    if not isPublicUser:
                        ws.cell(row=row, column=20).alignment = Alignment(horizontal="right", vertical="center")
                except Exception as format_error:
                    print(f"제품 {idx} 형식 적용 오류: {format_error}")
                
                row += 1
        except Exception as data_error:
            print(f"제품 데이터 작성 중 오류: {data_error}")
            import traceback
            print(traceback.format_exc())
            raise
        
        print(f"제품 데이터 작성 완료, 총 {row - 6}개 행")
        
        # 합계 행
        ws.cell(row=row, column=1).value = total_sum_label
        ws.cell(row=row, column=2).value = ''  # 코드명 빈칸
        ws.cell(row=row, column=3).value = totals.get('totalQuantity', 0)
        ws.cell(row=row, column=4).value = ''  # 프라이스북 단가 빈칸
        ws.cell(row=row, column=5).value = totals.get('totalPriceBook', 0)
        ws.cell(row=row, column=6).value = ''  # 할인가 단가 빈칸
        ws.cell(row=row, column=7).value = totals.get('totalDiscounted', 0)
        ws.cell(row=row, column=8).value = ''  # 운임(판매) 단가 빈칸
        ws.cell(row=row, column=9).value = totals.get('totalShippingSales', 0)
        ws.cell(row=row, column=10).value = ''  # 최종 판매가 단가 빈칸
        ws.cell(row=row, column=11).value = totals.get('totalFinalSales', 0)
        
        if not isPublicUser:
            ws.cell(row=row, column=12).value = ''  # 구매가 원가 빈칸
            ws.cell(row=row, column=13).value = totals.get('totalPurchase', 0)
            ws.cell(row=row, column=14).value = ''  # 운임(구매) 단가 빈칸
            ws.cell(row=row, column=15).value = totals.get('totalShippingPurchase', 0)
            ws.cell(row=row, column=16).value = ''  # 부대비용 단가 빈칸
            ws.cell(row=row, column=17).value = totals.get('totalIncidental', 0)
            ws.cell(row=row, column=18).value = ''  # 최종 원가 단가 빈칸
            ws.cell(row=row, column=19).value = totals.get('totalFinalCost', 0)
            ws.cell(row=row, column=20).value = f"{totals.get('totalMarginRate', 0)}%"
        
        # 합계 행 스타일
        max_col = 11 if isPublicUser else 20
        for col in range(1, max_col + 1):
            cell = ws.cell(row=row, column=col)
            cell.font = Font(bold=True, color="FF0000")
            if isPublicUser:
                if col in [3, 4, 5, 6, 7, 8, 9, 10, 11]:
                    cell.number_format = '#,##0.00'
                    cell.alignment = Alignment(horizontal="right", vertical="center")
            else:
                if col in [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]:
                    cell.number_format = '#,##0.00'
                    cell.alignment = Alignment(horizontal="right", vertical="center")
            cell.border = border
        
        # 열 너비 자동 조정
        if isPublicUser:
            column_widths = {
                'A': 15,  # 제품코드
                'B': 30,  # 제품명
                'C': 10,  # 수량
                'D': 18,  # 프라이스북 단가
                'E': 18,  # 프라이스북 합계
                'F': 15,  # 할인가 단가
                'G': 15,  # 할인가 합계
                'H': 15,  # 운임(판매) 단가
                'I': 15,  # 운임(판매) 합계
                'J': 18,  # 최종 판매가 단가
                'K': 18   # 최종 판매가 합계
            }
        else:
            column_widths = {
                'A': 15,  # 제품코드
                'B': 30,  # 제품명
                'C': 10,  # 수량
                'D': 18,  # 프라이스북 단가
                'E': 18,  # 프라이스북 합계
                'F': 15,  # 할인가 단가
                'G': 15,  # 할인가 합계
                'H': 15,  # 운임(판매) 단가
                'I': 15,  # 운임(판매) 합계
                'J': 18,  # 최종 판매가 단가
                'K': 18,  # 최종 판매가 합계
                'L': 15,  # 구매가 원가
                'M': 15,  # 구매가 합계
                'N': 15,  # 운임(구매) 단가
                'O': 15,  # 운임(구매) 합계
                'P': 15,  # 부대비용 단가
                'Q': 15,  # 부대비용 합계
                'R': 18,  # 최종 원가 단가
                'S': 18,  # 최종 원가 합계
                'T': 12   # 마진율
            }
        
        for col_letter, width in column_widths.items():
            ws.column_dimensions[col_letter].width = width
        
        # 파일 저장
        print(f"엑셀 파일 저장 중: {temp_filename}")
        import sys
        sys.stdout.flush()
        
        try:
            wb.save(temp_filename)
            print("엑셀 파일 저장 완료")
            sys.stdout.flush()
        except Exception as save_error:
            print(f"엑셀 파일 저장 오류: {save_error}")
            import traceback
            print(traceback.format_exc())
            sys.stdout.flush()
            try:
                wb.close()
            except:
                pass
            raise
        
        # 파일 크기 확인
        file_size = os.path.getsize(temp_filename)
        print(f"파일 크기: {file_size} bytes")
        sys.stdout.flush()
        
        # 응답 생성
        date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        if is_eng:
            filename_utf8 = f"Export_Quotation_Staff_{date_str}.xlsx"
            filename_ascii = f"export_quotation_staff_{date_str}.xlsx"
        else:
            filename_utf8 = f"수출견적서(스태프)_{date_str}.xlsx"
        filename_ascii = f"export_quotation_new_{date_str}.xlsx"  # ASCII 파일명 (latin-1 호환)
        
        # 파일명 인코딩 (RFC 5987 형식)
        import urllib.parse
        encoded_filename = urllib.parse.quote(filename_utf8.encode('utf-8'))
        
        # 파일을 스트리밍 방식으로 전송 (메모리 효율적)
        def generate_file():
            try:
                with open(temp_filename, 'rb') as f:
                    while True:
                        chunk = f.read(8192)  # 8KB 청크로 읽기
                        if not chunk:
                            break
                        yield chunk
            finally:
                # 파일 전송 후 임시 파일 삭제
                try:
                    os.remove(temp_filename)
                    os.rmdir(temp_dir)
                    print("임시 파일 삭제 완료")
                    sys.stdout.flush()
                except Exception as e:
                    print(f"임시 파일 삭제 오류 (무시 가능): {e}")
                    sys.stdout.flush()
        
        response = Response(
            generate_file(),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={
                'Content-Disposition': f'attachment; filename="{filename_ascii}"; filename*=UTF-8\'\'{encoded_filename}',
                'Content-Length': str(file_size),
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache'
            }
        )
        
        print(f"응답 생성 완료: {filename_utf8}")
        print(f"응답 크기: {file_size} bytes")
        print(f"응답 헤더: {dict(response.headers)}")
        print("=" * 50)
        print("응답 반환 직전")
        print("=" * 50)
        
        # 응답을 반환하기 전에 flush
        import sys
        sys.stdout.flush()
        
        try:
            return response
        except Exception as return_error:
            print(f"응답 반환 중 오류: {return_error}")
            import traceback
            traceback.print_exc()
            sys.stdout.flush()
            # 오류 발생 시 임시 파일 정리
            try:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
                if os.path.exists(temp_dir):
                    os.rmdir(temp_dir)
            except:
                pass
            raise
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print("=" * 50)
        print(f"ERROR in api_download_export_quotation_new_excel: {error_trace}")
        print("=" * 50)
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/export_price/excel_new', methods=['POST'])
def api_export_price_excel_new():
    """수출견적서(스태프) 대시보드 - 엑셀 다운로드 API (필터링된 제품만)"""
    try:
        print("=== 엑셀 다운로드 API 호출됨 (신규) ===")
        import tempfile
        import os
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from datetime import datetime
        from flask import Response
        
        # POST 요청에서 필터링된 제품 데이터 받기
        data = request.get_json(silent=True)
        if data and 'products' in data:
            products_data = data.get('products', [])
            print(f"클라이언트에서 전달된 제품 수: {len(products_data)}")
            
            # 데이터 형식 변환
            products = []
            for item in products_data:
                products.append({
                    '번호': item.get('번호', 0),
                    '제품코드': item.get('제품코드', ''),
                    '제품명': item.get('제품명', ''),
                    '분류': item.get('분류', ''),
                    '프라이스북단가': float(item.get('프라이스북단가', 0)) if item.get('프라이스북단가') is not None else 0,
                    '구매가원가': float(item.get('구매가원가', 0)) if item.get('구매가원가') is not None else 0
                })
        else:
            print("요청 데이터가 없거나 products 필드가 없습니다.")
            products = []
        
        print(f"엑셀에 포함될 제품 수: {len(products)}")
        
        if len(products) == 0:
            return jsonify({
                "success": False,
                "error": "다운로드할 제품이 없습니다."
            }), 400
        
        # 임시 파일명 생성
        temp_dir = tempfile.mkdtemp()
        temp_filename = os.path.join(temp_dir, f"수출견적서신규대시보드_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        
        # 엑셀 파일 생성
        wb = Workbook()
        ws = wb.active
        ws.title = "수출견적서(스태프) 대시보드"
        
        # 헤더 스타일 설정
        header_font = Font(bold=True, color="FFFFFF", size=12)
        header_fill = PatternFill(start_color="800020", end_color="800020", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center")
        
        # 헤더 작성 (번호, 제품코드, 제품명, 분류, 프라이스북 단가, 구매가 원가)
        headers = ['번호', '제품코드', '제품명', '분류', '프라이스북 단가 (EUR)', '구매가 원가 (EUR)']
        for col_idx, header in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx)
            cell.value = header
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
        
        # 데이터 작성
        for idx, product in enumerate(products, start=2):
            ws.cell(row=idx, column=1, value=product.get('번호', idx - 1))  # 번호
            ws.cell(row=idx, column=2, value=product.get('제품코드', ''))  # 제품코드
            ws.cell(row=idx, column=3, value=product.get('제품명', ''))  # 제품명
            ws.cell(row=idx, column=4, value=product.get('분류', ''))  # 분류
            ws.cell(row=idx, column=5, value=product.get('프라이스북단가', 0))  # 프라이스북 단가
            ws.cell(row=idx, column=6, value=product.get('구매가원가', 0))  # 구매가 원가
        
        # 열 너비 자동 조정
        column_widths = {
            'A': 10,  # 번호
            'B': 20,  # 제품코드
            'C': 40,  # 제품명
            'D': 25,  # 분류
            'E': 20,  # 프라이스북 단가
            'F': 20   # 구매가 원가
        }
        
        for col_letter, width in column_widths.items():
            ws.column_dimensions[col_letter].width = width
        
        # 숫자 형식 적용
        for row in range(2, len(products) + 2):
            ws.cell(row=row, column=5).number_format = '#,##0.00'  # 프라이스북 단가
            ws.cell(row=row, column=6).number_format = '#,##0.00'  # 구매가 원가
        
        # 파일 저장
        print(f"엑셀 파일 저장 중: {temp_filename}")
        wb.save(temp_filename)
        print("엑셀 파일 저장 완료")
        
        # 파일을 메모리로 읽어서 응답
        with open(temp_filename, 'rb') as f:
            file_data = f.read()
        
        print(f"파일 크기: {len(file_data)} bytes")
        
        # 임시 파일 삭제
        try:
            os.remove(temp_filename)
            os.rmdir(temp_dir)
            print("임시 파일 삭제 완료")
        except Exception as e:
            print(f"임시 파일 삭제 오류 (무시 가능): {e}")
        
        # 응답 생성
        date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_filename = f"export_price_new_{date_str}.xlsx"
        
        response = Response(
            file_data,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={
                'Content-Disposition': f'attachment; filename="{safe_filename}"',
                'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'Content-Length': str(len(file_data)),
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0'
            }
        )
        
        print("엑셀 다운로드 API 완료 (신규)")
        return response
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_export_price_excel_new: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/set_codes', methods=['GET'])
def api_get_set_codes():
    """세트코드 목록 조회 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        cursor.execute('''
            SELECT DISTINCT set_code, COUNT(*) as count
            FROM selected_combinations
            WHERE set_code IS NOT NULL AND set_code != ''
            GROUP BY set_code
            ORDER BY set_code
        ''')
        
        set_codes = []
        for row in cursor.fetchall():
            set_codes.append({
                'set_code': row['set_code'],
                'count': row['count']
            })
        
        return jsonify({
            "success": True,
            "set_codes": set_codes
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/set_price/list', methods=['GET'])
def api_get_set_price_list():
    """세트코드별 가격 목록 조회 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        ref = get_set_price_reference_data(system, cursor)
        return jsonify({
            "success": True,
            "price_list": build_price_list(ref),
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/api/set_price/summary', methods=['GET'])
def api_get_set_price_summary():
    """세트코드별 원/할인/구매가 합계 일괄 조회 API"""
    try:
        discount_rate = float(request.args.get('discount_rate', 0) or 0)
        system = get_welding_system()
        cursor = system.connection.cursor()
        ref = get_set_price_reference_data(system, cursor)
        registered_products = [
            {
                'code': code,
                'name': (info.get('product_name') or ''),
                'purchasePrice': info.get('purchase_price'),
            }
            for code, info in ref.get('products_by_code', {}).items()
        ]
        return jsonify({
            "success": True,
            "summaries": build_all_summaries(ref, discount_rate),
            "registered_product_codes": sorted(ref.get('registered_product_codes') or []),
            "registered_products": registered_products,
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/set_price/update', methods=['POST'])
def api_update_set_price():
    """세트코드 가격 입력/수정 API"""
    try:
        data = request.get_json()
        set_code = data.get('set_code')
        price = data.get('price')
        
        if not set_code:
            return jsonify({
                "success": False,
                "error": "세트코드를 입력해주세요."
            }), 400
        
        if price is None or price == '':
            return jsonify({
                "success": False,
                "error": "가격을 입력해주세요."
            }), 400
        
        try:
            price = float(price)
            if price < 0:
                return jsonify({
                    "success": False,
                    "error": "가격은 0 이상이어야 합니다."
                }), 400
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "error": "올바른 숫자를 입력해주세요."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 해당 세트코드의 모든 레코드에 가격 업데이트
        cursor.execute('''
            UPDATE selected_combinations
            SET price = ?
            WHERE set_code = ?
        ''', (price, set_code))
        
        system.connection.commit()
        invalidate_set_price_cache()
        
        updated_count = cursor.rowcount
        
        return jsonify({
            "success": True,
            "message": f"세트코드 {set_code}의 가격이 {price:,.0f} EUR로 업데이트되었습니다. ({updated_count}개 레코드)",
            "set_code": set_code,
            "price": price,
            "updated_count": updated_count
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/set_price/options', methods=['GET'])
def api_get_set_code_options():
    """세트코드의 옵션 목록 조회 API"""
    try:
        set_code = request.args.get('set_code')
        if not set_code:
            return jsonify({
                "success": False,
                "error": "세트코드를 입력해주세요."
            }), 400

        system = get_welding_system()
        if not system:
            return jsonify({
                "success": False,
                "error": "시스템 초기화 오류가 발생했습니다."
            }), 500

        cursor = system.connection.cursor()
        ref = get_set_price_reference_data(system, cursor)
        options_list = build_set_code_options(set_code, ref)
        if options_list is None:
            return jsonify({
                "success": False,
                "error": "해당 세트코드를 찾을 수 없습니다."
            }), 404

        registered_products = [
            {
                'code': code,
                'name': (info.get('product_name') or ''),
                'purchasePrice': info.get('purchase_price'),
            }
            for code, info in ref.get('products_by_code', {}).items()
        ]

        return jsonify({
            "success": True,
            "set_code": set_code,
            "options": options_list,
            "registered_product_codes": sorted(ref.get('registered_product_codes') or []),
            "registered_products": registered_products,
        })
    except Exception as e:
        import traceback
        print(f"Error in api_get_set_code_options: {traceback.format_exc()}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/set_price/options/excel', methods=['GET'])
def api_download_option_prices_excel():
    """세트코드별 옵션 가격 엑셀 다운로드 API"""
    try:
        set_code = request.args.get('set_code')
        discount_rate = float(request.args.get('discount_rate', 0))
        
        if not set_code:
            return jsonify({
                "success": False,
                "error": "세트코드를 입력해주세요."
            }), 400
        
        system = get_welding_system()
        if not system:
            return jsonify({
                "success": False,
                "error": "시스템 초기화 오류가 발생했습니다."
            }), 500
        
        cursor = system.connection.cursor()
        
        # 세트코드의 옵션 조합 조회
        cursor.execute('''
            SELECT selected_options
            FROM selected_combinations
            WHERE set_code = ?
            LIMIT 1
        ''', (set_code,))
        
        row = cursor.fetchone()
        if not row:
            return jsonify({
                "success": False,
                "error": "해당 세트코드를 찾을 수 없습니다."
            }), 404
        
        import json
        try:
            if hasattr(row, 'keys') and callable(getattr(row, 'keys', None)):
                selected_options_str = row['selected_options'] if 'selected_options' in row.keys() else (row[0] if len(row) > 0 else None)
            else:
                selected_options_str = row[0] if len(row) > 0 else None
            
            selected_options = json.loads(selected_options_str) if selected_options_str else {}
        except json.JSONDecodeError as e:
            return jsonify({
                "success": False,
                "error": f"옵션 데이터 파싱 오류: {str(e)}"
            }), 500
        
        # 카테고리 정보 가져오기
        try:
            categories = system.get_all_categories()
            category_map = {cat['category_number']: cat['category_name'] for cat in categories}
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"카테고리 정보 조회 오류: {str(e)}"
            }), 500
        
        # 옵션별 가격 정보 가져오기
        price_map = {}
        try:
            cursor.execute('''
                SELECT category_number, option_name, price
                FROM option_prices
                WHERE set_code = ?
            ''', (set_code,))
            
            for price_row in cursor.fetchall():
                try:
                    if hasattr(price_row, 'keys') and callable(getattr(price_row, 'keys', None)):
                        category_num = price_row['category_number'] if 'category_number' in price_row.keys() else (price_row[0] if len(price_row) > 0 else None)
                        option_name = price_row['option_name'] if 'option_name' in price_row.keys() else (price_row[1] if len(price_row) > 1 else None)
                        price = price_row['price'] if 'price' in price_row.keys() else (price_row[2] if len(price_row) > 2 else None)
                    else:
                        category_num = price_row[0] if len(price_row) > 0 else None
                        option_name = price_row[1] if len(price_row) > 1 else None
                        price = price_row[2] if len(price_row) > 2 else None
                    
                    if category_num is not None and option_name is not None:
                        key = f"{category_num}_{option_name}"
                        price_map[key] = price
                except (KeyError, IndexError, TypeError, AttributeError) as e:
                    continue
        except Exception as e:
            price_map = {}
        
        # 기본 가격 정보 가져오기
        base_price_map = {}
        try:
            cursor.execute('''
                SELECT category_number, option_name, price
                FROM option_base_prices
            ''')
            
            for base_price_row in cursor.fetchall():
                try:
                    if hasattr(base_price_row, 'keys') and callable(getattr(base_price_row, 'keys', None)):
                        category_num = base_price_row['category_number'] if 'category_number' in base_price_row.keys() else (base_price_row[0] if len(base_price_row) > 0 else None)
                        option_name = base_price_row['option_name'] if 'option_name' in base_price_row.keys() else (base_price_row[1] if len(base_price_row) > 1 else None)
                        price = base_price_row['price'] if 'price' in base_price_row.keys() else (base_price_row[2] if len(base_price_row) > 2 else None)
                    else:
                        category_num = base_price_row[0] if len(base_price_row) > 0 else None
                        option_name = base_price_row[1] if len(base_price_row) > 1 else None 
                        price = base_price_row[2] if len(base_price_row) > 2 else None
                    
                    if category_num is not None and option_name is not None:
                        key = f"{category_num}_{option_name}"
                        base_price_map[key] = price
                except (KeyError, IndexError, TypeError, AttributeError) as e:
                    continue
        except Exception as e:
            base_price_map = {}
        
        # 엑셀 파일 생성
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from io import BytesIO
        
        wb = Workbook()
        ws = wb.active
        ws.title = "옵션별 가격"
        
        # 헤더 스타일 설정
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center")
        
        # 헤더 작성
        ws['A1'] = '카테고리'
        ws['B1'] = '옵션명'
        ws['C1'] = '원 가격 (EUR)'
        ws['D1'] = '할인 가격 (EUR)'
        
        # 헤더 스타일 적용
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
        
        # 옵션 목록 구성 및 엑셀 작성
        options_list = []
        for category_num, option_name in selected_options.items():
            try:
                cat_num = int(category_num)
                key = f"{cat_num}_{option_name}"
                
                set_price = price_map.get(key)
                base_price = base_price_map.get(key)
                
                # 기본 가격을 우선 사용
                final_price = base_price if base_price is not None else set_price
                
                options_list.append({
                    'category_number': cat_num,
                    'category_name': category_map.get(cat_num, f'카테고리 {cat_num}'),
                    'option_name': option_name,
                    'price': final_price
                })
            except (ValueError, TypeError) as e:
                continue
        
        # 카테고리 번호 순서로 정렬
        options_list.sort(key=lambda x: x['category_number'])
        
        # 엑셀에 데이터 작성
        row = 2
        total_original = 0.0
        total_discounted = 0.0
        
        for option in options_list:
            original_price = option['price'] if option['price'] is not None else 0.0
            discounted_price = original_price * (1 - discount_rate / 100) if original_price > 0 else 0.0
            
            total_original += original_price
            total_discounted += discounted_price
            
            ws[f'A{row}'] = f"[{option['category_number']}] {option['category_name']}"
            ws[f'B{row}'] = option['option_name']
            ws[f'C{row}'] = original_price if original_price > 0 else None
            ws[f'D{row}'] = discounted_price if discounted_price > 0 else None
            
            # 숫자 형식 적용
            if original_price > 0:
                ws[f'C{row}'].number_format = '#,##0.00'
            if discounted_price > 0:
                ws[f'D{row}'].number_format = '#,##0.00'
            
            row += 1
        
        # 합계 행 추가
        ws[f'A{row}'] = '합계'
        ws[f'B{row}'] = ''
        ws[f'C{row}'] = total_original
        ws[f'D{row}'] = total_discounted
        
        # 합계 행 스타일 적용
        total_font = Font(bold=True)
        total_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
        for cell in ws[row]:
            cell.font = total_font
            cell.fill = total_fill
        
        ws[f'C{row}'].number_format = '#,##0.00'
        ws[f'D{row}'].number_format = '#,##0.00'
        
        # 컬럼 너비 자동 조정
        ws.column_dimensions['A'].width = 25
        ws.column_dimensions['B'].width = 30
        ws.column_dimensions['C'].width = 18
        ws.column_dimensions['D'].width = 18
        
        # 엑셀 파일을 메모리에 저장
        excel_buffer = BytesIO()
        wb.save(excel_buffer)
        excel_buffer.seek(0)
        
        from flask import send_file
        return send_file(
            excel_buffer,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'옵션별가격_{set_code}.xlsx'
        )
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_download_option_prices_excel: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/set_price/quote', methods=['GET'])
def api_download_quote():
    """세트코드별 견적서 엑셀 다운로드 API"""
    print(f"견적서 API 호출됨: set_code={request.args.get('set_code')}")
    try:
        set_code = request.args.get('set_code')
        discount_rate = float(request.args.get('discount_rate', 0))
        currency = request.args.get('currency', 'EUR')
        exchange_rate = float(request.args.get('exchange_rate', 1.0))
        lang = request.args.get('lang', 'KOR').upper()  # 언어 파라미터 추가
        expiration_date = request.args.get('expiration_date', '').strip()  # 만료일 파라미터 추가
        
        if not set_code:
            return jsonify({
                "success": False,
                "error": "세트코드를 입력해주세요."
            }), 400
        
        system = get_welding_system()
        if not system:
            return jsonify({
                "success": False,
                "error": "시스템 초기화 오류가 발생했습니다."
            }), 500
        
        cursor = system.connection.cursor()
        
        # 세트코드의 옵션 조합 조회
        cursor.execute('''
            SELECT selected_options, price
            FROM selected_combinations
            WHERE set_code = ?
            LIMIT 1
        ''', (set_code,))
        
        row = cursor.fetchone()
        if not row:
            return jsonify({
                "success": False,
                "error": "해당 세트코드를 찾을 수 없습니다."
            }), 404
        
        import json
        try:
            if hasattr(row, 'keys') and callable(getattr(row, 'keys', None)):
                selected_options_str = row['selected_options'] if 'selected_options' in row.keys() else (row[0] if len(row) > 0 else None)
                total_price = row['price'] if 'price' in row.keys() else (row[1] if len(row) > 1 else None)
            else:
                selected_options_str = row[0] if len(row) > 0 else None
                total_price = row[1] if len(row) > 1 else None
            
            print(f"견적서 생성 - selected_options_str: {selected_options_str}")
            selected_options = json.loads(selected_options_str) if selected_options_str else {}
            print(f"견적서 생성 - 파싱된 selected_options: {selected_options}, 개수: {len(selected_options)}")
        except json.JSONDecodeError as e:
            print(f"견적서 생성 - JSON 파싱 오류: {e}, 원본 데이터: {selected_options_str}")
            return jsonify({
                "success": False,
                "error": "옵션 데이터 파싱 오류"
            }), 500
        
        # 카테고리 정보 가져오기
        try:
            categories = system.get_all_categories()
            category_map = {cat['category_number']: cat['category_name'] for cat in categories}
            
            # 영어 버전인 경우 카테고리 이름을 영어로 변환
            if lang == 'ENG':
                category_name_map_eng = {
                    1: "Power Source",
                    2: "Type",
                    3: "Wire Feeder",
                    4: "Torch",
                    5: "Hose Package",
                    6: "Ground",
                    7: "Trolley",
                    8: "Cooler",
                    9: "Coolant",
                    10: "Gas Hose",
                    11: "Electrode Holder"
                }
                # 카테고리 맵을 영어 이름으로 업데이트
                for cat_num, eng_name in category_name_map_eng.items():
                    if cat_num in category_map:
                        category_map[cat_num] = eng_name
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"카테고리 정보 조회 오류: {str(e)}"
            }), 500
        
        # 옵션별 가격 정보 가져오기
        price_map = {}
        try:
            cursor.execute('''
                SELECT category_number, option_name, price
                FROM option_prices
                WHERE set_code = ?
            ''', (set_code,))
            
            for price_row in cursor.fetchall():
                try:
                    if hasattr(price_row, 'keys') and callable(getattr(price_row, 'keys', None)):
                        category_num = price_row['category_number'] if 'category_number' in price_row.keys() else (price_row[0] if len(price_row) > 0 else None)
                        option_name = price_row['option_name'] if 'option_name' in price_row.keys() else (price_row[1] if len(price_row) > 1 else None)
                        price = price_row['price'] if 'price' in price_row.keys() else (price_row[2] if len(price_row) > 2 else None)
                    else:
                        category_num = price_row[0] if len(price_row) > 0 else None
                        option_name = price_row[1] if len(price_row) > 1 else None
                        price = price_row[2] if len(price_row) > 2 else None
                    
                    if category_num is not None and option_name is not None:
                        key = f"{category_num}_{option_name}"
                        price_map[key] = price
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue
        except Exception:
            price_map = {}
        
        # 기본 가격 정보 가져오기
        base_price_map = {}
        try:
            cursor.execute('''
                SELECT category_number, option_name, price
                FROM option_base_prices
            ''')
            
            for base_price_row in cursor.fetchall():
                try:
                    if hasattr(base_price_row, 'keys') and callable(getattr(base_price_row, 'keys', None)):
                        category_num = base_price_row['category_number'] if 'category_number' in base_price_row.keys() else (base_price_row[0] if len(base_price_row) > 0 else None)
                        option_name = base_price_row['option_name'] if 'option_name' in base_price_row.keys() else (base_price_row[1] if len(base_price_row) > 1 else None)
                        price = base_price_row['price'] if 'price' in base_price_row.keys() else (base_price_row[2] if len(base_price_row) > 2 else None)
                    else:
                        category_num = base_price_row[0] if len(base_price_row) > 0 else None
                        option_name = base_price_row[1] if len(base_price_row) > 1 else None
                        price = base_price_row[2] if len(base_price_row) > 2 else None
                    
                    if category_num is not None and option_name is not None:
                        key = f"{category_num}_{option_name}"
                        base_price_map[key] = price
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue
        except Exception:
            base_price_map = {}
        
        # 옵션 목록 구성
        options_list = []
        print(f"견적서 생성 - selected_options.items() 반복 시작, 개수: {len(selected_options)}")
        for category_num, option_name in selected_options.items():
            try:
                cat_num = int(category_num)
                key = f"{cat_num}_{option_name}"
                
                set_price = price_map.get(key)
                base_price = base_price_map.get(key)
                final_price = base_price if base_price is not None else set_price
                
                print(f"  옵션 처리: 카테고리={cat_num}, 옵션명={option_name}, set_price={set_price}, base_price={base_price}, final_price={final_price}")
                
                options_list.append({
                    'category_number': cat_num,
                    'category_name': category_map.get(cat_num, f'카테고리 {cat_num}'),
                    'option_name': option_name,
                    'price': final_price
                })
            except (ValueError, TypeError) as e:
                print(f"  옵션 처리 오류: category_num={category_num}, option_name={option_name}, 오류={e}")
                continue
        
        # 카테고리 번호 순서로 정렬
        options_list.sort(key=lambda x: x['category_number'])
        
        print(f"견적서 생성 - 옵션 목록: {len(options_list)}개")
        for opt in options_list:
            print(f"  - [{opt['category_number']}] {opt['category_name']}: {opt['option_name']} (가격: {opt['price']})")
        
        # 양식 정보 가져오기
        template = None
        try:
            cursor.execute('''
                SELECT company_name, company_address, company_phone, company_email,
                       company_logo, quote_title, footer_note, terms_conditions
                FROM quote_template
                ORDER BY id DESC
                LIMIT 1
            ''')
            template_row = cursor.fetchone()
            if template_row:
                if hasattr(template_row, 'keys') and callable(getattr(template_row, 'keys', None)):
                    template = {
                        'company_name': template_row['company_name'] or '',
                        'company_address': template_row['company_address'] or '',
                        'company_phone': template_row['company_phone'] or '',
                        'company_email': template_row['company_email'] or '',
                        'company_logo': template_row['company_logo'] or '',
                        'quote_title': template_row['quote_title'] or '견적서',
                        'footer_note': template_row['footer_note'] or '',
                        'terms_conditions': template_row['terms_conditions'] or ''
                    }
                else:
                    template = {
                        'company_name': template_row[0] or '',
                        'company_address': template_row[1] or '',
                        'company_phone': template_row[2] or '',
                        'company_email': template_row[3] or '',
                        'company_logo': template_row[4] or '',
                        'quote_title': template_row[5] or '견적서',
                        'footer_note': template_row[6] or '',
                        'terms_conditions': template_row[7] or ''
                    }
        except Exception as e:
            print(f"양식 정보 조회 오류: {e}")
            import traceback
            print(traceback.format_exc())
            template = None
        
        if template:
            print(f"견적서 생성 시 사용할 양식 정보:")
            print(f"  - company_name: '{template.get('company_name')}'")
            print(f"  - company_address: '{template.get('company_address')}'")
            print(f"  - company_phone: '{template.get('company_phone')}'")
            print(f"  - company_email: '{template.get('company_email')}'")
            print(f"  - quote_title: '{template.get('quote_title')}'")
            print(f"  - footer_note: '{template.get('footer_note')}'")
            print(f"  - terms_conditions: '{template.get('terms_conditions')}'")
        else:
            print("견적서 생성 시 양식 정보 없음 - 기본값 사용")
        
        # 업로드한 엑셀 템플릿 파일 사용
        from openpyxl import load_workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        from openpyxl.cell.cell import MergedCell
        from io import BytesIO
        from datetime import datetime
        import re
        
        def safe_set_cell_value(ws, row, col, value):
            """병합된 셀을 고려하여 안전하게 셀에 값을 설정"""
            try:
                cell = ws.cell(row=row, column=col)
                # MergedCell인 경우 병합 범위의 첫 번째 셀을 찾아서 값 설정
                if isinstance(cell, MergedCell):
                    # 병합 범위 찾기
                    for merged_range in ws.merged_cells.ranges:
                        if (merged_range.min_row <= row <= merged_range.max_row and 
                            merged_range.min_col <= col <= merged_range.max_col):
                            # 병합 범위의 첫 번째 셀에 값 설정
                            first_cell = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
                            first_cell.value = value
                            return
                # 일반 셀이면 그냥 값 설정
                cell.value = value
            except Exception as e:
                print(f"셀 값 설정 오류 (행 {row}, 열 {col}): {e}")
                # 오류 발생 시에도 계속 진행
                try:
                    ws.cell(row=row, column=col).value = value
                except:
                    pass
        
        templates_dir = os.path.join(os.path.dirname(__file__), 'quote_templates')
        template_filepath = os.path.join(templates_dir, 'quote_template.xlsx')
        
        # 템플릿 파일이 있으면 사용, 없으면 새로 생성
        if os.path.exists(template_filepath):
            try:
                print(f"템플릿 파일 사용: {template_filepath}")
                wb = load_workbook(template_filepath)
                ws = wb.active
                
                max_row = ws.max_row if ws.max_row else 100
                max_col = ws.max_column if ws.max_column else 10
                
                # 템플릿에서 데이터 업데이트
                # 10번째 줄에 Expiration Date 설정 (입력받은 값 또는 빈 값)
                try:
                    for col_idx in range(1, max_col + 1):
                        cell_10 = ws.cell(row=10, column=col_idx)
                        if cell_10.value and ('Expiration Date' in str(cell_10.value) or '만료일' in str(cell_10.value)):
                            # 다음 셀에 입력받은 만료일 값 입력 (없으면 빈 값)
                            if col_idx < max_col:
                                next_cell = ws.cell(row=10, column=col_idx + 1)
                                expiration_value = expiration_date if expiration_date else ''
                                if not isinstance(next_cell, MergedCell):
                                    next_cell.value = expiration_value
                                else:
                                    safe_set_cell_value(ws, 10, col_idx + 1, expiration_value)
                            break
                except Exception as e:
                    print(f"10번째 줄 Expiration Date 설정 오류: {e}")
                
                # 13번째 줄에 세트코드, 14번째 줄에 통화 단위 직접 설정
                try:
                    # 13번째 줄의 "set code" 칸 찾기 (일반적으로 B열 또는 C열)
                    for col_idx in range(1, max_col + 1):
                        cell_13 = ws.cell(row=13, column=col_idx)
                        if cell_13.value and ('set code' in str(cell_13.value).lower() or '세트코드' in str(cell_13.value)):
                            # 다음 셀에 세트코드 입력
                            if col_idx < max_col:
                                next_cell = ws.cell(row=13, column=col_idx + 1)
                                if not isinstance(next_cell, MergedCell):
                                    next_cell.value = set_code
                                else:
                                    safe_set_cell_value(ws, 13, col_idx + 1, set_code)
                            break
                    
                    # 14번째 줄의 "Currency unit" 칸 찾기
                    for col_idx in range(1, max_col + 1):
                        cell_14 = ws.cell(row=14, column=col_idx)
                        if cell_14.value and ('currency unit' in str(cell_14.value).lower() or '통화' in str(cell_14.value)):
                            # 다음 셀에 통화 단위 입력
                            if col_idx < max_col:
                                next_cell = ws.cell(row=14, column=col_idx + 1)
                                if not isinstance(next_cell, MergedCell):
                                    next_cell.value = currency
                                else:
                                    safe_set_cell_value(ws, 14, col_idx + 1, currency)
                            break
                except Exception as e:
                    print(f"13-14번째 줄 데이터 설정 오류: {e}")
                
                # 기존 방식: 텍스트를 찾아서 업데이트 (13-14번째 줄 외의 경우)
                for row_idx in range(1, max_row + 1):
                    # 13-14번째 줄은 이미 처리했으므로 건너뛰기
                    if row_idx == 13 or row_idx == 14:
                        continue
                    
                    for col_idx in range(1, max_col + 1):
                        try:
                            cell = ws.cell(row=row_idx, column=col_idx)
                            if cell.value:
                                value_str = str(cell.value).strip()
                                # 세트코드 찾기
                                if '세트코드' in value_str or 'Set Code' in value_str:
                                    # 다음 셀에 세트코드 입력
                                    if col_idx < max_col:
                                        next_cell = ws.cell(row=row_idx, column=col_idx + 1)
                                        if not isinstance(next_cell, MergedCell):
                                            next_cell.value = set_code
                                        else:
                                            safe_set_cell_value(ws, row_idx, col_idx + 1, set_code)
                                # Expiration Date 찾기 (입력받은 값 또는 빈 값으로 설정)
                                elif 'Expiration Date' in value_str or '만료일' in value_str:
                                    if col_idx < max_col:
                                        next_cell = ws.cell(row=row_idx, column=col_idx + 1)
                                        expiration_value = expiration_date if expiration_date else ''
                                        if not isinstance(next_cell, MergedCell):
                                            next_cell.value = expiration_value
                                        else:
                                            safe_set_cell_value(ws, row_idx, col_idx + 1, expiration_value)
                                # 견적일자 찾기
                                elif '견적일자' in value_str or ('Date' in value_str and 'Expiration' not in value_str) or '견적일' in value_str:
                                    if col_idx < max_col:
                                        next_cell = ws.cell(row=row_idx, column=col_idx + 1)
                                        if not isinstance(next_cell, MergedCell):
                                            next_cell.value = datetime.now().strftime('%Y-%m-%d')
                                        else:
                                            safe_set_cell_value(ws, row_idx, col_idx + 1, datetime.now().strftime('%Y-%m-%d'))
                                # 통화 찾기
                                elif '통화' in value_str and '환율' not in value_str:
                                    if col_idx < max_col:
                                        next_cell = ws.cell(row=row_idx, column=col_idx + 1)
                                        if not isinstance(next_cell, MergedCell):
                                            next_cell.value = currency
                                        else:
                                            safe_set_cell_value(ws, row_idx, col_idx + 1, currency)
                                # 환율 찾기
                                elif '환율' in value_str or 'Exchange Rate' in value_str:
                                    if currency == 'USD' and col_idx < max_col:
                                        next_cell = ws.cell(row=row_idx, column=col_idx + 1)
                                        if not isinstance(next_cell, MergedCell):
                                            next_cell.value = f'{exchange_rate:.2f} USD'
                                        else:
                                            safe_set_cell_value(ws, row_idx, col_idx + 1, f'{exchange_rate:.2f} USD')
                                # 할인율 찾기
                                elif '할인율' in value_str or 'Discount' in value_str:
                                    if col_idx < max_col:
                                        next_cell = ws.cell(row=row_idx, column=col_idx + 1)
                                        if not isinstance(next_cell, MergedCell):
                                            next_cell.value = f'{discount_rate:.1f}%'
                                        else:
                                            safe_set_cell_value(ws, row_idx, col_idx + 1, f'{discount_rate:.1f}%')
                                # 예시 데이터 제거 (P51000000 등)
                                elif value_str == 'P51000000' or value_str == '(예시)' or 'EUR (예시)' in value_str or 'USD (예시)' in value_str:
                                    if not isinstance(cell, MergedCell):
                                        cell.value = ''
                                    else:
                                        safe_set_cell_value(ws, row_idx, col_idx, '')
                        except Exception as e:
                            print(f"셀 처리 오류 (행 {row_idx}, 열 {col_idx}): {e}")
                            continue
                
                # 옵션 테이블 찾아서 데이터 채우기
                # 헤더 행 찾기 (더 넓은 범위로 검색)
                header_row = None
                search_rows = min(100, max_row + 1)  # 검색 범위 확대
                for row_idx in range(1, search_rows):
                    for col_idx in range(1, max_col + 1):
                        try:
                            cell = ws.cell(row=row_idx, column=col_idx)
                            if cell.value:
                                cell_str = str(cell.value).strip()
                                # 더 많은 키워드로 헤더 행 찾기
                                if any(keyword in cell_str for keyword in ['카테고리', '옵션명', 'Category', 'Option', '옵션', '항목', 'Item', '품목']):
                                    header_row = row_idx
                                    print(f"헤더 행 찾음: {row_idx}, 셀 값: {cell_str}")
                                    break
                        except:
                            continue
                    if header_row:
                        break
                
                # 헤더 행을 찾지 못한 경우, 데이터가 있는 첫 번째 행을 헤더로 간주
                if not header_row:
                    print("헤더 행을 찾지 못함 - 데이터 영역 검색 중...")
                    for row_idx in range(1, min(50, max_row + 1)):
                        row_has_data = False
                        for col_idx in range(1, max_col + 1):
                            try:
                                cell = ws.cell(row=row_idx, column=col_idx)
                                if cell.value and str(cell.value).strip():
                                    row_has_data = True
                                    break
                            except:
                                continue
                        if row_has_data:
                            # 데이터가 있는 첫 번째 행을 헤더로 간주하고, 그 다음 행부터 데이터 삽입
                            header_row = row_idx
                            print(f"데이터 영역 찾음 - 헤더 행으로 설정: {row_idx}")
                            break
                
                if header_row:
                    print(f"옵션 데이터 삽입 시작 - 헤더 행: {header_row}, 옵션 수: {len(options_list)}")
                    # 예시 데이터 행 찾기 (역순으로 삭제해야 인덱스 문제 없음)
                    rows_to_delete = []
                    for row_idx in range(header_row + 1, max_row + 1):
                        try:
                            first_cell = ws.cell(row=row_idx, column=1)
                            if first_cell.value and ('P51000000' in str(first_cell.value) or '(예시)' in str(first_cell.value)):
                                rows_to_delete.append(row_idx)
                        except:
                            continue
                    
                    # 역순으로 삭제 (인덱스 문제 방지)
                    for row_idx in reversed(rows_to_delete):
                        try:
                            ws.delete_rows(row_idx)
                        except Exception as e:
                            print(f"행 삭제 오류 (행 {row_idx}): {e}")
                    
                    # 옵션 데이터 추가 위치 찾기
                    data_start_row = header_row + 1
                    max_row_after_delete = ws.max_row if ws.max_row else max_row
                    for row_idx in range(header_row + 1, max_row_after_delete + 1):
                        try:
                            first_cell = ws.cell(row=row_idx, column=1)
                            cell_value = str(first_cell.value) if first_cell.value else ''
                            if not first_cell.value or first_cell.value == '':
                                data_start_row = row_idx
                                print(f"빈 행 찾음 - 데이터 시작 행: {data_start_row}")
                                break
                            if '합계' in cell_value or 'Total' in cell_value or '합계' in cell_value:
                                data_start_row = row_idx
                                print(f"합계 행 찾음 - 데이터 시작 행: {data_start_row}")
                                break
                        except:
                            continue
                    
                    print(f"옵션 데이터 삽입 위치: {data_start_row}")
                    
                    # 옵션 데이터 삽입
                    current_row = data_start_row
                    total_original = 0.0
                    total_discounted = 0.0
                    
                    print(f"옵션 데이터 삽입 시작 - 총 {len(options_list)}개 옵션")
                    for option in options_list:
                        try:
                            original_price = option['price'] if option['price'] is not None else 0.0
                            if currency == 'USD':
                                original_price = original_price * exchange_rate
                            
                            discounted_price = original_price * (1 - discount_rate / 100) if original_price > 0 else 0.0
                            
                            total_original += original_price
                            total_discounted += discounted_price
                            
                            # 카테고리 (병합된 셀 고려)
                            safe_set_cell_value(ws, current_row, 1, f"[{option['category_number']}] {option['category_name']}")
                            # 옵션명 (병합된 셀 고려)
                            safe_set_cell_value(ws, current_row, 2, option['option_name'])
                            # 원 가격 (병합된 셀 고려)
                            if original_price > 0:
                                try:
                                    price_cell = ws.cell(row=current_row, column=3)
                                    if not isinstance(price_cell, MergedCell):
                                        price_cell.value = original_price
                                        price_cell.number_format = '#,##0.00'
                                    else:
                                        safe_set_cell_value(ws, current_row, 3, original_price)
                                        # 병합 범위의 첫 번째 셀에 포맷 적용
                                        for merged_range in ws.merged_cells.ranges:
                                            if (merged_range.min_row <= current_row <= merged_range.max_row and 
                                                merged_range.min_col <= 3 <= merged_range.max_col):
                                                first_cell = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
                                                first_cell.number_format = '#,##0.00'
                                                break
                                except Exception as e:
                                    print(f"가격 셀 설정 오류: {e}")
                                    safe_set_cell_value(ws, current_row, 3, original_price)
                            # 할인 가격 (병합된 셀 고려)
                            if discounted_price > 0:
                                try:
                                    discount_cell = ws.cell(row=current_row, column=4)
                                    if not isinstance(discount_cell, MergedCell):
                                        discount_cell.value = discounted_price
                                        discount_cell.number_format = '#,##0.00'
                                    else:
                                        safe_set_cell_value(ws, current_row, 4, discounted_price)
                                        # 병합 범위의 첫 번째 셀에 포맷 적용
                                        for merged_range in ws.merged_cells.ranges:
                                            if (merged_range.min_row <= current_row <= merged_range.max_row and 
                                                merged_range.min_col <= 4 <= merged_range.max_col):
                                                first_cell = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
                                                first_cell.number_format = '#,##0.00'
                                                break
                                except Exception as e:
                                    print(f"할인 가격 셀 설정 오류: {e}")
                                    safe_set_cell_value(ws, current_row, 4, discounted_price)
                            
                            current_row += 1
                            print(f"옵션 삽입 완료: [{option['category_number']}] {option['option_name']} (행 {current_row-1})")
                        except Exception as e:
                            print(f"옵션 데이터 삽입 오류: {e}")
                            import traceback
                            print(traceback.format_exc())
                            continue
                    
                    print(f"옵션 데이터 삽입 완료 - 총 {len(options_list)}개, 합계 원가: {total_original}, 합계 할인가: {total_discounted}")
                    
                    # 합계 행 찾아서 업데이트
                    total_row_found = False
                    max_row_final = ws.max_row if ws.max_row else current_row + 10
                    for row_idx in range(current_row, min(current_row + 10, max_row_final + 1)):
                        try:
                            first_cell = ws.cell(row=row_idx, column=1)
                            if first_cell.value and ('합계' in str(first_cell.value) or 'Total' in str(first_cell.value)):
                                # 합계 가격 업데이트 (병합된 셀 고려)
                                try:
                                    price_cell = ws.cell(row=row_idx, column=3)
                                    if not isinstance(price_cell, MergedCell):
                                        price_cell.value = total_original
                                        price_cell.number_format = '#,##0.00'
                                    else:
                                        safe_set_cell_value(ws, row_idx, 3, total_original)
                                        for merged_range in ws.merged_cells.ranges:
                                            if (merged_range.min_row <= row_idx <= merged_range.max_row and 
                                                merged_range.min_col <= 3 <= merged_range.max_col):
                                                first_cell = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
                                                first_cell.number_format = '#,##0.00'
                                                break
                                except Exception as e:
                                    print(f"합계 원가 셀 설정 오류: {e}")
                                    safe_set_cell_value(ws, row_idx, 3, total_original)
                                
                                try:
                                    discount_cell = ws.cell(row=row_idx, column=4)
                                    if not isinstance(discount_cell, MergedCell):
                                        discount_cell.value = total_discounted
                                        discount_cell.number_format = '#,##0.00'
                                    else:
                                        safe_set_cell_value(ws, row_idx, 4, total_discounted)
                                        for merged_range in ws.merged_cells.ranges:
                                            if (merged_range.min_row <= row_idx <= merged_range.max_row and 
                                                merged_range.min_col <= 4 <= merged_range.max_col):
                                                first_cell = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
                                                first_cell.number_format = '#,##0.00'
                                                break
                                except Exception as e:
                                    print(f"합계 할인가 셀 설정 오류: {e}")
                                    safe_set_cell_value(ws, row_idx, 4, total_discounted)
                                total_row_found = True
                                print(f"합계 행 업데이트 완료 (행 {row_idx})")
                                break
                        except Exception as e:
                            print(f"합계 행 업데이트 오류 (행 {row_idx}): {e}")
                            continue
                    
                    # 합계 행을 찾지 못한 경우 새로 추가
                    if not total_row_found:
                        print(f"합계 행을 찾지 못함 - 새로 추가 (행 {current_row})")
                        try:
                            safe_set_cell_value(ws, current_row, 1, '합계')
                            safe_set_cell_value(ws, current_row, 2, '')
                            safe_set_cell_value(ws, current_row, 3, total_original)
                            safe_set_cell_value(ws, current_row, 4, total_discounted)
                            
                            # 합계 행 스타일 적용
                            try:
                                total_font = Font(bold=True)
                                total_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
                                for col in range(1, 5):
                                    cell = ws.cell(row=current_row, column=col)
                                    if not isinstance(cell, MergedCell):
                                        cell.font = total_font
                                        cell.fill = total_fill
                                
                                # 숫자 포맷 적용
                                price_cell = ws.cell(row=current_row, column=3)
                                discount_cell = ws.cell(row=current_row, column=4)
                                if not isinstance(price_cell, MergedCell):
                                    price_cell.number_format = '#,##0.00'
                                if not isinstance(discount_cell, MergedCell):
                                    discount_cell.number_format = '#,##0.00'
                            except Exception as e:
                                print(f"합계 행 스타일 적용 오류: {e}")
                        except Exception as e:
                            print(f"합계 행 추가 오류: {e}")
                            import traceback
                            print(traceback.format_exc())
                # 헤더 행을 찾지 못한 경우에도 데이터 삽입
                if not header_row:
                    print(f"경고: 헤더 행을 찾을 수 없습니다. 마지막 행 다음에 옵션 데이터를 삽입합니다.")
                    # 마지막 행 다음에 데이터 추가
                    last_row = ws.max_row if ws.max_row else 1
                    print(f"헤더 행 없음 - 마지막 행 다음에 데이터 추가: {last_row + 1}")
                    current_row = last_row + 1
                    total_original = 0.0
                    total_discounted = 0.0
                    
                    # 헤더가 없으면 먼저 헤더 추가
                    ws.cell(row=current_row, column=1).value = '카테고리'
                    ws.cell(row=current_row, column=2).value = '옵션명'
                    ws.cell(row=current_row, column=3).value = f'원 가격 ({currency})'
                    ws.cell(row=current_row, column=4).value = f'할인 가격 ({currency})'
                    current_row += 1
                    
                    print(f"옵션 데이터 삽입 시작 (헤더 없음) - 총 {len(options_list)}개 옵션")
                    for option in options_list:
                        try:
                            original_price = option['price'] if option['price'] is not None else 0.0
                            if currency == 'USD':
                                original_price = original_price * exchange_rate
                            
                            discounted_price = original_price * (1 - discount_rate / 100) if original_price > 0 else 0.0
                            
                            total_original += original_price
                            total_discounted += discounted_price
                            
                            # 카테고리 (병합된 셀 고려)
                            safe_set_cell_value(ws, current_row, 1, f"[{option['category_number']}] {option['category_name']}")
                            # 옵션명 (병합된 셀 고려)
                            safe_set_cell_value(ws, current_row, 2, option['option_name'])
                            # 원 가격 (병합된 셀 고려)
                            if original_price > 0:
                                try:
                                    price_cell = ws.cell(row=current_row, column=3)
                                    if not isinstance(price_cell, MergedCell):
                                        price_cell.value = original_price
                                        price_cell.number_format = '#,##0.00'
                                    else:
                                        safe_set_cell_value(ws, current_row, 3, original_price)
                                        # 병합 범위의 첫 번째 셀에 포맷 적용
                                        for merged_range in ws.merged_cells.ranges:
                                            if (merged_range.min_row <= current_row <= merged_range.max_row and 
                                                merged_range.min_col <= 3 <= merged_range.max_col):
                                                first_cell = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
                                                first_cell.number_format = '#,##0.00'
                                                break
                                except Exception as e:
                                    print(f"가격 셀 설정 오류 (헤더 없음): {e}")
                                    safe_set_cell_value(ws, current_row, 3, original_price)
                            # 할인 가격 (병합된 셀 고려)
                            if discounted_price > 0:
                                try:
                                    discount_cell = ws.cell(row=current_row, column=4)
                                    if not isinstance(discount_cell, MergedCell):
                                        discount_cell.value = discounted_price
                                        discount_cell.number_format = '#,##0.00'
                                    else:
                                        safe_set_cell_value(ws, current_row, 4, discounted_price)
                                        # 병합 범위의 첫 번째 셀에 포맷 적용
                                        for merged_range in ws.merged_cells.ranges:
                                            if (merged_range.min_row <= current_row <= merged_range.max_row and 
                                                merged_range.min_col <= 4 <= merged_range.max_col):
                                                first_cell = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
                                                first_cell.number_format = '#,##0.00'
                                                break
                                except Exception as e:
                                    print(f"할인 가격 셀 설정 오류 (헤더 없음): {e}")
                                    safe_set_cell_value(ws, current_row, 4, discounted_price)
                            
                            current_row += 1
                        except Exception as e:
                            print(f"옵션 데이터 삽입 오류 (헤더 없음): {e}")
                            continue
                    
                    print(f"옵션 데이터 삽입 완료 (헤더 없음) - 총 {len(options_list)}개, 합계 원가: {total_original}, 합계 할인가: {total_discounted}")
                    
                    # 합계 행 추가
                    print(f"합계 행 추가 (행 {current_row})")
                    try:
                        safe_set_cell_value(ws, current_row, 1, '합계')
                        safe_set_cell_value(ws, current_row, 2, '')
                        safe_set_cell_value(ws, current_row, 3, total_original)
                        safe_set_cell_value(ws, current_row, 4, total_discounted)
                        
                        # 합계 행 스타일 적용
                        try:
                            total_font = Font(bold=True)
                            total_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
                            for col in range(1, 5):
                                cell = ws.cell(row=current_row, column=col)
                                if not isinstance(cell, MergedCell):
                                    cell.font = total_font
                                    cell.fill = total_fill
                            
                            # 숫자 포맷 적용
                            price_cell = ws.cell(row=current_row, column=3)
                            discount_cell = ws.cell(row=current_row, column=4)
                            if not isinstance(price_cell, MergedCell):
                                price_cell.number_format = '#,##0.00'
                            if not isinstance(discount_cell, MergedCell):
                                discount_cell.number_format = '#,##0.00'
                        except Exception as e:
                            print(f"합계 행 스타일 적용 오류: {e}")
                    except Exception as e:
                        print(f"합계 행 추가 오류 (헤더 없음): {e}")
                        import traceback
                        print(traceback.format_exc())
                    
            except Exception as template_error:
                print(f"템플릿 파일 처리 오류: {template_error}")
                import traceback
                print(traceback.format_exc())
                # 템플릿 파일 오류 시 새로 생성하도록 fallback
                wb = None
        else:
            wb = None
        
        # 템플릿 파일이 없거나 오류 발생 시 새로 생성
        if wb is None:
            # 템플릿 파일이 없으면 기존 방식으로 새로 생성
            print("템플릿 파일 없음 - 새로 생성")
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "견적서"
            
            # 헤더 스타일 설정
            header_font = Font(bold=True, color="FFFFFF")
            header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
            header_alignment = Alignment(horizontal="center", vertical="center")
            
            row = 1
            
            # 회사 정보 섹션 (양식이 있는 경우)
            if template and template.get('company_name'):
                ws[f'A{row}'] = template['company_name']
                ws.merge_cells(f'A{row}:D{row}')
                company_cell = ws[f'A{row}']
                company_cell.font = Font(bold=True, size=14)
                company_cell.alignment = Alignment(horizontal="center", vertical="center")
                row += 1
                
                if template.get('company_address'):
                    ws[f'A{row}'] = template['company_address']
                    ws.merge_cells(f'A{row}:D{row}')
                    ws[f'A{row}'].alignment = Alignment(horizontal="center", vertical="center")
                    row += 1
                
                contact_info = []
                if template.get('company_phone'):
                    contact_info.append(f"전화: {template['company_phone']}")
                if template.get('company_email'):
                    contact_info.append(f"이메일: {template['company_email']}")
                if contact_info:
                    ws[f'A{row}'] = ' | '.join(contact_info)
                    ws.merge_cells(f'A{row}:D{row}')
                    ws[f'A{row}'].alignment = Alignment(horizontal="center", vertical="center")
                    row += 1
                
                row += 1
            
            # 견적서 제목
            quote_title = template['quote_title'] if template and template.get('quote_title') else '견적서'
            ws[f'A{row}'] = quote_title
            ws.merge_cells(f'A{row}:D{row}')
            title_cell = ws[f'A{row}']
            title_cell.font = Font(bold=True, size=16, color="800020")
            title_cell.alignment = Alignment(horizontal="center", vertical="center")
            
            row += 2
            
            # 견적서 정보 섹션
            ws[f'A{row}'] = '세트코드'
            ws[f'B{row}'] = set_code
            row += 1
            ws[f'A{row}'] = '견적일자'
            ws[f'B{row}'] = datetime.now().strftime('%Y-%m-%d')
            row += 1
            ws[f'A{row}'] = '통화'
            ws[f'B{row}'] = currency
            if currency == 'USD':
                row += 1
                ws[f'A{row}'] = '환율 (1 EUR)'
                ws[f'B{row}'] = f'{exchange_rate:.2f} USD'
            row += 1
            ws[f'A{row}'] = '할인율'
            ws[f'B{row}'] = f'{discount_rate:.1f}%'
            
            # 정보 섹션 스타일
            for r in range(row - 3, row + 1):
                info_label = ws[f'A{r}']
                info_label.font = Font(bold=True)
                info_label.fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
            
            row += 2
            
            # 헤더 작성
            ws[f'A{row}'] = '카테고리'
            ws[f'B{row}'] = '옵션명'
            ws[f'C{row}'] = f'원 가격 ({currency})'
            ws[f'D{row}'] = f'할인 가격 ({currency})'
            
            # 헤더 스타일 적용
            for cell in ws[row]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
            
            # 옵션별 가격 데이터 작성
            row += 1
            total_original = 0.0
            total_discounted = 0.0
            
            for option in options_list:
                original_price = option['price'] if option['price'] is not None else 0.0
                if currency == 'USD':
                    original_price = original_price * exchange_rate
                
                discounted_price = original_price * (1 - discount_rate / 100) if original_price > 0 else 0.0
                
                total_original += original_price
                total_discounted += discounted_price
                
                ws[f'A{row}'] = f"[{option['category_number']}] {option['category_name']}"
                ws[f'B{row}'] = option['option_name']
                if original_price > 0:
                    ws[f'C{row}'] = original_price
                    ws[f'C{row}'].number_format = '#,##0.00'
                else:
                    ws[f'C{row}'] = '-'
                if discounted_price > 0:
                    ws[f'D{row}'] = discounted_price
                    ws[f'D{row}'].number_format = '#,##0.00'
                else:
                    ws[f'D{row}'] = '-'
                
                row += 1
            
            # 합계 행 추가
            ws[f'A{row}'] = '합계'
            ws[f'B{row}'] = ''
            ws[f'C{row}'] = total_original
            ws[f'D{row}'] = total_discounted
            
            # 합계 행 스타일 적용
            total_font = Font(bold=True)
            total_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
            for cell in ws[row]:
                cell.font = total_font
                cell.fill = total_fill
            
            ws[f'C{row}'].number_format = '#,##0.00'
            ws[f'D{row}'].number_format = '#,##0.00'
            
            # 컬럼 너비 자동 조정
            ws.column_dimensions['A'].width = 30
            ws.column_dimensions['B'].width = 30
            ws.column_dimensions['C'].width = 20
            ws.column_dimensions['D'].width = 20
            
            # 각주/메모 추가 (양식이 있는 경우)
            if template:
                row += 2
                if template.get('footer_note'):
                    ws[f'A{row}'] = template['footer_note']
                    ws.merge_cells(f'A{row}:D{row}')
                    ws[f'A{row}'].font = Font(italic=True, size=9)
                    ws[f'A{row}'].alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
                    row += 1
                
                if template.get('terms_conditions'):
                    row += 1
                    ws[f'A{row}'] = '조건/약관'
                    ws.merge_cells(f'A{row}:D{row}')
                    ws[f'A{row}'].font = Font(bold=True)
                    row += 1
                    ws[f'A{row}'] = template['terms_conditions']
                    ws.merge_cells(f'A{row}:D{row}')
                    ws[f'A{row}'].font = Font(size=9)
                    ws[f'A{row}'].alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        
        # 엑셀 파일을 메모리에 저장
        excel_buffer = BytesIO()
        wb.save(excel_buffer)
        excel_buffer.seek(0)
        
        from flask import send_file
        return send_file(
            excel_buffer,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'견적서_{set_code}.xlsx'
        )
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_download_quote: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/set_price/option_price/update', methods=['POST'])
def api_update_option_price():
    """옵션별 가격 저장/수정 API"""
    try:
        data = request.get_json()
        set_code = data.get('set_code')
        category_number = data.get('category_number')
        option_name = data.get('option_name')
        price = data.get('price')
        
        if not set_code or category_number is None or not option_name:
            return jsonify({
                "success": False,
                "error": "필수 정보가 누락되었습니다."
            }), 400
        
        # 빈 입력은 0으로 처리
        if price is None or price == '':
            price = 0.0
        else:
            try:
                price = float(price)
                if price < 0:
                    return jsonify({
                        "success": False,
                        "error": "가격은 0 이상이어야 합니다."
                    }), 400
                # 소수점 둘째 자리까지 반올림
                price = round(price, 2)
            except (ValueError, TypeError):
                return jsonify({
                    "success": False,
                    "error": "올바른 숫자를 입력해주세요."
                }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 기존 레코드 확인
        cursor.execute('''
            SELECT id FROM option_prices
            WHERE set_code = ? AND category_number = ? AND option_name = ?
        ''', (set_code, category_number, option_name))
        
        existing = cursor.fetchone()
        
        if existing:
            # 업데이트
            cursor.execute('''
                UPDATE option_prices
                SET price = ?, updated_at = CURRENT_TIMESTAMP
                WHERE set_code = ? AND category_number = ? AND option_name = ?
            ''', (price, set_code, category_number, option_name))
        else:
            # 삽입
            cursor.execute('''
                INSERT INTO option_prices (set_code, category_number, option_name, price, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (set_code, category_number, option_name, price))
        
        system.connection.commit()
        
        # 옵션별 가격 저장 후 자동으로 합계 계산하여 세트코드 가격 업데이트
        # 0인 가격도 포함하여 합계 계산
        cursor.execute('''
            SELECT COALESCE(SUM(COALESCE(price, 0)), 0) as total_price
            FROM option_prices
            WHERE set_code = ?
        ''', (set_code,))
        
        total_result = cursor.fetchone()
        total_price = 0
        if total_result:
            try:
                if hasattr(total_result, 'keys') and callable(getattr(total_result, 'keys', None)):
                    if 'total_price' in total_result.keys():
                        total_price = float(total_result['total_price']) if total_result['total_price'] is not None else 0
                    elif len(total_result) > 0:
                        total_price = float(total_result[0]) if total_result[0] is not None else 0
                else:
                    # keys() 메서드가 없으면 인덱스로 접근
                    total_price = float(total_result[0]) if len(total_result) > 0 and total_result[0] is not None else 0
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as e:
                print(f"Warning: Error accessing total_result: {e}, result type: {type(total_result)}")
                total_price = 0
        
        # 세트코드 가격 업데이트
        cursor.execute('''
            UPDATE selected_combinations
            SET price = ?
            WHERE set_code = ?
        ''', (total_price, set_code))
        
        system.connection.commit()
        invalidate_set_price_cache()
        
        return jsonify({
            "success": True,
            "message": f"옵션 가격이 저장되었습니다.",
            "set_code": set_code,
            "category_number": category_number,
            "option_name": option_name,
            "price": price,
            "total_price": total_price
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/option/base_price/update', methods=['POST'])
def api_update_option_base_price():
    """옵션별 기본 가격 저장/수정 API (카테고리별)"""
    try:
        data = request.get_json()
        category_number = data.get('category_number')
        option_name = data.get('option_name')
        price = data.get('price')
        
        if category_number is None or not option_name:
            return jsonify({
                "success": False,
                "error": "필수 정보가 누락되었습니다."
            }), 400
        
        # 빈 입력은 0으로 처리
        if price is None or price == '':
            price = 0.0
        else:
            try:
                price = float(price)
                if price < 0:
                    return jsonify({
                        "success": False,
                        "error": "가격은 0 이상이어야 합니다."
                    }), 400
                # 소수점 둘째 자리까지 반올림
                price = round(price, 2)
            except (ValueError, TypeError):
                return jsonify({
                    "success": False,
                    "error": "올바른 숫자를 입력해주세요."
                }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 기본 옵션 가격 테이블이 없으면 생성
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
        
        # 기존 레코드 확인
        cursor.execute('''
            SELECT id FROM option_base_prices
            WHERE category_number = ? AND option_name = ?
        ''', (category_number, option_name))
        
        existing = cursor.fetchone()
        
        if existing:
            # 업데이트
            cursor.execute('''
                UPDATE option_base_prices
                SET price = ?, updated_at = CURRENT_TIMESTAMP
                WHERE category_number = ? AND option_name = ?
            ''', (price, category_number, option_name))
        else:
            # 삽입
            cursor.execute('''
                INSERT INTO option_base_prices (category_number, option_name, price, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (category_number, option_name, price))
        
        system.connection.commit()
        
        return jsonify({
            "success": True,
            "message": f"옵션 기본 가격이 저장되었습니다.",
            "category_number": category_number,
            "option_name": option_name,
            "price": price
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/option/base_price/batch_update', methods=['POST'])
def api_batch_update_option_base_prices():
    """옵션별 기본 가격 일괄 저장/수정 API"""
    try:
        data = request.get_json()
        prices = data.get('prices', [])
        
        if not isinstance(prices, list) or len(prices) == 0:
            return jsonify({
                "success": False,
                "error": "가격 목록이 필요합니다."
            }), 400
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 기본 옵션 가격 테이블이 없으면 생성
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
        
        success_count = 0
        error_count = 0
        errors = []
        
        # 트랜잭션 시작
        for price_data in prices:
            try:
                category_number = price_data.get('category_number')
                option_name = price_data.get('option_name')
                price = price_data.get('price')
                
                if category_number is None or not option_name:
                    error_count += 1
                    errors.append(f"필수 정보 누락: category_number={category_number}, option_name={option_name}")
                    continue
                
                # 빈 입력은 0으로 처리
                if price is None or price == '':
                    price = 0.0
                else:
                    try:
                        price = float(price)
                        if price < 0:
                            error_count += 1
                            errors.append(f"가격 오류: {option_name} (가격은 0 이상이어야 합니다)")
                            continue
                        # 소수점 둘째 자리까지 반올림
                        price = round(price, 2)
                    except (ValueError, TypeError):
                        error_count += 1
                        errors.append(f"가격 형식 오류: {option_name}")
                        continue
                
                # 기존 레코드 확인 및 업데이트/삽입
                cursor.execute('''
                    INSERT INTO option_base_prices (category_number, option_name, price, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(category_number, option_name) 
                    DO UPDATE SET price = excluded.price, updated_at = CURRENT_TIMESTAMP
                ''', (category_number, option_name, price))
                
                success_count += 1
            except Exception as e:
                error_count += 1
                errors.append(f"처리 오류: {option_name} - {str(e)}")
        
        # 커밋
        system.connection.commit()
        
        return jsonify({
            "success": True,
            "message": f"{success_count}개의 옵션 가격이 저장되었습니다.",
            "success_count": success_count,
            "error_count": error_count,
            "errors": errors if errors else None
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/option/base_price/list', methods=['GET'])
def api_get_option_base_prices():
    """옵션별 기본 가격 목록 조회 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 기본 옵션 가격 테이블이 없으면 생성
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
        
        # 수출견적서(스태프) 제품 가격에서 옵션별 가격 계산 (제품코드 기준)
        # 모든 제품 조회 (최신 업데이트 순으로 정렬, updated_at이 NULL인 경우도 처리)
        cursor.execute('''
            SELECT product_code, price_book_price, updated_at, created_at
            FROM export_quotation_new_products
            WHERE price_book_price IS NOT NULL AND price_book_price > 0
            ORDER BY 
                CASE 
                    WHEN updated_at IS NULL THEN created_at
                    ELSE updated_at
                END DESC,
                created_at DESC
        ''')
        
        product_rows = cursor.fetchall()
        
        # 옵션별 가격 맵 (카테고리번호_옵션명 -> 최신 가격)
        option_price_map = {}  # 각 옵션의 최신 가격 (같은 옵션이 여러 제품에 있으면 최신 제품의 가격 사용)
        option_updated_map = {}  # 각 옵션의 최신 업데이트 시간
        option_product_map = {}  # 각 옵션이 어떤 제품에서 온 것인지 추적 (디버깅용)
        
        import json
        for product_row in product_rows:
            try:
                if hasattr(product_row, 'keys') and callable(getattr(product_row, 'keys', None)):
                    product_code = product_row['product_code'] if 'product_code' in product_row.keys() else (product_row[0] if len(product_row) > 0 else None)
                    price_book_price = product_row['price_book_price'] if 'price_book_price' in product_row.keys() else (product_row[1] if len(product_row) > 1 else None)
                    updated_at = product_row['updated_at'] if 'updated_at' in product_row.keys() else (product_row[2] if len(product_row) > 2 else None)
                    created_at = product_row['created_at'] if 'created_at' in product_row.keys() else (product_row[3] if len(product_row) > 3 else None)
                else:
                    product_code = product_row[0] if len(product_row) > 0 else None
                    price_book_price = product_row[1] if len(product_row) > 1 else None
                    updated_at = product_row[2] if len(product_row) > 2 else None
                    created_at = product_row[3] if len(product_row) > 3 else None
                
                # updated_at이 NULL이면 created_at 사용
                if not updated_at:
                    updated_at = created_at
                
                if not product_code or not price_book_price:
                    continue
                
                # 제품 코드로 옵션 조회 (제품코드 기준)
                cursor.execute('''
                    SELECT selected_options
                    FROM selected_combinations
                    WHERE combination_code = ? OR set_code = ?
                    LIMIT 1
                ''', (product_code, product_code))
                
                option_row = cursor.fetchone()
                if option_row:
                    try:
                        if hasattr(option_row, 'keys') and callable(getattr(option_row, 'keys', None)):
                            selected_options_str = option_row['selected_options'] if 'selected_options' in option_row.keys() else (option_row[0] if len(option_row) > 0 else None)
                        else:
                            selected_options_str = option_row[0] if len(option_row) > 0 else None
                        
                        if selected_options_str:
                            selected_options = json.loads(selected_options_str) if selected_options_str else {}
                            
                            # 유효한 옵션 개수 계산 (값이 있고 '선택없음'이 아닌 것)
                            valid_options = {k: v for k, v in selected_options.items() if v and v != '선택없음' and str(v).strip()}
                            option_count = len(valid_options)
                            
                            if option_count > 0:
                                # 제품의 프라이스북단가를 그대로 각 옵션의 가격으로 사용 (옵션 개수로 나누지 않음)
                                # 예: P58100000의 프라이스북단가가 3717이면, 해당 제품의 모든 옵션 가격이 3717
                                price_per_option = float(price_book_price)
                                
                                # 각 옵션의 가격 저장 (최신 제품의 가격 우선 사용)
                                for category_num_str, option_name in valid_options.items():
                                    try:
                                        category_number = int(category_num_str)
                                        key = f"{category_number}_{option_name}"
                                        
                                        # 같은 옵션이 여러 제품에 있으면 최신 제품의 가격 사용
                                        # (이미 ORDER BY updated_at DESC로 정렬되어 있으므로 처음 만난 것이 최신)
                                        # 특정 제품코드(P58100000 등)의 가격을 우선적으로 사용하도록 처리
                                        priority_product_codes = ['P58100000', 'P58100002']  # 우선 적용할 제품코드 목록
                                        is_priority_product = product_code in priority_product_codes
                                        
                                        if key not in option_price_map:
                                            # 처음 만난 옵션이므로 바로 저장 (이미 최신 순으로 정렬되어 있음)
                                            option_price_map[key] = price_per_option
                                            option_updated_map[key] = updated_at
                                            option_product_map[key] = product_code
                                        else:
                                            # 이미 있는 경우, updated_at을 비교하여 더 최신이면 업데이트
                                            existing_updated = option_updated_map.get(key)
                                            existing_product = option_product_map.get(key, 'N/A')
                                            existing_price = option_price_map.get(key, 0)
                                            
                                            # 우선 제품코드인 경우 무조건 업데이트
                                            existing_is_priority = existing_product in priority_product_codes
                                            if is_priority_product and not existing_is_priority:
                                                # 새 제품이 우선 제품이고 기존 제품이 우선 제품이 아니면 무조건 업데이트
                                                should_update = True

                                            elif not is_priority_product and existing_is_priority:
                                                # 새 제품이 우선 제품이 아니고 기존 제품이 우선 제품이면 업데이트 안 함
                                                should_update = False
                                            else:
                                                # 둘 다 우선 제품이거나 둘 다 우선 제품이 아닌 경우 updated_at 비교
                                                # updated_at 비교 (문자열 또는 datetime)
                                                should_update = False
                                                if updated_at:
                                                    if existing_updated is None:
                                                        # 기존 것이 NULL이고 새로운 것이 있으면 업데이트
                                                        should_update = True
                                                    else:
                                                        try:
                                                            # 문자열 비교 (ISO 형식: 'YYYY-MM-DD HH:MM:SS')
                                                            if isinstance(updated_at, str) and isinstance(existing_updated, str):
                                                                # SQLite datetime 문자열 비교 (ISO 8601 형식)
                                                                should_update = updated_at > existing_updated
                                                            # datetime 비교
                                                            elif hasattr(updated_at, '__gt__') and hasattr(existing_updated, '__gt__'):
                                                                should_update = updated_at > existing_updated
                                                            # 타입이 다른 경우 문자열로 변환하여 비교
                                                            else:
                                                                str_updated = str(updated_at) if updated_at else ''
                                                                str_existing = str(existing_updated) if existing_updated else ''
                                                                should_update = str_updated > str_existing
                                                        except Exception as e:
                                                            should_update = False
                                            
                                            if should_update:
                                                option_price_map[key] = price_per_option
                                                option_updated_map[key] = updated_at
                                                option_product_map[key] = product_code
                                    except (ValueError, TypeError):
                                        continue
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        continue
            except (KeyError, IndexError, TypeError, AttributeError):
                continue
        
        # 가격 계산 (이미 최신 제품의 가격으로 저장되어 있음)
        calculated_prices = {}
        for key in option_price_map:
            calculated_prices[key] = round(option_price_map[key], 2)
        
        for debug_product_code in []:  # 디버깅 비활성화
            cursor.execute('''
                SELECT product_code, price_book_price, updated_at
                FROM export_quotation_new_products
                WHERE product_code = ?
            ''', (debug_product_code,))
            debug_product = cursor.fetchone()
            if debug_product:
                product_price = debug_product[1] if len(debug_product) > 1 else 'N/A'
                product_updated = debug_product[2] if len(debug_product) > 2 else 'N/A'
                
                # 해당 제품의 옵션 조회
                cursor.execute('''
                    SELECT selected_options
                    FROM selected_combinations
                    WHERE combination_code = ? OR set_code = ?
                    LIMIT 1
                ''', (debug_product_code, debug_product_code))
                debug_option_row = cursor.fetchone()
                if debug_option_row:
                    import json
                    try:
                        if hasattr(debug_option_row, 'keys') and callable(getattr(debug_option_row, 'keys', None)):
                            selected_options_str = debug_option_row['selected_options'] if 'selected_options' in debug_option_row.keys() else (debug_option_row[0] if len(debug_option_row) > 0 else None)
                        else:
                            selected_options_str = debug_option_row[0] if len(debug_option_row) > 0 else None
                        
                        if selected_options_str:
                            selected_options = json.loads(selected_options_str) if selected_options_str else {}
                            valid_options = {k: v for k, v in selected_options.items() if v and v != '선택없음' and str(v).strip()}
                            option_count = len(valid_options)
                            if option_count > 0 and product_price != 'N/A':
                                price_per_option = float(product_price)  # 옵션 개수로 나누지 않고 프라이스북단가를 그대로 사용
                                for cat_num, opt_name in list(valid_options.items())[:5]:  # 처음 5개 출력
                                    key = f"{cat_num}_{opt_name}"
                                    calculated_price = calculated_prices.get(key, 'N/A')
                                    source_product = option_product_map.get(key, 'N/A')
                                    source_updated = option_updated_map.get(key, 'N/A')
                    except Exception as e:
                        pass
                else:
                    pass
        
        
        # 모든 옵션 가져오기 (코드가격관리에서 표시할 모든 옵션)
        categories = system.get_all_categories()
        prices = {}
        
        for category in categories:
            category_num = category['category_number']
            category_id = category['id']
            
            options = system.get_category_options(category_id)
            
            for option in options:
                try:
                    option_name = option.get('item_name') or option.get('name', '')
                    if not option_name:
                        continue
                    
                    key = f"{category_num}_{option_name}"
                    
                    # 수출견적서(스태프)에서 계산된 가격이 있으면 우선 사용
                    # 없으면 option_base_prices의 가격 사용
                    final_price = None
                    updated_at = None
                    
                    if key in calculated_prices:
                        # 수출견적서(스태프)에서 계산된 가격 사용 (프라이스북단가 기반)
                        final_price = calculated_prices[key]
                        updated_at = None
                    else:
                        # option_base_prices에서 가격 조회
                        cursor.execute('''
                            SELECT price, updated_at
                            FROM option_base_prices
                            WHERE category_number = ? AND option_name = ?
                            LIMIT 1
                        ''', (category_num, option_name))
                        
                        base_price_row = cursor.fetchone()
                        if base_price_row:
                            try:
                                if hasattr(base_price_row, 'keys') and callable(getattr(base_price_row, 'keys', None)):
                                    final_price = base_price_row['price'] if 'price' in base_price_row.keys() else (base_price_row[0] if len(base_price_row) > 0 else None)
                                    updated_at = base_price_row['updated_at'] if 'updated_at' in base_price_row.keys() else (base_price_row[1] if len(base_price_row) > 1 else None)
                                else:
                                    final_price = base_price_row[0] if len(base_price_row) > 0 else None
                                    updated_at = base_price_row[1] if len(base_price_row) > 1 else None
                            except (KeyError, IndexError, TypeError, AttributeError):
                                pass
                    
                    prices[key] = {
                        'category_number': category_num,
                        'option_name': option_name,
                        'price': final_price,
                        'updated_at': updated_at
                    }
                except (KeyError, TypeError, AttributeError):
                    continue
        
        # 12번 카테고리(기타)의 옵션들을 option_base_prices에서 직접 가져오기
        try:
            cursor.execute('''
                SELECT option_name, price, updated_at
                FROM option_base_prices
                WHERE category_number = 12
                ORDER BY option_name
            ''')
            
            base_price_rows = cursor.fetchall()
            for row in base_price_rows:
                try:
                    if hasattr(row, 'keys') and callable(getattr(row, 'keys', None)):
                        option_name = row['option_name'] if 'option_name' in row.keys() else (row[0] if len(row) > 0 else None)
                        price = row['price'] if 'price' in row.keys() else (row[1] if len(row) > 1 else None)
                        updated_at = row['updated_at'] if 'updated_at' in row.keys() else (row[2] if len(row) > 2 else None)
                    else:
                        option_name = row[0] if len(row) > 0 else None
                        price = row[1] if len(row) > 1 else None
                        updated_at = row[2] if len(row) > 2 else None
                    
                    if option_name:
                        key = f"12_{option_name}"
                        # 이미 있는 경우 가격이 더 최신이면 업데이트
                        if key not in prices or (price is not None and (prices[key]['price'] is None or prices[key]['updated_at'] is None or (updated_at and updated_at > prices[key]['updated_at']))):
                            prices[key] = {
                                'category_number': 12,
                                'option_name': option_name,
                                'price': price,
                                'updated_at': updated_at
                            }
                except Exception as e:
                    continue
        except Exception as e:
            import traceback
            print(traceback.format_exc())
        
        return jsonify({
            "success": True,
            "prices": prices
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/option/base_price/excel/download', methods=['GET'])
def api_download_option_base_prices_excel():
    """카테고리별 옵션 가격 엑셀 다운로드 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # option_base_prices 테이블이 없으면 생성
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
        
        # 모든 카테고리와 옵션 조회 (system 메서드 사용)
        categories = system.get_all_categories()
        all_options = []
        
        # 가격 정보를 미리 조회하여 맵으로 저장
        cursor.execute('''
            SELECT category_number, option_name, price
            FROM option_base_prices
        ''')
        price_map = {}
        for price_row in cursor.fetchall():
            try:
                if hasattr(price_row, 'keys') and callable(getattr(price_row, 'keys', None)):
                    cat_num = price_row['category_number'] if 'category_number' in price_row.keys() else price_row[0]
                    opt_name = price_row['option_name'] if 'option_name' in price_row.keys() else price_row[1]
                    price = price_row['price'] if 'price' in price_row.keys() else price_row[2]
                else:
                    cat_num = price_row[0] if len(price_row) > 0 else None
                    opt_name = price_row[1] if len(price_row) > 1 else None
                    price = price_row[2] if len(price_row) > 2 else 0
                
                if cat_num is not None and opt_name is not None:
                    key = f"{cat_num}_{opt_name}"
                    price_map[key] = price if price is not None else 0
            except (KeyError, IndexError, TypeError, AttributeError) as e:
                continue
        
        # 각 카테고리의 옵션들을 순회하면서 가격 정보와 함께 수집
        for category in categories:
            try:
                # 안전하게 카테고리 정보 추출
                if isinstance(category, dict):
                    category_num = category.get('category_number')
                    category_id = category.get('id')
                else:
                    # sqlite3.Row 객체인 경우
                    category_num = category['category_number'] if 'category_number' in category.keys() else None
                    category_id = category['id'] if 'id' in category.keys() else None
                
                if not category_num or not category_id:
                    print(f"Warning: Invalid category data: {category}")
                    continue
                
                # 해당 카테고리의 옵션들 가져오기
                options = system.get_category_options(category_id)
                
                for option in options:
                    try:
                        # 안전하게 옵션 정보 추출
                        if isinstance(option, dict):
                            option_name = option.get('item_name') or option.get('name', '')
                            option_code = option.get('item_code') or option.get('code', '')
                        else:
                            # sqlite3.Row 객체인 경우
                            option_name = option.get('item_name', '') if hasattr(option, 'get') else (option['item_name'] if 'item_name' in option.keys() else '')
                            option_code = option.get('item_code', '') if hasattr(option, 'get') else (option['item_code'] if 'item_code' in option.keys() else '')
                        
                        if not option_name:
                            continue
                        
                        # 가격 정보 조회
                        price_key = f"{category_num}_{option_name}"
                        price = price_map.get(price_key, 0)
                        
                        all_options.append({
                            'category_number': category_num,
                            'option_name': option_name,
                            'code': option_code if option_code else '',
                            'price': price
                        })
                    except (KeyError, TypeError, AttributeError) as e:
                        print(f"Error processing option: {option}, {e}")
                        continue
            except Exception as e:
                print(f"Error processing category: {category}, {e}")
                continue
        
        # 카테고리 번호와 옵션명으로 정렬
        all_options.sort(key=lambda x: (x['category_number'], x['option_name']))
        
        # 엑셀 파일 생성
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from datetime import datetime
        import tempfile
        import os
        from io import BytesIO
        
        wb = Workbook()
        ws = wb.active
        ws.title = "카테고리별 옵션 가격"
        
        # 헤더 스타일
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center")
        
        # 헤더 작성
        ws['A1'] = '카테고리'
        ws['B1'] = '옵션명'
        ws['C1'] = 'SAP 코드'
        ws['D1'] = '가격 (EUR)'
        
        # 헤더 스타일 적용
        for col in ['A1', 'B1', 'C1', 'D1']:
            cell = ws[col]
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
        
        # 데이터 작성
        row_num = 2
        for option in all_options:
            cat_num = option['category_number']
            opt_name = option['option_name']
            code = option['code']
            price = option['price']
            
            ws[f'A{row_num}'] = int(cat_num) if isinstance(cat_num, (int, float)) or (isinstance(cat_num, str) and cat_num.isdigit()) else cat_num
            ws[f'B{row_num}'] = opt_name
            ws[f'C{row_num}'] = code
            ws[f'D{row_num}'] = float(price) if price is not None else 0.0
            row_num += 1
        
        # 열 너비 조정
        ws.column_dimensions['A'].width = 12
        ws.column_dimensions['B'].width = 30
        ws.column_dimensions['C'].width = 15
        ws.column_dimensions['D'].width = 15
        
        # 파일을 메모리에 저장
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        
        # 파일명 생성
        date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"카테고리별_옵션_가격_{date_str}.xlsx"
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        import traceback
        print(f"Error in api_download_option_base_prices_excel: {e}")
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/option/base_price/excel/upload', methods=['POST'])
def api_upload_option_base_prices_excel():
    """카테고리별 옵션 가격 엑셀 업로드 API"""
    try:
        if 'file' not in request.files:
            return jsonify({
                "success": False,
                "error": "파일이 없습니다."
            }), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({
                "success": False,
                "error": "파일이 선택되지 않았습니다."
            }), 400
        
        if not file.filename.endswith(('.xlsx', '.xls')):
            return jsonify({
                "success": False,
                "error": "엑셀 파일(.xlsx, .xls)만 업로드 가능합니다."
            }), 400
        
        # 엑셀 파일 읽기
        from openpyxl import load_workbook
        from io import BytesIO
        
        file_data = file.read()
        file_stream = BytesIO(file_data)
        wb = load_workbook(file_stream, data_only=True)
        
        # 첫 번째 시트 사용
        if len(wb.sheetnames) > 0:
            ws = wb[wb.sheetnames[0]]
        else:
            ws = wb.active
        
        # 헤더 행 찾기
        header_row = None
        max_row = ws.max_row if ws.max_row else 100
        
        for row_idx in range(1, min(10, max_row + 1)):
            row_values = []
            for col_idx in range(1, 5):
                cell_value = ws.cell(row=row_idx, column=col_idx).value
                if cell_value:
                    row_values.append(str(cell_value).strip().lower())
            
            # 헤더 확인 (카테고리, 옵션명, SAP 코드, 가격)
            if any('카테고리' in v or 'category' in v for v in row_values) and \
               any('옵션' in v or 'option' in v for v in row_values) and \
               any('가격' in v or 'price' in v for v in row_values):
                header_row = row_idx
                break
        
        if header_row is None:
            header_row = 1
        
        # 데이터 읽기
        prices = []
        errors = []
        
        for row_idx in range(header_row + 1, max_row + 1):
            category_cell = ws.cell(row=row_idx, column=1).value
            option_name_cell = ws.cell(row=row_idx, column=2).value
            price_cell = ws.cell(row=row_idx, column=4).value  # D열이 가격
            
            if not category_cell or not option_name_cell:
                continue
            
            try:
                category_number = int(float(category_cell)) if category_cell else None
                option_name = str(option_name_cell).strip() if option_name_cell else None
                
                if price_cell is None or price_cell == '':
                    price = 0.0
                else:
                    price = float(price_cell)
                    if price < 0:
                        errors.append(f"행 {row_idx}: 가격은 0 이상이어야 합니다.")
                        continue
                    price = round(price, 2)
                
                if category_number and option_name:
                    prices.append({
                        'category_number': category_number,
                        'option_name': option_name,
                        'price': price
                    })
            except (ValueError, TypeError) as e:
                errors.append(f"행 {row_idx}: 데이터 형식 오류 - {str(e)}")
                continue
        
        if not prices:
            return jsonify({
                "success": False,
                "error": "업로드할 유효한 데이터가 없습니다."
            }), 400
        
        # 배치 업데이트 API 호출
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # option_base_prices 테이블이 없으면 생성
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
        
        success_count = 0
        error_count = 0
        error_messages = []
        
        for price_data in prices:
            try:
                category_number = price_data['category_number']
                option_name = price_data['option_name']
                price = price_data['price']
                
                # 기존 레코드 확인
                cursor.execute('''
                    SELECT id FROM option_base_prices
                    WHERE category_number = ? AND option_name = ?
                ''', (category_number, option_name))
                
                existing = cursor.fetchone()
                
                if existing:
                    # 업데이트
                    cursor.execute('''
                        UPDATE option_base_prices
                        SET price = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE category_number = ? AND option_name = ?
                    ''', (price, category_number, option_name))
                else:
                    # 삽입
                    cursor.execute('''
                        INSERT INTO option_base_prices (category_number, option_name, price, updated_at)
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ''', (category_number, option_name, price))
                
                success_count += 1
            except Exception as e:
                error_count += 1
                error_messages.append(f"{category_number}-{option_name}: {str(e)}")
        
        system.connection.commit()
        
        result = {
            "success": True,
            "success_count": success_count,
            "error_count": error_count
        }
        
        if error_messages:
            result["errors"] = error_messages[:10]  # 최대 10개만 반환
        
        return jsonify(result)
    except Exception as e:
        import traceback
        print(f"Error in api_upload_option_base_prices_excel: {e}")
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/quote_template')
def quote_template_edit():
    """견적서 양식 편집 페이지"""
    return render_template('quote_template_edit.html')

# 계약서/팩킹리스트/인보이스 엑셀 템플릿 업로드 페이지 (관리 메뉴)
DOC_TEMPLATE_TYPES = {
    'contract': {'title': '계약서(엑셀)', 'filename': 'contract_template.xlsx'},
    'packing_list': {'title': '팩킹리스트(엑셀)', 'filename': 'packing_list_template.xlsx'},
    'invoice': {'title': '인보이스(엑셀)', 'filename': 'invoice_template.xlsx'},
}
@app.route('/doc_template/<string:doc_type>', endpoint='doc_template_upload')
def doc_template_upload(doc_type):
    """계약서/팩킹리스트/인보이스 엑셀 템플릿 업로드 페이지"""
    if doc_type not in DOC_TEMPLATE_TYPES:
        from flask import abort
        abort(404)
    info = DOC_TEMPLATE_TYPES[doc_type]
    return render_template('doc_template_upload.html', doc_type=doc_type, title=info['title'])

# /excel/fill 를 /excel 보다 먼저 등록 (더 구체적인 경로 우선)
@app.route('/api/doc_template/<string:doc_type>/excel/fill', methods=['POST'])
def api_doc_template_excel_fill(doc_type):
    """계약서/팩킹리스트/인보이스 엑셀 템플릿에 제품 정보를 채워서 다운로드 (수출견적서 스태프 화면 선택 데이터 반영)"""
    if doc_type not in DOC_TEMPLATE_TYPES:
        from flask import abort
        abort(404)
    templates_dir = os.path.join(os.path.dirname(__file__), 'quote_templates')
    filename = DOC_TEMPLATE_TYPES[doc_type]['filename']
    filepath = os.path.join(templates_dir, filename)
    if not os.path.isfile(filepath):
        return jsonify({"success": False, "error": "등록된 엑셀 템플릿이 없습니다. 관리 메뉴에서 먼저 업로드해주세요."}), 404
    try:
        data = request.get_json() or {}
        products = data.get('products') or []
        if not isinstance(products, list):
            products = []
        if not products:
            return jsonify({"success": False, "error": "제품 정보가 없습니다. 제품을 추가한 후 다시 시도해주세요."}), 400
        from openpyxl import load_workbook
        from io import BytesIO
        wb = load_workbook(filepath, data_only=False, keep_vba=False)
        ws = wb.active
        if ws is None:
            return jsonify({"success": False, "error": "엑셀 템플릿에 시트가 없습니다."}), 400
        # 서류별 셀 매핑: 시작행, SAP코드열, 코드명열, 수량열, 프라이스북 단가열, 프라이스북 합계열 (None이면 미기입)
        if doc_type == 'invoice':
            # 인보이스: B17=넘버링(1,2,3...), C17=SAP코드, D17=코드명, G17=수량, H17=단가, I17=합계 (이후 18, 19, ...)
            start_row = 17
            col_no, col_code, col_name, col_qty, col_unit, col_total = 2, 3, 4, 7, 8, 9   # B, C, D, G, H, I
        elif doc_type == 'packing_list':
            # 팩킹리스트: B17=넘버링(1,2,3...), C17=SAP코드, D17=코드명, G17=수량 (단가/합계 없음)
            start_row = 17
            col_no, col_code, col_name, col_qty = 2, 3, 4, 7   # B, C, D, G
            col_unit, col_total = None, None
        else:
            # 계약서: A21=넘버링(1,2,3...), B21=SAP코드, C21=코드명, D21=수량, E21=단가, F21=합계
            start_row = 21
            col_no, col_code, col_name, col_qty, col_unit, col_total = 1, 2, 3, 4, 5, 6   # A, B, C, D, E, F
        data_cols = {col_code, col_name, col_qty}
        if col_no is not None:
            data_cols.add(col_no)
        if col_unit is not None:
            data_cols.add(col_unit)
        if col_total is not None:
            data_cols.add(col_total)
        end_row = start_row + len(products) - 1
        # 쓰기 영역과 겹치는 병합 셀 해제 (MergedCell은 쓰기 불가이므로)
        try:
            merged_ranges = getattr(ws, 'merged_cells', None)
            if merged_ranges is not None:
                ranges_list = getattr(merged_ranges, 'ranges', None)
                if ranges_list is not None:
                    try:
                        ranges_copy = list(ranges_list)
                    except (IndexError, TypeError):
                        ranges_copy = []
                    for mr in ranges_copy:
                        try:
                            min_col = getattr(mr, 'min_col', None) or getattr(mr, 'min_column', None)
                            max_col = getattr(mr, 'max_col', None) or getattr(mr, 'max_column', None)
                            if min_col is None or max_col is None:
                                continue
                            if getattr(mr, 'min_row', 0) <= end_row and getattr(mr, 'max_row', 0) >= start_row and (
                                min_col <= max(data_cols) and max_col >= min(data_cols)
                            ):
                                ws.unmerge_cells(str(mr))
                        except (IndexError, AttributeError, TypeError):
                            continue
        except (IndexError, AttributeError, TypeError):
            pass
        # unmerge 후에도 시트에 MergedCell 인스턴스가 남을 수 있음 → 쓰기 영역의 MergedCell 제거
        from openpyxl.cell.cell import MergedCell
        try:
            cells = getattr(ws, '_cells', {})
            if cells is not None:
                for r in range(start_row, end_row + 1):
                    for c in data_cols:
                        key = (r, c)
                        try:
                            if key in cells and isinstance(cells.get(key), MergedCell):
                                del cells[key]
                        except (IndexError, TypeError, KeyError):
                            pass
        except (IndexError, AttributeError, TypeError):
            pass
        for i, p in enumerate(products):
            if not isinstance(p, dict):
                p = {}
            row = start_row + i
            code = p.get('code') or p.get('product_code') or ''
            name = p.get('name') or p.get('product_name') or ''
            qty = p.get('quantity')
            if qty is None:
                qty = 1
            try:
                qty = int(float(qty))
            except (TypeError, ValueError):
                qty = 1
            # 계약서·인보이스: 최종 판매가 단가/합계 사용, 팩킹리스트: 단가·합계 없음
            unit_price = p.get('priceBookPrice') or p.get('price_book_price') or 0
            total_price = p.get('priceBookTotal')
            if doc_type in ('contract', 'invoice'):
                fs_unit = p.get('finalSalesPrice') or p.get('final_sales_price')
                fs_total = p.get('finalSalesPriceTotal') or p.get('final_sales_price_total')
                if fs_unit is not None or fs_total is not None:
                    try:
                        unit_price = float(fs_unit) if fs_unit is not None else unit_price
                    except (TypeError, ValueError):
                        pass
                    try:
                        total_price = float(fs_total) if fs_total is not None else (unit_price * qty)
                    except (TypeError, ValueError):
                        total_price = unit_price * qty
            if total_price is None:
                total_price = unit_price * qty
            try:
                unit_price = float(unit_price)
            except (TypeError, ValueError):
                unit_price = 0
            try:
                total_price = float(total_price)
            except (TypeError, ValueError):
                total_price = unit_price * qty
            if col_no is not None:
                ws.cell(row=row, column=col_no, value=i + 1)
            ws.cell(row=row, column=col_code, value=code)
            ws.cell(row=row, column=col_name, value=name)
            ws.cell(row=row, column=col_qty, value=qty)
            if col_unit is not None:
                ws.cell(row=row, column=col_unit, value=unit_price)
            if col_total is not None:
                ws.cell(row=row, column=col_total, value=total_price)
        buf = BytesIO()
        # 저장 시 스타일 인덱스(alignmentId 등)가 깨진 템플릿 대응: IndexError 나면 _alignments 패딩 후 재시도
        from openpyxl.styles import Alignment
        last_err = None
        for attempt in range(5):
            try:
                buf.seek(0)
                buf.truncate(0)
                wb.save(buf)
                last_err = None
                break
            except IndexError as e:
                last_err = e
                al = getattr(wb, '_alignments', None)
                if al is not None and isinstance(al, list):
                    for _ in range(64):
                        al.append(Alignment())
                else:
                    raise
        if last_err is not None:
            raise last_err
        buf.seek(0)
        from flask import send_file
        out_name = (doc_type + '_filled.xlsx') if doc_type else 'filled.xlsx'
        return send_file(buf, as_attachment=True, download_name=out_name, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/doc_template/<string:doc_type>/excel', methods=['GET', 'POST'])
def api_doc_template_excel(doc_type):
    """계약서/팩킹리스트/인보이스 엑셀 템플릿 다운로드(GET) / 업로드(POST)"""
    if doc_type not in DOC_TEMPLATE_TYPES:
        from flask import abort
        abort(404)
    templates_dir = os.path.join(os.path.dirname(__file__), 'quote_templates')
    filename = DOC_TEMPLATE_TYPES[doc_type]['filename']
    filepath = os.path.join(templates_dir, filename)
    title = DOC_TEMPLATE_TYPES[doc_type]['title']

    if request.method == 'GET':
        try:
            if not os.path.isfile(filepath):
                return jsonify({"success": False, "error": "등록된 엑셀 파일이 없습니다."}), 404
            from flask import send_file
            return send_file(filepath, as_attachment=True, download_name=filename)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    # POST: 업로드 (기존 파일 잠금 시 Permission denied 방지: 임시 파일에 저장 후 교체, 재시도)
    try:
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "파일이 없습니다."}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "error": "파일이 선택되지 않았습니다."}), 400
        if not (file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
            return jsonify({"success": False, "error": "엑셀 파일(.xlsx, .xls)만 업로드 가능합니다."}), 400
        os.makedirs(templates_dir, exist_ok=True)
        import tempfile
        import time
        import shutil
        fd, tmppath = tempfile.mkstemp(suffix='.xlsx', dir=templates_dir)
        try:
            os.close(fd)
            file.save(tmppath)
            replaced = False
            for attempt in range(3):
                try:
                    if os.path.isfile(filepath):
                        try:
                            os.remove(filepath)
                        except PermissionError:
                            pass
                    os.replace(tmppath, filepath)
                    replaced = True
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(0.4)
            if not replaced:
                try:
                    if os.path.isfile(filepath):
                        try:
                            os.remove(filepath)
                        except PermissionError:
                            pass
                    shutil.move(tmppath, filepath)
                    replaced = True
                except Exception:
                    pass
                if not replaced:
                    try:
                        os.remove(tmppath)
                    except Exception:
                        pass
                    return jsonify({
                        "success": False,
                        "error": "대상 파일이 다른 프로그램(예: Excel)에서 열려 있을 수 있습니다. 파일을 닫은 후 다시 업로드해 주세요."
                    }), 409
        except Exception as e:
            if os.path.isfile(tmppath):
                try:
                    os.remove(tmppath)
                except Exception:
                    pass
            raise e
        return jsonify({"success": True, "message": f"{title}이(가) 업로드되었습니다."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/quote_template/excel', methods=['GET'])
def api_download_quote_template_excel():
    """견적서 양식 엑셀 다운로드 API"""
    print(f"견적서 양식 엑셀 다운로드 API 호출됨")
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 양식 정보 가져오기
        cursor.execute('''
            SELECT company_name, company_address, company_phone, company_email,
                   company_logo, quote_title, footer_note, terms_conditions
            FROM quote_template
            ORDER BY id DESC
            LIMIT 1
        ''')
        
        template_row = cursor.fetchone()
        template = None
        if template_row:
            if hasattr(template_row, 'keys') and callable(getattr(template_row, 'keys', None)):
                template = {
                    'company_name': template_row['company_name'] or '',
                    'company_address': template_row['company_address'] or '',
                    'company_phone': template_row['company_phone'] or '',
                    'company_email': template_row['company_email'] or '',
                    'company_logo': template_row['company_logo'] or '',
                    'quote_title': template_row['quote_title'] or '견적서',
                    'footer_note': template_row['footer_note'] or '',
                    'terms_conditions': template_row['terms_conditions'] or ''
                }
            else:
                template = {
                    'company_name': template_row[0] or '',
                    'company_address': template_row[1] or '',
                    'company_phone': template_row[2] or '',
                    'company_email': template_row[3] or '',
                    'company_logo': template_row[4] or '',
                    'quote_title': template_row[5] or '견적서',
                    'footer_note': template_row[6] or '',
                    'terms_conditions': template_row[7] or ''
                }
        
        # 엑셀 파일 생성
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from io import BytesIO
        from datetime import datetime
        
        wb = Workbook()
        ws = wb.active
        ws.title = "견적서 양식"
        
        # 스타일 설정
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center")
        info_label_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
        title_font = Font(bold=True, size=16, color="800020")
        
        row = 1
        
        # 회사 정보 섹션
        if template and template.get('company_name'):
            ws[f'A{row}'] = template['company_name']
            ws.merge_cells(f'A{row}:D{row}')
            company_cell = ws[f'A{row}']
            company_cell.font = Font(bold=True, size=14)
            company_cell.alignment = Alignment(horizontal="center", vertical="center")
            row += 1
            
            if template.get('company_address'):
                ws[f'A{row}'] = template['company_address']
                ws.merge_cells(f'A{row}:D{row}')
                ws[f'A{row}'].alignment = Alignment(horizontal="center", vertical="center")
                row += 1
            
            contact_info = []
            if template.get('company_phone'):
                contact_info.append(f"전화: {template['company_phone']}")
            if template.get('company_email'):
                contact_info.append(f"이메일: {template['company_email']}")
            if contact_info:
                ws[f'A{row}'] = ' | '.join(contact_info)
                ws.merge_cells(f'A{row}:D{row}')
                ws[f'A{row}'].alignment = Alignment(horizontal="center", vertical="center")
                row += 1
            
            row += 1
        
        # 견적서 제목
        quote_title = template['quote_title'] if template and template.get('quote_title') else '견적서'
        ws[f'A{row}'] = quote_title
        ws.merge_cells(f'A{row}:D{row}')
        title_cell = ws[f'A{row}']
        title_cell.font = title_font
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        
        row += 2
        
        # 견적서 정보 섹션 (예시)
        ws[f'A{row}'] = '세트코드'
        ws[f'A{row}'].fill = info_label_fill
        ws[f'A{row}'].font = Font(bold=True)
        ws[f'B{row}'] = 'P51000000 (예시)'
        ws[f'C{row}'] = '견적일자'
        ws[f'C{row}'].fill = info_label_fill
        ws[f'C{row}'].font = Font(bold=True)
        ws[f'D{row}'] = datetime.now().strftime('%Y-%m-%d')
        row += 1
        
        ws[f'A{row}'] = '통화'
        ws[f'A{row}'].fill = info_label_fill
        ws[f'A{row}'].font = Font(bold=True)
        ws[f'B{row}'] = 'EUR (예시)'
        ws[f'C{row}'] = '환율 (1 EUR)'
        ws[f'C{row}'].fill = info_label_fill
        ws[f'C{row}'].font = Font(bold=True)
        ws[f'D{row}'] = '1.16 USD (예시)'
        row += 1
        
        ws[f'A{row}'] = '할인율'
        ws[f'A{row}'].fill = info_label_fill
        ws[f'A{row}'].font = Font(bold=True)
        ws[f'B{row}'] = '0% (예시)'
        row += 2
        
        # 옵션별 가격 테이블 헤더
        ws[f'A{row}'] = '카테고리'
        ws[f'B{row}'] = '옵션명'
        ws[f'C{row}'] = '원 가격 (EUR)'
        ws[f'D{row}'] = '할인 가격 (EUR)'
        
        for cell in ws[row]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
        
        row += 1
        
        # 안내 메시지
        ws[f'A{row}'] = '* 견적서 다운로드 시 실제 옵션 데이터가 자동으로 채워집니다.'
        ws.merge_cells(f'A{row}:D{row}')
        ws[f'A{row}'].font = Font(italic=True, size=9)
        ws[f'A{row}'].alignment = Alignment(horizontal="center", vertical="center")
        ws[f'A{row}'].fill = PatternFill(start_color="F9F9F9", end_color="F9F9F9", fill_type="solid")
        
        row += 2
        
        # 각주/메모
        if template and template.get('footer_note'):
            ws[f'A{row}'] = template['footer_note']
            ws.merge_cells(f'A{row}:D{row}')
            ws[f'A{row}'].font = Font(italic=True, size=9)
            ws[f'A{row}'].alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            ws[f'A{row}'].fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
            row += 1
        
        # 조건/약관
        if template and template.get('terms_conditions'):
            row += 1
            ws[f'A{row}'] = '조건/약관'
            ws.merge_cells(f'A{row}:D{row}')
            ws[f'A{row}'].fill = info_label_fill
            ws[f'A{row}'].font = Font(bold=True)
            row += 1
            ws[f'A{row}'] = template['terms_conditions']
            ws.merge_cells(f'A{row}:D{row}')
            ws[f'A{row}'].font = Font(size=9)
            ws[f'A{row}'].alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        
        # 컬럼 너비 조정
        ws.column_dimensions['A'].width = 30
        ws.column_dimensions['B'].width = 30
        ws.column_dimensions['C'].width = 20
        ws.column_dimensions['D'].width = 20
        
        # 엑셀 파일을 메모리에 저장
        excel_buffer = BytesIO()
        wb.save(excel_buffer)
        excel_buffer.seek(0)
        
        from flask import send_file
        return send_file(
            excel_buffer,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='견적서_양식.xlsx'
        )
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_download_quote_template_excel: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/quote_template/excel', methods=['POST'])
def api_upload_quote_template_excel():
    """견적서 양식 엑셀 업로드 API"""
    try:
        if 'file' not in request.files:
            return jsonify({
                "success": False,
                "error": "파일이 없습니다."
            }), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({
                "success": False,
                "error": "파일이 선택되지 않았습니다."
            }), 400
        
        if not file.filename.endswith('.xlsx'):
            return jsonify({
                "success": False,
                "error": "엑셀 파일(.xlsx)만 업로드 가능합니다."
            }), 400
        
        # 엑셀 파일 읽기
        from openpyxl import load_workbook
        from io import BytesIO
        
        file_data = file.read()
        file_stream = BytesIO(file_data)
        wb = load_workbook(file_stream, data_only=True)
        
        # 첫 번째 시트 사용 (활성 시트가 아닐 수 있음)
        if len(wb.sheetnames) > 0:
            ws = wb[wb.sheetnames[0]]
        else:
            ws = wb.active
        
        # 업로드한 엑셀 파일을 서버에 템플릿으로 저장
        templates_dir = os.path.join(os.path.dirname(__file__), 'quote_templates')
        if not os.path.exists(templates_dir):
            os.makedirs(templates_dir)
        
        template_filepath = os.path.join(templates_dir, 'quote_template.xlsx')
        with open(template_filepath, 'wb') as f:
            f.write(file_data)
        print(f"견적서 템플릿 파일 저장 완료: {template_filepath}")
        
        # 양식 정보 추출
        template = {
            'company_name': '',
            'company_address': '',
            'company_phone': '',
            'company_email': '',
            'company_logo': '',
            'quote_title': '견적서',
            'footer_note': '',
            'terms_conditions': ''
        }
        
        # 모든 셀을 순회하면서 양식 정보 추출
        max_row = ws.max_row if ws.max_row else 100
        max_col = ws.max_column if ws.max_column else 10
        import re
        
        print(f"엑셀 파일 읽기 시작: 시트명={ws.title}, 총 {max_row}행, {max_col}열")
        
        # 병합된 셀 정보 가져오기
        merged_cells = list(ws.merged_cells.ranges) if hasattr(ws, 'merged_cells') and ws.merged_cells else []
        
        # 예시 데이터 필터링 함수
        def is_example_or_skip(value):
            if not value:
                return True
            value_str = str(value).strip()
            # 예시 관련 키워드
            if any(keyword in value_str for keyword in ['(예시)', '예시', 'P51000000', 'EUR (예시)', 'USD (예시)', '1.16 USD', '0% (예시)']):
                return True
            # 견적서 정보 섹션 키워드
            if any(keyword in value_str for keyword in ['세트코드', '견적일자', '통화', '환율', '할인율']):
                return True
            # 옵션 테이블 헤더
            if any(keyword in value_str for keyword in ['카테고리', '옵션명', '원 가격', '할인 가격', '합계']):
                return True
            return False
        
        # 셀 안전하게 읽기 함수
        def get_cell_value(row_num, col_num=1):
            try:
                cell = ws.cell(row=row_num, column=col_num)
                return str(cell.value or '').strip() if cell.value else ''
            except Exception as e:
                print(f"셀 읽기 오류 (행 {row_num}, 열 {col_num}): {e}")
                return ''
        
        # 병합된 셀의 실제 값 가져오기
        def get_merged_cell_value(row_num, col_num=1):
            try:
                cell = ws.cell(row=row_num, column=col_num)
                # 병합된 셀인지 확인
                for merged_range in merged_cells:
                    if cell.coordinate in merged_range:
                        # 병합된 셀의 첫 번째 셀 값 사용
                        top_left_cell = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
                        return str(top_left_cell.value or '').strip() if top_left_cell.value else ''
                return str(cell.value or '').strip() if cell.value else ''
            except Exception as e:
                print(f"병합 셀 읽기 오류 (행 {row_num}, 열 {col_num}): {e}")
                return ''
        
        # 모든 셀을 순회하면서 양식 정보 추출 (더 유연한 방식)
        # 상단 부분 (1~30행)에서 회사 정보 및 제목 찾기
        for row in range(1, min(31, max_row + 1)):
            # A열부터 D열까지 확인 (병합된 셀 고려)
            for col in range(1, min(5, max_col + 1)):
                try:
                    value = get_merged_cell_value(row, col)
                    if not value or is_example_or_skip(value):
                        continue
                    
                    # 셀 스타일 정보
                    try:
                        cell = ws.cell(row=row, column=col)
                        font_size = cell.font.size if cell.font and cell.font.size else None
                        is_bold = cell.font.bold if cell.font and cell.font.bold else False
                    except:
                        font_size = None
                        is_bold = False
                    
                    # 회사명 찾기 (상단, 큰 글씨, 굵은 글씨, 또는 병합된 셀)
                    if not template['company_name'] and row <= 10:
                        is_merged = False
                        for merged_range in merged_cells:
                            try:
                                cell_check = ws.cell(row=row, column=col)
                                if cell_check.coordinate in merged_range:
                                    is_merged = True
                                    break
                            except:
                                pass
                        
                        # 큰 글씨(12pt 이상) 또는 굵은 글씨 또는 병합된 셀이면 회사명 후보
                        if (font_size and font_size >= 12) or is_bold or is_merged:
                            if len(value) > 2 and '견적서' not in value and '세트코드' not in value:
                                template['company_name'] = value
                                print(f"회사명 발견 (행 {row}, 열 {col}): {value}")
                                break
                    
                    # 주소 찾기 (회사명이 있고, 전화/이메일이 아닌 텍스트)
                    if template['company_name'] and not template['company_address'] and row <= 15:
                        if '전화:' not in value and '이메일:' not in value and '견적서' not in value:
                            if len(value) > 5 and not any(keyword in value for keyword in ['세트코드', '견적일자', '통화', '환율', '할인율']):
                                template['company_address'] = value
                                print(f"주소 발견 (행 {row}, 열 {col}): {value}")
                                break
                    
                    # 연락처 찾기
                    if '전화:' in value or 'Tel:' in value or 'Phone:' in value:
                        phone_match = re.search(r'(?:전화|Tel|Phone):\s*([^|,\n]+)', value, re.IGNORECASE)
                        if phone_match:
                            phone_value = phone_match.group(1).strip()
                            if not is_example_or_skip(phone_value) and len(phone_value) > 3:
                                template['company_phone'] = phone_value
                                print(f"전화번호 발견 (행 {row}, 열 {col}): {phone_value}")
                    
                    if '이메일:' in value or 'Email:' in value or '@' in value:
                        email_match = re.search(r'(?:이메일|Email):\s*([^|,\n]+)', value, re.IGNORECASE)
                        if not email_match and '@' in value:
                            # @ 기호가 있으면 이메일로 추정
                            email_match = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', value)
                        if email_match:
                            email_value = email_match.group(1).strip()
                            if not is_example_or_skip(email_value) and '@' in email_value:
                                template['company_email'] = email_value
                                print(f"이메일 발견 (행 {row}, 열 {col}): {email_value}")
                    
                    # 견적서 제목 찾기 (큰 글씨, '견적서' 포함)
                    if not template['quote_title'] or template['quote_title'] == '견적서':
                        if '견적서' in value or 'Quote' in value or 'QUOTATION' in value.upper():
                            if font_size and font_size >= 14:
                                template['quote_title'] = value
                                print(f"견적서 제목 발견 (행 {row}, 열 {col}): {value}")
                            elif not template['quote_title'] or template['quote_title'] == '견적서':
                                # 큰 글씨가 아니어도 '견적서'가 포함되어 있으면 사용
                                template['quote_title'] = value
                                print(f"견적서 제목 발견 (행 {row}, 열 {col}): {value}")
                except Exception as e:
                    print(f"셀 읽기 오류 (행 {row}, 열 {col}): {e}")
                    continue
        
        # 각주/조건 찾기 (하단에서 역순으로)
        skip_keywords = ['세트코드', '견적일자', '통화', '환율', '할인율', '카테고리', '옵션명', '원 가격', '할인 가격', '합계']
        
        footer_found = False
        for r in range(max_row, max(1, max_row - 25), -1):
            try:
                value = get_merged_cell_value(r, 1)
                
                if value and not is_example_or_skip(value):
                    # 견적서 정보 섹션이나 옵션 테이블 헤더는 건너뛰기
                    if any(keyword in value for keyword in skip_keywords):
                        continue
                    
                    # '조건' 또는 '약관'이 포함된 행 찾기
                    if ('조건' in value or '약관' in value) and not footer_found:
                        # 다음 행이 조건 내용
                        if r + 1 <= max_row:
                            terms = get_merged_cell_value(r + 1, 1)
                            if terms and not is_example_or_skip(terms):
                                template['terms_conditions'] = terms
                                print(f"조건/약관 발견 (행 {r+1}): {terms[:50]}...")
                                footer_found = True
                                continue
                    
                    # 작은 글씨(9pt 이하)나 회색 배경이면 각주
                    if not template['footer_note'] and len(value) > 5:
                        try:
                            cell = ws.cell(row=r, column=1)
                            if cell.font and cell.font.size and cell.font.size <= 10:
                                template['footer_note'] = value
                                print(f"각주 발견 (행 {r}): {value[:50]}...")
                            elif cell.fill:
                                # 회색 계열 배경이면 각주로 추정
                                fill_color = None
                                if hasattr(cell.fill, 'fgColor') and cell.fill.fgColor:
                                    if hasattr(cell.fill.fgColor, 'rgb'):
                                        fill_color = cell.fill.fgColor.rgb
                                    elif hasattr(cell.fill.fgColor, 'index'):
                                        fill_color = str(cell.fill.fgColor.index)
                                if fill_color and ('F2' in str(fill_color) or 'F9' in str(fill_color) or 'D9' in str(fill_color)):
                                    template['footer_note'] = value
                                    print(f"각주 발견 (회색 배경, 행 {r}): {value[:50]}...")
                        except Exception as e:
                            print(f"셀 스타일 확인 오류 (행 {r}): {e}")
            except Exception as e:
                print(f"하단 셀 읽기 오류 (행 {r}): {e}")
                continue
        
        print(f"최종 추출된 양식 정보: {template}")
        
        # 데이터베이스에 저장
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 기존 양식 확인
        cursor.execute('SELECT id FROM quote_template ORDER BY id DESC LIMIT 1')
        existing = cursor.fetchone()
        
        # 저장할 데이터 준비
        save_data = (
            template['company_name'] or '',
            template['company_address'] or '',
            template['company_phone'] or '',
            template['company_email'] or '',
            template['company_logo'] or '',
            template['quote_title'] or '견적서',
            template['footer_note'] or '',
            template['terms_conditions'] or ''
        )
        
        print(f"저장할 데이터: {save_data}")
        
        if existing:
            existing_id = existing[0] if isinstance(existing, tuple) else existing['id']
            print(f"기존 양식 업데이트: id={existing_id}")
            # 업데이트
            cursor.execute('''
                UPDATE quote_template
                SET company_name = ?, company_address = ?, company_phone = ?,
                    company_email = ?, company_logo = ?, quote_title = ?,
                    footer_note = ?, terms_conditions = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', save_data + (existing_id,))
        else:
            print("새 양식 생성")
            # 새로 생성
            cursor.execute('''
                INSERT INTO quote_template
                (company_name, company_address, company_phone, company_email,
                 company_logo, quote_title, footer_note, terms_conditions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', save_data)
        
        system.connection.commit()
        print("데이터베이스 저장 완료")
        
        # 저장 확인
        cursor.execute('''
            SELECT company_name, company_address, company_phone, company_email,
                   quote_title, footer_note, terms_conditions, updated_at
            FROM quote_template
            ORDER BY id DESC
            LIMIT 1
        ''')
        saved_row = cursor.fetchone()
        if saved_row:
            if hasattr(saved_row, 'keys'):
                print(f"저장 확인: company_name='{saved_row['company_name']}', quote_title='{saved_row['quote_title']}', updated_at={saved_row['updated_at']}")
            else:
                print(f"저장 확인: company_name='{saved_row[0]}', quote_title='{saved_row[4]}', updated_at={saved_row[7]}")
        
        return jsonify({
            "success": True,
            "message": "견적서 양식이 업로드되었습니다.",
            "template": template
        })
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_upload_quote_template_excel: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/quote_template', methods=['GET'])
def api_get_quote_template():
    """견적서 양식 정보 조회 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        cursor.execute('''
            SELECT id, company_name, company_address, company_phone, 
                   company_email, company_logo, quote_title, footer_note, terms_conditions,
                   created_at, updated_at
            FROM quote_template
            ORDER BY id DESC
            LIMIT 1
        ''')
        
        row = cursor.fetchone()
        if row:
            if hasattr(row, 'keys') and callable(getattr(row, 'keys', None)):
                template = {
                    'id': row['id'],
                    'company_name': row['company_name'] or '',
                    'company_address': row['company_address'] or '',
                    'company_phone': row['company_phone'] or '',
                    'company_email': row['company_email'] or '',
                    'company_logo': row['company_logo'] or '',
                    'quote_title': row['quote_title'] or '견적서',
                    'footer_note': row['footer_note'] or '',
                    'terms_conditions': row['terms_conditions'] or '',
                    'created_at': row['created_at'],
                    'updated_at': row['updated_at']
                }
            else:
                template = {
                    'id': row[0],
                    'company_name': row[1] or '',
                    'company_address': row[2] or '',
                    'company_phone': row[3] or '',
                    'company_email': row[4] or '',
                    'company_logo': row[5] or '',
                    'quote_title': row[6] or '견적서',
                    'footer_note': row[7] or '',
                    'terms_conditions': row[8] or '',
                    'created_at': row[9],
                    'updated_at': row[10]
                }
        else:
            template = {
                'id': None,
                'company_name': '',
                'company_address': '',
                'company_phone': '',
                'company_email': '',
                'company_logo': '',
                'quote_title': '견적서',
                'footer_note': '',
                'terms_conditions': ''
            }
        
        return jsonify({
            "success": True,
            "template": template
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_get_quote_template: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/quote_template', methods=['POST'])
def api_save_quote_template():
    """견적서 양식 정보 저장 API"""
    try:
        data = request.get_json()
        
        company_name = data.get('company_name', '')
        company_address = data.get('company_address', '')
        company_phone = data.get('company_phone', '')
        company_email = data.get('company_email', '')
        company_logo = data.get('company_logo', '')
        quote_title = data.get('quote_title', '견적서')
        footer_note = data.get('footer_note', '')
        terms_conditions = data.get('terms_conditions', '')
        
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # 기존 양식 확인
        cursor.execute('SELECT id FROM quote_template ORDER BY id DESC LIMIT 1')
        existing = cursor.fetchone()
        
        if existing:
            # 업데이트
            cursor.execute('''
                UPDATE quote_template
                SET company_name = ?, company_address = ?, company_phone = ?,
                    company_email = ?, company_logo = ?, quote_title = ?,
                    footer_note = ?, terms_conditions = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (company_name, company_address, company_phone, company_email,
                  company_logo, quote_title, footer_note, terms_conditions, existing[0] if isinstance(existing, tuple) else existing['id']))
        else:
            # 새로 생성
            cursor.execute('''
                INSERT INTO quote_template
                (company_name, company_address, company_phone, company_email,
                 company_logo, quote_title, footer_note, terms_conditions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (company_name, company_address, company_phone, company_email,
                  company_logo, quote_title, footer_note, terms_conditions))
        
        system.connection.commit()
        
        return jsonify({
            "success": True,
            "message": "견적서 양식이 저장되었습니다."
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in api_save_quote_template: {error_trace}")
        return jsonify({
            "success": False,
            "error": f"서버 오류: {str(e)}"
        }), 500

@app.route('/api/set_price/delete_all', methods=['POST'])
def api_delete_all_set_prices():
    """모든 세트가격 삭제 API"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        
        # selected_combinations 테이블의 모든 가격을 NULL로 업데이트
        cursor.execute('''
            UPDATE selected_combinations
            SET price = NULL
            WHERE price IS NOT NULL
        ''')
        
        updated_count = cursor.rowcount
        
        # option_prices 테이블의 모든 가격도 삭제
        cursor.execute('''
            DELETE FROM option_prices
        ''')
        
        deleted_option_prices_count = cursor.rowcount
        
        system.connection.commit()
        
        return jsonify({
            "success": True,
            "message": f"모든 세트가격이 삭제되었습니다. (세트코드 가격: {updated_count}개, 옵션별 가격: {deleted_option_prices_count}개)",
            "updated_count": updated_count,
            "deleted_option_prices_count": deleted_option_prices_count
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/background_image/upload', methods=['POST'])
def api_upload_background_image():
    """메인 화면 배경 이미지 업로드 API"""
    try:
        if 'file' not in request.files:
            return jsonify({
                "success": False,
                "error": "파일이 없습니다."
            }), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({
                "success": False,
                "error": "파일이 선택되지 않았습니다."
            }), 400
        
        # 이미지 파일 확장자 확인
        allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext not in allowed_extensions:
            return jsonify({
                "success": False,
                "error": "이미지 파일(.jpg, .jpeg, .png, .gif, .webp)만 업로드 가능합니다."
            }), 400
        
        # 파일 크기 확인 (10MB 제한)
        # Flask의 FileStorage 객체에서 파일 크기 확인
        file.seek(0, 2)  # 파일 끝으로 이동
        file_size = file.tell()
        file.seek(0)  # 파일 시작으로 복귀
        max_size = 10 * 1024 * 1024  # 10MB
        if file_size > max_size:
            return jsonify({
                "success": False,
                "error": f"파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다. (현재: {file_size / 1024 / 1024:.2f}MB)"
            }), 400
        
        print(f"업로드 파일 정보: 이름={file.filename}, 크기={file_size} bytes ({file_size / 1024 / 1024:.2f}MB)")
        
        # static/images 디렉토리 생성
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            images_dir = os.path.join(base_dir, 'static', 'images')
            print(f"이미지 디렉토리 경로: {images_dir}")
            
            if not os.path.exists(images_dir):
                os.makedirs(images_dir, exist_ok=True)
                print(f"이미지 디렉토리 생성 완료: {images_dir}")
            
            # 디렉토리 쓰기 권한 확인
            if not os.access(images_dir, os.W_OK):
                return jsonify({
                    "success": False,
                    "error": f"이미지 디렉토리에 쓰기 권한이 없습니다: {images_dir}"
                }), 500
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f"디렉토리 생성 오류: {str(e)}")
            print(f"상세 오류:\n{error_trace}")
            return jsonify({
                "success": False,
                "error": f"디렉토리 생성 중 오류가 발생했습니다: {str(e)}"
            }), 500
        
        # 배경 이미지 파일 저장 (hero_background.jpg로 고정)
        background_filename = 'hero_background' + file_ext
        background_filepath = os.path.join(images_dir, background_filename)
        print(f"저장할 파일 경로: {background_filepath}")
        
        # 기존 파일이 있으면 삭제
        for ext in allowed_extensions:
            old_file = os.path.join(images_dir, 'hero_background' + ext)
            if os.path.exists(old_file) and old_file != background_filepath:
                try:
                    os.remove(old_file)
                    print(f"기존 배경 이미지 삭제: {old_file}")
                except Exception as e:
                    print(f"기존 파일 삭제 오류: {e} (무시하고 계속 진행)")
        
        # 새 파일 저장
        try:
            file.save(background_filepath)
            
            # 파일이 제대로 저장되었는지 확인
            if not os.path.exists(background_filepath):
                return jsonify({
                    "success": False,
                    "error": "파일 저장에 실패했습니다. 파일이 생성되지 않았습니다."
                }), 500
            
            saved_size = os.path.getsize(background_filepath)
            print(f"배경 이미지 저장 완료: {background_filepath} (크기: {saved_size} bytes)")
        except PermissionError as e:
            print(f"파일 저장 권한 오류: {e}")
            return jsonify({
                "success": False,
                "error": f"파일 저장 권한이 없습니다: {str(e)}"
            }), 500
        except OSError as e:
            print(f"파일 저장 OS 오류: {e}")
            return jsonify({
                "success": False,
                "error": f"파일 저장 중 시스템 오류가 발생했습니다: {str(e)}"
            }), 500
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f"파일 저장 오류: {e}")
            print(f"상세 오류:\n{error_trace}")
            return jsonify({
                "success": False,
                "error": f"파일 저장 중 오류가 발생했습니다: {str(e)}"
            }), 500
        
        return jsonify({
            "success": True,
            "message": "배경 이미지가 업로드되었습니다.",
            "filename": background_filename,
            "url": f"/static/images/{background_filename}"
        })
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"배경 이미지 업로드 오류: {str(e)}")
        print(f"상세 오류:\n{error_trace}")
        return jsonify({
            "success": False,
            "error": f"업로드 중 오류가 발생했습니다: {str(e)}"
        }), 500

@app.route('/api/exchange_rates', methods=['GET'])
def api_get_exchange_rates():
    """하나은행 환율 조회 (일 단위 캐시, 필요 시 자동 동기화)"""
    try:
        force_refresh = str(request.args.get('refresh', '')).lower() in ('1', 'true', 'yes')
        system = get_welding_system()
        cursor = system.connection.cursor()
        ensure_exchange_rate_table(cursor)

        today = _today_kst()
        cached = get_cached_rates(cursor, today)

        need_fetch = force_refresh or not cached or not cached.get('usd_krw') or not cached.get('eur_krw')
        if not need_fetch and cached:
            # 당일 캐시가 있으면 반환
            return jsonify(cached)

        try:
            fresh = fetch_hana_exchange_rates()
            if fresh.get('usd_krw') and fresh.get('eur_krw'):
                def persist():
                    save_rates(cursor, fresh)
                    system.connection.commit()
                run_db_with_retry(persist)
                fresh['cached'] = False
                return jsonify(fresh)
        except Exception as fetch_error:
            print(f"Hana rate fetch error: {fetch_error}")
            # 최신 캐시라도 반환
            fallback = get_cached_rates(cursor)
            if fallback:
                fallback['warning'] = f"하나은행 동기화 실패, 캐시 환율 사용: {fetch_error}"
                return jsonify(fallback)
            return jsonify({
                "success": False,
                "error": f"환율 조회 실패: {fetch_error}"
            }), 502

        fallback = get_cached_rates(cursor)
        if fallback:
            return jsonify(fallback)
        return jsonify({"success": False, "error": "환율 데이터가 없습니다."}), 404
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/exchange_rates/sync', methods=['POST'])
def api_sync_exchange_rates():
    """하나은행 환율 강제 동기화"""
    try:
        system = get_welding_system()
        cursor = system.connection.cursor()
        fresh = fetch_hana_exchange_rates()

        def persist():
            save_rates(cursor, fresh)
            system.connection.commit()

        run_db_with_retry(persist)
        fresh['cached'] = False
        return jsonify(fresh)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/background_image/test', methods=['GET'])
def api_test_background_image():
    """배경 이미지 API 테스트 엔드포인트"""
    return jsonify({
        "success": True,
        "message": "배경 이미지 API가 정상적으로 작동합니다.",
        "endpoints": {
            "upload": "/api/background_image/upload",
            "get": "/api/background_image"
        }
    })

@app.route('/api/background_image', methods=['GET'])
def api_get_background_image():
    """현재 배경 이미지 URL 조회 API"""
    try:
        images_dir = os.path.join(os.path.dirname(__file__), 'static', 'images')
        allowed_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
        
        # hero_background 파일 찾기
        for ext in allowed_extensions:
            background_file = os.path.join(images_dir, 'hero_background' + ext)
            if os.path.exists(background_file):
                return jsonify({
                    "success": True,
                    "url": f"/static/images/hero_background{ext}",
                    "filename": f"hero_background{ext}"
                })
        
        # 기본 이미지 URL 반환 (업로드된 이미지가 없는 경우)
        return jsonify({
            "success": True,
            "url": "https://images.unsplash.com/photo-1621905251918-48416bd8575a?ixlib=rb-4.0.3&auto=format&fit=crop&w=2000&q=80",
            "filename": None
        })
        
    except Exception as e:
        print(f"배경 이미지 조회 오류: {str(e)}")
        return jsonify({
            "success": False,
            "error": f"조회 중 오류가 발생했습니다: {str(e)}"
        }), 500

if __name__ == '__main__':
    print("=" * 60)
    print("웰딩 장비 옵션 시스템 시작")
    print("=" * 60)
    print(f"로컬 접속: http://127.0.0.1:5000")
    print(f"외부 접속: http://10.164.38.45:5000")
    print(f"도메인 접속: http://pnsoptions.com:5000")
    print("=" * 60)
    print("브라우저에서 위 주소 중 하나로 접속하세요.")
    print("=" * 60)
    print("등록된 라우트:")
    for rule in app.url_map.iter_rules():
        print(f"  {rule.rule} -> {rule.endpoint}")
    print("=" * 60)
    
    try:
        print("포트 5000으로 서버 시작 시도...")
        app.run(
            debug=True, 
            host='0.0.0.0', 
            port=5000,
            threaded=True,
            use_reloader=False
        )
    except OSError as e:
        if "Address already in use" in str(e) or "포트가 이미 사용 중" in str(e):
            print(f"포트 5000이 이미 사용 중입니다. 다른 포트로 시도합니다...")
            try:
                app.run(
                    debug=True, 
                    host='0.0.0.0', 
                    port=5001,
                    threaded=True,
                    use_reloader=False
                )
            except Exception as e2:
                print(f"포트 5001도 사용 중입니다: {e2}")
                print("사용 가능한 포트를 찾아서 시도해보세요.")
        else:
            print(f"서버 시작 오류: {e}")
    except Exception as e:
        print(f"서버 시작 오류: {e}")
        print("다른 포트로 시도해보세요.")