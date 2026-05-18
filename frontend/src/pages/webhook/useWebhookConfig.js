import axios from 'axios';
import { reactive, ref, watch } from 'vue';

export function useWebhookConfig({ presetList, validateSelections, showToast }) {
    // ==========================================
    // 5. Webhook 逻辑
    // ==========================================
    const webhookConfig = reactive({
        enabled: false,
        engine: 'classic',
        preset: '',
        mode: 'random'
    });
    const webhookUrl = ref(window.location.origin + '/api/webhook');

    const fetchWebhookConfig = async () => {
        try {
            const res = await axios.get('/api/webhook/config');
            Object.assign(webhookConfig, res.data);
            validateSelections();
        } catch(e) {}
    };

    const saveWebhookConfig = async () => {
        try {
            await axios.post('/api/webhook/config', webhookConfig);
            showToast('Webhook 配置已保存', 'success');
        } catch(e) {
            showToast('保存失败', 'error');
        }
    };

    const toggleWebhookStatus = async (event) => {
        const newState = event.target.checked;
        const oldState = webhookConfig.enabled;
        webhookConfig.enabled = newState;
        try {
            await axios.post('/api/webhook/config', webhookConfig);
            showToast(newState ? 'Webhook 已启用' : 'Webhook 已关闭', newState ? 'success' : 'info');
        } catch(e) {
            webhookConfig.enabled = oldState;
            event.target.checked = oldState;
            showToast('状态切换失败', 'error');
        }
    };

    const copyWebhookUrl = () => {
        navigator.clipboard.writeText(webhookUrl.value).then(() => {
            showToast('地址已复制', 'success');
        }).catch(() => {
            showToast('复制失败，请手动复制', 'error');
        });
    };

    watch(() => webhookConfig.engine, (newVal) => {
        if (!newVal) return;
        const currentPreset = presetList.value.find(p => p.filename === webhookConfig.preset);
        if (!webhookConfig.preset || (currentPreset && currentPreset.engine !== newVal)) {
            const available = presetList.value.filter(p => p.engine === newVal);
            if (available.length > 0) {
                webhookConfig.preset = available[0].filename;
            } else {
                webhookConfig.preset = '';
            }
        }
    });

    return {
        webhookConfig,
        webhookUrl,
        fetchWebhookConfig,
        saveWebhookConfig,
        copyWebhookUrl,
        toggleWebhookStatus,
    };
}
