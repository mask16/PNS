"""수출견적서 PRICE BOOK PDF 생성 — 공통 1/마지막 + 파워소스 중간 페이지"""
from __future__ import annotations

import os
import re
from collections import OrderedDict
from io import BytesIO

from pricebook_page_service import (
    ensure_pricebook_page_tables,
    get_global_latest,
    list_accessory_images,
    list_content_images,
    list_power_source_folders,
    normalize_variant,
)

BURGUNDY = (0.502, 0.0, 0.125)  # #800020

# 파워소스별 사양 (캡쳐 레이아웃용). key = folder_label 대문자 정규화
POWER_SOURCE_SPECS = {
    'SUPER M500': {
        'input_voltage': '3 Phase, 400V(-10% +15%), 50/60Hz',
        'output': '500A 60%, 400A 100%',
        'weight': '74.6kg (164.46 lbs)',
        'dimension': '532*976*1101 mm (20.94*38.43*43.35 in)',
    },
    'SUPER M500 W': {
        'input_voltage': '3 Phase, 400V(-10% +15%), 50/60Hz',
        'output': '500A 60%, 400A 100%',
        'weight': '74.6kg (164.46 lbs)',
        'dimension': '532*976*1101 mm (20.94*38.43*43.35 in)',
    },
}


def _specs_for_label(folder_label: str) -> dict | None:
    label = (folder_label or '').strip().upper()
    if not label:
        return None
    if label in POWER_SOURCE_SPECS:
        return POWER_SOURCE_SPECS[label]
    # "SUPER M500 W" → base "SUPER M500"
    base = re.sub(r'\s+W$', '', label).strip()
    return POWER_SOURCE_SPECS.get(base)


def parse_product_name_tokens(product_name: str) -> dict:
    """코드명에서 모델/타입 키 추출. 예: M50,A001,... → model=M50, type_key=A, variant=std"""
    raw = (product_name or '').strip()
    if not raw:
        return {'model': None, 'type_key': None, 'variant': 'std', 'tokens': [], 'raw': ''}

    cleaned = raw
    if cleaned.startswith('(') and cleaned.endswith(')'):
        cleaned = cleaned[1:-1].strip()

    # MAG(SUPER M500) / DC TIG(SUPER T400 DC) 형태
    paren = re.search(r'\(([^)]*SUPER[^)]*)\)', cleaned, re.I)
    if paren:
        inner = paren.group(1).strip()
        model_keys = _extract_letter_digit_keys(inner)
        return {
            'model': model_keys[0] if model_keys else None,
            'type_key': None,
            'variant': 'std',
            'tokens': [cleaned],
            'raw': raw,
            'super_label': _normalize_super_label(inner),
        }

    if re.search(r'SUPER\s+', cleaned, re.I):
        model_keys = _extract_letter_digit_keys(cleaned)
        return {
            'model': model_keys[0] if model_keys else None,
            'type_key': None,
            'variant': 'std',
            'tokens': [cleaned],
            'raw': raw,
            'super_label': _normalize_super_label(cleaned),
        }

    # 파이프/언더스코어 앞까지만 토큰 분리에 사용
    head = re.split(r'[|_]', cleaned, maxsplit=1)[0]
    tokens = [t.strip() for t in head.split(',') if t.strip()]
    model = tokens[0].upper() if tokens else None
    type_token = tokens[1].upper() if len(tokens) > 1 else ''
    type_key = type_token[:1] if type_token else None
    if type_key == 'W':
        variant = 'W'
    else:
        variant = 'std'
    return {
        'model': model,
        'type_key': type_key,
        'variant': variant,
        'tokens': tokens,
        'raw': raw,
        'super_label': None,
    }


def _normalize_super_label(text: str) -> str:
    text = (text or '').strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def _extract_letter_digit_keys(text: str) -> list[str]:
    if not text:
        return []
    normalized = (
        str(text)
        .upper()
        .replace('(R)', 'R')
        .replace('(S)', 'S')
    )
    return re.findall(r'[A-Z]+\d+', normalized)


