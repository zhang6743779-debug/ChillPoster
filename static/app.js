const { createApp, reactive, ref, shallowRef, computed, onMounted, watch, onUnmounted, nextTick } = Vue;

createApp({
    setup() {
        const ACTIVE_TAB_STORAGE_KEY = 'chillposter-active-tab';
        const getInitialTab = () => {
            const savedTab = localStorage.getItem(ACTIVE_TAB_STORAGE_KEY);
            return savedTab && savedTab.trim() ? savedTab : 'dashboard';
        };
        const tab = ref(getInitialTab());
        const servers = ref([]);
        const syncServersFrom302 = () => {
            const previous = Array.isArray(servers.value) ? servers.value : [];
            const emby = Array.isArray(config302.embys) && config302.embys.length > 0 ? config302.embys[0] : null;
            const existing = previous[0] || {};
            const next = emby ? [{
                ...existing,
                ...emby,
                url: emby.url || '',
                key: emby.key || '',
                public_host: emby.public_host || '',
                name: emby.name || '',
                enabled: true,
                server_id: existing.server_id || '',
                libraries: existing.libraries,
                expanded: !!existing.expanded,
                testing: false,
                status: existing.status || emby.status || 'unknown'
            }] : [];
            servers.value = next;
            manualServerIdx.value = 0;
            previewServerIdx.value = 0;
            transServerIdx.value = 0;
        };
        const fontList = ref([]);
        const layoutList = ref([]); 
        const presetList = ref([]);
        const layoutGroups = ref([]); 
        const showCreateRss = ref(false);
        const showRssConfig = ref(false);
        const sidebarHover = ref(false);
        const isImmersiveMode = ref(false); // 手动控制菜单栏隐藏/显示

        // 切换菜单栏显示状态
        const toggleSidebar = () => {
            isImmersiveMode.value = !isImmersiveMode.value;
            sidebarHover.value = false; // 切换时重置 hover 状态
        };

        // ==========================================
        // macOS Dock 面板管理
        // ==========================================
        const isMobile = ref(window.innerWidth < 769);
        const openPanels = ref([]);
        const focusedPanel = ref(null);
        const showSettingsDrawer = ref(false);
        const showCoverDrawer = ref(false);
        const showStorageDrawer = ref(false);
        const showToolboxDrawer = ref(false);
        const showSpotlight = ref(false);
        const spotlightQuery = ref('');
        const spotlightFocusIndex = ref(0);
        const dockHoverIndex = ref(null);
        const spotlightInputRef = ref(null);
        const theme = ref('dark');
        const THEME_STORAGE_KEY = 'chillposter-theme';

        const applyTheme = (nextTheme) => {
            const normalizedTheme = nextTheme === 'light' ? 'light' : 'dark';
            theme.value = normalizedTheme;
            document.documentElement.dataset.theme = normalizedTheme;
            localStorage.setItem(THEME_STORAGE_KEY, normalizedTheme);
        };

        const resolveInitialTheme = () => {
            const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
            if (savedTheme === 'light' || savedTheme === 'dark') {
                return savedTheme;
            }
            return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
        };

        const toggleTheme = () => {
            applyTheme(theme.value === 'light' ? 'dark' : 'light');
        };

        // Dock 主栏图标 (11 个高频功能)
        const dockItems = [
            { id: 'media_subscribe', icon: 'fa-compass', label: '发现推荐', group: '网盘一条龙' },
        ];

        const storageItems = [
            { id: 'resource_transfer', icon: 'fa-cloud-arrow-down', label: '资源转存', group: '网盘一条龙' },
            { id: 'media_organize', icon: 'fa-folder-tree', label: '媒体整理', group: '网盘一条龙' },
            { id: 'strm_generate', icon: 'fa-file-code', label: 'STRM同步', group: '网盘一条龙' },
            { id: 'rename_template', icon: 'fa-pen-fancy', label: '重命名模板', group: '网盘一条龙' },
            { id: 'media_organize_rules', icon: 'fa-sitemap', label: '二级分类', group: '网盘一条龙' },
        ];

        const coverItems = [
            { id: 'manual', icon: 'fa-pen-ruler', label: '手动封面', group: '封面系统' },
            { id: 'custom', icon: 'fa-paintbrush', label: '封面设计', group: '封面系统' },
            { id: 'auto', icon: 'fa-robot', label: '自动封面', group: '封面系统' },
            { id: 'library_preview', icon: 'fa-images', label: '封面备份', group: '封面系统' },
            { id: 'fonts', icon: 'fa-font', label: '字体管理', group: '封面系统' },
            { id: 'templates', icon: 'fa-swatchbook', label: '模板管理', group: '封面系统' },
            { id: 'translations', icon: 'fa-language', label: '翻译配置', group: '封面系统' },
        ];

        const toolboxItems = [
            { id: 'rss', icon: 'fa-rss', label: 'RSS真实库', group: '工具箱' },
            { id: 'drive115_cleanup', icon: 'fa-broom', label: '115定时清空', group: '工具箱' },
            { id: 'config_yingchao', icon: 'fa-film', label: '影巢配置', group: '工具箱' },
            { id: 'webhook', icon: 'fa-bolt-lightning', label: 'Webhook', group: '工具箱' },
        ];

        // 设置抽屉项 (13 个低频功能)
        const settingsItems = [
            { id: 'server', icon: 'fa-server', label: 'Emby 配置', group: '核心配置' },
            { id: 'config_115', icon: 'fa-cloud', label: '115 配置', group: '核心配置' },
            { id: 'config_notification', icon: 'fa-bell', label: '通知配置', group: '核心配置' },
            { id: 'config_moviepilot', icon: 'fa-plane', label: 'MoviePilot', group: '核心配置' },
            { id: 'config_proxy', icon: 'fa-globe', label: '代理配置', group: '核心配置' },
            { id: 'config_tmdb', icon: 'fa-database', label: 'TMDB 配置', group: '核心配置' },
            { id: 'upgrade', icon: 'fa-cloud-arrow-up', label: '系统升级', group: '核心配置' },
            { id: 'account', icon: 'fa-user-gear', label: '账户管理', group: '核心配置' },
        ];

        // 所有可搜索项
        const allSearchItems = [
            { id: 'dashboard', icon: 'fa-house', label: '仪表盘', group: '首页' },
            ...dockItems,
            ...settingsItems,
        ];

        const allValidTabs = new Set([
            'dashboard',
            'manual',
            'custom',
            'auto',
            'library_preview',
            'fonts',
            'templates',
            'translations',
            'rss',
            'drive115_cleanup',
            'webhook',
            'media_subscribe',
            'resource_transfer',
            'media_organize',
            'media_organize_rules',
            'strm_generate',
            'server',
            'config_115',
            'config_notification',
            'config_yingchao',
            'config_moviepilot',
            'config_proxy',
            'config_tmdb',
            'upgrade',
            'account',
        ]);

        // 获取面板图标
        const getPanelIcon = (id) => {
            const item = allSearchItems.find(i => i.id === id);
            return item ? item.icon : 'fa-circle';
        };

        // 获取面板标签
        const getPanelLabel = (id) => {
            const item = allSearchItems.find(i => i.id === id);
            return item ? item.label : id;
        };

        // 切换面板 (核心逻辑)
        const togglePanel = (panelId) => {
            if (focusedPanel.value === panelId) {
                // 已是焦点 → 关闭
                closePanel(panelId);
            } else if (openPanels.value.includes(panelId)) {
                // 已打开但失焦 → 提升焦点
                focusPanel(panelId);
            } else {
                // 未打开 → 打开并聚焦
                openPanels.value = [panelId]; // 单面板模式
                focusedPanel.value = panelId;
                tab.value = panelId;
            }
            showSettingsDrawer.value = false;
        };

        // 关闭面板
        const closePanel = (panelId) => {
            openPanels.value = openPanels.value.filter(id => id !== panelId);
            if (focusedPanel.value === panelId) {
                // 聚焦到另一个面板或回到 dashboard
                if (openPanels.value.length > 0) {
                    focusedPanel.value = openPanels.value[openPanels.value.length - 1];
                    tab.value = focusedPanel.value;
                } else {
                    focusedPanel.value = null;
                    tab.value = 'dashboard';
                }
            }
        };

        // 聚焦面板
        const focusPanel = (panelId) => {
            if (openPanels.value.includes(panelId)) {
                focusedPanel.value = panelId;
                tab.value = panelId;
            }
        };

        // 回到首页
        const goHome = () => {
            openPanels.value = [];
            focusedPanel.value = null;
            tab.value = 'dashboard';
            closeDockDrawers();
        };

        const buildDrawerStyle = (e, drawerWidth = 480) => {
            if (!e) return {};
            const btn = e.currentTarget;
            const rect = btn.getBoundingClientRect();
            const btnCenterX = rect.left + rect.width / 2;
            let left = btnCenterX - drawerWidth / 2;
            left = Math.max(8, Math.min(left, window.innerWidth - drawerWidth - 8));
            return {
                position: 'fixed',
                bottom: (window.innerHeight - rect.top + 12) + 'px',
                left: left + 'px',
                right: 'auto',
                width: drawerWidth + 'px',
                borderRadius: '16px',
            };
        };

        const closeDockDrawers = () => {
            showSettingsDrawer.value = false;
            showCoverDrawer.value = false;
            showStorageDrawer.value = false;
            showToolboxDrawer.value = false;
        };

        // 设置抽屉
        const settingsDrawerStyle = ref({});
        const coverDrawerStyle = ref({});
        const storageDrawerStyle = ref({});
        const toolboxDrawerStyle = ref({});
        const toggleSettingsDrawer = (e) => {
            if (isMobile.value) return;
            const nextState = !showSettingsDrawer.value;
            closeDockDrawers();
            showSettingsDrawer.value = nextState;
            if (showSettingsDrawer.value) {
                settingsDrawerStyle.value = buildDrawerStyle(e);
            }
        };

        const toggleCoverDrawer = (e) => {
            if (isMobile.value) return;
            const nextState = !showCoverDrawer.value;
            closeDockDrawers();
            showCoverDrawer.value = nextState;
            if (showCoverDrawer.value) {
                coverDrawerStyle.value = buildDrawerStyle(e);
            }
        };

        const toggleStorageDrawer = (e) => {
            if (isMobile.value) return;
            const nextState = !showStorageDrawer.value;
            closeDockDrawers();
            showStorageDrawer.value = nextState;
            if (showStorageDrawer.value) {
                storageDrawerStyle.value = buildDrawerStyle(e);
            }
        };

        const toggleToolboxDrawer = (e) => {
            if (isMobile.value) return;
            const nextState = !showToolboxDrawer.value;
            closeDockDrawers();
            showToolboxDrawer.value = nextState;
            if (showToolboxDrawer.value) {
                toolboxDrawerStyle.value = buildDrawerStyle(e);
            }
        };

        // 从抽屉打开面板
        const openFromSettings = (id) => {
            closeDockDrawers();
            togglePanel(id);
        };

        // Spotlight 搜索
        const showSpotlightPanel = () => {
            if (isMobile.value) return;
            showSpotlight.value = true;
            spotlightQuery.value = '';
            spotlightFocusIndex.value = 0;
            nextTick(() => {
                if (spotlightInputRef.value) spotlightInputRef.value.focus();
            });
        };

        const spotlightResults = computed(() => {
            const q = spotlightQuery.value.toLowerCase().trim();
            if (!q) return allSearchItems;
            return allSearchItems.filter(item =>
                item.label.toLowerCase().includes(q) ||
                item.id.toLowerCase().includes(q) ||
                item.group.toLowerCase().includes(q)
            );
        });

        const jumpToItem = (id) => {
            showSpotlight.value = false;
            if (id === 'dashboard') {
                goHome();
            } else {
                // 确保面板打开并聚焦
                if (!openPanels.value.includes(id)) {
                    openPanels.value = [id];
                }
                focusedPanel.value = id;
                tab.value = id;
            }
        };

        const selectSpotlightResult = () => {
            if (spotlightResults.value.length > 0) {
                const item = spotlightResults.value[spotlightFocusIndex.value];
                if (item) jumpToItem(item.id);
            }
        };

        const spotlightUp = () => {
            if (spotlightFocusIndex.value > 0) {
                spotlightFocusIndex.value--;
            }
        };

        const spotlightDown = () => {
            if (spotlightFocusIndex.value < spotlightResults.value.length - 1) {
                spotlightFocusIndex.value++;
            }
        };

        // 键盘快捷键
        const handleKeydown = (e) => {
            // Cmd+K / Ctrl+K → Spotlight
            if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
                e.preventDefault();
                showSpotlightPanel();
            }
            // Escape → 关闭 spotlight / drawers
            if (e.key === 'Escape') {
                if (showSpotlight.value) {
                    showSpotlight.value = false;
                } else if (showSettingsDrawer.value || showCoverDrawer.value || showStorageDrawer.value) {
                    closeDockDrawers();
                }
            }
        };

        const closeDesktopOverlays = () => {
            closeDockDrawers();
            showSpotlight.value = false;
            dockHoverIndex.value = null;
            spotlightQuery.value = '';
            spotlightFocusIndex.value = 0;
        };

        // 监听窗口大小变化
        const handleResize = () => {
            const nextIsMobile = window.innerWidth < 769;
            if (nextIsMobile && !isMobile.value) {
                closeDesktopOverlays();
            }
            isMobile.value = nextIsMobile;
            if (dashboardCovers.value.length > 0) {
                splitIntoRows();
            }
        };
        
        // ==========================================
        // 0. 版本号与当前用户
        // ==========================================
        const projectVersion = ref('vdev');
        const currentUsername = ref(localStorage.getItem('username') || 'Administrator');
        const upgradeStatus = reactive({
            loading: false,
            checking: false,
            upgrading: false,
            waitingRestart: false,
            enabled: true,
            available: false,
            mode: 'docker',
            selected_mode: 'docker',
            current_version: '',
            latest_version: '',
            update_available: false,
            image: '',
            docker_available: false,
            container_id: '',
            message: ''
        });

        const loadProjectVersion = async () => {
            try {
                const res = await axios.get('/api/version');
                if (res.data?.version) projectVersion.value = res.data.version;
            } catch (e) { }
        };

        const fetchUpgradeStatus = async (force = false) => {
            upgradeStatus.loading = true;
            try {
                const res = force
                    ? await axios.post('/api/upgrade/check', { force: true })
                    : await axios.get('/api/upgrade/status');
                Object.assign(upgradeStatus, res.data || {});
            } catch (e) {
                upgradeStatus.available = false;
                upgradeStatus.message = e.response?.data?.detail || e.message || '升级状态获取失败';
            } finally {
                upgradeStatus.loading = false;
            }
        };

        const checkUpgrade = async () => {
            upgradeStatus.checking = true;
            try {
                await fetchUpgradeStatus(true);
                showToast(upgradeStatus.latest_version ? '版本检查完成' : '无法获取最新版本', upgradeStatus.latest_version ? 'success' : 'info');
            } finally {
                upgradeStatus.checking = false;
            }
        };

        const waitForUpgradeRestart = async (previousVersion, runId) => {
            upgradeStatus.waitingRestart = true;
            const startedAt = Date.now();
            let sawDisconnect = false;
            let taskFinished = false;
            let restartPhase = false;
            while (Date.now() - startedAt < 10 * 60 * 1000) {
                await new Promise(resolve => setTimeout(resolve, 3000));
                try {
                    const progress = await axios.get('/api/progress', { timeout: 5000 });
                    const task = runId ? progress.data?.[runId] : null;
                    if (task?.status === 'error') {
                        upgradeStatus.waitingRestart = false;
                        upgradeStatus.upgrading = false;
                        showToast(task.detail?.message || '升级任务失败', 'error');
                        return;
                    }
                    if (task?.status === 'finished') taskFinished = true;
                    if ((task?.percent || 0) >= 85 || ['recreate', 'restarting', 'done'].includes(task?.detail?.step)) {
                        restartPhase = true;
                    }
                } catch (e) { }

                try {
                    const res = await axios.get('/api/version', { timeout: 5000 });
                    const nextVersion = res.data?.version || '';
                    if (!nextVersion) continue;
                    if (nextVersion !== previousVersion || sawDisconnect || taskFinished) {
                        projectVersion.value = nextVersion;
                        upgradeStatus.waitingRestart = false;
                        upgradeStatus.upgrading = false;
                        await fetchUpgradeStatus(true);
                        showToast(nextVersion !== previousVersion ? `升级完成: ${nextVersion}` : '服务已恢复，版本未变化', nextVersion !== previousVersion ? 'success' : 'info');
                        return;
                    }
                    if (!restartPhase) continue;
                } catch (e) {
                    sawDisconnect = true;
                    restartPhase = true;
                }
            }
            upgradeStatus.waitingRestart = false;
            upgradeStatus.upgrading = false;
            showToast('升级命令已发出，但等待服务恢复超时，请检查容器日志', 'error');
        };

        const startUpgrade = async () => {
            const ok = await showConfirm('系统升级', '将拉取最新镜像并使用 Docker 直接模式重建当前容器，页面会短暂断开。确定继续吗？', 'warning');
            if (!ok) return;
            upgradeStatus.upgrading = true;
            const previousVersion = projectVersion.value;
            try {
                const res = await axios.post('/api/upgrade/start', {});
                showToast(res.data?.message || '升级任务已启动', 'info');
                waitForUpgradeRestart(previousVersion, res.data?.run_id || '');
            } catch (e) {
                upgradeStatus.upgrading = false;
                showToast('升级启动失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        // ==========================================
        // 1. Toast 通知系统
        // ==========================================
        const toasts = ref([]);
        let toastId = 0;
        
        const showToast = (msg, type = 'success') => {
            const id = toastId++;
            let icon = 'fa-circle-check';
            if (type === 'error') icon = 'fa-circle-xmark';
            if (type === 'info') icon = 'fa-circle-info';
            
            toasts.value.push({ id, msg, type, icon });
            
            setTimeout(() => {
                const idx = toasts.value.findIndex(t => t.id === id);
                if (idx !== -1) toasts.value.splice(idx, 1);
            }, 3000);
        };

        // ==========================================
        // 2. Confirm 弹窗系统
        // ==========================================
        const confirmState = reactive({
            visible: false, title: '', msg: '', type: 'warning', icon: 'fa-triangle-exclamation',
            confirmText: '确定', confirmBtnClass: 'btn-primary', resolve: null
        });

        // 选择对话框状态
        const selectState = reactive({
            visible: false, title: '', msg: '', options: [], resolve: null
        });

        const numberDialogState = reactive({
            visible: false,
            title: '',
            msg: '',
            placeholder: '',
            suffix: '%',
            value: '',
            validator: null,
            resolve: null,
        });

        const showConfirm = (title, msg, type = 'warning') => {
            return new Promise((resolve) => {
                confirmState.title = title;
                confirmState.msg = msg;
                confirmState.type = type;
                confirmState.visible = true;
                confirmState.resolve = resolve;

                if (type === 'danger') {
                    confirmState.icon = 'fa-trash-can';
                    confirmState.confirmText = '确认删除';
                    confirmState.confirmBtnClass = 'btn-danger';
                } else if (type === 'warning') {
                    confirmState.icon = 'fa-triangle-exclamation';
                    confirmState.confirmText = '确定执行';
                    confirmState.confirmBtnClass = 'btn-primary';
                } else {
                    confirmState.icon = 'fa-circle-info';
                    confirmState.confirmText = '确定';
                    confirmState.confirmBtnClass = 'btn-primary';
                }
            });
        };

        const handleConfirm = (result) => {
            confirmState.visible = false;
            if (confirmState.resolve) { confirmState.resolve(result); confirmState.resolve = null; }
        };

        const showSelectDialog = (title, msg, options) => {
            return new Promise((resolve) => {
                selectState.title = title;
                selectState.msg = msg;
                selectState.options = options;
                selectState.visible = true;
                selectState.resolve = resolve;
            });
        };

        const handleSelect = (index) => {
            selectState.visible = false;
            if (selectState.resolve) { selectState.resolve(index); selectState.resolve = null; }
        };

        const closeSelectDialog = () => {
            selectState.visible = false;
            if (selectState.resolve) { selectState.resolve(null); selectState.resolve = null; }
        };

        const showNumberDialog = (title, msg, defaultValue = '', placeholder = '', validator = null) => {
            return new Promise((resolve) => {
                numberDialogState.title = title;
                numberDialogState.msg = msg;
                numberDialogState.value = defaultValue === null || defaultValue === undefined ? '' : String(defaultValue);
                numberDialogState.placeholder = placeholder;
                numberDialogState.validator = validator;
                numberDialogState.visible = true;
                numberDialogState.resolve = resolve;
            });
        };

        const handleNumberDialog = (result) => {
            const value = numberDialogState.value;
            if (result && typeof numberDialogState.validator === 'function') {
                const errorMessage = numberDialogState.validator(value);
                if (errorMessage) {
                    showToast(errorMessage, 'error');
                    return;
                }
            }
            numberDialogState.visible = false;
            numberDialogState.validator = null;
            if (numberDialogState.resolve) {
                numberDialogState.resolve(result ? value : null);
                numberDialogState.resolve = null;
            }
        };

        const closeNumberDialog = () => {
            handleNumberDialog(false);
        };

        // ==========================================
        // 3. 任务与核心逻辑
        // ==========================================
        
        const TASK_LOGS_STORAGE_KEY = 'dashboard_task_logs';
        const tasksState = reactive({
            activeTasks: {}, hasRunning: false, logs: [], logVisible: false, isPolling: false
        });
        
        const consoleLogState = reactive({
            visible: false,
            content: '',
            autoRefresh: true,
            loading: false,
            streaming: false,
            levelFilter: 'INFO',
            categoryFilter: 'ALL',
            keywordFilter: '',
            keywordInput: '',
            maxLines: 1000,
            lastEventId: null,
            partialLineBuffer: '',
        });
        const parsedLogs = shallowRef([]);

        const dashboardStats = reactive({ tasks: 0, backups: 0, fonts: 0 });
        const dashboardRecentItems = ref([]);
        const dashboardRecentPlaybacks = ref([]);
        const dashboardMediaStats = reactive({ total: 0, movie_count: 0, series_count: 0, episode_count: 0, user_count: 0, movie_libraries: 0, series_libraries: 0, other_libraries: 0, libraries: [] });
        const DASHBOARD_DEVICE_POLL_INTERVAL = 2000;
        const DASHBOARD_DEVICE_HISTORY_LIMIT = 40;
        const DASHBOARD_SPARKLINE_VIEWBOX = '0 0 100 30';
        const DASHBOARD_SPARKLINE_WIDTH = 100;
        const DASHBOARD_SPARKLINE_TOP = 3;
        const DASHBOARD_SPARKLINE_BOTTOM = 20;
        const DASHBOARD_SPARKLINE_BASELINE = 26;
        const dashboardDeviceMetrics = reactive({
            cpu: { percent: 0 },
            memory: { percent: 0, used_gb: 0, total_gb: 0 },
            network: { up_bytes_per_sec: 0, down_bytes_per_sec: 0, up_human: '0 B/s', down_human: '0 B/s' },
            disk: { read_bytes_per_sec: 0, write_bytes_per_sec: 0, read_human: '0 B/s', write_human: '0 B/s' },
            timestamp: null,
        });
        const dashboardDeviceMetricHistory = reactive({
            cpuPercent: [],
            memoryPercent: [],
            uploadBytes: [],
            downloadBytes: [],
            diskReadBytes: [],
            diskWriteBytes: [],
        });
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
        const dashboardDeviceMetricsPulse = ref(false);
        const dashboardCovers = ref([]);
        const wallRows = reactive([[], [], [], []]);
        const wallReady = ref(false);
        const dashboardOverviewLoading = ref(false);
        const dashboardOverviewLoaded = ref(false);
        const dashboardDeviceMetricsLoaded = ref(false);
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

        let pollInterval = null;
        let dashboardDeviceMetricsPolling = null;
        let dashboard115Polling = null;

        const persistTaskLogs = () => {
            try {
                localStorage.setItem(TASK_LOGS_STORAGE_KEY, JSON.stringify(tasksState.logs));
            } catch (_) {}
        };

        const hydrateTaskLogs = () => {
            try {
                const raw = localStorage.getItem(TASK_LOGS_STORAGE_KEY);
                if (!raw) return;
                const parsed = JSON.parse(raw);
                if (!Array.isArray(parsed)) return;
                tasksState.logs = parsed
                    .filter(item => item && typeof item.msg === 'string')
                    .slice(0, 50)
                    .map(item => ({
                        type: typeof item.type === 'string' ? item.type : 'info',
                        msg: item.msg,
                        time: typeof item.time === 'string' ? item.time : '',
                    }));
            } catch (_) {
                tasksState.logs = [];
            }
        };

        const addLog = (type, msg) => {
            const time = new Date().toLocaleTimeString();
            tasksState.logs.unshift({ type, msg, time });
            if (tasksState.logs.length > 50) tasksState.logs.pop();
            persistTaskLogs();
        };

        const clearLogs = () => {
            tasksState.logs = [];
            persistTaskLogs();
        };

        const stopTask = async (runId) => {
            const ok = await showConfirm('停止任务', '确定要强制停止当前正在运行的任务吗？', 'danger');
            if(!ok) return;
            try {
                await axios.post('/api/stop_task', { run_id: runId });
                showToast("已发送停止请求...", "info");
            } catch {
                showToast("停止失败", "error");
            }
        };

        const processedTaskIds = new Set();
        let isFirstPoll = true;

        const startHdhiveEventStream = () => {
            const es = new EventSource('/api/hdhive/events');
            es.onmessage = (e) => {
                try {
                    const data = JSON.parse(e.data);
                    if (data.type === 'checkin_success') {
                        fetchHdhiveConfig();
                    }
                } catch (_) {}
            };
            es.onerror = () => {
                es.close();
                setTimeout(startHdhiveEventStream, 5000);
            };
        };

        const startPolling = () => {
            if (tasksState.isPolling) return;
            tasksState.isPolling = true;

            pollInterval = setInterval(async () => {
                try {
                    const res = await axios.get('/api/progress');
                    const activeMap = res.data;
                    tasksState.activeTasks = activeMap;
                    let running = false;

                    // 首次加载：记录所有终态任务，不弹通知
                    if (isFirstPoll) {
                        for (const id in activeMap) {
                            const task = activeMap[id];
                            if (task.status === 'finished' || task.status === 'error' || task.status === 'stopped') {
                                processedTaskIds.add(id);
                            }
                        }
                        isFirstPoll = false;
                        return;
                    }

                    for (const id in activeMap) {
                        const task = activeMap[id];
                        if (task.status === 'running') running = true;

                        if (task.status === 'finished' || task.status === 'error' || task.status === 'stopped') {
                            if (!processedTaskIds.has(id)) {
                                const label = task.status === 'finished' ? '完成' : (task.status === 'stopped' ? '已取消' : '失败');
                                const msgText = `${task.name} ${label}`;
                                addLog(task.status === 'finished' ? 'success' : 'error', msgText);

                                if (task.status === 'finished') {
                                    showToast(msgText, 'success');
                                    if (task.name && task.name.startsWith('RSS')) {
                                        console.log("RSS任务完成，自动刷新媒体库...");
                                        refreshAllLibraries();
                                    }
                                    if (task.name && task.name.includes('备份')) fetchSuites();
                                } else {
                                    showToast(msgText, 'error');
                                }

                                processedTaskIds.add(id);

                                setTimeout(() => axios.post('/api/clear_task_progress', { run_id: id }), 3000);
                            }
                        }
                    }
                    
                    for (const cachedId of processedTaskIds) {
                        if (!activeMap[cachedId]) {
                            processedTaskIds.delete(cachedId);
                        }
                    }

                    tasksState.hasRunning = running;
                } catch { }
            }, 1000);
        };

        const stopPolling = () => {
            if (pollInterval) clearInterval(pollInterval);
            tasksState.isPolling = false;
            isFirstPoll = true;
        };

        const pushDashboardMetricSample = (queue, value) => {
            const nextValue = Number(value);
            queue.push(Number.isFinite(nextValue) ? nextValue : 0);
            while (queue.length > DASHBOARD_DEVICE_HISTORY_LIMIT) {
                queue.shift();
            }
        };

        const resetDashboardDeviceMetricHistory = () => {
            Object.values(dashboardDeviceMetricHistory).forEach((queue) => queue.splice(0, queue.length));
        };

        const recordDashboardDeviceMetricHistory = () => {
            pushDashboardMetricSample(dashboardDeviceMetricHistory.cpuPercent, dashboardDeviceMetrics.cpu.percent);
            pushDashboardMetricSample(dashboardDeviceMetricHistory.memoryPercent, dashboardDeviceMetrics.memory.percent);
            pushDashboardMetricSample(dashboardDeviceMetricHistory.uploadBytes, dashboardDeviceMetrics.network.up_bytes_per_sec);
            pushDashboardMetricSample(dashboardDeviceMetricHistory.downloadBytes, dashboardDeviceMetrics.network.down_bytes_per_sec);
            pushDashboardMetricSample(dashboardDeviceMetricHistory.diskReadBytes, dashboardDeviceMetrics.disk.read_bytes_per_sec);
            pushDashboardMetricSample(dashboardDeviceMetricHistory.diskWriteBytes, dashboardDeviceMetrics.disk.write_bytes_per_sec);
        };

        const resetDashboardDeviceMetrics = () => {
            Object.assign(dashboardDeviceMetrics, {
                cpu: { percent: 0 },
                memory: { percent: 0, used_gb: 0, total_gb: 0 },
                network: { up_bytes_per_sec: 0, down_bytes_per_sec: 0, up_human: '0 B/s', down_human: '0 B/s' },
                disk: { read_bytes_per_sec: 0, write_bytes_per_sec: 0, read_human: '0 B/s', write_human: '0 B/s' },
                timestamp: null,
            });
            resetDashboardDeviceMetricHistory();
        };

        const fetchDashboardDeviceMetrics = async () => {
            try {
                const res = await axios.get('/api/dashboard_device_metrics');
                Object.assign(dashboardDeviceMetrics, {
                    cpu: { percent: 0 },
                    memory: { percent: 0, used_gb: 0, total_gb: 0 },
                    network: { up_bytes_per_sec: 0, down_bytes_per_sec: 0, up_human: '0 B/s', down_human: '0 B/s' },
                    disk: { read_bytes_per_sec: 0, write_bytes_per_sec: 0, read_human: '0 B/s', write_human: '0 B/s' },
                    timestamp: null,
                }, res.data || {});
                recordDashboardDeviceMetricHistory();
                dashboardDeviceMetricsLoaded.value = true;
                dashboardDeviceMetricsPulse.value = false;
                requestAnimationFrame(() => {
                    dashboardDeviceMetricsPulse.value = true;
                });
                setTimeout(() => {
                    dashboardDeviceMetricsPulse.value = false;
                }, 380);
            } catch (e) {
                console.log('Dashboard device metrics failed', e);
                dashboardDeviceMetricsLoaded.value = false;
            }
        };

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

        const stopDashboardDeviceMetricsPolling = () => {
            if (dashboardDeviceMetricsPolling) {
                clearInterval(dashboardDeviceMetricsPolling);
                dashboardDeviceMetricsPolling = null;
            }
        };

        const startDashboardDeviceMetricsPolling = () => {
            stopDashboardDeviceMetricsPolling();
            if (tab.value !== 'dashboard') return;
            fetchDashboardDeviceMetrics();
            dashboardDeviceMetricsPolling = setInterval(() => {
                if (tab.value !== 'dashboard') return;
                fetchDashboardDeviceMetrics();
            }, DASHBOARD_DEVICE_POLL_INTERVAL);
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

        const normalizeLogLevel = (level) => {
            const raw = String(level || 'INFO').toUpperCase();
            if (raw === 'WARN') return 'WARNING';
            if (raw === 'ERR') return 'ERROR';
            if (!['INFO', 'DEBUG', 'WARNING', 'ERROR'].includes(raw)) return 'INFO';
            return raw;
        };

        const decorateLogLevel = (level) => {
            if (level === 'ERROR') return { icon: 'fa-times', statusClass: 'error', badgeClass: 'error' };
            if (level === 'WARNING') return { icon: 'fa-exclamation', statusClass: 'warning', badgeClass: 'warning' };
            if (level === 'DEBUG') return { icon: 'fa-bug', statusClass: 'debug', badgeClass: 'debug' };
            return { icon: 'fa-check', statusClass: 'success', badgeClass: 'info' };
        };

        const pickLogEmoji = (message, level = 'INFO') => {
            const normalizedLevel = normalizeLogLevel(level);
            if (normalizedLevel === 'ERROR') return '❌';
            if (normalizedLevel === 'WARNING') return '⚠️';
            if (normalizedLevel === 'DEBUG') return '🐞';

            const text = String(message || '').toLowerCase();

            const rules = [
                { test: /webhook|回调/, emoji: '🪝' },
                { test: /telegram|polling|getupdates/, emoji: '📨' },
                { test: /rss|订阅/, emoji: '📰' },
                { test: /定时任务|scheduler|cron/, emoji: '🔄' },
                { test: /播放|并发|锁定用户/, emoji: '🛰️' },
                { test: /115|life|网盘|转存/, emoji: '☁️' },
                { test: /清理|删除/, emoji: '🧹' },
                { test: /代理|proxy/, emoji: '🌐' },
                { test: /启动|初始化|已启动|已加载|恢复/, emoji: '🚀' },
                { test: /完成|成功|finished|ok/, emoji: '✅' },
                { test: /跳过/, emoji: '⏭️' }
            ];

            for (const rule of rules) {
                if (rule.test.test(text)) return rule.emoji;
            }
            return '📝';
        };

        const LOG_CATEGORY_KEYWORDS = {
            PLAYBACK_302: ['播放信息接口触发预加载', '后台预加载成功', '后台预加载失败', 'Pickcode模式检测', '从Path提取Pickcode成功', 'Pickcode提取成功', '开始获取直链', '直链获取成功', '命中直链缓存', '收到播放请求', '302重定向到115直链', '收到 STRM 直连请求', 'STRM 302重定向到115直链', '播放通知去重', '115直链获取失败，已降级反向代理', 'STRM 直链获取失败，已降级反向代理'],
            MEDIA_ORGANIZE: ['[MediaOrganize]', '[媒体库缓存]', '[Wash]', '[CategoryDir]', '[EmbyLib]', '整理:', '洗版'],
            DRIVE_115: ['[115]', '[115-', '[115Life]', '[Rapid]', '[Sync-', '[115风控', '网盘'],
            STRM: ['[STRM]', 'STRM', 'strm'],
            NOTIFY: ['微信', 'wechat', 'Telegram', 'telegram', '通知'],
            SCHEDULER: ['[Scheduler]', '定时任务', '任务', 'cron'],
            DIAGNOSTIC: ['失败', '异常', '超时', '990009', '风控', 'Traceback', '错误'],
            TMDB_SCRAPE: ['TMDb', 'TMDB', '刮削', '元数据', '图片下载'],
        };

        const detectLogCategory = (message) => {
            const text = String(message || '');
            for (const [category, keywords] of Object.entries(LOG_CATEGORY_KEYWORDS)) {
                if (keywords.some(keyword => text.includes(keyword))) return category;
            }
            return 'ALL';
        };

        const parseLogLine = (line) => {
            if (!line || !line.trim()) return null;

            const parts = line.split(' - ');
            let timestamp = '';
            let level = 'INFO';
            let message = line.trim();

            if (parts.length >= 3) {
                const timeParts = parts[0].trim().split(' ');
                if (timeParts.length > 1) {
                    timestamp = timeParts[1];
                }
                level = normalizeLogLevel(parts[1].trim());
                message = parts.slice(2).join(' - ').trim();
                message = message.replace(/^\[[\w\s]+\]\s*/, '');
            }

            const decorated = decorateLogLevel(level);
            return {
                timestamp,
                level,
                category: detectLogCategory(message),
                message,
                emoji: pickLogEmoji(message, level),
                icon: decorated.icon,
                statusClass: decorated.statusClass,
                badgeClass: decorated.badgeClass
            };
        };

        // 解析日志内容为结构化数据（兜底全量）
        const parseLogContent = (content) => {
            if (!content || content.trim() === '') return [];
            const lines = content.split('\n');
            const parsed = [];
            for (const line of lines) {
                const row = parseLogLine(line);
                if (row) parsed.push(row);
            }
            return parsed;
        };

        // --- 虚拟列表状态 ---
        const LOG_ITEM_H = 26;     // 每行高度 px
        const LOG_OVERSCAN = 20;   // 上下多渲染的缓冲行数
        const logContainerRef = ref(null);
        const logScrollTop = ref(0);

        const filteredLogs = computed(() => {
            const level = consoleLogState.levelFilter;
            const category = consoleLogState.categoryFilter;
            const keyword = (consoleLogState.keywordFilter || '').toLowerCase();
            return parsedLogs.value.filter(item => {
                const levelMatch = level === 'ALL' || item.level === level;
                const categoryMatch = category === 'ALL' || item.category === category;
                const keywordMatch = !keyword || item.message.toLowerCase().includes(keyword) || item.level.toLowerCase().includes(keyword);
                return levelMatch && categoryMatch && keywordMatch;
            });
        });

        const logVirtualState = computed(() => {
            const items = filteredLogs.value;
            const total = items.length;
            const totalH = total * LOG_ITEM_H;
            const start = Math.max(0, Math.floor(logScrollTop.value / LOG_ITEM_H) - LOG_OVERSCAN);
            const containerH = logContainerRef.value ? logContainerRef.value.clientHeight : 600;
            const end = Math.min(total, Math.ceil((logScrollTop.value + containerH) / LOG_ITEM_H) + LOG_OVERSCAN);
            return { items: items.slice(start, end), start, totalH, offsetY: start * LOG_ITEM_H };
        });

        const onLogScroll = () => {
            const el = logContainerRef.value;
            if (el) logScrollTop.value = el.scrollTop;
        };

        const copyLogLine = (log) => {
            const text = `[${log.level}] ${log.timestamp} ${log.message}`;
            navigator.clipboard.writeText(text).then(() => showToast('已复制日志', 'success')).catch(() => {});
        };

        const scrollConsoleLogToBottom = () => {
            nextTick(() => {
                const el = logContainerRef.value;
                if (el) {
                    logScrollTop.value = el.scrollHeight - el.clientHeight;
                    el.scrollTop = el.scrollHeight;
                }
            });
        };

        let _logBatchTimer = null;
        let _logBatchBuffer = [];

        const _flushLogBatch = () => {
            if (_logBatchBuffer.length === 0) return;
            const batch = _logBatchBuffer.splice(0);
            const merged = [...parsedLogs.value, ...batch];
            const excess = merged.length - consoleLogState.maxLines;
            parsedLogs.value = excess > 0 ? merged.slice(excess) : merged;
            scrollConsoleLogToBottom();
        };

        const appendSystemLogChunk = (chunk) => {
            if (!chunk) return;

            consoleLogState.content = (consoleLogState.content || '') + chunk;
            consoleLogState.partialLineBuffer += chunk;

            const lines = consoleLogState.partialLineBuffer.split('\n');
            consoleLogState.partialLineBuffer = lines.pop() || '';

            for (const line of lines) {
                const row = parseLogLine(line);
                if (row) {
                    _logBatchBuffer.push(Object.freeze(row));
                }
            }

            // 每 200ms 批量 flush 一次，避免逐条触发 Vue 重渲染
            if (!_logBatchTimer) {
                _logBatchTimer = setTimeout(() => {
                    _logBatchTimer = null;
                    _flushLogBatch();
                }, 200);
            }
        };

        const rebuildConsoleLogFromContent = () => {
            consoleLogState.partialLineBuffer = '';
            let items = parseLogContent(consoleLogState.content || '').map(Object.freeze);
            if (items.length > consoleLogState.maxLines) items = items.slice(-consoleLogState.maxLines);
            parsedLogs.value = items;
            scrollConsoleLogToBottom();
        };

        const loadSystemLogsFallback = async () => {
            try {
                const res = await axios.get('/api/system_logs', {
                    params: {
                        level: consoleLogState.levelFilter,
                        keyword: (consoleLogState.keywordFilter || '').trim(),
                        category: consoleLogState.categoryFilter || 'ALL'
                    }
                });
                consoleLogState.content = res.data.logs || '';
                if (res.data.latest_id) {
                    consoleLogState.lastEventId = Number(res.data.latest_id);
                }
                rebuildConsoleLogFromContent();
            } catch (e) {
                consoleLogState.content = '读取日志失败: ' + e.message;
                parsedLogs.value = [];
                consoleLogState.partialLineBuffer = '';
            }
        };

        const reconnectConsoleLogStream = () => {
            if (!consoleLogState.visible) return;
            consoleLogState.autoRefresh = true;
            consoleLogState.loading = true;
            stopConsoleLogStream();
            startConsoleLogStream();
            setTimeout(() => {
                consoleLogState.loading = false;
            }, 300);
        };

        let consoleLogEventSource = null;

        const stopConsoleLogStream = () => {
            if (consoleLogEventSource) {
                consoleLogEventSource.close();
                consoleLogEventSource = null;
            }
            consoleLogState.streaming = false;
        };

        const startConsoleLogStream = () => {
            stopConsoleLogStream();
            try {
                const params = new URLSearchParams();
                params.set('level', consoleLogState.levelFilter || 'ALL');
                const keyword = (consoleLogState.keywordFilter || '').trim();
                if (keyword) {
                    params.set('keyword', keyword);
                }
                if (consoleLogState.categoryFilter && consoleLogState.categoryFilter !== 'ALL') {
                    params.set('category', consoleLogState.categoryFilter);
                }
                if (consoleLogState.lastEventId) {
                    params.set('last_event_id', String(consoleLogState.lastEventId));
                }

                consoleLogEventSource = new EventSource('/api/system_logs/stream?' + params.toString());
                consoleLogState.streaming = true;

                consoleLogEventSource.addEventListener('init', (e) => {
                    try {
                        const data = JSON.parse(e.data || '{}');
                        if (!consoleLogState.lastEventId) {
                            consoleLogState.content = data.chunk || '';
                            rebuildConsoleLogFromContent();
                        }
                    } catch (_) {}
                });

                consoleLogEventSource.addEventListener('reset', async () => {
                    await loadSystemLogsFallback();
                    stopConsoleLogStream();
                    if (consoleLogState.visible && consoleLogState.autoRefresh) {
                        setTimeout(startConsoleLogStream, 100);
                    }
                });

                consoleLogEventSource.onmessage = (e) => {
                    try {
                        if (e.lastEventId) {
                            const eventId = Number(e.lastEventId);
                            if (!Number.isNaN(eventId)) {
                                consoleLogState.lastEventId = eventId;
                            }
                        }
                        const data = JSON.parse(e.data || '{}');
                        appendSystemLogChunk(data.chunk || '');
                    } catch (_) {}
                };

                consoleLogEventSource.onerror = () => {
                    stopConsoleLogStream();
                    if (consoleLogState.visible && consoleLogState.autoRefresh) {
                        setTimeout(startConsoleLogStream, 1000);
                    }
                };
            } catch (_) {
                stopConsoleLogStream();
            }
        };

        const toggleConsoleAutoScroll = () => {
            consoleLogState.autoRefresh = !consoleLogState.autoRefresh;
            if (consoleLogState.autoRefresh) {
                if (consoleLogState.visible) {
                    startConsoleLogStream();
                }
            } else {
                stopConsoleLogStream();
            }
        };

        const openConsoleLog = () => {
            consoleLogState.visible = true;
            nextTick(async () => {
                const app = document.querySelector('#app');
                if (app) app.style.overflow = 'auto';
                document.body.style.overflow = 'hidden';
                document.body.style.position = 'fixed';
                document.body.style.width = '100%';

                await loadSystemLogsFallback();
                scrollConsoleLogToBottom();

                if (consoleLogState.autoRefresh) {
                    startConsoleLogStream();
                }
            });
        };

        const closeConsoleLog = () => {
            consoleLogState.visible = false;
            const app = document.querySelector('#app');
            if (app) app.style.overflow = 'hidden';
            document.body.style.overflow = '';
            document.body.style.position = '';
            document.body.style.width = '';
            stopConsoleLogStream();
        };

        const changeConsoleLogLevel = (level) => {
            const target = String(level || 'ALL').toUpperCase();
            if (consoleLogState.levelFilter === target) return;
            consoleLogState.levelFilter = target;
            consoleLogState.lastEventId = null;
            consoleLogState.content = '';
            parsedLogs.value = [];
            consoleLogState.partialLineBuffer = '';
            if (consoleLogState.visible) {
                reconnectConsoleLogStream();
            }
        };

        const changeConsoleLogCategory = (category) => {
            const target = String(category || 'ALL').toUpperCase();
            if (consoleLogState.categoryFilter === target) return;
            consoleLogState.categoryFilter = target;
            consoleLogState.lastEventId = null;
            consoleLogState.content = '';
            parsedLogs.value = [];
            consoleLogState.partialLineBuffer = '';
            if (consoleLogState.visible) {
                reconnectConsoleLogStream();
            }
        };

        let _keywordDebounceTimer = null;
        watch(() => consoleLogState.keywordInput, (val) => {
            const nextKeyword = (val || '').trim();
            if (nextKeyword === consoleLogState.keywordFilter) return;
            consoleLogState.keywordFilter = nextKeyword;
            clearTimeout(_keywordDebounceTimer);
            _keywordDebounceTimer = setTimeout(() => {
                if (!consoleLogState.visible) return;
                consoleLogState.lastEventId = null;
                consoleLogState.content = '';
                parsedLogs.value = [];
                consoleLogState.partialLineBuffer = '';
                reconnectConsoleLogStream();
            }, 400);
        });

        const clearSystemLogs = async () => {
            try {
                const res = await axios.post('/api/clear_system_logs');
                if (res.data.status === 'ok') {
                    consoleLogState.content = '';
                    parsedLogs.value = [];
                    consoleLogState.partialLineBuffer = '';
                    consoleLogState.lastEventId = null;
                    showToast('日志已清空', 'success');
                }
            } catch(e) {
                console.error('清空日志失败:', e);
                showToast('清空日志失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const manualServerIdx = ref(0);
        const currentLibId = ref('');
        const previewImage = ref('');
        const loading = ref(false);
        const applying = ref(false);
        const selectedPresetIdx = ref(''); 
        const currentPresetFile = ref(''); 
        const manualMode = ref('random');
        
        const accordions = reactive({ 
            layout: false, badge: false, target: false, params: false,
            c_mode: false, c_target_obj: false, c_materials: false, c_params: false, c_upload: false
        });
        
        const config = reactive({ 
            engine: 'classic', badge_style: 'none', badge_font: '', badge_size: 40, badge_bg_color: '#0f172a',
            badge_text_color: '#ffffff', badge_opacity: 255, badge_border_opacity: 40, title: '', subtitle: '', font_title: '', font_subtitle: '' 
        });
        
        // [修复点 1] 定义全局配置对象
        const globalConfig = reactive({
            proxy_url: '',
            tmdb_key: '',
            douban_cookie: '',
            log_level: 'INFO',
            debug_mode: false,
            app_public_base_url: ''
        });

        // ==========================================
        // [新增] 302 配置对象
        // ==========================================
        // ==========================================
        // [修改] 302 配置对象 (改为数组结构支持多配置)
        // ==========================================
        const config302 = reactive({
            drives: [],
            embys: [],
            standard_topology: null
        });

        // 定义默认模板
        const defaultDrive115 = {
            name: '',
            cookie: '',
            enable_sync: true,
            enable_rapid: false,
            enable_standard_topology: true,
            remote_root_name: '影视库',

            rapid_mode: 'auto',
            rapid_accounts: [],

            auto_delete: true,
            delete_cron: '0 */2 * * *',
            recycle_code: '',
            upload_dir: '',
            status: 'unknown',
            login_app: '',
            login_app_label: '',
            testing: false,
            qr_loading: false
        };

        const qr115AppOptions = [
            { value: '115android', label: '115网盘(Android端)' },
            { value: 'web', label: '网页版' },
            { value: 'android', label: '115生活(Android端)' },
            { value: 'ios', label: '115生活(iOS端)' },
            { value: 'ipad', label: '115生活(iPad端)' },
            { value: '115ios', label: '115网盘(iOS端)' },
            { value: '115ipad', label: '115网盘(iPad端)' },
            { value: 'tv', label: '115生活(Android电视端)' },
            { value: 'apple_tv', label: '115生活(Apple TV端)' },
            { value: 'wechatmini', label: '115生活(微信小程序)' },
            { value: 'alipaymini', label: '115生活(支付宝小程序)' },
            { value: 'windows', label: '115生活(Windows端)' },
            { value: 'mac', label: '115生活(macOS端)' },
            { value: 'linux', label: '115生活(Linux端)' },
            { value: 'qandroid', label: '115管理(Android端)' },
            { value: 'qios', label: '115管理(iOS端)' },
            { value: 'qipad', label: '115管理(iPad端)' },
            { value: 'harmony', label: '115网盘(鸿蒙端)' }
        ];

        const qrcode115State = reactive({
            visible: false,
            driveIndex: -1,
            driveRef: null,
            app: '115android',
            appOptions: qr115AppOptions,
            loading: false,
            polling: false,
            token: null,
            qrcode: '',
            qrcodeUrl: '',
            status: 'idle',
            statusText: '',
            error: '',
            autoTest: true,
            pollTimer: null,
            resultFetching: false
        });

        const defaultEmby302 = {
            name: '',
            url: '',
            key: '',
            public_host: '',
            proxy_port: '',
            drive_index: -1,
            modes: {
                pickcode: true
            },
            preload: true,
            enabled: true,
            status: 'unknown',
            testing: false
        };

        const ensureSingle302Drive = () => {
            if (!Array.isArray(config302.drives) || config302.drives.length === 0) {
                config302.drives = [JSON.parse(JSON.stringify(defaultDrive115))];
            } else {
                config302.drives = [ensure302DriveUiFields(config302.drives[0])];
            }
            return config302.drives[0];
        };

        const ensureSingle302Emby = () => {
            if (!Array.isArray(config302.embys) || config302.embys.length === 0) {
                config302.embys = [JSON.parse(JSON.stringify(defaultEmby302))];
            } else {
                const emby = JSON.parse(JSON.stringify(config302.embys[0] || defaultEmby302));
                normalizeEmbyModes(emby);
                emby.drive_index = 0;
                emby.enabled = true;
                emby.preload = true;
                config302.embys = [emby];
            }
            return config302.embys[0];
        };

        const add302Drive = () => ensureSingle302Drive();
        const remove302Drive = async () => showToast('已固定为单个主 115 配置', 'warning');
        const add302Emby = () => ensureSingle302Emby();
        const remove302Emby = async () => showToast('已固定为单个 Emby 配置', 'warning');

        const test115Cookie = async (drive) => {
            if (!drive.cookie) return showToast('请先填写 Cookie', 'error');

            drive.testing = true;
            drive.status = 'unknown';

            try {
                const res = await axios.post('/api/config_302/test_115', { cookie: drive.cookie });

                if (res.data.status === 'ok') {
                    drive.status = 'ok';
                    drive.login_app = res.data.login_app || '';
                    drive.login_app_label = res.data.login_app_label || '';
                    showToast(res.data.message, 'success');
                } else {
                    drive.status = 'error';
                    drive.login_app = '';
                    drive.login_app_label = '';
                    showToast(res.data.message, 'error');
                }
            } catch (e) {
                drive.status = 'error';
                drive.login_app = '';
                drive.login_app_label = '';
                showToast('请求失败: ' + (e.response?.data?.message || e.message), 'error');
            } finally {
                drive.testing = false;
            }
        };

        const clear115QrPollTimer = () => {
            if (qrcode115State.pollTimer) {
                clearTimeout(qrcode115State.pollTimer);
                qrcode115State.pollTimer = null;
            }
        };

        const reset115QrState = () => {
            clear115QrPollTimer();
            qrcode115State.loading = false;
            qrcode115State.polling = false;
            qrcode115State.token = null;
            qrcode115State.qrcode = '';
            qrcode115State.qrcodeUrl = '';
            qrcode115State.status = 'idle';
            qrcode115State.statusText = '';
            qrcode115State.error = '';
            qrcode115State.autoTest = true;
            qrcode115State.resultFetching = false;
        };

        const mark115QrDriveLoading = (loading) => {
            const drive = qrcode115State.driveRef;
            if (drive) {
                drive.qr_loading = !!loading;
            }
        };

        const close115QrLogin = () => {
            mark115QrDriveLoading(false);
            qrcode115State.visible = false;
            qrcode115State.driveRef = null;
            qrcode115State.driveIndex = -1;
            reset115QrState();
        };

        const fetch115QrResult = async () => {
            if (!qrcode115State.visible || !qrcode115State.driveRef || !qrcode115State.token?.uid || qrcode115State.resultFetching) return;
            qrcode115State.resultFetching = true;
            qrcode115State.loading = true;
            qrcode115State.polling = false;
            qrcode115State.status = 'confirmed';
            qrcode115State.statusText = '扫码确认成功，正在获取 Cookie';
            qrcode115State.error = '';
            clear115QrPollTimer();

            try {
                const res = await axios.post('/api/config_302/115_qrcode/result', {
                    uid: qrcode115State.token.uid,
                    app: qrcode115State.app
                });
                if (res.data.status !== 'ok' || !res.data.cookie) {
                    qrcode115State.status = 'error';
                    qrcode115State.statusText = '获取 Cookie 失败';
                    qrcode115State.error = res.data.message || '未能提取 Cookie';
                    showToast(qrcode115State.error, 'error');
                    return;
                }

                qrcode115State.driveRef.cookie = res.data.cookie;
                qrcode115State.driveRef.status = 'unknown';
                qrcode115State.status = 'success';
                qrcode115State.statusText = '扫码登录成功，Cookie 已写入';

                const payload = build302Payload();
                await axios.post('/api/config_302/save', payload);
                showToast('扫码登录成功，Cookie 已写入后台配置', 'success');

                if (qrcode115State.autoTest) {
                    await test115Cookie(qrcode115State.driveRef);
                }

                close115QrLogin();
            } catch (e) {
                qrcode115State.status = 'error';
                qrcode115State.statusText = '获取 Cookie 失败';
                qrcode115State.error = e.response?.data?.message || e.response?.data?.detail || e.message;
                showToast('获取扫码结果失败: ' + qrcode115State.error, 'error');
            } finally {
                qrcode115State.loading = false;
                qrcode115State.resultFetching = false;
                if (qrcode115State.visible && qrcode115State.status !== 'success') {
                    mark115QrDriveLoading(false);
                }
            }
        };

        const poll115QrStatus = async () => {
            if (!qrcode115State.visible || !qrcode115State.token) return;
            qrcode115State.polling = true;
            try {
                const res = await axios.post('/api/config_302/115_qrcode/status', qrcode115State.token);
                if (res.data.status !== 'ok') {
                    qrcode115State.status = 'error';
                    qrcode115State.statusText = '查询扫码状态失败';
                    qrcode115State.error = res.data.message || '状态查询失败';
                    qrcode115State.polling = false;
                    mark115QrDriveLoading(false);
                    return;
                }

                const scanStatus = res.data.scan_status || 'error';
                qrcode115State.status = scanStatus;
                qrcode115State.statusText = res.data.message || '';
                qrcode115State.error = '';

                if (scanStatus === 'confirmed') {
                    await fetch115QrResult();
                    return;
                }

                if (scanStatus === 'expired' || scanStatus === 'cancelled' || scanStatus === 'error') {
                    qrcode115State.polling = false;
                    mark115QrDriveLoading(false);
                    return;
                }

                qrcode115State.pollTimer = setTimeout(poll115QrStatus, 2500);
            } catch (e) {
                qrcode115State.status = 'error';
                qrcode115State.statusText = '查询扫码状态失败';
                qrcode115State.error = e.response?.data?.message || e.response?.data?.detail || e.message;
                qrcode115State.polling = false;
                mark115QrDriveLoading(false);
            }
        };

        const create115QrCode = async () => {
            if (!qrcode115State.visible || !qrcode115State.driveRef) return;
            qrcode115State.loading = true;
            qrcode115State.error = '';
            qrcode115State.status = 'loading';
            qrcode115State.statusText = '正在生成二维码...';
            qrcode115State.qrcode = '';
            qrcode115State.qrcodeUrl = '';
            qrcode115State.token = null;
            clear115QrPollTimer();
            mark115QrDriveLoading(true);

            try {
                const res = await axios.post('/api/config_302/115_qrcode/start', { app: qrcode115State.app });
                if (res.data.status !== 'ok') {
                    qrcode115State.status = 'error';
                    qrcode115State.statusText = '生成二维码失败';
                    qrcode115State.error = res.data.message || '生成二维码失败';
                    mark115QrDriveLoading(false);
                    return;
                }
                qrcode115State.token = res.data.token;
                qrcode115State.qrcode = res.data.qrcode || '';
                qrcode115State.qrcodeUrl = res.data.qrcode_url || '';
                qrcode115State.status = 'waiting';
                qrcode115State.statusText = '请使用 115 App 扫描二维码';
                qrcode115State.polling = true;
                qrcode115State.pollTimer = setTimeout(poll115QrStatus, 1500);
            } catch (e) {
                qrcode115State.status = 'error';
                qrcode115State.statusText = '生成二维码失败';
                qrcode115State.error = e.response?.data?.message || e.response?.data?.detail || e.message;
                mark115QrDriveLoading(false);
            } finally {
                qrcode115State.loading = false;
            }
        };

        const open115QrLogin = (drive, idx) => {
            reset115QrState();
            qrcode115State.visible = true;
            qrcode115State.driveRef = drive;
            qrcode115State.driveIndex = idx;
            qrcode115State.app = '115android';
            drive.qr_loading = false;
        };

        // 手动清理 115 目录和回收站
        const manualCleanup115 = async (drive, driveIndex, accountType, accountIndex) => {
            const accountTypeName = accountType === 'main' ? '主号' : '小号';
            const ok = await showConfirm(
                `手动清理 ${accountTypeName}`,
                `确定要清理 ${accountTypeName} 的秒传目录和回收站吗？此操作不可撤销！`,
                'warning'
            );
            if (!ok) return;

            // 设置 cleaning 状态
            const target = accountType === 'main' ? drive : drive.rapid_accounts[accountIndex];
            target.cleaning = true;

            try {
                const res = await axios.post('/api/config_302/manual_cleanup', {
                    drive_index: 0,
                    account_type: accountType,
                    account_index: accountIndex
                });

                if (res.data.status === 'ok') {
                    showToast(res.data.message, 'success');
                } else {
                    showToast(res.data.message, 'error');
                }
            } catch (e) {
                showToast('清理失败: ' + (e.response?.data?.message || e.message), 'error');
            } finally {
                target.cleaning = false;
            }
        };

        const normalizeEmbyModes = (emby) => {
            if (!emby.modes || typeof emby.modes !== 'object') {
                emby.modes = { pickcode: true };
                return;
            }
            emby.modes = {
                pickcode: emby.modes.pickcode !== undefined ? !!emby.modes.pickcode : true
            };
        };

        const ensure302DriveUiFields = (drive) => ({
            ...drive,
            enable_standard_topology: true,
            remote_root_name: drive?.remote_root_name || '影视库',
            testing: false,
            qr_loading: false,
            status: drive?.status || 'unknown',
            login_app: drive?.login_app || '',
            login_app_label: drive?.login_app_label || ''
        });

        const build302Payload = () => {
            const drive = JSON.parse(JSON.stringify(ensureSingle302Drive()));
            delete drive.qr_loading;
            drive.transfer_drive_index = 0;
            drive.enable_standard_topology = true;
            const sourceEmby = ensureSingle302Emby();
            const modes = sourceEmby.modes || {};
            const emby = {
                name: sourceEmby.name || '',
                url: sourceEmby.url || '',
                key: sourceEmby.key || '',
                public_host: sourceEmby.public_host || '',
                proxy_port: sourceEmby.proxy_port || '',
                modes: {
                    pickcode: modes.pickcode !== undefined ? !!modes.pickcode : true
                },
                preload: true,
                rapid_play: !!sourceEmby.rapid_play,
                enabled: true,
                drive_index: 0,
            };
            return { drives: [drive], embys: [emby] };
        };

        // 获取 302 配置 (兼容旧数据结构)
        const fetch302Config = async () => {
            try {
                const res = await axios.get('/api/config_302/get');
                if (res.data) {
                    const rawDrives = Array.isArray(res.data.drives)
                        ? res.data.drives
                        : (res.data.drive ? [res.data.drive] : []);
                    const rawEmbys = Array.isArray(res.data.embys)
                        ? res.data.embys
                        : (res.data.emby ? [res.data.emby] : []);
                    config302.drives = rawDrives.slice(0, 1).map(ensure302DriveUiFields);
                    config302.embys = rawEmbys.slice(0, 1);
                    config302.standard_topology = res.data.standard_topology || null;
                    ensureSingle302Drive();
                    ensureSingle302Emby();
                    syncServersFrom302();
                }
            } catch (e) {
                ensureSingle302Drive();
                ensureSingle302Emby();
                syncServersFrom302();
            }
        };

        // 保存 302 配置
        const save302Config = async () => {
            // 保存前先获取服务端当前配置，用于检测端口变更
            let oldPort = '';
            try {
                const res = await axios.get('/api/config_302/get');
                const oldEmbys = Array.isArray(res.data?.embys)
                    ? res.data.embys
                    : (res.data?.emby ? [res.data.emby] : []);
                oldPort = String(oldEmbys[0]?.proxy_port || '').trim();
            } catch (e) { /* 忽略 */ }

            try {
                const payload = build302Payload();
                const saveRes = await axios.post('/api/config_302/save', payload);
                if (saveRes.data?.standard_topology) {
                    config302.standard_topology = saveRes.data.standard_topology;
                }
                showToast(saveRes.data?.message || '302 配置已保存', 'success');
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
                return;
            }

            // 检测端口是否变更
            const newPort = String((config302.embys[0]?.proxy_port || '')).trim();
            const portChanged = oldPort !== '' && newPort !== oldPort;
            if (portChanged) {
                const confirmed = await showConfirm('需要重启', '网关端口号已变更，重启后生效。是否现在重启？', 'warning');
                if (confirmed) {
                    try {
                        await axios.post('/api/server/restart');
                        showToast('正在重启...', 'info');
                    } catch (e) {
                        showToast('重启请求失败: ' + e.message, 'error');
                    }
                }
            }
        };

        const saveEmbyConfig = async () => {
            const sourceEmby = ensureSingle302Emby();
            const modes = sourceEmby.modes || {};
            const emby = {
                name: sourceEmby.name || '',
                url: sourceEmby.url || '',
                key: sourceEmby.key || '',
                public_host: sourceEmby.public_host || '',
                proxy_port: sourceEmby.proxy_port || '',
                modes: { pickcode: modes.pickcode !== undefined ? !!modes.pickcode : true },
                preload: true,
                rapid_play: !!sourceEmby.rapid_play,
                enabled: true,
                drive_index: 0,
            };
            try {
                const saveRes = await axios.post('/api/config_302/save_emby', { embys: [emby] });
                showToast(saveRes.data?.message || 'Emby 配置已保存', 'success');
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const toggle302Switch = async (event, obj, field) => {
            const newState = event.target.checked;
            const oldState = obj[field];
            obj[field] = newState;
            try {
                ensureSingle302Emby();
                const payload = build302Payload();
                await axios.post('/api/config_302/save', payload);
                showToast('配置已保存', 'success');
            } catch (e) {
                obj[field] = oldState;
                event.target.checked = oldState;
                showToast('状态切换失败', 'error');
            }
        };

        // ==========================================
        // STRM 配置
        // ==========================================
        const defaultStrmTask = {
            name: '',
            drive_index: 0,
            remote_path: '',
            local_path: '',
            sync_video: true,
            download_auxiliary: true,
            download_tmdb_metadata: false,
            min_video_size_mb: 0,
            overwrite: 'skip',
            aux_download_mode: 'cdn',
            video_exts_str: '.mp4,.mpg,.mkv,.mpeg,.ts,.vob,.iso,.m4v,.avi,.3gp,.wmv,.webm,.flv,.mov,.m2ts,.rmvb,.rm,.asf,.f4v,.m2t,.mts,.mpe,.tp,.trp,.divx,.ogv,.dv',
            audio_exts_str: '.mp3,.flac,.wav,.m4a,.ape,.dsd,.dff,.dsf,.ac3,.dts',
            image_exts_str: '.jpg,.jpeg,.png,.webp,.bmp,.tiff,.tif,.ico,.gif,.svg,.heic,.avif,.raw',
            data_exts_str: '.nfo,.lrc,.srt,.pdf,.ass,.ssa,.md,.sub,.sup,.idx,.txt,.xml,.json,.smi,.vtt,.ttml,.dfxp,.scc,.bup,.ifo'
        };

        const strmConfig = reactive({
            sync_tasks: []
        });

        const strmProgress = reactive({
            running: false,
            run_id: '',
            percent: 0,
            status_text: '',
            scanned: 0,
            scanned_dirs: 0,
            scanned_files: 0,
            generated: 0,
            generated_dirs: 0,
            downloaded: 0,
            downloaded_dirs: 0,
            download_failed: 0,
            skipped: 0,
            skip_reasons: {},
            failed: 0,
            last_result: ''
        });

        const strmBrowser = reactive({
            taskIdx: -1,
            dirs: [],
            path: '',
            history: []  // [{cid, path}] 栈，用于返回上级
        });

        let strmPollTimer = null;

        const fetchStrmConfig = async () => {
            try {
                const res = await axios.get('/api/strm/get');
                if (res.data) {
                    strmConfig.sync_tasks = Array.isArray(res.data.sync_tasks)
                        ? res.data.sync_tasks.map(task => ({
                            ...JSON.parse(JSON.stringify(defaultStrmTask)),
                            ...task,
                            drive_index: 0,
                            overwrite: task?.overwrite === 'overwrite' ? 'overwrite' : 'skip'
                        }))
                        : [];
                    if (strmConfig.sync_tasks.length === 0) addStrmTask();
                }
            } catch (e) {
                addStrmTask();
            }
        };

        const saveStrmConfig = async () => {
            try {
                const syncTasks = strmConfig.sync_tasks.map(task => ({
                    ...task,
                    drive_index: 0,
                }));
                await axios.post('/api/strm/save', {
                    sync_tasks: syncTasks
                });
                showToast('STRM 配置已保存', 'success');
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const addStrmTask = () => {
            strmConfig.sync_tasks.push(JSON.parse(JSON.stringify(defaultStrmTask)));
        };

        const removeStrmTask = async (idx) => {
            const ok = await showConfirm('删除任务', '确定删除此同步任务吗？', 'danger');
            if (ok) strmConfig.sync_tasks.splice(idx, 1);
        };

        // 后缀下拉选项
        const videoExtOptions = ['.mp4', '.mpg', '.mkv', '.mpeg', '.ts', '.vob', '.iso', '.m4v', '.avi', '.3gp', '.wmv', '.webm', '.flv', '.mov', '.m2ts', '.rmvb', '.rm', '.asf', '.f4v', '.m2t', '.mts', '.mpe', '.tp', '.trp', '.divx', '.ogv', '.dv'];
        const audioExtOptions = ['.mp3', '.flac', '.wav', '.m4a', '.ape', '.dsd', '.dff', '.dsf', '.ac3', '.dts'];
        const imageExtOptions = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.ico', '.gif', '.svg', '.heic', '.avif', '.raw'];
        const dataExtOptions  = ['.nfo', '.lrc', '.srt', '.pdf', '.ass', '.ssa', '.md', '.sub', '.sup', '.idx', '.txt', '.xml', '.json', '.smi', '.vtt', '.ttml', '.dfxp', '.scc', '.bup', '.ifo'];

        const hasExt = (extsStr, ext) => {
            if (!extsStr) return false;
            return extsStr.split(',').map(s => s.trim().toLowerCase()).includes(ext.toLowerCase());
        };

        const toggleExt = (task, field, ext) => {
            const current = task[field] || '';
            const parts = current.split(',').map(s => s.trim()).filter(Boolean);
            const idx = parts.findIndex(e => e.toLowerCase() === ext.toLowerCase());
            if (idx >= 0) {
                parts.splice(idx, 1);
            } else {
                parts.push(ext);
            }
            task[field] = parts.join(',');
        };

        // ==========================================
        // 媒体整理模块 (115 网盘)
        // ==========================================

        const mediaOrganizeConfig = reactive({
            drive_index: 0,
            source_cid: '0',
            source_name: '根目录',
            target_cid: '0',
            target_name: '根目录',
            failed_cid: '0',
            failed_name: '',
            movie_enabled: true,
            tv_enabled: true,
            scrape_enabled: true,
            emby_local_scrape: false,
            scrape_nfo: true,
            scrape_poster: true,
            scrape_fanart: true,
            scrape_logo: true,
            scrape_banner: false,
            scrape_thumb: true,
            scrape_season_poster: true,
            scrape_episode_thumb: true,
            policy_nfo: 'missing_only',
            policy_poster: 'missing_only',
            policy_fanart: 'missing_only',
            policy_logo: 'missing_only',
            policy_banner: 'missing_only',
            policy_thumb: 'missing_only',
            policy_season_poster: 'missing_only',
            policy_episode_thumb: 'missing_only',
            auto_detect_bluray: true,
            life_monitor_enabled: false,
            auto_sync_strm: false,
            wash_enabled: false,
            wash_by_equivalent_size: false,
            wash_tolerance_ratio: 0,
            wash_reserved_1: false,
            wash_reserved_2: false,
            organize_parse_mode: 'ffprobe',
            safe_mode_threshold: 1000,
            write_pacing_min_seconds: 1,
            write_pacing_max_seconds: 2,
            direct_link_pacing_min_seconds: 1,
            direct_link_pacing_max_seconds: 2,
            ffprobe_concurrency: 2,
            waf_cooldown_seconds: 1800,
            movie_folder_format: '{title} ({year}) {tmdb-{tmdb_id}}',
            movie_rename_format: '{en_title}.{year}.{resource_pix}.{web_source}.{resource_type}.{resource_effect}.{video_encode}.{color_depth}.{video_effect}.{fps}.{audio_encode}-{resource_team}',
            tv_folder_format: '{title} ({year}) {tmdb-{tmdb_id}}',
            tv_episode_format: '{en_title}.{season_episode}.{year}.{resource_pix}.{web_source}.{resource_type}.{video_encode}.{color_depth}.{video_effect}.{fps}.{audio_encode}-{resource_team}',
        });

        const ORGANIZE_RUN_ID_STORAGE_KEY = 'media_organize_run_id';
        const organizeLoading = ref(false);
        const organizeResult = ref(null);
        const organizeRunId = ref(localStorage.getItem(ORGANIZE_RUN_ID_STORAGE_KEY) || null);
        const organizeProgress = reactive({ percent: 0, status_text: '', detail: null });

        const syncOrganizeTaskFromTaskMap = (tasks, { adoptRunning = false } = {}) => {
            const taskMap = tasks || {};
            let currentRunId = organizeRunId.value;
            let currentTask = currentRunId ? taskMap[currentRunId] : null;

            if ((!currentTask || currentTask.status !== 'running') && adoptRunning) {
                const runningEntry = Object.entries(taskMap).find(([id, task]) => {
                    if (!task || task.status !== 'running') return false;
                    return task.task_type === 'media_organize' || id.startsWith('organize_');
                });
                if (runningEntry) {
                    currentRunId = runningEntry[0];
                    currentTask = runningEntry[1];
                    if (organizeRunId.value !== currentRunId) {
                        organizeRunId.value = currentRunId;
                        localStorage.setItem(ORGANIZE_RUN_ID_STORAGE_KEY, currentRunId);
                    }
                }
            }

            if (!currentTask) {
                if (adoptRunning && organizeRunId.value && !taskMap[organizeRunId.value]) {
                    organizeRunId.value = null;
                    localStorage.removeItem(ORGANIZE_RUN_ID_STORAGE_KEY);
                }
                return null;
            }

            organizeLoading.value = currentTask.status === 'running';
            organizeProgress.percent = Math.round(currentTask.percent || 0);
            organizeProgress.status_text = currentTask.cancel_requested ? '正在取消...' : (currentTask.name || '');
            organizeProgress.detail = currentTask.detail || null;
            return currentTask;
        };

        const restoreRunningOrganizeTask = async () => {
            try {
                const res = await axios.get('/api/progress');
                const task = syncOrganizeTaskFromTaskMap(res.data || {}, { adoptRunning: true });
                if (task && task.status === 'running') {
                    startOrganizePolling();
                }
            } catch (_) { }
        };
        let organizePollTimer = null;

        // 二级分类规则
        const categoryRulesEditor = reactive({ activeType: 'movie', movie: [], tv: [] });
        const categoryRulesSaving = ref(false);
        const subClassify = reactive({
            movie: { enabled: false, levels: [] },
            tv:    { enabled: false, levels: [] },
            sync_emby_library: false,
            emby_server_idx: 0,
            emby_library_level: 'level1',
        });
        const subClassifyVars = [
            { key: 'year_decade', label: '年代' },
            { key: 'rating_tier', label: '评分段' },
            { key: 'origin_country', label: '国家' },
            { key: 'genre_label', label: '类型' },
        ];
        const subClassifyVarExamples = {
            year_decade: '2010s',
            rating_tier: '8-9分',
            origin_country: '日本',
            genre_label: '动画',
        };
        const subClassifyBaseExamples = {
            movie: '电影/日本电影',
            tv: '剧集/日本动漫',
        };
        const subClassifyPreviewSegments = (mtype) => {
            return (subClassify[mtype]?.levels || []).map(key => {
                const meta = subClassifyVars.find(x => x.key === key);
                return {
                    key,
                    label: meta?.label || key,
                    example: subClassifyVarExamples[key] || (meta?.label || key),
                };
            });
        };
        const ruleListEl = ref(null);
        let _ruleDragState = null;

        const fetchCategoryRules = async () => {
            try {
                const res = await axios.get('/api/media_organize/category_rules/get');
                categoryRulesEditor.movie = JSON.parse(JSON.stringify(res.data.movie || []));
                categoryRulesEditor.tv = JSON.parse(JSON.stringify(res.data.tv || []));
                const sc = res.data.sub_classify || {};
                for (const t of ['movie', 'tv']) {
                    subClassify[t].enabled = sc[t]?.enabled || false;
                    subClassify[t].levels = JSON.parse(JSON.stringify(sc[t]?.levels || []));
                }
                subClassify.sync_emby_library = sc.sync_emby_library || false;
                subClassify.emby_server_idx = 0;
                subClassify.emby_library_level = sc.emby_library_level || 'level1';
            } catch (e) {
                console.error('获取分类规则失败', e);
            }
        };

        const saveCategoryRules = async () => {
            categoryRulesSaving.value = true;
            try {
                const res = await axios.post('/api/media_organize/category_rules/save', {
                    movie: categoryRulesEditor.movie,
                    tv: categoryRulesEditor.tv,
                });
                const data = res.data || {};
                showToast('分类规则已保存');

                const removedPaths = Array.isArray(data.removed_paths)
                    ? data.removed_paths
                    : (Array.isArray(data.diff?.removed_paths) ? data.diff.removed_paths : []);
                const warnings = Array.isArray(data.warnings) ? data.warnings : [];
                if (removedPaths.length) {
                    const warningText = warnings[0] || '这些旧分类路径已删除，但对应 Emby 媒体库不会自动删除，请自行到 Emby 手动清理';
                    setTimeout(() => {
                        alert(`${warningText}\n\n已删除路径：\n- ${removedPaths.join('\n- ')}`);
                    }, 80);
                }
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                categoryRulesSaving.value = false;
            }
        };

        let _subClassifyTimer = null;
        const saveSubClassify = () => {
            clearTimeout(_subClassifyTimer);
            _subClassifyTimer = setTimeout(async () => {
                try {
                    await axios.post('/api/media_organize/category_rules/sub_classify/save', {
                        movie: { enabled: subClassify.movie.enabled, levels: subClassify.movie.levels },
                        tv:    { enabled: subClassify.tv.enabled,    levels: subClassify.tv.levels },
                        sync_emby_library: subClassify.sync_emby_library,
                        emby_server_idx: 0,
                        emby_library_level: subClassify.emby_library_level,
                    });
                } catch (e) {
                    console.error('子分类设置保存失败', e);
                }
            }, 500);
        };

        const addRule = (type) => {
            categoryRulesEditor[type].push({ path: '', conditions: [] });
        };
        const removeRule = (type, idx) => {
            categoryRulesEditor[type].splice(idx, 1);
        };
        const addCondition = (type, rIdx) => {
            categoryRulesEditor[type][rIdx].conditions.push({ field: '', logic: 'AND', value: '' });
        };
        const removeCondition = (type, rIdx, cIdx) => {
            categoryRulesEditor[type][rIdx].conditions.splice(cIdx, 1);
        };
        const resetCategoryRules = async () => {
            if (!confirm('确定要恢复出厂默认分类规则吗？当前分类规则将被覆盖，子分类设置不受影响。')) return;
            try {
                const res = await axios.get('/api/media_organize/category_rules/defaults');
                categoryRulesEditor.movie = JSON.parse(JSON.stringify(res.data.movie || []));
                categoryRulesEditor.tv = JSON.parse(JSON.stringify(res.data.tv || []));
                showToast('已加载出厂默认分类规则，点击保存生效');
            } catch (e) {
                showToast('加载默认规则失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };
        const subClassifyToggleLevel = (mtype, key) => {
            const levels = subClassify[mtype].levels;
            const idx = levels.indexOf(key);
            if (idx === -1) levels.push(key);
            else levels.splice(idx, 1);
            saveSubClassify();
        };
        const embyLibCount = (level) => {
            const paths = new Set();
            for (const r of (categoryRulesEditor.movie || [])) { if (r.path) paths.add(r.path); }
            for (const r of (categoryRulesEditor.tv || [])) { if (r.path) paths.add(r.path); }
            if (level === 'rule') return paths.size;
            const n = parseInt(level.replace('level', ''));
            const groups = new Set();
            for (const p of paths) {
                const parts = p.split('/');
                groups.add(parts.length >= n ? parts.slice(0, n).join('/') : p);
            }
            return groups.size;
        };
        const embyLibLevelOptions = () => {
            const paths = new Set();
            for (const r of (categoryRulesEditor.movie || [])) { if (r.path) paths.add(r.path); }
            for (const r of (categoryRulesEditor.tv || [])) { if (r.path) paths.add(r.path); }
            let maxDepth = 1;
            for (const p of paths) { const d = p.split('/').length; if (d > maxDepth) maxDepth = d; }
            const opts = [{ value: 'rule', label: `每条规则一个库（${paths.size}个）` }];
            for (let i = maxDepth; i >= 1; i--) {
                opts.push({ value: `level${i}`, label: `按${i}级目录合并（${embyLibCount(`level${i}`)}个）` });
            }
            return opts;
        };
        const embyCacheRefreshing = ref(false);
        async function refreshEmbyCache() {
            embyCacheRefreshing.value = true;
            try {
                const resp = await fetch('/api/media_organize/emby_lib_cache/refresh', { method: 'POST' });
                const data = await resp.json();
                if (data.status === 'success') {
                    showToast(`缓存已刷新（${data.count} 个媒体库）`, 'success');
                }
            } catch (e) {
                showToast('刷新失败: ' + e.message, 'error');
            } finally {
                embyCacheRefreshing.value = false;
            }
        }
        let _levelDragState = null;
        const onLevelDragStart = (e, mtype, idx) => { _levelDragState = { mtype, from: idx }; e.dataTransfer.effectAllowed = 'move'; };
        const onLevelDragOver = (e, mtype, idx) => { if (_levelDragState?.mtype === mtype) _levelDragState.to = idx; };
        const onLevelDrop = (e, mtype, idx) => {
            if (!_levelDragState || _levelDragState.mtype !== mtype || _levelDragState.from === idx) return;
            const levels = subClassify[mtype].levels;
            const [moved] = levels.splice(_levelDragState.from, 1);
            levels.splice(idx, 0, moved);
            _levelDragState = null;
            saveSubClassify();
        };
        const onLevelDragEnd = () => { _levelDragState = null; };

        // 拖拽排序（原生 HTML5 drag，无需额外依赖）
        const onRuleDragStart = (e, type, idx) => {
            _ruleDragState = { type, from: idx };
            e.dataTransfer.effectAllowed = 'move';
        };
        const onRuleDragOver = (e, type, idx) => {
            e.preventDefault();
            if (_ruleDragState && _ruleDragState.type === type && _ruleDragState.from !== idx) _ruleDragState.to = idx;
        };
        const onRuleDrop = (e, type, idx) => {
            e.preventDefault();
            if (!_ruleDragState || _ruleDragState.type !== type || _ruleDragState.from === idx) return;
            const list = categoryRulesEditor[type];
            const [moved] = list.splice(_ruleDragState.from, 1);
            list.splice(idx, 0, moved);
            _ruleDragState = null;
        };
        const onRuleDragEnd = () => { _ruleDragState = null; };

        // Refs for rename template textareas (for cursor-aware token insertion)
        const movieFormatRef = ref(null);
        const movieFolderFormatRef = ref(null);
        const tvFolderFormatRef = ref(null);
        const tvEpisodeFormatRef = ref(null);

        // Preview example variables
        const MOVIE_PREVIEW_VARS = {
            title: '流浪地球', en_title: 'The.Wandering.Earth', original_title: '',
            year: '2019', tmdb_id: '521777',
            resource_pix: '2160p', resource_type: 'BluRay',
            video_encode: 'HEVC', audio_encode: 'DTS-HD.MA.7.1',
            web_source: 'UHD', resource_effect: 'REMUX',
            video_effect: 'DV.HDR',
            resource_team: 'CHD', fps: '60fps', part: '',
            color_depth: '10bit',
            first_letter: 'T', ext: 'mkv',
            season_episode: '', season_num: '', episode_num: '',
        };
        const TV_PREVIEW_VARS = {
            title: '怪奇物语', en_title: 'Stranger.Things', original_title: '',
            year: '2022', tmdb_id: '66732',
            season_episode: 'S04E01', season_num: '04', episode_num: '01',
            resource_pix: '2160p', resource_type: 'WEB-DL',
            video_encode: 'HEVC', audio_encode: 'Atmos.5.1',
            web_source: 'NF', resource_effect: '',
            video_effect: 'DV.HDR',
            resource_team: 'CHD', fps: '23.976fps', part: '',
            color_depth: '10bit',
            first_letter: 'S', ext: 'mkv',
        };

        /**
         * 渲染重命名模板（前端版，与后端 _render_template 逻辑一致）
         */
        function renderPreview(template, vars) {
            if (!template) return '';
            let result = template;
            for (const [key, value] of Object.entries(vars)) {
                result = result.replaceAll('{' + key + '}', value || '');
            }
            // 清理多余分隔符
            result = result.replace(/\.{2,}/g, '.');
            result = result.replace(/-{2,}/g, '-');
            result = result.replace(/_{2,}/g, '_');
            result = result.replace(/ {2,}/g, ' ');
            result = result.replace(/\(\s*\)/g, '');
            result = result.replace(/\[\s*\]/g, '');
            result = result.replace(/\.\./g, '.');
            result = result.replace(/^[.\-_ ]+|[.\-_ ]+$/g, '');
            return result;
        }

        const MOVIE_FOLDER_DISPLAY_FORMAT = '{中文标题} ({公映年份}) {TMDB编号}';
        const TV_FOLDER_DISPLAY_FORMAT = '{中文剧名} ({首播年份}) {TMDB编号}';
        const MOVIE_DISPLAY_FORMAT = '{英文片名}.{公映年份}.{分辨率}.{介质来源}.{处理方式}.{视频编码}.{色深}.{动态范围}.{帧率}.{音频规格}-{制作组}.mkv';
        const TV_DISPLAY_FORMAT = '{英文剧名}.{季数集数}.{首播年份}.{分辨率}.{来源平台}.{介质类型}.{视频编码}.{色深}.{动态范围}.{帧率}.{音频规格}-{制作组}.mkv';

        function movieFolderTemplateToDisplay(template) {
            let result = template || '';
            result = result.replaceAll('{title}', '{中文标题}');
            result = result.replaceAll('{year}', '{公映年份}');
            result = result.replaceAll('{tmdb-{TMDB编号}}', '{TMDB编号}');
            result = result.replaceAll('{tmdb-{tmdb_id}}', '{TMDB编号}');
            return result;
        }

        function movieFolderDisplayToTemplate(display) {
            let result = display || '';
            result = result.replaceAll('{tmdb-{TMDB编号}}', '{tmdb-{tmdb_id}}');
            result = result.replaceAll('{中文标题}', '{title}');
            result = result.replaceAll('{公映年份}', '{year}');
            result = result.replaceAll('{TMDB编号}', '{tmdb-{tmdb_id}}');
            return result;
        }

        function tvFolderTemplateToDisplay(template) {
            let result = template || '';
            result = result.replaceAll('{title}', '{中文剧名}');
            result = result.replaceAll('{year}', '{首播年份}');
            result = result.replaceAll('{tmdb-{TMDB编号}}', '{TMDB编号}');
            result = result.replaceAll('{tmdb-{tmdb_id}}', '{TMDB编号}');
            return result;
        }

        function tvFolderDisplayToTemplate(display) {
            let result = display || '';
            result = result.replaceAll('{tmdb-{TMDB编号}}', '{tmdb-{tmdb_id}}');
            result = result.replaceAll('{中文剧名}', '{title}');
            result = result.replaceAll('{首播年份}', '{year}');
            result = result.replaceAll('{TMDB编号}', '{tmdb-{tmdb_id}}');
            return result;
        }

        function movieTemplateToDisplay(template) {
            let result = template || '';
            result = result.replaceAll('{audio_encode}', '{音频规格}');
            result = result.replaceAll('{en_title}', '{英文片名}');
            result = result.replaceAll('{year}', '{公映年份}');
            result = result.replaceAll('{resource_pix}', '{分辨率}');
            result = result.replaceAll('{video_encode}', '{视频编码}');
            result = result.replaceAll('{color_depth}', '{色深}');
            result = result.replaceAll('{video_effect}', '{动态范围}');
            result = result.replaceAll('{fps}', '{帧率}');
            result = result.replaceAll('{resource_team}', '{制作组}');
            result = result.replaceAll('{web_source}.{resource_type}.{resource_effect}', '{介质来源}.{处理方式}');
            result = result.replaceAll('{web_source}.{resource_effect}', '{介质来源}.{处理方式}');
            return result.endsWith('.mkv') ? result : `${result}.mkv`;
        }

        function movieDisplayToTemplate(display) {
            let result = (display || '').replace(/\.mkv$/i, '');
            result = result.replaceAll('{音频规格}', '{audio_encode}');
            result = result.replaceAll('{英文片名}', '{en_title}');
            result = result.replaceAll('{公映年份}', '{year}');
            result = result.replaceAll('{分辨率}', '{resource_pix}');
            result = result.replaceAll('{视频编码}', '{video_encode}');
            result = result.replaceAll('{色深}', '{color_depth}');
            result = result.replaceAll('{动态范围}', '{video_effect}');
            result = result.replaceAll('{帧率}', '{fps}');
            result = result.replaceAll('{制作组}', '{resource_team}');
            result = result.replaceAll('{介质来源}.{处理方式}', '{web_source}.{resource_type}.{resource_effect}');
            return result;
        }

        function tvTemplateToDisplay(template) {
            let result = template || '';
            result = result.replaceAll('{audio_encode}', '{音频规格}');
            result = result.replaceAll('{en_title}', '{英文剧名}');
            result = result.replaceAll('{season_episode}', '{季数集数}');
            result = result.replaceAll('{year}', '{首播年份}');
            result = result.replaceAll('{resource_pix}', '{分辨率}');
            result = result.replaceAll('{web_source}', '{来源平台}');
            result = result.replaceAll('{resource_type}', '{介质类型}');
            result = result.replaceAll('{video_encode}', '{视频编码}');
            result = result.replaceAll('{color_depth}', '{色深}');
            result = result.replaceAll('{video_effect}', '{动态范围}');
            result = result.replaceAll('{fps}', '{帧率}');
            result = result.replaceAll('{resource_team}', '{制作组}');
            return result.endsWith('.mkv') ? result : `${result}.mkv`;
        }

        function tvDisplayToTemplate(display) {
            let result = (display || '').replace(/\.mkv$/i, '');
            result = result.replaceAll('{音频规格}', '{audio_encode}');
            result = result.replaceAll('{英文剧名}', '{en_title}');
            result = result.replaceAll('{季数集数}', '{season_episode}');
            result = result.replaceAll('{首播年份}', '{year}');
            result = result.replaceAll('{分辨率}', '{resource_pix}');
            result = result.replaceAll('{来源平台}', '{web_source}');
            result = result.replaceAll('{介质类型}', '{resource_type}');
            result = result.replaceAll('{视频编码}', '{video_encode}');
            result = result.replaceAll('{色深}', '{color_depth}');
            result = result.replaceAll('{动态范围}', '{video_effect}');
            result = result.replaceAll('{帧率}', '{fps}');
            result = result.replaceAll('{制作组}', '{resource_team}');
            return result;
        }

        const movieFolderFormatDisplay = computed({
            get: () => movieFolderTemplateToDisplay(mediaOrganizeConfig.movie_folder_format),
            set: (value) => {
                mediaOrganizeConfig.movie_folder_format = movieFolderDisplayToTemplate(value);
            }
        });
        const tvFolderFormatDisplay = computed({
            get: () => tvFolderTemplateToDisplay(mediaOrganizeConfig.tv_folder_format),
            set: (value) => {
                mediaOrganizeConfig.tv_folder_format = tvFolderDisplayToTemplate(value);
            }
        });
        const movieFormatDisplay = computed({
            get: () => movieTemplateToDisplay(mediaOrganizeConfig.movie_rename_format),
            set: (value) => {
                mediaOrganizeConfig.movie_rename_format = movieDisplayToTemplate(value);
            }
        });
        const tvEpisodeFormatDisplay = computed({
            get: () => tvTemplateToDisplay(mediaOrganizeConfig.tv_episode_format),
            set: (value) => {
                mediaOrganizeConfig.tv_episode_format = tvDisplayToTemplate(value);
            }
        });

        // 实时预览计算属性
        const moviePreviewName = computed(() =>
            renderPreview(mediaOrganizeConfig.movie_rename_format, MOVIE_PREVIEW_VARS) || '（请输入模板）'
        );
        const movieFolderPreviewName = computed(() =>
            renderPreview(mediaOrganizeConfig.movie_folder_format, MOVIE_PREVIEW_VARS) || '（请输入模板）'
        );
        const tvFolderPreviewName = computed(() =>
            renderPreview(mediaOrganizeConfig.tv_folder_format, TV_PREVIEW_VARS) || '（请输入模板）'
        );
        const tvEpisodePreviewName = computed(() =>
            renderPreview(mediaOrganizeConfig.tv_episode_format, TV_PREVIEW_VARS) || '（请输入模板）'
        );

        /**
         * 在光标位置插入 token
         * @param {string} type - 'movie' | 'tvFolder' | 'tvEpisode'
         * @param {string} token - 要插入的 token 字符串
         */
        function insertToken(type, token) {
            const refMap = {
                movie: movieFormatRef,
                movieFolder: movieFolderFormatRef,
                tvFolder: tvFolderFormatRef,
                tvEpisode: tvEpisodeFormatRef,
            };
            const fieldMap = {
                movie: 'movie_rename_format',
                movieFolder: 'movie_folder_format',
                tvFolder: 'tv_folder_format',
                tvEpisode: 'tv_episode_format',
            };
            const el = refMap[type]?.value;
            const field = fieldMap[type];
            if (!el || !field) return;

            const start = el.selectionStart ?? el.value.length;
            const end = el.selectionEnd ?? el.value.length;
            let current = mediaOrganizeConfig[field] || '';
            mediaOrganizeConfig[field] = current.slice(0, start) + token + current.slice(end);

            // 恢复光标到插入后的位置
            nextTick(() => {
                el.focus();
                const pos = start + token.length;
                el.setSelectionRange(pos, pos);
            });
        }

        async function resetMovieFormat() {
            const res = await axios.get('/api/media_organize/defaults');
            mediaOrganizeConfig.movie_folder_format = res.data.movie_folder_format;
            mediaOrganizeConfig.movie_rename_format = res.data.movie_rename_format;
        }

        async function resetTvFormat() {
            const res = await axios.get('/api/media_organize/defaults');
            mediaOrganizeConfig.tv_folder_format = res.data.tv_folder_format;
            mediaOrganizeConfig.tv_episode_format = res.data.tv_episode_format;
        }

        // 整理专用表单
        const organizeForm = reactive({
            media_type: '',
            overwrite: false,
            is_bluray: false,
        });

        // 源目录浏览器
        const orgSourceBrowser = reactive({
            dirs: [],
            path: '',
            history: [],
            currentCid: '0',
            opened: false
        });

        // 目标目录浏览器
        const orgTargetBrowser = reactive({
            dirs: [],
            path: '',
            history: [],
            currentCid: '0',
            opened: false
        });

        // 失败目录浏览器
        const orgFailedBrowser = reactive({
            dirs: [],
            path: '',
            history: [],
            currentCid: '0',
            opened: false
        });

        const normalizeOrganizeParseMode = () => {
            const mode = (mediaOrganizeConfig.organize_parse_mode || '').toLowerCase();
            if (mode === 'filename' || mode === 'ffprobe' || mode === 'ffprobe_full' || mode === '') {
                mediaOrganizeConfig.organize_parse_mode = mode;
                return;
            }
            mediaOrganizeConfig.organize_parse_mode = 'filename';
        };

        const normalizeMediaOrganizeTuning = () => {
            const toNumber = (value, fallback, min) => {
                const parsed = Number(value);
                if (!Number.isFinite(parsed)) return fallback;
                return Math.max(min, parsed);
            };
            const toInt = (value, fallback, min, max) => {
                const parsed = parseInt(value, 10);
                if (!Number.isFinite(parsed)) return fallback;
                return Math.min(max, Math.max(min, parsed));
            };
            mediaOrganizeConfig.safe_mode_threshold = toInt(mediaOrganizeConfig.safe_mode_threshold, 1000, 1, 999999);
            mediaOrganizeConfig.write_pacing_min_seconds = toNumber(mediaOrganizeConfig.write_pacing_min_seconds, 1, 0.1);
            mediaOrganizeConfig.write_pacing_max_seconds = Math.max(
                mediaOrganizeConfig.write_pacing_min_seconds,
                toNumber(mediaOrganizeConfig.write_pacing_max_seconds, 2, 0.1),
            );
            mediaOrganizeConfig.direct_link_pacing_min_seconds = toNumber(mediaOrganizeConfig.direct_link_pacing_min_seconds, 1, 0.1);
            mediaOrganizeConfig.direct_link_pacing_max_seconds = Math.max(
                mediaOrganizeConfig.direct_link_pacing_min_seconds,
                toNumber(mediaOrganizeConfig.direct_link_pacing_max_seconds, 2, 0.1),
            );
            mediaOrganizeConfig.ffprobe_concurrency = toInt(mediaOrganizeConfig.ffprobe_concurrency, 2, 1, 10);
            mediaOrganizeConfig.waf_cooldown_seconds = toInt(mediaOrganizeConfig.waf_cooldown_seconds, 1800, 60, 86400);
        };

        const fetchMediaOrganizeConfig = async () => {
            try {
                const res = await axios.get('/api/media_organize/get');
                if (res.data) {
                    Object.assign(mediaOrganizeConfig, res.data);
                    mediaOrganizeConfig.drive_index = 0;
                }
            } catch (e) { /* first load, use defaults */ }
            normalizeOrganizeParseMode();
            normalizeMediaOrganizeTuning();
            mediaOrganizeConfig.scrape_enabled = !!mediaOrganizeConfig.emby_local_scrape;
        };

        const saveMediaOrganizeConfig = async () => {
            try {
                normalizeOrganizeParseMode();
                normalizeMediaOrganizeTuning();
                await axios.post('/api/media_organize/save', { ...mediaOrganizeConfig, drive_index: 0 });
                showToast('媒体整理配置已保存', 'success');
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const toggleAutoSyncStrm = async (event) => {
            const nextChecked = !!event?.target?.checked;
            if (!nextChecked) {
                mediaOrganizeConfig.auto_sync_strm = false;
                return;
            }
            try {
                const res = await axios.get('/api/strm/get');
                const tasks = res.data?.sync_tasks || [];
                const valid = tasks.some(t => t.remote_path && t.local_path);
                if (!valid) {
                    showToast('请先在 STRM生成 页面配置好同步任务（远程路径、本地路径）', 'error');
                    mediaOrganizeConfig.auto_sync_strm = false;
                } else {
                    mediaOrganizeConfig.auto_sync_strm = true;
                }
            } catch (e) {
                showToast('验证 STRM 配置失败: ' + e.message, 'error');
                mediaOrganizeConfig.auto_sync_strm = false;
            }
        };

        const toggleFilenameOnlyMode = () => {
            const mode = mediaOrganizeConfig.organize_parse_mode;

            if (mode === 'filename') {
                mediaOrganizeConfig.organize_parse_mode = '';
                showToast('已关闭纯文件名整理', 'success');
                return;
            }
            if (mode === 'ffprobe') {
                showToast('当前已开启智能ffprobe整理，请先关闭再切换', 'warning');
                return;
            }
            if (mode === 'ffprobe_full') {
                showToast('当前已开启全量ffprobe整理，请先关闭再切换', 'warning');
                return;
            }
            mediaOrganizeConfig.organize_parse_mode = 'filename';
            showToast('已开启纯文件名整理', 'success');
        };

        const toggleFfprobeMode = () => {
            const mode = mediaOrganizeConfig.organize_parse_mode;

            if (mode === 'ffprobe') {
                mediaOrganizeConfig.organize_parse_mode = '';
                showToast('已关闭智能ffprobe整理', 'success');
                return;
            }
            if (mode === 'filename') {
                showToast('当前已开启纯文件名整理，请先关闭再切换', 'warning');
                return;
            }
            if (mode === 'ffprobe_full') {
                showToast('当前已开启全量ffprobe整理，请先关闭再切换', 'warning');
                return;
            }
            mediaOrganizeConfig.organize_parse_mode = 'ffprobe';
            showToast('已开启智能ffprobe整理', 'success');
        };

        const toggleFullFfprobeMode = () => {
            const mode = mediaOrganizeConfig.organize_parse_mode;

            if (mode === 'ffprobe_full') {
                mediaOrganizeConfig.organize_parse_mode = '';
                showToast('已关闭全量ffprobe整理', 'success');
                return;
            }
            if (mode === 'filename') {
                showToast('当前已开启纯文件名整理，请先关闭再切换', 'warning');
                return;
            }
            if (mode === 'ffprobe') {
                showToast('当前已开启智能ffprobe整理，请先关闭再切换', 'warning');
                return;
            }
            mediaOrganizeConfig.organize_parse_mode = 'ffprobe_full';
            showToast('已开启全量ffprobe整理', 'success');
        };

        const toggleWashByEquivalentSize = async (event) => {
            const nextChecked = !!event?.target?.checked;
            if (!nextChecked) {
                mediaOrganizeConfig.wash_by_equivalent_size = false;
                return;
            }
            const input = await showNumberDialog(
                '等效体积洗版容差',
                '请输入容差百分比。填写 2 表示当新文件等效体积大于旧文件的 0.98 倍时，也允许替换。适合在画质接近时优先保留较新的资源。',
                0,
                '例如 2 或 2.5',
                (value) => {
                    const normalized = String(value).trim();
                    const parsed = Number(normalized);
                    if (!normalized || !Number.isFinite(parsed) || parsed < 0 || parsed >= 100) {
                        return '请输入 0 到 100 之间的数字，且不能等于 100';
                    }
                    return '';
                }
            );
            if (input === null) {
                mediaOrganizeConfig.wash_by_equivalent_size = false;
                return;
            }
            mediaOrganizeConfig.wash_tolerance_ratio = Number(String(input).trim());
            mediaOrganizeConfig.wash_by_equivalent_size = true;
        };

        watch(() => mediaOrganizeConfig.emby_local_scrape, val => {
            mediaOrganizeConfig.scrape_enabled = !!val;
        });

        // --- 源目录 115 浏览 ---
        const browseOrganizeSource = async () => {
            orgSourceBrowser.history = [];
            orgSourceBrowser.path = '';
            orgSourceBrowser.opened = true;
            await loadOrgSourceDir('0');
        };

        const loadOrgSourceDir = async (cid) => {
            try {
                const res = await axios.post('/api/media_organize/browse115', {
                    cid: cid,
                    drive_index: 0
                });
                if (res.data.status === 'ok') {
                    orgSourceBrowser.dirs = res.data.dirs || [];
                    orgSourceBrowser.currentCid = cid;
                } else {
                    showToast(res.data.message, 'error');
                    orgSourceBrowser.dirs = [];
                }
            } catch (e) {
                showToast('浏览失败: ' + e.message, 'error');
                orgSourceBrowser.dirs = [];
            }
        };

        const selectOrgSourceDir = async (dir) => {
            orgSourceBrowser.history.push({ cid: orgSourceBrowser.currentCid, path: orgSourceBrowser.path });
            orgSourceBrowser.path = (orgSourceBrowser.path ? orgSourceBrowser.path + '/' : '/') + dir.name;
            await loadOrgSourceDir(dir.cid);
        };

        const orgSourceUp = async () => {
            if (orgSourceBrowser.history.length > 0) {
                const prev = orgSourceBrowser.history.pop();
                orgSourceBrowser.path = prev.path;
                await loadOrgSourceDir(prev.cid);
            }
        };

        const applyOrgSourcePath = () => {
            mediaOrganizeConfig.source_cid = orgSourceBrowser.currentCid;
            mediaOrganizeConfig.source_name = orgSourceBrowser.path || '根目录';
            orgSourceBrowser.dirs = [];
            orgSourceBrowser.path = '';
            orgSourceBrowser.history = [];
            orgSourceBrowser.opened = false;
        };

        // --- 目标目录 115 浏览 ---
        const browseOrganizeTarget = async () => {
            orgTargetBrowser.history = [];
            orgTargetBrowser.path = '';
            orgTargetBrowser.opened = true;
            await loadOrgTargetDir('0');
        };

        const loadOrgTargetDir = async (cid) => {
            try {
                const res = await axios.post('/api/media_organize/browse115', {
                    cid: cid,
                    drive_index: 0
                });
                if (res.data.status === 'ok') {
                    orgTargetBrowser.dirs = res.data.dirs || [];
                    orgTargetBrowser.currentCid = cid;
                } else {
                    showToast(res.data.message, 'error');
                    orgTargetBrowser.dirs = [];
                }
            } catch (e) {
                showToast('浏览失败: ' + e.message, 'error');
                orgTargetBrowser.dirs = [];
            }
        };

        const selectOrgTargetDir = async (dir) => {
            orgTargetBrowser.history.push({ cid: orgTargetBrowser.currentCid, path: orgTargetBrowser.path });
            orgTargetBrowser.path = (orgTargetBrowser.path ? orgTargetBrowser.path + '/' : '/') + dir.name;
            await loadOrgTargetDir(dir.cid);
        };

        const orgTargetUp = async () => {
            if (orgTargetBrowser.history.length > 0) {
                const prev = orgTargetBrowser.history.pop();
                orgTargetBrowser.path = prev.path;
                await loadOrgTargetDir(prev.cid);
            }
        };

        const applyOrgTargetPath = () => {
            mediaOrganizeConfig.target_cid = orgTargetBrowser.currentCid;
            mediaOrganizeConfig.target_name = orgTargetBrowser.path || '根目录';
            orgTargetBrowser.dirs = [];
            orgTargetBrowser.path = '';
            orgTargetBrowser.history = [];
            orgTargetBrowser.opened = false;
        };

        // --- 失败目录 115 浏览 ---
        const browseOrganizeFailed = async () => {
            orgFailedBrowser.history = [];
            orgFailedBrowser.path = '';
            orgFailedBrowser.opened = true;
            await loadOrgFailedDir('0');
        };

        const loadOrgFailedDir = async (cid) => {
            try {
                const res = await axios.post('/api/media_organize/browse115', {
                    cid: cid,
                    drive_index: 0
                });
                if (res.data.status === 'ok') {
                    orgFailedBrowser.dirs = res.data.dirs || [];
                    orgFailedBrowser.currentCid = cid;
                } else {
                    showToast(res.data.message, 'error');
                    orgFailedBrowser.dirs = [];
                }
            } catch (e) {
                showToast('浏览失败: ' + e.message, 'error');
                orgFailedBrowser.dirs = [];
            }
        };

        const selectOrgFailedDir = async (dir) => {
            orgFailedBrowser.history.push({ cid: orgFailedBrowser.currentCid, path: orgFailedBrowser.path });
            orgFailedBrowser.path = (orgFailedBrowser.path ? orgFailedBrowser.path + '/' : '/') + dir.name;
            await loadOrgFailedDir(dir.cid);
        };

        const orgFailedUp = async () => {
            if (orgFailedBrowser.history.length > 0) {
                const prev = orgFailedBrowser.history.pop();
                orgFailedBrowser.path = prev.path;
                await loadOrgFailedDir(prev.cid);
            }
        };

        const applyOrgFailedPath = () => {
            mediaOrganizeConfig.failed_cid = orgFailedBrowser.currentCid;
            mediaOrganizeConfig.failed_name = orgFailedBrowser.path || '根目录';
            orgFailedBrowser.dirs = [];
            orgFailedBrowser.path = '';
            orgFailedBrowser.history = [];
            orgFailedBrowser.opened = false;
        };

        // 执行整理
        const runOrganize = async () => {
            if (!mediaOrganizeConfig.source_cid || mediaOrganizeConfig.source_cid === '0') { showToast('请先配置源目录', 'error'); return; }
            if (!mediaOrganizeConfig.target_cid || mediaOrganizeConfig.target_cid === '0') { showToast('请先配置目标目录', 'error'); return; }

            organizeLoading.value = true;
            organizeResult.value = null;
            organizeProgress.percent = 0;
            organizeProgress.status_text = '启动中...';
            organizeProgress.detail = null;
            try {
                await saveMediaOrganizeConfig();
                const res = await axios.post('/api/media_organize/organize', {
                    media_type: organizeForm.media_type,
                    is_bluray: organizeForm.is_bluray,
                    overwrite: organizeForm.overwrite,
                    drive_index: 0,
                });
                if (res.data.status === 'ok') {
                    organizeRunId.value = res.data.run_id;
                    localStorage.setItem(ORGANIZE_RUN_ID_STORAGE_KEY, organizeRunId.value);
                    showToast('整理任务已启动', 'success');
                    startOrganizePolling();
                } else {
                    showToast(res.data.message || '整理启动失败', 'error');
                    organizeLoading.value = false;
                }
            } catch (e) {
                organizeResult.value = { status: 'error', message: e.response?.data?.detail || e.message };
                showToast('整理请求失败', 'error');
                organizeLoading.value = false;
            }
        };

        const cancelOrganize = async () => {
            if (!organizeRunId.value) return;
            try {
                await axios.post('/api/stop_task', { run_id: organizeRunId.value });
                showToast('已发送取消请求', 'info');
            } catch (e) {
                showToast('取消失败: ' + e.message, 'error');
            }
        };

        const startOrganizePolling = () => {
            if (!organizeRunId.value) return;
            organizeLoading.value = true;
            stopOrganizePolling();
            organizePollTimer = setInterval(async () => {
                try {
                    const res = await axios.get('/api/progress');
                    const tasks = res.data || {};
                    const task = syncOrganizeTaskFromTaskMap(tasks, { adoptRunning: true });
                    if (!task) return;

                    if (task.status === 'finished' || task.status === 'error' || task.status === 'stopped') {
                        organizeLoading.value = false;
                        const detail = task.detail || {};
                        const label = task.status === 'finished' ? '完成' : (task.status === 'stopped' ? '已取消' : '异常');
                        organizeResult.value = {
                            status: task.status === 'finished' ? 'success' : 'error',
                            message: `整理${label}: 成功 ${detail.success || 0}/${detail.total || 0} | 失败 ${detail.failed || 0}`,
                            detail: detail,
                        };
                        showToast(`整理${label}`, task.status === 'finished' ? 'success' : 'error');
                        stopOrganizePolling();
                        const finishedRunId = organizeRunId.value;
                        organizeRunId.value = null;
                        localStorage.removeItem(ORGANIZE_RUN_ID_STORAGE_KEY);
                        if (finishedRunId) {
                            setTimeout(() => axios.post('/api/clear_task_progress', { run_id: finishedRunId }), 3000);
                        }
                    }
                } catch (e) { /* ignore */ }
            }, 2000);
        };

        const stopOrganizePolling = () => {
            if (organizePollTimer) {
                clearInterval(organizePollTimer);
                organizePollTimer = null;
            }
        };

        // tab 切换时加载配置
        watch(tab, (v) => {
            if (v === 'media_organize') {
                fetchMediaOrganizeConfig();
                restoreRunningOrganizeTask();
            }
            if (v === 'media_organize_rules') fetchCategoryRules();
        });

        const startStrmSync = async (taskIndex, mode) => {
            try {
                // 先保存配置
                await saveStrmConfig();

                const res = await axios.post('/api/strm/start', {
                    task_index: taskIndex,
                    mode: mode
                });

                if (res.data.status === 'ok') {
                    strmProgress.running = true;
                    strmProgress.run_id = res.data.run_id;
                    strmProgress.percent = 0;
                    strmProgress.status_text = mode === 'full' ? '全量同步中...' : '增量同步中...';
                    strmProgress.scanned = 0;
                    strmProgress.scanned_dirs = 0;
                    strmProgress.scanned_files = 0;
                    strmProgress.generated = 0;
                    strmProgress.generated_dirs = 0;
                    strmProgress.downloaded = 0;
                    strmProgress.downloaded_dirs = 0;
                    strmProgress.download_failed = 0;
                    strmProgress.skipped = 0;
                    strmProgress.skip_reasons = {};
                    strmProgress.failed = 0;
                    strmProgress.last_result = '';
                    showToast(res.data.message, 'success');
                    startStrmPolling();
                } else {
                    showToast(res.data.message, 'error');
                }
            } catch (e) {
                showToast('启动失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const stopStrmSync = async () => {
            if (!strmProgress.run_id) return;
            try {
                await axios.post('/api/strm/stop', { run_id: strmProgress.run_id });
                strmProgress.status_text = '正在取消...';
                showToast('已发送取消请求', 'info');
            } catch (e) {
                showToast('取消失败: ' + e.message, 'error');
            }
        };

        const startStrmPolling = () => {
            stopStrmPolling();
            strmPollTimer = setInterval(async () => {
                try {
                    const res = await axios.get('/api/strm/progress');
                    const tasks = res.data?.tasks || {};

                    // 找到当前运行的任务
                    let found = false;
                    for (const [rid, task] of Object.entries(tasks)) {
                        if (rid === strmProgress.run_id) {
                            found = true;
                            const detail = task.detail || {};
                            strmProgress.percent = Math.round(task.percent || 0);
                            strmProgress.status_text = task.cancel_requested ? '正在取消...' : (task.name || '');
                            strmProgress.scanned = detail.scanned || 0;
                            strmProgress.scanned_dirs = detail.scanned_dirs || 0;
                            strmProgress.scanned_files = detail.scanned_files || 0;
                            strmProgress.generated = detail.generated || 0;
                            strmProgress.generated_dirs = detail.generated_dirs || 0;
                            strmProgress.downloaded = detail.downloaded || 0;
                            strmProgress.downloaded_dirs = detail.downloaded_dirs || 0;
                            strmProgress.download_failed = detail.download_failed || 0;
                            strmProgress.skipped = detail.skipped || 0;
                            strmProgress.skip_reasons = detail.skip_reasons || {};
                            strmProgress.failed = detail.failed || 0;

                            if (task.status === 'finished' || task.status === 'error' || task.status === 'stopped') {
                                strmProgress.running = false;
                                const label = task.status === 'finished' ? '完成' : (task.status === 'stopped' ? '已取消' : '异常');
                                strmProgress.last_result = `同步${label}: 扫描 ${strmProgress.scanned} | 生成 ${strmProgress.generated} | 下载 ${strmProgress.downloaded} | 失败 ${strmProgress.failed}`;
                                stopStrmPolling();
                            }
                            break;
                        }
                    }

                    if (!found && strmProgress.running) {
                        // 任务已从活动列表消失
                        strmProgress.running = false;
                        strmProgress.last_result = '同步任务已完成';
                        stopStrmPolling();
                    }
                } catch (e) { /* ignore */ }
            }, 2000);
        };

        const stopStrmPolling = () => {
            if (strmPollTimer) {
                clearInterval(strmPollTimer);
                strmPollTimer = null;
            }
        };

        // 115 目录浏览
        const browseStrmDir = async (taskIdx) => {
            strmBrowser.taskIdx = taskIdx;
            strmBrowser.history = [];
            await loadBrowseDir('0');
        };

        const loadBrowseDir = async (cid) => {
            try {
                const res = await axios.post('/api/strm/browse115', {
                    cid: cid,
                    drive_index: 0
                });
                if (res.data.status === 'ok') {
                    strmBrowser.dirs = res.data.dirs || [];
                } else {
                    showToast(res.data.message, 'error');
                    strmBrowser.dirs = [];
                }
            } catch (e) {
                showToast('浏览失败: ' + e.message, 'error');
                strmBrowser.dirs = [];
            }
        };

        const selectStrmDir = async (taskIdx, dir) => {
            strmBrowser.history.push({ cid: '0', path: strmBrowser.path });
            strmBrowser.path = (strmBrowser.path ? strmBrowser.path + '/' : '/') + dir.name;
            await loadBrowseDir(dir.cid);
        };

        const browseStrmDirUp = async (taskIdx) => {
            if (strmBrowser.history.length > 0) {
                const prev = strmBrowser.history.pop();
                strmBrowser.path = prev.path;
                await loadBrowseDir(prev.cid);
            }
        };

        // 双击目录名选中为路径
        const applyBrowsePath = (taskIdx) => {
            if (strmBrowser.path) {
                strmConfig.sync_tasks[taskIdx].remote_path = strmBrowser.path;
                strmBrowser.taskIdx = -1;
                strmBrowser.dirs = [];
                strmBrowser.path = '';
            }
        };

        // ==========================================
        // 本地目录浏览
        // ==========================================
        const localBrowser = reactive({
            taskIdx: -1,
            dirs: [],
            current: ''
        });

        const browseLocalDir = async (taskIdx) => {
            localBrowser.taskIdx = taskIdx;
            const task = strmConfig.sync_tasks[taskIdx];
            const startPath = task.local_path || '/';
            await loadLocalDir(startPath);
        };

        const loadLocalDir = async (path) => {
            try {
                const res = await axios.post('/api/strm/browse_local', { path });
                if (res.data.status === 'ok') {
                    localBrowser.dirs = res.data.dirs || [];
                    localBrowser.current = res.data.current || path;
                } else {
                    showToast(res.data.message, 'error');
                    localBrowser.dirs = [];
                }
            } catch (e) {
                showToast('浏览失败: ' + e.message, 'error');
            }
        };

        const selectLocalDir = async (dir) => {
            await loadLocalDir(dir.path);
        };

        const applyLocalBrowsePath = (taskIdx) => {
            if (localBrowser.current) {
                strmConfig.sync_tasks[taskIdx].local_path = localBrowser.current;
                localBrowser.taskIdx = -1;
                localBrowser.dirs = [];
            }
        };

        // 从服务器列表导入 (传入具体的目标对象)
        const importEmbyInfo = async (targetEmbyObj) => {
            if (servers.value.length === 0) return showToast('无可用服务器配置', 'error');

            let svr;
            if (servers.value.length === 1) {
                svr = servers.value[0];
            } else {
                // 多个服务器时，让用户选择
                const serverOptions = servers.value.map((s, i) =>
                    ({ label: `${s.name || '未命名'} (${s.url})`, value: i })
                );
                const selectedIndex = await showSelectDialog('选择服务器', '请选择要导入的服务器：', serverOptions);
                if (selectedIndex === null) return;  // 用户取消
                svr = servers.value[selectedIndex];
            }

            const ok = await showConfirm('导入配置', `从 ${svr.name || svr.url} 导入地址和密钥？`, 'warning');
            if (!ok) return;

            targetEmbyObj.url = svr.url;
            targetEmbyObj.key = svr.key;
            targetEmbyObj.public_host = svr.public_host || '';
            showToast('已导入', 'success');
        };


        // ==========================================
        // [新增] 影巢 (HDHive) 配置
        // ==========================================
        const hdhiveConfig = reactive({
            accounts: []
        });
        const hdhiveChecking = ref(false);

        const fetchHdhiveConfig = async () => {
            try {
                const res = await axios.get('/api/hdhive/config');
                Object.assign(hdhiveConfig, res.data);
                // 为每个账号初始化显示状态
                hdhiveConfig.accounts.forEach(acc => {
                    if (acc.showPassword === undefined) acc.showPassword = false;
                    if (acc.showToken === undefined) acc.showToken = false;
                    if (acc.showApiKey === undefined) acc.showApiKey = false;
                    if (acc.saving === undefined) acc.saving = false;
                    // 兼容旧配置：如果有 auto_checkin 字段，转换成 checkin_type
                    if (acc.checkin_type === undefined) {
                        if (acc.auto_checkin === true) {
                            acc.checkin_type = "normal";
                        } else {
                            acc.checkin_type = "none";
                        }
                        delete acc.auto_checkin;
                    }
                    // 如果 cron 为空或还是旧默认值，生成随机时间
                    if (!acc.checkin_cron || acc.checkin_cron === '0 8 * * *') {
                        acc.checkin_cron = `1 0 * * *`;
                    }
                    // 如果有用户信息则默认折叠，否则展开
                    if (acc.expanded === undefined) acc.expanded = !acc.user_info;
                });
            } catch (e) {
                console.error('加载影巢配置失败:', e);
            }
        };

        const saveHdhiveAccount = async (accountId) => {
            const account = hdhiveConfig.accounts.find(a => a.id === accountId);
            if (!account) return;

            account.saving = true;
            try {
                // 1. 保存配置
                await axios.post('/api/hdhive/account/update?account_id=' + accountId, {
                    name: account.name,
                    password: account.password,
                    token: account.token,
                    api_key: account.api_key,
                    enabled: account.enabled,
                    checkin_type: account.checkin_type,
                    checkin_cron: account.checkin_cron
                });

                // 2. 如果有密码但没有token，自动登录获取token
                if (account.password && !account.token) {
                    try {
                        const loginRes = await axios.post('/api/hdhive/login', { account_id: accountId });
                        if (loginRes.data.status === 'ok') {
                            account.token = loginRes.data.token;
                        }
                    } catch (e) {
                        console.log('自动获取Token失败:', e);
                    }
                }

                // 3. 如果有token，自动获取用户信息
                if (account.token) {
                    try {
                        await axios.post('/api/hdhive/user-info', { account_id: accountId });
                    } catch (e) {
                        console.log('自动获取用户信息失败:', e);
                    }
                }

                // 4. 如果有apikey，自动获取用量信息
                if (account.api_key) {
                    try {
                        await axios.post('/api/hdhive/usage', { account_id: accountId });
                    } catch (e) {
                        console.log('自动获取用量信息失败:', e);
                    }
                }

                // 5. 刷新配置以更新显示
                await fetchHdhiveConfig();

                account.expanded = false;  // 保存后折叠
                showToast('账号配置已保存', 'success');
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                account.saving = false;
            }
        };

        const toggleHdhiveCheckin = async (accountId, enabled) => {
            const account = hdhiveConfig.accounts.find(a => a.id === accountId);
            if (!account) return;
            const newType = enabled ? 'normal' : 'none';
            account.checkin_type = newType;
            try {
                await axios.post('/api/hdhive/account/update?account_id=' + accountId, {
                    checkin_type: newType,
                    checkin_cron: account.checkin_cron
                });
            } catch (e) {
                showToast('保存签到设置失败', 'error');
                account.checkin_type = enabled ? 'none' : 'normal';
            }
        };

        const addHdhiveAccount = async () => {
            try {
                const res = await axios.post('/api/hdhive/account/add', {
                    name: '',
                    password: '',
                    token: ''
                });
                const newAccount = res.data.account;
                newAccount.showPassword = false;
                newAccount.showToken = false;
                newAccount.showApiKey = false;
                newAccount.saving = false;
                newAccount.checkin_type = newAccount.checkin_type || 'none';
                newAccount.checkin_cron = `1 0 * * *`;
                newAccount.expanded = true;  // 新账号默认展开
                hdhiveConfig.accounts.push(newAccount);
                showToast('账号已添加', 'success');
            } catch (e) {
                showToast('添加失败', 'error');
            }
        };

        const removeHdhiveAccount = async (accountId) => {
            const ok = await showConfirm('删除账号', '确定删除此影巢账号吗？', 'danger');
            if (!ok) return;
            try {
                await axios.post('/api/hdhive/account/remove?account_id=' + accountId);
                const idx = hdhiveConfig.accounts.findIndex(a => a.id === accountId);
                if (idx > -1) hdhiveConfig.accounts.splice(idx, 1);
                showToast('账号已删除', 'success');
            } catch (e) {
                showToast('删除失败', 'error');
            }
        };

        const testHdhiveAccount = async (accountId) => {
            const account = hdhiveConfig.accounts.find(a => a.id === accountId);
            if (!account) return;
            account.testing = true;
            try {
                // 先保存账号信息
                await axios.post('/api/hdhive/account/update?account_id=' + accountId, {
                    name: account.name,
                    password: account.password,
                    token: account.token
                });
                // 测试连接
                const res = await axios.post('/api/hdhive/account/test', { account_id: accountId });
                if (res.data.success) {
                    account.status = 'ok';
                    showToast(res.data.message || '连接成功', 'success');
                } else {
                    account.status = 'error';
                    showToast(res.data.message || '连接失败', 'error');
                }
            } catch (e) {
                account.status = 'error';
                showToast('测试失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                account.testing = false;
            }
        };

        const loginHdhive = async (accountId) => {
            const account = hdhiveConfig.accounts.find(a => a.id === accountId);
            if (!account) return;
            if (!account.name || !account.password) {
                return showToast('请先填写账号和密码', 'error');
            }
            account.logging = true;
            try {
                const res = await axios.post('/api/hdhive/login', { account_id: accountId });
                if (res.data.status === 'ok') {
                    account.token = res.data.token;
                    account.status = 'ok';
                    showToast('Token 获取成功', 'success');
                } else {
                    showToast(res.data.message || '登录失败', 'error');
                    if (res.data.hint) {
                        console.log('提示:', res.data.hint);
                    }
                }
            } catch (e) {
                showToast('登录失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                account.logging = false;
            }
        };

        const checkinHdhive = async (accountId) => {
            const account = hdhiveConfig.accounts.find(a => a.id === accountId);
            if (!account) return;
            if (!account.token) {
                return showToast('请先获取 Token', 'error');
            }
            account.checking = true;
            try {
                // 先保存账号信息
                await axios.post('/api/hdhive/account/update?account_id=' + accountId, {
                    token: account.token
                });
                const res = await axios.post('/api/hdhive/checkin', { account_id: accountId });
                if (res.data.success) {
                    // 只有真正签到成功才增加计数
                    if (!res.data.already_checked_in) {
                        account.checkin_count = (account.checkin_count || 0) + 1;
                    }
                    account.last_checkin = new Date().toLocaleString();
                    showToast(res.data.message || '签到成功', 'success');
                } else {
                    showToast(res.data.message || '签到失败', 'error');
                }
            } catch (e) {
                showToast('签到失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                account.checking = false;
            }
        };

        const gamblerCheckinHdhive = async (accountId) => {
            const account = hdhiveConfig.accounts.find(a => a.id === accountId);
            if (!account) return;
            if (!account.token) {
                return showToast('请先获取 Token', 'error');
            }
            account.gambler_checking = true;
            try {
                const res = await axios.post('/api/hdhive/gambler-checkin', { account_id: accountId });
                if (res.data.success) {
                    // 只有真正签到成功才增加计数
                    if (!res.data.already_checked_in) {
                        account.checkin_count = (account.checkin_count || 0) + 1;
                    }
                    account.last_checkin = new Date().toLocaleString();
                    showToast(res.data.message || '赌狗签到成功', 'success');
                    // 刷新用户信息
                    await fetchHdhiveConfig();
                } else {
                    showToast(res.data.message || '赌狗签到失败', 'error');
                }
            } catch (e) {
                showToast('赌狗签到失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                account.gambler_checking = false;
            }
        };

        const checkinAllHdhive = async () => {
            hdhiveChecking.value = true;
            try {
                const res = await axios.post('/api/hdhive/checkin', {});
                const results = res.data.results || [];
                const successCount = results.filter(r => r.success).length;
                showToast(`签到完成: ${successCount}/${results.length} 成功`, successCount > 0 ? 'success' : 'error');
                // 刷新配置以更新签到状态
                await fetchHdhiveConfig();
            } catch (e) {
                showToast('批量签到失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                hdhiveChecking.value = false;
            }
        };

        const refreshHdhiveUserInfo = async (accountId) => {
            const account = hdhiveConfig.accounts.find(a => a.id === accountId);
            if (!account) return;
            account.refreshingInfo = true;
            try {
                const res = await axios.post('/api/hdhive/user-info', { account_id: accountId });
                if (res.data.status === 'ok') {
                    account.user_info = res.data.user_info;
                    showToast('获取用户信息成功', 'success');
                } else {
                    showToast(res.data.message || '获取用户信息失败', 'error');
                }
            } catch (e) {
                showToast('获取用户信息失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                account.refreshingInfo = false;
            }
        };

        const refreshHdhiveUsage = async (accountId) => {
            const account = hdhiveConfig.accounts.find(a => a.id === accountId);
            if (!account) return;
            account.refreshingUsage = true;
            try {
                const res = await axios.post('/api/hdhive/usage', { account_id: accountId });
                if (res.data.status === 'ok') {
                    account.usage = res.data.usage;

                    // 检查是否需要VIP
                    if (res.data.vip_required) {
                        showToast('API用量已更新（详细用户信息需要VIP会员）', 'success');
                    } else {
                        // 如果有用户详细信息，更新到user_info
                        if (res.data.user_detail && account.user_info) {
                            const detail = res.data.user_detail;
                            account.user_info.id = detail.id;
                            account.user_info.nickname = detail.nickname;
                            account.user_info.username = detail.username;
                            account.user_info.email = detail.email;
                            account.user_info.avatar_url = detail.avatar_url;
                            account.user_info.is_vip = detail.is_vip;
                            account.user_info.vip_expiration_date = detail.vip_expiration_date;
                            account.user_info.last_active_at = detail.last_active_at;
                            account.user_info.created_at = detail.created_at;
                            account.user_info.telegram_user = detail.telegram_user;
                            account.user_info.points = detail.points;
                            account.user_info.signin_days_total = detail.signin_days_total;
                            account.user_info.share_num = detail.share_num;
                            account.user_info.is_activate = detail.is_activate;
                            account.user_info.notification_method = detail.notification_method;
                        }
                        showToast('获取用量信息成功', 'success');
                    }
                } else {
                    showToast(res.data.message || '获取用量信息失败', 'error');
                }
            } catch (e) {
                showToast('获取用量信息失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                account.refreshingUsage = false;
            }
        };

        const previewServerIdx = ref(0);
        const libraryCards = ref([]);
        const loadingCovers = ref(false);
        const suiteList = ref([]);
        const newSuiteName = ref('');
        const creatingBackup = ref(false);
        const viewingSuite = ref(null);
        const viewingSuiteImages = ref([]);
        const selectedRestoreIds = ref([]);
        
        const autoSelections = reactive({});
        const taskName = ref('');
        const taskCron = ref('0 2 * * *');
        const taskEngine = ref('classic'); 
        const taskPreset = ref('');
        const runningTask = ref(false);
        const taskList = ref([]); 
        const taskMode = ref('random');
        const editingTaskId = ref(null);
        const showCreateTask = ref(false);

        const translationList = ref([]);
        const transServerIdx = ref(0); 
        const customAssets = reactive({ bg_url: null, posters: [] });
        
        const modalVisible = ref(false);
        const modalType = ref('Backdrop'); 
        const modalSource = ref('pool');
        const modalStep = ref('list'); 
        const modalImages = ref([]);
        const loadingModal = ref(false);
        const searchQuery = ref('');
        const searchResults = ref([]);
        
        const layoutSchemas = ref({});
        const accountForm = reactive({ old_password: '', new_username: '', new_password: '' });
        const updatingAccount = ref(false);
        const cleanup115Tasks = ref([]);
        const cleanup115EditingId = ref('');
        const showCreate115Cleanup = ref(false);
        const cleanup115Form = reactive({
            name: '',
            cron: '30 3 * * *',
            enabled: true,
            drive_index: 0,
            clear_recycle_bin: true,
            folders: []
        });
        const cleanup115Browser = reactive({
            visible: false,
            loading: false,
            currentCid: '0',
            currentPath: '/',
            history: [],
            dirs: []
        });

        const directUploadImg = ref('');

        const handleDirectUpload = (e) => {
            const file = e.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = (evt) => {
                directUploadImg.value = evt.target.result;
            };
            reader.readAsDataURL(file);
        }

        const applyDirectUpload = async () => {
            if (!directUploadImg.value) return showToast('请先选择图片', 'error');
            if (!currentLibId.value) return showToast('请先选择目标媒体库', 'error');
            
            const ok = await showConfirm('替换封面', '确定要使用这张图片直接替换当前媒体库封面吗？', 'warning');
            if (!ok) return;

            applying.value = true;
            try {
                const payload = {
                    url: currentManualServer.value.url,
                    key: currentManualServer.value.key,
                    public_host: currentManualServer.value.public_host,
                    library_id: currentLibId.value,
                    config: {}, 
                    image_data: directUploadImg.value
                };
                await axios.post('/api/apply', payload);
                showToast('封面替换成功', 'success');
                addLog('success', '已上传自定义封面');
            } catch (e) {
                showToast('上传失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                applying.value = false;
            }
        }

        // ==========================================
        // 4. RSS 相关逻辑 (修改版 - 含编辑功能)
        // ==========================================
        const rssConfig = reactive({ source_root: '', link_root: '' });
        const rssForm = reactive({ name: '', cron: '0 */4 * * *', rss_url: '', target_server_idx: 0, content_type: 'movies' });
        const rssTasks = ref([]);
        
        // ★ 新增：记录当前正在编辑的任务ID
        const editingRssTaskId = ref(null); 

        const fetchRssData = async () => {
            try {
                const cRes = await axios.get('/api/rss/config');
                Object.assign(rssConfig, cRes.data);
                const tRes = await axios.get('/api/rss/tasks');
                rssTasks.value = tRes.data;
            } catch {}
        };

        const saveRssConfig = async () => {
            try {
                const res = await axios.post('/api/rss/save_config', rssConfig);
                Object.assign(rssConfig, (await axios.get('/api/rss/config')).data || {});
                showToast(res.data?.message || 'RSS 路径已按标准拓扑保存', 'success');
            } catch { showToast('保存失败', 'error'); }
        };

        // 替换原有的 editRssTask
        const editRssTask = (task) => {
            editingRssTaskId.value = task.id;
            // 回填表单数据
            rssForm.name = task.name;
            rssForm.cron = task.cron;
            rssForm.rss_url = task.rss_url;
            rssForm.target_server_idx = task.target_server_idx || 0;
            rssForm.content_type = task.content_type || 'movies';
            
            showCreateRss.value = true; // <--- 自动展开表单

            // 滚动到顶部
            const container = document.querySelector('.content-area');
            if (container) container.scrollTop = 0;
        };

        // ★ 新增：取消编辑状态
       // 替换原有的 cancelRssEdit
        const cancelRssEdit = () => {
            editingRssTaskId.value = null;
            rssForm.name = '';
            rssForm.rss_url = '';
            rssForm.cron = '0 */4 * * *';
            rssForm.content_type = 'movies';
            rssForm.target_server_idx = 0;
            
            showCreateRss.value = false; // <--- 取消时收起表单
        };

        // 替换原有的 createRssTask
        const createRssTask = async () => {
            if(!rssForm.name || !rssForm.rss_url) return showToast('请填写完整信息', 'error');
            try {
                if (editingRssTaskId.value) {
                    // === 更新模式 ===
                    const originalTask = rssTasks.value.find(t => t.id === editingRssTaskId.value);
                    const enabledState = originalTask ? (originalTask.enabled !== false) : true;
                    
                    const payload = { ...rssForm, id: editingRssTaskId.value, enabled: enabledState };
                    await axios.post('/api/rss/update_task', payload);
                    showToast('RSS 订阅更新成功', 'success');
                    cancelRssEdit(); 
                } else {
                    // === 创建模式 ===
                    const payload = { ...rssForm };
                    await axios.post('/api/rss/create_task', payload);
                    showToast('RSS 订阅创建成功', 'success');
                    // 创建成功后清空表单并收起
                    rssForm.name = ''; 
                    rssForm.rss_url = '';
                    showCreateRss.value = false; // <--- 创建成功后收起
                }
                fetchRssData();
            } catch (e) { 
                showToast('操作失败: ' + (e.response?.data?.detail || e.message), 'error'); 
            }
        };

        const runRssTask = async (id) => {
            try {
                await axios.post('/api/rss/run_now', { id });
                showToast('RSS 抓取任务已后台运行', 'info');
            } catch { showToast('触发失败', 'error'); }
        };

        const deleteRssTask = async (id) => {
            const confirmTask = await showConfirm('删除订阅', '确定要删除此 RSS 订阅吗？', 'danger');
            if(!confirmTask) return;

            const deleteFiles = await showConfirm(
                '清理关联文件', 
                '是否同时删除硬盘上的硬链接文件和 Emby 中的媒体库？\n\n点击【确定】：彻底删除 (任务+文件+库)\n点击【取消】：仅删除任务配置 (保留文件)', 
                'warning'
            );
            
            try {
                await axios.post('/api/rss/delete_task', { id, delete_files: deleteFiles });
                if (deleteFiles) {
                    showToast('任务及关联资源已彻底删除', 'success');
                } else {
                    showToast('仅删除了任务配置 (文件已保留)', 'success');
                }
                fetchRssData();
            } catch (e) { 
                showToast('删除失败: ' + (e.response?.data?.detail || e.message), 'error'); 
            }
        };

        watch(tab, async (val) => {
            if (val !== 'config_115' && qrcode115State.visible) {
                close115QrLogin();
            }
            if (val === 'rss') fetchRssData();
            if (val === 'webhook') fetchWebhookConfig();
            if (val === 'library_preview') fetchLibraryCovers();
            if (val === 'config_yingchao') fetchHdhiveConfig();
            if (val === 'config_notification') { fetchWechatNotifyConfig(); fetchTelegramNotifyConfig(); }
            if (val === 'config_302') fetch302Config();
            if (val === 'server') {
                await fetch302Config();
                await nextTick();
                for (const emby of (config302.embys || [])) {
                    if (emby?.url && emby?.key && !emby.testing) {
                        await test302EmbyConnection(emby);
                    }
                }
            }
            if (val === 'config_115') {
                await fetch302Config();
                await nextTick();
                const drive = config302.drives?.[0];
                if (drive?.cookie && !drive.testing && !drive.qr_loading) {
                    await test115Cookie(drive);
                }
            }
            if (val === 'media_subscribe') {
                if (!discoverSourceTabs.value.length) {
                    loadDiscoverSources().then(() => { if (!mainGridItems.value.length) loadMainGrid(true); });
                } else if (!mainGridItems.value.length) {
                    loadMainGrid(true);
                }
            }
            if (val === 'config_moviepilot') fetchMpConfig();
        });

        // ==========================================
        // 5. Webhook 逻辑
        // ==========================================
        const webhookConfig = reactive({
            enabled: false,
            engine: 'classic',
            preset: '',
            mode: 'random'
        });
        const webhookUrl = ref(window.location.origin + '/api/webhook');

        const fetchWebhookConfig = async () => {
            try {
                const res = await axios.get('/api/webhook/config');
                Object.assign(webhookConfig, res.data);
                validateSelections();
            } catch(e) {}
        };

        const saveWebhookConfig = async () => {
            try {
                await axios.post('/api/webhook/config', webhookConfig);
                showToast('Webhook 配置已保存', 'success');
            } catch(e) {
                showToast('保存失败', 'error');
            }
        };

        const toggleWebhookStatus = async (event) => {
            const newState = event.target.checked;
            const oldState = webhookConfig.enabled;
            webhookConfig.enabled = newState;
            try {
                await axios.post('/api/webhook/config', webhookConfig);
                showToast(newState ? 'Webhook 已启用' : 'Webhook 已关闭', newState ? 'success' : 'info');
            } catch(e) {
                webhookConfig.enabled = oldState;
                event.target.checked = oldState;
                showToast('状态切换失败', 'error');
            }
        };

        const copyWebhookUrl = () => {
            navigator.clipboard.writeText(webhookUrl.value).then(() => {
                showToast('地址已复制', 'success');
            }).catch(() => {
                showToast('复制失败，请手动复制', 'error');
            });
        };

        watch(() => webhookConfig.engine, (newVal) => {
            if (!newVal) return;
            const currentPreset = presetList.value.find(p => p.filename === webhookConfig.preset);
            if (!webhookConfig.preset || (currentPreset && currentPreset.engine !== newVal)) {
                const available = presetList.value.filter(p => p.engine === newVal);
                if (available.length > 0) {
                    webhookConfig.preset = available[0].filename;
                } else {
                    webhookConfig.preset = '';
                }
            }
        });

        const mobileMenuVisible = ref(false); // 控制"更多"菜单抽屉的显示
        const navTrack = ref(null); // 导航栏轨道引用
        const indicatorStyle = ref({ left: '0px', width: '0px' }); // 指示器样式

        // 更新活动指示器位置
        const updateIndicator = () => {
            if (!navTrack.value) return;
            const items = navTrack.value.querySelectorAll('.mb-item');
            const activeIndex = Array.from(items).findIndex(item => item.classList.contains('active'));
            if (activeIndex === -1) return;

            const activeItem = items[activeIndex];

            // 使用 offsetLeft 获取相对于轨道的位置（包含滚动偏移）
            const itemLeft = activeItem.offsetLeft;
            const itemWidth = activeItem.offsetWidth;
            const indicatorWidth = 30;

            // 计算指示器位置，使其居中对齐到活动项
            const indicatorLeft = itemLeft + itemWidth / 2 - indicatorWidth / 2;

            indicatorStyle.value = {
                left: indicatorLeft + 'px',
                width: indicatorWidth + 'px'
            };

            // 自动滚动到可见区域
            const trackWidth = navTrack.value.offsetWidth;
            const itemCenter = itemLeft + itemWidth / 2;
            const scrollLeft = navTrack.value.scrollLeft;

            if (itemCenter - scrollLeft > trackWidth * 0.8 || itemCenter - scrollLeft < trackWidth * 0.2) {
                navTrack.value.scrollTo({
                    left: itemCenter - trackWidth / 2,
                    behavior: 'smooth'
                });
            }
        };

        // 监听tab变化更新指示器
        watch(tab, (newTab) => {
            try {
                localStorage.setItem(ACTIVE_TAB_STORAGE_KEY, newTab);
            } catch (_) {}

            nextTick(() => {
                updateIndicator();
            });

            if (tab.value === 'dashboard' && dashboardCovers.value.length > 0) {
                splitIntoRows();
            }
        });

        // 切换菜单显示
        const toggleMobileMenu = () => {
            mobileMenuVisible.value = !mobileMenuVisible.value;
        };

        // 选中 Tab 后自动关闭菜单
        const selectMobileTab = (t) => {
            tab.value = t;
            mobileMenuVisible.value = false;
        };

        // 手势滑动检测
        let touchStartX = 0;
        let touchStartY = 0;
        const handleTouchStart = (e) => {
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
        };

        const handleTouchEnd = (e) => {
            const touchEndX = e.changedTouches[0].clientX;
            const touchEndY = e.changedTouches[0].clientY;
            const diffX = touchEndX - touchStartX;
            const diffY = touchEndY - touchStartY;

            // 只响应水平滑动，且滑动距离超过50px
            if (Math.abs(diffX) > Math.abs(diffY) && Math.abs(diffX) > 50) {
                const tabs = ['dashboard', 'manual', 'custom', 'auto', 'rss', 'webhook',
                             'media_subscribe', 'library_preview', 'server', 'config_115', 'fonts',
                             'templates', 'translations', 'config_moviepilot', 'upgrade', 'account'];
                const currentIndex = tabs.indexOf(tab.value);

                if (diffX > 0 && currentIndex > 0) {
                    // 向右滑动 - 上一个tab
                    selectMobileTab(tabs[currentIndex - 1]);
                } else if (diffX < 0 && currentIndex < tabs.length - 1) {
                    // 向左滑动 - 下一个tab
                    selectMobileTab(tabs[currentIndex + 1]);
                }
            }
        };

        // 添加触摸事件监听 + Dock 键盘/窗口事件
        onMounted(() => {
            const contentArea = document.querySelector('.content-area');
            if (contentArea) {
                contentArea.addEventListener('touchstart', handleTouchStart);
                contentArea.addEventListener('touchend', handleTouchEnd);
            }
            // 初始化指示器位置
            nextTick(() => {
                updateIndicator();
            });

            // Dock: 键盘快捷键 (Cmd+K / Ctrl+K)
            document.addEventListener('keydown', handleKeydown);
            // Dock: 窗口大小变化监听
            handleResize();
            window.addEventListener('resize', handleResize);
            window.addEventListener('popstate', handleDetailPopstate);
        });

        axios.interceptors.request.use(cfg => cfg, error => Promise.reject(error));

        const modalTitle = computed(() => {
            const t = modalType.value === 'Backdrop' ? '背景图' : '海报';
            const s = modalSource.value === 'pool' ? '随机池' : '搜索';
            return `${s} - ${t}`;
        });

        const filteredPresets = computed(() => {
            if (!config.engine) return [];
            return presetList.value.map((p, index) => ({ ...p, originalIndex: index })).filter(p => p.engine === config.engine);
        });

        const currentSchema = computed(() => layoutSchemas.value[config.engine] || []);

        const pageTitle = computed(() => {
            const map = {
                'dashboard': '仪表盘', 'manual':'手动设计', 'custom':'封面设计', 'auto':'自动封面',
                'rss': 'RSS 真实库', 'webhook': 'Webhook', 'config_302': '302 配置',
                'server':'Emby 配置', 'fonts':'字体库', 'templates':'模板管理',
                'library_preview':'封面备份', 'translations':'翻译配置', 'account':'账户管理',
                'upgrade': '系统升级',
                'media_subscribe': '发现推荐', 'resource_transfer': '资源转存',
                'media_organize': '媒体整理', 'media_organize_rules': '二级分类规则', 'strm_generate': 'STRM 生成',
                'drive115_cleanup': '115 定时清空',
                'config_115': '115 配置', 'config_wechat': '微信配置',
                'config_telegram': '电报配置', 'config_yingchao': '影巢配置',
                'config_moviepilot': 'MoviePilot 配置', 'config_proxy': '代理配置',
                'config_tmdb': 'TMDB 配置'
            };
            return map[tab.value] || '仪表盘';
        });
        
        const currentManualServer = computed(() => servers.value[0] || {});
        const callbackUrl = computed(() => `http://<你的服务器IP或域名>${window.location.port ? ':' + window.location.port : ''}/api/wechat-notify/callback`);

        const fetchCurrentUserInfo = async () => {
            try {
                const res = await axios.get('/api/user_info');
                if (res.data && res.data.username) {
                    currentUsername.value = res.data.username;
                    localStorage.setItem('username', res.data.username);
                }
            } catch (e) { }
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

        const validateSelections = () => {
            const layouts = layoutList.value;
            if (layouts.length === 0) return;
            if (!taskEngine.value || !layouts.includes(taskEngine.value)) {
                taskEngine.value = layouts[0];
            }
            const taskAvail = presetList.value.filter(p => p.engine === taskEngine.value);
            if (taskAvail.length > 0 && (!taskPreset.value || !taskAvail.find(p=>p.filename===taskPreset.value))) {
                taskPreset.value = taskAvail[0].filename;
            }
            if (!webhookConfig.engine || !layouts.includes(webhookConfig.engine)) {
                webhookConfig.engine = layouts[0];
            }
            const whAvail = presetList.value.filter(p => p.engine === webhookConfig.engine);
            if (whAvail.length > 0 && (!webhookConfig.preset || !whAvail.find(p=>p.filename===webhookConfig.preset))) {
                webhookConfig.preset = whAvail[0].filename;
            }
            if (!config.engine || !layouts.includes(config.engine)) {
                config.engine = layouts[0];
                initLayoutConfig();
            }
        };

        watch(layoutList, (newVal) => { if (newVal && newVal.length > 0) validateSelections(); });
        watch(presetList, (newVal) => { if (newVal && newVal.length > 0) validateSelections(); });
        watch(() => config302.embys, () => { syncServersFrom302(); }, { deep: true });
        watch(isMobile, (mobile) => {
            if (mobile) {
                closeDesktopOverlays();
            }
        });

        // 找到 app.js 中的 onMounted 部分
        onMounted(async () => {
            applyTheme(resolveInitialTheme());
            if (!localStorage.getItem('isLoggedIn')) window.location.href = 'login.html';

            if (!allValidTabs.has(tab.value)) {
                tab.value = 'dashboard';
            }

            hydrateTaskLogs();
            webhookUrl.value = window.location.origin + '/api/webhook';

            startPolling();
            startDashboardDeviceMetricsPolling();
            startDashboard115Polling();
            loadProjectVersion();
            fetchUpgradeStatus();
            fetchCurrentUserInfo();
            fetchFonts(); fetchLayouts(); fetchLayoutAndPresets(); fetchSuites(); fetchTranslations(); fetchTasks(); fetchDashboardStats();
            fetchWebhookConfig();
            await fetch302Config();
            fetchStrmConfig();
            fetchMediaOrganizeConfig();
            await restoreRunningOrganizeTask();
            fetchHdhiveConfig();
            startHdhiveEventStream();
            loadDiscoverSources().then(() => {
                if (tab.value === 'media_subscribe' && !mainGridItems.value.length) loadMainGrid(true);
            });

            try {
                const res = await axios.get('/api/load');
                if (res.data) {
                    if (res.data.proxy_url) globalConfig.proxy_url = res.data.proxy_url;
                    if (res.data.tmdb_key) globalConfig.tmdb_key = res.data.tmdb_key;
                    if (res.data.douban_cookie) globalConfig.douban_cookie = res.data.douban_cookie;
                    if (res.data.app_public_base_url) globalConfig.app_public_base_url = res.data.app_public_base_url;
                    if (res.data.log_level) {
                        globalConfig.log_level = String(res.data.log_level).toUpperCase();
                    }
                    globalConfig.debug_mode = globalConfig.log_level === 'DEBUG';
                }
            } catch {}

            if (servers.value.length > 0) {
                await initDashboard();
                await fetchDashboardOverview();
                fetchLibs(0);
            }
        });

        onUnmounted(() => {
            close115QrLogin();
            stopPolling();
            stopDashboardDeviceMetricsPolling();
            stopDashboard115Polling();
            stopConsoleLogStream();
            document.removeEventListener('keydown', handleKeydown);
            window.removeEventListener('resize', handleResize);
            window.removeEventListener('popstate', handleDetailPopstate);
        });

        const splitIntoRows = () => {
            const covers = dashboardCovers.value || [];
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

        const refreshAllLibraries = async () => {
            for (let i = 0; i < servers.value.length; i++) {
                await fetchLibs(i);
            }
        };

        watch(tab, (newVal) => {
            if (newVal === 'drive115_cleanup') {
                fetch115CleanupTasks();
            }
            if (newVal === 'dashboard') {
                startDashboardDeviceMetricsPolling();
                startDashboard115Polling();
                if (dashboardCovers.value.length === 0) initDashboard();
                fetchDashboardOverview({ allowStale: true });
            } else {
                stopDashboardDeviceMetricsPolling();
                stopDashboard115Polling();
            }
        });

        const toggleAccordion = (key) => accordions[key] = !accordions[key];
        const toggleTaskLog = () => tasksState.logVisible = !tasksState.logVisible;

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

        const getDeviceMetricState = (percent) => {
            const value = Number(percent || 0);
            if (value >= 95) return 'danger';
            if (value >= 80) return 'warning';
            return 'normal';
        };

        const formatDevicePercent = (value) => `${Math.round(Number(value || 0))}%`;
        const formatDeviceMemory = (used, total) => `${Number(used || 0).toFixed(1)} / ${Number(total || 0).toFixed(1)} GB`;

        const splitMetricDisplay = (valueText, options = {}) => {
            const fallback = options.fallback || '--';
            const raw = String(valueText || fallback).trim();
            if (!raw || raw === fallback) {
                return { main: fallback, unit: '', split: false };
            }
            if (raw.endsWith('%')) {
                return {
                    main: raw.slice(0, -1) || '0',
                    unit: '%',
                    split: true,
                };
            }
            const matched = raw.match(/^([\d.]+)\s+(.+)$/);
            if (matched) {
                return {
                    main: matched[1],
                    unit: matched[2],
                    split: true,
                };
            }
            return { main: raw, unit: '', split: false };
        };

        const padSparklineHistory = (samples, targetLength = DASHBOARD_DEVICE_HISTORY_LIMIT) => {
            const safeSamples = Array.isArray(samples)
                ? samples.map((value) => {
                    const numeric = Number(value);
                    return Number.isFinite(numeric) ? numeric : 0;
                })
                : [];
            if (safeSamples.length >= targetLength) {
                return safeSamples.slice(-targetLength);
            }
            const firstValue = safeSamples.length > 0 ? safeSamples[0] : 0;
            return Array(targetLength - safeSamples.length).fill(firstValue).concat(safeSamples);
        };

        const buildSparklinePoints = (samples, options = {}) => {
            const width = options.width || DASHBOARD_SPARKLINE_WIDTH;
            const top = options.top ?? DASHBOARD_SPARKLINE_TOP;
            const bottom = options.bottom ?? DASHBOARD_SPARKLINE_BOTTOM;
            const paddingX = options.paddingX ?? 0;
            const mode = options.mode || 'throughput';
            const paddedSamples = padSparklineHistory(samples);
            let minValue = 0;
            let maxValue = 100;
            if (mode === 'percent') {
                minValue = 0;
                maxValue = 100;
            } else {
                const rawMin = Math.min(...paddedSamples);
                const rawMax = Math.max(...paddedSamples, 1);
                const rawRange = Math.max(rawMax - rawMin, Math.max(Math.abs(rawMax) * 0.16, 1));
                const verticalPadding = Math.max(rawRange * 0.18, 1);
                minValue = rawMin - verticalPadding;
                maxValue = rawMax + verticalPadding * 0.45;
            }
            if (!Number.isFinite(minValue)) minValue = 0;
            if (!Number.isFinite(maxValue)) maxValue = mode === 'percent' ? 100 : 1;
            if (maxValue <= minValue) {
                maxValue = minValue + (mode === 'percent' ? 1 : Math.max(1, Math.abs(minValue) * 0.1));
            }
            const range = maxValue - minValue;
            const stepX = paddedSamples.length > 1 ? (width - paddingX * 2) / (paddedSamples.length - 1) : 0;
            return paddedSamples.map((value, index) => {
                const ratio = Math.max(0, Math.min(1, (value - minValue) / range));
                const x = paddingX + stepX * index;
                const y = bottom - ratio * (bottom - top);
                return {
                    x: Number(x.toFixed(2)),
                    y: Number(y.toFixed(2)),
                };
            });
        };

        const buildSmoothSparklinePath = (points) => {
            if (!Array.isArray(points) || points.length === 0) return '';
            if (points.length === 1) {
                return `M ${points[0].x} ${points[0].y}`;
            }
            if (points.length === 2) {
                return `M ${points[0].x} ${points[0].y} L ${points[1].x} ${points[1].y}`;
            }
            let path = `M ${points[0].x} ${points[0].y}`;
            for (let index = 1; index < points.length - 1; index += 1) {
                const nextPoint = points[index + 1];
                const controlPoint = points[index];
                const midX = Number(((controlPoint.x + nextPoint.x) / 2).toFixed(2));
                const midY = Number(((controlPoint.y + nextPoint.y) / 2).toFixed(2));
                path += ` Q ${controlPoint.x} ${controlPoint.y} ${midX} ${midY}`;
            }
            const lastPoint = points[points.length - 1];
            path += ` T ${lastPoint.x} ${lastPoint.y}`;
            return path;
        };

        const buildSparklineAreaPath = (points, linePath, baselineY = DASHBOARD_SPARKLINE_BASELINE) => {
            if (!Array.isArray(points) || points.length === 0 || !linePath) return '';
            const firstPoint = points[0];
            const lastPoint = points[points.length - 1];
            return `${linePath} L ${lastPoint.x} ${baselineY} L ${firstPoint.x} ${baselineY} Z`;
        };

        const getMetricSparkline = (history, mode = 'throughput', key = 'metric', tone = 'cpu') => {
            const points = buildSparklinePoints(history, { mode });
            const linePath = buildSmoothSparklinePath(points);
            const tonePaletteMap = {
                cpu: {
                    lineStart: '#4caeb5',
                    lineEnd: '#5f8fdd',
                    fillStart: '#4caeb5',
                    fillEnd: '#5f8fdd',
                },
                memory: {
                    lineStart: '#6d9de0',
                    lineEnd: '#7f8fe6',
                    fillStart: '#6d9de0',
                    fillEnd: '#7f8fe6',
                },
                upload: {
                    lineStart: '#56c2b1',
                    lineEnd: '#5caecb',
                    fillStart: '#56c2b1',
                    fillEnd: '#5caecb',
                },
                download: {
                    lineStart: '#5c9fe0',
                    lineEnd: '#4f83dc',
                    fillStart: '#5c9fe0',
                    fillEnd: '#4f83dc',
                },
                'disk-read': {
                    lineStart: '#74acd8',
                    lineEnd: '#6d96d8',
                    fillStart: '#74acd8',
                    fillEnd: '#6d96d8',
                },
                'disk-write': {
                    lineStart: '#4fb3ca',
                    lineEnd: '#5a8fd2',
                    fillStart: '#4fb3ca',
                    fillEnd: '#5a8fd2',
                },
            };
            return {
                viewBox: DASHBOARD_SPARKLINE_VIEWBOX,
                linePath,
                areaPath: buildSparklineAreaPath(points, linePath),
                lineGradientId: `metric-sparkline-line-${key}`,
                fillGradientId: `metric-sparkline-fill-${key}`,
                palette: tonePaletteMap[tone] || tonePaletteMap.cpu,
                hasData: Array.isArray(history) && history.length > 0,
            };
        };

        const dashboardDeviceMetricCards = computed(() => {
            const cpuValueText = formatDevicePercent(dashboardDeviceMetrics.cpu.percent);
            const memoryValueText = formatDevicePercent(dashboardDeviceMetrics.memory.percent);
            const uploadValueText = dashboardDeviceMetrics.network.up_human || '--';
            const downloadValueText = dashboardDeviceMetrics.network.down_human || '--';
            const diskReadValueText = dashboardDeviceMetrics.disk.read_human || '--';
            const diskWriteValueText = dashboardDeviceMetrics.disk.write_human || '--';
            return [
                {
                    key: 'cpu',
                    label: 'CPU',
                    icon: 'fa-microchip',
                    tone: 'cpu',
                    state: getDeviceMetricState(dashboardDeviceMetrics.cpu.percent),
                    valueText: cpuValueText,
                    valueDisplay: splitMetricDisplay(cpuValueText),
                    subText: '',
                    sparkline: getMetricSparkline(dashboardDeviceMetricHistory.cpuPercent, 'percent', 'cpu', 'cpu'),
                },
                {
                    key: 'memory',
                    label: '内存',
                    icon: 'fa-memory',
                    tone: 'memory',
                    state: getDeviceMetricState(dashboardDeviceMetrics.memory.percent),
                    valueText: memoryValueText,
                    valueDisplay: splitMetricDisplay(memoryValueText),
                    subText: formatDeviceMemory(dashboardDeviceMetrics.memory.used_gb, dashboardDeviceMetrics.memory.total_gb),
                    sparkline: getMetricSparkline(dashboardDeviceMetricHistory.memoryPercent, 'percent', 'memory', 'memory'),
                },
                {
                    key: 'upload',
                    label: '上传',
                    icon: 'fa-arrow-up',
                    tone: 'upload',
                    state: 'normal',
                    valueText: uploadValueText,
                    valueDisplay: splitMetricDisplay(uploadValueText),
                    subText: '',
                    sparkline: getMetricSparkline(dashboardDeviceMetricHistory.uploadBytes, 'throughput', 'upload', 'upload'),
                },
                {
                    key: 'download',
                    label: '下载',
                    icon: 'fa-arrow-down',
                    tone: 'download',
                    state: 'normal',
                    valueText: downloadValueText,
                    valueDisplay: splitMetricDisplay(downloadValueText),
                    subText: '',
                    sparkline: getMetricSparkline(dashboardDeviceMetricHistory.downloadBytes, 'throughput', 'download', 'download'),
                },
                {
                    key: 'disk-read',
                    label: '读取',
                    icon: 'fa-hard-drive',
                    tone: 'disk-read',
                    state: 'normal',
                    valueText: diskReadValueText,
                    valueDisplay: splitMetricDisplay(diskReadValueText),
                    subText: '',
                    sparkline: getMetricSparkline(dashboardDeviceMetricHistory.diskReadBytes, 'throughput', 'disk-read', 'disk-read'),
                },
                {
                    key: 'disk-write',
                    label: '写入',
                    icon: 'fa-pen-to-square',
                    tone: 'disk-write',
                    state: 'normal',
                    valueText: diskWriteValueText,
                    valueDisplay: splitMetricDisplay(diskWriteValueText),
                    subText: '',
                    sparkline: getMetricSparkline(dashboardDeviceMetricHistory.diskWriteBytes, 'throughput', 'disk-write', 'disk-write'),
                },
            ];
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

        const fetchLayouts = async () => { 
            try { 
                const res = await axios.get('/api/layouts'); 
                layoutSchemas.value = res.data.layouts;
                const keys = Object.keys(res.data.layouts).sort();
                layoutList.value = keys;
                validateSelections();
            } catch (e) { console.error(e); } 
        }
        
        const initLayoutConfig = () => {
            const schema = layoutSchemas.value[config.engine];
            const baseConfig = { 
                engine: config.engine, badge_style: config.badge_style || 'none', badge_font: config.badge_font || '',
                badge_size: config.badge_size || 40, badge_bg_color: config.badge_bg_color || '#0f172a',
                badge_text_color: config.badge_text_color || '#ffffff', badge_opacity: config.badge_opacity !== undefined ? config.badge_opacity : 255,
                title: config.title, subtitle: config.subtitle, font_title: config.font_title || '', font_subtitle: config.font_subtitle || ''
            };
            for (const key in config) delete config[key];
            Object.assign(config, baseConfig);
            if (schema) {
                schema.forEach(group => { group.items.forEach(item => { if (config[item.key] === undefined) config[item.key] = item.default; }); });
            }
        };

        const tryAutoSelectTaskPreset = () => {
            if (!taskEngine.value) return;
            const available = presetList.value.filter(p => p.engine === taskEngine.value);
            if (available.length > 0) {
                const currentIsAvailable = available.some(p => p.filename === taskPreset.value);
                if (!taskPreset.value || !currentIsAvailable) taskPreset.value = available[0].filename;
            } else taskPreset.value = '';
        };

        const tryAutoSelectPreset = () => {
            if (!config.engine) return;
            const available = presetList.value.map((p, index) => ({ ...p, originalIndex: index })).filter(p => p.engine === config.engine);
            if (available.length > 0) { selectedPresetIdx.value = available[0].originalIndex; loadPreset(); } 
            else { selectedPresetIdx.value = ''; initLayoutConfig(); }
        };

        watch(() => config.engine, (newVal, oldVal) => { if (newVal !== oldVal) tryAutoSelectPreset(); });
        watch(taskEngine, (newVal, oldVal) => { if (newVal !== oldVal) { taskPreset.value = ''; tryAutoSelectTaskPreset(); } });
        watch(showCreateTask, (val) => { if (val && !taskPreset.value) tryAutoSelectTaskPreset(); });

        const fetchLayoutAndPresets = async () => {
            try {
                const res = await axios.get('/api/templates_v2');
                layoutGroups.value = res.data.data;
                presetList.value = res.data.all_raw;
                validateSelections();
                // 自动选择第一个预设
                tryAutoSelectPreset();
            } catch (e) { console.error(e); }
        };

        const loadPreset = () => { 
            const p = presetList.value[selectedPresetIdx.value]; 
            if (p) { 
                currentPresetFile.value = p.filename; 
                config.engine = p.engine || 'classic'; 
                initLayoutConfig(); 
                
                // === [修复开始] 暂存当前的标题信息 ===
                const currentTitle = config.title;
                const currentSubtitle = config.subtitle;

                // 应用模板配置 (此时旧标题会覆盖新标题)
                Object.assign(config, p.config); 
                
                // === [修复结束] 如果当前有选中的库，强制还原回当前的标题 ===
                if (currentLibId.value) {
                    config.title = currentTitle;
                    // 如果你希望副标题也跟随媒体库，不随模板改变，加上下面这行：
                    config.subtitle = currentSubtitle;
                }

                if(currentLibId.value) preview(); 
            } 
        }
        
        const saveAsNewPreset = async () => { const n = prompt("名称:"); if(n) { const payloadConfig = JSON.parse(JSON.stringify(config)); doSave("preset_"+Date.now()+".json", n, payloadConfig); } }
        
        const overwritePreset = async () => { 
            if(currentPresetFile.value) {
                const ok = await showConfirm('覆盖预设', '确定要覆盖当前的预设配置吗？此操作无法撤销。', 'warning');
                if(ok) {
                    const oldName = presetList.value.find(p=>p.filename===currentPresetFile.value).name; 
                    const payloadConfig = JSON.parse(JSON.stringify(config)); 
                    doSave(currentPresetFile.value, oldName, payloadConfig); 
                }
            } 
        }
        
        const doSave = async (f, n, cfg) => { 
            try{ 
                await axios.post('/api/save_template', { filename:f, name:n, engine:config.engine, config: cfg, image_data: previewImage.value }); 
                showToast('预设保存成功: ' + n, 'success');
                fetchLayoutAndPresets(); 
            }catch{ showToast('保存预设失败', 'error'); } 
        }
        
        const deleteTemplate = async (f) => { 
            const ok = await showConfirm('删除预设', '确定要删除这个预设模板吗？', 'danger');
            if(ok) { 
                await axios.post('/api/delete_template', {filename:f}); 
                showToast('预设已删除', 'success');
                fetchLayoutAndPresets(); 
            } 
        }

        const loadTransFromLib = async () => {
            const svr = servers.value[0];
            if (!svr || !svr.url || !svr.key) { showToast("请先配置有效的服务器信息", 'error'); return; }
            try {
                const res = await axios.post('/api/connect', { url: svr.url, key: svr.key, public_host: svr.public_host });
                const libs = res.data.libraries || [];
                const savedRes = await axios.get('/api/translations');
                const savedMap = savedRes.data;
                const newList = libs.map(lib => ({ key: lib.name, val: savedMap[lib.name] || '' }));
                for (const k in savedMap) { if (!newList.find(item => item.key === k)) { newList.push({ key: k, val: savedMap[k] }); } }
                translationList.value = newList;
                showToast(`已读取 ${libs.length} 个媒体库翻译`, 'info');
            } catch (e) { showToast('连接服务器失败', 'error'); }
        };
        const fetchTranslations = async () => { try { const res = await axios.get('/api/translations'); translationList.value = Object.entries(res.data).map(([k, v]) => ({ key: k, val: v })); } catch { } };
        const saveTranslations = async () => { const map = {}; translationList.value.forEach(item => { if (item.key) map[item.key.trim()] = item.val.trim(); }); try { await axios.post('/api/save_translations', { translations: map }); showToast("翻译配置已保存", 'success'); } catch { } };
        const addTransRow = () => translationList.value.push({ key: '', val: '' });
        const removeTransRow = (idx) => translationList.value.splice(idx, 1);

        const addServer = () => add302Emby();

        const removeServer = async (i) => {
            await remove302Emby(i);
            syncServersFrom302();
        };
        
        const testConnection = async (idx, options = {}) => {
            const { silent = false } = options;
            const svr = servers.value[idx];
            if (!svr) return;
            svr.testing = true;
            try {
                const res = await axios.post('/api/connect', { url: svr.url, key: svr.key, public_host: svr.public_host });
                svr.libraries = res.data.libraries;
                svr.server_id = res.data.server_id || '';
                svr.status = 'ok';
                if (idx === 0) {
                    dashboardOverviewLoaded.value = false;
                    await initDashboard();
                    await fetchDashboardOverview();
                }
                if (idx === 0 && svr.libraries && svr.libraries.length > 0) {
                    if (!currentLibId.value) { currentLibId.value = svr.libraries[0].id; onLibChange(); }
                }
                if (!silent) showToast(`连接成功: ${svr.name}`, 'success');
                return res.data;
            } catch {
                svr.status = 'error';
                if (!silent) showToast(`连接失败: ${svr.name}`, 'error');
                return null;
            } finally {
                svr.testing = false;
            }
        }

        const test302EmbyConnection = async (emby) => {
            if (!emby || !emby.url || !emby.key) {
                return showToast('请先填写 Emby 地址和密钥', 'error');
            }
            emby.testing = true;
            try {
                await axios.post('/api/connect', { url: emby.url, key: emby.key, public_host: emby.public_host || '' });
                emby.status = 'ok';
                syncServersFrom302();
                showToast(`连接成功: ${emby.name || emby.url}`, 'success');
            } catch {
                emby.status = 'error';
                syncServersFrom302();
                showToast(`连接失败: ${emby.name || emby.url}`, 'error');
            } finally {
                emby.testing = false;
            }
        }
        
        const saveAllConfigs = async () => {
            await save302Config();
            dashboardOverviewLoaded.value = false;
            await initDashboard();
            await fetchDashboardOverview();
        }

        // [修复点 3] 新增保存全局配置的功能
        const saveGlobalSettings = async (showSuccessToast = true) => {
            try {
                const payload = {
                    proxy_url: globalConfig.proxy_url,
                    tmdb_key: globalConfig.tmdb_key,
                    douban_cookie: globalConfig.douban_cookie,
                    log_level: globalConfig.debug_mode ? 'DEBUG' : 'INFO',
                    app_public_base_url: globalConfig.app_public_base_url
                };
                await axios.post('/api/save', payload);
                globalConfig.log_level = payload.log_level;
                if (showSuccessToast) showToast('全局配置已保存', 'success');
                return true;
            } catch (e) {
                showToast('保存失败', 'error');
                return false;
            }
        };

        const toggleDebugMode = async (event) => {
            globalConfig.debug_mode = !!event.target.checked;
            try {
                const logLevel = globalConfig.debug_mode ? 'DEBUG' : 'INFO';
                await axios.post('/api/save', { log_level: logLevel });
                globalConfig.log_level = logLevel;
                showToast(globalConfig.debug_mode ? '调试日志已开启' : '调试日志已关闭', 'success');
            } catch (e) {
                globalConfig.debug_mode = !globalConfig.debug_mode;
                showToast('保存失败', 'error');
            }
        };

        const fetchLibs = (idx = 0) => testConnection(typeof idx === 'number' ? idx : 0);

        const onManualServerChange = () => { currentLibId.value = ''; previewImage.value = ''; fetchLibs(0); }
        
        const onLibChange = () => { 
            if (!currentManualServer.value.libraries) return;
            const l = currentManualServer.value.libraries.find(x => x.id === currentLibId.value); 
            if(l) { 
                config.title = l.name; 
                const trans = translationList.value.find(t => t.key === l.name); 
                config.subtitle = (trans && trans.val) ? trans.val : ''; 
                previewImage.value = ''; 
            } 
        }
        
        const preview = async () => { 
            if(!currentLibId.value) return; 
            loading.value = true; 
            try { 
                const res = await axios.post('/api/preview', getCustomPayload()); 
                previewImage.value = res.data.image; 
                showToast('预览已生成', 'success'); 
            } catch { showToast('预览生成失败', 'error'); } finally { loading.value = false; } 
        }
        
        const apply = async () => { 
            const ok = await showConfirm('应用封面', '确定要将此封面应用到 Emby 媒体库吗？', 'warning');
            if(!ok) return; 
            applying.value = true; 
            try { 
                const p = getCustomPayload(); p.image_data = previewImage.value; 
                await axios.post('/api/apply', p); 
                addLog('success', '封面已应用'); showToast('封面上传成功', 'success'); 
            } catch { addLog('error', '应用失败'); showToast('应用失败', 'error'); } finally { applying.value = false; } 
        }

        const handleBgUpload = (e) => { const file = e.target.files[0]; if(!file) return; const reader = new FileReader(); reader.onload = (evt) => { customAssets.bg_url = evt.target.result; }; reader.readAsDataURL(file); };
        const handlePosterUpload = (e) => { for(let file of e.target.files) { const reader = new FileReader(); reader.onload = (evt) => { customAssets.posters.push(evt.target.result); }; reader.readAsDataURL(file); } };
        
        const openPoolModal = async (type, source) => {
            modalVisible.value = true; modalType.value = type; modalSource.value = source; modalImages.value = []; searchResults.value = []; searchQuery.value = ''; modalStep.value = (source === 'search') ? 'list' : 'grid';
            if (source === 'pool') { loadingModal.value = true; try { const svr = currentManualServer.value; const res = await axios.post('/api/emby/random_pool', { url: svr.url, key: svr.key, public_host: svr.public_host, library_id: currentLibId.value, type: type, limit: 60 }); modalImages.value = res.data.images; } finally { loadingModal.value = false; } }
        };
        
        const doSearchInModal = async () => { if (!searchQuery.value) return; loadingModal.value = true; searchResults.value = []; modalStep.value = 'list'; try { const svr = currentManualServer.value; const res = await axios.post('/api/emby/search', { url: svr.url, key: svr.key, public_host: svr.public_host, query: searchQuery.value, library_id: currentLibId.value, type: modalType.value }); searchResults.value = res.data.items; } finally { loadingModal.value = false; } };
        const fetchItemImagesInModal = async (itemId) => { loadingModal.value = true; try { const svr = currentManualServer.value; const res = await axios.post('/api/emby/get_images', { url: svr.url, key: svr.key, public_host: svr.public_host, item_id: itemId, type: modalType.value }); modalImages.value = res.data.images; modalStep.value = 'grid'; } finally { loadingModal.value = false; } };
        const selectFromSearchResult = (url) => { const highResUrl = url.replace(/&maxHeight=\d+/, "&maxHeight=2160").replace(/&maxWidth=\d+/, ""); if (modalType.value === 'Backdrop') customAssets.bg_url = (customAssets.bg_url === highResUrl) ? null : highResUrl; else { const idx = customAssets.posters.indexOf(highResUrl); if (idx > -1) customAssets.posters.splice(idx, 1); else customAssets.posters.push(highResUrl); } };
        const isSelectedInModal = (url) => { if (!url) return false; const cleanUrl = url.split('&maxHeight')[0]; return modalType.value === 'Backdrop' ? (customAssets.bg_url && customAssets.bg_url.includes(cleanUrl)) : customAssets.posters.some(p => p.includes(cleanUrl)); };
        const selectInModal = (url) => { if (modalType.value === 'Backdrop') customAssets.bg_url = url; else { const idx = customAssets.posters.indexOf(url); if (idx > -1) customAssets.posters.splice(idx, 1); else customAssets.posters.push(url); } };
        const closeModal = () => modalVisible.value = false;

        const getCustomPayload = () => { const payloadConfig = JSON.parse(JSON.stringify(config)); const payload = { url: currentManualServer.value.url, key: currentManualServer.value.key, public_host: currentManualServer.value.public_host, library_id: currentLibId.value, config: payloadConfig, mode: manualMode.value, custom_assets: {} }; if (customAssets.bg_url) payload.custom_assets.bg_url = customAssets.bg_url; if (customAssets.posters.length > 0) payload.custom_assets.posters = customAssets.posters; return payload; };
        
        const previewCustom = async () => { 
            if (!currentLibId.value) return; 
            loading.value = true; 
            try { 
                const res = await axios.post('/api/preview', getCustomPayload()); 
                previewImage.value = res.data.image; 
                showToast('自定义预览生成', 'success'); 
            } catch { showToast('预览失败', 'error'); } finally { loading.value = false; } 
        };
        
        const applyCustom = async () => { 
            const ok = await showConfirm('应用封面', '确定要应用此自定义设计吗？', 'warning');
            if (!ok) return; 
            applying.value = true; 
            try { 
                const p = getCustomPayload(); p.image_data = previewImage.value; 
                await axios.post('/api/apply', p); 
                showToast('自定义封面已应用', 'success'); 
            } catch { showToast('应用失败', 'error'); } finally { applying.value = false; } 
        };

        // [修复] 增加对 idx 的边界检查，防止报错
        const toggleServerExpand = (idx) => { 
            const svr = servers.value[idx]; 
            if (!svr) {
                console.error("Server index out of bound:", idx);
                return;
            }
            svr.expanded = !svr.expanded; 
            if(svr.expanded && !svr.libraries) fetchLibs(idx); 
        }
        

        const isLibSelected = (sIdx, lId) => autoSelections[sIdx]?.includes(lId);
        const toggleLibSelection = (sIdx, lId) => { if(!autoSelections[sIdx]) autoSelections[sIdx] = []; const list = autoSelections[sIdx]; if(list.includes(lId)) list.splice(list.indexOf(lId), 1); else list.push(lId); }
        const isAllSelected = (sIdx) => { const svr = servers.value[sIdx]; if (!svr || !svr.libraries) return false; const sel = autoSelections[sIdx]; return sel && sel.length === svr.libraries.length; };
        const toggleSelectAll = (sIdx) => { const svr = servers.value[sIdx]; if (!svr || !svr.libraries) return; if (isAllSelected(sIdx)) { autoSelections[sIdx] = []; } else { autoSelections[sIdx] = svr.libraries.map(l => l.id); } };

        const getTaskTargets = () => {
            const targets = [];
            const selectedIds = autoSelections[0] || [];
            const server = servers.value[0];
            if (!server || !server.libraries || !selectedIds.length) return targets;
            selectedIds.forEach(id => {
                const library = server.libraries.find(x => x.id === id);
                if (library) {
                    targets.push({ server_idx: 0, library_id: id, library_name: library.name });
                }
            });
            return targets;
        }

        const runSavedTask = async (id) => { 
            const ok = await showConfirm('立即运行', '确定要立即触发此自动任务吗？', 'info');
            if(!ok) return; 
            try { 
                tasksState.activeCount++; 
                tasksState.statusText = "准备执行任务..."; 
                await axios.post('/api/run_saved_task', { id: id }); 
                showToast(`任务已提交后台运行`, 'info'); 
            } catch { showToast('请求执行任务失败', 'error'); } 
        };
        
        const editTask = (task) => {
            editingTaskId.value = task.id;
            taskName.value = task.name;
            taskCron.value = task.cron;
            taskMode.value = task.mode || 'random';
            showCreateTask.value = true;
            const presetObj = presetList.value.find(p => p.filename === task.preset);
            if (presetObj) {
                taskEngine.value = presetObj.engine;
                setTimeout(() => taskPreset.value = task.preset, 50);
            } else {
                taskPreset.value = task.preset;
            }
            for (const key in autoSelections) delete autoSelections[key];
            autoSelections[0] = [];
            task.targets.forEach(target => {
                autoSelections[0].push(target.library_id);
            });
            if (servers.value[0]) {
                servers.value[0].expanded = true;
                if (!servers.value[0].libraries) fetchLibs(0);
            }
            document.querySelector('.content-area').scrollTop = 0;
        };
        const cancelEdit = () => { editingTaskId.value = null; taskName.value = ''; taskCron.value = '0 2 * * *'; for (const key in autoSelections) delete autoSelections[key]; showCreateTask.value = false; };
        
        const createTask = async () => { 
            if (!taskName.value || !taskPreset.value) return showToast("请完善信息", 'error'); 
            const targets = getTaskTargets(); 
            if (!targets.length) return showToast("请选择库", 'error'); 
            try { 
                const payload = { id: editingTaskId.value, name: taskName.value, cron: taskCron.value, preset_filename: taskPreset.value, targets: targets, mode: taskMode.value }; 
                if (editingTaskId.value) { 
                    await axios.post('/api/update_task', payload); 
                    showToast('任务更新成功: ' + taskName.value, 'success'); 
                    cancelEdit(); 
                } else { 
                    await axios.post('/api/create_task', payload); 
                    showToast('任务创建成功: ' + taskName.value, 'success'); 
                    showCreateTask.value = false; 
                } 
                fetchTasks(); fetchDashboardStats(); 
            } catch (e) { showToast("操作失败: " + e, 'error'); } 
        };
        
        const runTaskNow = async () => { 
            if (!taskPreset.value) return showToast("请先选择预设", 'error'); 
            const targets = getTaskTargets(); 
            if (!targets.length) return showToast("请选择库", 'error'); 
            const ok = await showConfirm('批量试运行', `确定要对 ${targets.length} 个媒体库立即执行一次生成吗？`, 'warning');
            if(!ok) return; 
            runningTask.value = true; 
            try { 
                await axios.post('/api/run_task', { preset_filename: taskPreset.value, targets: targets, mode: taskMode.value }); 
                showToast(`手动批量任务已启动`, 'success'); 
            } catch { showToast(`启动失败`, 'error'); } finally { runningTask.value = false; } 
        }
        
        const deleteTask = async (id) => { 
            const ok = await showConfirm('删除任务', '确定要永久删除此自动任务吗？', 'danger');
            if(!ok) return; 
            try { await axios.post('/api/delete_task', { id: id }); showToast('任务已删除', 'success'); fetchTasks(); fetchDashboardStats(); } catch { showToast("删除失败", 'error'); } 
        };
        const fetchTasks = async () => { try { const res = await axios.get('/api/tasks'); taskList.value = res.data.tasks; } catch {} };
        
        const fetchLibraryCovers = async () => {
            const server = servers.value[0];
            if (!server || !server.url) return showToast("请先配置有效的服务器", "error");
            loadingCovers.value = true;
            try {
                const res = await axios.post("/api/library_covers", {
                    url: server.url,
                    key: server.key,
                    public_host: server.public_host,
                });
                const ts = Date.now();
                libraryCards.value = (res.data.libraries || []).map(item => {
                    if (item.cover_url) {
                        item.cover_url += `${item.cover_url.includes("?") ? "&" : "?"}_t=${ts}`;
                    }
                    return item;
                });
                showToast(`成功加载 ${libraryCards.value.length} 个媒体库`, "success");
            } catch (e) {
                console.error(e);
                showToast("刷新失败: " + (e.response?.data?.detail || e.message), "error");
            } finally {
                loadingCovers.value = false;
            }
        };
        const fetchSuites = async () => { 
    try { 
        const res = await axios.get('/api/list_suites?_t=' + new Date().getTime()); 
        suiteList.value = res.data.suites; 
        fetchDashboardStats(); 
    } catch {} 
};
        
        const createSuiteBackup = async () => { 
            if(!newSuiteName.value) return; 
            creatingBackup.value = true; tasksState.activeCount = 1; tasksState.statusText = "请求备份中..."; 
            try { 
                const svr = servers.value[0];
                await axios.post('/api/create_suite', { url: svr.url, key: svr.key, public_host: svr.public_host, suite_name: newSuiteName.value });
                newSuiteName.value=''; showToast('备份任务已开始', 'success'); 
            } catch (e) { showToast('备份请求异常: ' + e, 'error'); } finally { creatingBackup.value = false; } 
        }
        
        const deleteSuite = async (n) => { 
            const ok = await showConfirm('删除快照', `确定要删除备份快照 "${n}" 吗？`, 'danger');
            if(ok) { await axios.post('/api/delete_suite', { suite_name: n }); fetchSuites(); } 
        }
        const viewSuite = async (s) => { viewingSuite.value = s; try { const res = await axios.post('/api/get_suite_content', { suite_name: s.name }); viewingSuiteImages.value = res.data.images; selectedRestoreIds.value = res.data.images.map(i=>i.id); } catch{} }
        const closeSuiteView = () => { viewingSuite.value = null; viewingSuiteImages.value = []; }
        const getLibraryName = (id) => { const f = libraryCards.value.find(l=>l.id==id); return f?f.name:id; }
        const toggleRestoreSelect = (id) => { if(selectedRestoreIds.value.includes(id)) selectedRestoreIds.value = selectedRestoreIds.value.filter(x=>x!==id); else selectedRestoreIds.value.push(id); }
        
        const restoreSelected = async () => { 
            const ok = await showConfirm('恢复快照', `确定要恢复 ${selectedRestoreIds.value.length} 个库的封面吗？`, 'warning');
            if(!ok) return; 
            const svr = servers.value[0];
            try {
                await axios.post('/api/restore_suite', { url: svr.url, key: svr.key, public_host: svr.public_host, suite_name: viewingSuite.value.name, target_ids: selectedRestoreIds.value });
                showToast('恢复任务已提交', 'success'); closeSuiteView(); 
            } catch{ showToast('恢复失败', 'error'); } 
        }
        
        const restoreAll = async () => { 
            const ok = await showConfirm('全量恢复', `警告：这将覆盖当前所有库的封面。确定继续吗？`, 'danger');
            if(!ok) return; 
            const svr = servers.value[0];
            try {
                await axios.post('/api/restore_suite', { url: svr.url, key: svr.key, public_host: svr.public_host, suite_name: viewingSuite.value.name, target_ids: [] });
                showToast('全量恢复任务已提交', 'success'); closeSuiteView(); 
            } catch{ showToast('恢复失败', 'error'); } 
        }
        
        const fetchFonts = async () => { try{ fontList.value = (await axios.get('/api/fonts')).data.fonts; if (!config.font_title && fontList.value.length > 0) { config.font_title = fontList.value[0]; config.font_subtitle = fontList.value[0]; config.badge_font = fontList.value[0]; } fetchDashboardStats(); } catch{} }
        const uploadFont = async (e) => { const fd=new FormData(); fd.append("file", e.target.files[0]); await axios.post('/api/upload_font', fd); fetchFonts(); showToast('字体上传成功', 'success'); }
        
        const deleteFont = async (f) => { 
            const ok = await showConfirm('删除字体', `确定要删除字体 ${f} 吗？`, 'danger');
            if(ok) { await axios.post('/api/delete_font', {filename:f}); fetchFonts(); } 
        }

        const toggleTaskStatus = async (task, event) => {
            const newState = event.target.checked;
            // 乐观更新 UI (防止网络延迟导致的卡顿感)
            task.enabled = newState;
            
            try {
                await axios.post('/api/toggle_task', { id: task.id, enabled: newState });
                showToast(newState ? '任务已启用' : '任务已暂停', newState ? 'success' : 'info');
            } catch (e) {
                // 失败回滚
                task.enabled = !newState;
                event.target.checked = !newState;
                showToast('状态切换失败', 'error');
            }
        };

        const toggleRssStatus = async (task, event) => {
            const newState = event.target.checked;
            // 乐观更新
            task.enabled = newState;
            
            try {
                await axios.post('/api/rss/toggle_task', { id: task.id, enabled: newState });
                showToast(newState ? '订阅已启用' : '订阅已暂停', newState ? 'success' : 'info');
            } catch (e) {
                // 失败回滚
                task.enabled = !newState;
                event.target.checked = !newState;
                showToast('状态切换失败', 'error');
            }
        };
        
        const logout = () => {
            localStorage.removeItem('isLoggedIn');
            localStorage.removeItem('username');
            localStorage.removeItem(ACTIVE_TAB_STORAGE_KEY);
            window.location.href='login.html';
        }
        const forceReset = () => { modalVisible.value = false; loading.value = false; loadingModal.value = false; };

        // ==========================================
        // 微信通知逻辑
        // ==========================================
        const notificationTypes = ref({
            playback: { name: '播放通知', description: '有人通过302播放媒体时发送通知', icon: '🎬' },
            media_added: { name: '入库通知', description: '新媒体添加到媒体库时发送通知', icon: '📚' },
            organize_complete: { name: '整理通知', description: '媒体整理完成时发送通知', icon: '💿' },
            resource_transfer: { name: '转存通知', description: '115网盘转存完成时发送通知', icon: '📥' },
            checkin: { name: '签到通知', description: '影巢签到完成时发送通知', icon: '✅' },
            task_complete: { name: '任务通知', description: '海报生成等任务完成时发送通知', icon: '🎨' }
        });

        const templateLabels = {
            media_added: '入库通知模板',
            organize_complete: '整理通知模板',
            playback: '播放通知模板'
        };

        // 模板可用变量
        const templateVars = {
            media_added: ['title', 'year', 'media_type', 'library_name', 'rating', 'genres', 'overview', 'tagline', 'poster_url', 'now'],
            playback: ['title', 'year', 'original_name', 'media_type', 'rating', 'genres', 'overview', 'tagline', 'emby_name', 'user_name', 'client_info', 'now', 'poster_url'],
            organize_complete: ['title', 'year', 'media_type', 'season_episode', 'rating', 'genres', 'overview', 'tmdb_id', 'quality', 'audio', 'episode_count', 'file_size', 'release_group', 'elapsed']
        };

        // 默认模板
        const defaultTemplates = {
            media_added: {
                title: '《{{ title }}》{% if year %}({{ year }}){% endif %} 已入库 ✅',
                text: '⭐️评分：{{ rating or \'暂无\' }} ｜ 🎬类型：{{ genres or media_type }}{% if tagline %}\n💬标语：{{ tagline }}{% endif %}\n\n📝简介：{{ overview or \'暂无简介\' }}\n\n📁媒体库：{{ library_name }} ｜ 🕐入库时间：{{ now }}'
            },
            playback: {
                title: '🎬 正在播放《{{ title }}》{% if year %}({{ year }}){% endif %}',
                text: '⭐️评分：{{ rating or \'暂无\' }} ｜ 🎬类型：{{ genres or media_type }}{% if tagline %}\n💬标语：{{ tagline }}{% endif %}\n\n👤用户：{{ user_name or \'未知\' }}\n🖥️服务器：{{ emby_name }} ｜ 📱客户端：{{ client_info or \'未知\' }}\n🕐时间：{{ now }}\n\n📝简介：{{ overview or \'暂无简介\' }}'
            },
            organize_complete: {
                title: '💿 整理完成 ✅ 《{{ title }}》{% if year %}({{ year }}){% endif %}{% if season_episode %} {{ season_episode }}{% endif %}',
                text: '⭐️评分：{{ rating or \'暂无\' }}\n🎬类型：{{ media_type }}{% if genres %} · {{ genres }}{% endif %}{% if quality %}\n💎画质：{{ quality }}{% endif %}{% if audio %}\n🎵音质：{{ audio }}{% endif %}{% if episode_count %}\n📖数量：{{ episode_count }} 集{% endif %}{% if file_size %}\n⚖️大小：{{ file_size }}{% endif %}{% if tmdb_id %}\n🎬tmdbid：{{ tmdb_id }}{% endif %}{% if release_group %}\n👨\u200d🎨制作组：{{ release_group }}{% endif %}{% if elapsed %}\n⏱️整理耗时：{{ elapsed }}{% endif %}{% if overview %}\n\n📝简介：{{ overview }}{% endif %}'
            }
        };

        const createDefaultNotifyTypes = () => ({
            playback: true,
            media_added: true,
            organize_complete: true,
            resource_transfer: true,
            checkin: true,
            task_complete: true
        });

        const createDefaultTemplates = () => JSON.parse(JSON.stringify(defaultTemplates));

        const sanitizeTemplates = (templates) => {
            const result = {};
            for (const [key, tpl] of Object.entries(templates)) {
                result[key] = { title: tpl.title || '', text: tpl.text || '' };
            }
            return result;
        };

        const mergeNotifyConfig = (targetConfig, data, fields) => {
            targetConfig.enabled = data.enabled || false;
            fields.forEach((field) => {
                targetConfig[field] = data[field] || '';
            });
            targetConfig.notify_types = { ...createDefaultNotifyTypes(), ...(data.notify_types || {}) };
            const mergedTemplates = createDefaultTemplates();
            if (data.templates) {
                for (const key of Object.keys(defaultTemplates)) {
                    if (data.templates[key]) {
                        mergedTemplates[key] = { ...defaultTemplates[key], ...data.templates[key] };
                    }
                }
            }
            targetConfig.templates = mergedTemplates;
        };

        const buildNotifyPayload = (config, fields) => {
            const payload = {
                enabled: config.enabled,
                notify_types: config.notify_types,
                templates: sanitizeTemplates(config.templates)
            };
            fields.forEach((field) => {
                payload[field] = config[field];
            });
            return payload;
        };

        const toggleNotifyTypeFor = (config, typeKey) => {
            if (config.notify_types) {
                config.notify_types[typeKey] = !config.notify_types[typeKey];
            }
        };

        const resetNotifyTemplateFor = (config, tplKey) => {
            if (defaultTemplates[tplKey]) {
                config.templates[tplKey] = JSON.parse(JSON.stringify(defaultTemplates[tplKey]));
            }
        };

        const fetchNotifyConfig = async (endpoint, targetConfig, fields, errorMessage) => {
            try {
                const res = await axios.get(endpoint);
                mergeNotifyConfig(targetConfig, res.data, fields);
            } catch (e) {
                console.error(errorMessage, e);
            }
        };

        const saveNotifyConfig = async ({ savingRef, endpoint, config, fields, successMessage }) => {
            savingRef.value = true;
            try {
                await axios.post(endpoint, buildNotifyPayload(config, fields));
                showToast(successMessage, 'success');
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                savingRef.value = false;
            }
        };

        const testNotifyConnection = async ({ testingRef, endpoint }) => {
            testingRef.value = true;
            try {
                const res = await axios.post(endpoint);
                if (res.data.status === 'ok') {
                    showToast('连接成功: ' + res.data.message, 'success');
                } else {
                    showToast('连接失败: ' + res.data.message, 'error');
                }
            } catch (e) {
                showToast('测试失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                testingRef.value = false;
            }
        };

        const sendNotifyTestMessage = async ({ sendingRef, endpoint }) => {
            sendingRef.value = true;
            try {
                const res = await axios.post(`${endpoint}?message=${encodeURIComponent('这是一条来自ChillPoster的测试消息')}`);
                if (res.data.status === 'ok') {
                    showToast('测试消息发送成功', 'success');
                } else {
                    showToast('发送失败: ' + res.data.message, 'error');
                }
            } catch (e) {
                showToast('发送失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                sendingRef.value = false;
            }
        };

        const testNotifyTemplate = async ({ templateTestingRef, saveEndpoint, testEndpoint, config, fields }) => {
            templateTestingRef.value = true;
            try {
                await axios.post(saveEndpoint, buildNotifyPayload(config, fields));
                const res = await axios.post(testEndpoint);
                if (res.data.status === 'ok') {
                    showToast('模板测试通知发送成功', 'success');
                } else {
                    showToast('模板测试失败: ' + res.data.message, 'error');
                }
            } catch (e) {
                showToast('模板测试失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                templateTestingRef.value = false;
            }
        };

        const wechatNotifyFields = ['name', 'channel_name', 'corp_id', 'app_secret', 'token', 'agent_id', 'proxy_url', 'encoding_aes_key', 'admin_whitelist'];
        const telegramNotifyFields = ['name', 'bot_token', 'chat_id'];

        const wechatNotifyConfig = reactive({
            enabled: false,
            name: '微信',
            channel_name: '',
            corp_id: '',
            app_secret: '',
            token: '',
            agent_id: '',
            proxy_url: '',
            encoding_aes_key: '',
            admin_whitelist: '',
            notify_types: createDefaultNotifyTypes(),
            templates: createDefaultTemplates(),
            showSecret: false,
            showToken: false,
            showAesKey: false
        });

        const wechatNotifyTesting = ref(false);
        const wechatNotifySending = ref(false);
        const wechatNotifySaving = ref(false);

        const fetchWechatNotifyConfig = async () => {
            await fetchNotifyConfig('/api/wechat-notify/config', wechatNotifyConfig, wechatNotifyFields, '获取微信通知配置失败');
        };

        const toggleNotifyType = (typeKey) => {
            toggleNotifyTypeFor(wechatNotifyConfig, typeKey);
        };

        const saveWechatNotifyConfig = async () => {
            await saveNotifyConfig({
                savingRef: wechatNotifySaving,
                endpoint: '/api/wechat-notify/config',
                config: wechatNotifyConfig,
                fields: wechatNotifyFields,
                successMessage: '微信通知配置已保存'
            });
            await saveGlobalSettings(false);
        };

        const testWechatNotify = async () => {
            await testNotifyConnection({
                testingRef: wechatNotifyTesting,
                endpoint: '/api/wechat-notify/test'
            });
        };

        const sendWechatTestMsg = async () => {
            await sendNotifyTestMessage({
                sendingRef: wechatNotifySending,
                endpoint: '/api/wechat-notify/send'
            });
        };

        const wechatTemplateTesting = ref(false);
        const testWechatTemplate = async () => {
            await testNotifyTemplate({
                templateTestingRef: wechatTemplateTesting,
                saveEndpoint: '/api/wechat-notify/config',
                testEndpoint: '/api/wechat-notify/test-template',
                config: wechatNotifyConfig,
                fields: wechatNotifyFields
            });
        };

        // ==========================================
        // Telegram 通知逻辑
        // ==========================================
        const telegramNotifyConfig = reactive({
            enabled: false,
            name: 'Telegram',
            bot_token: '',
            chat_id: '',
            notify_types: createDefaultNotifyTypes(),
            templates: createDefaultTemplates(),
            showToken: false
        });
        const telegramNotifyTesting = ref(false);
        const telegramNotifySending = ref(false);
        const telegramNotifySaving = ref(false);

        const fetchTelegramNotifyConfig = async () => {
            await fetchNotifyConfig('/api/telegram-notify/config', telegramNotifyConfig, telegramNotifyFields, '获取Telegram通知配置失败');
        };

        const toggleTelegramNotifyType = (typeKey) => {
            toggleNotifyTypeFor(telegramNotifyConfig, typeKey);
        };

        const saveTelegramNotifyConfig = async () => {
            await saveNotifyConfig({
                savingRef: telegramNotifySaving,
                endpoint: '/api/telegram-notify/config',
                config: telegramNotifyConfig,
                fields: telegramNotifyFields,
                successMessage: 'Telegram通知配置已保存'
            });
        };

        const testTelegramNotify = async () => {
            await testNotifyConnection({
                testingRef: telegramNotifyTesting,
                endpoint: '/api/telegram-notify/test'
            });
        };

        const sendTelegramTestMsg = async () => {
            await sendNotifyTestMessage({
                sendingRef: telegramNotifySending,
                endpoint: '/api/telegram-notify/send'
            });
        };

        const telegramTemplateTesting = ref(false);
        const testTelegramTemplate = async () => {
            await testNotifyTemplate({
                templateTestingRef: telegramTemplateTesting,
                saveEndpoint: '/api/telegram-notify/config',
                testEndpoint: '/api/telegram-notify/test-template',
                config: telegramNotifyConfig,
                fields: telegramNotifyFields
            });
        };

        const resetWechatTemplate = (tplKey) => {
            resetNotifyTemplateFor(wechatNotifyConfig, tplKey);
        };

        const resetTelegramTemplate = (tplKey) => {
            resetNotifyTemplateFor(telegramNotifyConfig, tplKey);
        };

        const notificationChannels = computed(() => ([
            {
                key: 'telegram',
                title: 'Telegram 通知',
                iconClass: 'fa-brands fa-telegram icon-brand-telegram',
                config: telegramNotifyConfig,
                testing: telegramNotifyTesting,
                sending: telegramNotifySending,
                saving: telegramNotifySaving,
                templateTesting: telegramTemplateTesting,
                toggleType: toggleTelegramNotifyType,
                resetTemplate: resetTelegramTemplate,
                sendTest: sendTelegramTestMsg,
                testTemplate: testTelegramTemplate,
                save: saveTelegramNotifyConfig
            },
            {
                key: 'wechat',
                title: '微信通知',
                iconClass: 'fa-solid fa-comments icon-brand-wechat',
                config: wechatNotifyConfig,
                testing: wechatNotifyTesting,
                sending: wechatNotifySending,
                saving: wechatNotifySaving,
                templateTesting: wechatTemplateTesting,
                toggleType: toggleNotifyType,
                resetTemplate: resetWechatTemplate,
                sendTest: sendWechatTestMsg,
                testTemplate: testWechatTemplate,
                save: saveWechatNotifyConfig
            }
        ]));

        const wrapVar = (v) => '{{ ' + v + ' }}';

        const updateAccount = async () => {
            if(!accountForm.old_password || !accountForm.new_password) return showToast("请填写密码", 'error');
            updatingAccount.value = true;
            try {
                await axios.post('/api/change_auth', accountForm);
                showToast("修改成功，请重新登录", 'success');
                localStorage.setItem('username', accountForm.new_username);
                currentUsername.value = accountForm.new_username;
                setTimeout(logout, 1500);
            } catch (e) { showToast("修改失败", 'error'); } finally { updatingAccount.value = false; }
        };

        const fetch115CleanupTasks = async () => {
            try {
                const res = await axios.get('/api/drive115_cleanup/tasks');
                cleanup115Tasks.value = res.data?.tasks || [];
            } catch (e) {
                showToast('获取 115 定时清空任务失败', 'error');
            }
        };

        const reset115CleanupForm = () => {
            cleanup115EditingId.value = '';
            cleanup115Form.name = '';
            cleanup115Form.cron = '30 3 * * *';
            cleanup115Form.enabled = true;
            cleanup115Form.drive_index = 0;
            cleanup115Form.clear_recycle_bin = true;
            cleanup115Form.folders.splice(0);
            cleanup115Browser.visible = false;
        };

        const openCreate115Cleanup = () => {
            reset115CleanupForm();
            showCreate115Cleanup.value = true;
        };

        const edit115CleanupTask = (task) => {
            cleanup115EditingId.value = task.id || '';
            cleanup115Form.name = task.name || '';
            cleanup115Form.cron = task.cron || '30 3 * * *';
            cleanup115Form.enabled = task.enabled !== false;
            cleanup115Form.drive_index = Number(task.drive_index || 0);
            cleanup115Form.clear_recycle_bin = task.clear_recycle_bin !== false;
            cleanup115Form.folders.splice(0, cleanup115Form.folders.length, ...((task.folders || []).map(f => ({ ...f }))));
            showCreate115Cleanup.value = true;
        };

        const save115CleanupTask = async () => {
            if (!cleanup115Form.name.trim()) return showToast('请填写任务名称', 'error');
            if (!cleanup115Form.cron.trim()) return showToast('请填写 Cron 表达式', 'error');
            if (!cleanup115Form.folders.length) return showToast('请选择至少一个 115 文件夹', 'error');
            try {
                const payload = JSON.parse(JSON.stringify(cleanup115Form));
                if (cleanup115EditingId.value) {
                    await axios.post(`/api/drive115_cleanup/tasks/${cleanup115EditingId.value}`, payload);
                } else {
                    await axios.post('/api/drive115_cleanup/tasks', payload);
                }
                showToast('定时清空任务已保存', 'success');
                showCreate115Cleanup.value = false;
                reset115CleanupForm();
                fetch115CleanupTasks();
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const delete115CleanupTask = async (task) => {
            const ok = await showConfirm('删除任务', `确定删除定时清空任务「${task.name}」吗？`, 'danger');
            if (!ok) return;
            try {
                await axios.delete(`/api/drive115_cleanup/tasks/${task.id}`);
                showToast('任务已删除', 'success');
                fetch115CleanupTasks();
            } catch (e) {
                showToast('删除失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const toggle115CleanupTask = async (task) => {
            try {
                await axios.post(`/api/drive115_cleanup/tasks/${task.id}/toggle`, { enabled: task.enabled === false });
                fetch115CleanupTasks();
            } catch (e) {
                showToast('切换状态失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const run115CleanupTask = async (task) => {
            const folderText = (task.folders || []).map(f => f.path || f.name || f.cid).join('、');
            const recycleText = task.clear_recycle_bin !== false ? '，并清空回收站，删除不可恢复' : '';
            const ok = await showConfirm('立即清空 115 文件夹', `将清空以下目录内部内容：${folderText}${recycleText}。确定继续吗？`, 'danger');
            if (!ok) return;
            try {
                const res = await axios.post(`/api/drive115_cleanup/tasks/${task.id}/run`);
                const result = res.data?.result || {};
                showToast(result.message || '清理完成', result.status === 'error' ? 'error' : 'success');
                fetch115CleanupTasks();
            } catch (e) {
                showToast('执行失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const load115CleanupDir = async (cid = '0', path = '/') => {
            cleanup115Browser.loading = true;
            try {
                const res = await axios.post('/api/drive115_cleanup/browse115', { cid, drive_index: cleanup115Form.drive_index || 0 });
                if (res.data?.status !== 'ok') throw new Error(res.data?.message || '读取目录失败');
                cleanup115Browser.currentCid = String(cid || '0');
                cleanup115Browser.currentPath = path || '/';
                cleanup115Browser.dirs = res.data.dirs || [];
            } catch (e) {
                showToast('浏览失败: ' + (e.message || e), 'error');
            } finally {
                cleanup115Browser.loading = false;
            }
        };

        const open115CleanupBrowser = () => {
            if (cleanup115Browser.visible) {
                cleanup115Browser.visible = false;
                return;
            }
            cleanup115Browser.visible = true;
            cleanup115Browser.history.splice(0);
            load115CleanupDir('0', '/');
        };

        const select115CleanupDir = (dir) => {
            cleanup115Browser.history.push({ cid: cleanup115Browser.currentCid, path: cleanup115Browser.currentPath });
            const nextPath = cleanup115Browser.currentPath === '/' ? `/${dir.name}` : `${cleanup115Browser.currentPath}/${dir.name}`;
            load115CleanupDir(dir.cid, nextPath);
        };

        const cleanup115Up = () => {
            const prev = cleanup115Browser.history.pop();
            if (!prev) return;
            load115CleanupDir(prev.cid, prev.path);
        };

        const addCurrent115CleanupFolder = () => {
            if (!cleanup115Browser.currentCid || cleanup115Browser.currentCid === '0') return showToast('不能选择根目录', 'error');
            if (cleanup115Form.folders.some(f => String(f.cid) === String(cleanup115Browser.currentCid))) return showToast('该目录已添加', 'info');
            const path = cleanup115Browser.currentPath || cleanup115Browser.currentCid;
            const name = path.split('/').filter(Boolean).pop() || path;
            cleanup115Form.folders.push({ cid: cleanup115Browser.currentCid, name, path });
            cleanup115Browser.visible = false;
            showToast('已添加清空目录', 'success');
        };

        const remove115CleanupFolder = (cid) => {
            const idx = cleanup115Form.folders.findIndex(f => String(f.cid) === String(cid));
            if (idx >= 0) cleanup115Form.folders.splice(idx, 1);
        };

        watch(fontList, (newList) => { const old = document.getElementById('dynamic-font-styles'); if (old) old.remove(); let css = ''; newList.forEach(f => { css += `@font-face { font-family: '${f}'; src: url('/fonts/${f}'); font-display: swap; }`; }); const s = document.createElement('style'); s.id = 'dynamic-font-styles'; s.textContent = css; document.head.appendChild(s); }, { immediate: true, deep: true });

        // ==========================================
        // 13. MoviePilot 配置
        // ==========================================
        const mpConfig = reactive({ mp_url: '', mp_username: '', mp_password: '' });
        const mpTesting = ref(false);
        const mpTestResult = ref(null);

        const fetchMpConfig = async () => {
            try {
                const res = await axios.get('/api/moviepilot/config');
                Object.assign(mpConfig, res.data);
            } catch (e) { console.error('fetchMpConfig:', e); }
        };
        const saveMpConfig = async () => {
            try {
                await axios.post('/api/moviepilot/config', mpConfig);
                showToast('MoviePilot 配置已保存', 'success');
                mpTestResult.value = null;
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };
        const testMpConnection = async () => {
            mpTesting.value = true;
            mpTestResult.value = null;
            try {
                // 先保存再测试
                await axios.post('/api/moviepilot/config', mpConfig);
                const res = await axios.post('/api/moviepilot/test');
                mpTestResult.value = { ok: res.data.status === 'ok', msg: res.data.message || '连接成功' };
            } catch (e) {
                mpTestResult.value = { ok: false, msg: e.response?.data?.detail || '连接失败' };
            } finally { mpTesting.value = false; }
        };

        // ==========================================
        // 14. 发现推荐页
        // ==========================================
        const discoverRows = [
            { key: 'today_picks', title: '今日推荐', icon: 'fa-solid fa-gift', source: 'tmdb', endpoint: '/api/discover/today_picks' },
            { key: 'tmdb_trending', title: '本周热门', icon: 'fa-solid fa-fire', source: 'tmdb', endpoint: '/api/discover/tmdb/trending' },
            { key: 'tmdb_now_playing', title: '正在热映', icon: 'fa-solid fa-ticket', source: 'tmdb', endpoint: '/api/discover/tmdb/now_playing' },
            { key: 'tmdb_popular_movies', title: 'TMDB热门电影', icon: 'fa-solid fa-film', source: 'tmdb', endpoint: '/api/discover/tmdb/popular_movies' },
            { key: 'tmdb_popular_tv', title: 'TMDB热门剧集', icon: 'fa-solid fa-tv', source: 'tmdb', endpoint: '/api/discover/tmdb/popular_tv' },
            { key: 'douban_hot_movies', title: '豆瓣热门电影', icon: 'fa-solid fa-fire-flame-curved', source: 'douban', endpoint: '/api/discover/douban/hot_movies' },
            { key: 'douban_hot_tv', title: '豆瓣热门剧集', icon: 'fa-solid fa-tv', source: 'douban', endpoint: '/api/discover/douban/hot_tv' },
            { key: 'douban_hot_anime', title: '豆瓣热门动漫', icon: 'fa-solid fa-dragon', source: 'douban', endpoint: '/api/discover/douban/hot_anime' },
            { key: 'douban_showing', title: '豆瓣正在上映', icon: 'fa-solid fa-clapperboard', source: 'douban', endpoint: '/api/discover/douban/showing' },
            { key: 'douban_new_movies', title: '豆瓣最新电影', icon: 'fa-solid fa-sparkles', source: 'douban', endpoint: '/api/discover/douban/new_movies' },
            { key: 'douban_new_tv', title: '豆瓣热门国产剧', icon: 'fa-solid fa-list', source: 'douban', endpoint: '/api/discover/douban/new_tv' },
            { key: 'douban_top250', title: '豆瓣 Top 250', icon: 'fa-solid fa-trophy', source: 'douban', endpoint: '/api/discover/douban/top250' },
            { key: 'douban_chinese_weekly', title: '华语口碑剧集榜', icon: 'fa-solid fa-ranking-star', source: 'douban', endpoint: '/api/discover/douban/chinese_weekly' },
            { key: 'douban_global_weekly', title: '全球口碑剧集榜', icon: 'fa-solid fa-earth-americas', source: 'douban', endpoint: '/api/discover/douban/global_weekly' },
        ];

        const discoverData = reactive({});
        const discoverLoading = reactive({});
        const discoverErrors = reactive({});
        const detailModal = reactive({ visible: false, item: null, detail: null, loading: false, subscribed: false, selectedSeason: null, castExpanded: false, seasonSubscribed: false });
        let detailHistoryActive = false;
        let suppressDetailPopstate = false;
        const gridModal = reactive({ visible: false, title: '', row: null, items: [], page: 1, totalPages: 1, loadingMore: false, noMore: false });
        const gridModalEl = ref(null);
        const gridSentinel = ref(null);
        let gridObserver = null;
        const discoverSearchQuery = ref('');
        const discoverSearchResults = ref([]);
        const searchMovieResults = ref([]);
        const searchTvResults = ref([]);
        const discoverSearchLoading = ref(false);
        const discoverHasSearched = ref(false);
        const searchPage = ref(1);
        const searchTotalPages = ref(1);

        // ===== 发现页状态 (MP 克隆) =====
        const LIBRARY_STATUS_FILTER_KEY = '__library_status';
        const LIBRARY_STATUS_FILTER_ROW = {
            key: LIBRARY_STATUS_FILTER_KEY,
            label: '状态',
            control: 'chips',
            default: '',
            options: [
                { label: '已入库', value: 'exists' },
                { label: '未入库', value: 'missing' },
            ],
            show: '',
            depends_on: [],
        };
        const discoverSourceTabs = ref([]);
        const discoverActiveSource = ref('themoviedb');
        const discoverSourceMap = computed(() => Object.fromEntries((discoverSourceTabs.value || []).map(item => [item.key, item])));
        const discoverFiltersBySource = reactive({});
        const genreList = ref([]);

        const discoverSourceSupported = computed(() => !!discoverSourceMap.value[discoverActiveSource.value]);
        const activeSourceDef = computed(() => discoverSourceMap.value[discoverActiveSource.value] || null);
        const activeSourceSchema = computed(() => activeSourceDef.value?.filter_schema || []);
        const activeSourceFilters = computed(() => {
            const key = discoverActiveSource.value;
            if (!discoverFiltersBySource[key]) discoverFiltersBySource[key] = {};
            return discoverFiltersBySource[key];
        });
        const discoverEmptyText = computed(() => discoverSourceSupported.value ? '暂无内容' : '该数据源暂未接入当前项目');

        const fetchGenreList = async () => {
            if (genreList.value.length) return;
            try {
                const res = await axios.get('/api/discover/genres');
                genreList.value = res.data.genres || [];
            } catch (e) {
                console.error('加载类型失败:', e);
            }
        };

        const patchTmdbGenreSchema = () => {
            const source = discoverSourceMap.value['themoviedb'];
            if (!source) return;
            const schema = Array.isArray(source.filter_schema) ? source.filter_schema : [];
            const genreRow = schema.find(item => item.key === 'with_genres');
            if (genreRow && (!genreRow.options || !genreRow.options.length)) {
                genreRow.options = (genreList.value || []).map(g => ({ label: g.name, value: String(g.id), media_type: g.media_type }));
            }
        };

        const ensureSourceFilters = (source) => {
            if (!source) return;
            const defaults = {};
            Object.entries(source.filter_params || {}).forEach(([key, value]) => {
                defaults[key] = value == null ? '' : String(value);
            });
            (source.filter_schema || []).forEach(row => {
                if (!(row.key in defaults)) defaults[row.key] = row.default == null ? '' : String(row.default);
            });
            defaults[LIBRARY_STATUS_FILTER_KEY] = '';
            discoverFiltersBySource[source.key] = { ...(discoverFiltersBySource[source.key] || {}), ...defaults };
        };

        const loadDiscoverSources = async () => {
            if (discoverSourceTabs.value.length) return;
            try {
                const res = await axios.get('/api/discover/sources');
                discoverSourceTabs.value = res.data.sources || [];
                discoverSourceTabs.value.forEach(source => ensureSourceFilters(source));
                await fetchGenreList();
                patchTmdbGenreSchema();
                if (!discoverSourceMap.value[discoverActiveSource.value] && discoverSourceTabs.value.length) {
                    discoverActiveSource.value = discoverSourceTabs.value[0].key;
                }
            } catch (e) {
                console.error('加载发现源失败:', e);
            }
        };

        const getNormalizedDisplayFilters = () => {
            const filters = { ...(activeSourceFilters.value || {}) };
            if (discoverActiveSource.value === 'bilibili' && filters.mtype === 'guochuang') {
                filters.mtype = 'guo';
            }
            return filters;
        };

        const isFilterRowVisible = (row) => {
            if (!row || !row.show) return true;
            let expr = row.show.trim();
            if (expr.startsWith('{{') && expr.endsWith('}}')) expr = expr.slice(2, -2).trim();
            expr = expr.replace(/\|\|/g, '||').replace(/&&/g, '&&');
            const filters = getNormalizedDisplayFilters();
            try {
                return !!Function('filters', `with (filters) { return (${expr}); }`)(filters);
            } catch {
                return false;
            }
        };

        const getFilterRowDefaultValue = (row) => {
            if (!row) return '';
            if (row.default != null) return String(row.default);
            return '';
        };

        const getActiveSourceSchemaRows = () => [...(activeSourceSchema.value || []), LIBRARY_STATUS_FILTER_ROW];
        const getActiveSourceSchemaMap = () => Object.fromEntries(getActiveSourceSchemaRows().map(row => [row.key, row]));

        const getOptionParentValues = (option) => {
            if (!option) return [];
            if (Array.isArray(option.parent_values)) return option.parent_values.map(v => String(v));
            if (option.parent_values != null && option.parent_values !== '') return [String(option.parent_values)];
            if (option.media_type != null && option.media_type !== '') return [String(option.media_type)];
            return [];
        };

        const getFilteredRowOptions = (row) => {
            const options = Array.isArray(row?.options) ? row.options : [];
            if (!options.length) return options;
            const parents = Array.isArray(row?.depends_on) ? row.depends_on : [];
            const filtered = (!parents.length && row?.key !== 'with_genres') ? options : options.filter(opt => {
                const parentValues = getOptionParentValues(opt);
                if (!parentValues.length) return true;
                const matches = parents.some(parentKey => {
                    const currentValue = String(activeSourceFilters.value?.[parentKey] ?? '');
                    return parentValues.includes(currentValue) || parentValues.includes('both');
                });
                if (matches) return true;
                return String(opt.value ?? '') === '';
            });
            const deduped = [];
            const seen = new Set();
            filtered.forEach(opt => {
                const normalizedValue = String(opt?.value ?? '');
                const normalizedLabel = String(opt?.label ?? '');
                const signature = normalizedValue === ''
                    ? `__all__${normalizedLabel}`
                    : `${normalizedValue}__${normalizedLabel}`;
                if (seen.has(signature)) return;
                seen.add(signature);
                deduped.push(opt);
            });
            return deduped;
        };

        const getResolvedRowLabel = (row) => {
            const variants = Array.isArray(row?.label_variants) ? row.label_variants : [];
            if (!variants.length) return row?.label || '';
            const matched = variants.find(variant => {
                const parentValues = getOptionParentValues(variant);
                if (!parentValues.length) return false;
                const show = String(variant.show || '').trim();
                if (show && !isFilterRowVisible({ ...row, show })) return false;
                return (row.depends_on || []).some(parentKey => {
                    const currentValue = String(activeSourceFilters.value?.[parentKey] ?? '');
                    return parentValues.includes(currentValue) || parentValues.includes('both');
                });
            });
            return matched?.label || row?.label || '';
        };

        const resetDependentFilters = (changedKey) => {
            const schemaMap = getActiveSourceSchemaMap();
            const queue = [changedKey];
            const visited = new Set(queue);
            while (queue.length) {
                const current = queue.shift();
                (getActiveSourceSchemaRows()).forEach(row => {
                    if (!(row.depends_on || []).includes(current)) return;
                    if (visited.has(row.key)) return;
                    activeSourceFilters.value[row.key] = getFilterRowDefaultValue(row);
                    visited.add(row.key);
                    queue.push(row.key);
                });
            }
        };

        const pruneHiddenOrInvalidFilters = () => {
            const schemaMap = getActiveSourceSchemaMap();
            (getActiveSourceSchemaRows()).forEach(row => {
                if (!isFilterRowVisible(row)) {
                    activeSourceFilters.value[row.key] = getFilterRowDefaultValue(row);
                    return;
                }
                if (row.control !== 'chips') return;
                const filteredOptions = getFilteredRowOptions(row);
                const currentValue = String(activeSourceFilters.value?.[row.key] ?? '');
                if (!currentValue) return;
                const valid = filteredOptions.some(opt => String(opt.value ?? '') === currentValue);
                if (!valid) activeSourceFilters.value[row.key] = getFilterRowDefaultValue(row);
            });
        };

        const commitSourceFilterChange = (filterKey, value) => {
            if (!activeSourceFilters.value) return;
            activeSourceFilters.value[filterKey] = value;
            resetDependentFilters(filterKey);
            pruneHiddenOrInvalidFilters();
            resetMainGrid();
        };

        const getVisibleFilterRows = computed(() => {
            return (getActiveSourceSchemaRows()).filter(row => isFilterRowVisible(row)).map(row => ({
                ...row,
                label: getResolvedRowLabel(row),
                options: row.control === 'chips' ? getFilteredRowOptions(row) : (row.options || []),
            }));
        });

        const switchDiscoverSource = async (key) => {
            discoverActiveSource.value = key;
            if (key === 'themoviedb') await fetchGenreList();
            pruneHiddenOrInvalidFilters();
            resetMainGrid();
        };

        const updateSourceFilter = (filterKey, value) => {
            commitSourceFilterChange(filterKey, value);
        };

        const toggleSourceChip = (filterKey, value) => {
            const current = String(activeSourceFilters.value?.[filterKey] ?? '');
            const nextValue = String(value ?? '');
            const canToggleOff = filterKey !== 'media_type' || filterKey === LIBRARY_STATUS_FILTER_KEY;
            const changedValue = canToggleOff && current === nextValue && nextValue !== '' ? '' : nextValue;
            commitSourceFilterChange(filterKey, changedValue);
        };

        const applyNumberFilter = (filterKey) => {
            const row = getActiveSourceSchemaMap()[filterKey] || {};
            let val = Number(activeSourceFilters.value?.[filterKey] ?? 0);
            if (Number.isNaN(val)) val = 0;
            const min = Number(row.min ?? 0);
            const max = Number(row.max ?? 10);
            if (val < min) val = min;
            if (val > max) val = max;
            commitSourceFilterChange(filterKey, String(val));
        };

        // ===== 主网格状态 =====
        const mainGridItems = ref([]);
        const mainGridPage = ref(1);
        const mainGridTotalPages = ref(1);
        const mainGridLoading = ref(false);
        const mainGridNoMore = ref(false);
        const mainGridSentinel = ref(null);
        const mainGridScrollRoot = ref(null);
        let mainGridObserver = null;
        let mainGridObserverRetryTimer = null;
        let _mainGridGen = 0;
        const mainGridPrefetch = reactive({ pages: {} });
        const MAIN_GRID_PREFETCH_AHEAD = 2;

        const isDoubanMainGrid = () => discoverActiveSource.value === 'douban';

        const resetMainGridPrefetch = () => {
            mainGridPrefetch.pages = {};
        };

        const getProviderFilterParams = () => {
            const params = { ...(activeSourceFilters.value || {}) };
            delete params[LIBRARY_STATUS_FILTER_KEY];
            return params;
        };

        const fetchMainGridPage = async (source, page) => {
            const params = { ...getProviderFilterParams(), page };
            const res = await axios.get(`/api/discover/provider/${source}`, { params });
            return res.data || {};
        };

        const getItemTmdbId = (item = {}) => {
            if (item._tmdb_id || item.tmdb_id) return item._tmdb_id || item.tmdb_id;
            return ['tmdb', 'themoviedb'].includes(item.source) ? item.id : '';
        };

        const getItemExistenceKey = (item = {}) => {
            const tmdbId = getItemTmdbId(item);
            if (!tmdbId) return '';
            const mediaType = item.media_type || 'movie';
            return `${tmdbId}:${mediaType}`;
        };

        const markLibraryExists = async (items = []) => {
            const candidates = (items || []).filter(item => getItemTmdbId(item));
            if (!candidates.length) return;
            try {
                const payload = candidates.map(item => ({
                    tmdb_id: getItemTmdbId(item),
                    media_type: item.media_type || 'movie',
                }));
                const res = await axios.post('/api/discover/library/exists', payload);
                const results = res.data?.results || {};
                candidates.forEach(item => {
                    item.exists_in_library = !!results[getItemExistenceKey(item)];
                });
            } catch (e) {
                console.error('检查媒体库存在状态失败:', e);
            }
        };

        const applyLibraryStatusFilter = (items = []) => {
            const status = String(activeSourceFilters.value?.[LIBRARY_STATUS_FILTER_KEY] ?? '');
            if (!status) return items;
            return items.filter(item => status === 'exists' ? !!item.exists_in_library : !item.exists_in_library);
        };

        const getDisplayableMainGridItems = (items = []) => {
            return isDoubanMainGrid() ? items.filter(item => item?.poster_url) : items;
        };

        const prepareDisplayableMainGridItems = async (items = []) => {
            const displayable = getDisplayableMainGridItems(items);
            await markLibraryExists(displayable);
            return applyLibraryStatusFilter(displayable);
        };

        const mainGridPageHasMore = (data, page, rawItems) => {
            const totalPages = data.total_pages || 1;
            return !(data.has_more === false || page >= totalPages || !rawItems.length);
        };

        const pruneMainGridPrefetch = () => {
            Object.keys(mainGridPrefetch.pages).forEach(key => {
                const page = Number(key);
                const entry = mainGridPrefetch.pages[key];
                if (entry.gen !== _mainGridGen || page <= mainGridPage.value || page > mainGridPage.value + MAIN_GRID_PREFETCH_AHEAD + 1) {
                    delete mainGridPrefetch.pages[key];
                }
            });
        };

        const prefetchMainGridPage = (page, gen) => {
            if (!isDoubanMainGrid() || page < 1 || mainGridNoMore.value) return null;
            const cached = mainGridPrefetch.pages[page];
            if (cached && cached.gen === gen) {
                if (cached.ready) return Promise.resolve(cached.data);
                if (cached.loading) return cached.promise;
            }

            const source = discoverActiveSource.value;
            const entry = reactive({
                page,
                data: null,
                loading: true,
                ready: false,
                promise: null,
                gen,
            });
            mainGridPrefetch.pages[page] = entry;

            const promise = fetchMainGridPage(source, page)
                .then(async data => {
                    if (gen !== _mainGridGen || source !== discoverActiveSource.value) return null;
                    const rawItems = data.items || [];
                    const items = await prepareDisplayableMainGridItems(rawItems);
                    entry.data = { ...data, items, _rawItemCount: rawItems.length };
                    entry.ready = true;
                    return entry.data;
                })
                .catch(e => {
                    if (gen === _mainGridGen) console.error('预取发现网格失败:', e);
                    delete mainGridPrefetch.pages[page];
                    return null;
                })
                .finally(() => {
                    if (entry.gen === gen) entry.loading = false;
                    pruneMainGridPrefetch();
                });
            entry.promise = promise;
            return promise;
        };

        const prefetchMainGridAhead = (fromPage, gen) => {
            if (!isDoubanMainGrid() || mainGridNoMore.value) return;
            for (let offset = 1; offset <= MAIN_GRID_PREFETCH_AHEAD; offset += 1) {
                prefetchMainGridPage(fromPage + offset, gen);
            }
        };

        const consumeMainGridPrefetch = (page) => {
            const entry = mainGridPrefetch.pages[page];
            if (!entry || entry.gen !== _mainGridGen || !entry.ready || !entry.data) return false;
            const data = entry.data;
            const items = data.items || [];
            mainGridItems.value.push(...items);
            mainGridPage.value = page;
            mainGridTotalPages.value = data.total_pages || 1;
            mainGridNoMore.value = !mainGridPageHasMore(data, page, Array(data._rawItemCount ?? items.length).fill(null));
            delete mainGridPrefetch.pages[page];
            prefetchMainGridAhead(page, _mainGridGen);
            nextTick(() => setupMainGridObserver());
            return true;
        };


        const loadMainGrid = async (reset = true) => {
            if (reset) {
                resetMainGridPrefetch();
                mainGridItems.value = [];
                mainGridPage.value = 1;
                mainGridNoMore.value = false;
            }
            const gen = ++_mainGridGen;
            mainGridLoading.value = true;
            try {
                const source = discoverActiveSource.value;
                if (!source) {
                    mainGridItems.value = [];
                    mainGridTotalPages.value = 1;
                    mainGridNoMore.value = true;
                    return;
                }

                const page = mainGridPage.value;
                const data = await fetchMainGridPage(source, page);
                if (gen !== _mainGridGen) return;
                const rawItems = data.items || [];
                const items = await prepareDisplayableMainGridItems(rawItems);
                mainGridTotalPages.value = data.total_pages || 1;
                mainGridPage.value = page;
                mainGridNoMore.value = !mainGridPageHasMore(data, page, rawItems);
                if (reset) {
                    mainGridItems.value = items;
                } else {
                    mainGridItems.value.push(...items);
                }
                if (source === 'douban' && !mainGridNoMore.value) prefetchMainGridAhead(page, gen);
            } catch (e) {
                console.error('加载发现网格失败:', e);
            } finally {
                if (gen === _mainGridGen) {
                    mainGridLoading.value = false;
                    nextTick(() => setupMainGridObserver());
                }
            }
        };

        const loadNextMainGridPage = async () => {
            if (mainGridLoading.value || mainGridNoMore.value) return;
            const nextPage = mainGridPage.value + 1;
            if (isDoubanMainGrid()) {
                if (consumeMainGridPrefetch(nextPage)) return;
                const pending = mainGridPrefetch.pages[nextPage];
                if (pending?.loading && pending.promise) {
                    mainGridLoading.value = true;
                    await pending.promise;
                    mainGridLoading.value = false;
                    if (consumeMainGridPrefetch(nextPage)) return;
                }
            }
            mainGridPage.value = nextPage;
            await loadMainGrid(false);
        };

        const setupMainGridObserver = (attempt = 0) => {
            if (mainGridObserver) { mainGridObserver.disconnect(); mainGridObserver = null; }
            if (mainGridObserverRetryTimer) {
                clearTimeout(mainGridObserverRetryTimer);
                mainGridObserverRetryTimer = null;
            }
            if (mainGridNoMore.value) return;
            if (!mainGridSentinel.value) {
                if (attempt < 8) mainGridObserverRetryTimer = setTimeout(() => setupMainGridObserver(attempt + 1), 80);
                return;
            }
            mainGridObserver = new IntersectionObserver((entries) => {
                if (entries[0].isIntersecting && !mainGridLoading.value && !mainGridNoMore.value) {
                    loadNextMainGridPage();
                }
            }, { root: mainGridScrollRoot.value || null, rootMargin: '900px 0px', threshold: 0.01 });
            mainGridObserver.observe(mainGridSentinel.value);
        };

        const resetMainGrid = () => {
            if (mainGridObserver) { mainGridObserver.disconnect(); mainGridObserver = null; }
            if (mainGridObserverRetryTimer) { clearTimeout(mainGridObserverRetryTimer); mainGridObserverRetryTimer = null; }
            resetMainGridPrefetch();
            mainGridItems.value = [];
            mainGridPage.value = 1;
            mainGridNoMore.value = false;
            loadMainGrid(true);
        };

        watch(discoverActiveSource, async (val) => {
            if (val === 'themoviedb') {
                await fetchGenreList();
                patchTmdbGenreSchema();
            }
        });

        const getDetailSeasons = (detail = detailModal.detail) => {
            return (detail?.seasons || []).filter(season => season.season_number > 0);
        };

        const buildTmdbImageUrl = (path) => path ? `/api/discover/tmdb_img?path=${path}` : '';

        const normalizeDetailCardItem = (entry = {}, fallbackType = 'movie') => {
            const entryType = entry.media_type || fallbackType || (entry.title ? 'movie' : 'tv');
            return {
                id: entry.id,
                _tmdb_id: entry.id,
                title: entry.title || entry.name || '',
                original_title: entry.original_title || entry.original_name || '',
                year: (entry.release_date || entry.first_air_date || '').toString().slice(0, 4),
                poster_url: entry.poster_url || buildTmdbImageUrl(entry.poster_path),
                backdrop_url: entry.backdrop_url || buildTmdbImageUrl(entry.backdrop_path),
                rating: entry.vote_average || 0,
                overview: entry.overview || '',
                media_type: entryType,
                genre_ids: entry.genre_ids || [],
                source: entry.source || 'tmdb',
                subscribed: false,
                exists_in_library: false,
            };
        };

        const normalizeMediaDetail = (detail = {}, item = {}) => {
            const mediaType = detail.media_type || item.media_type || 'movie';
            const externalIds = detail.external_ids && typeof detail.external_ids === 'object' ? detail.external_ids : {};
            const imdbId = detail.imdb_id || externalIds.imdb_id || '';
            const tvdbId = detail.tvdb_id || externalIds.tvdb_id || '';
            return {
                ...detail,
                tmdb_id: detail.tmdb_id || detail.id || item._tmdb_id || item.id,
                media_type: mediaType,
                title: detail.title || detail.name || item.title || '',
                original_title: detail.original_title || detail.original_name || item.original_title || '',
                year: (detail.release_date || detail.first_air_date || item.year || '').toString().slice(0, 4),
                poster_url: item.poster_url || detail.poster_url || buildTmdbImageUrl(detail.poster_path),
                backdrop_url: detail.backdrop_url || buildTmdbImageUrl(detail.backdrop_path),
                genres: detail.genres || [],
                vote_average: detail.vote_average || item.rating || 0,
                overview: detail.overview || item.overview || '',
                imdb_id: imdbId,
                tvdb_id: tvdbId,
                external_ids: {
                    ...externalIds,
                    imdb_id: imdbId,
                    tvdb_id: tvdbId,
                },
                recommendation_items: (detail.recommendations?.results || []).map(entry => normalizeDetailCardItem(entry, mediaType)),
                similar_items: (detail.similar?.results || []).map(entry => normalizeDetailCardItem(entry, mediaType)),
            };
        };

        const getImdbLink = (detail = detailModal.detail) => {
            const imdbId = detail?.imdb_id || detail?.external_ids?.imdb_id;
            return imdbId ? `https://www.imdb.com/title/${imdbId}` : '';
        };

        const getTvdbLink = (detail = detailModal.detail) => {
            const tvdbId = detail?.tvdb_id || detail?.external_ids?.tvdb_id;
            return tvdbId ? `https://www.thetvdb.com/series/${tvdbId}` : '';
        };

        const closeDetailModalInternal = () => {
            detailModal.visible = false;
            detailModal.item = null;
            detailModal.detail = null;
            detailModal.selectedSeason = null;
            detailModal.seasonSubscribed = false;
            detailModal.castExpanded = false;
            detailModal.loading = false;
            detailHistoryActive = false;
        };

        const handleDetailPopstate = () => {
            if (suppressDetailPopstate) {
                suppressDetailPopstate = false;
                return;
            }
            if (detailModal.visible && detailHistoryActive) {
                closeDetailModalInternal();
            }
        };

        const refreshDetailSubscriptionState = async (item = detailModal.item, season = detailModal.selectedSeason) => {
            if (!mpConfig.mp_url || !detailModal.detail) {
                detailModal.subscribed = false;
                detailModal.seasonSubscribed = false;
                return;
            }
            const tmdbId = detailModal.detail.tmdb_id || item?.id;
            const mediaType = detailModal.detail.media_type || item?.media_type || 'movie';
            try {
                const requests = [
                    axios.get('/api/moviepilot/subscribe/check', { params: { tmdbid: tmdbId, type_name: mediaType } })
                ];
                if (mediaType === 'tv' && season != null) {
                    requests.push(
                        axios.get('/api/moviepilot/subscribe/check', { params: { tmdbid: tmdbId, type_name: mediaType, season } })
                    );
                }
                const [mediaRes, seasonRes] = await Promise.all(requests);
                detailModal.subscribed = !!mediaRes?.data?.subscribed;
                detailModal.seasonSubscribed = mediaType === 'tv' && season != null
                    ? !!seasonRes?.data?.subscribed
                    : !!mediaRes?.data?.subscribed;
                if (item) item.subscribed = detailModal.subscribed;
            } catch (e) {
                detailModal.subscribed = false;
                detailModal.seasonSubscribed = false;
            }
        };

        const setDetailSeason = async (seasonNumber) => {
            if (seasonNumber == null || seasonNumber === '') {
                detailModal.selectedSeason = null;
                detailModal.seasonSubscribed = false;
                return;
            }
            detailModal.selectedSeason = Number(seasonNumber);
            await refreshDetailSubscriptionState(detailModal.item, detailModal.selectedSeason);
        };

        const toggleDetailSeasonSubscription = async (seasonNumber) => {
            await setDetailSeason(seasonNumber);
            if (detailModal.seasonSubscribed) {
                await unsubscribeMedia(detailModal.item);
            } else {
                await subscribeMedia(detailModal.item);
            }
        };

        const openMediaDetail = async (item) => {
            if (!detailModal.visible || !detailHistoryActive) {
                history.pushState({ ...(history.state || {}), detailModal: true }, '');
                detailHistoryActive = true;
            }
            detailModal.item = item;
            detailModal.visible = true;
            detailModal.loading = true;
            detailModal.subscribed = false;
            detailModal.selectedSeason = null;
            detailModal.castExpanded = false;
            detailModal.seasonSubscribed = false;
            try {
                const mediaType = item.media_type || 'movie';
                let tmdbId = item._tmdb_id || item.id;

                if (item.source !== 'tmdb' && !item._tmdb_id) {
                    const searchRes = await axios.get('/api/discover/search', {
                        params: { query: item.title, type: mediaType, page: 1 }
                    });
                    const found = (searchRes.data?.items || [])[0];
                    if (found) {
                        tmdbId = found.id;
                    } else {
                        detailModal.detail = { ...item, overview: item.overview || '暂无简介' };
                        detailModal.loading = false;
                        return;
                    }
                }

                // 获取 TMDB 详情
                const res = await axios.get(`/api/discover/detail/${tmdbId}`, { params: { type: mediaType } });
                const detail = normalizeMediaDetail(res.data, item);
                detail.tmdb_id = tmdbId;
                item._tmdb_id = tmdbId;  // 缓存到 item 上，避免重复搜索
                await markLibraryExists([item, ...detail.recommendation_items, ...detail.similar_items]);
                detail.exists_in_library = !!item.exists_in_library;
                detailModal.detail = detail;

                const detailSeasons = getDetailSeasons(detail);
                if (mediaType === 'tv' && detailSeasons.length) {
                    detailModal.selectedSeason = Number(detailSeasons[0].season_number);
                }

                await refreshDetailSubscriptionState(item, detailModal.selectedSeason);
            } catch (e) {
                console.error('加载详情失败:', e);
                detailModal.detail = normalizeMediaDetail({ overview: item.overview || '暂无简介' }, item);
            } finally {
                detailModal.loading = false;
            }
        };

        const closeDetailModal = () => {
            if (detailModal.visible && detailHistoryActive) {
                suppressDetailPopstate = true;
                history.back();
                closeDetailModalInternal();
                return;
            }
            closeDetailModalInternal();
        };

        const subscribeMedia = async (item) => {
            if (!mpConfig.mp_url) {
                showToast('请先配置 MoviePilot 连接信息', 'warning');
                return;
            }
            try {
                const tmdbId = (detailModal.detail && detailModal.detail.tmdb_id) || item.id;
                const mediaType = detailModal.detail?.media_type || item.media_type || 'movie';
                const body = {
                    tmdbid: tmdbId,
                    type_name: mediaType,
                    name: detailModal.detail?.title || item.title,
                    year: detailModal.detail?.year || item.year
                };
                if (mediaType === 'tv' && detailModal.selectedSeason != null) {
                    body.season = detailModal.selectedSeason;
                }
                await axios.post('/api/moviepilot/subscribe', body);
                detailModal.subscribed = true;
                detailModal.seasonSubscribed = true;
                if (detailModal.item) detailModal.item.subscribed = true;
                showToast(mediaType === 'tv' && detailModal.selectedSeason != null ? `已订阅第 ${detailModal.selectedSeason} 季` : '订阅成功', 'success');
            } catch (e) {
                showToast('订阅失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const unsubscribeMedia = async (item) => {
            try {
                const tmdbId = (detailModal.detail && detailModal.detail.tmdb_id) || item.id;
                const mediaType = detailModal.detail?.media_type || item.media_type || 'movie';
                const params = { tmdbid: tmdbId, type_name: mediaType };
                if (mediaType === 'tv' && detailModal.selectedSeason != null) {
                    params.season = detailModal.selectedSeason;
                }
                await axios.delete('/api/moviepilot/subscribe', { params });
                detailModal.subscribed = false;
                detailModal.seasonSubscribed = false;
                if (detailModal.item) detailModal.item.subscribed = false;
                showToast(mediaType === 'tv' && detailModal.selectedSeason != null ? `已取消第 ${detailModal.selectedSeason} 季订阅` : '已取消订阅', 'success');
            } catch (e) {
                showToast('取消失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const openRowGrid = async (row) => {
            gridModal.visible = true;
            gridModal.row = row;
            gridModal.title = row.title;
            gridModal.page = 1;
            gridModal.items = [];
            gridModal.totalPages = 1;
            gridModal.loadingMore = true;
            gridModal.noMore = false;
            try {
                const params = row.source === 'douban' ? { start: 0, count: 30 } : { page: 1 };
                const res = await axios.get(row.endpoint, { params });
                const items = res.data.items || [];
                await markLibraryExists(items);
                gridModal.items = applyLibraryStatusFilter(items);
                gridModal.totalPages = res.data.total_pages || 1;
            } catch (e) {
                showToast('加载失败', 'error');
            } finally {
                gridModal.loadingMore = false;
                if (gridModal.page >= gridModal.totalPages) gridModal.noMore = true;
                // Wait for DOM update then setup intersection observer
                nextTick(() => setupGridObserver());
            }
        };

        const loadGridNextPage = async () => {
            const row = gridModal.row;
            if (!row || gridModal.loadingMore || gridModal.noMore) return;
            gridModal.page++;
            gridModal.loadingMore = true;
            try {
                const params = row.source === 'douban' ? { start: (gridModal.page - 1) * 30, count: 30 } : { page: gridModal.page };
                const res = await axios.get(row.endpoint, { params });
                const newItems = res.data.items || [];
                await markLibraryExists(newItems);
                gridModal.totalPages = res.data.total_pages || 1;
                gridModal.items.push(...applyLibraryStatusFilter(newItems));
            } catch (e) {
                gridModal.page--;
            } finally {
                gridModal.loadingMore = false;
                if (gridModal.page >= gridModal.totalPages) gridModal.noMore = true;
            }
        };

        const setupGridObserver = () => {
            if (gridObserver) { gridObserver.disconnect(); gridObserver = null; }
            if (!gridSentinel.value || gridModal.noMore) return;
            gridObserver = new IntersectionObserver((entries) => {
                if (entries[0].isIntersecting && !gridModal.loadingMore && !gridModal.noMore) {
                    loadGridNextPage();
                }
            }, { root: gridModalEl.value, threshold: 0.1 });
            gridObserver.observe(gridSentinel.value);
            // sentinel 已在可视区域内时，IntersectionObserver 不会回调，需立即检查
            if (gridModal.items.length > 0) {
                const rect = gridSentinel.value.getBoundingClientRect();
                const rootRect = gridModalEl.value?.getBoundingClientRect();
                if (rootRect && rect.top < rootRect.bottom && !gridModal.noMore) {
                    loadGridNextPage();
                }
            }
        };

        const closeGridModal = () => {
            if (gridObserver) { gridObserver.disconnect(); gridObserver = null; }
            gridModal.visible = false;
            gridModal.row = null;
            gridModal.items = [];
        };

        const searchDiscover = async (append = false) => {
            const q = discoverSearchQuery.value.trim();
            if (!q) return;
            if (!append) {
                searchPage.value = 1;
                searchMovieResults.value = [];
                searchTvResults.value = [];
            }
            discoverSearchLoading.value = true;
            discoverHasSearched.value = true;
            try {
                const page = searchPage.value;
                // 同时搜电影+剧集
                const [movieRes, tvRes] = await Promise.all([
                    axios.get('/api/discover/search', { params: { query: q, type: 'movie', page } }),
                    axios.get('/api/discover/search', { params: { query: q, type: 'tv', page } })
                ]);
                const movieItems = movieRes.data.items || [];
                const tvItems = tvRes.data.items || [];

                // 各自按标题相似度排序
                const sortFn = (a, b) => {
                    const qLower = q.toLowerCase();
                    const aTitle = (a.title || '').toLowerCase();
                    const bTitle = (b.title || '').toLowerCase();
                    const aExact = aTitle === qLower ? 0 : 1;
                    const bExact = bTitle === qLower ? 0 : 1;
                    if (aExact !== bExact) return aExact - bExact;
                    const aStarts = aTitle.startsWith(qLower) ? 0 : 1;
                    const bStarts = bTitle.startsWith(qLower) ? 0 : 1;
                    if (aStarts !== bStarts) return aStarts - bStarts;
                    const aIncludes = aTitle.includes(qLower) ? 0 : 1;
                    const bIncludes = bTitle.includes(qLower) ? 0 : 1;
                    if (aIncludes !== bIncludes) return aIncludes - bIncludes;
                    return (b.rating || 0) - (a.rating || 0);
                };
                movieItems.sort(sortFn);
                tvItems.sort(sortFn);
                await markLibraryExists([...movieItems, ...tvItems]);

                searchTotalPages.value = Math.max(movieRes.data.total_pages || 1, tvRes.data.total_pages || 1);
                const filteredMovieItems = applyLibraryStatusFilter(movieItems);
                const filteredTvItems = applyLibraryStatusFilter(tvItems);
                if (append) {
                    searchMovieResults.value.push(...filteredMovieItems);
                    searchTvResults.value.push(...filteredTvItems);
                } else {
                    searchMovieResults.value = filteredMovieItems;
                    searchTvResults.value = filteredTvItems;
                }
            } catch (e) {
                showToast('搜索失败', 'error');
            } finally {
                discoverSearchLoading.value = false;
            }
        };

        const loadMoreSearch = () => {
            if (searchPage.value < searchTotalPages.value && !discoverSearchLoading.value) {
                searchPage.value++;
                searchDiscover(true);
            }
        };

        const clearDiscoverSearch = () => {
            discoverSearchQuery.value = '';
            searchMovieResults.value = [];
            searchTvResults.value = [];
            discoverHasSearched.value = false;
            searchPage.value = 1;
            searchTotalPages.value = 1;
        };

        // ==========================================
        // 资源转存
        // ==========================================
        const transferInput = ref('');
        const transferLoading = ref(false);
        const transferResult = ref(null);
        const transferHistory = ref([]);
        const transferConfig = reactive({ dir: '', drive_index: 0 });
        const transferConfigForm = reactive({ dir: '', drive_index: 0 });
        const transferDirBrowser = reactive({ show: false, dirs: [], path: '', history: [], cid: '0' });

        const loadTransferConfig = () => {
            if (config302.drives && config302.drives.length > 0) {
                transferConfig.dir = config302.drives[0].transfer_dir || '';
                transferConfig.drive_index = 0;
                transferConfigForm.dir = transferConfig.dir;
                transferConfigForm.drive_index = 0;
            }
        };

        const browseTransferDir = async (cid = '0') => {
            try {
                const res = await axios.post('/api/strm/browse115', { cid, drive_index: 0 });
                if (res.data.status === 'ok') {
                    transferDirBrowser.dirs = res.data.dirs || [];
                    transferDirBrowser.cid = cid;
                    transferDirBrowser.show = true;
                } else {
                    showToast(res.data.message, 'error');
                }
            } catch (e) {
                showToast('浏览失败: ' + e.message, 'error');
            }
        };

        const selectTransferDir = async (dir) => {
            transferDirBrowser.history.push({ cid: transferDirBrowser.cid, path: transferDirBrowser.path });
            transferDirBrowser.path = (transferDirBrowser.path ? transferDirBrowser.path + '/' : '/') + dir.name;
            await browseTransferDir(dir.cid);
        };

        const transferDirUp = async () => {
            if (transferDirBrowser.history.length > 0) {
                const prev = transferDirBrowser.history.pop();
                transferDirBrowser.path = prev.path;
                await browseTransferDir(prev.cid);
            }
        };

        const applyTransferDir = () => {
            transferConfigForm.dir = transferDirBrowser.path;
            transferDirBrowser.show = false;
            transferDirBrowser.dirs = [];
            transferDirBrowser.path = '';
            transferDirBrowser.history = [];
        };

        const saveTransferConfig = async () => {
            if (config302.drives && config302.drives.length > 0) {
                config302.drives[0].transfer_dir = transferConfigForm.dir;
                config302.drives[0].transfer_drive_index = 0;
            }
            try {
                const payload = build302Payload();
                await axios.post('/api/config_302/save', payload);
                transferConfig.dir = transferConfigForm.dir;
                transferConfig.drive_index = 0;
                transferConfigForm.drive_index = 0;
                showToast('转存配置已保存', 'success');
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const manualTransfer = async () => {
            const link = transferInput.value.trim();
            if (!link) return;
            transferLoading.value = true;
            transferResult.value = null;
            try {
                const res = await axios.post('/api/transfer/manual', { link });
                transferResult.value = res.data;
                transferInput.value = '';
                loadTransferHistory();
            } catch (e) {
                transferResult.value = { success: false, message: e.response?.data?.detail || '转存请求失败' };
            } finally {
                transferLoading.value = false;
            }
        };

        const loadTransferHistory = async () => {
            try {
                const res = await axios.get('/api/transfer/history');
                transferHistory.value = res.data || [];
            } catch { /* ignore */ }
        };

        const clearTransferHistory = async () => {
            if (!confirm('确定要清空所有转存记录吗？')) return;
            try {
                await axios.delete('/api/transfer/history');
                transferHistory.value = [];
            } catch { /* ignore */ }
        };

        // tab 切换时自动加载转存数据
        watch(tab, (v) => {
            if (v === 'resource_transfer') {
                loadTransferConfig();
                loadTransferHistory();
            }
        });

        // 302 配置加载后同步转存配置
        watch(() => config302.drives, () => loadTransferConfig(), { deep: true });

        return {
            tab, pageTitle, servers, fontList, presetList, layoutGroups, config,
            manualServerIdx, currentManualServer, currentLibId, previewImage, loading, applying, selectedPresetIdx, currentPresetFile,
            previewServerIdx, libraryCards, loadingCovers, suiteList, newSuiteName, creatingBackup, viewingSuite, viewingSuiteImages, selectedRestoreIds,
            taskName, taskCron, taskEngine, taskPreset, runningTask, runTaskNow, createTask, taskList, deleteTask,
            translationList, fetchTranslations, saveTranslations, addTransRow, removeTransRow, 
            fetchLibraryCovers, createSuiteBackup, deleteSuite, viewSuite, closeSuiteView, getLibraryName, toggleRestoreSelect, restoreSelected, restoreAll,
            addServer, removeServer, testConnection, saveAllConfigs, test302EmbyConnection, fetchLibs, onManualServerChange, onLibChange, preview, apply, saveAsNewPreset, overwritePreset, loadPreset, deleteTemplate, toggleServerExpand, isLibSelected, toggleLibSelection, uploadFont, deleteFont, logout,
            customAssets, handleBgUpload, handlePosterUpload,
            modalVisible, modalTitle, modalImages, loadingModal, modalType, modalSource, searchQuery, searchResults, modalStep,
            openPoolModal, doSearchInModal, fetchItemImagesInModal, isSelectedInModal, selectInModal, closeModal, selectFromSearchResult,
            previewCustom, applyCustom,
            isAllSelected, toggleSelectAll, manualMode, taskMode, forceReset,
            layoutList, fetchLayouts, fetchLayoutAndPresets, filteredPresets,
            currentSchema, accountForm, updateAccount, updatingAccount,
            transServerIdx, loadTransFromLib, editingTaskId, editTask, cancelEdit, runSavedTask,
            tasksState, toggleTaskLog, accordions, toggleAccordion, showCreateTask, clearLogs,
            dashboardStats, dashboardRecentItems, dashboardRecentPlaybacks, dashboardMediaStats,
            dashboardDeviceMetrics, dashboardDeviceMetricsLoaded, dashboardDeviceMetricsPulse, dashboardDeviceMetricCards,
            dashboard115Account, dashboard115Loaded, handleDashboard115CardClick,
            dashboardCovers, wallRows, wallReady, dashboardOverviewLoading, initDashboard, fetchDashboardOverview, formatDashboardPlayedAt, getDeviceMetricState, formatDevicePercent, formatDeviceMemory, openDashboardLibrary, openDashboardItem, ensureDashboardServerId,
            toasts, showToast,
            confirmState, handleConfirm,
            selectState, handleSelect, closeSelectDialog,
            numberDialogState, handleNumberDialog, closeNumberDialog,
            projectVersion, currentUsername, stopTask,
            upgradeStatus, fetchUpgradeStatus, checkUpgrade, startUpgrade,
            cleanup115Tasks, cleanup115Form, cleanup115EditingId, showCreate115Cleanup, cleanup115Browser,
            fetch115CleanupTasks, openCreate115Cleanup, reset115CleanupForm, save115CleanupTask, edit115CleanupTask,
            delete115CleanupTask, toggle115CleanupTask, run115CleanupTask, open115CleanupBrowser, select115CleanupDir,
            cleanup115Up, addCurrent115CleanupFolder, remove115CleanupFolder,

            // [新增] 真实后台日志
            consoleLogState, filteredLogs, logVirtualState, logContainerRef, onLogScroll, copyLogLine,
            openConsoleLog, closeConsoleLog, reconnectConsoleLogStream, changeConsoleLogLevel, changeConsoleLogCategory, toggleConsoleAutoScroll, clearSystemLogs,

            // [新增] RSS 订阅
            rssConfig, rssForm, rssTasks,
            saveRssConfig, createRssTask, runRssTask, deleteRssTask,

            // [新增] Webhook 
            webhookConfig, webhookUrl, fetchWebhookConfig, saveWebhookConfig, copyWebhookUrl, toggleWebhookStatus,

            // [新增] 直接上传封面
            directUploadImg, handleDirectUpload, applyDirectUpload,

            // [新增] 302 配置
            config302, save302Config, saveEmbyConfig, toggle302Switch, importEmbyInfo, add302Drive, remove302Drive,
            add302Emby, remove302Emby,
            test115Cookie, manualCleanup115,
            qrcode115State, open115QrLogin, close115QrLogin, create115QrCode,

            // [修复] 全局变量及方法
            globalConfig, saveGlobalSettings, toggleDebugMode,

            // [新增] 影巢配置
            hdhiveConfig, hdhiveChecking, fetchHdhiveConfig,
            addHdhiveAccount, removeHdhiveAccount, testHdhiveAccount,
            loginHdhive, checkinHdhive, gamblerCheckinHdhive, checkinAllHdhive, saveHdhiveAccount,
            toggleHdhiveCheckin,
            refreshHdhiveUserInfo,
            refreshHdhiveUsage,

            // [新增] 微信通知配置
            wechatNotifyConfig, wechatNotifyTesting, wechatNotifySending, wechatNotifySaving, wechatTemplateTesting,
            fetchWechatNotifyConfig, saveWechatNotifyConfig, testWechatNotify, sendWechatTestMsg, testWechatTemplate,
            notificationTypes, notificationChannels, templateLabels, toggleNotifyType, templateVars, resetWechatTemplate, resetTelegramTemplate, wrapVar,

            // [新增] Telegram通知配置
            telegramNotifyConfig, telegramNotifyTesting, telegramNotifySending, telegramNotifySaving, telegramTemplateTesting,
            fetchTelegramNotifyConfig, saveTelegramNotifyConfig, testTelegramNotify, sendTelegramTestMsg, testTelegramTemplate,
            toggleTelegramNotifyType,
            
            // 新增移动端变量
            mobileMenuVisible, toggleMobileMenu, selectMobileTab,
            navTrack, indicatorStyle,
            editingRssTaskId, editRssTask, cancelRssEdit,
            
            // 新增开关控制方法
            toggleTaskStatus, showCreateRss,
            toggleRssStatus,
            showRssConfig,
            sidebarHover,
            isImmersiveMode,
            toggleSidebar,
            callbackUrl,

            // macOS Dock 面板管理
            isMobile, openPanels, focusedPanel, showSettingsDrawer, showCoverDrawer, showStorageDrawer, showToolboxDrawer, settingsDrawerStyle, coverDrawerStyle, storageDrawerStyle, toolboxDrawerStyle, showSpotlight,
            spotlightQuery, spotlightFocusIndex, dockHoverIndex, spotlightInputRef,
            theme, toggleTheme,
            dockItems, storageItems, coverItems, toolboxItems, settingsItems, allSearchItems,
            getPanelIcon, getPanelLabel, togglePanel, closePanel, focusPanel, goHome,
            toggleSettingsDrawer, toggleCoverDrawer, toggleStorageDrawer, toggleToolboxDrawer, openFromSettings,
            showSpotlightPanel, spotlightResults, jumpToItem,
            selectSpotlightResult, spotlightUp, spotlightDown,

            // [新增] MoviePilot 配置
            mpConfig, mpTesting, mpTestResult,
            fetchMpConfig, saveMpConfig, testMpConnection,

            // [新增] 发现推荐页
            detailModal, openMediaDetail, closeDetailModal,
            subscribeMedia, unsubscribeMedia, getImdbLink, getTvdbLink,
            gridModal, gridModalEl, gridSentinel, openRowGrid, closeGridModal,
            searchMovieResults, searchTvResults, discoverSearchLoading,
            discoverHasSearched, searchPage, searchTotalPages,
            loadMoreSearch,
            genreList,
            discoverSourceTabs, discoverActiveSource,
            discoverSourceSupported, discoverEmptyText,
            activeSourceDef, activeSourceSchema, activeSourceFilters, getVisibleFilterRows,
            switchDiscoverSource, updateSourceFilter, toggleSourceChip, applyNumberFilter,
            loadDiscoverSources,
            mainGridItems, mainGridPage, mainGridTotalPages, mainGridLoading, mainGridNoMore,
            mainGridSentinel, mainGridScrollRoot, loadMainGrid, resetMainGrid,

            // [新增] 资源转存
            transferInput, transferLoading, transferResult, transferHistory, transferConfig, transferConfigForm, transferDirBrowser, browseTransferDir, selectTransferDir, transferDirUp, applyTransferDir, saveTransferConfig, clearTransferHistory,
            manualTransfer, loadTransferHistory,

            // [新增] STRM 同步
            strmConfig, strmProgress, strmBrowser, localBrowser,
            fetchStrmConfig, saveStrmConfig, addStrmTask, removeStrmTask,
            startStrmSync, stopStrmSync,
            browseStrmDir, selectStrmDir, browseStrmDirUp, applyBrowsePath,
            browseLocalDir, selectLocalDir, applyLocalBrowsePath,
            videoExtOptions, audioExtOptions, imageExtOptions, dataExtOptions, toggleExt, hasExt,

            // [新增] 媒体整理
            mediaOrganizeConfig,
            organizeForm, organizeLoading, organizeResult, organizeProgress,
            runOrganize, cancelOrganize,
            categoryRulesEditor, categoryRulesSaving, ruleListEl,
            subClassify, subClassifyVars, subClassifyVarExamples, subClassifyBaseExamples, subClassifyPreviewSegments, subClassifyToggleLevel, embyLibCount, embyLibLevelOptions,
            embyCacheRefreshing, refreshEmbyCache,
            onLevelDragStart, onLevelDragOver, onLevelDrop, onLevelDragEnd,
            fetchCategoryRules, saveCategoryRules, saveSubClassify, addRule, removeRule,
            addCondition, removeCondition, resetCategoryRules,
            onRuleDragStart, onRuleDragOver, onRuleDrop, onRuleDragEnd,
            orgSourceBrowser, orgTargetBrowser, orgFailedBrowser,
            fetchMediaOrganizeConfig, saveMediaOrganizeConfig, toggleAutoSyncStrm, toggleFilenameOnlyMode, toggleFfprobeMode, toggleFullFfprobeMode, toggleWashByEquivalentSize,
            browseOrganizeSource, selectOrgSourceDir, orgSourceUp, applyOrgSourcePath,
            browseOrganizeTarget, selectOrgTargetDir, orgTargetUp, applyOrgTargetPath,
            browseOrganizeFailed, selectOrgFailedDir, orgFailedUp, applyOrgFailedPath,
            // 重命名模板编辑器
            movieFormatRef, movieFolderFormatRef, tvFolderFormatRef, tvEpisodeFormatRef,
            movieFolderFormatDisplay, tvFolderFormatDisplay, movieFormatDisplay, tvEpisodeFormatDisplay,
            moviePreviewName, movieFolderPreviewName, tvFolderPreviewName, tvEpisodePreviewName,
            insertToken, resetMovieFormat, resetTvFormat,
        }
        
    }
}).mount('#app')