import axios from 'axios';
import { createApp, reactive, ref, computed, onMounted, watch, onUnmounted, nextTick } from 'vue';
import { FeedbackDialogs } from './components/FeedbackDialogs';
import { ToastStack } from './components/ToastStack';
import { useFeedbackDialogs } from './composables/useFeedbackDialogs';
import { useConsoleLogs } from './composables/useConsoleLogs';
import { useTaskProgress } from './composables/useTaskProgress';
import { useShellNavigation } from './composables/useShellNavigation';
import { useSystemHealth } from './composables/useSystemHealth';
import { useNetworkConnectivity } from './composables/useNetworkConnectivity';
import { allSearchItems, allValidTabs, coverItems, dockItems, getPanelIcon, getPanelLabel, settingsItems, storageItems, toolboxItems } from './modules/navigationConfig';
import { useDockerManager } from './pages/docker/useDockerManager';
import { useWebhookConfig } from './pages/webhook/useWebhookConfig';
import { useRssTasks } from './pages/rss/useRssTasks';
import { useRealLibrary } from './pages/realLibrary/useRealLibrary';
import { useEmbyTasks } from './pages/embyTasks/useEmbyTasks';
import { useResourceTransfer } from './pages/transfer/useResourceTransfer';
import { useMoviePilotConfig } from './pages/moviepilot/useMoviePilotConfig';
import { useHdhiveConfig } from './pages/hdhive/useHdhiveConfig';
import { useForwardHdhive } from './pages/forward/useForwardHdhive';
import { useNotificationSettings } from './pages/notifications/useNotificationSettings';
import { useConfig302 } from './pages/config302/useConfig302';
import { useStrmConfig } from './pages/strm/useStrmConfig';
import { useDrive115Maintenance } from './pages/drive115/useDrive115Maintenance';
import { useMediaOrganize } from './pages/mediaOrganize/useMediaOrganize';
import { useOrganizeHistory } from './pages/organizeHistory/useOrganizeHistory';
import { useDiscover } from './pages/discover/useDiscover';
import { useDashboardDeviceMetrics } from './pages/dashboard/useDashboardDeviceMetrics';
import { useDashboardOverview } from './pages/dashboard/useDashboardOverview';
import { useCoverBackups } from './pages/cover/useCoverBackups';
import { useCoverResources } from './pages/cover/useCoverResources';
import './style.css';

