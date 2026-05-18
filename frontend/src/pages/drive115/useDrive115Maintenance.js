import axios from 'axios';
import { reactive, ref } from 'vue';

export function useDrive115Maintenance({ showToast, showConfirm }) {
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
        const upload115Tasks = ref([]);
        const upload115Status = ref({ tasks: {} });
        const upload115EditingId = ref('');
        const showCreate115Upload = ref(false);
        const upload115Form = reactive({
            name: '',
            enabled: true,
            drive_index: 0,
            local_folder: '',
            target_cid: '',
            target_name: '',
            target_path: '',
            watch_mode: 'realtime',
            include_existing_on_start: true,
            delete_local_after_success: true,
            concurrency: 5
        });
        const upload115Browser = reactive({
            visible: false,
            loading: false,
            currentCid: '0',
            currentPath: '/',
            history: [],
            dirs: []
        });
        const upload115LocalBrowser = reactive({
            visible: false,
            loading: false,
            currentPath: '/',
            history: [],
            dirs: []
        });
        let upload115PollingTimer = null;

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

        const fetch115UploadTasks = async () => {
            try {
                const res = await axios.get('/api/drive115_upload/tasks');
                upload115Tasks.value = res.data?.tasks || [];
            } catch (e) {
                showToast('获取 115 上传任务失败', 'error');
            }
        };

        const fetch115UploadStatus = async () => {
            try {
                const res = await axios.get('/api/drive115_upload/status');
                upload115Status.value = res.data || { tasks: {} };
            } catch (e) {
                console.warn('fetch115UploadStatus failed', e);
            }
        };

        const start115UploadPolling = () => {
            stop115UploadPolling();
            fetch115UploadStatus();
            upload115PollingTimer = setInterval(fetch115UploadStatus, 2500);
        };

        const stop115UploadPolling = () => {
            if (upload115PollingTimer) {
                clearInterval(upload115PollingTimer);
                upload115PollingTimer = null;
            }
        };

        const reset115UploadForm = () => {
            upload115EditingId.value = '';
            upload115Form.name = '';
            upload115Form.enabled = true;
            upload115Form.drive_index = 0;
            upload115Form.local_folder = '';
            upload115Form.target_cid = '';
            upload115Form.target_name = '';
            upload115Form.target_path = '';
            upload115Form.watch_mode = 'realtime';
            upload115Form.include_existing_on_start = true;
            upload115Form.delete_local_after_success = true;
            upload115Form.concurrency = 5;
            upload115Browser.visible = false;
            upload115LocalBrowser.visible = false;
        };

        const openCreate115Upload = () => {
            reset115UploadForm();
            showCreate115Upload.value = true;
        };

        const edit115UploadTask = (task) => {
            upload115EditingId.value = task.id || '';
            upload115Form.name = task.name || '';
            upload115Form.enabled = task.enabled !== false;
            upload115Form.drive_index = Number(task.drive_index || 0);
            upload115Form.local_folder = task.local_folder || '';
            upload115Form.target_cid = String(task.target_cid || '');
            upload115Form.target_name = task.target_name || '';
            upload115Form.target_path = task.target_path || '';
            upload115Form.watch_mode = 'realtime';
            upload115Form.include_existing_on_start = true;
            upload115Form.delete_local_after_success = task.delete_local_after_success !== false;
            upload115Form.concurrency = Number(task.concurrency || 5);
            showCreate115Upload.value = true;
        };

        const save115UploadTask = async () => {
            if (!upload115Form.name.trim()) return showToast('请填写任务名称', 'error');
            if (!upload115Form.local_folder.trim()) return showToast('请选择本地监听目录', 'error');
            if (!upload115Form.target_cid || upload115Form.target_cid === '0') return showToast('请选择 115 目标目录', 'error');
            try {
                const payload = JSON.parse(JSON.stringify(upload115Form));
                payload.concurrency = Number(payload.concurrency || 5);
                if (upload115EditingId.value) {
                    await axios.post(`/api/drive115_upload/tasks/${upload115EditingId.value}`, payload);
                } else {
                    await axios.post('/api/drive115_upload/tasks', payload);
                }
                showToast('上传监听任务已保存', 'success');
                showCreate115Upload.value = false;
                reset115UploadForm();
                fetch115UploadTasks();
                fetch115UploadStatus();
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const delete115UploadTask = async (task) => {
            const ok = await showConfirm('删除上传任务', `确定删除监听上传任务「${task.name}」吗？`, 'danger');
            if (!ok) return;
            try {
                await axios.delete(`/api/drive115_upload/tasks/${task.id}`);
                showToast('任务已删除', 'success');
                fetch115UploadTasks();
                fetch115UploadStatus();
            } catch (e) {
                showToast('删除失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const toggle115UploadTask = async (task) => {
            try {
                await axios.post(`/api/drive115_upload/tasks/${task.id}/toggle`, { enabled: task.enabled === false });
                fetch115UploadTasks();
                fetch115UploadStatus();
            } catch (e) {
                showToast('切换状态失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const scan115UploadTask = async (task) => {
            try {
                const res = await axios.post(`/api/drive115_upload/tasks/${task.id}/scan`, { force: true });
                showToast(`已加入队列 ${res.data?.queued || 0} 个文件`, 'success');
                fetch115UploadStatus();
            } catch (e) {
                showToast('扫描失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const retry115UploadFile = async (task, item) => {
            try {
                const res = await axios.post(`/api/drive115_upload/tasks/${task.id}/retry`, { job_id: item.job_id });
                showToast(`已重新入队 ${res.data?.queued || 0} 个文件`, 'success');
                fetch115UploadStatus();
            } catch (e) {
                showToast('重试失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const clear115UploadHistory = async (task) => {
            const ok = await showConfirm('清理上传记录', `确定清理「${task.name}」的成功和失败记录吗？`, 'warning');
            if (!ok) return;
            try {
                await axios.post(`/api/drive115_upload/tasks/${task.id}/clear_history`);
                showToast('记录已清理', 'success');
                fetch115UploadStatus();
            } catch (e) {
                showToast('清理失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const load115UploadDir = async (cid = '0', path = '/') => {
            upload115Browser.loading = true;
            try {
                const res = await axios.post('/api/drive115_upload/browse115', { cid, drive_index: upload115Form.drive_index || 0 });
                if (res.data?.status !== 'ok') throw new Error(res.data?.message || '读取目录失败');
                upload115Browser.currentCid = String(cid || '0');
                upload115Browser.currentPath = path || '/';
                upload115Browser.dirs = res.data.dirs || [];
            } catch (e) {
                showToast('浏览失败: ' + (e.message || e), 'error');
            } finally {
                upload115Browser.loading = false;
            }
        };

        const open115UploadBrowser = () => {
            if (upload115Browser.visible) {
                upload115Browser.visible = false;
                return;
            }
            upload115Browser.visible = true;
            upload115Browser.history.splice(0);
            load115UploadDir('0', '/');
        };

        const select115UploadDir = (dir) => {
            upload115Browser.history.push({ cid: upload115Browser.currentCid, path: upload115Browser.currentPath });
            const nextPath = upload115Browser.currentPath === '/' ? `/${dir.name}` : `${upload115Browser.currentPath}/${dir.name}`;
            load115UploadDir(dir.cid, nextPath);
        };

        const upload115Up = () => {
            const prev = upload115Browser.history.pop();
            if (!prev) return;
            load115UploadDir(prev.cid, prev.path);
        };

        const selectCurrent115UploadFolder = () => {
            if (!upload115Browser.currentCid || upload115Browser.currentCid === '0') return showToast('不能选择根目录', 'error');
            const path = upload115Browser.currentPath || upload115Browser.currentCid;
            const name = path.split('/').filter(Boolean).pop() || path;
            upload115Form.target_cid = upload115Browser.currentCid;
            upload115Form.target_name = name;
            upload115Form.target_path = path;
            upload115Browser.visible = false;
            showToast('已选择 115 目标目录', 'success');
        };

        const load115UploadLocalDir = async (path = '/') => {
            upload115LocalBrowser.loading = true;
            try {
                const res = await axios.post('/api/drive115_upload/browse_local', { path });
                if (res.data?.status !== 'ok') throw new Error(res.data?.message || '读取目录失败');
                upload115LocalBrowser.currentPath = res.data.current || path || '/';
                upload115LocalBrowser.dirs = res.data.dirs || [];
            } catch (e) {
                showToast('浏览失败: ' + (e.message || e), 'error');
            } finally {
                upload115LocalBrowser.loading = false;
            }
        };

        const open115UploadLocalBrowser = () => {
            if (upload115LocalBrowser.visible) {
                upload115LocalBrowser.visible = false;
                return;
            }
            upload115LocalBrowser.visible = true;
            upload115LocalBrowser.history.splice(0);
            load115UploadLocalDir(upload115Form.local_folder || '/');
        };

        const select115UploadLocalDir = (dir) => {
            upload115LocalBrowser.history.push({ path: upload115LocalBrowser.currentPath });
            load115UploadLocalDir(dir.path);
        };

        const upload115LocalUp = () => {
            const prev = upload115LocalBrowser.history.pop();
            if (!prev) return;
            load115UploadLocalDir(prev.path);
        };

        const selectCurrent115UploadLocalFolder = () => {
            upload115Form.local_folder = upload115LocalBrowser.currentPath || '/';
            upload115LocalBrowser.visible = false;
            showToast('已选择本地监听目录', 'success');
        };

        const get115UploadTaskState = (taskId) => upload115Status.value?.tasks?.[taskId] || { queue_size: 0, active: [], recent: [], failed: [] };

        const format115UploadSize = (size) => {
            const value = Number(size || 0);
            if (value < 1024) return `${value} B`;
            if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
            if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
            return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
        };

        const get115UploadStageLabel = (stage) => ({
            queued: '排队中',
            checking: '秒传检测',
            rapid_success: '秒传成功',
            uploading: '真实上传',
            success: '成功',
            failed: '失败'
        }[stage] || stage || '等待中');

        const get115UploadMethodLabel = (method) => method === 'rapid' ? '秒传' : (method === 'multipart' ? '真实上传' : '上传');

    return {
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
    };
}
