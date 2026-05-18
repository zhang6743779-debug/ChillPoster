import axios from 'axios';
import { ref } from 'vue';

export function useCoverBackups({ servers, tasksState, showToast, showConfirm, fetchDashboardStats }) {
        const previewServerIdx = ref(0);
        const libraryCards = ref([]);
        const loadingCovers = ref(false);
        const suiteList = ref([]);
        const newSuiteName = ref('');
        const creatingBackup = ref(false);
        const viewingSuite = ref(null);
        const viewingSuiteImages = ref([]);
        const selectedRestoreIds = ref([]);
        

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
        

    return {
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
    };
}
