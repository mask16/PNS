/**
 * 웰딩 장비 옵션 시스템 (PNS) - 메인 JavaScript
 */

// 전역 변수
let currentSelections = {};

// DOM 로드 완료 시 실행
document.addEventListener('DOMContentLoaded', function() {
    initializeApp();
});

/**
 * 애플리케이션 초기화
 */
function initializeApp() {
    console.log('웰딩 장비 옵션 시스템 초기화');
    
    // 폼 이벤트 리스너 등록
    setupFormEventListeners();
    
    // 툴팁 초기화
    initializeTooltips();
    
    // 애니메이션 효과 적용
    applyAnimations();
}

/**
 * 폼 이벤트 리스너 설정
 */
function setupFormEventListeners() {
    // 옵션 선택 폼
    const optionsForm = document.getElementById('optionsForm');
    if (optionsForm) {
        optionsForm.addEventListener('change', handleOptionChange);
    }
}

/**
 * 옵션 변경 처리
 */
function handleOptionChange(event) {
    if (event.target.type === 'radio') {
        const categoryNumber = event.target.name.replace('category_', '');
        const selectedValue = event.target.value;
        
        // 선택 상태 업데이트
        currentSelections[categoryNumber] = selectedValue;
        
        // 시각적 피드백 제공
        updateSelectionFeedback(event.target);
        
        console.log(`카테고리 ${categoryNumber}: ${selectedValue} 선택됨`);
    }
}

/**
 * 선택 피드백 업데이트
 */
function updateSelectionFeedback(radioButton) {
    // 이전 선택 해제
    const categoryName = radioButton.name;
    const previousSelected = document.querySelector(`input[name="${categoryName}"]:checked`);
    if (previousSelected && previousSelected !== radioButton) {
        const previousCard = previousSelected.closest('.card');
        if (previousCard) {
            previousCard.classList.remove('selected');
        }
    }
    
    // 현재 선택 표시
    const currentCard = radioButton.closest('.card');
    if (currentCard) {
        currentCard.classList.add('selected');
    }
}

/**
 * 코드 조회 제출 처리
 */
function handleLookupSubmit(event) {
    event.preventDefault();
    
    const codeInput = document.getElementById('codeInput');
    const code = codeInput.value.trim();
    
    if (!code) {
        showAlert('코드를 입력해주세요.', 'warning');
        return;
    }
    
    // 코드 형식 검증
    if (!isValidCodeFormat(code)) {
        showAlert('올바른 코드 형식을 입력해주세요. (예: P51000001)', 'warning');
        return;
    }
    
    performLookup(code);
}

/**
 * 코드 형식 검증
 */
function isValidCodeFormat(code) {
    // 세트코드(P5 + 7자리) 또는 레거시 조합코드(50000xxx / 8자리 숫자)
    return /^(P5\d{7}|50000\d{3}|\d{8})$/i.test(code);
}

/**
 * 코드 조회 수행
 */
function performLookup(code) {
    showLoading('코드를 조회하고 있습니다...');
    
    fetch('/api/lookup', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ code: code })
    })
    .then(response => response.json())
    .then(data => {
        hideLoading();
        
        if (data.success) {
            if (data.found) {
                showLookupResult(data);
            } else {
                showNotFoundResult(data);
            }
        } else {
            showAlert('조회 중 오류가 발생했습니다: ' + data.error, 'error');
        }
    })
    .catch(error => {
        hideLoading();
        console.error('Lookup error:', error);
        showAlert('네트워크 오류가 발생했습니다.', 'error');
    });
}

/**
 * 조회 결과 표시
 */