def _model_keys_match(abbr: str, full: str) -> bool:
    """M50 ↔ M500, C30 ↔ C300 등 약어 매칭"""
    if not abbr or not full:
        return False
    a = abbr.upper().strip()
    f = full.upper().strip()
    if a == f:
        return True
    ma = re.match(r'^([A-Z]+)(\d+)$', a)
    mf = re.match(r'^([A-Z]+)(\d+)$', f)
    if not ma or not mf:
        return False
    if ma.group(1) != mf.group(1):
        return False
    ad, fd = ma.group(2), mf.group(2)
    return fd.startswith(ad) or ad.startswith(fd)


def resolve_power_source(cursor, product_name: str) -> dict | None:
    """제품 코드명 → 파워소스 폴더(option_item_id, variant, folder_label)"""
    parsed = parse_product_name_tokens(product_name)
    folders = list_power_source_folders(cursor)
    if not folders:
        return None

    variant = normalize_variant(parsed.get('variant'))
    model = parsed.get('model')
    super_label = (parsed.get('super_label') or '').upper()

    best = None
    for folder in folders:
        label = folder.get('folder_label') or ''
        label_u = label.upper()
        keys = _extract_letter_digit_keys(label)

        matched = False
        if super_label and (super_label in label_u or label_u in super_label):
            matched = True
        elif model:
            for key in keys:
                if _model_keys_match(model, key):
                    matched = True
                    break
            if not matched and _model_keys_match(model, ''.join(keys[:1])):
                matched = True

        if not matched:
            continue

        folder_label = label
        if variant == 'W':
            folder_label = folder.get('folder_label_w') or f'{label} W'

        candidate = {
            'option_item_id': folder['option_item_id'],
            'variant': variant,
            'folder_label': folder_label,
            'base_label': label,
            'model_key': f"{model or ''},{parsed.get('type_key') or ''}".rstrip(','),
            'parsed': parsed,
        }
        # 더 긴 키 매칭 우선 (M500 > M50 충돌 시)
        score = max((len(k) for k in keys if model and _model_keys_match(model, k)), default=0)
        candidate['_score'] = score
        if best is None or candidate['_score'] > best['_score']:
            best = candidate

    if best:
        best.pop('_score', None)
    return best


def build_price_description(folder_label: str, product_name: str) -> str:
    """가격표 Description: SUPER M500 - FEEDER WF4 5m"""
    parsed = parse_product_name_tokens(product_name)
    tokens = parsed.get('tokens') or []
    feeder = None
    hose_m = None
    for token in tokens[2:]:
        t = token.upper()
        if t.startswith('WF'):
            feeder = t
        elif re.match(r'^H\d+$', t):
            hose_m = t[1:] + 'm'
    if feeder:
        parts = [folder_label, '-', 'FEEDER', feeder]
        if hose_m:
            parts.append(hose_m)
        return ' '.join(parts)
    name = (product_name or '').strip()
    if len(name) > 60:
        name = name[:57] + '...'
    return name or folder_label


def lookup_products_from_db(cursor, items: list[dict]) -> list[dict]:
    """클라이언트 코드 목록 → DB 프라이스북 단가/코드명 보강"""
    ensure_pricebook_page_tables(cursor)
    results = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get('code') or item.get('product_code') or '').strip()
        if not code:
            continue
        qty = item.get('quantity', 1)
        try:
            qty = int(qty) or 1
        except (TypeError, ValueError):
            qty = 1

        name = str(item.get('name') or item.get('product_name') or '').strip()
        price = item.get('priceBookPrice', item.get('price_book_price'))
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None

        cursor.execute(
            '''
            SELECT product_code, product_name, price_book_price
            FROM export_quotation_new_products
            WHERE product_code = ?
            LIMIT 1
            ''',
            (code,),
        )
        row = cursor.fetchone()
        if row:
            if not name:
                name = row[1] or ''
            if price is None:
                price = float(row[2] or 0)

        if price is None:
            price = 0.0

        results.append({
            'code': code,
            'name': name,
            'quantity': qty,
            'price_book_price': float(price),
        })
    return results


