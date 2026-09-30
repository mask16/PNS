#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
모든 세트가격 삭제 스크립트
"""

import sqlite3
import sys
import os

# 데이터베이스 경로
db_path = "welding_options.db"

if not os.path.exists(db_path):
    print(f"오류: 데이터베이스 파일을 찾을 수 없습니다: {db_path}")
    sys.exit(1)

try:
    # 데이터베이스 연결
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
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
    
    # 변경사항 커밋
    conn.commit()
    
    print("=" * 60)
    print("모든 세트가격 삭제 완료")
    print("=" * 60)
    print(f"세트코드 가격 삭제: {updated_count}개")
    print(f"옵션별 가격 삭제: {deleted_option_prices_count}개")
    print("=" * 60)
    
    conn.close()
    
except sqlite3.Error as e:
    print(f"데이터베이스 오류: {e}")
    sys.exit(1)
except Exception as e:
    print(f"오류 발생: {e}")
    sys.exit(1)







