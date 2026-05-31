import axios from 'axios';
import { reactive, ref, watch } from 'vue';

export function useForwardAiying({ showToast }) {
    const forwardAiyingConfig = reactive({
        enabled: true,
        public_base_url: '',
        library_enabled: true,
        transfer_mode: 'series',
        aiying_enabled: false,
        aiying_tg_id: '',
        aiying_chill_token: '',
        aiying_rate_limit_per_minute: 6,
        aiying_daily_limit: 500,
        aiying_success_count: 0,
        aiying_today_used: 0,
        aiying_last_times: null,
        aiying_last_message: '',
        aiying_last_result_count: 0,
        aiying_last_checked_at: '',
        telegram_user_id: '',
        widget_path: '',
        widget_url: ''
    });

    const forwardAiyingSaving = ref(false);
    const forwardAiyingTesting = ref(false);
    const forwardAiyingTestForm = reactive({
        type: 'movie',
        tmdb_id: '550'
    });
    const forwardAiyingTestResult = reactive({
        aiying_total: 0,
        aiying_filtered: 0,
        aiying_items: [],
        errors: {}
    });

    const normalizeBaseUrl = (value) => String(value || '').trim().replace(/\/+$/, '');

    const refreshWidgetUrlPreview = () => {
        const path = forwardAiyingConfig.widget_path || '';
        if (!path) return;
        const base = normalizeBaseUrl(forwardAiyingConfig.public_base_url) || window.location.origin;
        forwardAiyingConfig.widget_url = `${base}${path}`;
    };

    const fetchForwardAiyingConfig = async () => {
        try {
            const res = await axios.get('/api/forward/config');
            Object.assign(forwardAiyingConfig, res.data || {});
            if (!forwardAiyingConfig.aiying_tg_id && forwardAiyingConfig.telegram_user_id) {
                forwardAiyingConfig.aiying_tg_id = forwardAiyingConfig.telegram_user_id;
            }
            refreshWidgetUrlPreview();
        } catch (e) {
            showToast?.('加载 Forward 模块配置失败: ' + (e.response?.data?.detail || e.message), 'error');
        }
    };

    const saveForwardAiyingConfig = async () => {
        forwardAiyingSaving.value = true;
        try {
            const payload = {
                enabled: !!forwardAiyingConfig.enabled,
                public_base_url: normalizeBaseUrl(forwardAiyingConfig.public_base_url),
                library_enabled: !!forwardAiyingConfig.library_enabled,
                transfer_mode: forwardAiyingConfig.transfer_mode || 'series',
                aiying_enabled: !!forwardAiyingConfig.aiying_enabled,
                aiying_tg_id: forwardAiyingConfig.aiying_tg_id || '',
                aiying_chill_token: forwardAiyingConfig.aiying_chill_token || ''
            };
            const res = await axios.post('/api/forward/config', payload);
            Object.assign(forwardAiyingConfig, res.data || {});
            refreshWidgetUrlPreview();
            showToast?.('Forward 模块配置已保存', 'success');
        } catch (e) {
            showToast?.('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            forwardAiyingSaving.value = false;
        }
    };

    const copyForwardAiyingWidgetUrl = async () => {
        const url = forwardAiyingConfig.widget_url || '';
        if (!url) return;
        try {
            await navigator.clipboard.writeText(url);
            showToast?.('模块地址已复制', 'success');
        } catch (e) {
            showToast?.('复制失败，请手动选择地址', 'error');
        }
    };

    const refreshForwardAiyingToken = async () => {
        const ok = window.confirm('刷新 Token 后，Forward 里已添加的旧模块地址会失效，需要复制新地址重新添加。确定刷新吗？');
        if (!ok) return;
        forwardAiyingSaving.value = true;
        try {
            const res = await axios.post('/api/forward/token/refresh');
            Object.assign(forwardAiyingConfig, res.data || {});
            refreshWidgetUrlPreview();
            showToast?.('模块 Token 已刷新，请复制新的模块地址', 'success');
        } catch (e) {
            showToast?.('刷新 Token 失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            forwardAiyingSaving.value = false;
        }
    };

    const testForwardAiyingResources = async () => {
        if (!forwardAiyingTestForm.tmdb_id) {
            showToast?.('请填写 TMDB ID', 'warning');
            return;
        }
        forwardAiyingTesting.value = true;
        try {
            const res = await axios.post('/api/forward/test_resources', {
                type: forwardAiyingTestForm.type,
                tmdb_id: forwardAiyingTestForm.tmdb_id
            });
            forwardAiyingTestResult.aiying_total = res.data?.aiying_total || 0;
            forwardAiyingTestResult.aiying_filtered = res.data?.aiying_filtered || 0;
            forwardAiyingTestResult.aiying_items = res.data?.aiying_items || [];
            forwardAiyingTestResult.errors = res.data?.errors || {};
            if (res.data?.aiying_stats) {
                forwardAiyingConfig.aiying_success_count = res.data.aiying_stats.success_count || 0;
                forwardAiyingConfig.aiying_today_used = res.data.aiying_stats.today_used || 0;
                forwardAiyingConfig.aiying_last_times = res.data.aiying_stats.last_times ?? null;
                forwardAiyingConfig.aiying_last_message = res.data.aiying_stats.last_message || '';
                forwardAiyingConfig.aiying_last_result_count = res.data.aiying_stats.last_result_count || 0;
                forwardAiyingConfig.aiying_last_checked_at = res.data.aiying_stats.last_checked_at || '';
            }
            showToast?.(`查询完成: 爱影 ${forwardAiyingTestResult.aiying_filtered}/${forwardAiyingTestResult.aiying_total} 条`, 'success');
        } catch (e) {
            forwardAiyingTestResult.aiying_total = 0;
            forwardAiyingTestResult.aiying_filtered = 0;
            forwardAiyingTestResult.aiying_items = [];
            forwardAiyingTestResult.errors = {};
            showToast?.('查询失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            forwardAiyingTesting.value = false;
        }
    };

    watch(
        () => [forwardAiyingConfig.public_base_url, forwardAiyingConfig.widget_path],
        refreshWidgetUrlPreview
    );

    return {
        forwardAiyingConfig,
        forwardAiyingSaving,
        forwardAiyingTesting,
        forwardAiyingTestForm,
        forwardAiyingTestResult,
        fetchForwardAiyingConfig,
        saveForwardAiyingConfig,
        copyForwardAiyingWidgetUrl,
        refreshForwardAiyingToken,
        testForwardAiyingResources
    };
}
