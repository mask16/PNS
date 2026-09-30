#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
모든 세트코드 가격을 일괄 업데이트하는 스크립트
"""

import sqlite3
import sys

def update_all_prices():
    """모든 세트코드의 가격을 20,000,000원으로 업데이트"""
    db_path = "welding_options.db"
    price = 20000000  # 20,000,000원
    
    try:
        # 데이터베이스 연결
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 먼저 price 컬럼이 있는지 확인
        cursor.execute("PRAGMA table_info(selected_combinations)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'price' not in columns:
            print("price 컬럼이 없습니다. 컬럼을 추가합니다...")
            cursor.execute('ALTER TABLE selected_combinations ADD COLUMN price REAL')
            conn.commit()
            print("price 컬럼이 추가되었습니다.")
        
        # set_code가 있는 모든 레코드 조회
        cursor.execute('''
            SELECT DISTINCT set_code
            FROM selected_combinations
            WHERE set_code IS NOT NULL AND set_code != ''
        ''')
        
        set_codes = cursor.fetchall()
        total_count = len(set_codes)
        
        if total_count == 0:
            print("업데이트할 세트코드가 없습니다.")
            conn.close()
            return
        
        print(f"총 {total_count}개의 세트코드를 찾았습니다.")
        print(f"모든 세트코드의 가격을 {price:,}원으로 업데이트합니다...")
        print("-" * 60)
        
        # 모든 세트코드의 가격 업데이트
        updated_count = 0
        for row in set_codes:
            set_code = row['set_code']
            cursor.execute('''
                UPDATE selected_combinations
                SET price = ?
                WHERE set_code = ?
            ''', (price, set_code))
            
            affected = cursor.rowcount
            updated_count += affected
            print(f"세트코드 {set_code}: {affected}개 레코드 업데이트 완료")
        
        conn.commit()
        
        print("-" * 60)
        print(f"업데이트 완료!")
        print(f"총 {updated_count}개의 레코드가 업데이트되었습니다.")
        print(f"모든 세트코드의 가격이 {price:,}원으로 설정되었습니다.")
        
        conn.close()
        
    except sqlite3.Error as e:
        print(f"데이터베이스 오류: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"오류 발생: {e}")
        sys.exit(1)

if __name__ == '__main__':
    print("=" * 60)
    print("세트코드 가격 일괄 업데이트 스크립트")
    print("=" * 60)
    print()
    print("모든 세트코드의 가격을 20,000,000원으로 업데이트합니다...")
    print()
    
    update_all_prices()
