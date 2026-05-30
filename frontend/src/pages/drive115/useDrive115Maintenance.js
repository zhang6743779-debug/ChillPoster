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
        const cloud115Form = reactive({
            source_cookie: '',
            target_cookie: '',
            target_cid: '',
            target_name: '',
            target_path: '',
            concurrency: 1,
            selected_items: []
        });
        const cloud115SourceBrowser = reactive({
            visible: false,
            loading: false,
            currentCid: '0',
            currentPath: '/',
            history: [],
            dirs: [],
            files: []
        });
        const cloud115TargetBrowser = reactive({
            visible: false,
            loading: false,
            currentCid: '0',
            currentPath: '/',
            history: [],
            dirs: []
        });
        const cloud115Transfer = reactive({
            loading: false,
            result: null,
            recent: []
        });
        let upload115PollingTimer = null;
        let cloud115PollingTimer = null;

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

        const isCloud115TransferTerminal = (job) => ['success', 'partial', 'error', 'ok'].includes(String(job?.status || ''));

        const rememberCloud115TransferJob = (job) => {
            if (!job) return;
            const jobId = job.job_id || `${Date.now()}_${Math.random().toString(16).slice(2)}`;
            if (cloud115Transfer.recent.some(item => String(item.job_id || item.id) === String(jobId))) return;
            cloud115Transfer.recent.unshift({ ...job, id: jobId });
            cloud115Transfer.recent.splice(5);
        };

        const stopCloud115TransferPolling = () => {
            if (cloud115PollingTimer) {
                clearInterval(cloud115PollingTimer);
                cloud115PollingTimer = null;
            }
        };

        const fetchCloud115TransferJob = async (jobId, silent = true) => {
            if (!jobId) return null;
            try {
                const res = await axios.get(`/api/drive115_upload/cloud/jobs/${jobId}`);
                const job = res.data?.job || null;
                if (!job) return null;
                cloud115Transfer.result = job;
                if (isCloud115TransferTerminal(job)) {
                    cloud115Transfer.loading = false;
                    stopCloud115TransferPolling();
                    rememberCloud115TransferJob(job);
                    if (!silent) {
                        showToast(job.summary || job.message || '网盘资源秒传完成', job.status === 'error' ? 'error' : 'success');
                    }
                }
                return job;
            } catch (e) {
                if (!silent) showToast('获取网盘秒传进度失败: ' + (e.response?.data?.detail || e.message), 'error');
                return null;
            }
        };

        const startCloud115TransferPolling = (jobId) => {
            stopCloud115TransferPolling();
            fetchCloud115TransferJob(jobId, true);
            cloud115PollingTimer = setInterval(async () => {
                const job = await fetchCloud115TransferJob(jobId, true);
                if (job && isCloud115TransferTerminal(job)) {
                    showToast(job.summary || job.message || '网盘资源秒传完成', job.status === 'error' ? 'error' : 'success');
                }
            }, 1500);
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

        const cloud115ItemKey = (item) => `${item?.type || 'file'}:${item?.id || item?.cid || item?.file_id || item?.name || ''}`;

        const isCloud115ItemSelected = (item) => {
            const key = cloud115ItemKey(item);
            return cloud115Form.selected_items.some(selected => cloud115ItemKey(selected) === key);
        };

        const normalizeCloud115ItemForSelection = (item) => {
            const name = item?.name || '';
            const currentPath = cloud115SourceBrowser.currentPath === '/' ? '' : cloud115SourceBrowser.currentPath;
            return {
                type: item?.type || 'file',
                id: String(item?.id || item?.cid || item?.file_id || ''),
                cid: String(item?.cid || item?.id || ''),
                file_id: String(item?.file_id || item?.id || ''),
                name,
                path: `${currentPath}/${name}`.replace(/\/+/g, '/'),
                pickcode: item?.pickcode || '',
                sha1: item?.sha1 || '',
                size: Number(item?.size || 0)
            };
        };

        const loadCloud115SourceDir = async (cid = '0', path = '/') => {
            if (!cloud115Form.source_cookie.trim()) return showToast('请先填写来源账号 CK', 'error');
            cloud115SourceBrowser.loading = true;
            try {
                const res = await axios.post('/api/drive115_upload/cloud/browse', {
                    cookie: cloud115Form.source_cookie,
                    cid,
                    include_files: true
                });
                if (res.data?.status !== 'ok') throw new Error(res.data?.message || '读取目录失败');
                cloud115SourceBrowser.currentCid = String(cid || '0');
                cloud115SourceBrowser.currentPath = path || '/';
                cloud115SourceBrowser.dirs = (res.data.dirs || []).map(item => ({ ...item, type: 'dir' }));
                cloud115SourceBrowser.files = (res.data.files || []).map(item => ({ ...item, type: 'file' }));
            } catch (e) {
                showToast('浏览来源网盘失败: ' + (e.message || e), 'error');
            } finally {
                cloud115SourceBrowser.loading = false;
            }
        };

        const openCloud115SourceBrowser = () => {
            if (cloud115SourceBrowser.visible) {
                cloud115SourceBrowser.visible = false;
                return;
            }
            if (!cloud115Form.source_cookie.trim()) return showToast('请先填写来源账号 CK', 'error');
            cloud115SourceBrowser.visible = true;
            cloud115SourceBrowser.history.splice(0);
            loadCloud115SourceDir('0', '/');
        };

        const selectCloud115SourceDir = (dir) => {
            cloud115SourceBrowser.history.push({ cid: cloud115SourceBrowser.currentCid, path: cloud115SourceBrowser.currentPath });
            const nextPath = cloud115SourceBrowser.currentPath === '/' ? `/${dir.name}` : `${cloud115SourceBrowser.currentPath}/${dir.name}`;
            loadCloud115SourceDir(dir.cid || dir.id, nextPath);
        };

        const cloud115SourceUp = () => {
            const prev = cloud115SourceBrowser.history.pop();
            if (!prev) return;
            loadCloud115SourceDir(prev.cid, prev.path);
        };

        const toggleCloud115SourceItem = (item) => {
            const normalized = normalizeCloud115ItemForSelection(item);
            if (!normalized.id) return;
            const key = cloud115ItemKey(normalized);
            const idx = cloud115Form.selected_items.findIndex(selected => cloud115ItemKey(selected) === key);
            if (idx >= 0) {
                cloud115Form.selected_items.splice(idx, 1);
            } else {
                cloud115Form.selected_items.push(normalized);
            }
        };

        const removeCloud115SelectedItem = (item) => {
            const key = cloud115ItemKey(item);
            const idx = cloud115Form.selected_items.findIndex(selected => cloud115ItemKey(selected) === key);
            if (idx >= 0) cloud115Form.selected_items.splice(idx, 1);
        };

        const clearCloud115SelectedItems = () => {
            cloud115Form.selected_items.splice(0);
        };

        const loadCloud115TargetDir = async (cid = '0', path = '/') => {
            if (!cloud115Form.target_cookie.trim()) return showToast('请先填写目标账号 CK', 'error');
            cloud115TargetBrowser.loading = true;
            try {
                const res = await axios.post('/api/drive115_upload/cloud/browse', {
                    cookie: cloud115Form.target_cookie,
                    cid,
                    include_files: false
                });
                if (res.data?.status !== 'ok') throw new Error(res.data?.message || '读取目录失败');
                cloud115TargetBrowser.currentCid = String(cid || '0');
                cloud115TargetBrowser.currentPath = path || '/';
                cloud115TargetBrowser.dirs = (res.data.dirs || []).map(item => ({ ...item, type: 'dir' }));
            } catch (e) {
                showToast('浏览目标网盘失败: ' + (e.message || e), 'error');
            } finally {
                cloud115TargetBrowser.loading = false;
            }
        };

        const openCloud115TargetBrowser = () => {
            if (cloud115TargetBrowser.visible) {
                cloud115TargetBrowser.visible = false;
                return;
            }
            if (!cloud115Form.target_cookie.trim()) return showToast('请先填写目标账号 CK', 'error');
            cloud115TargetBrowser.visible = true;
            cloud115TargetBrowser.history.splice(0);
            loadCloud115TargetDir('0', '/');
        };

        const selectCloud115TargetDir = (dir) => {
            cloud115TargetBrowser.history.push({ cid: cloud115TargetBrowser.currentCid, path: cloud115TargetBrowser.currentPath });
            const nextPath = cloud115TargetBrowser.currentPath === '/' ? `/${dir.name}` : `${cloud115TargetBrowser.currentPath}/${dir.name}`;
            loadCloud115TargetDir(dir.cid || dir.id, nextPath);
        };

        const cloud115TargetUp = () => {
            const prev = cloud115TargetBrowser.history.pop();
            if (!prev) return;
            loadCloud115TargetDir(prev.cid, prev.path);
        };

        const selectCurrentCloud115TargetFolder = () => {
            if (!cloud115TargetBrowser.currentCid || cloud115TargetBrowser.currentCid === '0') return showToast('不能选择根目录', 'error');
            const path = cloud115TargetBrowser.currentPath || cloud115TargetBrowser.currentCid;
            const name = path.split('/').filter(Boolean).pop() || path;
            cloud115Form.target_cid = cloud115TargetBrowser.currentCid;
            cloud115Form.target_name = name;
            cloud115Form.target_path = path;
            cloud115TargetBrowser.visible = false;
            showToast('已选择目标网盘目录', 'success');
        };

        const runCloud115RapidTransfer = async () => {
            if (!cloud115Form.source_cookie.trim()) return showToast('请填写来源账号 CK', 'error');
            if (!cloud115Form.target_cookie.trim()) return showToast('请填写目标账号 CK', 'error');
            if (!cloud115Form.selected_items.length) return showToast('请选择需要秒传的文件或文件夹', 'error');
            if (!cloud115Form.target_cid || cloud115Form.target_cid === '0') return showToast('请选择目标网盘目录', 'error');
            const concurrency = Math.max(1, Math.min(10, parseInt(cloud115Form.concurrency || 1, 10) || 1));
            cloud115Form.concurrency = concurrency;
            const ok = await showConfirm(
                '开始网盘资源秒传',
                `将 ${cloud115Form.selected_items.length} 个文件/文件夹秒传到「${cloud115Form.target_path || cloud115Form.target_name}」，并发 ${concurrency}。确定继续吗？`,
                'warning'
            );
            if (!ok) return;
            cloud115Transfer.loading = true;
            stopCloud115TransferPolling();
            cloud115Transfer.result = {
                status: 'queued',
                stage: 'queued',
                message: '任务已提交，等待开始',
                selected_count: cloud115Form.selected_items.length,
                total_files: 0,
                processed: 0,
                success: 0,
                skipped: 0,
                failed: 0,
                folders: 0,
                concurrency,
                progress: 0,
                results: []
            };
            try {
                const payload = {
                    source_cookie: cloud115Form.source_cookie,
                    target_cookie: cloud115Form.target_cookie,
                    target_cid: cloud115Form.target_cid,
                    target_path: cloud115Form.target_path,
                    concurrency,
                    items: cloud115Form.selected_items
                };
                const res = await axios.post('/api/drive115_upload/cloud/rapid_transfer', payload);
                const result = res.data?.job || res.data || {};
                cloud115Transfer.result = result;
                if (result.job_id && !isCloud115TransferTerminal(result)) {
                    startCloud115TransferPolling(result.job_id);
                    showToast('网盘资源秒传任务已开始', 'success');
                } else {
                    cloud115Transfer.loading = false;
                    rememberCloud115TransferJob(result);
                    showToast(result.summary || '网盘资源秒传完成', result.status === 'error' ? 'error' : 'success');
                }
            } catch (e) {
                showToast('网盘资源秒传失败: ' + (e.response?.data?.detail || e.message), 'error');
                cloud115Transfer.loading = false;
                stopCloud115TransferPolling();
            }
        };

        const getCloud115TransferCount = (field) => Number(cloud115Transfer.result?.[field] || 0);

        const getCloud115TransferPending = () => {
            const total = getCloud115TransferCount('total_files');
            const processed = getCloud115TransferCount('processed');
            return Math.max(0, total - processed);
        };

        const getCloud115TransferProgress = () => Math.max(0, Math.min(100, Number(cloud115Transfer.result?.progress || 0)));

        const getCloud115TransferStatusLabel = () => {
            const status = String(cloud115Transfer.result?.status || '');
            const stage = String(cloud115Transfer.result?.stage || '');
            if (status === 'success' || status === 'ok') return '已完成';
            if (status === 'partial') return '部分完成';
            if (status === 'error') return '失败';
            if (stage === 'scanning') return '扫描中';
            if (stage === 'transferring') return '秒传中';
            if (status === 'queued') return '排队中';
            return cloud115Transfer.loading ? '处理中' : '未开始';
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
        cloud115Form,
        cloud115SourceBrowser,
        cloud115TargetBrowser,
        cloud115Transfer,
        fetch115UploadTasks,
        fetch115UploadStatus,
        start115UploadPolling,
        stop115UploadPolling,
        stopCloud115TransferPolling,
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
        openCloud115SourceBrowser,
        selectCloud115SourceDir,
        cloud115SourceUp,
        toggleCloud115SourceItem,
        isCloud115ItemSelected,
        removeCloud115SelectedItem,
        clearCloud115SelectedItems,
        openCloud115TargetBrowser,
        selectCloud115TargetDir,
        cloud115TargetUp,
        selectCurrentCloud115TargetFolder,
        runCloud115RapidTransfer,
        getCloud115TransferCount,
        getCloud115TransferPending,
        getCloud115TransferProgress,
        getCloud115TransferStatusLabel,
        get115UploadTaskState,
        format115UploadSize,
        get115UploadStageLabel,
        get115UploadMethodLabel,
    };
}
