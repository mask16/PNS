@echo off
chcp 65001 >nul
echo ========================================
echo 웰딩 장비 옵션 시스템 (PNS)
echo Nginx + Flask 시작 스크립트
echo ========================================
echo.

REM Nginx 경로 설정 (실제 설치 경로로 변경하세요)
set NGINX_PATH=C:\nginx
set NGINX_EXE=%NGINX_PATH%\nginx.exe

REM Nginx가 설치되어 있는지 확인
if not exist "%NGINX_EXE%" (
    echo [경고] Nginx를 찾을 수 없습니다: %NGINX_EXE%
    echo Nginx 설치 경로를 확인하거나 nginx_setup_guide.md를 참조하세요.
    echo.
    echo Flask만 실행합니다...
    echo.
    goto :start_flask
)

REM Nginx 설정 파일 테스트
echo Nginx 설정 파일 확인 중...
"%NGINX_EXE%" -t
if errorlevel 1 (
    echo [오류] Nginx 설정 파일에 오류가 있습니다.
    echo 설정 파일을 확인하세요: %NGINX_PATH%\conf\nginx.conf
    pause
    exit /b 1
)

REM 기존 Nginx 프로세스 확인 및 중지
echo.
echo 기존 Nginx 프로세스 확인 중...
tasklist /fi "imagename eq nginx.exe" 2>nul | find /i "nginx.exe" >nul
if not errorlevel 1 (
    echo 기존 Nginx 프로세스를 중지합니다...
    "%NGINX_EXE%" -s stop
    timeout /t 2 /nobreak >nul
)

REM Nginx 시작
echo.
echo Nginx 시작 중...
cd /d "%NGINX_PATH%"
start "Nginx Server" "%NGINX_EXE%"
timeout /t 2 /nobreak >nul

REM Nginx 실행 확인
tasklist /fi "imagename eq nginx.exe" 2>nul | find /i "nginx.exe" >nul
if errorlevel 1 (
    echo [경고] Nginx 시작에 실패했습니다.
    echo 관리자 권한으로 실행해보세요.
    echo.
) else (
    echo [성공] Nginx가 시작되었습니다.
    echo.
)

:start_flask
REM Flask 애플리케이션 디렉토리로 이동
cd /d "%~dp0"

REM 필요한 패키지 설치 확인
echo 필요한 패키지 설치 확인 중...
pip install -r requirements.txt >nul 2>&1

REM Flask 애플리케이션 실행
echo.
echo Flask 애플리케이션을 시작합니다...
echo ========================================
echo 접속 주소:
echo   - 로컬: http://127.0.0.1:5000
echo   - 도메인: http://pnsoptions.com (Nginx 사용 시)
echo   - 직접: http://pnsoptions.com:5000
echo ========================================
echo.
echo 종료하려면 Ctrl+C를 누르세요.
echo ========================================
echo.

python app.py

REM Flask 종료 후 Nginx도 중지
echo.
echo Flask 애플리케이션이 종료되었습니다.
echo Nginx를 중지하시겠습니까? (Y/N)
set /p stop_nginx=

if /i "%stop_nginx%"=="Y" (
    if exist "%NGINX_EXE%" (
        echo Nginx 중지 중...
        "%NGINX_EXE%" -s stop
        echo Nginx가 중지되었습니다.
    )
)

pause


