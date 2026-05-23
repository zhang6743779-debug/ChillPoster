import axios from 'axios';
import { reactive, ref } from 'vue';

const defaultConfig = () => ({
    enabled: true,
    emby_name: '独立真实库',
    emby_url: '',
    emby_key: '',
    emby_public_host: '',
    source_root: '',
    link_root: '',
    tmdb_key: '',
    proxy_url: '',
});

const defaultTask = () => ({
    name: '',
    rss_url: '',
    cron: '0 */4 * * *',
    content_type: 'movies',
    enabled: true,
});

export function useRealLibrary({ showToast, showConfirm }) {
    const realLibraryConfig = reactive(defaultConfig());
    const realLibraryForm = reactive(defaultTask());
    const realLibraryTasks = ref([]);
    const realLibraryEditingId = ref(null);
    const showCreateRealLibrary = ref(false);
    const realLibraryTesting = ref(false);
    const realLibraryPathChecking = ref(false);
    const realLibraryTestResult = ref(null);
    const realLibraryPathResult = ref(null);

    const resetRealLibraryForm = () => {
        Object.assign(realLibraryForm, defaultTask());
        realLibraryEditingId.value = null;
    };

    const fetchRealLibraryData = async () => {
        try {
            const [configRes, taskRes] = await Promise.all([
                axios.get('/api/real_library/config'),
                axios.get('/api/real_library/tasks'),
            ]);
            Object.assign(realLibraryConfig, defaultConfig(), configRes.data || {});
            realLibraryTasks.value = Array.isArray(taskRes.data) ? taskRes.data : [];
        } catch (e) {
            showToast('独立真实库配置加载失败', 'error');
        }
    };

    const saveRealLibraryConfig = async () => {
        try {
            const res = await axios.post('/api/real_library/save_config', realLibraryConfig);
            Object.assign(realLibraryConfig, defaultConfig(), res.data?.config || {});
            showToast(res.data?.message || '独立真实库配置已保存', 'success');
        } catch (e) {
            showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
        }
    };

    const testRealLibraryEmby = async () => {
        realLibraryTesting.value = true;
        realLibraryTestResult.value = null;
        try {
            const res = await axios.post('/api/real_library/test_emby', realLibraryConfig);
            realLibraryTestResult.value = res.data || {};
            showToast(realLibraryTestResult.value.message || '检测完成', realLibraryTestResult.value.status === 'success' ? 'success' : 'warning');
        } catch (e) {
            realLibraryTestResult.value = { status: 'error', message: e.response?.data?.detail || e.message };
            showToast('检测失败', 'error');
        } finally {
            realLibraryTesting.value = false;
        }
    };

    const validateRealLibraryPaths = async () => {
        realLibraryPathChecking.value = true;
        realLibraryPathResult.value = null;
        try {
            const res = await axios.post('/api/real_library/validate_paths', realLibraryConfig);
            realLibraryPathResult.value = res.data || {};
            showToast(realLibraryPathResult.value.status === 'success' ? '路径检测通过' : '路径需要检查', realLibraryPathResult.value.status === 'success' ? 'success' : 'warning');
        } catch (e) {
            realLibraryPathResult.value = { status: 'error', message: e.response?.data?.detail || e.message };
            showToast('路径检测失败', 'error');
        } finally {
            realLibraryPathChecking.value = false;
        }
    };

    const editRealLibraryTask = (task) => {
        realLibraryEditingId.value = task.id;
        Object.assign(realLibraryForm, defaultTask(), {
            name: task.name || '',
            rss_url: task.rss_url || '',
            cron: task.cron || '0 */4 * * *',
            content_type: task.content_type || 'movies',
            enabled: task.enabled !== false,
        });
        showCreateRealLibrary.value = true;
        const container = document.querySelector('.content-area');
        if (container) container.scrollTop = 0;
    };

    const cancelRealLibraryEdit = () => {
        resetRealLibraryForm();
        showCreateRealLibrary.value = false;
    };

    const saveRealLibraryTask = async () => {
        if (!realLibraryForm.name || !realLibraryForm.rss_url) {
            showToast('请填写任务名称和 RSS 地址', 'warning');
            return;
        }
        try {
            if (realLibraryEditingId.value) {
                await axios.post('/api/real_library/update_task', {
                    ...realLibraryForm,
                    id: realLibraryEditingId.value,
                });
                showToast('独立真实库任务已更新', 'success');
            } else {
                await axios.post('/api/real_library/create_task', realLibraryForm);
                showToast('独立真实库任务已创建', 'success');
            }
            resetRealLibraryForm();
            showCreateRealLibrary.value = false;
            await fetchRealLibraryData();
        } catch (e) {
            showToast('保存任务失败: ' + (e.response?.data?.detail || e.message), 'error');
        }
    };

    const runRealLibraryTask = async (id) => {
        try {
            await axios.post('/api/real_library/run_now', { id });
            showToast('独立真实库任务已提交，进度会在任务卡片显示', 'info');
        } catch (e) {
            showToast('启动失败: ' + (e.response?.data?.detail || e.message), 'error');
        }
    };

    const toggleRealLibraryTask = async (task, event) => {
        const next = event.target.checked;
        task.enabled = next;
        try {
            await axios.post('/api/real_library/toggle_task', { id: task.id, enabled: next });
            showToast(next ? '任务已启用' : '任务已暂停', next ? 'success' : 'info');
        } catch (e) {
            task.enabled = !next;
            event.target.checked = !next;
            showToast('状态切换失败', 'error');
        }
    };

    const deleteRealLibraryTask = async (id) => {
        const ok = await showConfirm('删除真实库任务', '确定要删除这个独立真实库任务吗？', 'danger');
        if (!ok) return;
        const deleteFiles = await showConfirm(
            '清理真实库文件',
            '是否同时删除这个任务生成的硬链接目录和 Emby 媒体库？',
            'warning'
        );
        try {
            await axios.post('/api/real_library/delete_task', { id, delete_files: deleteFiles });
            showToast(deleteFiles ? '任务和关联资源已删除' : '任务已删除，文件已保留', 'success');
            await fetchRealLibraryData();
        } catch (e) {
            showToast('删除失败: ' + (e.response?.data?.detail || e.message), 'error');
        }
    };

    return {
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
    };
}
