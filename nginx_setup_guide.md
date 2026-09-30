# Nginx 리버스 프록시 설정 가이드

## 개요
이 가이드는 `pnsoptions.com` 도메인으로 포트 없이 접속할 수 있도록 Nginx를 설정하는 방법을 설명합니다.

## 1. Nginx 설치 (Windows)

### 방법 1: 공식 웹사이트에서 다운로드
1. http://nginx.org/en/download.html 방문
2. Windows 버전 다운로드 (예: nginx/Windows-1.24.0)
3. 압축 해제 (예: `C:\nginx`)

### 방법 2: Chocolatey 사용
```powershell
choco install nginx
```

## 2. 설정 파일 복사

1. 이 디렉토리의 `nginx.conf` 파일을 Nginx 설치 디렉토리의 `conf` 폴더로 복사
2. 또는 기존 `nginx.conf` 파일을 백업하고 이 파일의 내용을 추가

## 3. Nginx 설정 적용

### 설정 파일 경로 확인
- 기본 경로: `C:\nginx\conf\nginx.conf`
- 또는 설치한 경로의 `conf\nginx.conf`

### 설정 파일 편집
1. `nginx.conf` 파일을 텍스트 에디터로 열기
2. 기존 `server` 블록이 있다면 주석 처리
3. 이 가이드의 `nginx.conf` 내용을 추가하거나 include로 포함

### 경로 수정
`nginx.conf` 파일에서 다음 경로를 실제 경로로 수정:
```nginx
location /static/ {
    alias D:/Warehouse/wwwroot/pns/static/;  # 실제 경로로 변경
}
```

## 4. Nginx 실행

### 명령 프롬프트 또는 PowerShell에서:
```powershell
# Nginx 디렉토리로 이동
cd C:\nginx

# Nginx 시작
start nginx

# 또는
nginx.exe
```

### 서비스로 등록 (선택사항)
```powershell
# 관리자 권한으로 실행
# Nginx를 Windows 서비스로 등록
```

## 5. 방화벽 설정

### Windows 방화벽에서 포트 80 열기
1. Windows 방화벽 고급 설정 열기
2. 인바운드 규칙 → 새 규칙
3. 포트 선택 → TCP → 특정 로컬 포트: 80
4. 연결 허용 선택
5. 모든 프로필 적용
6. 이름: "Nginx HTTP"

## 6. DNS 설정 확인

도메인 등록 업체에서 다음 설정 확인:
- A 레코드: `pnsoptions.com` → `43.201.229.107`
- A 레코드: `www.pnsoptions.com` → `43.201.229.107` (선택사항)

## 7. 테스트

### 로컬 테스트
```powershell
# hosts 파일에 추가 (C:\Windows\System32\drivers\etc\hosts)
# 관리자 권한으로 편집:
127.0.0.1 pnsoptions.com
```

브라우저에서 `http://pnsoptions.com` 접속 테스트

### 외부 테스트
DNS 전파 후 (보통 몇 분~24시간):
- `http://pnsoptions.com` 접속 테스트
- `http://pnsoptions.com:5000` (직접 접속)도 여전히 작동해야 함

## 8. Nginx 관리 명령어

```powershell
# Nginx 시작
nginx.exe

# Nginx 중지
nginx.exe -s stop

# Nginx 재시작 (설정 변경 후)
nginx.exe -s reload

# 설정 파일 테스트
nginx.exe -t

# Nginx 상태 확인
tasklist /fi "imagename eq nginx.exe"
```

## 9. 로그 확인

- 접근 로그: `C:\nginx\logs\pns_access.log`
- 오류 로그: `C:\nginx\logs\pns_error.log`

## 10. 문제 해결

### 포트 80이 이미 사용 중인 경우
```powershell
# 포트 80을 사용하는 프로세스 확인
netstat -ano | findstr :80

# 다른 웹 서버(IIS 등)가 실행 중이면 중지
```

### Nginx가 시작되지 않는 경우
1. `nginx.exe -t`로 설정 파일 문법 확인
2. 로그 파일(`error.log`) 확인
3. 관리자 권한으로 실행 시도

### 502 Bad Gateway 오류
1. Flask 애플리케이션이 실행 중인지 확인 (`http://127.0.0.1:5000`)
2. `proxy_pass` URL이 올바른지 확인
3. 방화벽에서 로컬 연결 허용 확인

## 11. HTTPS 설정 (선택사항)

SSL 인증서가 있는 경우:
1. `nginx.conf`의 HTTPS 설정 주석 해제
2. SSL 인증서 경로 설정
3. HTTP에서 HTTPS로 리다이렉트 설정 활성화

### Let's Encrypt 사용 (Windows)
- Win-ACME 도구 사용: https://www.win-acme.com/

## 참고사항

- Nginx와 Flask 앱이 모두 실행 중이어야 합니다
- 포트 80은 관리자 권한이 필요할 수 있습니다
- 프로덕션 환경에서는 `debug=False`로 설정하는 것을 권장합니다


