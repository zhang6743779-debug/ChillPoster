import axios from 'axios';
import { reactive, ref, watch } from 'vue';

export function useForwardHdhive({ showToast }) {
    const forwardHdhiveConfig = reactive({
        enabled: true,
        account_id: '',
        public_base_url: '',
        max_unlock_points: 4,
        accounts: [],
        widget_path: '',
        widget_url: ''
    });

    const forwardHdhiveSaving = ref(false);
    const forwardHdhiveTesting = ref(false);
    const forwardHdhiveTestForm = reactive({
        type: 'movie',
        tmdb_id: '550'
    });
    const forwardHdhiveTestResult = reactive({
        total: 0,
        filtered: 0,
        items: []
    });

    const normalizeBaseUrl = (value) => String(value || '').trim().replace(/\/+$/, '');

    const refreshWidgetUrlPreview = () => {
        const path = forwardHdhiveConfig.widget_path || '';
        if (!path) return;
        const base = normalizeBaseUrl(forwardHdhiveConfig.public_base_url) || window.location.origin;
        forwardHdhiveConfig.widget_url = `${base}${path}`;
    };

    const fetchForwardHdhiveConfig = async () => {
        try {
            const res = await axios.get('/api/forward/config');
            Object.assign(forwardHdhiveConfig, res.data || {});
            refreshWidgetUrlPreview();
        } catch (e) {
            showToast?.('加载 Forward 影巢配置失败: ' + (e.response?.data?.detail || e.message), 'error');
        }
    };

    const saveForwardHdhiveConfig = async () => {
        forwardHdhiveSaving.value = true;
        try {
            const payload = {
                enabled: !!forwardHdhiveConfig.enabled,
                account_id: forwardHdhiveConfig.account_id || '',
                public_base_url: normalizeBaseUrl(forwardHdhiveConfig.public_base_url),
                max_unlock_points: Number(forwardHdhiveConfig.max_unlock_points || 0)
            };
            const res = await axios.post('/api/forward/config', payload);
            Object.assign(forwardHdhiveConfig, res.data || {});
            refreshWidgetUrlPreview();
            showToast?.('Forward 影巢配置已保存', 'success');
        } catch (e) {
            showToast?.('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            forwardHdhiveSaving.value = false;
        }
    };

    const copyForwardHdhiveWidgetUrl = async () => {
        const url = forwardHdhiveConfig.widget_url || '';
        if (!url) return;
        try {
            await navigator.clipboard.writeText(url);
            showToast?.('模块地址已复制', 'success');
        } catch (e) {
            showToast?.('复制失败，请手动选择地址', 'error');
        }
    };

    const testForwardHdhiveResources = async () => {
        if (!forwardHdhiveTestForm.tmdb_id) {
            showToast?.('请填写 TMDB ID', 'warning');
            return;
        }
        forwardHdhiveTesting.value = true;
        try {
            const res = await axios.post('/api/forward/test_resources', {
                type: forwardHdhiveTestForm.type,
                tmdb_id: forwardHdhiveTestForm.tmdb_id
            });
            forwardHdhiveTestResult.total = res.data?.total || 0;
            forwardHdhiveTestResult.filtered = res.data?.filtered || 0;
            forwardHdhiveTestResult.items = res.data?.items || [];
            showToast?.(`查询完成: ${forwardHdhiveTestResult.filtered}/${forwardHdhiveTestResult.total} 条符合限制`, 'success');
        } catch (e) {
            forwardHdhiveTestResult.total = 0;
            forwardHdhiveTestResult.filtered = 0;
            forwardHdhiveTestResult.items = [];
            showToast?.('查询失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            forwardHdhiveTesting.value = false;
        }
    };

    watch(
        () => [forwardHdhiveConfig.public_base_url, forwardHdhiveConfig.widget_path],
        refreshWidgetUrlPreview
    );

    return {
        forwardHdhiveConfig,
        forwardHdhiveSaving,
        forwardHdhiveTesting,
        forwardHdhiveTestForm,
        forwardHdhiveTestResult,
        fetchForwardHdhiveConfig,
        saveForwardHdhiveConfig,
        copyForwardHdhiveWidgetUrl,
        testForwardHdhiveResources
    };
}
