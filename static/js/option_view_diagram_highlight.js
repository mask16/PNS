/**
 * 옵션 조회 — 세트 도면 영역 하이라이트 (버건디/검정/회색)
 */
(function () {
    'use strict';

    var COLORS = {
        current: { stroke: '#800020', fill: 'rgba(128, 0, 32, 0.18)' },
        completed: { stroke: '#111111', fill: 'rgba(17, 17, 17, 0.12)' },
        unselected: { stroke: '#9ca3af', fill: 'rgba(156, 163, 175, 0.08)' }
    };

    function escapeHtml(text) {
        var d = document.createElement('div');
        d.textContent = text == null ? '' : String(text);
        return d.innerHTML;
    }

    function getRegionPolygonList(region) {
        var data = region.region_json || {};
        if (Array.isArray(data.polygons) && data.polygons.length) {
            return data.polygons;
        }
        if (Array.isArray(data.points) && data.points.length >= 3) {
            return [data.points];
        }
        return [];
    }

    function pointsToPercentAttr(points) {
        return points.map(function (p) {
            return (parseFloat(p[0]) * 100).toFixed(3) + ',' + (parseFloat(p[1]) * 100).toFixed(3);
        }).join(' ');
    }

    function normalizePointList(points) {
        return points.map(function (p) {
            return [
                parseFloat(Math.max(0, Math.min(1, p[0])).toFixed(4)),
                parseFloat(Math.max(0, Math.min(1, p[1])).toFixed(4))
            ];
        });
    }

    function regionPointsFromList(points) {
        return points.map(function (p) {
            return (parseFloat(p[0]) * 100).toFixed(4) + ',' + (parseFloat(p[1]) * 100).toFixed(4);
        }).join(' ');
    }

    function regionPoints(region) {
        var type = (region.region_type || 'rect').toLowerCase();
        var data = region.region_json || {};
        if (type === 'polygon') {
            var polys = getRegionPolygonList(region);
            if (polys.length) {
                return regionPointsFromList(polys[0]);
            }
        }
        var x = parseFloat(data.x) * 100;
        var y = parseFloat(data.y) * 100;
        var w = parseFloat(data.w) * 100;
        var h = parseFloat(data.h) * 100;
        return [
            x + ',' + y,
            (x + w) + ',' + y,
            (x + w) + ',' + (y + h),
            x + ',' + (y + h)
        ].join(' ');
    }

    function rectAttrs(region) {
        var data = region.region_json || {};
        return {
            x: (parseFloat(data.x) * 100).toFixed(4) + '%',
            y: (parseFloat(data.y) * 100).toFixed(4) + '%',
            width: (parseFloat(data.w) * 100).toFixed(4) + '%',
            height: (parseFloat(data.h) * 100).toFixed(4) + '%'
        };
    }

    function applyShapeColors(node, state) {
        var palette = COLORS[state] || COLORS.unselected;
        node.setAttribute('stroke', palette.stroke);
        node.setAttribute('fill', palette.fill);
        node.setAttribute('data-state', state);
    }

    function buildRegionNodes(regions, categoryLabels) {
        var nodes = [];
        (regions || []).forEach(function (region) {
            var catNum = region.category_number;
            var label = categoryLabels[catNum] || ('[' + catNum + ']');
            var type = (region.region_type || 'rect').toLowerCase();
            if (type === 'polygon') {
                getRegionPolygonList(region).forEach(function (poly) {
                    nodes.push('<polygon class="option-view-region" data-category-number="' + catNum + '" points="' +
                        regionPointsFromList(poly) + '" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"></polygon>');
                });
                return;
            }
            var attrs = rectAttrs(region);
            nodes.push('<rect class="option-view-region" data-category-number="' + catNum + '" x="' + attrs.x +
                '" y="' + attrs.y + '" width="' + attrs.width + '" height="' + attrs.height +
                '" vector-effect="non-scaling-stroke" data-label="' + escapeHtml(label) + '"></rect>');
        });
        return nodes.join('');
    }

    function getRegionState(categoryNumber, currentCategory, selectedCategories) {
        var isSelected = selectedCategories.has(categoryNumber);
        if (categoryNumber === currentCategory && isSelected) {
            return 'current';
        }
        if (isSelected) {
            return 'completed';
        }
        return 'unselected';
    }

    window.OptionViewDiagramHighlight = {
        colors: COLORS,

        renderOverlay: function (svgEl, regions, categoryLabels) {
            if (!svgEl) return;
            svgEl.setAttribute('viewBox', '0 0 100 100');
            svgEl.setAttribute('preserveAspectRatio', 'none');
            svgEl.innerHTML = buildRegionNodes(regions, categoryLabels || {});
        },

        updateHighlights: function (svgEl, currentCategory, selectedCategories, categoryLabels) {
            if (!svgEl) return;
            var selected = selectedCategories instanceof Set
                ? selectedCategories
                : new Set(selectedCategories || []);
            svgEl.querySelectorAll('.option-view-region').forEach(function (node) {
                var catNum = parseInt(node.getAttribute('data-category-number'), 10);
                var state = getRegionState(catNum, currentCategory, selected);
                applyShapeColors(node, state);
            });
        },

        syncOverlaySize: function (wrapperEl) {
            if (!wrapperEl) return;
            var svg = wrapperEl.querySelector('svg.option-view-region-overlay');
            if (!svg) return;
            svg.style.width = '100%';
            svg.style.height = '100%';
        }
    };
})();