function showLookupResult(data) {
    const resultArea = document.getElementById('resultArea');
    const resultContent = document.getElementById('resultContent');
    
    if (!resultArea || !resultContent) return;
    
    let html = `
        <div class="alert alert-success fade-in">
            <h6><i class="fas fa-check-circle me-2"></i>코드 조회 성공!</h6>
            <p class="mb-0">코드 <strong>${data.code}</strong>에 대한 정보를 찾았습니다.</p>
        </div>
        
        <div class="row mb-3">
            <div class="col-md-6">
                <h6><i class="fas fa-barcode me-2"></i>조회된 코드</h6>
                <p class="code-display">${data.code}</p>
            </div>
            <div class="col-md-6">
                <h6><i class="fas fa-clock me-2"></i>생성일시</h6>
                <p class="text-muted">${formatDateTime(data.created_at)}</p>
            </div>
            </div>
            
        <hr>
        
        <h6><i class="fas fa-list me-2"></i>선택된 옵션</h6>
        <div class="table-responsive">
            <table class="table table-striped">
                <thead class="table-light">
                    <tr>
                        <th>카테고리</th>
                        <th>선택된 옵션</th>
                        <th>코드</th>
                        <th>설명</th>
                    </tr>
                </thead>
                <tbody>
    `;
    
    // 선택된 옵션 표시 - 안전한 데이터 처리
    for (const [categoryNumber, optionDetail] of Object.entries(data.options)) {
        // optionDetail이 객체인지 확인
        const isObject = typeof optionDetail === 'object' && optionDetail !== null;
        
        let categoryName = `카테고리 ${categoryNumber}`;
        let optionName = '옵션';
        let itemCode = '-';
        let description = '-';
        
        if (isObject) {
            categoryName = optionDetail.category_name || `카테고리 ${categoryNumber}`;
            optionName = optionDetail.option_name || '옵션';
            itemCode = optionDetail.item_code || '-';
            description = optionDetail.description || '-';
        } else {
            optionName = String(optionDetail);
        }
        
        html += `
            <tr>
                <td><strong>[${categoryNumber}] ${categoryName}</strong></td>
                <td><span class="fw-bold text-primary">${optionName}</span></td>
                <td><code class="bg-light px-2 py-1">${itemCode}</code></td>
                <td><small class="text-muted">${description}</small></td>
            </tr>
        `;
    }
        
        html += `
                </tbody>
            </table>
            </div>
        `;
        
    resultContent.innerHTML = html;
    resultArea.style.display = 'block';
    resultArea.classList.add('fade-in');
    
    // 결과 영역으로 스크롤
    resultArea.scrollIntoView({ behavior: 'smooth' });
}

/**
 * 조회 결과 없음 표시
 */
function showNotFoundResult(data) {
    const resultArea = document.getElementById('resultArea');
    const resultContent = document.getElementById('resultContent');
    
    if (!resultArea || !resultContent) return;
    
        let html = `
        <div class="alert alert-warning fade-in">
            <h6><i class="fas fa-exclamation-triangle me-2"></i>코드를 찾을 수 없습니다</h6>
            <p class="mb-0">${data.message}</p>
            </div>
            
        <div class="text-center py-4">
            <i class="fas fa-search fa-3x text-muted mb-3"></i>
            <p class="text-muted">입력한 코드가 데이터베이스에 존재하지 않습니다.</p>
            <p class="text-muted">코드를 다시 확인해주세요.</p>
        </div>
    `;
    
    resultContent.innerHTML = html;
    resultArea.style.display = 'block';
    resultArea.classList.add('fade-in');
    
    // 결과 영역으로 스크롤
    resultArea.scrollIntoView({ behavior: 'smooth' });
}

/**
 * 툴팁 초기화
 */
function initializeTooltips() {
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
}

/**
 * 애니메이션 효과 적용
 */
function applyAnimations() {
    // 카드에 페이드인 효과 적용
    const cards = document.querySelectorAll('.card');
    cards.forEach((card, index) => {
        card.style.opacity = '0';
        card.style.transform = 'translateY(20px)';
        
        setTimeout(() => {
            card.style.transition = 'all 0.5s ease';
            card.style.opacity = '1';
            card.style.transform = 'translateY(0)';
        }, index * 100);
    });
}

