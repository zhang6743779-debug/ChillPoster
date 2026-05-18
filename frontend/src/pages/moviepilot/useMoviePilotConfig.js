import axios from 'axios';
import { reactive, ref } from 'vue';

export function useMoviePilotConfig({ showToast }) {
    // ==========================================
    // 13. MoviePilot 配置
    // ==========================================
    const mpConfig = reactive({ mp_url: '', mp_username: '', mp_password: '' });
    const mpTesting = ref(false);
    const mpTestResult = ref(null);

    const fetchMpConfig = async () => {
        try {
            const res = await axios.get('/api/moviepilot/config');
            Object.assign(mpConfig, res.data);
        } catch (e) { console.error('fetchMpConfig:', e); }
    };
    const saveMpConfig = async () => {
        try {
            await axios.post('/api/moviepilot/config', mpConfig);
            showToast('MoviePilot 配置已保存', 'success');
            mpTestResult.value = null;
        } catch (e) {
            showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
        }
    };
    const testMpConnection = async () => {
        mpTesting.value = true;
        mpTestResult.value = null;
        try {
            // 先保存再测试
            await axios.post('/api/moviepilot/config', mpConfig);
            const res = await axios.post('/api/moviepilot/test');
            mpTestResult.value = { ok: res.data.status === 'ok', msg: res.data.message || '连接成功' };
        } catch (e) {
            mpTestResult.value = { ok: false, msg: e.response?.data?.detail || '连接失败' };
        } finally { mpTesting.value = false; }
    };

    return {
        mpConfig,
        mpTesting,
        mpTestResult,
        fetchMpConfig,
        saveMpConfig,
        testMpConnection,
    };
}
