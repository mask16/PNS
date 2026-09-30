/**
 * 하나은행 환율 공통 유틸
 * - 헤더 표시
 * - 페이지별 환율 자동 적용/선택
 */
(function (window) {
    const DEFAULT_RATES = {
        usd_krw: 1400,
        eur_krw: 1600,
        eur_usd: 1.15,
        krw: 1
    };

    let cachedRates = null;
    let loadingPromise = null;

    function formatNumber(value, digits) {
        const n = Number(value);
        if (!Number.isFinite(n)) return '-';
        return n.toLocaleString('en-US', {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        });
    }

    async function fetchExchangeRates(forceRefresh) {
        if (!forceRefresh && cachedRates) {
            return cachedRates;
        }
        if (!forceRefresh && loadingPromise) {
            return loadingPromise;
        }

        const url = forceRefresh ? '/api/exchange_rates?refresh=1' : '/api/exchange_rates';
        loadingPromise = fetch(url)
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (data && data.success !== false && (data.usd_krw || data.eur_krw)) {
                    cachedRates = Object.assign({}, DEFAULT_RATES, data, { success: true });
                } else {
                    cachedRates = Object.assign({}, DEFAULT_RATES, {
                        success: false,
                        error: (data && data.error) || '환율 조회 실패',
                        source: 'fallback'
                    });
                }
                loadingPromise = null;
                return cachedRates;
            })
            .catch(function (err) {
                loadingPromise = null;
                cachedRates = Object.assign({}, DEFAULT_RATES, {
                    success: false,
                    error: String(err),
                    source: 'fallback'
                });
                return cachedRates;
            });

        return loadingPromise;
    }

    function renderHeaderRates(rates) {
        const box = document.getElementById('headerExchangeRates');
        if (!box) return;

        const usd = formatNumber(rates.usd_krw, 2);
        const eur = formatNumber(rates.eur_krw, 2);
        const eurUsd = formatNumber(rates.eur_usd, 4);
        const dateText = rates.base_date || '-';
        const source = rates.source || 'Hana';
        const announced = rates.announced_at || '';

        box.innerHTML =
            '<div class="exchange-rate-widget" title="' + source + ' · ' + dateText + (announced ? (' · ' + announced) : '') + '">' +
                '<div class="er-rates">' +
                    '<div class="er-chip">' +
                        '<span class="er-code">USD</span>' +
                        '<span class="er-value">₩' + usd + '</span>' +
                    '</div>' +
                    '<div class="er-chip">' +
                        '<span class="er-code">EUR</span>' +
                        '<span class="er-value">₩' + eur + '</span>' +
                    '</div>' +
                    '<div class="er-chip">' +
                        '<span class="er-code">EUR/USD</span>' +
                        '<span class="er-value">' + eurUsd + '</span>' +
                    '</div>' +
                '</div>' +
                '<button type="button" class="er-refresh" id="headerFxRefreshBtn" title="환율 새로고침" aria-label="환율 새로고침">' +
                    '<i class="fas fa-sync-alt"></i>' +
                '</button>' +
            '</div>';

        const refreshBtn = document.getElementById('headerFxRefreshBtn');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', function () {
                refreshBtn.classList.add('is-spinning');
                refreshBtn.disabled = true;
                fetchExchangeRates(true).then(function (r) {
                    renderHeaderRates(r);
                    window.dispatchEvent(new CustomEvent('exchangeRatesUpdated', { detail: r }));
                }).finally(function () {
                    refreshBtn.classList.remove('is-spinning');
                    refreshBtn.disabled = false;
                });
            });
        }
    }

    function buildSelectableRateTable(containerId, options) {
        const container = document.getElementById(containerId);
        if (!container) return;

        const opts = options || {};
        const selected = opts.selectedCurrency || 'EUR';
        const lang = (opts.lang || 'kor').toLowerCase();
        const title = lang === 'eng' ? 'Exchange Rates (Hana Bank)' : '환율 선택 (하나은행)';
        const applyLabel = lang === 'eng' ? 'Apply' : '적용';

        container.innerHTML =
            '<div class="card border-info mb-3" id="' + containerId + 'Card">' +
                '<div class="card-header bg-light d-flex justify-content-between align-items-center">' +
                    '<strong><i class="fas fa-won-sign me-2"></i>' + title + '</strong>' +
                    '<small class="text-muted" id="' + containerId + 'Meta"></small>' +
                '</div>' +
                '<div class="card-body p-2">' +
                    '<div class="table-responsive">' +
                        '<table class="table table-sm table-bordered mb-2">' +
                            '<thead class="table-light">' +
                                '<tr>' +
                                    '<th class="text-center">Currency</th>' +
                                    '<th class="text-end">Rate</th>' +
                                    '<th class="text-center" style="width:90px;">Select</th>' +
                                '</tr>' +
                            '</thead>' +
                            '<tbody id="' + containerId + 'Body">' +
                                '<tr><td colspan="3" class="text-center text-muted">Loading...</td></tr>' +
                            '</tbody>' +
                        '</table>' +
                    '</div>' +
                '</div>' +
            '</div>';

        fetchExchangeRates(false).then(function (rates) {
            const meta = document.getElementById(containerId + 'Meta');
            if (meta) {
                meta.textContent = (rates.base_date || '') + (rates.announced_at ? (' / ' + rates.announced_at) : '');
            }

            const rows = [
                { code: 'USD', label: 'USD / KRW', value: rates.usd_krw, digits: 2 },
                { code: 'EUR', label: 'EUR / KRW', value: rates.eur_krw, digits: 2 },
                { code: 'EURUSD', label: 'EUR / USD', value: rates.eur_usd, digits: 4 },
                { code: 'KRW', label: 'KRW', value: 1, digits: 0 }
            ];

            const body = document.getElementById(containerId + 'Body');
            if (!body) return;
            body.innerHTML = rows.map(function (row) {
                const checked = (selected === row.code || (selected === 'USD' && row.code === 'EURUSD')) ? 'checked' : '';
                return '<tr data-fx-code="' + row.code + '" data-fx-value="' + row.value + '" style="cursor:pointer;">' +
                    '<td class="text-center"><strong>' + row.label + '</strong></td>' +
                    '<td class="text-end">' + formatNumber(row.value, row.digits) + '</td>' +
                    '<td class="text-center">' +
                        '<input type="radio" name="' + containerId + 'FxSelect" value="' + row.code + '" ' + checked + '>' +
                    '</td>' +
                '</tr>';
            }).join('') +
            '<tr><td colspan="3" class="text-end">' +
                '<button type="button" class="btn btn-sm btn-primary" id="' + containerId + 'ApplyBtn">' +
                    '<i class="fas fa-check me-1"></i>' + applyLabel +
                '</button>' +
            '</td></tr>';

            Array.prototype.forEach.call(body.querySelectorAll('tr[data-fx-code]'), function (tr) {
                tr.addEventListener('click', function () {
                    const radio = tr.querySelector('input[type="radio"]');
                    if (radio) radio.checked = true;
                });
            });

            const applyBtn = document.getElementById(containerId + 'ApplyBtn');
            if (applyBtn) {
                applyBtn.addEventListener('click', function () {
                    const selectedRadio = body.querySelector('input[type="radio"]:checked');
                    if (!selectedRadio) return;
                    const code = selectedRadio.value;
                    const payload = {
                        currency: code === 'EURUSD' ? 'USD' : code,
                        rateCode: code,
                        usd_krw: rates.usd_krw,
                        eur_krw: rates.eur_krw,
                        eur_usd: rates.eur_usd,
                        rates: rates
                    };
                    if (typeof opts.onApply === 'function') {
                        opts.onApply(payload);
                    }
                    window.dispatchEvent(new CustomEvent('exchangeRateSelected', { detail: payload }));
                });
            }
        });
    }

    function getRateForCurrency(rates, currency) {
        const cur = (currency || 'EUR').toUpperCase();
        if (cur === 'USD') return Number(rates.eur_usd) || DEFAULT_RATES.eur_usd;
        if (cur === 'KRW') return Number(rates.eur_krw) || DEFAULT_RATES.eur_krw;
        return 1;
    }

    async function initHeaderExchangeRates() {
        const rates = await fetchExchangeRates(false);
        renderHeaderRates(rates);
        window.dispatchEvent(new CustomEvent('exchangeRatesUpdated', { detail: rates }));
        return rates;
    }

    window.PnsExchangeRates = {
        fetch: fetchExchangeRates,
        initHeader: initHeaderExchangeRates,
        buildSelectableTable: buildSelectableRateTable,
        getRateForCurrency: getRateForCurrency,
        formatNumber: formatNumber,
        defaults: DEFAULT_RATES
    };

    document.addEventListener('DOMContentLoaded', function () {
        initHeaderExchangeRates();
    });
})(window);