def group_products_by_power_source(cursor, products: list[dict]) -> OrderedDict:
    """(option_item_id, variant) → {folder_label, products, images}"""
    groups: OrderedDict = OrderedDict()
    for product in products:
        resolved = resolve_power_source(cursor, product.get('name') or '')
        if not resolved:
            continue
        key = (resolved['option_item_id'], resolved['variant'])
        if key not in groups:
            images = list_content_images(
                cursor,
                resolved['option_item_id'],
                variant=resolved['variant'],
            )
            # 오래된 것부터 → PDF에서 등록 순에 가깝게
            images = list(reversed(images)) if images else []
            accessories = list(reversed(list_accessory_images(cursor, resolved['option_item_id']) or []))
            groups[key] = {
                'option_item_id': resolved['option_item_id'],
                'variant': resolved['variant'],
                'folder_label': resolved['folder_label'],
                'products': [],
                'images': images,
                'accessories': accessories,
            }
        # description은 그룹 라벨 기준
        product = dict(product)
        product['description'] = build_price_description(
            groups[key]['folder_label'],
            product.get('name') or '',
        )
        groups[key]['products'].append(product)
    return groups


def _resolve_filepath(filepath: str) -> str | None:
    if not filepath:
        return None
    if os.path.isfile(filepath):
        return filepath
    alt = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'uploads',
        'pricebook_pages',
        os.path.basename(filepath.replace('\\', '/')),
    )
    if os.path.isfile(alt):
        return alt
    return None


def _prepare_image(filepath: str):
    from reportlab.lib.utils import ImageReader
    try:
        from PIL import Image, ImageOps
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
            return ImageReader(buf)
    except Exception:
        return ImageReader(filepath)


def _ensure_fonts():
    font = 'Helvetica'
    font_bold = 'Helvetica-Bold'
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        regular = r'C:\Windows\Fonts\malgun.ttf'
        bold = r'C:\Windows\Fonts\malgunbd.ttf'
        if os.path.exists(regular):
            if 'PNSGothicPB' not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont('PNSGothicPB', regular))
            font = 'PNSGothicPB'
            if os.path.exists(bold):
                if 'PNSGothicPB-Bold' not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont('PNSGothicPB-Bold', bold))
                font_bold = 'PNSGothicPB-Bold'
            else:
                font_bold = 'PNSGothicPB'
    except Exception as err:
        print(f'PRICE BOOK PDF font fallback: {err}')
    return font, font_bold


def _format_euro(value: float) -> str:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    # € 6,780 형태 (정수에 가깝면 정수)
    if abs(n - round(n)) < 0.005:
        return f'€ {int(round(n)):,}'
    return f'€ {n:,.2f}'


def _draw_full_page_image(pdf, filepath: str, page_size) -> None:
    from reportlab.lib.pagesizes import A4
    page_width, page_height = page_size or A4
    img = _prepare_image(filepath)
    iw, ih = img.getSize()
    if iw <= 0 or ih <= 0:
        raise ValueError('invalid image')
    margin = 18
    max_w = page_width - margin * 2
    max_h = page_height - margin * 2
    scale = min(max_w / iw, max_h / ih)
    dw, dh = iw * scale, ih * scale
    x = (page_width - dw) / 2
    y = (page_height - dh) / 2
    pdf.drawImage(img, x, y, width=dw, height=dh, preserveAspectRatio=True, mask='auto')
    pdf.showPage()


def _logo_path() -> str | None:
    """PRICE BOOK 중간페이지용 HYUNDAI WELDING 로고"""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'images')
    for name in ('hyundai-welding-logo.png', 'hyundai-logo.png.png', 'hyundai-logo.png'):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return None


def _draw_header_brand(pdf, page_width, page_height, left, right) -> float:
    """상단 HYUNDAI 로고 + 전체 폭 버건디 구분선. 반환: 타이틀 y"""
    from reportlab.lib.colors import Color, HexColor

    y_top = page_height - 26
    logo = _logo_path()
    logo_bottom = y_top - 10
    if logo:
        try:
            img = _prepare_image(logo)
            iw, ih = img.getSize()
            logo_h = 20
            logo_w = iw * (logo_h / float(ih)) if ih else 90
            max_w = 110
            if logo_w > max_w:
                logo_w = max_w
                logo_h = ih * (logo_w / float(iw)) if iw else logo_h
            pdf.drawImage(
                img,
                right - logo_w,
                y_top - logo_h,
                width=logo_w,
                height=logo_h,
                preserveAspectRatio=True,
                mask='auto',
            )
            logo_bottom = y_top - logo_h
        except Exception:
            logo_bottom = y_top - 10
    else:
        font, font_bold = _ensure_fonts()
        pdf.setFillColor(HexColor('#333333'))
        pdf.setFont(font_bold, 9)
        pdf.drawRightString(right, y_top - 6, 'HYUNDAI WELDING')
        logo_bottom = y_top - 14

    y_line = logo_bottom - 8
    pdf.setStrokeColor(Color(*BURGUNDY))
    pdf.setLineWidth(1.3)
    pdf.line(left, y_line, right, y_line)
    return y_line - 14


