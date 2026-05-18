import axios from 'axios';
import { reactive } from 'vue';

export function useTaskProgress({ showToast, showConfirm, onRssFinished, onBackupFinished }) {
        const TASK_LOGS_STORAGE_KEY = 'dashboard_task_logs';
        const tasksState = reactive({
            activeTasks: {}, hasRunning: false, logs: [], logVisible: false, isPolling: false
        });
        
        let pollInterval = null;

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
                            if (task.status === 'finished' || task.status === 'error' || task.status === 'stopped') {
                                processedTaskIds.add(id);
                                setTimeout(() => axios.post('/api/clear_task_progress', { run_id: id }), 3000);
                            }
                        }
                        tasksState.hasRunning = running;
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
                                        onRssFinished?.();
                                    }
                                    if (task.name && task.name.includes('备份')) onBackupFinished?.();
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


        const toggleTaskLog = () => tasksState.logVisible = !tasksState.logVisible;


    return {
        tasksState,
        hydrateTaskLogs,
        addLog,
        clearLogs,
        stopTask,
        startPolling,
        stopPolling,
        toggleTaskLog,
    };
}
