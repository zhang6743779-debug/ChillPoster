import axios from 'axios';
import { reactive } from 'vue';

export function useStrmConfig({ needs115Setup, notify115SetupRequired, showToast, showConfirm }) {
        // ==========================================
        // STRM 配置
        // ==========================================
        const defaultStrmTask = {
            name: '标准媒体库同步',
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
                    if (strmConfig.sync_tasks.length === 0 && !needs115Setup.value) addStrmTask();
                }
            } catch (e) {
                if (!needs115Setup.value) addStrmTask();
            }
        };

        const saveStrmConfig = async () => {
            if (needs115Setup.value) {
                notify115SetupRequired();
                return false;
            }
            try {
                const syncTasks = strmConfig.sync_tasks.map(task => ({
                    ...task,
                    drive_index: 0,
                }));
                await axios.post('/api/strm/save', {
                    sync_tasks: syncTasks
                });
                showToast('STRM 配置已保存', 'success');
                return true;
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
                return false;
            }
        };

        const addStrmTask = () => {
            if (needs115Setup.value) {
                notify115SetupRequired();
                return;
            }
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

        const startStrmSync = async (taskIndex, mode) => {
            if (needs115Setup.value) {
                notify115SetupRequired();
                return;
            }
            try {
                // 先保存配置
                const saved = await saveStrmConfig();
                if (!saved) return;

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
            if (needs115Setup.value) {
                notify115SetupRequired();
                return;
            }
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

    return {
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
    };
}
