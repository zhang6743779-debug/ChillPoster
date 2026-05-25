import axios from 'axios';
import { computed, reactive } from 'vue';

const NOTIFY_STORAGE_KEY = 'chillposter-emby-task-notify';

export function useEmbyTasks({ tab, showToast }) {
    const embyTasksState = reactive({
        loading: false,
        actionLoading: '',
        dropdownOpen: false,
        notifyEnabled: localStorage.getItem(NOTIFY_STORAGE_KEY) === '1',
        tasks: [],
        categories: [],
        running: [],
        running_count: 0,
        updated_at: '',
        error: '',
        triggerDialog: {
            visible: false,
            loading: false,
            saving: false,
            task: null,
            triggers: [],
            draft: {
                type: 'DailyTrigger',
                time: '00:00',
                day_of_week: 'Monday',
                interval_hours: 24,
                max_runtime_hours: '',
            },
        },
    });

    let pollTimer = null;
    let initializedRunningSnapshot = false;
    let lastRunningIds = new Set();

    const runningEmbyTasks = computed(() => embyTasksState.running || []);
    const hasEmbyTaskGroups = computed(() => (embyTasksState.categories || []).some(group => (group.tasks || []).length > 0));

    const getTaskId = (task) => String(task?.id || task?.key || task?.name || '');
    const formatEmbyTaskProgress = (task) => `${Number(task?.progress || 0).toFixed(1)}%`;
    const triggerTypeOptions = [
        { value: 'DailyTrigger', label: '每天' },
        { value: 'WeeklyTrigger', label: '每周' },
        { value: 'IntervalTrigger', label: '按间隔' },
        { value: 'StartupTrigger', label: '服务器启动时' },
    ];
    const weekDayOptions = [
        { value: 'Monday', label: '周一' },
        { value: 'Tuesday', label: '周二' },
        { value: 'Wednesday', label: '周三' },
        { value: 'Thursday', label: '周四' },
        { value: 'Friday', label: '周五' },
        { value: 'Saturday', label: '周六' },
        { value: 'Sunday', label: '周日' },
    ];

    const resetTriggerDraft = () => {
        Object.assign(embyTasksState.triggerDialog.draft, {
            type: 'DailyTrigger',
            time: '00:00',
            day_of_week: 'Monday',
            interval_hours: 24,
            max_runtime_hours: '',
        });
    };

    const notifyTaskFinished = (task) => {
        if (!embyTasksState.notifyEnabled || !task) return;
        const label = task.status_label || '执行完成';
        const body = task.name || '计划任务';
        showToast(`${body}: ${label}`, task.status_type === 'error' ? 'error' : 'success');
        if (typeof window === 'undefined' || !('Notification' in window)) return;
        if (Notification.permission === 'granted') {
            try { new Notification(`Emby任务${label}`, { body }); } catch (_) { }
        }
    };

    const reconcileFinishedNotifications = (nextTasks = [], nextRunning = []) => {
        const nextRunningIds = new Set(nextRunning.map(getTaskId).filter(Boolean));
        if (!initializedRunningSnapshot) {
            lastRunningIds = nextRunningIds;
            initializedRunningSnapshot = true;
            return;
        }
        for (const id of lastRunningIds) {
            if (nextRunningIds.has(id)) continue;
            const task = nextTasks.find(item => getTaskId(item) === id);
            if (task && task.status_type !== 'running') notifyTaskFinished(task);
        }
        lastRunningIds = nextRunningIds;
    };

    const fetchEmbyTasks = async (silent = false) => {
        if (!silent) embyTasksState.loading = true;
        try {
            const res = await axios.get('/api/emby_tasks');
            const data = res.data || {};
            embyTasksState.tasks = Array.isArray(data.tasks) ? data.tasks : [];
            embyTasksState.categories = Array.isArray(data.categories) ? data.categories : [];
            embyTasksState.running = Array.isArray(data.running) ? data.running : [];
            embyTasksState.running_count = Number(data.running_count || embyTasksState.running.length || 0);
            embyTasksState.updated_at = data.updated_at || '';
            embyTasksState.error = '';
            reconcileFinishedNotifications(embyTasksState.tasks, embyTasksState.running);
        } catch (e) {
            embyTasksState.error = e.response?.data?.detail || e.message || '获取 Emby 任务失败';
            if (!silent) showToast(embyTasksState.error, 'error');
        } finally {
            if (!silent) embyTasksState.loading = false;
        }
    };

    const refreshEmbyTasks = async () => {
        await fetchEmbyTasks(false);
        if (!embyTasksState.error) showToast('任务状态已刷新', 'success');
    };

    const runEmbyTask = async (task) => {
        const taskId = getTaskId(task);
        if (!taskId || task?.is_running) return;
        embyTasksState.actionLoading = `run:${taskId}`;
        try {
            await axios.post(`/api/emby_tasks/${encodeURIComponent(taskId)}/run`, {});
            showToast('任务已启动', 'success');
            await fetchEmbyTasks(true);
            startEmbyTaskPolling();
        } catch (e) {
            showToast(e.response?.data?.detail || e.message || '启动任务失败', 'error');
        } finally {
            embyTasksState.actionLoading = '';
        }
    };

    const stopEmbyTask = async (task) => {
        const taskId = getTaskId(task);
        if (!taskId) return;
        embyTasksState.actionLoading = `stop:${taskId}`;
        try {
            await axios.post(`/api/emby_tasks/${encodeURIComponent(taskId)}/stop`, {});
            showToast('停止命令已发送', 'success');
            await fetchEmbyTasks(true);
        } catch (e) {
            showToast(e.response?.data?.detail || e.message || '停止任务失败', 'error');
        } finally {
            embyTasksState.actionLoading = '';
        }
    };

    const syncTaskTriggerSummary = (taskId, triggerData = {}) => {
        const updateOne = (task) => {
            if (!task || getTaskId(task) !== taskId) return;
            task.triggers = Array.isArray(triggerData.triggers) ? triggerData.triggers : [];
            task.trigger_summary = triggerData.trigger_summary || '未设置计划';
        };
        embyTasksState.tasks.forEach(updateOne);
        embyTasksState.categories.forEach(group => (group.tasks || []).forEach(updateOne));
        embyTasksState.running.forEach(updateOne);
    };

    const openEmbyTriggerDialog = async (task) => {
        const taskId = getTaskId(task);
        if (!taskId) return;
        embyTasksState.triggerDialog.visible = true;
        embyTasksState.triggerDialog.loading = true;
        embyTasksState.triggerDialog.task = task;
        embyTasksState.triggerDialog.triggers = Array.isArray(task.triggers) ? JSON.parse(JSON.stringify(task.triggers)) : [];
        resetTriggerDraft();
        try {
            const res = await axios.get(`/api/emby_tasks/${encodeURIComponent(taskId)}/triggers`);
            const data = res.data || {};
            embyTasksState.triggerDialog.task = data.task || task;
            embyTasksState.triggerDialog.triggers = Array.isArray(data.triggers) ? data.triggers : [];
            syncTaskTriggerSummary(taskId, data);
        } catch (e) {
            showToast(e.response?.data?.detail || e.message || '读取触发器失败', 'error');
        } finally {
            embyTasksState.triggerDialog.loading = false;
        }
    };

    const closeEmbyTriggerDialog = () => {
        embyTasksState.triggerDialog.visible = false;
        embyTasksState.triggerDialog.loading = false;
        embyTasksState.triggerDialog.saving = false;
        embyTasksState.triggerDialog.task = null;
        embyTasksState.triggerDialog.triggers = [];
        resetTriggerDraft();
    };

    const addEmbyTriggerDraft = () => {
        const draft = embyTasksState.triggerDialog.draft;
        const trigger = {
            type: draft.type,
            time: draft.time || '00:00',
            day_of_week: draft.day_of_week || 'Monday',
            interval_hours: Number(draft.interval_hours || 0),
            max_runtime_hours: draft.max_runtime_hours === '' ? 0 : Number(draft.max_runtime_hours || 0),
        };
        if (trigger.type === 'IntervalTrigger' && trigger.interval_hours <= 0) {
            showToast('间隔小时数需要大于 0', 'error');
            return;
        }
        embyTasksState.triggerDialog.triggers.push(trigger);
        resetTriggerDraft();
    };

    const removeEmbyTrigger = (index) => {
        embyTasksState.triggerDialog.triggers.splice(index, 1);
    };

    const saveEmbyTriggers = async () => {
        const task = embyTasksState.triggerDialog.task;
        const taskId = getTaskId(task);
        if (!taskId) return;
        embyTasksState.triggerDialog.saving = true;
        try {
            const payload = { triggers: embyTasksState.triggerDialog.triggers };
            const res = await axios.post(`/api/emby_tasks/${encodeURIComponent(taskId)}/triggers`, payload);
            const data = res.data || {};
            embyTasksState.triggerDialog.triggers = Array.isArray(data.triggers) ? data.triggers : [];
            embyTasksState.triggerDialog.task = data.task || task;
            syncTaskTriggerSummary(taskId, data);
            showToast('触发器已保存', 'success');
            await fetchEmbyTasks(true);
            closeEmbyTriggerDialog();
        } catch (e) {
            showToast(e.response?.data?.detail || e.message || '保存触发器失败', 'error');
        } finally {
            embyTasksState.triggerDialog.saving = false;
        }
    };

    const toggleEmbyTaskNotify = async () => {
        embyTasksState.notifyEnabled = !embyTasksState.notifyEnabled;
        localStorage.setItem(NOTIFY_STORAGE_KEY, embyTasksState.notifyEnabled ? '1' : '0');
        if (embyTasksState.notifyEnabled && typeof window !== 'undefined' && 'Notification' in window && Notification.permission === 'default') {
            try { await Notification.requestPermission(); } catch (_) { }
        }
    };

    const toggleEmbyTaskRunningDropdown = () => {
        embyTasksState.dropdownOpen = !embyTasksState.dropdownOpen;
    };

    const startEmbyTaskPolling = () => {
        if (pollTimer) return;
        pollTimer = window.setInterval(() => {
            if (tab.value === 'emby_tasks') fetchEmbyTasks(true);
        }, 3000);
    };

    const stopEmbyTaskPolling = () => {
        if (pollTimer) {
            window.clearInterval(pollTimer);
            pollTimer = null;
        }
        embyTasksState.dropdownOpen = false;
    };

    return {
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
    };
}
