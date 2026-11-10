/**
 * 옵션조회 영역 매핑 — 자동 누끼 (매직 완드)
 */
(function () {
    'use strict';

    var cache = {
        data: null,
        width: 0,
        height: 0,
        ready: false
    };

    function rebuildCache(img) {
        if (!img || !img.naturalWidth) {
            cache.ready = false;
            return false;
        }
        var maxDim = 1400;
        var scale = Math.min(1, maxDim / Math.max(img.naturalWidth, img.naturalHeight));
        var w = Math.max(1, Math.round(img.naturalWidth * scale));
        var h = Math.max(1, Math.round(img.naturalHeight * scale));
        var canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        var ctx = canvas.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, w, h);
        var imageData = ctx.getImageData(0, 0, w, h);
        cache.data = imageData.data;
        cache.width = w;
        cache.height = h;
        buildPartIndex(imageData.data, w, h);
        cache.ready = true;
        return true;
    }

    function isPaperPixel(data, idx) {
        var r = data[idx];
        var g = data[idx + 1];
        var b = data[idx + 2];
        var a = data[idx + 3];
        if (a < 16) return true;
        var max = r > g ? (r > b ? r : b) : (g > b ? g : b);
        var min = r < g ? (r < b ? r : b) : (g < b ? g : b);
        var lum = (r + g + b) / 3;
        return lum >= 236 && (max - min) <= 26;
    }

    function buildPartIndex(data, w, h) {
        var n = w * h;
        var fg = new Uint8Array(n);
        var labels = new Uint32Array(n);
        for (var i = 0; i < n; i++) {
            fg[i] = isPaperPixel(data, i * 4) ? 0 : 1;
        }

        var area = [0];
        var bbox = [0, 0, 0, 0];
        var touchesBorder = [0];
        var nextId = 1;

        for (var start = 0; start < n; start++) {
            if (fg[start] || labels[start]) continue;
            var stack = [start];
            labels[start] = nextId;
            var count = 0;
            var x0 = start % w;
            var x1 = x0;
            var y0 = (start / w) | 0;
            var y1 = y0;
            var border = 0;
            while (stack.length) {
                var idx = stack.pop();
                count++;
                var x = idx % w;
                var y = (idx / w) | 0;
                if (x < x0) x0 = x;
                if (x > x1) x1 = x;
                if (y < y0) y0 = y;
                if (y > y1) y1 = y;
                if (x === 0 || y === 0 || x === w - 1 || y === h - 1) border = 1;
                if (x > 0 && !fg[idx - 1] && !labels[idx - 1]) {
                    labels[idx - 1] = nextId;
                    stack.push(idx - 1);
                }
                if (x < w - 1 && !fg[idx + 1] && !labels[idx + 1]) {
                    labels[idx + 1] = nextId;
                    stack.push(idx + 1);
                }
                if (y > 0 && !fg[idx - w] && !labels[idx - w]) {
                    labels[idx - w] = nextId;
                    stack.push(idx - w);
                }
                if (y < h - 1 && !fg[idx + w] && !labels[idx + w]) {
                    labels[idx + w] = nextId;
                    stack.push(idx + w);
                }
            }
            area.push(count);
            bbox.push(x0, y0, x1, y1);
            touchesBorder.push(border);
            nextId++;
        }

        var backgroundId = 0;
        var backgroundArea = -1;
        for (var id = 1; id < nextId; id++) {
            if (!touchesBorder[id]) continue;
            if (area[id] > backgroundArea) {
                backgroundArea = area[id];
                backgroundId = id;
            }
        }

        var adj = new Array(nextId);
        for (var a = 0; a < nextId; a++) adj[a] = [];
        var seenStamp = new Uint32Array(nextId);
        var stamp = 1;
        for (var y = 0; y < h; y++) {
            for (var x = 0; x < w; x++) {
                var pix = y * w + x;
                if (!fg[pix]) continue;
                stamp++;
                if (stamp > 4000000000) {
                    seenStamp = new Uint32Array(nextId);
                    stamp = 1;
                }
                var found = [];
                for (var oy = -4; oy <= 4; oy++) {
                    var ny = y + oy;
                    if (ny < 0 || ny >= h) continue;
                    var row = ny * w;
                    for (var ox = -4; ox <= 4; ox++) {
                        var nx = x + ox;
                        if (nx < 0 || nx >= w) continue;
                        var lab = labels[row + nx];
                        if (!lab || lab === backgroundId || seenStamp[lab] === stamp) continue;
                        seenStamp[lab] = stamp;
                        found.push(lab);
                    }
                }
                for (var p = 0; p < found.length; p++) {
                    for (var q = p + 1; q < found.length; q++) {
                        var la = found[p];
                        var lb = found[q];
                        if (adj[la].indexOf(lb) === -1) adj[la].push(lb);
                        if (adj[lb].indexOf(la) === -1) adj[lb].push(la);
                    }
                }
            }
        }

        cache.foreground = fg;
        cache.labels = labels;
        cache.area = area;
        cache.bbox = bbox;
        cache.adj = adj;
        cache.backgroundId = backgroundId;
        cache.componentCount = nextId - 1;
    }

    function isBridge(id, tolerance) {
        var area = cache.area[id];
        if (!area || area < 36) return false;
        var o = id * 4;
        var bw = cache.bbox[o + 2] - cache.bbox[o] + 1;
        var bh = cache.bbox[o + 3] - cache.bbox[o + 1] + 1;
        var minor = Math.min(bw, bh);
        var major = Math.max(bw, bh);
        var limit = Math.max(8, Math.round(36 - tolerance * 0.45));
        return major >= minor * 4 && minor <= limit;
    }

    function collectPart(seed, tolerance) {
        var selected = new Uint8Array(cache.componentCount + 1);
        if (!seed || seed === cache.backgroundId) return selected;
        selected[seed] = 1;
        if (isBridge(seed, tolerance)) return selected;
        var stack = [seed];
        var adj = cache.adj;
        while (stack.length) {
            var cur = stack.pop();
            var neighbors = adj[cur];
            for (var i = 0; i < neighbors.length; i++) {
                var nb = neighbors[i];
                if (selected[nb] || nb === cache.backgroundId || isBridge(nb, tolerance)) continue;
                selected[nb] = 1;
                stack.push(nb);
            }
        }
        return selected;
    }

    function findNearbyPart(px, py, tolerance) {
        var w = cache.width;
        var h = cache.height;
        var labels = cache.labels;
        var bg = cache.backgroundId;
        var reach = Math.max(14, Math.round(12 + tolerance * 0.2));
        var best = 0;
        var bestArea = -1;
        var thinBest = 0;
        var thinDist = 1e9;
        var y0 = Math.max(0, py - reach);
        var y1 = Math.min(h - 1, py + reach);
        var x0 = Math.max(0, px - reach);
        var x1 = Math.min(w - 1, px + reach);
        for (var y = y0; y <= y1; y++) {
            var row = y * w;
            for (var x = x0; x <= x1; x++) {
                var lab = labels[row + x];
                if (!lab || lab === bg) continue;
                var dist = Math.max(Math.abs(x - px), Math.abs(y - py));
                if (dist > reach) continue;
                if (isBridge(lab, tolerance)) {
                    if (dist < thinDist) {
                        thinDist = dist;
                        thinBest = lab;
                    }
                } else if (cache.area[lab] > bestArea) {
                    bestArea = cache.area[lab];
                    best = lab;
                }
            }
        }
        return best || thinBest || 0;
    }

    function maskFromSelection(selected) {
        var w = cache.width;
        var h = cache.height;
        var labels = cache.labels;
        var fg = cache.foreground;
        var n = w * h;
        var mask = new Uint8Array(n);
        var filled = 0;
        for (var i = 0; i < n; i++) {
            var lab = labels[i];
            if (lab && selected[lab]) {
                mask[i] = 1;
                filled++;
            }
        }
        for (var y = 0; y < h; y++) {
            for (var x = 0; x < w; x++) {
                var idx = y * w + x;
                if (mask[idx] || !fg[idx]) continue;
                var hit = false;
                for (var oy = -3; oy <= 3 && !hit; oy++) {
                    var ny = y + oy;
                    if (ny < 0 || ny >= h) continue;
                    for (var ox = -3; ox <= 3; ox++) {
                        var nx = x + ox;
                        if (nx < 0 || nx >= w) continue;
                        var near = labels[ny * w + nx];
                        if (near && selected[near]) {
                            hit = true;
                            break;
                        }
                    }
                }
                if (hit) {
                    mask[idx] = 1;
                    filled++;
                }
            }
        }
        return { mask: mask, filled: filled };
    }

    function isBoundary(mask, w, h, x, y) {
        var idx = y * w + x;
        if (!mask[idx]) return false;
        if (x === 0 || y === 0 || x === w - 1 || y === h - 1) return true;
        return !mask[idx - 1] || !mask[idx + 1] || !mask[idx - w] || !mask[idx + w];
    }

    function traceContour(mask, w, h) {
        var start = null;
        for (var y = 0; y < h && !start; y++) {
            for (var x = 0; x < w; x++) {
                if (isBoundary(mask, w, h, x, y)) {
                    start = { x: x, y: y };
                    break;
                }
            }
        }
        if (!start) return [];

        var dirs = [
            [1, 0], [1, 1], [0, 1], [-1, 1],
            [-1, 0], [-1, -1], [0, -1], [1, -1]
        ];
        var contour = [];
        var x = start.x;
        var y = start.y;
        var dir = 0;
        var guard = 0;
        var maxGuard = w * h * 6;

        do {
            contour.push({ x: x, y: y });
            var found = false;
            for (var i = 0; i < 8; i++) {
                var nd = (dir + i + 5) % 8;
                var nx = x + dirs[nd][0];
                var ny = y + dirs[nd][1];
                if (nx < 0 || ny < 0 || nx >= w || ny >= h) continue;
                if (mask[ny * w + nx]) {
                    x = nx;
                    y = ny;
                    dir = nd;
                    found = true;
                    break;
                }
            }
            if (!found) break;
            guard++;
        } while ((x !== start.x || y !== start.y || contour.length < 3) && guard < maxGuard);

        return contour;
    }

    function perpendicularDistance(point, start, end) {
        var dx = end[0] - start[0];
        var dy = end[1] - start[1];
        if (dx === 0 && dy === 0) {
            dx = point[0] - start[0];
            dy = point[1] - start[1];
            return Math.sqrt(dx * dx + dy * dy);
        }
        var t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy);
        t = Math.max(0, Math.min(1, t));
        var px = start[0] + t * dx;
        var py = start[1] + t * dy;
        dx = point[0] - px;
        dy = point[1] - py;
        return Math.sqrt(dx * dx + dy * dy);
    }

    function douglasPeucker(points, epsilon) {
        if (points.length <= 2) return points;
        var maxDist = 0;
        var index = 0;
        var start = points[0];
        var end = points[points.length - 1];
        for (var i = 1; i < points.length - 1; i++) {
            var dist = perpendicularDistance(points[i], start, end);
            if (dist > maxDist) {
                maxDist = dist;
                index = i;
            }
        }
        if (maxDist > epsilon) {
            var left = douglasPeucker(points.slice(0, index + 1), epsilon);
            var right = douglasPeucker(points.slice(index), epsilon);
            return left.slice(0, -1).concat(right);
        }
        return [start, end];
    }

    function simplifyContour(contour, w, h) {
        if (!contour.length) return [];
        var step = Math.max(1, Math.floor(contour.length / 800));
        var sampled = [];
        for (var i = 0; i < contour.length; i += step) {
            sampled.push([
                contour[i].x / w,
                contour[i].y / h
            ]);
        }
        if (sampled.length < 3) return sampled;
        return douglasPeucker(sampled, 0.0025);
    }

    function pointInPolygon(nx, ny, poly) {
        var inside = false;
        for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
            var xi = poly[i][0];
            var yi = poly[i][1];
            var xj = poly[j][0];
            var yj = poly[j][1];
            var intersect = ((yi > ny) !== (yj > ny)) &&
                (nx < (xj - xi) * (ny - yi) / ((yj - yi) || 1e-9) + xi);
            if (intersect) inside = !inside;
        }
        return inside;
    }

    function rasterizePolygonsToMask(polygons, w, h) {
        var mask = new Uint8Array(w * h);
        for (var y = 0; y < h; y++) {
            var ny = h > 1 ? y / (h - 1) : 0;
            for (var x = 0; x < w; x++) {
                var nx = w > 1 ? x / (w - 1) : 0;
                for (var p = 0; p < polygons.length; p++) {
                    if (pointInPolygon(nx, ny, polygons[p])) {
                        mask[y * w + x] = 1;
                        break;
                    }
                }
            }
        }
        return mask;
    }

    function clearDisc(mask, w, h, cx, cy, radius) {
        var r2 = radius * radius;
        var y0 = Math.max(0, cy - radius);
        var y1 = Math.min(h - 1, cy + radius);
        var x0 = Math.max(0, cx - radius);
        var x1 = Math.min(w - 1, cx + radius);
        for (var y = y0; y <= y1; y++) {
            for (var x = x0; x <= x1; x++) {
                var dx = x - cx;
                var dy = y - cy;
                if (dx * dx + dy * dy <= r2) {
                    mask[y * w + x] = 0;
                }
            }
        }
    }

    function subtractStrokeFromMask(mask, w, h, strokePoints, radiusNorm) {
        if (!strokePoints || strokePoints.length < 1) return;
        var radius = Math.max(2, Math.round(radiusNorm * Math.min(w, h)));
        var prev = null;
        for (var i = 0; i < strokePoints.length; i++) {
            var px = Math.round(strokePoints[i][0] * (w - 1));
            var py = Math.round(strokePoints[i][1] * (h - 1));
            if (prev) {
                var dx = px - prev[0];
                var dy = py - prev[1];
                var steps = Math.max(Math.abs(dx), Math.abs(dy), 1);
                for (var s = 0; s <= steps; s++) {
                    var ix = Math.round(prev[0] + dx * s / steps);
                    var iy = Math.round(prev[1] + dy * s / steps);
                    clearDisc(mask, w, h, ix, iy, radius);
                }
            } else {
                clearDisc(mask, w, h, px, py, radius);
            }
            prev = [px, py];
        }
    }

    function extractPolygonsFromMask(mask, w, h) {
        var visited = new Uint8Array(w * h);
        var polygons = [];
        var minArea = Math.max(40, Math.floor(w * h * 0.00002));

        for (var start = 0; start < w * h; start++) {
            if (!mask[start] || visited[start]) continue;

            var stack = [start];
            var component = new Uint8Array(w * h);
            var filled = 0;

            while (stack.length) {
                var idx = stack.pop();
                if (visited[idx] || !mask[idx]) continue;
                visited[idx] = 1;
                component[idx] = 1;
                filled++;
                var x = idx % w;
                var y = (idx / w) | 0;
                if (x > 0) stack.push(idx - 1);
                if (x < w - 1) stack.push(idx + 1);
                if (y > 0) stack.push(idx - w);
                if (y < h - 1) stack.push(idx + w);
            }

            if (filled < minArea) continue;

            var contour = traceContour(component, w, h);
            var points = simplifyContour(contour, w, h);
            if (points.length >= 3) {
                polygons.push(points);
            }
        }
        return polygons;
    }

    window.OptionViewMagicWand = {
        invalidate: function () {
            cache.ready = false;
        },

        rebuild: function (img) {
            return rebuildCache(img);
        },

        isReady: function () {
            return cache.ready;
        },

        extract: function (normX, normY, options) {
            options = options || {};
            if (!cache.ready) {
                return { error: '이미지 분석 데이터가 준비되지 않았습니다.' };
            }

            var tolerance = typeof options.tolerance === 'number' ? options.tolerance : 32;
            var w = cache.width;
            var h = cache.height;
            var px = Math.max(0, Math.min(w - 1, Math.round(normX * (w - 1))));
            var py = Math.max(0, Math.min(h - 1, Math.round(normY * (h - 1))));
            var total = w * h;
            var clickLabel = cache.labels[py * w + px];
            var seed = 0;
            if (clickLabel && clickLabel !== cache.backgroundId) {
                seed = clickLabel;
            } else {
                seed = findNearbyPart(px, py, tolerance);
            }
            if (!seed) {
                return { error: '빈 배경입니다. 부품 안쪽을 한 번 클릭해 주세요.' };
            }

            var selected = collectPart(seed, tolerance);
            var fill = maskFromSelection(selected);

            var minArea = Math.max(80, total * 0.00005);
            var maxArea = total * (options.maxAreaRatio || 0.55);

            if (fill.filled < minArea) {
                return { error: '선택 영역이 너무 작습니다. 부품의 선이나 면 위를 클릭해 주세요.' };
            }
            if (fill.filled > maxArea) {
                return { error: '선택 영역이 너무 큽니다. 부품 위를 클릭하거나 허용 범위를 낮춰 주세요.' };
            }

            var contour = traceContour(fill.mask, w, h);
            var points = simplifyContour(contour, w, h);
            if (points.length < 3) {
                return { error: '누끼 윤곽을 만들지 못했습니다. 다른 위치를 클릭해 주세요.' };
            }

            return { points: points, filled: fill.filled };
        },

        extractInside: function (guidePoints) {
            if (!cache.ready) {
                return { error: '이미지 분석 데이터가 준비되지 않았습니다.' };
            }
            if (!Array.isArray(guidePoints) || guidePoints.length < 3) {
                return { error: '영역을 더 길게 그려 주세요.' };
            }

            var w = cache.width;
            var h = cache.height;
            var userMask = rasterizePolygonsToMask([guidePoints], w, h);
            var counts = new Uint32Array(cache.componentCount + 1);
            var inside = 0;
            var n = w * h;
            for (var i = 0; i < n; i++) {
                if (!userMask[i]) continue;
                inside++;
                var lab = cache.labels[i];
                if (lab && lab !== cache.backgroundId) counts[lab]++;
            }
            if (inside < 80) {
                return { error: '그린 범위가 너무 작습니다.' };
            }

            var selected = new Uint8Array(cache.componentCount + 1);
            var chosen = 0;
            for (var id = 1; id <= cache.componentCount; id++) {
                if (!counts[id] || !cache.area[id]) continue;
                if (counts[id] / cache.area[id] >= 0.5) {
                    selected[id] = 1;
                    chosen++;
                }
            }
            if (!chosen) {
                return { error: '그린 범위 안에서 부품 선을 찾지 못했습니다.' };
            }

            var fill = maskFromSelection(selected);
            var polygons = extractPolygonsFromMask(fill.mask, w, h);
            if (!polygons.length) {
                return { error: '그림 윤곽을 만들지 못했습니다.' };
            }
            return { polygons: polygons, filled: fill.filled };
        },

        eraseStroke: function (polygons, strokePoints, radiusNorm) {
            if (!cache.ready) {
                return { error: '이미지 분석 데이터가 준비되지 않았습니다.' };
            }
            if (!Array.isArray(polygons) || !polygons.length) {
                return { polygons: [] };
            }
            if (!Array.isArray(strokePoints) || strokePoints.length < 2) {
                return { polygons: polygons };
            }

            var w = cache.width;
            var h = cache.height;
            var mask = rasterizePolygonsToMask(polygons, w, h);
            subtractStrokeFromMask(mask, w, h, strokePoints, radiusNorm || 0.024);
            var result = extractPolygonsFromMask(mask, w, h);
            return { polygons: result };
        }
    };
})();
