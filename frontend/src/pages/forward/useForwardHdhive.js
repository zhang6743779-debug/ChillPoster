import axios from 'axios';
import { reactive, ref, watch } from 'vue';

export function useForwardHdhive({ showToast }) {
    const forwardHdhiveConfig = reactive({
        enabled: true,
        account_id: '',
        public_base_url: '',
        hdhive_enabled: true,
        max_unlock_points: 4,
        library_enabled: true,
        transfer_mode: 'series',
        aiying_enabled: false,
        aiying_tg_id: '',
        aiying_chill_token: '',
        aiying_success_count: 0,
        aiying_today_used: 0,
        aiying_last_times: null,
        aiying_last_message: '',
        aiying_last_result_count: 0,
        aiying_last_checked_at: '',
        telegram_user_id: '',
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
        items: [],
        aiying_total: 0,
        aiying_filtered: 0,
        aiying_items: [],
        errors: {}
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
            if (!forwardHdhiveConfig.aiying_tg_id && forwardHdhiveConfig.telegram_user_id) {
                forwardHdhiveConfig.aiying_tg_id = forwardHdhiveConfig.telegram_user_id;
            }
            refreshWidgetUrlPreview();
        } catch (e) {
            showToast?.('加载 Forward 模块配置失败: ' + (e.response?.data?.detail || e.message), 'error');
        }
    };

    const saveForwardHdhiveConfig = async () => {
        forwardHdhiveSaving.value = true;
        try {
            const payload = {
                enabled: !!forwardHdhiveConfig.enabled,
                account_id: forwardHdhiveConfig.account_id || '',
                public_base_url: normalizeBaseUrl(forwardHdhiveConfig.public_base_url),
                hdhive_enabled: !!forwardHdhiveConfig.hdhive_enabled,
                max_unlock_points: Number(forwardHdhiveConfig.max_unlock_points || 0),
                library_enabled: !!forwardHdhiveConfig.library_enabled,
                transfer_mode: forwardHdhiveConfig.transfer_mode || 'series',
                aiying_enabled: !!forwardHdhiveConfig.aiying_enabled,
                aiying_tg_id: forwardHdhiveConfig.aiying_tg_id || '',
                aiying_chill_token: forwardHdhiveConfig.aiying_chill_token || ''
            };
            const res = await axios.post('/api/forward/config', payload);
            Object.assign(forwardHdhiveConfig, res.data || {});
            refreshWidgetUrlPreview();
            showToast?.('Forward 模块配置已保存', 'success');
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

    const refreshForwardHdhiveToken = async () => {
        const ok = window.confirm('刷新 Token 后，Forward 里已添加的旧模块地址会失效，需要复制新地址重新添加。确定刷新吗？');
        if (!ok) return;
        forwardHdhiveSaving.value = true;
        try {
            const res = await axios.post('/api/forward/token/refresh');
            Object.assign(forwardHdhiveConfig, res.data || {});
            refreshWidgetUrlPreview();
            showToast?.('模块 Token 已刷新，请复制新的模块地址', 'success');
        } catch (e) {
            showToast?.('刷新 Token 失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            forwardHdhiveSaving.value = false;
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
            forwardHdhiveTestResult.aiying_total = res.data?.aiying_total || 0;
            forwardHdhiveTestResult.aiying_filtered = res.data?.aiying_filtered || 0;
            forwardHdhiveTestResult.aiying_items = res.data?.aiying_items || [];
            forwardHdhiveTestResult.errors = res.data?.errors || {};
            if (res.data?.aiying_stats) {
                forwardHdhiveConfig.aiying_success_count = res.data.aiying_stats.success_count || 0;
                forwardHdhiveConfig.aiying_today_used = res.data.aiying_stats.today_used || 0;
                forwardHdhiveConfig.aiying_last_times = res.data.aiying_stats.last_times ?? null;
                forwardHdhiveConfig.aiying_last_message = res.data.aiying_stats.last_message || '';
                forwardHdhiveConfig.aiying_last_result_count = res.data.aiying_stats.last_result_count || 0;
                forwardHdhiveConfig.aiying_last_checked_at = res.data.aiying_stats.last_checked_at || '';
            }
            showToast?.(`查询完成: 影巢 ${forwardHdhiveTestResult.filtered}/${forwardHdhiveTestResult.total} 条，爱影 ${forwardHdhiveTestResult.aiying_filtered}/${forwardHdhiveTestResult.aiying_total} 条`, 'success');
        } catch (e) {
            forwardHdhiveTestResult.total = 0;
            forwardHdhiveTestResult.filtered = 0;
            forwardHdhiveTestResult.items = [];
            forwardHdhiveTestResult.aiying_total = 0;
            forwardHdhiveTestResult.aiying_filtered = 0;
            forwardHdhiveTestResult.aiying_items = [];
            forwardHdhiveTestResult.errors = {};
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
        refreshForwardHdhiveToken,
        testForwardHdhiveResources
    };
}
