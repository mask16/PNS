# DNS 설정 및 문제 해결 가이드

## 현재 오류: DNS_PROBE_FINISHED_NXDOMAIN

이 오류는 `pnsoptions.com` 도메인이 DNS에 등록되지 않았거나 아직 전파되지 않았다는 의미입니다.

## 해결 방법

### 방법 1: DNS 등록 확인 및 설정

#### 1. 도메인 등록 업체 확인
- 도메인을 어디서 구매했는지 확인 (가비아, 후이즈, GoDaddy 등)
- 도메인 관리 패널에 로그인

#### 2. DNS 레코드 추가
도메인 관리 패널에서 다음 A 레코드를 추가:

```
타입: A
호스트: @ (또는 비워두기)
값/주소: 43.201.229.107
TTL: 3600 (또는 기본값)
```

www 서브도메인도 원하면:
```
타입: A
호스트: www
값/주소: 43.201.229.107
TTL: 3600
```

#### 3. DNS 전파 대기
- DNS 변경 후 전파까지 보통 **5분~24시간** 소요
- 글로벌 DNS 서버에 전파되는 시간이 다를 수 있음

#### 4. DNS 전파 확인 방법
```powershell
# Windows 명령 프롬프트에서
nslookup pnsoptions.com

# 또는
ping pnsoptions.com
```

결과가 `43.201.229.107`을 반환하면 전파 완료

---

### 방법 2: 로컬 테스트용 hosts 파일 설정 (임시 해결책)

도메인 등록 전이나 전파 대기 중 로컬에서 테스트하려면:

#### 1. hosts 파일 열기
- 경로: `C:\Windows\System32\drivers\etc\hosts`
- **관리자 권한**으로 메모장 또는 텍스트 에디터 실행 필요

#### 2. 관리자 권한으로 메모장 실행
```powershell
# PowerShell (관리자 권한)
notepad C:\Windows\System32\drivers\etc\hosts
```

#### 3. hosts 파일에 추가
파일 끝에 다음 줄 추가:
```
43.201.229.107    pnsoptions.com
43.201.229.107    www.pnsoptions.com
```

#### 4. 저장 및 확인
- 파일 저장
- 브라우저 캐시 지우기 (Ctrl+Shift+Delete)
- `http://pnsoptions.com` 접속 테스트

**주의**: hosts 파일은 해당 컴퓨터에서만 작동합니다. 다른 컴퓨터에서는 여전히 DNS 설정이 필요합니다.

---

### 방법 3: IP 주소로 직접 접속 (즉시 사용 가능)

도메인 설정이 완료될 때까지 IP 주소로 직접 접속:

```
http://43.201.229.107:5000
```

이 방법은 즉시 작동하며, 도메인 설정과 무관합니다.

---

## DNS 전파 확인 도구

### 온라인 도구
1. **whatsmydns.net**: https://www.whatsmydns.net/#A/pnsoptions.com
   - 전 세계 DNS 서버에서 도메인 확인 상태 확인

2. **dnschecker.org**: https://dnschecker.org/#A/pnsoptions.com
   - DNS 전파 상태 실시간 확인

### 명령줄 도구
```powershell
# Windows
nslookup pnsoptions.com
ping pnsoptions.com

# Linux/Mac
dig pnsoptions.com
host pnsoptions.com
```

---

## 단계별 체크리스트

### ✅ DNS 설정 확인
- [ ] 도메인 등록 업체에 로그인
- [ ] DNS 관리 패널 접근
- [ ] A 레코드 추가: `@` → `43.201.229.107`
- [ ] (선택) www 서브도메인 추가

### ✅ 서버 확인
- [ ] 서버가 실행 중인지 확인 (`http://43.201.229.107:5000`)
- [ ] 방화벽에서 포트 80, 5000 열려 있는지 확인
- [ ] Nginx가 실행 중인지 확인 (포트 80 사용 시)

### ✅ DNS 전파 확인
- [ ] `nslookup pnsoptions.com` 실행
- [ ] 온라인 DNS 체크 도구 사용
- [ ] 여러 지역에서 확인 (전파 시간 차이)

### ✅ 로컬 테스트 (선택)
- [ ] hosts 파일 설정 (로컬 테스트용)
- [ ] 브라우저 캐시 지우기
- [ ] `http://pnsoptions.com` 접속 테스트

---

## 일반적인 문제 해결

### 문제 1: DNS는 설정했는데 여전히 안 됨
**해결책**:
- DNS 전파 대기 (최대 24시간)
- 브라우저 캐시 및 DNS 캐시 지우기
- 다른 네트워크에서 테스트

### 문제 2: 일부 지역에서만 작동
**원인**: DNS 전파가 아직 완료되지 않음
**해결책**: 시간이 지나면 자동으로 해결됨

### 문제 3: IP는 되는데 도메인은 안 됨
**원인**: DNS 설정 문제 또는 전파 미완료
**해결책**: 
- DNS 설정 재확인
- `nslookup`으로 DNS 응답 확인
- 도메인 등록 업체에 문의

### 문제 4: 포트 80으로 접속 안 됨
**원인**: Nginx가 실행되지 않았거나 방화벽 문제
**해결책**:
- Nginx 실행 확인
- 방화벽에서 포트 80 허용 확인
- `http://43.201.229.107` (포트 80) 직접 테스트

---

## 빠른 테스트 명령어

```powershell
# 1. DNS 확인
nslookup pnsoptions.com

# 2. IP로 직접 접속 테스트
curl http://43.201.229.107:5000

# 3. 포트 80 확인 (Nginx)
curl http://43.201.229.107

# 4. 방화벽 포트 확인
netstat -an | findstr :80
netstat -an | findstr :5000
```

---

## 참고사항

- **도메인 미등록**: 도메인을 아직 구매하지 않았다면 먼저 도메인을 등록해야 합니다.
- **DNS 제공업체**: 일부 도메인 등록 업체는 DNS 서비스를 별도로 제공합니다.
- **TTL 값**: DNS 변경 시 TTL 값을 낮게 설정하면 전파가 빨라집니다 (예: 300초).

---

## 즉시 사용 가능한 대안

도메인 설정이 완료될 때까지:

1. **IP 주소 직접 사용**: `http://43.201.229.107:5000`
2. **로컬 hosts 파일**: 로컬 컴퓨터에서만 `pnsoptions.com` 사용 가능
3. **임시 도메인**: 무료 동적 DNS 서비스 사용 (예: DuckDNS, No-IP)


