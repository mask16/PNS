/**
 * 제품명 약어(코드명) 부가 설명 - 수출견적서 제품 조회 모달
 */
(function () {
    'use strict';

    /** 약어 → 영문 풀네임 (필요 시 항목 추가) */
    const TERM_GLOSSARY = {
        WFR: 'Wire Feeding Roller',
        WFS: 'Wire Feeding System',
        CT: 'Contact Tip',
        NL: 'Nozzle',
        TIG: 'Tungsten Inert Gas',
        MIG: 'Metal Inert Gas',
        MAG: 'Metal Active Gas',
        MMA: 'Manual Metal Arc',
        ERC: 'Earth Return Cable',
        HF: 'High Frequency',
        REMOTE: 'Remote Control',
    };

    function escapeHtml(text) {
        const d = document.createElement('div');
        d.textContent = text == null ? '' : String(text);
        return d.innerHTML;
    }

    function escapeRegex(text) {
        return String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    function findTermsInName(name) {
        if (!name) return [];
        const found = [];
        const sortedKeys = Object.keys(TERM_GLOSSARY).sort(function (a, b) {
            return b.length - a.length;
        });
        const seen = new Set();
        sortedKeys.forEach(function (term) {
            const re = new RegExp('\\b' + escapeRegex(term) + '\\b', 'i');
            if (re.test(name) && !seen.has(term.toUpperCase())) {
                seen.add(term.toUpperCase());
                found.push({ term: term, definition: TERM_GLOSSARY[term] });
            }
        });
        return found;
    }

    function highlightTerms(name, terms) {
        let html = escapeHtml(name || '-');
        if (!terms.length) return html;
        terms.forEach(function (item) {
            const re = new RegExp('\\b(' + escapeRegex(item.term) + ')\\b', 'gi');
            html = html.replace(re, '<span class="product-term-mark">$1</span>');
        });
        return html;
    }

    function formatPrice(value) {
        return (value || 0).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    function buildDetailText(terms) {
        return terms.map(function (t) {
            return t.term + ' = ' + t.definition;
        }).join(' · ');
    }

    function buildRowsHtml(products, options) {
        options = options || {};
        const addedCodes = options.addedCodes || [];
        const hidePurchaseCost = !!options.hidePurchaseCost;
        const purchaseClass = hidePurchaseCost ? ' text-end hide-purchase-cost' : ' text-end';

        return products.map(function (product, index) {
            const code = product.code || product.product_code || '';
            const name = product.name || product.product_name || '';
            const rowKey = 'ps-' + (code || index);
            const terms = findTermsInName(name);
            const isAlreadyAdded = addedCodes.indexOf(code) !== -1;
            const priceBook = product.priceBookPrice != null
                ? product.priceBookPrice
                : (product.price_book_price || 0);
            const purchase = product.purchasePrice != null
                ? product.purchasePrice
                : (product.purchase_price || 0);

            const nameHtml = highlightTerms(name, terms);
            const toggleBtn = terms.length
                ? '<button type="button" class="term-toggle-btn" data-row-key="' + escapeHtml(rowKey) + '" aria-expanded="false" title="부가 설명"><span class="term-toggle-icon">▼</span></button>'
                : '';

            let html = '<tr class="product-search-row' + (isAlreadyAdded ? ' table-danger-soft' : '') + '"'
                + ' data-row-key="' + escapeHtml(rowKey) + '"'
                + ' data-product-code="' + escapeHtml(code) + '"'
                + ' data-product-name="' + escapeHtml(name) + '"'
                + ' data-price-book="' + priceBook + '"'
                + ' data-purchase="' + purchase + '"'
                + (isAlreadyAdded ? ' style="background-color:#ffe6e6;"' : '')
                + '>'
                + '<td><input type="checkbox" value="' + escapeHtml(String(product.id || index)) + '"></td>'
                + '<td>' + escapeHtml(code || '-') + '</td>'
                + '<td class="product-name-cell">' + nameHtml + toggleBtn + '</td>'
                + '<td class="text-end">' + formatPrice(priceBook) + '</td>'
                + '<td class="' + purchaseClass.trim() + '">' + formatPrice(purchase) + '</td>'
                + '</tr>';

            if (terms.length) {
                html += '<tr class="term-detail-row d-none" data-detail-for="' + escapeHtml(rowKey) + '">'
                    + '<td colspan="5"><div class="term-detail-content">' + escapeHtml(buildDetailText(terms)) + '</div></td>'
                    + '</tr>';
            }
            return html;
        }).join('');
    }

    function toggleTermDetail(rowKey) {
        const detailRow = document.querySelector('tr.term-detail-row[data-detail-for="' + rowKey + '"]');
        const mainRow = document.querySelector('tr.product-search-row[data-row-key="' + rowKey + '"]');
        const btn = mainRow ? mainRow.querySelector('.term-toggle-btn') : null;
        if (!detailRow || !btn) return;

        const isHidden = detailRow.classList.contains('d-none');
        detailRow.classList.toggle('d-none', !isHidden);
        if (mainRow) {
            mainRow.classList.toggle('term-row-expanded', isHidden);
        }
        btn.setAttribute('aria-expanded', isHidden ? 'true' : 'false');
        const icon = btn.querySelector('.term-toggle-icon');
        if (icon) {
            icon.textContent = isHidden ? '▲' : '▼';
        }
    }

    function initToggleHandlers(container) {
        if (!container || container._termToggleBound) return;
        container._termToggleBound = true;
        container.addEventListener('click', function (e) {
            const btn = e.target.closest('.term-toggle-btn');
            if (!btn) return;
            e.preventDefault();
            e.stopPropagation();
            toggleTermDetail(btn.getAttribute('data-row-key'));
        });
    }

    function readProductFromSearchRow(row) {
        if (!row) return null;
        return {
            code: row.getAttribute('data-product-code') || '',
            name: row.getAttribute('data-product-name') || '',
            priceBookPrice: parseFloat(row.getAttribute('data-price-book') || '0') || 0,
            purchasePrice: parseFloat(row.getAttribute('data-purchase') || '0') || 0
        };
    }

    window.ProductNameGlossary = {
        TERM_GLOSSARY: TERM_GLOSSARY,
        findTermsInName: findTermsInName,
        buildRowsHtml: buildRowsHtml,
        initToggleHandlers: initToggleHandlers,
        toggleTermDetail: toggleTermDetail,
        readProductFromSearchRow: readProductFromSearchRow
    };
})();