def _draw_title_with_bar(pdf, text: str, x: float, y: float, font_bold: str) -> None:
    from reportlab.lib.colors import Color, HexColor

    bar_w = 5
    bar_h = 18
    pdf.setFillColor(Color(*BURGUNDY))
    pdf.rect(x, y - 3, bar_w, bar_h, fill=1, stroke=0)
    pdf.setFillColor(HexColor('#222222'))
    pdf.setFont(font_bold, 18)
    pdf.drawString(x + bar_w + 8, y + 1, text or '')


def _draw_left_image(
    pdf,
    filepath: str | None,
    left: float,
    max_w: float,
    y_top: float,
    max_h: float,
    fill_width: bool = False,
) -> tuple[float, float]:
    """좌측 정렬 이미지(전체 표시, 잘림 없음). 반환: (하단 y, 오른쪽 x)"""
    if not filepath:
        return y_top, left + max_w
    try:
        img = _prepare_image(filepath)
        iw, ih = img.getSize()
        if iw <= 0 or ih <= 0:
            return y_top, left + max_w
        # 가로·세로 모두 영역에 들어가도록 축소 (클리핑/강제폭 없음)
        scale = min(max_w / float(iw), max_h / float(ih))
        dw, dh = iw * scale, ih * scale
        y = y_top - dh
        pdf.drawImage(img, left, y, width=dw, height=dh, preserveAspectRatio=True, mask='auto')
        # 가격표 폭 맞춤: fill_width면 열 전체 폭 반환, 아니면 실제 그림 폭
        right_x = left + max_w if fill_width else left + dw
        return y, right_x
    except Exception as img_err:
        font, _ = _ensure_fonts()
        from reportlab.lib.colors import HexColor
        pdf.setFont(font, 9)
        pdf.setFillColor(HexColor('#666666'))
        pdf.drawString(left, y_top - 14, f'Image failed: {img_err}')
        return y_top - 20, left + max_w


def _draw_specs_table(pdf, folder_label: str, left: float, right: float, y: float, font: str, font_bold: str) -> float:
    """캡쳐형 사양표. 반환: 테이블 하단 y"""
    from reportlab.lib.colors import Color, black, HexColor

    specs = _specs_for_label(folder_label)
    if not specs:
        return y

    width = right - left
    label_w = width * 0.28
    value_w = width - label_w
    row_h = 15
    gray = HexColor('#efefef')

    # 상단 버건디 라인
    pdf.setStrokeColor(Color(*BURGUNDY))
    pdf.setLineWidth(1.4)
    pdf.line(left, y, right, y)
    y -= 1

    # 행 구성: MIG/DC TIG/MMA 는 값 셀 병합
    rows = [
        ('Input Voltage', specs.get('input_voltage') or '', 1),
        ('MIG', specs.get('output') or '', 3),  # 값 높이 3행
        ('DC TIG (Lift)', None, 0),
        ('MMA', None, 0),
        ('Weight', specs.get('weight') or '', 1),
        ('Dimension (W*L*H)', specs.get('dimension') or '', 1),
    ]

    i = 0
    while i < len(rows):
        label, value, span = rows[i]
        if span == 0:
            i += 1
            continue
        block_h = row_h * span
        # label 배경
        pdf.setFillColor(gray)
        pdf.rect(left, y - block_h, label_w, block_h, fill=1, stroke=0)
        pdf.setFillColor(black)
        pdf.setFont(font, 8.5)
        if span == 1:
            pdf.drawString(left + 6, y - row_h + 4, label)
        else:
            # 3개 라벨
            for k in range(span):
                lab = rows[i + k][0]
                pdf.drawString(left + 6, y - row_h * (k + 1) + 4, lab)
            # 값 세로 중앙
            pdf.setFont(font, 8.5)
            pdf.drawString(left + label_w + 8, y - (block_h / 2) - 3, value)
            # 구분선
            pdf.setStrokeColor(HexColor('#d5d5d5'))
            pdf.setLineWidth(0.4)
            for k in range(1, span):
                ly = y - row_h * k
                pdf.line(left, ly, left + label_w, ly)
            pdf.setStrokeColor(HexColor('#d5d5d5'))
            pdf.line(left + label_w, y - block_h, right, y - block_h)
            pdf.line(left, y - block_h, right, y - block_h)
            y -= block_h
            i += span
            continue

        pdf.setFillColor(black)
        pdf.setFont(font, 8.5)
        pdf.drawString(left + label_w + 8, y - row_h + 4, value)
        pdf.setStrokeColor(HexColor('#d5d5d5'))
        pdf.setLineWidth(0.4)
        pdf.line(left, y - row_h, right, y - row_h)
        y -= row_h
        i += 1

    # 하단 버건디 라인
    pdf.setStrokeColor(Color(*BURGUNDY))
    pdf.setLineWidth(1.4)
    pdf.line(left, y, right, y)
    return y - 14


