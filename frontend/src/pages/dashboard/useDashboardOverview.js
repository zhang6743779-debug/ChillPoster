import axios from 'axios';
import { computed, nextTick, reactive, ref, watch } from 'vue';

export function useDashboardOverview({ tab, servers, isMobile, syncServersFrom302, showToast, showConfirm, startDashboardDeviceMetricsPolling, stopDashboardDeviceMetricsPolling }) {
        const dashboardStats = reactive({ tasks: 0, backups: 0, fonts: 0 });
        const dashboardRecentItems = ref([]);
        const dashboardRecentPlaybacks = ref([]);
        const dashboardMediaStats = reactive({ total: 0, movie_count: 0, series_count: 0, episode_count: 0, user_count: 0, movie_libraries: 0, series_libraries: 0, other_libraries: 0, libraries: [] });
        const DASHBOARD_RECENT_RENDER_LIMIT_DESKTOP = 24;
        const DASHBOARD_RECENT_RENDER_LIMIT_MOBILE = 12;
        const DASHBOARD_RECENT_RENDER_STEP_DESKTOP = 12;
        const DASHBOARD_RECENT_RENDER_STEP_MOBILE = 8;
        const DASHBOARD_PLAYBACK_RENDER_LIMIT_DESKTOP = 6;
        const DASHBOARD_PLAYBACK_RENDER_LIMIT_MOBILE = 4;
        const DASHBOARD_PLAYBACK_RENDER_STEP_DESKTOP = 4;
        const DASHBOARD_PLAYBACK_RENDER_STEP_MOBILE = 2;
        const DASHBOARD_LIBRARY_RENDER_LIMIT_DESKTOP = 12;
        const DASHBOARD_LIBRARY_RENDER_LIMIT_MOBILE = 8;
        const DASHBOARD_LIBRARY_RENDER_STEP_DESKTOP = 8;
        const DASHBOARD_LIBRARY_RENDER_STEP_MOBILE = 4;
        const DASHBOARD_WALL_RENDER_LIMIT = 32;

        const dashboard115Account = reactive({
            connected: false,
            account_name: '115 网盘',
            uid: '--',
            login_app: '',
            login_app_label: '',
            vip_active: false,
            vip_label: '未连接',
            vip_forever: false,
            vip_expire_at: null,
            used_bytes: 0,
            total_bytes: 0,
            remain_bytes: 0,
            used_human: '--',
            total_human: '--',
            remain_human: '--',
            usage_percent: 0,
            message: '',
            timestamp: null,
        });
        const dashboard115ClickTimestamps = ref([]);
        const dashboardCovers = ref([]);
        const dashboardRecentRenderPage = ref(0);
        const dashboardPlaybackRenderPage = ref(0);
        const dashboardLibraryRenderPage = ref(0);
        const wallRows = reactive([[], [], [], []]);
        const wallReady = ref(false);
        const dashboardOverviewLoading = ref(false);
        const dashboardOverviewLoaded = ref(false);
        const dashboard115Loaded = ref(false);
        const DASHBOARD_OVERVIEW_CACHE_KEY = 'cp_dashboard_overview';
        const DASHBOARD_OVERVIEW_CACHE_VERSION = 1;
        const DASHBOARD_OVERVIEW_TTL = Infinity;
        let dashboardOverviewRequestSeq = 0;

        const getDashboardOverviewServerFingerprint = () => {
            const svr = servers.value[0];
            if (!svr?.url || !svr?.key) return '';
            const keyTail = String(svr.key || '').slice(-8);
            return [svr.url || '', svr.public_host || '', svr.server_id || '', keyTail].join('|');
        };

        const getDashboardOverviewCache = () => {
            try {
                const raw = localStorage.getItem(DASHBOARD_OVERVIEW_CACHE_KEY);
                if (!raw) return null;
                const parsed = JSON.parse(raw);
                if (!parsed || parsed.version !== DASHBOARD_OVERVIEW_CACHE_VERSION) return null;
                if (!parsed.data || !Array.isArray(parsed.data.recent_items) || !Array.isArray(parsed.data.recent_playbacks) || typeof parsed.data.media_stats !== 'object' || parsed.data.media_stats === null) {
                    return null;
                }
                return parsed;
            } catch (_) {
                return null;
            }
        };

        const setDashboardOverviewCache = (payload) => {
            try {
                localStorage.setItem(DASHBOARD_OVERVIEW_CACHE_KEY, JSON.stringify(payload));
            } catch (_) {}
        };

        const isDashboardOverviewCacheFresh = (payload, fingerprint) => {
            if (!payload || !fingerprint) return false;
            if (payload.serverFingerprint !== fingerprint) return false;
            const updatedAt = Number(payload.updatedAt || 0);
            if (!updatedAt) return false;
            return (Date.now() - updatedAt) < DASHBOARD_OVERVIEW_TTL;
        };


        let dashboard115Polling = null;

        const resetDashboard115Account = () => {
            Object.assign(dashboard115Account, {
                connected: false,
                account_name: '115 网盘',
                uid: '--',
                login_app: '',
                login_app_label: '',
                vip_active: false,
                vip_label: '未连接',
                vip_forever: false,
                vip_expire_at: null,
                used_bytes: 0,
                total_bytes: 0,
                remain_bytes: 0,
                used_human: '--',
                total_human: '--',
                remain_human: '--',
                usage_percent: 0,
                message: '',
                timestamp: null,
            });
        };

        const fetchDashboard115Account = async () => {
            try {
                const res = await axios.get('/api/dashboard_115_account');
                Object.assign(dashboard115Account, {
                    connected: false,
                    account_name: '115 网盘',
                    uid: '--',
                    login_app: '',
                    login_app_label: '',
                    vip_active: false,
                    vip_label: '未连接',
                    vip_forever: false,
                    vip_expire_at: null,
                    used_bytes: 0,
                    total_bytes: 0,
                    remain_bytes: 0,
                    used_human: '--',
                    total_human: '--',
                    remain_human: '--',
                    usage_percent: 0,
                    message: '',
                    timestamp: null,
                }, res.data || {});
                dashboard115Loaded.value = true;
            } catch (e) {
                console.log('Dashboard 115 account failed', e);
                resetDashboard115Account();
                dashboard115Account.message = '115 信息获取失败';
                dashboard115Loaded.value = false;
            }
        };

        const stopDashboard115Polling = () => {
            if (dashboard115Polling) {
                clearInterval(dashboard115Polling);
                dashboard115Polling = null;
            }
        };

        const startDashboard115Polling = () => {
            stopDashboard115Polling();
            if (tab.value !== 'dashboard') return;
            fetchDashboard115Account();
            dashboard115Polling = setInterval(() => {
                if (tab.value !== 'dashboard') return;
                fetchDashboard115Account();
            }, 300000);
        };

        const handleDashboardVisibilityChange = () => {
            if (document.hidden) {
                stopDashboardDeviceMetricsPolling();
                stopDashboard115Polling();
                return;
            }
            if (tab.value === 'dashboard') {
                startDashboardDeviceMetricsPolling();
                startDashboard115Polling();
            }
        };


        const triggerManual115Signin = async () => {
            try {
                const res = await axios.post('/api/config_302/manual_signin_all');
                if (res.data?.status === 'ok') {
                    showToast(res.data.message || '签到完成', 'success');
                    fetchDashboard115Account();
                } else {
                    showToast(res.data?.message || '签到失败', 'error');
                }
            } catch (e) {
                showToast('签到失败: ' + (e.response?.data?.message || e.message), 'error');
            }
        };

        const handleDashboard115CardClick = async () => {
            const now = Date.now();
            dashboard115ClickTimestamps.value = dashboard115ClickTimestamps.value.filter(ts => now - ts <= 1000);
            dashboard115ClickTimestamps.value.push(now);

            if (dashboard115ClickTimestamps.value.length < 9) return;

            dashboard115ClickTimestamps.value = [];
            const ok = await showConfirm('哥', '那么高频率用力的点我是要签到吗? 以后十二点可以经常找我练手速哦~', 'info');
            if (!ok) return;
            showToast('开始手动签到...', 'info');
            await triggerManual115Signin();
        };


        const splitIntoRows = () => {
            const covers = (dashboardCovers.value || [])
                .filter(item => item?.cover_url)
                .slice(0, DASHBOARD_WALL_RENDER_LIMIT);
            const rowCount = wallRows.length;
            for (let i = 0; i < rowCount; i++) {
                wallRows[i] = [];
            }
            if (!covers.length) {
                wallReady.value = false;
                return;
            }

            const rows = Array.from({ length: rowCount }, () => []);
            covers.forEach((item, idx) => {
                rows[idx % rowCount].push(item);
            });

            const minTrackItems = Math.max(8, Math.ceil(window.innerWidth / 240));
            for (let i = 0; i < rowCount; i++) {
                let current = [...rows[i]];
                if (!current.length) current = [...covers];
                while (current.length < minTrackItems) {
                    current = [...current, ...current];
                }
                const duplicated = [...current, ...current];
                wallRows[i] = duplicated;
            }

            wallReady.value = false;
            nextTick(() => {
                wallReady.value = true;
            });
        };

        const getDashboardLibraryUrl = (item) => {
            if (!item?.id) return '';
            const svr = servers.value[0];
            if (!svr?.server_id) return '';
            const base = (svr.public_host || svr.url || '').replace(/\/$/, '');
            if (!base) return '';
            return `${base}/web/index.html#!/videos?serverId=${encodeURIComponent(svr.server_id)}&parentId=${encodeURIComponent(item.id)}`;
        };

        const getDashboardItemUrl = (item) => {
            if (!item?.id) return '';
            const svr = servers.value[0];
            if (!svr?.server_id) return '';
            const base = (svr.public_host || svr.url || '').replace(/\/$/, '');
            if (!base) return '';
            return `${base}/web/index.html#!/item?id=${encodeURIComponent(item.id)}&serverId=${encodeURIComponent(svr.server_id)}`;
        };

        const ensureDashboardServerId = async () => {
            const svr = servers.value[0];
            if (!svr?.url || !svr?.key) return '';
            if (svr.server_id) return svr.server_id;
            try {
                const res = await axios.post('/api/connect', {
                    url: svr.url,
                    key: svr.key,
                    public_host: svr.public_host
                });
                svr.server_id = res.data.server_id || '';
                if (res.data.libraries) {
                    svr.libraries = res.data.libraries;
                }
                syncServersFrom302();
                return svr.server_id;
            } catch (e) {
                return '';
            }
        };

        const openDashboardLibrary = async (item) => {
            if (!item?.id) {
                showToast('未找到可用的媒体库', 'error');
                return;
            }
            await ensureDashboardServerId();
            const url = getDashboardLibraryUrl(item);
            if (!url) {
                showToast('未获取到 Emby serverId，请重启服务后重试', 'error');
                return;
            }
            window.open(url, '_blank', 'noopener');
        };

        const openDashboardItem = async (item) => {
            if (!item?.id) {
                showToast('未找到可用的媒体条目', 'error');
                return;
            }
            await ensureDashboardServerId();
            const url = getDashboardItemUrl(item);
            if (!url) {
                showToast('未获取到 Emby serverId，请重启服务后重试', 'error');
                return;
            }
            window.open(url, '_blank', 'noopener');
        };

        const initDashboard = async () => {
            if (servers.value.length === 0) return;
            const svr = servers.value[0];
            if (!svr.url || !svr.key) return;

            try {
                const res = await axios.post('/api/library_covers', {
                    url: svr.url, key: svr.key, public_host: svr.public_host
                });
                dashboardCovers.value = res.data.libraries || [];
                svr.server_id = res.data.server_id || svr.server_id || '';
                if (res.data.libraries) {
                    const simpleLibs = res.data.libraries.map(l => ({ id: l.id, name: l.name }));
                    svr.libraries = simpleLibs;
                    syncServersFrom302();
                }
                splitIntoRows();
            } catch (e) { console.log("Dashboard init failed", e); }
        };


        const fetchDashboardStats = async () => {
            try {
                const res = await axios.get('/api/dashboard_stats');
                Object.assign(dashboardStats, res.data);
            } catch {}
        };

        const resetDashboardOverview = () => {
            dashboardRecentItems.value = [];
            dashboardRecentPlaybacks.value = [];
            Object.assign(dashboardMediaStats, {
                total: 0,
                movie_count: 0,
                series_count: 0,
                episode_count: 0,
                user_count: 0,
                movie_libraries: 0,
                series_libraries: 0,
                other_libraries: 0,
                libraries: []
            });
        };

        const applyDashboardOverviewData = (payload) => {
            dashboardRecentItems.value = payload?.recent_items || [];
            dashboardRecentPlaybacks.value = payload?.recent_playbacks || [];
            Object.assign(dashboardMediaStats, {
                total: 0,
                movie_count: 0,
                series_count: 0,
                episode_count: 0,
                user_count: 0,
                movie_libraries: 0,
                series_libraries: 0,
                other_libraries: 0,
                libraries: []
            }, payload?.media_stats || {});
        };

        const formatDashboardPlayedAt = (value) => {
            if (!value) return '最近播放';
            const normalized = String(value).replace(/\.(\d{3})\d*Z$/, '.$1Z');
            const date = new Date(normalized);
            if (Number.isNaN(date.getTime())) return '最近播放';

            const diff = Date.now() - date.getTime();
            const minute = 60 * 1000;
            const hour = 60 * minute;
            const day = 24 * hour;

            if (diff >= 0 && diff < hour) {
                return `${Math.max(1, Math.floor(diff / minute))} 分钟前`;
            }
            if (diff >= hour && diff < day) {
                return `${Math.floor(diff / hour)} 小时前`;
            }
            if (diff >= day && diff < day * 7) {
                return `${Math.floor(diff / day)} 天前`;
            }
            return date.toLocaleString('zh-CN', {
                month: 'numeric',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });
        };

        const truncateDashboardText = (value, maxLength = 80) => {
            const text = String(value || '').trim();
            if (text.length <= maxLength) return text;
            return text.slice(0, Math.max(0, maxLength - 3)).trimEnd() + '...';
        };
        const getDashboardRecentSubtitle = (item) => {
            const year = item?.year || '未知年份';
            const detail = item?.media_type === 'tv'
                ? truncateDashboardText(item?.episode_label || '剧集', 84)
                : '电影';
            return `${year} · ${detail}`;
        };

        const getDashboardRenderLimit = (baseDesktop, baseMobile, stepDesktop, stepMobile, pageRef) => {
            const base = isMobile.value ? baseMobile : baseDesktop;
            const step = isMobile.value ? stepMobile : stepDesktop;
            return base + Math.max(0, pageRef.value) * step;
        };

        const dashboardRecentVisibleLimit = computed(() => getDashboardRenderLimit(
            DASHBOARD_RECENT_RENDER_LIMIT_DESKTOP,
            DASHBOARD_RECENT_RENDER_LIMIT_MOBILE,
            DASHBOARD_RECENT_RENDER_STEP_DESKTOP,
            DASHBOARD_RECENT_RENDER_STEP_MOBILE,
            dashboardRecentRenderPage
        ));

        const dashboardPlaybackVisibleLimit = computed(() => getDashboardRenderLimit(
            DASHBOARD_PLAYBACK_RENDER_LIMIT_DESKTOP,
            DASHBOARD_PLAYBACK_RENDER_LIMIT_MOBILE,
            DASHBOARD_PLAYBACK_RENDER_STEP_DESKTOP,
            DASHBOARD_PLAYBACK_RENDER_STEP_MOBILE,
            dashboardPlaybackRenderPage
        ));

        const dashboardLibraryVisibleLimit = computed(() => getDashboardRenderLimit(
            DASHBOARD_LIBRARY_RENDER_LIMIT_DESKTOP,
            DASHBOARD_LIBRARY_RENDER_LIMIT_MOBILE,
            DASHBOARD_LIBRARY_RENDER_STEP_DESKTOP,
            DASHBOARD_LIBRARY_RENDER_STEP_MOBILE,
            dashboardLibraryRenderPage
        ));

        const dashboardVisibleRecentItems = computed(() => {
            return dashboardRecentItems.value.slice(0, dashboardRecentVisibleLimit.value);
        });

        const dashboardVisibleRecentPlaybacks = computed(() => {
            return dashboardRecentPlaybacks.value.slice(0, dashboardPlaybackVisibleLimit.value);
        });

        const dashboardVisibleMediaLibraries = computed(() => {
            const libraries = Array.isArray(dashboardMediaStats.libraries) ? dashboardMediaStats.libraries : [];
            return libraries.slice(0, dashboardLibraryVisibleLimit.value);
        });

        const dashboardLazyLoadLastAt = { recent: 0, playback: 0, libraries: 0 };
        const dashboardLazyEnsurePending = { recent: false, playback: false, libraries: false };
        const dashboardLazySelectorMap = {
            recent: '.recent-media-row',
            playback: '.playback-hero-list',
            libraries: '.media-library-list',
        };

        const getDashboardLazyLoadState = (section) => {
            if (section === 'recent') {
                return {
                    total: dashboardRecentItems.value.length,
                    visible: dashboardVisibleRecentItems.value.length,
                    page: dashboardRecentRenderPage,
                };
            }
            if (section === 'playback') {
                return {
                    total: dashboardRecentPlaybacks.value.length,
                    visible: dashboardVisibleRecentPlaybacks.value.length,
                    page: dashboardPlaybackRenderPage,
                };
            }
            const libraries = Array.isArray(dashboardMediaStats.libraries) ? dashboardMediaStats.libraries : [];
            return {
                total: libraries.length,
                visible: dashboardVisibleMediaLibraries.value.length,
                page: dashboardLibraryRenderPage,
            };
        };

        const loadMoreDashboardSection = (section) => {
            const state = getDashboardLazyLoadState(section);
            if (state.visible >= state.total) return;
            state.page.value += 1;
            ensureDashboardLazyScrollable(section);
        };

        const ensureDashboardLazyScrollable = (section) => {
            if (dashboardLazyEnsurePending[section]) return;
            dashboardLazyEnsurePending[section] = true;
            nextTick(() => {
                requestAnimationFrame(() => {
                    dashboardLazyEnsurePending[section] = false;
                    if (tab.value !== 'dashboard') return;

                    const selector = dashboardLazySelectorMap[section];
                    const el = selector ? document.querySelector(selector) : null;
                    if (!el) return;

                    const state = getDashboardLazyLoadState(section);
                    if (state.visible >= state.total) return;

                    const hasHorizontalScroll = el.scrollWidth > el.clientWidth + 4;
                    const hasVerticalScroll = el.scrollHeight > el.clientHeight + 4;
                    if (!hasHorizontalScroll && !hasVerticalScroll) {
                        loadMoreDashboardSection(section);
                    }
                });
            });
        };

        const ensureDashboardLazyScrollableSections = () => {
            ensureDashboardLazyScrollable('recent');
            ensureDashboardLazyScrollable('playback');
            ensureDashboardLazyScrollable('libraries');
        };

        const onDashboardLazyScroll = (section, event) => {
            const el = event?.currentTarget;
            if (!el) return;

            const remainingX = el.scrollWidth - el.clientWidth - el.scrollLeft;
            const remainingY = el.scrollHeight - el.clientHeight - el.scrollTop;
            const nearHorizontalEnd = el.scrollWidth > el.clientWidth + 4 && remainingX < 96;
            const nearVerticalEnd = el.scrollHeight > el.clientHeight + 4 && remainingY < 96;
            if (!nearHorizontalEnd && !nearVerticalEnd) return;

            const now = Date.now();
            if (now - (dashboardLazyLoadLastAt[section] || 0) < 180) return;
            dashboardLazyLoadLastAt[section] = now;
            loadMoreDashboardSection(section);
        };

        watch(dashboardRecentItems, () => {
            dashboardRecentRenderPage.value = 0;
            ensureDashboardLazyScrollable('recent');
        });

        watch(dashboardRecentPlaybacks, () => {
            dashboardPlaybackRenderPage.value = 0;
            ensureDashboardLazyScrollable('playback');
        });

        watch(() => dashboardMediaStats.libraries, () => {
            dashboardLibraryRenderPage.value = 0;
            ensureDashboardLazyScrollable('libraries');
        });

        watch(isMobile, () => {
            dashboardRecentRenderPage.value = 0;
            dashboardPlaybackRenderPage.value = 0;
            dashboardLibraryRenderPage.value = 0;
            ensureDashboardLazyScrollableSections();
        });

        const fetchDashboardOverview = async (options = {}) => {
            const { forceRefresh = false, allowStale = true } = options;
            if (servers.value.length === 0) {
                resetDashboardOverview();
                dashboardOverviewLoaded.value = false;
                return;
            }
            const svr = servers.value[0];
            if (!svr.url || !svr.key) {
                resetDashboardOverview();
                dashboardOverviewLoaded.value = false;
                return;
            }

            const fingerprint = getDashboardOverviewServerFingerprint();
            const cached = getDashboardOverviewCache();
            const cacheMatches = !!(cached && fingerprint && cached.serverFingerprint === fingerprint);
            const cacheFresh = isDashboardOverviewCacheFresh(cached, fingerprint);
            const hasRenderableCache = !!(cacheMatches && cached?.data);

            if (allowStale && hasRenderableCache) {
                applyDashboardOverviewData(cached.data);
                dashboardOverviewLoaded.value = true;
            }

            const shouldRefresh = forceRefresh || !cacheFresh || allowStale;
            if (!shouldRefresh) {
                dashboardOverviewLoading.value = false;
                return;
            }

            dashboardOverviewLoading.value = true;
            const requestId = ++dashboardOverviewRequestSeq;
            try {
                const res = await axios.post('/api/dashboard_emby_overview', {
                    url: svr.url, key: svr.key, public_host: svr.public_host
                });
                if (requestId !== dashboardOverviewRequestSeq) return;
                const nextData = {
                    recent_items: res.data.recent_items || [],
                    recent_playbacks: res.data.recent_playbacks || [],
                    media_stats: res.data.media_stats || {}
                };
                applyDashboardOverviewData(nextData);
                dashboardOverviewLoaded.value = true;
                if (fingerprint) {
                    setDashboardOverviewCache({
                        version: DASHBOARD_OVERVIEW_CACHE_VERSION,
                        serverFingerprint: fingerprint,
                        updatedAt: Date.now(),
                        data: nextData
                    });
                }
            } catch (e) {
                if (!hasRenderableCache) {
                    resetDashboardOverview();
                    dashboardOverviewLoaded.value = false;
                }
                console.log('Dashboard overview failed', e);
            } finally {
                if (requestId === dashboardOverviewRequestSeq) {
                    dashboardOverviewLoading.value = false;
                }
            }
        };


    return {
        dashboardStats,
        dashboardRecentItems,
        dashboardVisibleRecentItems,
        dashboardRecentPlaybacks,
        dashboardVisibleRecentPlaybacks,
        dashboardVisibleMediaLibraries,
        dashboardMediaStats,
        onDashboardLazyScroll,
        dashboard115Account,
        dashboard115Loaded,
        handleDashboard115CardClick,
        dashboardCovers,
        wallRows,
        wallReady,
        dashboardOverviewLoading,
        initDashboard,
        fetchDashboardOverview,
        fetchDashboardStats,
        splitIntoRows,
        ensureDashboardLazyScrollableSections,
        startDashboard115Polling,
        stopDashboard115Polling,
        handleDashboardVisibilityChange,
        formatDashboardPlayedAt,
        getDashboardRecentSubtitle,
        openDashboardLibrary,
        openDashboardItem,
        ensureDashboardServerId,
    };
}
