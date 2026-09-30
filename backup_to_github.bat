@echo off
echo ========================================
echo GitHub 백업 시작
echo ========================================
echo.

REM Git 저장소 초기화
echo [1/6] Git 저장소 초기화 중...
git init
if %errorlevel% neq 0 (
    echo 오류: Git이 설치되어 있지 않거나 PATH에 추가되지 않았습니다.
    echo PowerShell을 재시작하거나 Git을 설치해주세요.
    pause
    exit /b 1
)

REM 원격 저장소 확인 및 설정
echo [2/6] 원격 저장소 설정 중...
git remote remove origin 2>nul
git remote add origin https://github.com/mask16/pnsweb.git

REM 모든 파일 추가
echo [3/6] 파일 추가 중...
git add .

REM 커밋
echo [4/6] 커밋 생성 중...
git commit -m "Initial commit - PNS 웰딩 장비 옵션 시스템 v4.0 (세트가격조회/입력, 일괄펼치기/접기 기능 포함)"

REM 기본 브랜치를 main으로 설정
echo [5/6] 브랜치 설정 중...
git branch -M main

REM 푸시
echo [6/6] GitHub에 푸시 중...
echo.
echo GitHub 인증이 필요할 수 있습니다.
echo 사용자명과 Personal Access Token을 입력해주세요.
echo.
git push -u origin main

if %errorlevel% equ 0 (
    echo.
    echo ========================================
    echo 백업 완료!
    echo ========================================
) else (
    echo.
    echo ========================================
    echo 푸시 실패. 인증 정보를 확인해주세요.
    echo ========================================
)

pause

