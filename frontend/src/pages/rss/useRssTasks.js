import axios from 'axios';
import { reactive, ref } from 'vue';

export function useRssTasks({ showToast, showConfirm }) {
    // ==========================================
    // 4. RSS 相关逻辑 (修改版 - 含编辑功能)
    // ==========================================
    const rssConfig = reactive({ source_root: '', link_root: '' });
    const rssForm = reactive({ name: '', cron: '0 */4 * * *', rss_url: '', target_server_idx: 0, content_type: 'movies' });
    const rssTasks = ref([]);
    const showCreateRss = ref(false);

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

    return {
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
    };
}
