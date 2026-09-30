/**
 * 파트가격조회 - 도면 부품 번호 하이라이트 (체크박스 ↔ 좌측 도면)
 */
(function () {
    'use strict';

    const diagramPartsCache = {};

    function escapeHtml(text) {
        const d = document.createElement('div');
        d.textContent = text == null ? '' : String(text);
        return d.innerHTML;
    }

    function resolvePartNumber(part, index, imageParts) {
        const rowNumber = index + 1;
        const code = (part.material_code || '').trim().toUpperCase();
        if (imageParts && imageParts.length) {
            const byCode = imageParts.find(function (p) {
                return (p.stock_code || '').trim().toUpperCase() === code;
            });
            if (byCode) return byCode.part_number;
            const byRow = imageParts.find(function (p) {
                return p.part_number === rowNumber;
            });
            if (byRow) return byRow.part_number;
        }
        return rowNumber;
    }

    function hasPosition(part) {
        return part.position_x != null && part.position_y != null &&
            !isNaN(parseFloat(part.position_x)) && !isNaN(parseFloat(part.position_y));
    }

    function buildPartMarkers(imageId, parts) {
        diagramPartsCache[imageId] = parts || [];
        const layer = document.getElementById('partMarkers_' + imageId);
        if (!layer) return;

        const positioned = (parts || []).filter(hasPosition);
        if (!positioned.length) {
            layer.innerHTML = '';
            return;
        }

        layer.innerHTML = positioned.map(function (p) {
            const left = (parseFloat(p.position_x) * 100).toFixed(2);
            const top = (parseFloat(p.position_y) * 100).toFixed(2);
            return '<div class="part-marker" data-part-number="' + p.part_number + '" '
                + 'style="left:' + left + '%;top:' + top + '%;" '
                + 'title="' + escapeHtml('부품 ' + p.part_number) + '">'
                + '<span class="part-marker-ring"></span>'
                + '<span class="part-marker-num">' + p.part_number + '</span>'
                + '</div>';
        }).join('');
    }

    function updateFallbackBanner(imageId, activeNumbers) {
        const wrapper = document.getElementById('diagramWrapper_' + imageId);
        if (!wrapper) return;

        let banner = wrapper.querySelector('.part-fallback-banner');
        if (!activeNumbers.size) {
            if (banner) banner.remove();
            return;
        }

        const nums = Array.from(activeNumbers).sort(function (a, b) { return a - b; });
        const text = '선택 부품: ' + nums.map(function (n) { return 'No.' + n; }).join(', ');
        if (!banner) {
            banner = document.createElement('div');
            banner.className = 'part-fallback-banner';
            wrapper.appendChild(banner);
        }
        banner.textContent = text;
    }

    function updatePartHighlights(imageId) {
        const checked = document.querySelectorAll(
            'input.part-checkbox[data-image-id="' + imageId + '"]:checked'
        );
        const activeNumbers = new Set();
        checked.forEach(function (cb) {
            const num = parseInt(cb.getAttribute('data-part-number'), 10);
            if (!isNaN(num)) activeNumbers.add(num);
        });

        const layer = document.getElementById('partMarkers_' + imageId);
        const markerCount = layer ? layer.querySelectorAll('.part-marker').length : 0;

        if (layer) {
            layer.querySelectorAll('.part-marker').forEach(function (marker) {
                const num = parseInt(marker.getAttribute('data-part-number'), 10);
                marker.classList.toggle('part-marker-active', activeNumbers.has(num));
            });
        }

        const table = document.getElementById('matchesTable_' + imageId);
        if (table) {
            table.querySelectorAll('tbody tr').forEach(function (row) {
                const cb = row.querySelector('.part-checkbox');
                row.classList.toggle('part-row-active', !!(cb && cb.checked));
            });
        }

        const wrapper = document.getElementById('diagramWrapper_' + imageId);
        if (wrapper) {
            wrapper.classList.toggle('diagram-has-selection', activeNumbers.size > 0);
        }

        if (markerCount === 0) {
            updateFallbackBanner(imageId, activeNumbers);
        } else {
            updateFallbackBanner(imageId, new Set());
        }
    }

    window.PartDiagramHighlight = {
        cacheParts: function (imageId, parts) {
            diagramPartsCache[imageId] = parts || [];
        },
        resolvePartNumber: resolvePartNumber,
        buildPartMarkers: buildPartMarkers,
        updatePartHighlights: updatePartHighlights
    };
})();
