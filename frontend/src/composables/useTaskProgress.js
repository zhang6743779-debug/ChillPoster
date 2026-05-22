import axios from 'axios';
import { reactive } from 'vue';

export function useTaskProgress({ showToast, showConfirm, onRssFinished, onBackupFinished }) {
        const TASK_LOGS_STORAGE_KEY = 'dashboard_task_logs';
        const TASK_HISTORY_STORAGE_KEY = 'dashboard_task_history';
        const tasksState = reactive({
            activeTasks: {},
            hasRunning: false,
            logs: [],
            taskHistory: [],
            selectedTaskCategory: null,
            categoryLogVisible: false,
            logVisible: false,
            isPolling: false
        });
        
        let pollInterval = null;

        const persistTaskLogs = () => {
            try {
                localStorage.setItem(TASK_LOGS_STORAGE_KEY, JSON.stringify(tasksState.logs));
            } catch (_) {}
        };

        const persistTaskHistory = () => {
            try {
                localStorage.setItem(TASK_HISTORY_STORAGE_KEY, JSON.stringify(tasksState.taskHistory));
            } catch (_) {}
        };

        const normalizeTaskCategory = (task = {}, runId = '', message = '') => {
            const taskType = String(task.task_type || '').trim();
            const name = String(task.name || message || '').trim();
            const id = String(runId || '').trim();
            if (taskType === 'media_organize' || id.startsWith('organize_') || name.includes('整理')) return 'media_organize';
            if (taskType === 'strm' || name.includes('STRM')) return 'strm';
            if (taskType === 'rss' || id.startsWith('rss_run_') || name.startsWith('RSS')) return 'rss';
            if (taskType === 'upgrade' || name.includes('升级')) return 'system';
            if (taskType === 'backup' || taskType === 'preset_task' || name.includes('封面') || name.includes('备份') || name.startsWith('任务:')) return 'cover';
            return 'other';
        };

        const formatElapsed = (detail = {}) => {
            const raw = detail.elapsed_seconds ?? detail.elapsed;
            if (raw === undefined || raw === null || raw === '') return '';
            if (typeof raw === 'string') return raw.endsWith('s') || raw.endsWith('秒') ? raw : `${raw}s`;
            const seconds = Number(raw);
            if (!Number.isFinite(seconds) || seconds < 0) return '';
            if (seconds < 60) return `${seconds.toFixed(1)}s`;
            const minutes = Math.floor(seconds / 60);
            const rest = Math.round(seconds % 60);
            return `${minutes}分${String(rest).padStart(2, '0')}秒`;
        };

        const buildTaskSummary = (task = {}) => {
            const detail = task.detail || {};
            const category = normalizeTaskCategory(task);
            const elapsed = formatElapsed(detail);
            let summary = task.name || '任务完成';
            if (category === 'media_organize') {
                const parts = [];
                if (detail.movies) parts.push(`电影 ${detail.movies}`);
                if (detail.tv_episodes) parts.push(`剧集 ${detail.tv_episodes}`);
                if (detail.total !== undefined) parts.push(`成功 ${detail.success || 0}/${detail.total || 0}`);
                if (detail.failed) parts.push(`失败 ${detail.failed}`);
                if (detail.sha1_duplicate_skipped) parts.push(`SHA1重复 ${detail.sha1_duplicate_skipped}`);
                if (detail.wash_rejected_skipped) parts.push(`洗版未通过 ${detail.wash_rejected_skipped}`);
                if (detail.same_batch_duplicate_skipped) parts.push(`同批次重复 ${detail.same_batch_duplicate_skipped}`);
                if (detail.trailer_skipped) parts.push(`预告片 ${detail.trailer_skipped}`);
                if (detail.other_skipped) parts.push(`其他跳过 ${detail.other_skipped}`);
                if (
                    detail.skipped
                    && !detail.sha1_duplicate_skipped
                    && !detail.wash_rejected_skipped
                    && !detail.same_batch_duplicate_skipped
                    && !detail.trailer_skipped
                    && !detail.other_skipped
                ) parts.push(`跳过 ${detail.skipped}`);
                if (detail.strm) parts.push(`STRM ${detail.strm}`);
                summary = parts.length ? `媒体整理：${parts.join(' · ')}` : summary;
            } else if (category === 'strm') {
                const parts = [];
                if (detail.scanned !== undefined) parts.push(`扫描 ${detail.scanned || 0}`);
                if (detail.generated !== undefined) parts.push(`生成 ${detail.generated || 0}`);
                if (detail.downloaded !== undefined) parts.push(`下载 ${detail.downloaded || 0}`);
                if (detail.failed) parts.push(`失败 ${detail.failed}`);
                summary = parts.length ? `STRM 同步：${parts.join(' · ')}` : summary;
            }
            return elapsed ? `${summary} · 用时 ${elapsed}` : summary;
        };

        const addTaskHistory = (runId, task = {}) => {
            if (!runId || tasksState.taskHistory.some(item => item.run_id === runId)) return;
            const now = Date.now();
            const item = {
                run_id: runId,
                category: normalizeTaskCategory(task, runId),
                name: task.name || '任务',
                status: task.status || 'finished',
                percent: task.percent || 100,
                summary: buildTaskSummary(task),
                detail: task.detail || {},
                time: new Date(now).toLocaleString(),
                timestamp: now,
            };
            tasksState.taskHistory.unshift(item);
            tasksState.taskHistory = tasksState.taskHistory.slice(0, 160);
            persistTaskHistory();
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

            try {
                const rawHistory = localStorage.getItem(TASK_HISTORY_STORAGE_KEY);
                if (!rawHistory) return;
                const parsedHistory = JSON.parse(rawHistory);
                if (!Array.isArray(parsedHistory)) return;
                tasksState.taskHistory = parsedHistory
                    .filter(item => item && typeof item.run_id === 'string')
                    .slice(0, 160)
                    .map(item => ({
                        run_id: item.run_id,
                        category: typeof item.category === 'string' ? item.category : 'other',
                        name: typeof item.name === 'string' ? item.name : '任务',
                        status: typeof item.status === 'string' ? item.status : 'finished',
                        percent: Number(item.percent || 100),
                        summary: typeof item.summary === 'string' ? item.summary : '',
                        detail: item.detail && typeof item.detail === 'object' ? item.detail : {},
                        time: typeof item.time === 'string' ? item.time : '',
                        timestamp: Number(item.timestamp || 0),
                    }));
            } catch (_) {
                tasksState.taskHistory = [];
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

        const clearTaskHistoryCategory = (category) => {
            tasksState.taskHistory = tasksState.taskHistory.filter(item => item.category !== category);
            persistTaskHistory();
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
        const isTerminalTaskStatus = (status) => ['finished', 'error', 'stopped', 'interrupted'].includes(status);
        const terminalTaskLabel = (status) => {
            if (status === 'finished') return '完成';
            if (status === 'stopped') return '已取消';
            if (status === 'interrupted') return '已中断';
            return '失败';
        };

        const shouldAutoClearTerminalTask = (task = {}, runId = '') => {
            if (task.status !== 'interrupted') return true;
            return normalizeTaskCategory(task, runId) !== 'media_organize';
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
                            if (task.status === 'running') running = true;
                            if (isTerminalTaskStatus(task.status)) {
                                addTaskHistory(id, task);
                                processedTaskIds.add(id);
                                if (shouldAutoClearTerminalTask(task, id)) {
                                    setTimeout(() => axios.post('/api/clear_task_progress', { run_id: id }), 3000);
                                }
                            }
                        }
                        tasksState.hasRunning = running;
                        isFirstPoll = false;
                        return;
                    }

                    for (const id in activeMap) {
                        const task = activeMap[id];
                        if (task.status === 'running') running = true;

                        if (isTerminalTaskStatus(task.status)) {
                            if (!processedTaskIds.has(id)) {
                                const label = terminalTaskLabel(task.status);
                                const msgText = `${task.name} ${label}`;
                                addTaskHistory(id, task);
                                addLog(task.status === 'finished' ? 'success' : 'error', msgText);

                                if (task.status === 'finished') {
                                    showToast(msgText, 'success');
                                    if (task.name && task.name.startsWith('RSS')) {
                                        console.log("RSS任务完成，自动刷新媒体库...");
                                        onRssFinished?.();
                                    }
                                    if (task.name && task.name.includes('备份')) onBackupFinished?.();
                                } else {
                                    showToast(msgText, 'error');
                                }

                                processedTaskIds.add(id);

                                if (shouldAutoClearTerminalTask(task, id)) {
                                    setTimeout(() => axios.post('/api/clear_task_progress', { run_id: id }), 3000);
                                }
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


        const toggleTaskLog = () => tasksState.logVisible = !tasksState.logVisible;
        const openTaskCategoryLog = (category) => {
            tasksState.selectedTaskCategory = category;
            tasksState.categoryLogVisible = true;
        };
        const closeTaskCategoryLog = () => {
            tasksState.categoryLogVisible = false;
        };


    return {
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
    };
}
