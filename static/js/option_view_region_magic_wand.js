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

    function pixelLum(data, idx) {
        return (data[idx] + data[idx + 1] + data[idx + 2]) / 3;
    }

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
        cache.ready = true;
        return true;
    }

    function floodFill(sx, sy, tolerance) {
        var w = cache.width;
        var h = cache.height;
        var data = cache.data;
        var start = sy * w + sx;
        var seedLum = pixelLum(data, start * 4);
        var mask = new Uint8Array(w * h);
        var stack = [start];
        var filled = 0;

        while (stack.length) {
            var idx = stack.pop();
            if (mask[idx]) continue;
            var lum = pixelLum(data, idx * 4);
            if (Math.abs(lum - seedLum) > tolerance) continue;
            mask[idx] = 1;
            filled++;
            var x = idx % w;
            var y = (idx / w) | 0;
            if (x > 0) stack.push(idx - 1);
            if (x < w - 1) stack.push(idx + 1);
            if (y > 0) stack.push(idx - w);
            if (y < h - 1) stack.push(idx + w);
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

            var fill = floodFill(px, py, tolerance);
            var total = w * h;
            var minArea = Math.max(80, total * 0.00005);
            var maxArea = total * (options.maxAreaRatio || 0.42);

            if (fill.filled < minArea) {
                return { error: '선택 영역이 너무 작습니다. 부품 안쪽(흰색/밝은 영역)을 클릭해 주세요.' };
            }
            if (fill.filled > maxArea) {
                return { error: '선택 영역이 너무 큽니다. 특정 부품 안쪽을 클릭하거나 허용 범위를 낮춰 주세요.' };
            }

            var contour = traceContour(fill.mask, w, h);
            var points = simplifyContour(contour, w, h);
            if (points.length < 3) {
                return { error: '누끼 윤곽을 만들지 못했습니다. 다른 위치를 클릭해 주세요.' };
            }

            return { points: points, filled: fill.filled };
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