/**
 * 로딩 표시
 */
function showLoading(message = '처리 중...') {
    // 기존 로딩 제거
    hideLoading();
    
    const loadingOverlay = document.createElement('div');
    loadingOverlay.className = 'loading-overlay';
    loadingOverlay.id = 'loadingOverlay';
    loadingOverlay.innerHTML = `
        <div class="text-center">
            <div class="spinner-border mb-3" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            <p class="mb-0">${message}</p>
            </div>
        `;
        
    document.body.appendChild(loadingOverlay);
}

/**
 * 로딩 숨기기
 */
function hideLoading() {
    const loadingOverlay = document.getElementById('loadingOverlay');
    if (loadingOverlay) {
        loadingOverlay.remove();
    }
}

/**
 * 알림 표시
 */
function showAlert(message, type = 'info') {
    const alertClass = {
        'success': 'alert-success',
        'error': 'alert-danger',
        'warning': 'alert-warning',
        'info': 'alert-info'
    }[type] || 'alert-info';
    
    const alertHtml = `
        <div class="alert ${alertClass} alert-dismissible fade show" role="alert">
            <i class="fas fa-${getAlertIcon(type)} me-2"></i>
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
    `;
    
    // 기존 알림 제거
    const existingAlerts = document.querySelectorAll('.alert');
    existingAlerts.forEach(alert => alert.remove());
    
    // 새 알림 추가
    const container = document.querySelector('.container');
    if (container) {
        container.insertAdjacentHTML('afterbegin', alertHtml);
        
        // 5초 후 자동 제거
        setTimeout(() => {
            const alert = document.querySelector('.alert');
            if (alert) {
                alert.remove();
            }
        }, 5000);
    }
}

/**
 * 알림 아이콘 가져오기
 */
function getAlertIcon(type) {
    const icons = {
        'success': 'check-circle',
        'error': 'exclamation-triangle',
        'warning': 'exclamation-triangle',
        'info': 'info-circle'
    };
    return icons[type] || 'info-circle';
}

/**
 * 날짜 시간 포맷팅
 */
