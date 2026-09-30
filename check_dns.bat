@echo off
chcp 65001 >nul
echo ========================================
echo DNS 설정 확인 스크립트
echo ========================================
echo.

set DOMAIN=pnsoptions.com
set EXPECTED_IP=43.201.229.107

echo [1] DNS 조회 중: %DOMAIN%
echo.
nslookup %DOMAIN%
echo.

echo [2] Ping 테스트
echo.
ping -n 1 %DOMAIN% 2>nul
if errorlevel 1 (
    echo [경고] %DOMAIN%에 연결할 수 없습니다.
    echo DNS가 아직 전파되지 않았거나 설정되지 않았을 수 있습니다.
) else (
    echo [성공] %DOMAIN%에 연결되었습니다.
)
echo.

echo [3] IP 주소 직접 접속 테스트
echo.
echo 테스트 중: http://%EXPECTED_IP%:5000
curl -s -o nul -w "HTTP 상태 코드: %%{http_code}\n" http://%EXPECTED_IP%:5000 2>nul
if errorlevel 1 (
    echo [경고] 서버에 연결할 수 없습니다.
    echo Flask 애플리케이션이 실행 중인지 확인하세요.
) else (
    echo [성공] 서버가 정상적으로 응답합니다.
)
echo.

echo [4] 포트 80 확인 (Nginx)
echo.
netstat -an | findstr :80 >nul
if errorlevel 1 (
    echo [정보] 포트 80이 열려있지 않습니다. (Nginx 미실행 가능)
) else (
    echo [정보] 포트 80이 사용 중입니다.
)
echo.

echo [5] 포트 5000 확인 (Flask)
echo.
netstat -an | findstr :5000 >nul
if errorlevel 1 (
    echo [경고] 포트 5000이 열려있지 않습니다.
    echo Flask 애플리케이션을 실행하세요.
) else (
    echo [성공] 포트 5000이 사용 중입니다. (Flask 실행 중)
)
echo.

echo ========================================
echo 요약
echo ========================================
echo.
echo 도메인: %DOMAIN%
echo 예상 IP: %EXPECTED_IP%
echo.
echo 현재 상태:
echo   - DNS 전파: nslookup 결과 확인 필요
echo   - 서버 응답: http://%EXPECTED_IP%:5000
echo.
echo 다음 단계:
echo   1. DNS가 전파되지 않았다면 DNS_SETUP_GUIDE.md 참조
echo   2. 서버가 응답하지 않으면 Flask 앱 실행 확인
echo   3. 임시로 IP 주소로 접속: http://%EXPECTED_IP%:5000
echo.
echo ========================================
pause


