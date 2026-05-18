import axios from 'axios';
import { ref, watch } from 'vue';

export function useCoverResources({ config, servers, showToast, showConfirm, fetchDashboardStats }) {
        const fontList = ref([]);
        const translationList = ref([]);
        const transServerIdx = ref(0); 

        const loadTransFromLib = async () => {
            const svr = servers.value[0];
            if (!svr || !svr.url || !svr.key) { showToast("请先配置有效的服务器信息", 'error'); return; }
            try {
                const res = await axios.post('/api/connect', { url: svr.url, key: svr.key, public_host: svr.public_host });
                const libs = res.data.libraries || [];
                const savedRes = await axios.get('/api/translations');
                const savedMap = savedRes.data;
                const newList = libs.map(lib => ({ key: lib.name, val: savedMap[lib.name] || '' }));
                for (const k in savedMap) { if (!newList.find(item => item.key === k)) { newList.push({ key: k, val: savedMap[k] }); } }
                translationList.value = newList;
                showToast(`已读取 ${libs.length} 个媒体库翻译`, 'info');
            } catch (e) { showToast('连接服务器失败', 'error'); }
        };
        const fetchTranslations = async () => { try { const res = await axios.get('/api/translations'); translationList.value = Object.entries(res.data).map(([k, v]) => ({ key: k, val: v })); } catch { } };
        const saveTranslations = async () => { const map = {}; translationList.value.forEach(item => { if (item.key) map[item.key.trim()] = item.val.trim(); }); try { await axios.post('/api/save_translations', { translations: map }); showToast("翻译配置已保存", 'success'); } catch { } };
        const addTransRow = () => translationList.value.push({ key: '', val: '' });
        const removeTransRow = (idx) => translationList.value.splice(idx, 1);


        const fetchFonts = async () => { try{ fontList.value = (await axios.get('/api/fonts')).data.fonts; if (!config.font_title && fontList.value.length > 0) { config.font_title = fontList.value[0]; config.font_subtitle = fontList.value[0]; config.badge_font = fontList.value[0]; } fetchDashboardStats(); } catch{} }
        const uploadFont = async (e) => { const fd=new FormData(); fd.append("file", e.target.files[0]); await axios.post('/api/upload_font', fd); fetchFonts(); showToast('字体上传成功', 'success'); }
        
        const deleteFont = async (f) => { 
            const ok = await showConfirm('删除字体', `确定要删除字体 ${f} 吗？`, 'danger');
            if(ok) { await axios.post('/api/delete_font', {filename:f}); fetchFonts(); } 
        }


        watch(fontList, (newList) => { const old = document.getElementById('dynamic-font-styles'); if (old) old.remove(); let css = ''; newList.forEach(f => { css += `@font-face { font-family: '${f}'; src: url('/fonts/${f}'); font-display: swap; }`; }); const s = document.createElement('style'); s.id = 'dynamic-font-styles'; s.textContent = css; document.head.appendChild(s); }, { immediate: true, deep: true });


    return {
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
    };
}