function formatDateTime(dateString) {
    const date = new Date(dateString);
    return date.toLocaleString('ko-KR', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

/**
 * 유틸리티 함수들
 */

// 전역 함수로 노출
window.showAlert = showAlert;
window.showLoading = showLoading;
window.hideLoading = hideLoading;

/**
 * 모든 옵션 보기 (전역 함수)
 */
function showAllOptions() {
    console.log('=== showAllOptions called ===');
    try {
        const modalElement = document.getElementById('allOptionsModal');
        if (!modalElement) {
            console.error('allOptionsModal element not found!');
            alert('모달을 찾을 수 없습니다');
            return;
        }
        
        const modal = new bootstrap.Modal(modalElement);
        modal.show();
        console.log('Modal shown successfully');
        
        // 약간의 딜레이를 주고 데이터 로드
        setTimeout(loadAllOptionsData, 300);
    } catch(e) {
        console.error('Error in showAllOptions:', e);
        alert('오류: ' + e.toString());
    }
}

/**
 * 모든 옵션 데이터 로드
 */
function loadAllOptionsData() {
    console.log('=== loadAllOptionsData called ===');
    
    // 절대 URL 구성
    const protocol = window.location.protocol;
    const host = window.location.host;
    const url = protocol + '//' + host + '/api/all-options';
    
    console.log('Full URL:', url);
    console.log('Window location:', window.location.href);
    
    // 캐시 회피를 위해 타임스탬프 추가
    const timestamp = Date.now();
    const urlWithCache = url + '?t=' + timestamp;
    
    fetch(urlWithCache, {
        method: 'GET',
        headers: {
            'Accept': 'application/json',
            'Cache-Control': 'no-cache'
        }
    })
        .then(response => {
            console.log('Response received');
            console.log('Status:', response.status);
            console.log('Status text:', response.statusText);
            console.log('OK:', response.ok);
            
            if (!response.ok) {
                console.error('Response not OK!');
                return response.text().then(text => {
                    console.error('Response text:', text);
                    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                });
            }
            return response.json();
        })
        .then(data => {
            console.log('Data received and parsed');
            console.log('Data:', data);
            
            if (data && typeof data === 'object' && data.success && data.options) {
                console.log('Data is valid');
                displayAllOptionsData(data.options);
            } else {
                console.error('Invalid data format:', data);
                showAllOptionsError('유효하지 않은 데이터 형식입니다');
            }
        })
        .catch(error => {
            console.error('=== FETCH ERROR ===');
            console.error('Error message:', error.message);
            console.error('Error name:', error.name);
            console.error('Error:', error);
            showAllOptionsError('API 호출 실패: ' + error.message);
        });
}

/**
 * 모든 옵션 데이터 표시
 */
function displayAllOptionsData(optionsData) {
    console.log('=== displayAllOptionsData called ===');
    const modalBody = document.getElementById('allOptionsBody');
    
    if (!modalBody) {
        console.error('allOptionsBody element not found!');
        return;
    }
    
    let html = '<div class="row">';
    let categoryCount = 0;
    
    for (let catNum = 1; catNum <= 11; catNum++) {
        if (optionsData && optionsData[catNum]) {
            categoryCount++;
            const category = optionsData[catNum];
            const items = category.items || [];
            
            console.log(`Processing category ${catNum}: ${category.name} with ${items.length} items`);
            
            html += `
                <div class="col-md-6 mb-3">
                    <div class="card h-100">
                        <div class="card-header bg-burgundy">
                            <h6 class="mb-0">[${catNum}] ${category.name}</h6>
                        </div>
                        <div class="card-body" style="max-height: 400px; overflow-y: auto;">
                            <table class="table table-sm table-striped">
                                <tbody>
            `;
            
            if (items && items.length > 0) {
                items.forEach((item, idx) => {
                    html += `
                        <tr>
                            <td style="font-size: 0.85rem;">
                                <strong>${item.name || '-'}</strong><br>
                                <code>${item.code || '-'}</code><br>
                                <small class="text-muted">${(item.description !== undefined && item.description !== null && item.description !== '') ? item.description : ''}</small>
                            </td>
                        </tr>
                    `;
                });
            } else {
                html += '<tr><td class="text-muted text-center">항목 없음</td></tr>';
            }
            
            html += `
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            `;
        }
    }
    
    html += '</div>';
    
    console.log(`Total categories displayed: ${categoryCount}`);
    
    if (categoryCount === 0) {
        console.error('No categories found to display');
        showAllOptionsError('표시할 카테고리가 없습니다');
        return;
    }
    
    try {
        modalBody.innerHTML = html;
        console.log('HTML rendered successfully');
    } catch (e) {
        console.error('Error rendering HTML:', e);
        showAllOptionsError('HTML 렌더링 실패: ' + e.message);
    }
}

/**
 * 옵션 오류 표시
 */
function showAllOptionsError(message) {
    console.error('=== showAllOptionsError called ===');
    console.error('Error message:', message);
    
    const modalBody = document.getElementById('allOptionsBody');
    if (modalBody) {
        try {
            modalBody.innerHTML = `
                <div class="alert alert-danger" role="alert">
                    <h5>오류 발생</h5>
                    <p>${message}</p>
                    <hr>
                    <small>브라우저 콘솔(F12)에서 더 자세한 정보를 확인하세요.</small>
                </div>
            `;
        } catch (e) {
            console.error('Error setting error HTML:', e);
        }
    } else {
        console.error('modalBody not found!');
    }
}

// 전역 함수로 노출
window.showAllOptions = showAllOptions;