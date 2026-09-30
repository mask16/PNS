/**
 * PNS 사용자 활동 추적 (체류시간, 관심 페이지, 견적 다운로드)
 */
(function () {
    'use strict';

    const STORAGE_KEY = 'pnsTrackingContext';
    const HEARTBEAT_MS = 30000;
    const INTEREST_THRESHOLD_SEC = 30;

    let pageEnteredAt = Date.now();
    let sessionStartAt = Date.now();
    let heartbeatTimer = null;
    let currentPath = window.location.pathname;

    function loadContext() {
        try {
            return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null');
        } catch (e) {
            return null;
        }
    }

    function saveContext(ctx) {
        if (ctx) {
            sessionStorage.setItem(STORAGE_KEY, JSON.stringify(ctx));
        } else {
            sessionStorage.removeItem(STORAGE_KEY);
        }
    }

    function getContext() {
        return loadContext();
    }

    function setContext(ctx) {
        saveContext(ctx);
        if (ctx && ctx.session_id) {
            startHeartbeat();
        } else {
            stopHeartbeat();
        }
    }

    function clearContext() {
        saveContext(null);
        stopHeartbeat();
    }

    function totalSessionSeconds() {
        return Math.floor((Date.now() - sessionStartAt) / 1000);
    }

    function pageDurationSeconds() {
        return Math.floor((Date.now() - pageEnteredAt) / 1000);
    }

    function postJson(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body || {})
        }).catch(function () { return null; });
    }

    function sendPageView(forceDuration) {
        const ctx = getContext();
        if (!ctx || !ctx.session_id) return;
        const duration = typeof forceDuration === 'number' ? forceDuration : pageDurationSeconds();
        postJson('/api/tracking/pageview', {
            session_id: ctx.session_id,
            user_id: ctx.user_id,
            user_type: ctx.user_type,
            page_path: currentPath,
            page_title: document.title,
            duration_seconds: duration,
            total_seconds: totalSessionSeconds()
        });
    }

    function sendHeartbeat() {
        const ctx = getContext();
        if (!ctx || !ctx.session_id) return;
        postJson('/api/tracking/heartbeat', {
            session_id: ctx.session_id,
            total_seconds: totalSessionSeconds()
        });
    }

    function startHeartbeat() {
        stopHeartbeat();
        heartbeatTimer = setInterval(sendHeartbeat, HEARTBEAT_MS);
    }

    function stopHeartbeat() {
        if (heartbeatTimer) {
            clearInterval(heartbeatTimer);
            heartbeatTimer = null;
        }
    }

    function onPageLeave() {
        const duration = pageDurationSeconds();
        if (duration >= 1) {
            sendPageView(duration);
        }
    }

    function onPageEnter() {
        pageEnteredAt = Date.now();
        currentPath = window.location.pathname;
    }

    function loginViaApi(endpoint, username, password) {
        return fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ username: username, password: password })
        }).then(function (res) {
            return res.json().then(function (data) {
                if (!res.ok || !data.success) {
                    throw new Error(data.error || '로그인 실패');
                }
                return data;
            });
        });
    }

    function adminLogin(username, password) {
        return unifiedLogin(username, password);
    }

    function unifiedLogin(username, password) {
        return loginViaApi('/api/auth/login', username, password).then(function (data) {
            sessionStartAt = Date.now();
            setContext({
                session_id: data.tracking_session_id,
                user_id: data.user_id,
                user_type: data.user_type,
                permissions: data.permissions || []
            });
            onPageEnter();
            document.dispatchEvent(new CustomEvent('pnsAuthLoggedIn', { detail: data }));
            return data;
        });
    }

    function adminLogout() {
        return postJson('/api/auth/logout', {}).then(function () {
            onPageLeave();
            clearContext();
            document.dispatchEvent(new CustomEvent('pnsAuthLoggedOut'));
        });
    }

    function trackingLogin(username, password) {
        return loginViaApi('/api/tracking/login', username, password).then(function (data) {
            sessionStartAt = Date.now();
            setContext({
                session_id: data.tracking_session_id,
                user_id: data.user_id,
                user_type: data.user_type,
                permissions: data.permissions || []
            });
            onPageEnter();
            return data;
        });
    }

    function restoreFromServer() {
        return fetch('/api/auth/session', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success && data.logged_in && data.tracking_session_id) {
                    const existing = getContext();
                    if (!existing || existing.session_id !== data.tracking_session_id) {
                        sessionStartAt = Date.now();
                        setContext({
                            session_id: data.tracking_session_id,
                            user_id: data.user_id,
                            user_type: data.user_type,
                            permissions: data.permissions || []
                        });
                    } else {
                        startHeartbeat();
                    }
                    onPageEnter();
                    return data;
                }
                return null;
            })
            .catch(function () { return null; });
    }

    function hasPermission(permission) {
        const ctx = getContext();
        if (!ctx) return false;
        if (ctx.user_type === 'admin') return true;
        const perms = ctx.permissions || [];
        return perms.indexOf(permission) !== -1;
    }

    function canAccessExportStaff() {
        const ctx = getContext();
        return !!(ctx && (ctx.user_type === 'admin' || ctx.user_type === 'export_dept'));
    }

    function canAccessExportCustomer() {
        const ctx = getContext();
        return !!(ctx && (ctx.user_type === 'admin' || ctx.user_type === 'customer'));
    }

    function logQuoteDownload(quoteSource, products, fileName) {
        const ctx = getContext();
        if (!ctx) return Promise.resolve();
        return postJson('/api/tracking/quote_download', {
            session_id: ctx.session_id,
            user_id: ctx.user_id,
            user_type: ctx.user_type,
            quote_source: quoteSource,
            products: products || [],
            file_name: fileName || ''
        });
    }

    function init() {
        const ctx = getContext();
        if (ctx && ctx.session_id) {
            sessionStartAt = Date.now();
            startHeartbeat();
        }

        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'hidden') {
                onPageLeave();
            } else if (document.visibilityState === 'visible') {
                onPageEnter();
            }
        });

        window.addEventListener('beforeunload', onPageLeave);
        window.addEventListener('pagehide', onPageLeave);

        restoreFromServer();
    }

    window.PnsTracking = {
        INTEREST_THRESHOLD_SEC: INTEREST_THRESHOLD_SEC,
        getContext: getContext,
        setContext: setContext,
        clearContext: clearContext,
        adminLogin: adminLogin,
        unifiedLogin: unifiedLogin,
        adminLogout: adminLogout,
        trackingLogin: trackingLogin,
        logQuoteDownload: logQuoteDownload,
        restoreFromServer: restoreFromServer,
        sendPageView: sendPageView,
        hasPermission: hasPermission,
        canAccessExportStaff: canAccessExportStaff,
        canAccessExportCustomer: canAccessExportCustomer,
        init: init
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
