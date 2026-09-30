@echo off
chcp 65001 >nul
echo ========================================
echo 웰딩 장비 옵션 선택 시스템 (PNS) 시작
echo ========================================
echo.

REM 현재 디렉토리를 배치 파일이 있는 위치로 설정
cd /d "%~dp0"

REM Python이 설치되어 있는지 확인
python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo Python 3.7 이상을 설치해주세요.
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Python 버전 확인 중...
python --version

REM 가상환경이 있는지 확인하고 활성화
if exist "venv\Scripts\activate.bat" (
    echo 가상환경 활성화 중...
    call venv\Scripts\activate.bat
) else (
    echo 가상환경이 없습니다. 시스템 Python을 사용합니다.
)

REM 필요한 패키지 설치 확인
echo.
echo 필요한 패키지 설치 확인 중...
pip install -r requirements.txt

REM Flask 애플리케이션 실행
echo.
echo Flask 애플리케이션을 시작합니다...
echo 브라우저에서 http://localhost:5000 으로 접속하세요.
echo.
echo 종료하려면 Ctrl+C를 누르세요.
echo ========================================
echo.

python app.py

REM 오류 발생 시 일시정지
if errorlevel 1 (
    echo.
    echo [오류] 애플리케이션 실행 중 오류가 발생했습니다.
    pause
)
