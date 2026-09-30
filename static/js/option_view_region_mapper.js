/**
 * 옵션조회 이미지 — 관리자 영역 매핑 (자동 누끼 / 연필 / 사각형)
 */
(function () {
    'use strict';

    var MIN_POINT_DIST = 0.004;
    var MIN_POLYGON_POINTS = 3;

    var state = {
        imageId: null,
        imageUrl: '',
        categories: [],
        regions: [],
        activeCategory: null,
        tool: 'pencil',
        drawing: false,
        startX: 0,
        startY: 0,
        draftRect: null,
        draftPoints: [],
        eraserPoints: []
    };

    function escapeHtml(text) {
        var d = document.createElement('div');
        d.textContent = text == null ? '' : String(text);
        return d.innerHTML;
    }

    function categoryLabel(catNum) {
        var found = state.categories.find(function (c) {
            return c.category_number === catNum;
        });
        return found ? ('[' + found.category_number + '] ' + found.category_name) : ('[' + catNum + ']');
    }

    function normalizedPoint(svg, clientX, clientY) {
        var rect = svg.getBoundingClientRect();
        if (!rect.width || !rect.height) {
            return { x: 0, y: 0 };
        }
        return {
            x: Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)),
            y: Math.max(0, Math.min(1, (clientY - rect.top) / rect.height))
        };
    }

    function rectFromPoints(a, b) {
        var x = Math.min(a.x, b.x);
        var y = Math.min(a.y, b.y);
        var w = Math.abs(a.x - b.x);
        var h = Math.abs(a.y - b.y);
        return { x: x, y: y, w: w, h: h };
    }

    function pointDistance(a, b) {
        var dx = a[0] - b[0];
        var dy = a[1] - b[1];
        return Math.sqrt(dx * dx + dy * dy);
    }

    function simplifyPath(points, minDist) {
        if (!points || points.length <= 2) {
            return points || [];
        }
        var result = [points[0]];
        for (var i = 1; i < points.length; i++) {
            if (pointDistance(result[result.length - 1], points[i]) >= minDist) {
                result.push(points[i]);
            }
        }
        if (points.length > 1 && pointDistance(result[result.length - 1], points[points.length - 1]) >= minDist * 0.5) {
            result.push(points[points.length - 1]);
        }
        return result;
    }

    function pointsToPercentAttr(points) {
        return points.map(function (p) {
            return (parseFloat(p[0]) * 100).toFixed(3) + ',' + (parseFloat(p[1]) * 100).toFixed(3);
        }).join(' ');
    }

    function renderRegionList() {
        var list = document.getElementById('regionMapperList');
        if (!list) return;
        if (!state.regions.length) {
            list.innerHTML = '<div class="text-muted small">등록된 영역이 없습니다.</div>';
            return;
        }
        list.innerHTML = state.regions.map(function (region, idx) {
            var polyCount = region.region_type === 'polygon' ? getRegionPolygonList(region).length : 0;
            var typeLabel = region.region_type === 'polygon'
                ? (region.source === 'magic' ? ('자동누끼' + (polyCount > 1 ? ' x' + polyCount : '')) : '누끼')
                : '사각';
            return '<div class="region-mapper-item">' +
                '<span>' + escapeHtml(categoryLabel(region.category_number)) +
                ' <span class="text-muted">(' + typeLabel + ')</span></span>' +
                '<button type="button" class="btn btn-sm btn-outline-danger" data-region-index="' + idx + '">삭제</button>' +
                '</div>';
        }).join('');
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

    function regionToSvg(region) {
        var data = region.region_json || {};
        var cls = 'region-mapper-shape';
        if (region.region_type === 'polygon') {
            return getRegionPolygonList(region).map(function (poly, polyIdx) {
                return '<polygon class="' + cls + '" data-category-number="' + region.category_number +
                    '" data-polygon-index="' + polyIdx + '" data-region-type="polygon" points="' +
                    pointsToPercentAttr(poly) + '"></polygon>';
            }).join('');
        }
        var x = parseFloat(data.x) * 100;
        var y = parseFloat(data.y) * 100;
        var w = parseFloat(data.w) * 100;
        var h = parseFloat(data.h) * 100;
        return '<rect class="' + cls + '" data-category-number="' + region.category_number +
            '" data-polygon-index="0" data-region-type="rect" x="' + x + '%" y="' + y + '%" width="' + w +
            '%" height="' + h + '%"></rect>';
    }

    function renderRegionsOnSvg() {
        var svg = document.getElementById('regionMapperSvg');
        if (!svg) return;

        var shapes = state.regions.map(regionToSvg).join('');
        var draft = '';

        if (state.tool === 'pencil' && state.draftPoints.length >= 2) {
            draft = '<polyline class="region-mapper-draft region-mapper-draft--pencil" points="' +
                pointsToPercentAttr(state.draftPoints) + '"></polyline>';
            if (state.draftPoints.length >= 3) {
                draft += '<polygon class="region-mapper-draft-fill" points="' +
                    pointsToPercentAttr(state.draftPoints) + '"></polygon>';
            }
        } else if (state.tool === 'rect' && state.draftRect) {
            draft = '<rect class="region-mapper-draft" x="' + (state.draftRect.x * 100) + '%" y="' +
                (state.draftRect.y * 100) + '%" width="' + (state.draftRect.w * 100) + '%" height="' +
                (state.draftRect.h * 100) + '%"></rect>';
        } else if (state.tool === 'eraser' && state.eraserPoints.length >= 2) {
            draft = '<polyline class="region-mapper-draft region-mapper-draft--eraser" points="' +
                pointsToPercentAttr(state.eraserPoints) + '"></polyline>';
        }

        svg.innerHTML = shapes + draft;
        svg.classList.toggle('is-pencil-tool', state.tool === 'pencil');
        svg.classList.toggle('is-rect-tool', state.tool === 'rect');
        svg.classList.toggle('is-magic-tool', state.tool === 'magic');
        svg.classList.toggle('is-eraser-tool', state.tool === 'eraser');
    }

    function getEraserRadius() {
        var slider = document.getElementById('regionMapperEraserSize');
        var val = slider ? parseInt(slider.value, 10) : 24;
        return (val || 24) / 1000;
    }

    function getMagicTolerance() {
        var slider = document.getElementById('regionMapperTolerance');
        if (!slider) return 32;
        return parseInt(slider.value, 10) || 32;
    }

    function rebuildMagicCache() {
        if (!window.OptionViewMagicWand) return false;
        var img = document.getElementById('regionMapperImage');
        OptionViewMagicWand.invalidate();
        return OptionViewMagicWand.rebuild(img);
    }

    function runMagicWand(svg, clientX, clientY) {
        if (!window.OptionViewMagicWand) {
            alert('자동 누끼 기능을 불러오지 못했습니다.');
            return;
        }
        if (!OptionViewMagicWand.isReady()) {
            rebuildMagicCache();
        }
        if (!OptionViewMagicWand.isReady()) {
            alert('이미지 분석 준비가 되지 않았습니다.');
            return;
        }

        var pt = normalizedPoint(svg, clientX, clientY);
        var result = OptionViewMagicWand.extract(pt.x, pt.y, {
            tolerance: getMagicTolerance()
        });
        if (result.error) {
            alert(result.error);
            return;
        }
        appendMagicPolygon(state.activeCategory, result.points);
    }

    function appendMagicPolygon(categoryNumber, newPoints) {
        var simplified = simplifyPath(newPoints, MIN_POINT_DIST);
        if (simplified.length < 3) {
            return;
        }
        var normalized = simplified.map(function (p) {
            return [
                parseFloat(Math.max(0, Math.min(1, p[0])).toFixed(4)),
                parseFloat(Math.max(0, Math.min(1, p[1])).toFixed(4))
            ];
        });

        var existing = state.regions.find(function (r) {
            return r.category_number === categoryNumber;
        });
        var polygons = [];
        if (existing && existing.region_type === 'polygon') {
            polygons = getRegionPolygonList(existing).map(function (poly) {
                return poly.slice();
            });
        }
        polygons.push(normalized);
        upsertRegion(categoryNumber, 'polygon', { polygons: polygons }, 'magic');
    }

    function clearActiveCategoryRegion() {
        if (!state.activeCategory) {
            alert('먼저 매핑할 옵션 카테고리를 선택해 주세요.');
            return;
        }
        state.regions = state.regions.filter(function (r) {
            return r.category_number !== state.activeCategory;
        });
        renderRegionList();
        renderRegionsOnSvg();
    }

    function setRegionPolygons(categoryNumber, polygons, source) {
        if (!polygons || !polygons.length) {
            state.regions = state.regions.filter(function (r) {
                return r.category_number !== categoryNumber;
            });
            renderRegionList();
            renderRegionsOnSvg();
            return;
        }
        if (polygons.length === 1) {
            upsertRegion(categoryNumber, 'polygon', { points: polygons[0] }, source || 'magic');
            return;
        }
        upsertRegion(categoryNumber, 'polygon', { polygons: polygons }, source || 'magic');
    }

    function removePolygonPiece(shapeEl) {
        if (!shapeEl) return;
        var catNum = parseInt(shapeEl.getAttribute('data-category-number'), 10);
        var polyIdx = parseInt(shapeEl.getAttribute('data-polygon-index'), 10);
        var regionType = shapeEl.getAttribute('data-region-type') || 'polygon';
        if (isNaN(catNum)) return;

        if (regionType === 'rect') {
            state.regions = state.regions.filter(function (r) {
                return r.category_number !== catNum;
            });
            renderRegionList();
            renderRegionsOnSvg();
            return;
        }

        var region = state.regions.find(function (r) {
            return r.category_number === catNum;
        });
        if (!region) return;

        var polys = getRegionPolygonList(region).map(function (poly) {
            return poly.slice();
        });
        if (!isNaN(polyIdx) && polyIdx >= 0 && polyIdx < polys.length) {
            polys.splice(polyIdx, 1);
        }
        setRegionPolygons(catNum, polys, region.source || 'magic');
    }

    function applyEraserStroke(categoryNumber) {
        if (!categoryNumber || state.eraserPoints.length < 2) {
            state.eraserPoints = [];
            renderRegionsOnSvg();
            return;
        }
        if (!window.OptionViewMagicWand || !OptionViewMagicWand.isReady()) {
            rebuildMagicCache();
        }
        if (!window.OptionViewMagicWand || !OptionViewMagicWand.isReady()) {
            alert('이미지 분석 준비가 되지 않았습니다.');
            state.eraserPoints = [];
            renderRegionsOnSvg();
            return;
        }

        var region = state.regions.find(function (r) {
            return r.category_number === categoryNumber;
        });
        if (!region || region.region_type !== 'polygon') {
            state.eraserPoints = [];
            renderRegionsOnSvg();
            return;
        }

        var polygons = getRegionPolygonList(region).map(function (poly) {
            return poly.slice();
        });
        var result = OptionViewMagicWand.eraseStroke(
            polygons,
            state.eraserPoints,
            getEraserRadius()
        );
        state.eraserPoints = [];
        if (result.error) {
            alert(result.error);
            renderRegionsOnSvg();
            return;
        }
        setRegionPolygons(categoryNumber, result.polygons || [], region.source || 'magic');
    }

    function updateToolHelpText() {
        var help = document.getElementById('regionMapperToolHelp');
        if (!help) return;
        if (state.tool === 'magic') {
            help.innerHTML = '카테고리 선택 후 <strong>부품 안쪽을 여러 번 클릭</strong>하면 클릭할 때마다 누끼 영역이 추가됩니다.';
        } else if (state.tool === 'eraser') {
            help.innerHTML = '<strong>영역을 클릭</strong>하면 해당 조각이 삭제되고, <strong>드래그</strong>하면 현재 카테고리 영역을 부분적으로 지웁니다.';
        } else if (state.tool === 'pencil') {
            help.innerHTML = '카테고리 선택 후 도면 위를 <strong>연필처럼 따라 그려</strong> 누끼 영역을 지정합니다.';
        } else {
            help.innerHTML = '카테고리 선택 후 도면 위를 <strong>드래그</strong>하여 사각 영역을 지정합니다.';
        }
        var toleranceWrap = document.getElementById('regionMapperToleranceWrap');
        if (toleranceWrap) {
            toleranceWrap.style.display = state.tool === 'magic' ? '' : 'none';
        }
        var eraserWrap = document.getElementById('regionMapperEraserWrap');
        if (eraserWrap) {
            eraserWrap.style.display = state.tool === 'eraser' ? '' : 'none';
        }
    }

    function parseRegionJson(raw) {
        if (typeof raw === 'string') {
            try {
                return JSON.parse(raw);
            } catch (e) {
                return {};
            }
        }
        return raw && typeof raw === 'object' ? raw : {};
    }

    function normalizePolygonPoints(points) {
        if (!Array.isArray(points) || points.length < 3) {
            return null;
        }
        var normalized = [];
        for (var i = 0; i < points.length; i++) {
            var p = points[i];
            if (!Array.isArray(p) || p.length !== 2) {
                return null;
            }
            var px = parseFloat(p[0]);
            var py = parseFloat(p[1]);
            if (isNaN(px) || isNaN(py)) {
                return null;
            }
            normalized.push([
                parseFloat(Math.max(0, Math.min(1, px)).toFixed(4)),
                parseFloat(Math.max(0, Math.min(1, py)).toFixed(4))
            ]);
        }
        return normalized.length >= 3 ? normalized : null;
    }

    function buildRegionJsonForSave(region) {
        var json = parseRegionJson(region.region_json);
        if (region.region_type === 'rect') {
            return {
                x: parseFloat(json.x),
                y: parseFloat(json.y),
                w: parseFloat(json.w),
                h: parseFloat(json.h)
            };
        }

        var polys = [];
        if (Array.isArray(json.polygons) && json.polygons.length) {
            json.polygons.forEach(function (poly) {
                var normalized = normalizePolygonPoints(poly);
                if (normalized) polys.push(normalized);
            });
        } else if (Array.isArray(json.points)) {
            var single = normalizePolygonPoints(json.points);
            if (single) polys.push(single);
        }

        if (!polys.length) {
            return null;
        }
        if (polys.length === 1) {
            return { points: polys[0] };
        }
        return { polygons: polys };
    }

    function prepareRegionsForSave() {
        var prepared = [];
        for (var i = 0; i < state.regions.length; i++) {
            var region = state.regions[i];
            var regionJson = buildRegionJsonForSave(region);
            if (!regionJson) {
                throw new Error('[' + region.category_number + '] 유효하지 않은 polygon 영역입니다. 3개 이상의 점이 필요합니다.');
            }
            prepared.push({
                category_number: region.category_number,
                region_type: region.region_type,
                region_json: regionJson
            });
        }
        return prepared;
    }

    function upsertRegion(categoryNumber, regionType, regionJson, source) {
        state.regions = state.regions.filter(function (r) {
            return r.category_number !== categoryNumber;
        });
        state.regions.push({
            category_number: categoryNumber,
            region_type: regionType,
            region_json: regionJson,
            source: source || ''
        });
        state.regions.sort(function (a, b) { return a.category_number - b.category_number; });
        renderRegionList();
        renderRegionsOnSvg();
    }

    function upsertRectRegion(categoryNumber, rect) {
        if (rect.w < 0.01 || rect.h < 0.01) {
            return;
        }
        upsertRegion(categoryNumber, 'rect', {
            x: parseFloat(rect.x.toFixed(4)),
            y: parseFloat(rect.y.toFixed(4)),
            w: parseFloat(rect.w.toFixed(4)),
            h: parseFloat(rect.h.toFixed(4))
        });
    }

    function upsertPolygonRegion(categoryNumber, points, source) {
        var simplified = simplifyPath(points, MIN_POINT_DIST);
        if (simplified.length < 3) {
            return;
        }
        upsertRegion(categoryNumber, 'polygon', {
            points: simplified.map(function (p) {
                return [
                    parseFloat(Math.max(0, Math.min(1, p[0])).toFixed(4)),
                    parseFloat(Math.max(0, Math.min(1, p[1])).toFixed(4))
                ];
            })
        }, source);
    }

    function appendDraftPoint(pt) {
        var point = [pt.x, pt.y];
        if (!state.draftPoints.length) {
            state.draftPoints.push(point);
            return;
        }
        var last = state.draftPoints[state.draftPoints.length - 1];
        if (pointDistance(last, point) >= MIN_POINT_DIST) {
            state.draftPoints.push(point);
        }
    }

    function appendEraserPoint(pt) {
        var point = [pt.x, pt.y];
        if (!state.eraserPoints.length) {
            state.eraserPoints.push(point);
            return;
        }
        var last = state.eraserPoints[state.eraserPoints.length - 1];
        if (pointDistance(last, point) >= MIN_POINT_DIST * 0.5) {
            state.eraserPoints.push(point);
        }
    }

    function bindCanvasEvents() {
        var svg = document.getElementById('regionMapperSvg');
        if (!svg || svg.dataset.bound === '1') return;
        svg.dataset.bound = '1';

        function ensureCategorySelected() {
            if (!state.activeCategory) {
                alert('먼저 매핑할 옵션 카테고리를 선택해 주세요.');
                return false;
            }
            return true;
        }

        svg.addEventListener('mousedown', function (e) {
            if (e.button !== 0) return;
            if (state.tool === 'magic') return;

            if (state.tool === 'eraser') {
                var shape = e.target.closest('.region-mapper-shape');
                if (shape) {
                    e.preventDefault();
                    removePolygonPiece(shape);
                    return;
                }
                if (!ensureCategorySelected()) return;
                e.preventDefault();
                state.drawing = true;
                var eraserPt = normalizedPoint(svg, e.clientX, e.clientY);
                state.eraserPoints = [[eraserPt.x, eraserPt.y]];
                renderRegionsOnSvg();
                return;
            }

            if (!ensureCategorySelected()) return;
            e.preventDefault();
            state.drawing = true;
            var pt = normalizedPoint(svg, e.clientX, e.clientY);

            if (state.tool === 'pencil') {
                state.draftPoints = [[pt.x, pt.y]];
            } else {
                state.startX = pt.x;
                state.startY = pt.y;
                state.draftRect = { x: pt.x, y: pt.y, w: 0, h: 0 };
            }
            renderRegionsOnSvg();
        });

        svg.addEventListener('mousemove', function (e) {
            if (!state.drawing) return;
            var pt = normalizedPoint(svg, e.clientX, e.clientY);

            if (state.tool === 'pencil') {
                appendDraftPoint(pt);
            } else if (state.tool === 'eraser') {
                appendEraserPoint(pt);
            } else {
                state.draftRect = rectFromPoints(
                    { x: state.startX, y: state.startY },
                    { x: pt.x, y: pt.y }
                );
            }
            renderRegionsOnSvg();
        });

        function finishDraw(e) {
            if (!state.drawing) return;
            state.drawing = false;

            if (state.tool === 'pencil') {
                var pt = normalizedPoint(svg, e.clientX, e.clientY);
                appendDraftPoint(pt);
                var points = state.draftPoints.slice();
                state.draftPoints = [];
                if (points.length >= MIN_POLYGON_POINTS) {
                    upsertPolygonRegion(state.activeCategory, points);
                } else {
                    renderRegionsOnSvg();
                    alert('누끼 영역을 더 길게 그려 주세요.');
                }
                return;
            }

            if (state.tool === 'eraser') {
                applyEraserStroke(state.activeCategory);
                return;
            }

            var pt = normalizedPoint(svg, e.clientX, e.clientY);
            var rect = rectFromPoints(
                { x: state.startX, y: state.startY },
                { x: pt.x, y: pt.y }
            );
            state.draftRect = null;
            upsertRectRegion(state.activeCategory, rect);
        }

        svg.addEventListener('click', function (e) {
            if (state.tool !== 'magic') return;
            if (!ensureCategorySelected()) return;
            e.preventDefault();
            runMagicWand(svg, e.clientX, e.clientY);
        });

        svg.addEventListener('mouseup', finishDraw);
        svg.addEventListener('mouseleave', function (e) {
            if (state.drawing) finishDraw(e);
        });

        svg.addEventListener('touchstart', function (e) {
            if (state.tool === 'magic') return;
            if (state.tool === 'eraser') {
                if (!ensureCategorySelected()) return;
                if (!e.touches || !e.touches.length) return;
                e.preventDefault();
                var touchStart = e.touches[0];
                state.drawing = true;
                var eraserStart = normalizedPoint(svg, touchStart.clientX, touchStart.clientY);
                state.eraserPoints = [[eraserStart.x, eraserStart.y]];
                renderRegionsOnSvg();
                return;
            }
            if (!ensureCategorySelected()) return;
            if (!e.touches || !e.touches.length) return;
            e.preventDefault();
            var touch = e.touches[0];
            state.drawing = true;
            var pt = normalizedPoint(svg, touch.clientX, touch.clientY);
            if (state.tool === 'pencil') {
                state.draftPoints = [[pt.x, pt.y]];
            } else {
                state.startX = pt.x;
                state.startY = pt.y;
                state.draftRect = { x: pt.x, y: pt.y, w: 0, h: 0 };
            }
            renderRegionsOnSvg();
        }, { passive: false });

        svg.addEventListener('touchmove', function (e) {
            if (!state.drawing || !e.touches || !e.touches.length) return;
            e.preventDefault();
            var touch = e.touches[0];
            var pt = normalizedPoint(svg, touch.clientX, touch.clientY);
            if (state.tool === 'pencil') {
                appendDraftPoint(pt);
            } else if (state.tool === 'eraser') {
                appendEraserPoint(pt);
            } else {
                state.draftRect = rectFromPoints(
                    { x: state.startX, y: state.startY },
                    { x: pt.x, y: pt.y }
                );
            }
            renderRegionsOnSvg();
        }, { passive: false });

        svg.addEventListener('touchend', function (e) {
            if (state.tool === 'magic') {
                if (!ensureCategorySelected()) return;
                var touch = (e.changedTouches && e.changedTouches[0]) || null;
                if (touch) {
                    e.preventDefault();
                    runMagicWand(svg, touch.clientX, touch.clientY);
                }
                return;
            }
            if (state.tool === 'eraser' && state.drawing) {
                applyEraserStroke(state.activeCategory);
                state.drawing = false;
                return;
            }
            if (!state.drawing) return;
            var touch = (e.changedTouches && e.changedTouches[0]) || null;
            finishDraw({
                clientX: touch ? touch.clientX : 0,
                clientY: touch ? touch.clientY : 0
            });
        });
    }

    function populateCategorySelect() {
        var select = document.getElementById('regionMapperCategory');
        if (!select) return;
        select.innerHTML = '<option value="">카테고리 선택</option>' +
            state.categories.map(function (cat) {
                return '<option value="' + cat.category_number + '">[' + cat.category_number + '] ' +
                    escapeHtml(cat.category_name) + '</option>';
            }).join('');
    }

    function syncToolUi() {
        document.querySelectorAll('input[name="regionTool"]').forEach(function (input) {
            input.checked = input.value === state.tool;
        });
        updateToolHelpText();
        renderRegionsOnSvg();
    }

    function setMapperLoading(isLoading) {
        var stage = document.getElementById('regionMapperStage');
        var svg = document.getElementById('regionMapperSvg');
        if (stage) {
            stage.classList.toggle('is-ready', !isLoading);
        }
        if (svg) {
            svg.style.pointerEvents = isLoading ? 'none' : 'auto';
        }
    }

    function fitMapperStage() {
        var img = document.getElementById('regionMapperImage');
        var svg = document.getElementById('regionMapperSvg');
        if (!img || !svg || !img.naturalWidth || !img.clientWidth) {
            return;
        }
        svg.style.width = img.clientWidth + 'px';
        svg.style.height = img.clientHeight + 'px';
    }

    function loadMapperImage(imageUrl, onReady, onError) {
        var img = document.getElementById('regionMapperImage');
        if (!img) {
            onError(new Error('이미지 영역을 찾을 수 없습니다.'));
            return;
        }

        setMapperLoading(true);
        img.removeAttribute('src');

        function handleReady() {
            fitMapperStage();
            rebuildMagicCache();
            setMapperLoading(false);
            renderRegionsOnSvg();
            onReady();
        }

        img.onload = handleReady;
        img.onerror = function () {
            setMapperLoading(false);
            onError(new Error('이미지를 불러오지 못했습니다.'));
        };

        var preload = new Image();
        preload.onload = function () {
            img.src = imageUrl;
            if (img.complete && img.naturalWidth > 0) {
                handleReady();
            }
        };
        preload.onerror = function () {
            setMapperLoading(false);
            onError(new Error('이미지를 불러오지 못했습니다.'));
        };
        preload.src = imageUrl;
    }

    window.OptionViewRegionMapper = {
        open: function (options) {
            state.imageId = options.imageId;
            state.imageUrl = options.imageUrl || '';
            state.categories = options.categories || [];
            state.regions = (options.regions || []).map(function (region) {
                return {
                    category_number: region.category_number,
                    region_type: region.region_type || 'rect',
                    region_json: parseRegionJson(region.region_json),
                    source: region.source || ''
                };
            });
            state.activeCategory = null;
            state.tool = 'magic';
            state.drawing = false;
            state.draftRect = null;
            state.draftPoints = [];
            state.eraserPoints = [];

            populateCategorySelect();
            syncToolUi();
            renderRegionList();
            renderRegionsOnSvg();
            bindCanvasEvents();

            var modalEl = document.getElementById('regionMapperModal');
            if (modalEl && window.bootstrap) {
                bootstrap.Modal.getOrCreateInstance(modalEl).show();
            }

            loadMapperImage(
                state.imageUrl,
                function () {},
                function (err) {
                    alert(err.message || '이미지를 불러오지 못했습니다.');
                }
            );
        },

        save: function () {
            if (!state.imageId) {
                return Promise.reject(new Error('이미지 정보가 없습니다.'));
            }
            var payloadRegions;
            try {
                payloadRegions = prepareRegionsForSave();
            } catch (err) {
                return Promise.reject(err);
            }
            return fetch('/api/option_view_images/' + encodeURIComponent(state.imageId) + '/regions', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ regions: payloadRegions })
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.success) throw new Error(data.error || '저장 실패');
                state.regions = data.regions || state.regions;
                renderRegionList();
                renderRegionsOnSvg();
                return data;
            });
        }
    };

    document.addEventListener('DOMContentLoaded', function () {
        var categorySelect = document.getElementById('regionMapperCategory');
        if (categorySelect) {
            categorySelect.addEventListener('change', function () {
                var val = parseInt(categorySelect.value, 10);
                state.activeCategory = isNaN(val) ? null : val;
            });
        }

        document.querySelectorAll('input[name="regionTool"]').forEach(function (input) {
            input.addEventListener('change', function () {
                if (!input.checked) return;
                var val = input.value;
                if (val === 'rect' || val === 'pencil' || val === 'eraser') {
                    state.tool = val;
                } else {
                    state.tool = 'magic';
                }
                state.drawing = false;
                state.draftRect = null;
                state.draftPoints = [];
                state.eraserPoints = [];
                syncToolUi();
            });
        });

        var list = document.getElementById('regionMapperList');
        if (list) {
            list.addEventListener('click', function (e) {
                var btn = e.target.closest('button[data-region-index]');
                if (!btn) return;
                var idx = parseInt(btn.getAttribute('data-region-index'), 10);
                if (isNaN(idx)) return;
                state.regions.splice(idx, 1);
                renderRegionList();
                renderRegionsOnSvg();
            });
        }

        var saveBtn = document.getElementById('regionMapperSaveBtn');
        if (saveBtn) {
            saveBtn.addEventListener('click', function () {
                saveBtn.disabled = true;
                OptionViewRegionMapper.save()
                    .then(function (data) {
                        alert(data.message || '저장되었습니다.');
                    })
                    .catch(function (err) {
                        alert(err.message || '저장 중 오류가 발생했습니다.');
                    })
                    .finally(function () {
                        saveBtn.disabled = false;
                    });
            });
        }

        var clearCategoryBtn = document.getElementById('regionMapperClearCategoryBtn');
        if (clearCategoryBtn) {
            clearCategoryBtn.addEventListener('click', clearActiveCategoryRegion);
        }

        var modalEl = document.getElementById('regionMapperModal');
        if (modalEl) {
            modalEl.addEventListener('shown.bs.modal', function () {
                fitMapperStage();
                rebuildMagicCache();
                renderRegionsOnSvg();
            });
            window.addEventListener('resize', function () {
                if (!modalEl.classList.contains('show')) return;
                fitMapperStage();
                rebuildMagicCache();
                renderRegionsOnSvg();
            });
        }
    });
})();
