"""
로직 제한조건 정의

프론트엔드 선택 페이지와 대시보드에서 공통으로 사용되는
상세 옵션 제약 조건 데이터를 정의한다.
"""

DETAILED_RULES = [
    {
        "powerSourceCode": "P58100013",
        "typeValue": "AIR",
        "actions": [
            {"category": 3, "allowCodes": ["P58200003", "P58200001"], "allowNone": False},
            {"category": 4, "allowCodes": ["P58400005"], "allowNone": True},
            {"category": 5, "allowCodes": ["P58500027", "P58500028", "P58500029", "P58500030"], "allowNone": False},
            {"category": 6, "allowCodes": ["P58600001"], "allowNone": True},
            {"category": 7, "allowCodes": ["P58300002"], "allowNone": False},
            {"category": 8, "disableCategory": True},
            {"category": 9, "disableCategory": True},
        ],
    },
    {
        "powerSourceCode": "P58100013",
        "typeValue": "WATER",
        "actions": [
            {"category": 3, "allowCodes": ["P58200002", "P58200000"], "allowNone": False},
            {"category": 4, "allowCodes": ["P58400002", "P58400003"], "allowNone": True},
            {"category": 5, "allowCodes": ["P58500031", "P58500032", "P58500033", "P58500034"], "allowNone": False},
            {"category": 6, "allowCodes": ["P58600001"], "allowNone": True},
            {"category": 7, "allowCodes": ["P58300001"], "allowNone": False},
            {"category": 8, "allowCodes": ["P58900001"], "allowNone": False},
            {"category": 9, "allowCodes": ["P58900002"], "allowNone": True},
        ],
    },
    {
        "powerSourceCode": "P58100014",
        "typeValue": "AIR",
        "actions": [
            {"category": 3, "allowCodes": ["P58200003", "P58200001"], "allowNone": False},
            {"category": 4, "allowCodes": ["P58400005"], "allowNone": True},
            {"category": 5, "allowCodes": ["P58500035", "P58500028", "P58500029", "P58500030"], "allowNone": False},
            {"category": 6, "allowCodes": ["P58600002"], "allowNone": True},
            {"category": 7, "allowCodes": ["P58300001"], "allowNone": False},
            {"category": 8, "disableCategory": True},
            {"category": 9, "disableCategory": True},
        ],
    },
    {
        "powerSourceCode": "P58100014",
        "typeValue": "WATER",
        "actions": [
            {"category": 3, "allowCodes": ["P58200002", "P58200000"], "allowNone": False},
            {"category": 4, "allowCodes": ["P58400002", "P58400003"], "allowNone": True},
            {"category": 5, "allowCodes": ["P58500036", "P58500032", "P58500033", "P58500034"], "allowNone": False},
            {"category": 6, "allowCodes": ["P58600002"], "allowNone": True},
            {"category": 7, "allowCodes": ["P58300001"], "allowNone": False},
            {"category": 8, "allowCodes": ["P58900001"], "allowNone": False},
            {"category": 9, "allowCodes": ["P58900002"], "allowNone": True},
        ],
    },
    {
        "powerSourceCode": "P58100015",
        "typeValue": "AIR",
        "actions": [
            {"category": 4, "allowCodes": ["P58400005"], "allowNone": True},
            {"category": 6, "allowCodes": ["P58600003"], "allowNone": True},
            {"category": 10, "allowCodes": ["P58700000"], "allowNone": True},
            {"category": 7, "allowCodes": ["P58300001"], "allowNone": False},
            {"category": 8, "disableCategory": True},
            {"category": 9, "disableCategory": True},
        ],
    },
    {
        "powerSourceCode": "P58100015",
        "typeValue": "WATER",
        "actions": [
            {"category": 4, "allowCodes": ["P58400002", "P58400003"], "allowNone": True},
            {"category": 6, "allowCodes": ["P58600003"], "allowNone": True},
            {"category": 10, "allowCodes": ["P58700000"], "allowNone": True},
            {"category": 7, "allowCodes": ["P58300001"], "allowNone": False},
            {"category": 8, "allowCodes": ["P58900001"], "allowNone": False},
        ],
    },
    {
        "powerSourceCode": "P58100000",
        "typeValue": "AIR",
        "actions": [
            {"category": 4, "allowCodes": ["P58400000"], "allowNone": True},
            {"category": 6, "allowCodes": ["P58600003"], "allowNone": True},
            {"category": 10, "allowCodes": ["P58700000"], "allowNone": True},
            {"category": 7, "allowCodes": ["P58600000"], "allowNone": False},
            {"category": 8, "disableCategory": True},
            {"category": 9, "disableCategory": True},
        ],
    },
    {
        "powerSourceCode": "P58100000",
        "typeValue": "WATER",
        "actions": [
            {"category": 4, "allowCodes": ["P58400001"], "allowNone": True},
            {"category": 6, "allowCodes": ["P58600003"], "allowNone": True},
            {"category": 10, "allowCodes": ["P58700000"], "allowNone": True},
            {"category": 7, "allowCodes": ["P58600000"], "allowNone": False},
            {"category": 8, "allowCodes": ["P58900001"], "allowNone": False},
            {"category": 9, "allowCodes": ["P58900002"], "allowNone": True},
        ],
    },
    {
        "powerSourceCode": "P58100002",
        "typeValue": "AIR",
        "actions": [
            {"category": 4, "allowCodes": ["P58400000"], "allowNone": True},
            {"category": 6, "allowCodes": ["P58600005"], "allowNone": True},
            {"category": 10, "allowCodes": ["P58700000"], "allowNone": True},
            {"category": 7, "disableCategory": True},
            {"category": 8, "disableCategory": True},
            {"category": 9, "disableCategory": True},
        ],
    },
    {
        "powerSourceCode": "P58100004",
        "typeValue": "AIR",
        "actions": [
            {"category": 4, "allowCodes": ["P58400000"], "allowNone": True},
            {"category": 6, "allowCodes": ["P58600005"], "allowNone": True},
            {"category": 10, "allowCodes": ["P58700000"], "allowNone": True},
            {"category": 7, "disableCategory": True},
            {"category": 8, "disableCategory": True},
            {"category": 9, "disableCategory": True},
        ],
    },
    {
        "powerSourceCode": "P58100006",
        "typeValue": "AIR",
        "actions": [
            {"category": 4, "allowCodes": ["P58400000"], "allowNone": True},
            {"category": 6, "allowCodes": ["P58600005"], "allowNone": True},
            {"category": 10, "allowCodes": ["P58700000"], "allowNone": True},
            {"category": 7, "disableCategory": True},
            {"category": 8, "disableCategory": True},
            {"category": 9, "disableCategory": True},
        ],
    },
    {
        "powerSourceCode": "P58100008",
        "typeValue": "AIR",
        "actions": [
            {"category": 11, "allowCodes": ["P58800000"], "allowNone": False},
            {"category": 6, "allowCodes": ["P58600003"], "allowNone": False},
        ],
    },
    {
        "powerSourceCode": "P58100009",
        "typeValue": "AIR",
        "actions": [
            {"category": 11, "allowCodes": ["P58800001"], "allowNone": False},
            {"category": 6, "allowCodes": ["P58600005"], "allowNone": False},
        ],
    },
    {
        "powerSourceCode": "P58100010",
        "typeValue": "AIR",
        "actions": [
            {"category": 11, "allowCodes": ["P58800001"], "allowNone": False},
            {"category": 6, "allowCodes": ["P58600005"], "allowNone": False},
        ],
    },
    {
        "powerSourceCode": "P58100011",
        "typeValue": "AIR",
        "actions": [
            {"category": 11, "allowCodes": ["P58800002"], "allowNone": False},
            {"category": 6, "allowCodes": ["P58600006"], "allowNone": False},
        ],
    },
    {
        "powerSourceValue": "MAG(SUPER M500)",
        "typeValue": "AIR",
        "actions": [
            {"category": 3, "allowCodes": ["P58200003", "P58200001"], "allowNone": True},
            {"category": 4, "allowCodes": ["P58400005"], "allowNone": True},
            {"category": 5, "allowCodes": ["P58500027", "P58500028", "P58500029", "P58500030"], "allowNone": True},
            {"category": 6, "allowCodes": ["P58600001"], "allowNone": True},
            {"category": 7, "allowCodes": ["P58300002"], "allowNone": True},
            {"category": 8, "disableCategory": True},
            {"category": 9, "disableCategory": True},
        ],
    },
    {
        "powerSourceValue": "MAG(SUPER M500)",
        "typeValue": "WATER",
        "actions": [
            {"category": 3, "allowCodes": ["P58200002", "P58200000"], "allowNone": True},
            {"category": 4, "allowCodes": ["P58400002", "P58400003"], "allowNone": True},
            {"category": 5, "allowCodes": ["P58500031", "P58500032", "P58500033", "P58500034"], "allowNone": True},
            {"category": 6, "allowCodes": ["P58600001"], "allowNone": True},
            {"category": 7, "allowCodes": ["P58300002"], "allowNone": False},
            {"category": 8, "allowCodes": ["P58900000"], "allowNone": False},
            {"category": 9, "allowCodes": ["P58900002"], "allowNone": True},
        ],
    },
]


def get_rule_categories():
    """제한 조건이 적용되는 카테고리 번호 집합 반환"""
    categories = set()
    for rule in DETAILED_RULES:
        for action in rule.get("actions", []):
            categories.add(action["category"])
    return categories