def _wrap_text_to_width(text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
    """폭에 맞게 텍스트를 여러 줄로 분할 (잘림 없이 전체 표기)"""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    raw = (text or '').strip()
    if not raw:
        return ['']
    if stringWidth(raw, font_name, font_size) <= max_width:
        return [raw]

    lines = []
    current = ''
    for ch in raw:
        trial = current + ch
        if stringWidth(trial, font_name, font_size) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines or [raw]


def _draw_price_table(
    pdf,
    folder_label: str,
    products: list[dict],
    left: float,
    right: float,
    y: float,
    page_width: float,
    page_height: float,
    font: str,
    font_bold: str,
    allow_page_break: bool = True,
    compact: bool = False,
) -> float:
    """캡쳐 가격표: 상·하단 버건디, 좌·우 외곽선 없음. Description 전체 표기."""
    from reportlab.lib.colors import Color, black, HexColor

    width = right - left
    bar_h = 14 if compact else 18
    # 설명 칸을 넓게: 코드 좁게 / 가격 적당히
    col_code = width * (0.18 if compact else 0.18)
    col_price = width * (0.16 if compact else 0.18)
    col_desc = width - col_code - col_price
    x_code_end = left + col_code
    x_desc_end = left + col_code + col_desc
    line_color = HexColor('#d0d0d0')
    header_bg = HexColor('#e8e8e8')
    code_bg = HexColor('#efefef')
    font_size = 7 if compact else 9
    desc_size = 6.5 if compact else 8.5
    header_size = 8 if compact else 10
    line_gap = 8.5 if compact else 11
    pad_y = 2.5 if compact else 3.5
    desc_max_w = col_desc - 6

    def draw_burgundy(at_y: float) -> None:
        pdf.setStrokeColor(Color(*BURGUNDY))
        pdf.setLineWidth(1.6 if compact else 1.8)
        pdf.line(left, at_y, right, at_y)

    def draw_header(start_y: float) -> float:
        draw_burgundy(start_y)
        top = start_y - 1
        pdf.setFillColor(header_bg)
        pdf.rect(left, top - bar_h, width, bar_h, fill=1, stroke=0)
        pdf.setFillColor(HexColor('#333333'))
        pdf.setFont(font_bold, header_size)
        pdf.drawCentredString((left + right) / 2, top - bar_h + (4 if compact else 5), folder_label or '')
        pdf.setStrokeColor(line_color)
        pdf.setLineWidth(0.5)
        pdf.line(left, top - bar_h, right, top - bar_h)
        return top - bar_h

    def draw_row(text_y: float, code: str, desc: str, price: str) -> float:
        desc_lines = _wrap_text_to_width(desc, font, desc_size, desc_max_w)
        row_h = max(line_gap + pad_y, len(desc_lines) * line_gap + pad_y)
        bottom = text_y - row_h

        pdf.setFillColor(code_bg)
        pdf.rect(left, bottom, col_code, row_h, fill=1, stroke=0)
        pdf.setStrokeColor(line_color)
        pdf.setLineWidth(0.5)
        pdf.line(x_code_end, bottom, x_code_end, text_y)
        pdf.line(x_desc_end, bottom, x_desc_end, text_y)
        pdf.line(left, bottom, right, bottom)

        pdf.setFillColor(black)
        pdf.setFont(font, font_size)
        # 코드/가격은 첫 줄 높이 기준
        first_baseline = text_y - line_gap + 1
        pdf.drawString(left + 3, first_baseline, code)
        pdf.drawRightString(right - 3, first_baseline, price)

        pdf.setFont(font, desc_size)
        for i, line in enumerate(desc_lines):
            pdf.drawString(x_code_end + 3, text_y - (i + 1) * line_gap + 1, line)
        return bottom

    y = draw_header(y)

    for product in products:
        desc = str(product.get('description') or product.get('name') or '')
        desc_lines = _wrap_text_to_width(desc, font, desc_size, desc_max_w)
        need_h = max(line_gap + pad_y, len(desc_lines) * line_gap + pad_y) + 4

        if allow_page_break and y - need_h < 48:
            draw_burgundy(y)
            pdf.showPage()
            y = _draw_header_brand(pdf, page_width, page_height, left, right)
            y -= 8
            y = draw_header(y)

        if not allow_page_break and y - need_h < 30:
            break

        code = str(product.get('code') or '')
        price = _format_euro(product.get('price_book_price') or 0)
        y = draw_row(y, code, desc, price)

    draw_burgundy(y)
    return y


def _draw_accessory_images(
    pdf,
    accessories: list[dict] | None,
    left: float,
    right: float,
    y_top: float,
    max_h: float,
) -> float:
    """중간 페이지 하단에 하단악세서리 이미지 표기. 반환: 하단 y"""
    if not accessories:
        return y_top
    paths = []
    for item in accessories:
        path = _resolve_filepath(item.get('filepath'))
        if path and not _is_pdf(path):
            paths.append(path)
    if not paths:
        return y_top

    y = y_top - 8
    width = right - left
    gap = 8
    n = min(len(paths), 4)
    cell_w = (width - gap * (n - 1)) / n if n > 1 else width
    cell_h = max_h
    bottoms = []
    for i, path in enumerate(paths[:4]):
        x = left + i * (cell_w + gap)
        try:
            img = _prepare_image(path)
            iw, ih = img.getSize()
            if iw <= 0 or ih <= 0:
                continue
            scale = min(cell_w / iw, cell_h / ih)
            dw, dh = iw * scale, ih * scale
            draw_x = x + (cell_w - dw) / 2
            draw_y = y - dh
            pdf.drawImage(img, draw_x, draw_y, width=dw, height=dh, preserveAspectRatio=True, mask='auto')
            bottoms.append(draw_y)
        except Exception as err:
            print(f'accessory image draw failed: {err}')
    return min(bottoms) if bottoms else y_top


def _primary_image_path(group: dict) -> str | None:
    for image in group.get('images') or []:
        path = _resolve_filepath(image.get('filepath'))
        if path and not _is_pdf(path):
            return path
    return None


def _draw_middle_page(
    pdf,
    folder_label: str,
    image_paths: list[str | None] | str | None,
    products: list[dict] | None,
    page_size,
    accessories: list[dict] | None = None,
) -> None:
    """단일 모델: 로고 + 등록이미지 + 가격표 + 하단악세서리"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.colors import HexColor

    font, font_bold = _ensure_fonts()
    page_width, page_height = page_size or A4
    left = 40
    page_right = page_width - 40
    page_content_w = page_right - left

    if isinstance(image_paths, str) or image_paths is None:
        paths = [image_paths]
    else:
        paths = list(image_paths)
    image_path = None
    for p in paths:
        if p:
            image_path = p
            break

    y = _draw_header_brand(pdf, page_width, page_height, left, page_right)
    y -= 6

    has_acc = bool(accessories)
    # 등록 이미지(제목+사진+사양)가 잘리지 않도록 높이 여유 확보
    img_max_w = page_content_w * 0.55
    img_max_h = page_height * (0.52 if has_acc else 0.62)
    y_before = y
    content_right = left + img_max_w
    if image_path:
        y, content_right = _draw_left_image(
            pdf, image_path, left, img_max_w, y, img_max_h, fill_width=False
        )
    else:
        pdf.setFont(font, 10)
        pdf.setFillColor(HexColor('#888888'))
        pdf.drawString(left, y_before - 40, '등록된 이미지가 없습니다.')
        y = y_before - 70
        content_right = left + img_max_w
    y -= 10

    table_bottom = y
    if products:
        table_bottom = _draw_price_table(
            pdf,
            folder_label or '',
            products,
            left,
            content_right,
            y,
            page_width,
            page_height,
            font,
            font_bold,
            allow_page_break=True,
            compact=False,
        )

    if accessories:
        acc_max_h = max(60, table_bottom - 36)
        _draw_accessory_images(pdf, accessories, left, content_right, table_bottom, acc_max_h)

    pdf.showPage()


def _draw_dual_middle_page(pdf, left_group: dict, right_group: dict, page_size) -> None:
    """SUPER M500 | SUPER M500 W 한 페이지 좌우 배치 + 하단악세서리"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.colors import HexColor

    font, font_bold = _ensure_fonts()
    page_width, page_height = page_size or A4
    margin = 28
    gap = 14
    left = margin
    page_right = page_width - margin
    usable = page_right - left
    col_w = (usable - gap) / 2
    left_x = left
    right_x = left + col_w + gap

    accessories = left_group.get('accessories') or right_group.get('accessories') or []
    has_acc = bool(accessories)

    y = _draw_header_brand(pdf, page_width, page_height, left, page_right)
    y -= 4

    # 등록 이미지 전체(제목·사진·사양)가 보이도록 높이 확보
    img_max_h = page_height * (0.48 if has_acc else 0.55)
    left_img = _primary_image_path(left_group)
    right_img = _primary_image_path(right_group)

    y_left = y
    y_right = y

    if left_img:
        y_left, left_right_edge = _draw_left_image(
            pdf, left_img, left_x, col_w, y, img_max_h, fill_width=False
        )
    else:
        pdf.setFont(font, 8)
        pdf.setFillColor(HexColor('#888888'))
        pdf.drawString(left_x, y - 20, '등록된 이미지가 없습니다.')
        y_left = y - 40
        left_right_edge = left_x + col_w

    if right_img:
        y_right, right_right_edge = _draw_left_image(
            pdf, right_img, right_x, col_w, y, img_max_h, fill_width=False
        )
    else:
        pdf.setFont(font, 8)
        pdf.setFillColor(HexColor('#888888'))
        pdf.drawString(right_x, y - 20, '등록된 이미지가 없습니다.')
        y_right = y - 40
        right_right_edge = right_x + col_w

    # 가격표 폭 = 실제 그려진 이미지 폭에 맞춤
    table_y = min(y_left, y_right) - 8

    y_table_l = _draw_price_table(
        pdf,
        left_group.get('folder_label') or '',
        left_group.get('products') or [],
        left_x,
        left_right_edge,
        table_y,
        page_width,
        page_height,
        font,
        font_bold,
        allow_page_break=False,
        compact=True,
    )
    y_table_r = _draw_price_table(
        pdf,
        right_group.get('folder_label') or '',
        right_group.get('products') or [],
        right_x,
        right_right_edge,
        table_y,
        page_width,
        page_height,
        font,
        font_bold,
        allow_page_break=False,
        compact=True,
    )

    if accessories:
        acc_top = min(y_table_l, y_table_r)
        acc_max_h = max(50, acc_top - 28)
        _draw_accessory_images(pdf, accessories, left, page_right, acc_top, acc_max_h)

    pdf.showPage()


def _pair_power_source_groups(groups: OrderedDict) -> list[tuple]:
    """같은 파워소스의 std+W 를 한 쌍으로 묶음. [('dual', std, W) | ('single', g, None)]"""
    by_id: OrderedDict = OrderedDict()
    for group in groups.values():
        oid = group.get('option_item_id')
        if oid not in by_id:
            by_id[oid] = {'std': None, 'W': None, 'order': len(by_id)}
        if group.get('variant') == 'W':
            by_id[oid]['W'] = group
        else:
            by_id[oid]['std'] = group

    pages = []
    for pair in by_id.values():
        std_g = pair['std']
        w_g = pair['W']
        if std_g and w_g:
            pages.append(('dual', std_g, w_g))
        elif std_g:
            pages.append(('single', std_g, None))
        elif w_g:
            pages.append(('single', w_g, None))
    return pages


def _append_pdf_file(writer, filepath: str) -> bool:
    """기존 PDF 파일을 writer에 병합. 성공 여부 반환."""
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            print('pypdf not installed; cannot merge PDF cover pages')
            return False
    try:
        reader = PdfReader(filepath)
        for page in reader.pages:
            writer.add_page(page)
        return True
    except Exception as err:
        print(f'PDF merge failed ({filepath}): {err}')
        return False


def _is_pdf(path: str) -> bool:
    return (path or '').lower().endswith('.pdf')


def build_pricebook_pdf(cursor, items: list[dict]) -> tuple[BytesIO | None, str | None]:
    """
    PRICE BOOK PDF 생성.
    반환: (buffer, error)
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4

    ensure_pricebook_page_tables(cursor)
    products = lookup_products_from_db(cursor, items)
    if not products:
        return None, '다운로드할 제품이 없습니다. 제품을 먼저 추가해 주세요.'

    groups = group_products_by_power_source(cursor, products)
    global_pages = get_global_latest(cursor, variant='std')
    first = global_pages.get('first')
    last = global_pages.get('last')

    first_path = _resolve_filepath(first.get('filepath') if first else None)
    last_path = _resolve_filepath(last.get('filepath') if last else None)

    if not first_path and not last_path and not groups:
        return None, '프라이스북 페이지(1페이지/이미지/마지막페이지)가 등록되지 않았습니다.'

    needs_merge = (
        (first_path and _is_pdf(first_path))
        or (last_path and _is_pdf(last_path))
    )
    if needs_merge:
        return _build_with_pdf_covers(groups, first_path, last_path)

    _ensure_fonts()
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)

    if first_path:
        try:
            _draw_full_page_image(pdf, first_path, A4)
        except Exception as err:
            print(f'first page draw failed: {err}')

    _draw_all_middle_pages(pdf, groups, A4)

    if last_path:
        try:
            _draw_full_page_image(pdf, last_path, A4)
        except Exception as err:
            print(f'last page draw failed: {err}')

    pdf.save()
    buffer.seek(0)
    return buffer, None


def _draw_all_middle_pages(pdf, groups, page_size) -> None:
    for kind, primary, secondary in _pair_power_source_groups(groups):
        if kind == 'dual' and primary and secondary:
            # std(기본) 왼쪽, W 오른쪽
            left_g, right_g = primary, secondary
            if primary.get('variant') == 'W':
                left_g, right_g = secondary, primary
            _draw_dual_middle_page(pdf, left_g, right_g, page_size)
            continue

        group = primary
        images = group.get('images') or []
        products_in_group = group.get('products') or []
        label = group.get('folder_label') or ''
        resolved_paths = []
        for image in images:
            path = _resolve_filepath(image.get('filepath'))
            if path and not _is_pdf(path):
                resolved_paths.append(path)
        primary_img = resolved_paths[0] if resolved_paths else None
        _draw_middle_page(
            pdf,
            label,
            [primary_img],
            products_in_group,
            page_size,
            accessories=group.get('accessories') or [],
        )


def _image_to_single_page_pdf(filepath: str) -> BytesIO:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    try:
        _draw_full_page_image(c, filepath, A4)
    except Exception:
        c.showPage()
    c.save()
    buf.seek(0)
    return buf


def _build_with_pdf_covers(groups, first_path, last_path) -> tuple[BytesIO | None, str | None]:
    """1/마지막이 PDF일 때 pypdf로 병합"""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4

    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return None, 'PDF 표지 병합을 위해 pypdf 패키지가 필요합니다. (pip install pypdf)'

    mid_buf = BytesIO()
    pdf = canvas.Canvas(mid_buf, pagesize=A4)
    _draw_all_middle_pages(pdf, groups, A4)
    pdf.save()
    mid_buf.seek(0)

    writer = PdfWriter()
    if first_path:
        if _is_pdf(first_path):
            if not _append_pdf_file(writer, first_path):
                return None, '1페이지 PDF를 읽지 못했습니다.'
        else:
            writer.append(PdfReader(_image_to_single_page_pdf(first_path)))

    mid_reader = PdfReader(mid_buf)
    for page in mid_reader.pages:
        writer.add_page(page)

    if last_path:
        if _is_pdf(last_path):
            if not _append_pdf_file(writer, last_path):
                return None, '마지막페이지 PDF를 읽지 못했습니다.'
        else:
            writer.append(PdfReader(_image_to_single_page_pdf(last_path)))

    out = BytesIO()
    writer.write(out)
    out.seek(0)
    return out, None