createApp({
    components: {
        FeedbackDialogs,
        ToastStack,
    },
    setup() {
        const ACTIVE_TAB_STORAGE_KEY = 'chillposter-active-tab';
        const normalizeTab = (value) => (value === 'strm_generate' ? 'media_organize' : value);
        const getInitialTab = () => {
            try {
                localStorage.removeItem(ACTIVE_TAB_STORAGE_KEY);
                const hashTab = decodeURIComponent((window.location.hash || '').replace(/^#/, '')).trim();
                const normalizedHashTab = normalizeTab(hashTab);
                if (hashTab && normalizedHashTab !== hashTab) {
                    const cleanUrl = `${window.location.pathname}${window.location.search}`;
                    window.history.replaceState(null, '', `${cleanUrl}#${encodeURIComponent(normalizedHashTab)}`);
                }
                return allValidTabs.has(normalizedHashTab) ? normalizedHashTab : 'dashboard';
            } catch (_) {}
            return 'dashboard';
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
        const layoutList = ref([]); 
        const presetList = ref([]);
        const layoutGroups = ref([]); 
        const showRssConfig = ref(false);
        const {
            sidebarHover,
            isImmersiveMode,
            toggleSidebar,
            isMobile,
            openPanels,
            focusedPanel,
            showSettingsDrawer,
            showCoverDrawer,
            showStorageDrawer,
            showToolboxDrawer,
            showSpotlight,
            spotlightQuery,
            spotlightFocusIndex,
            dockHoverIndex,
            spotlightInputRef,
            theme,
            settingsDrawerStyle,
            coverDrawerStyle,
            storageDrawerStyle,
            toolboxDrawerStyle,
            toggleTheme,
            togglePanel,
            closePanel,
            focusPanel,
            goHome,
            closeDockDrawers,
            closeDesktopOverlays,
            toggleSettingsDrawer,
            toggleCoverDrawer,
            toggleStorageDrawer,
            toggleToolboxDrawer,
            openFromSettings,
            showSpotlightPanel,
            spotlightResults,
            jumpToItem,
            selectSpotlightResult,
            spotlightUp,
            spotlightDown,
            handleKeydown,
            handleResize,
        } = useShellNavigation({
            tab,
            allSearchItems,
            onResize: () => {
                if (dashboardCovers.value.length > 0) {
                    splitIntoRows();
                }
            },
        });
        
        // ==========================================
        // 0. 版本号与当前用户
        // ==========================================
        const projectVersion = ref('vdev');
        const currentUsername = ref(localStorage.getItem('username') || 'Administrator');
        const {
            toasts,
            showToast,
            confirmState,
            handleConfirm,
            showConfirm,
            selectState,
            handleSelect,
            closeSelectDialog,
            showSelectDialog,
            numberDialogState,
            handleNumberDialog,
            closeNumberDialog,
            showNumberDialog,
        } = useFeedbackDialogs();

        const {
            systemHealth,
            systemHealthHeadline,
            systemHealthMetaText,
            openSystemHealth,
            closeSystemHealth,
            runSystemHealthCheck,
            getSystemHealthStatusLabel,
            getSystemHealthStatusIcon,
        } = useSystemHealth({ showToast });

        const {
            networkConnectivity,
            networkConnectivityHeadline,
            networkConnectivityMetaText,
            openNetworkConnectivity,
            closeNetworkConnectivity,
            runNetworkConnectivityTest,
            getNetworkStatusLabel,
            getNetworkStatusIcon,
        } = useNetworkConnectivity({ showToast });

        const {
            upgradeStatus,
            loadProjectVersion,
            fetchUpgradeStatus,
            checkUpgrade,
            startUpgrade,
            dockerManager,
            filteredDockerContainers,
            filteredDockerImages,
            dockerUpdateCount,
            dockerContainerStats,
            dockerImageStats,
            fetchDockerStatus,
            fetchDockerContainers,
            fetchDockerImages,
            refreshDockerManager,
            checkDockerUpdates,
            startDockerSilentRefresh,
            stopDockerSilentRefresh,
            stopDockerUpdatePolling,
            setDockerContainerFilter,
            runDockerContainerAction,
            toggleDockerAutoUpdate,
            openDockerScheduledRestartDialog,
            closeDockerScheduledRestartDialog,
            saveDockerScheduledRestartDialog,
            disableDockerScheduledRestart,
            openDockerLogs,
            closeDockerLogs,
            pullDockerImage,
            deleteDockerImage,
            pruneUnusedDockerImages,
            pruneUntaggedDockerImages,
            setDockerImageFilter,
            closeDockerUpdateDialog,
            openDockerVersionDialog,
            closeDockerVersionDialog,
            saveDockerVersionDialog,
            formatDockerBytes,
            formatDockerDate,
            isDockerImageUntagged,
            dockerImageTagLabel,
        } = useDockerManager({ tab, projectVersion, showToast, showConfirm });

        const {
            embyTasksState,
            runningEmbyTasks,
            hasEmbyTaskGroups,
            fetchEmbyTasks,
            refreshEmbyTasks,
            runEmbyTask,
            stopEmbyTask,
            openEmbyTriggerDialog,
            closeEmbyTriggerDialog,
            addEmbyTriggerDraft,
            removeEmbyTrigger,
            saveEmbyTriggers,
            toggleEmbyTaskNotify,
            toggleEmbyTaskRunningDropdown,
            startEmbyTaskPolling,
            stopEmbyTaskPolling,
            formatEmbyTaskProgress,
            triggerTypeOptions,
            weekDayOptions,
        } = useEmbyTasks({ tab, showToast });

        const {
            consoleLogState,
            logCategoryOptions,
            filteredLogs,
            logVirtualState,
            logContainerRef,
            onLogScroll,
            copyLogLine,
            openConsoleLog,
            closeConsoleLog,
            reconnectConsoleLogStream,
            changeConsoleLogLevel,
            changeConsoleLogCategory,
            toggleConsoleAutoScroll,
            clearSystemLogs,
            stopConsoleLogStream,
        } = useConsoleLogs({ showToast });

        const {
            dashboardDeviceMetrics,
            dashboardDeviceMetricsLoaded,
            dashboardDeviceMetricCards,
            startDashboardDeviceMetricsPolling,
            stopDashboardDeviceMetricsPolling,
            getDeviceMetricState,
            formatDevicePercent,
            formatDeviceMemory,
        } = useDashboardDeviceMetrics({ tab });

        const {
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
        } = useDashboardOverview({
            tab,
            servers,
            isMobile,
            syncServersFrom302,
            showToast,
            showConfirm,
            startDashboardDeviceMetricsPolling,
            stopDashboardDeviceMetricsPolling,
        });

        const {
            rssConfig,
            rssForm,
            rssTasks,
            showCreateRss,
            editingRssTaskId,
            fetchRssData,
            saveRssConfig,
            editRssTask,
            cancelRssEdit,
            createRssTask,
            runRssTask,
            deleteRssTask,
        } = useRssTasks({ showToast, showConfirm });

        const {
            realLibraryConfig,
            realLibraryForm,
            realLibraryTasks,
            realLibraryEditingId,
            showCreateRealLibrary,
            realLibraryTesting,
            realLibraryPathChecking,
            realLibraryTestResult,
            realLibraryPathResult,
            fetchRealLibraryData,
            saveRealLibraryConfig,
            testRealLibraryEmby,
            validateRealLibraryPaths,
            saveRealLibraryTask,
            editRealLibraryTask,
            cancelRealLibraryEdit,
            runRealLibraryTask,
            toggleRealLibraryTask,
            deleteRealLibraryTask,
        } = useRealLibrary({ showToast, showConfirm });

        const {
            webhookConfig,
            webhookUrl,
            fetchWebhookConfig,
            saveWebhookConfig,
            copyWebhookUrl,
            toggleWebhookStatus,
        } = useWebhookConfig({ presetList, validateSelections: () => validateSelections(), showToast });

        const {
            mpConfig,
            mpTesting,
            mpTestResult,
            fetchMpConfig,
            saveMpConfig,
            testMpConnection,
        } = useMoviePilotConfig({ showToast });

        const {
            forwardHdhiveConfig,
            forwardHdhiveSaving,
            forwardHdhiveTesting,
            forwardHdhiveTestForm,
            forwardHdhiveTestResult,
            fetchForwardHdhiveConfig,
            saveForwardHdhiveConfig,
            copyForwardHdhiveWidgetUrl,
            refreshForwardHdhiveToken,
            testForwardHdhiveResources,
        } = useForwardHdhive({ showToast });

        const {
            tasksState,
            hydrateTaskLogs,
            addLog,
            clearLogs,
            clearTaskHistoryCategory,
            stopTask,
            startPolling,
            stopPolling,
            toggleTaskLog,
            openTaskCategoryLog,
            closeTaskCategoryLog,
        } = useTaskProgress({
            showToast,
            showConfirm,
            onRssFinished: () => refreshAllLibraries(),
            onBackupFinished: () => fetchSuites(),
        });

        const dashboardTaskCategoryConfig = [
            { key: 'media_organize', title: '媒体整理', desc: '扫描整理媒体目录', icon: 'fa-folder-tree', action: '手动整理' },
            { key: 'strm', title: 'STRM 同步', desc: '运行 STRM 同步任务', icon: 'fa-file-code', action: '运行同步' },
            { key: 'cover', title: '封面任务', desc: '封面生成、应用与备份', icon: 'fa-images', action: '运行任务' },
            { key: 'rss', title: 'RSS 同步', desc: '订阅任务同步记录', icon: 'fa-rss', action: '查看' },
            { key: 'real_library', title: '独立真实库', desc: '独立 RSS 真实库同步记录', icon: 'fa-hard-drive', action: '查看' },
            { key: 'system', title: '系统任务', desc: '升级、健康检查等后台任务', icon: 'fa-shield-heart', action: '查看' },
        ];
        const activeTaskEntries = computed(() => Object.entries(tasksState.activeTasks || {}));
        const getTaskCategoryKey = (task = {}, runId = '') => {
            const taskType = String(task.task_type || '').trim();
            const name = String(task.name || '').trim();
            const id = String(runId || '').trim();
            if (taskType === 'media_organize' || id.startsWith('organize_') || name.includes('整理')) return 'media_organize';
            if (taskType === 'strm' || name.includes('STRM')) return 'strm';
            if (taskType === 'rss' || id.startsWith('rss_run_') || name.startsWith('RSS')) return 'rss';
            if (taskType === 'real_library' || id.startsWith('real_library_run_') || name.startsWith('真实库')) return 'real_library';
            if (taskType === 'upgrade' || name.includes('升级')) return 'system';
            if (taskType === 'backup' || taskType === 'preset_task' || name.includes('封面') || name.includes('备份') || name.startsWith('任务:')) return 'cover';
            return 'system';
        };
        const getTaskStatusLabel = (status) => {
            if (status === 'running') return '运行中';
            if (status === 'finished') return '成功';
            if (status === 'stopped') return '已取消';
            if (status === 'interrupted') return '已中断';
            if (status === 'error') return '失败';
            return '空闲';
        };
        const getTaskStatusClass = (status) => {
            if (status === 'running') return 'running';
            if (status === 'finished') return 'success';
            if (status === 'stopped') return 'warning';
            if (status === 'interrupted') return 'warning';
            if (status === 'error') return 'error';
            return 'idle';
        };
        const formatActiveTaskTitle = (task = {}) => {
            const detail = task.detail || {};
            if (task.task_type === 'media_organize' && detail.processed !== undefined && detail.total !== undefined) {
                return `已处理: ${detail.processed}/${detail.total}`;
            }
            return task.name || '任务';
        };
        const dashboardTaskCategories = computed(() => dashboardTaskCategoryConfig.map(config => {
            const active = activeTaskEntries.value
                .filter(([id, task]) => getTaskCategoryKey(task, id) === config.key)
                .map(([id, task]) => ({ id, ...task }));
            const history = tasksState.taskHistory.filter(item => item.category === config.key);
            const latest = history[0] || null;
            const running = active.find(item => item.status === 'running') || null;
            const current = running || active[0] || null;
            const status = running ? 'running' : (current?.status || latest?.status || 'idle');
            return {
                ...config,
                running,
                current,
                status,
                statusLabel: getTaskStatusLabel(status),
                statusClass: getTaskStatusClass(status),
                historyCount: history.length,
                latest,
                summary: current?.name || latest?.summary || '暂无运行记录',
                time: running ? '实时' : (latest?.time || ''),
            };
        }));
        const selectedTaskCategory = computed(() => dashboardTaskCategories.value.find(item => item.key === tasksState.selectedTaskCategory) || dashboardTaskCategoryConfig[0]);
        const selectedTaskCategoryHistory = computed(() => tasksState.taskHistory.filter(item => item.category === tasksState.selectedTaskCategory));
        const getRealLibraryTaskState = (taskId) => {
            const prefix = `real_library_run_${taskId}_`;
            const entries = activeTaskEntries.value
                .filter(([id, task]) => id.startsWith(prefix) || (task.task_type === 'real_library' && id.includes(`_${taskId}_`)))
                .map(([id, task]) => ({ id, ...task }))
                .sort((a, b) => Number(b.updated_at || b.completed_at || 0) - Number(a.updated_at || a.completed_at || 0));
            return entries.find(item => item.status === 'running') || entries[0] || null;
        };
        const isRealLibraryTaskRunning = (taskId) => getRealLibraryTaskState(taskId)?.status === 'running';

        // ==========================================
        // 3. 任务与核心逻辑
        // ==========================================
        
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
        const globalConfigLoaded = ref(false);
        const sensitiveVisibility = reactive({
            driveRecycleCode: false,
            embyApiKey: false,
            mpPassword: false,
            tmdbKey: false,
            doubanCookie: false,
            accountOldPassword: false,
            accountNewPassword: false,
            rapidCookie: {},
            rapidRecycleCode: {}
        });

        const fetchGlobalSettings = async () => {
            try {
                const res = await axios.get('/api/load');
                const data = res.data || {};
                globalConfig.proxy_url = data.proxy_url || '';
                globalConfig.tmdb_key = data.tmdb_key || '';
                globalConfig.douban_cookie = data.douban_cookie || '';
                globalConfig.app_public_base_url = data.app_public_base_url || '';
                if (data.log_level) {
                    globalConfig.log_level = String(data.log_level).toUpperCase();
                }
                globalConfig.debug_mode = globalConfig.log_level === 'DEBUG';
                globalConfigLoaded.value = true;
                return true;
            } catch {
                return false;
            }
        };

        const {
            config302,
            hasPrimary115Cookie,
            needs115Setup,
            standardTopologyEnabled,
            open115ConfigPanel,
            notify115SetupRequired,
            qrcode115State,
            manual115CookieState,
            add302Drive,
            remove302Drive,
            add302Emby,
            remove302Emby,
            test115Cookie,
            close115QrLogin,
            create115QrCode,
            open115QrLogin,
            open115CkTool,
            copy115FetchedCookie,
            openManual115CookieDialog,
            closeManual115CookieDialog,
            saveManual115Cookie,
            manualCleanup115,
            build302Payload,
            fetch302Config,
            save302Config,
            saveEmbyConfig,
            toggle302Switch,
        } = useConfig302({
            tab,
            isMobile,
            jumpToItem,
            closeMobileMenu: () => { mobileMenuVisible.value = false; },
            syncServersFrom302,
            showToast,
            showConfirm,
            refreshLinkedConfigs: () => {
                fetchMediaOrganizeConfig();
                fetchStrmConfig();
            },
        });

        const openToolboxItem = (item) => {
            if (item?.action === 'open115CkTool' || item?.id === 'drive115_ck_tool') {
                closeDockDrawers();
                open115CkTool();
                return;
            }
            openFromSettings(item.id);
        };

        const {
            transferInput,
            transferLoading,
            transferResult,
            transferHistory,
            transferHistoryStats,
            transferPage,
            transferPageSize,
            transferPageCount,
            transferHistoryRange,
            paginatedTransferHistory,
            transferConfig,
            transferConfigForm,
            transferDirLabel,
            transferDirBrowser,
            getTransferSourceClass,
            getTransferSourceText,
            getTransferSourceDetail,
            getTransferStatusClass,
            setTransferPage,
            loadTransferConfig,
            loadTransferHistory,
            browseTransferDir,
            selectTransferDir,
            transferDirUp,
            applyTransferDir,
            saveTransferConfig,
            clearTransferHistory,
            manualTransfer,
        } = useResourceTransfer({ tab, config302, build302Payload: () => build302Payload(), showToast });

        const {
            strmConfig,
            strmProgress,
            strmBrowser,
            localBrowser,
            fetchStrmConfig,
            saveStrmConfig,
            addStrmTask,
            removeStrmTask,
            startStrmSync,
            stopStrmSync,
            browseStrmDir,
            selectStrmDir,
            browseStrmDirUp,
            applyBrowsePath,
            browseLocalDir,
            selectLocalDir,
            applyLocalBrowsePath,
            videoExtOptions,
            audioExtOptions,
            imageExtOptions,
            dataExtOptions,
            toggleExt,
            hasExt,
        } = useStrmConfig({ needs115Setup, notify115SetupRequired, showToast, showConfirm });

        const {
            mediaOrganizeConfig,
            organizeForm,
            organizeLoading,
            organizeResult,
            organizeProgress,
            runOrganize,
            cancelOrganize,
            identifyTest,
            openIdentifyTest,
            closeIdentifyTest,
            runIdentifyTest,
            categoryRulesEditor,
            categoryRulesSaving,
            ruleListEl,
            subClassify,
            subClassifyVars,
            subClassifyVarExamples,
            subClassifyBaseExamples,
            subClassifyPreviewSegments,
            subClassifyToggleLevel,
            embyLibCount,
            embyLibLevelOptions,
            embyCacheRefreshing,
            refreshEmbyCache,
            onLevelDragStart,
            onLevelDragOver,
            onLevelDrop,
            onLevelDragEnd,
            fetchCategoryRules,
            saveCategoryRules,
            saveSubClassify,
            addRule,
            removeRule,
            addCondition,
            removeCondition,
            resetCategoryRules,
            onRuleDragStart,
            onRuleDragOver,
            onRuleDrop,
            onRuleDragEnd,
            orgSourceBrowser,
            orgTargetBrowser,
            orgFailedBrowser,
            monitorDirBrowser,
            monitorDirsSaving,
            fetchMediaOrganizeConfig,
            saveMediaOrganizeConfig,
            saveMonitorDirs,
            restoreRunningOrganizeTask,
            toggleAutoSyncStrm,
            toggleEmbyScrapers,
            toggleFilenameOnlyMode,
            toggleFfprobeMode,
            toggleFullFfprobeMode,
            toggleWashByEquivalentSize,
            browseOrganizeSource,
            selectOrgSourceDir,
            orgSourceUp,
            applyOrgSourcePath,
            browseOrganizeTarget,
            selectOrgTargetDir,
            orgTargetUp,
            applyOrgTargetPath,
            browseOrganizeFailed,
            selectOrgFailedDir,
            orgFailedUp,
            applyOrgFailedPath,
            openMonitorDirBrowser,
            selectMonitorDir,
            monitorDirUp,
            addCurrentMonitorDir,
            removeMonitorDir,
            movieFormatRef,
            movieFolderFormatRef,
            tvFolderFormatRef,
            tvEpisodeFormatRef,
            movieFolderFormatDisplay,
            tvFolderFormatDisplay,
            movieFormatDisplay,
            tvEpisodeFormatDisplay,
            moviePreviewName,
            movieFolderPreviewName,
            tvFolderPreviewName,
            tvEpisodePreviewName,
            insertToken,
            resetMovieFormat,
            resetTvFormat,
        } = useMediaOrganize({ tab, needs115Setup, notify115SetupRequired, showToast, showNumberDialog });

        const runDashboardTaskCategory = async (category) => {
            if (category === 'media_organize') {
                await runOrganize();
                return;
            }
            if (category === 'strm') {
                if (!Array.isArray(strmConfig.sync_tasks) || strmConfig.sync_tasks.length === 0) {
                    showToast('暂无 STRM 同步任务，请先配置同步任务', 'warning');
                    return;
                }
                await startStrmSync(0, 'full');
                return;
            }
            if (category === 'cover') {
                tab.value = 'auto';
                showToast('已打开封面任务页面，可选择任务运行', 'info');
                return;
            }
            if (category === 'rss') {
                tab.value = 'rss';
                showToast('已打开 RSS 任务页面，可选择订阅任务运行', 'info');
                return;
            }
            openTaskCategoryLog(category);
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


        const {
            hdhiveConfig,
            hdhiveChecking,
            fetchHdhiveConfig,
            saveHdhiveAccount,
            toggleHdhiveCheckin,
            addHdhiveAccount,
            removeHdhiveAccount,
            testHdhiveAccount,
            loginHdhive,
            checkinHdhive,
            gamblerCheckinHdhive,
            checkinAllHdhive,
            refreshHdhiveUserInfo,
            refreshHdhiveUsage,
        } = useHdhiveConfig({ showToast, showConfirm });

        const {
            previewServerIdx,
            libraryCards,
            loadingCovers,
            suiteList,
            newSuiteName,
            creatingBackup,
            viewingSuite,
            viewingSuiteImages,
            selectedRestoreIds,
            fetchLibraryCovers,
            fetchSuites,
            createSuiteBackup,
            deleteSuite,
            viewSuite,
            closeSuiteView,
            getLibraryName,
            toggleRestoreSelect,
            restoreSelected,
            restoreAll,
        } = useCoverBackups({ servers, tasksState, showToast, showConfirm, fetchDashboardStats });

        const {
            fontList,
            translationList,
            transServerIdx,
            loadTransFromLib,
            fetchTranslations,
            saveTranslations,
            addTransRow,
            removeTransRow,
            fetchFonts,
            uploadFont,
            deleteFont,
        } = useCoverResources({ config, servers, showToast, showConfirm, fetchDashboardStats });

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
        const {
            cleanup115Tasks,
            cleanup115Form,
            cleanup115EditingId,
            showCreate115Cleanup,
            cleanup115Browser,
            fetch115CleanupTasks,
            openCreate115Cleanup,
            reset115CleanupForm,
            save115CleanupTask,
            edit115CleanupTask,
            delete115CleanupTask,
            toggle115CleanupTask,
            run115CleanupTask,
            open115CleanupBrowser,
            select115CleanupDir,
            cleanup115Up,
            addCurrent115CleanupFolder,
            remove115CleanupFolder,
            upload115Tasks,
            upload115Status,
            upload115Form,
            upload115EditingId,
            showCreate115Upload,
            upload115Browser,
            upload115LocalBrowser,
            fetch115UploadTasks,
            fetch115UploadStatus,
            start115UploadPolling,
            stop115UploadPolling,
            openCreate115Upload,
            reset115UploadForm,
            save115UploadTask,
            edit115UploadTask,
            delete115UploadTask,
            toggle115UploadTask,
            scan115UploadTask,
            retry115UploadFile,
            clear115UploadHistory,
            open115UploadBrowser,
            select115UploadDir,
            upload115Up,
            selectCurrent115UploadFolder,
            open115UploadLocalBrowser,
            select115UploadLocalDir,
            upload115LocalUp,
            selectCurrent115UploadLocalFolder,
            get115UploadTaskState,
            format115UploadSize,
            get115UploadStageLabel,
            get115UploadMethodLabel,
        } = useDrive115Maintenance({ showToast, showConfirm });

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

        watch(tab, async (val) => {
            if (val !== 'config_115' && qrcode115State.visible) {
                close115QrLogin();
            }
            if (val === 'rss') fetchRssData();
            if (val === 'emby_tasks') {
                fetchEmbyTasks();
                startEmbyTaskPolling();
            } else {
                stopEmbyTaskPolling();
            }
            if (val === 'real_library') fetchRealLibraryData();
            if (val === 'webhook') fetchWebhookConfig();
            if (val === 'library_preview') fetchLibraryCovers();
            if (val === 'config_yingchao') fetchHdhiveConfig();
            if (val === 'forward_hdhive') { fetchForwardHdhiveConfig(); fetchHdhiveConfig(); }
            if (val === 'config_notification') { fetchWechatNotifyConfig(); fetchTelegramNotifyConfig(); }
            if (val === 'telegram_monitor') fetchTelegramNotifyConfig();
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
            if (val === 'organize_history') fetchOrganizeHistory();
            if (val === 'missing_episode_stats') loadMissingEpisodeStatsShell();
            if (val === 'config_moviepilot') fetchMpConfig();
        });

        const mobileMenuVisible = ref(false); // 控制"更多"菜单抽屉的显示

        // 监听tab变化更新指示器
        watch(tab, (newTab) => {
            try {
                const cleanUrl = `${window.location.pathname}${window.location.search}`;
                if (newTab && newTab !== 'dashboard' && allValidTabs.has(newTab)) {
                    const nextHash = `#${encodeURIComponent(newTab)}`;
                    if (window.location.hash !== nextHash) {
                        window.history.replaceState(null, '', `${cleanUrl}${nextHash}`);
                    }
                } else if (window.location.hash) {
                    window.history.replaceState(null, '', cleanUrl);
                }
            } catch (_) {}

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
            tab.value = normalizeTab(t);
            mobileMenuVisible.value = false;
        };

        // 添加 Dock 键盘/窗口事件
        onMounted(() => {
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
                'rss': 'RSS 真实库', 'real_library': '独立真实库', 'emby_tasks': 'Emby任务中心', 'webhook': 'Webhook', 'config_302': '302 配置',
                'server':'Emby 配置', 'fonts':'字体库', 'templates':'模板管理',
                'library_preview':'封面备份', 'translations':'翻译配置', 'account':'账户管理',
                'upgrade': '系统升级',
                'docker_manager': 'Docker 管理',
                'media_subscribe': '发现推荐', 'missing_episode_stats': '缺集统计', 'resource_transfer': '资源转存',
                'media_organize': '一条龙菜单', 'organize_history': '整理记录', 'media_organize_rules': '二级分类规则',
                'drive115_cleanup': '115 定时清空',
                'drive115_upload': '115 秒传/上传',
                'organize_monitor_dirs': '整理监控目录',
                'forward_hdhive': 'Forward模块',
                'config_115': '115 配置', 'config_wechat': '微信配置',
                'telegram_monitor': 'Telegram 监听',
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
            if (!localStorage.getItem('isLoggedIn')) window.location.href = 'login.html';

            if (!allValidTabs.has(tab.value)) {
                tab.value = 'dashboard';
            }

            hydrateTaskLogs();
            webhookUrl.value = window.location.origin + '/api/webhook';
            document.addEventListener('visibilitychange', handleDashboardVisibilityChange);

            startPolling();
            startDashboardDeviceMetricsPolling();
            startDashboard115Polling();
            loadProjectVersion();
            fetchUpgradeStatus();
            fetchCurrentUserInfo();
            await fetchGlobalSettings();
            fetchFonts(); fetchLayouts(); fetchLayoutAndPresets(); fetchSuites(); fetchTranslations(); fetchTasks(); fetchDashboardStats();
            fetchWebhookConfig();
            if (tab.value === 'emby_tasks') {
                fetchEmbyTasks();
                startEmbyTaskPolling();
            }
            if (tab.value === 'real_library') fetchRealLibraryData();
            if (tab.value === 'organize_history') fetchOrganizeHistory();
            await fetch302Config();
            fetchStrmConfig();
            fetchMediaOrganizeConfig();
            if (tab.value === 'media_organize_rules') {
                await fetchCategoryRules();
            }
            if (tab.value === 'drive115_upload') {
                fetch115UploadTasks();
                start115UploadPolling();
            }
            if (tab.value === 'docker_manager') {
                refreshDockerManager();
                startDockerSilentRefresh();
            }
            await restoreRunningOrganizeTask();
            fetchHdhiveConfig();
            if (tab.value === 'config_notification') {
                fetchWechatNotifyConfig();
                fetchTelegramNotifyConfig();
            }
            if (tab.value === 'telegram_monitor') {
                fetchTelegramNotifyConfig();
            }
            startHdhiveEventStream();
            loadDiscoverSources().then(() => {
                if (tab.value === 'media_subscribe' && !mainGridItems.value.length) loadMainGrid(true);
            });
            if (tab.value === 'missing_episode_stats') loadMissingEpisodeStatsShell();
            if (servers.value.length > 0) {
                await initDashboard();
                await fetchDashboardOverview();
            }
        });

        onUnmounted(() => {
            close115QrLogin();
            stopPolling();
            stopDashboardDeviceMetricsPolling();
            stopDashboard115Polling();
            stop115UploadPolling();
            stopDockerSilentRefresh();
            stopDockerUpdatePolling();
            stopEmbyTaskPolling();
            stopConsoleLogStream();
            document.removeEventListener('keydown', handleKeydown);
            window.removeEventListener('resize', handleResize);
            window.removeEventListener('popstate', handleDetailPopstate);
            document.removeEventListener('visibilitychange', handleDashboardVisibilityChange);
        });

        const refreshAllLibraries = async () => {
            for (let i = 0; i < servers.value.length; i++) {
                await fetchLibs(i);
            }
        };

        watch(tab, (newVal, oldVal) => {
            if (newVal === 'drive115_cleanup') {
                fetch115CleanupTasks();
            }
            if (newVal === 'docker_manager') {
                refreshDockerManager();
                startDockerSilentRefresh();
            } else if (oldVal === 'docker_manager') {
                stopDockerSilentRefresh();
            }
            if (newVal === 'drive115_upload') {
                fetch115UploadTasks();
                start115UploadPolling();
            } else if (oldVal === 'drive115_upload') {
                stop115UploadPolling();
            }
            if (newVal === 'organize_monitor_dirs') {
                fetchMediaOrganizeConfig();
            }
            if (newVal === 'dashboard') {
                startDashboardDeviceMetricsPolling();
                startDashboard115Polling();
                if (dashboardCovers.value.length === 0) initDashboard();
                fetchDashboardOverview({ allowStale: true });
                ensureDashboardLazyScrollableSections();
            } else {
                stopDashboardDeviceMetricsPolling();
                stopDashboard115Polling();
            }
        });

        const toggleAccordion = (key) => accordions[key] = !accordions[key];
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
                if (!globalConfigLoaded.value) {
                    const loaded = await fetchGlobalSettings();
                    if (!loaded) {
                        showToast('全局配置未加载，保存已取消', 'error');
                        return false;
                    }
                }
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

        const {
            notificationTypes,
            templateLabels,
            templateVars,
            wechatNotifyConfig,
            wechatNotifyTesting,
            wechatNotifySending,
            wechatNotifySaving,
            wechatTemplateTesting,
            fetchWechatNotifyConfig,
            saveWechatNotifyConfig,
            testWechatNotify,
            sendWechatTestMsg,
            testWechatTemplate,
            toggleNotifyType,
            resetWechatTemplate,
            resetTelegramTemplate,
            notificationChannels,
            telegramNotifyConfig,
            telegramStatus,
            telegramLoginForm,
            telegramDialogs,
            telegramDialogSearch,
            telegramDialogPickerOpen,
            selectedTelegramDialogs,
            filteredTelegramDialogs,
            telegramTransferDirBrowser,
            telegramNotifyTesting,
            telegramNotifySending,
            telegramNotifySaving,
            telegramTemplateTesting,
            telegramCodeSending,
            telegramSigningIn,
            telegramLoggingOut,
            telegramDialogsLoading,
            telegramDialogsSaving,
            telegramDialogsDirty,
            fetchTelegramNotifyConfig,
            saveTelegramNotifyConfig,
            testTelegramNotify,
            sendTelegramTestMsg,
            testTelegramTemplate,
            sendTelegramLoginCode,
            signInTelegramAccount,
            logoutTelegramAccount,
            fetchTelegramDialogs,
            openTelegramDialogPicker,
            isTelegramDialogSelected,
            toggleTelegramDialog,
            removeTelegramSelectedDialog,
            saveTelegramDialogs,
            toggleTelegramNotifyType,
            browseTelegramTransferDir,
            selectTelegramTransferDir,
            telegramTransferDirUp,
            applyTelegramTransferDir,
            saveTelegramTransferSettings,
            wrapVar,
        } = useNotificationSettings({ showToast, saveGlobalSettings });

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

        const {
            detailModal,
            openMediaDetail,
            closeDetailModal,
            handleDetailPopstate,
            missingEpisodeCompareModal,
            mpSubscribeModal,
            missingEpisodeStats,
            missingEpisodeLibraries,
            missingEpisodeActiveLibrary,
            missingEpisodeActiveSummary,
            missingEpisodeActiveErrorCount,
            missingEpisodeActionableMissingCount,
            missingEpisodeActionableEpisodeCount,
            missingEpisodeSearchActive,
            missingEpisodeStatsProblemItems,
            visibleMissingEpisodeStatsProblemItems,
            missingEpisodeHasMoreVisibleItems,
            missingEpisodePosterGridRef,
            missingEpisodeLoadMoreSentinel,
            getMissingEpisodePosterKey,
            getMissingEpisodePosterCategoryLabel,
            isMissingEpisodeErrorRow,
            shouldShowMissingEpisodeTmdbCompare,
            isMissingEpisodeManualComplete,
            isMissingEpisodeManualCompleteUpdating,
            isMissingEpisodePosterReady,
            countLocalEpisodes,
            formatLocalSeasonBrief,
            getLocalSeasonRows,
            getTmdbSeasonRows,
            formatEpisodeNumber,
            isEpisodeListed,
            openMissingEpisodeCard,
            toggleMissingEpisodeManualComplete,
            openMissingEpisodeCompareDetail,
            openMissingEpisodeResourceSearch,
            openMissingEpisodeMpSubscribe,
            closeMpSubscribeModal,
            confirmMpSubscribe,
            toggleMpSubscribeSeason,
            openDetailMpSubscribe,
            canOpenMissingEpisodeEmby,
            getMissingEpisodeEmbyUrl,
            openMissingEpisodeEmby,
            closeMissingEpisodeCompare,
            loadMissingEpisodeStatsShell,
            runMissingEpisodeStats,
            refreshMissingEpisodeStats,
            calibrateMissingEpisodeStats,
            setMissingEpisodeLibrary,
            setMissingEpisodeFilter,
            setMissingEpisodeSort,
            openDiscoverFromMissingStats,
            setDetailSeason,
            toggleDetailSeasonExpanded,
            toggleDetailSeasonSubscription,
            loadSeasonEpisodes,
            getSeasonLibraryState,
            getDetailLibraryState,
            isEpisodeInLibrary,
            subscribeMedia,
            unsubscribeMedia,
            getImdbLink,
            getTvdbLink,
            gridModal,
            gridModalEl,
            gridSentinel,
            openRowGrid,
            closeGridModal,
            searchMovieResults,
            searchTvResults,
            discoverSearchLoading,
            discoverSearchQuery,
            discoverHasSearched,
            searchPage,
            searchTotalPages,
            searchDiscover,
            loadMoreSearch,
            clearDiscoverSearch,
            resourceSearchSources,
            resourceSearchSourceLoading,
            resourceSearchSourceMenuOpen,
            selectedResourceSearchSources,
            selectedResourceSearchSourceLabels,
            resourceSearchSourceButtonText,
            resourceSearchSourceReady,
            toggleResourceSearchSourceMenu,
            closeResourceSearchSourceMenu,
            toggleResourceSearchSource,
            loadResourceSearchSources,
            resourceSearchModal,
            openDetailResourceSearch,
            closeResourceSearchModal,
            openForwardResource,
            previewForwardResource,
            genreList,
            discoverSourceTabs,
            discoverActiveSource,
            discoverSourceSupported,
            discoverEmptyText,
            activeSourceDef,
            activeSourceSchema,
            activeSourceFilters,
            getVisibleFilterRows,
            switchDiscoverSource,
            updateSourceFilter,
            toggleSourceChip,
            applyNumberFilter,
            loadDiscoverSources,
            mainGridItems,
            mainGridPage,
            mainGridTotalPages,
            mainGridLoading,
            mainGridNoMore,
            mainGridSentinel,
            mainGridScrollRoot,
            loadMainGrid,
            resetMainGrid,
        } = useDiscover({ tab, isMobile, openPanels, focusedPanel, closeDockDrawers, mobileMenuVisible, mpConfig, config302, servers, ensureDashboardServerId, showToast });

        const {
            organizeHistory,
            fetchOrganizeHistory,
            selectOrganizeHistoryCategory,
            applyOrganizeHistorySearch,
            clearOrganizeHistorySearch,
            changeOrganizeHistoryPage,
        } = useOrganizeHistory({ showToast });

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
            tasksState, activeTaskEntries, dashboardTaskCategories, selectedTaskCategory, selectedTaskCategoryHistory,
            getTaskStatusLabel, getTaskStatusClass, formatActiveTaskTitle, runDashboardTaskCategory,
            toggleTaskLog, openTaskCategoryLog, closeTaskCategoryLog, accordions, toggleAccordion, showCreateTask, clearLogs, clearTaskHistoryCategory,
            dashboardStats, dashboardRecentItems, dashboardVisibleRecentItems, dashboardRecentPlaybacks, dashboardVisibleRecentPlaybacks, dashboardVisibleMediaLibraries, dashboardMediaStats,
            onDashboardLazyScroll,
            dashboardDeviceMetrics, dashboardDeviceMetricsLoaded, dashboardDeviceMetricCards,
            dashboard115Account, dashboard115Loaded, handleDashboard115CardClick,
            dashboardCovers, wallRows, wallReady, dashboardOverviewLoading, initDashboard, fetchDashboardOverview, formatDashboardPlayedAt, getDeviceMetricState, formatDevicePercent, formatDeviceMemory, getDashboardRecentSubtitle, openDashboardLibrary, openDashboardItem, ensureDashboardServerId,
            toasts, showToast,
            systemHealth, systemHealthHeadline, systemHealthMetaText,
            openSystemHealth, closeSystemHealth, runSystemHealthCheck,
            getSystemHealthStatusLabel, getSystemHealthStatusIcon,
            networkConnectivity, networkConnectivityHeadline, networkConnectivityMetaText,
            openNetworkConnectivity, closeNetworkConnectivity, runNetworkConnectivityTest,
            getNetworkStatusLabel, getNetworkStatusIcon,
            confirmState, handleConfirm,
            selectState, handleSelect, closeSelectDialog,
            numberDialogState, handleNumberDialog, closeNumberDialog,
            projectVersion, currentUsername, stopTask,
            upgradeStatus, fetchUpgradeStatus, checkUpgrade, startUpgrade,
            dockerManager, filteredDockerContainers, filteredDockerImages, dockerUpdateCount, dockerContainerStats, dockerImageStats,
            fetchDockerStatus, fetchDockerContainers, fetchDockerImages, checkDockerUpdates,
            refreshDockerManager, setDockerContainerFilter, runDockerContainerAction, toggleDockerAutoUpdate, openDockerScheduledRestartDialog, closeDockerScheduledRestartDialog, saveDockerScheduledRestartDialog, disableDockerScheduledRestart, openDockerLogs, closeDockerLogs, pullDockerImage, deleteDockerImage, pruneUnusedDockerImages, pruneUntaggedDockerImages, setDockerImageFilter,
            closeDockerUpdateDialog, openDockerVersionDialog, closeDockerVersionDialog, saveDockerVersionDialog,
            formatDockerBytes, formatDockerDate, isDockerImageUntagged, dockerImageTagLabel,
            cleanup115Tasks, cleanup115Form, cleanup115EditingId, showCreate115Cleanup, cleanup115Browser,
            fetch115CleanupTasks, openCreate115Cleanup, reset115CleanupForm, save115CleanupTask, edit115CleanupTask,
            delete115CleanupTask, toggle115CleanupTask, run115CleanupTask, open115CleanupBrowser, select115CleanupDir,
            cleanup115Up, addCurrent115CleanupFolder, remove115CleanupFolder,
            upload115Tasks, upload115Status, upload115Form, upload115EditingId, showCreate115Upload, upload115Browser, upload115LocalBrowser,
            fetch115UploadTasks, fetch115UploadStatus, openCreate115Upload, reset115UploadForm, save115UploadTask, edit115UploadTask,
            delete115UploadTask, toggle115UploadTask, scan115UploadTask, retry115UploadFile, clear115UploadHistory,
            open115UploadBrowser, select115UploadDir, upload115Up, selectCurrent115UploadFolder,
            open115UploadLocalBrowser, select115UploadLocalDir, upload115LocalUp, selectCurrent115UploadLocalFolder,
            get115UploadTaskState, format115UploadSize, get115UploadStageLabel, get115UploadMethodLabel,
            embyTasksState, runningEmbyTasks, hasEmbyTaskGroups,
            fetchEmbyTasks, refreshEmbyTasks, runEmbyTask, stopEmbyTask,
            openEmbyTriggerDialog, closeEmbyTriggerDialog, addEmbyTriggerDraft,
            removeEmbyTrigger, saveEmbyTriggers, toggleEmbyTaskNotify,
            toggleEmbyTaskRunningDropdown, formatEmbyTaskProgress, triggerTypeOptions, weekDayOptions,

            // [新增] 真实后台日志
            consoleLogState, logCategoryOptions, filteredLogs, logVirtualState, logContainerRef, onLogScroll, copyLogLine,
            openConsoleLog, closeConsoleLog, reconnectConsoleLogStream, changeConsoleLogLevel, changeConsoleLogCategory, toggleConsoleAutoScroll, clearSystemLogs,

            // [新增] RSS 订阅
            rssConfig, rssForm, rssTasks,
            saveRssConfig, createRssTask, runRssTask, deleteRssTask,
            realLibraryConfig, realLibraryForm, realLibraryTasks, realLibraryEditingId,
            showCreateRealLibrary, realLibraryTesting, realLibraryPathChecking,
            realLibraryTestResult, realLibraryPathResult,
            fetchRealLibraryData, saveRealLibraryConfig, testRealLibraryEmby, validateRealLibraryPaths,
            saveRealLibraryTask, editRealLibraryTask, cancelRealLibraryEdit,
            runRealLibraryTask, toggleRealLibraryTask, deleteRealLibraryTask,
            getRealLibraryTaskState, isRealLibraryTaskRunning,

            // [新增] Webhook 
            webhookConfig, webhookUrl, fetchWebhookConfig, saveWebhookConfig, copyWebhookUrl, toggleWebhookStatus,

            // [新增] 直接上传封面
            directUploadImg, handleDirectUpload, applyDirectUpload,

            // [新增] 302 配置
            config302, save302Config, saveEmbyConfig, toggle302Switch, importEmbyInfo, add302Drive, remove302Drive,
            add302Emby, remove302Emby,
            test115Cookie, manualCleanup115,
            qrcode115State, manual115CookieState, open115QrLogin, close115QrLogin, create115QrCode,
            open115CkTool, copy115FetchedCookie, openToolboxItem,
            openManual115CookieDialog, closeManual115CookieDialog, saveManual115Cookie,
            hasPrimary115Cookie, needs115Setup, standardTopologyEnabled, open115ConfigPanel,

            // [修复] 全局变量及方法
            globalConfig, sensitiveVisibility, saveGlobalSettings, toggleDebugMode,

            // [新增] 影巢配置
            hdhiveConfig, hdhiveChecking, fetchHdhiveConfig,
            addHdhiveAccount, removeHdhiveAccount, testHdhiveAccount,
            loginHdhive, checkinHdhive, gamblerCheckinHdhive, checkinAllHdhive, saveHdhiveAccount,
            toggleHdhiveCheckin,
            refreshHdhiveUserInfo,
            refreshHdhiveUsage,

            // [新增] Forward 模块
            forwardHdhiveConfig, forwardHdhiveSaving, forwardHdhiveTesting,
            forwardHdhiveTestForm, forwardHdhiveTestResult,
            fetchForwardHdhiveConfig, saveForwardHdhiveConfig,
            copyForwardHdhiveWidgetUrl,
            refreshForwardHdhiveToken,
            testForwardHdhiveResources,

            // [新增] 微信通知配置
            wechatNotifyConfig, wechatNotifyTesting, wechatNotifySending, wechatNotifySaving, wechatTemplateTesting,
            fetchWechatNotifyConfig, saveWechatNotifyConfig, testWechatNotify, sendWechatTestMsg, testWechatTemplate,
            notificationTypes, notificationChannels, templateLabels, toggleNotifyType, templateVars, resetWechatTemplate, resetTelegramTemplate, wrapVar,

            // [新增] Telegram通知配置
            telegramNotifyConfig, telegramStatus, telegramLoginForm, telegramDialogs, telegramDialogSearch, telegramDialogPickerOpen, selectedTelegramDialogs, filteredTelegramDialogs, telegramTransferDirBrowser,
            telegramNotifyTesting, telegramNotifySending, telegramNotifySaving, telegramTemplateTesting,
            telegramCodeSending, telegramSigningIn, telegramLoggingOut, telegramDialogsLoading, telegramDialogsSaving, telegramDialogsDirty,
            fetchTelegramNotifyConfig, saveTelegramNotifyConfig, testTelegramNotify, sendTelegramTestMsg, testTelegramTemplate,
            sendTelegramLoginCode, signInTelegramAccount, logoutTelegramAccount, fetchTelegramDialogs,
            openTelegramDialogPicker, isTelegramDialogSelected, toggleTelegramDialog, removeTelegramSelectedDialog, saveTelegramDialogs, toggleTelegramNotifyType,
            browseTelegramTransferDir, selectTelegramTransferDir, telegramTransferDirUp, applyTelegramTransferDir, saveTelegramTransferSettings,
            
            // 新增移动端变量
            mobileMenuVisible, toggleMobileMenu, selectMobileTab,
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
            detailModal, missingEpisodeCompareModal, mpSubscribeModal, openMediaDetail, closeDetailModal,
            missingEpisodeStats, missingEpisodeLibraries, missingEpisodeActiveLibrary, missingEpisodeActiveSummary, missingEpisodeActiveErrorCount, missingEpisodeActionableMissingCount, missingEpisodeActionableEpisodeCount, missingEpisodeSearchActive, missingEpisodeStatsProblemItems,
            visibleMissingEpisodeStatsProblemItems, missingEpisodeHasMoreVisibleItems, missingEpisodePosterGridRef, missingEpisodeLoadMoreSentinel, getMissingEpisodePosterKey, getMissingEpisodePosterCategoryLabel, isMissingEpisodeErrorRow, shouldShowMissingEpisodeTmdbCompare, isMissingEpisodeManualComplete, isMissingEpisodeManualCompleteUpdating, isMissingEpisodePosterReady, countLocalEpisodes, formatLocalSeasonBrief, getLocalSeasonRows, getTmdbSeasonRows, formatEpisodeNumber, isEpisodeListed, openMissingEpisodeCard, toggleMissingEpisodeManualComplete, openMissingEpisodeCompareDetail, openMissingEpisodeResourceSearch, openMissingEpisodeMpSubscribe, closeMpSubscribeModal, confirmMpSubscribe, toggleMpSubscribeSeason, openDetailMpSubscribe, canOpenMissingEpisodeEmby, getMissingEpisodeEmbyUrl, openMissingEpisodeEmby, closeMissingEpisodeCompare,
            runMissingEpisodeStats, refreshMissingEpisodeStats, calibrateMissingEpisodeStats, setMissingEpisodeLibrary, setMissingEpisodeFilter, setMissingEpisodeSort, openDiscoverFromMissingStats,
            setDetailSeason, toggleDetailSeasonExpanded, toggleDetailSeasonSubscription, loadSeasonEpisodes, getSeasonLibraryState, getDetailLibraryState, isEpisodeInLibrary,
            subscribeMedia, unsubscribeMedia, getImdbLink, getTvdbLink,
            gridModal, gridModalEl, gridSentinel, openRowGrid, closeGridModal,
            searchMovieResults, searchTvResults, discoverSearchLoading,
            discoverSearchQuery, discoverHasSearched, searchPage, searchTotalPages,
            searchDiscover, loadMoreSearch, clearDiscoverSearch,
            resourceSearchSources, resourceSearchSourceLoading, resourceSearchSourceMenuOpen,
            selectedResourceSearchSources, selectedResourceSearchSourceLabels, resourceSearchSourceButtonText,
            resourceSearchSourceReady, toggleResourceSearchSourceMenu, closeResourceSearchSourceMenu,
            toggleResourceSearchSource, loadResourceSearchSources,
            resourceSearchModal, openDetailResourceSearch, closeResourceSearchModal, openForwardResource, previewForwardResource,
            genreList,
            discoverSourceTabs, discoverActiveSource,
            discoverSourceSupported, discoverEmptyText,
            activeSourceDef, activeSourceSchema, activeSourceFilters, getVisibleFilterRows,
            switchDiscoverSource, updateSourceFilter, toggleSourceChip, applyNumberFilter,
            loadDiscoverSources,
            mainGridItems, mainGridPage, mainGridTotalPages, mainGridLoading, mainGridNoMore,
            mainGridSentinel, mainGridScrollRoot, loadMainGrid, resetMainGrid,

            // [新增] 整理记录
            organizeHistory,
            fetchOrganizeHistory,
            selectOrganizeHistoryCategory,
            applyOrganizeHistorySearch,
            clearOrganizeHistorySearch,
            changeOrganizeHistoryPage,

            // [新增] 资源转存
            transferInput, transferLoading, transferResult, transferHistory, transferHistoryStats, transferPage, transferPageSize, transferPageCount, transferHistoryRange, paginatedTransferHistory, transferConfig, transferConfigForm, transferDirLabel, transferDirBrowser, getTransferSourceClass, getTransferSourceText, getTransferSourceDetail, getTransferStatusClass, setTransferPage, browseTransferDir, selectTransferDir, transferDirUp, applyTransferDir, saveTransferConfig, clearTransferHistory,
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
            identifyTest, openIdentifyTest, closeIdentifyTest, runIdentifyTest,
            categoryRulesEditor, categoryRulesSaving, ruleListEl,
            subClassify, subClassifyVars, subClassifyVarExamples, subClassifyBaseExamples, subClassifyPreviewSegments, subClassifyToggleLevel, embyLibCount, embyLibLevelOptions,
            embyCacheRefreshing, refreshEmbyCache,
            onLevelDragStart, onLevelDragOver, onLevelDrop, onLevelDragEnd,
            fetchCategoryRules, saveCategoryRules, saveSubClassify, addRule, removeRule,
            addCondition, removeCondition, resetCategoryRules,
            onRuleDragStart, onRuleDragOver, onRuleDrop, onRuleDragEnd,
            orgSourceBrowser, orgTargetBrowser, orgFailedBrowser, monitorDirBrowser, monitorDirsSaving,
            fetchMediaOrganizeConfig, saveMediaOrganizeConfig, saveMonitorDirs, toggleAutoSyncStrm, toggleEmbyScrapers, toggleFilenameOnlyMode, toggleFfprobeMode, toggleFullFfprobeMode, toggleWashByEquivalentSize,
            browseOrganizeSource, selectOrgSourceDir, orgSourceUp, applyOrgSourcePath,
            browseOrganizeTarget, selectOrgTargetDir, orgTargetUp, applyOrgTargetPath,
            browseOrganizeFailed, selectOrgFailedDir, orgFailedUp, applyOrgFailedPath,
            openMonitorDirBrowser, selectMonitorDir, monitorDirUp, addCurrentMonitorDir, removeMonitorDir,
            // 重命名模板编辑器
            movieFormatRef, movieFolderFormatRef, tvFolderFormatRef, tvEpisodeFormatRef,
            movieFolderFormatDisplay, tvFolderFormatDisplay, movieFormatDisplay, tvEpisodeFormatDisplay,
            moviePreviewName, movieFolderPreviewName, tvFolderPreviewName, tvEpisodePreviewName,
            insertToken, resetMovieFormat, resetTvFormat,
        }
        
    }
}).mount('#app')
