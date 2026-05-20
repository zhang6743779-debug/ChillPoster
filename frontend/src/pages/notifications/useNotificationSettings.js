import axios from 'axios';
import { computed, reactive, ref } from 'vue';

export function useNotificationSettings({ showToast, saveGlobalSettings }) {
        // ==========================================
        // 微信通知逻辑
        // ==========================================
        const notificationTypes = ref({
            playback: { name: '播放通知', description: '有人通过302播放媒体时发送通知', icon: '🎬' },
            media_added: { name: '入库通知', description: '新媒体添加到媒体库时发送通知', icon: '📚' },
            organize_complete: { name: '整理通知', description: '媒体整理完成时发送通知', icon: '💿' },
            resource_transfer: { name: '转存通知', description: '115网盘转存完成时发送通知', icon: '📥' },
            checkin: { name: '签到通知', description: '影巢签到完成时发送通知', icon: '✅' },
            task_complete: { name: '任务通知', description: '海报生成等任务完成时发送通知', icon: '🎨' }
        });

        const templateLabels = {
            media_added: '入库通知模板',
            organize_complete: '整理通知模板',
            playback: '播放通知模板'
        };

        // 模板可用变量
        const templateVars = {
            media_added: ['title', 'year', 'media_type', 'library_name', 'rating', 'genres', 'overview', 'tagline', 'poster_url', 'now'],
            playback: ['title', 'year', 'original_name', 'media_type', 'rating', 'genres', 'overview', 'tagline', 'emby_name', 'user_name', 'client_info', 'now', 'poster_url'],
            organize_complete: ['title', 'year', 'media_type', 'season_episode', 'rating', 'genres', 'overview', 'tmdb_id', 'quality', 'video', 'audio', 'library_location', 'episode_count', 'episode_ranges', 'file_size', 'release_group', 'elapsed']
        };

        // 默认模板
        const defaultTemplates = {
            media_added: {
                title: '《{{ title }}》{% if year %}({{ year }}){% endif %} 已入库 ✅',
                text: '⭐️评分：{{ rating or \'暂无\' }} ｜ 🎬类型：{{ genres or media_type }}{% if tagline %}\n💬标语：{{ tagline }}{% endif %}\n\n📝简介：{{ overview or \'暂无简介\' }}\n\n📁媒体库：{{ library_name }} ｜ 🕐入库时间：{{ now }}'
            },
            playback: {
                title: '🎬 正在播放《{{ title }}》{% if year %}({{ year }}){% endif %}',
                text: '⭐️评分：{{ rating or \'暂无\' }} ｜ 🎬类型：{{ genres or media_type }}{% if tagline %}\n💬标语：{{ tagline }}{% endif %}\n\n👤用户：{{ user_name or \'未知\' }}\n🖥️服务器：{{ emby_name }} ｜ 📱客户端：{{ client_info or \'未知\' }}\n🕐时间：{{ now }}\n\n📝简介：{{ overview or \'暂无简介\' }}'
            },
            organize_complete: {
                title: '💿 整理完成 ✅ 《{{ title }}》{% if year %}({{ year }}){% endif %}{% if season_episode %} {{ season_episode }}{% endif %}',
                text: '⭐️评分：{{ rating or \'暂无\' }}\n🎬类型：{{ media_type }}{% if genres %} · {{ genres }}{% endif %}{% if quality %}\n💎画质：{{ quality }}{% endif %}{% if video %}\n🎞️视频：{{ video }}{% endif %}{% if audio %}\n🎵音频：{{ audio }}{% endif %}{% if library_location %}\n📁库位：{{ library_location }}{% endif %}{% if episode_count %}\n📖数量：{{ episode_count }} 集{% endif %}{% if episode_ranges %}\n📚集数：{{ episode_ranges }}{% endif %}{% if file_size %}\n⚖️大小：{{ file_size }}{% endif %}{% if tmdb_id %}\n🎬tmdbid：{{ tmdb_id }}{% endif %}{% if release_group %}\n👨\u200d🎨制作组：{{ release_group }}{% endif %}{% if elapsed %}\n⏱️整理耗时：{{ elapsed }}{% endif %}{% if overview %}\n\n📝简介：{{ overview }}{% endif %}'
            }
        };

        const createDefaultNotifyTypes = () => ({
            playback: true,
            media_added: true,
            organize_complete: true,
            resource_transfer: true,
            checkin: true,
            task_complete: true
        });

        const createDefaultTemplates = () => JSON.parse(JSON.stringify(defaultTemplates));

        const sanitizeTemplates = (templates) => {
            const result = {};
            for (const [key, tpl] of Object.entries(templates)) {
                result[key] = { title: tpl.title || '', text: tpl.text || '' };
            }
            return result;
        };

        const mergeNotifyConfig = (targetConfig, data, fields) => {
            targetConfig.enabled = data.enabled || false;
            fields.forEach((field) => {
                if (Array.isArray(targetConfig[field])) {
                    targetConfig[field] = Array.isArray(data[field]) ? data[field] : [];
                } else if (typeof targetConfig[field] === 'boolean') {
                    targetConfig[field] = !!data[field];
                } else {
                    targetConfig[field] = data[field] || '';
                }
            });
            targetConfig.notify_types = { ...createDefaultNotifyTypes(), ...(data.notify_types || {}) };
            const mergedTemplates = createDefaultTemplates();
            if (data.templates) {
                for (const key of Object.keys(defaultTemplates)) {
                    if (data.templates[key]) {
                        mergedTemplates[key] = { ...defaultTemplates[key], ...data.templates[key] };
                    }
                }
            }
            targetConfig.templates = mergedTemplates;
        };

        const buildNotifyPayload = (config, fields) => {
            const payload = {
                enabled: config.enabled,
                notify_types: config.notify_types,
                templates: sanitizeTemplates(config.templates)
            };
            fields.forEach((field) => {
                payload[field] = config[field];
            });
            return payload;
        };

        const toggleNotifyTypeFor = (config, typeKey) => {
            if (config.notify_types) {
                config.notify_types[typeKey] = !config.notify_types[typeKey];
            }
        };

        const resetNotifyTemplateFor = (config, tplKey) => {
            if (defaultTemplates[tplKey]) {
                config.templates[tplKey] = JSON.parse(JSON.stringify(defaultTemplates[tplKey]));
            }
        };

        const fetchNotifyConfig = async (endpoint, targetConfig, fields, errorMessage) => {
            try {
                const res = await axios.get(endpoint);
                mergeNotifyConfig(targetConfig, res.data, fields);
            } catch (e) {
                console.error(errorMessage, e);
            }
        };

        const saveNotifyConfig = async ({ savingRef, endpoint, config, fields, successMessage }) => {
            savingRef.value = true;
            try {
                await axios.post(endpoint, buildNotifyPayload(config, fields));
                showToast(successMessage, 'success');
                return true;
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
                return false;
            } finally {
                savingRef.value = false;
            }
        };

        const testNotifyConnection = async ({ testingRef, endpoint }) => {
            testingRef.value = true;
            try {
                const res = await axios.post(endpoint);
                if (res.data.status === 'ok') {
                    showToast('连接成功: ' + res.data.message, 'success');
                } else {
                    showToast('连接失败: ' + res.data.message, 'error');
                }
            } catch (e) {
                showToast('测试失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                testingRef.value = false;
            }
        };

        const sendNotifyTestMessage = async ({ sendingRef, endpoint }) => {
            sendingRef.value = true;
            try {
                const res = await axios.post(`${endpoint}?message=${encodeURIComponent('这是一条来自ChillPoster的测试消息')}`);
                if (res.data.status === 'ok') {
                    showToast('测试消息发送成功', 'success');
                } else {
                    showToast('发送失败: ' + res.data.message, 'error');
                }
            } catch (e) {
                showToast('发送失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                sendingRef.value = false;
            }
        };

        const testNotifyTemplate = async ({ templateTestingRef, saveEndpoint, testEndpoint, config, fields }) => {
            templateTestingRef.value = true;
            try {
                await axios.post(saveEndpoint, buildNotifyPayload(config, fields));
                const res = await axios.post(testEndpoint);
                if (res.data.status === 'ok') {
                    showToast('模板测试通知发送成功', 'success');
                } else {
                    showToast('模板测试失败: ' + res.data.message, 'error');
                }
            } catch (e) {
                showToast('模板测试失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                templateTestingRef.value = false;
            }
        };

        const wechatNotifyFields = ['name', 'channel_name', 'corp_id', 'app_secret', 'token', 'agent_id', 'proxy_url', 'encoding_aes_key', 'admin_whitelist'];
        const telegramNotifyFields = ['name', 'bot_token', 'chat_id', 'account_monitor_enabled', 'api_id', 'api_hash', 'phone', 'selected_dialogs', 'monitor_reply_enabled', 'transfer_dir_mode', 'transfer_dir'];

        const wechatNotifyConfig = reactive({
            enabled: false,
            name: '微信',
            channel_name: '',
            corp_id: '',
            app_secret: '',
            token: '',
            agent_id: '',
            proxy_url: '',
            encoding_aes_key: '',
            admin_whitelist: '',
            notify_types: createDefaultNotifyTypes(),
            templates: createDefaultTemplates(),
            showSecret: false,
            showToken: false,
            showAesKey: false
        });

        const wechatNotifyTesting = ref(false);
        const wechatNotifySending = ref(false);
        const wechatNotifySaving = ref(false);

        const fetchWechatNotifyConfig = async () => {
            await fetchNotifyConfig('/api/wechat-notify/config', wechatNotifyConfig, wechatNotifyFields, '获取微信通知配置失败');
        };

        const toggleNotifyType = (typeKey) => {
            toggleNotifyTypeFor(wechatNotifyConfig, typeKey);
        };

        const saveWechatNotifyConfig = async () => {
            await saveNotifyConfig({
                savingRef: wechatNotifySaving,
                endpoint: '/api/wechat-notify/config',
                config: wechatNotifyConfig,
                fields: wechatNotifyFields,
                successMessage: '微信通知配置已保存'
            });
            await saveGlobalSettings(false);
        };

        const testWechatNotify = async () => {
            await testNotifyConnection({
                testingRef: wechatNotifyTesting,
                endpoint: '/api/wechat-notify/test'
            });
        };

        const sendWechatTestMsg = async () => {
            await sendNotifyTestMessage({
                sendingRef: wechatNotifySending,
                endpoint: '/api/wechat-notify/send'
            });
        };

        const wechatTemplateTesting = ref(false);
        const testWechatTemplate = async () => {
            await testNotifyTemplate({
                templateTestingRef: wechatTemplateTesting,
                saveEndpoint: '/api/wechat-notify/config',
                testEndpoint: '/api/wechat-notify/test-template',
                config: wechatNotifyConfig,
                fields: wechatNotifyFields
            });
        };

        // ==========================================
        // Telegram 通知逻辑
        // ==========================================
        const telegramNotifyConfig = reactive({
            enabled: false,
            name: 'Telegram',
            bot_token: '',
            chat_id: '',
            account_monitor_enabled: false,
            api_id: '',
            api_hash: '',
            phone: '',
            selected_dialogs: [],
            monitor_reply_enabled: false,
            transfer_dir_mode: 'system',
            transfer_dir: '',
            notify_types: createDefaultNotifyTypes(),
            templates: createDefaultTemplates(),
            showToken: false,
            showHash: false,
            showApiId: false,
            showPhone: false
        });
        const telegramStatus = reactive({ authorized: false, monitor_running: false, user: null, message: '' });
        const telegramLoginForm = reactive({ code: '', password: '', showPassword: false });
        const telegramDialogs = ref([]);
        const telegramDialogSearch = ref('');
        const telegramDialogPickerOpen = ref(false);
        const telegramNotifyTesting = ref(false);
        const telegramNotifySending = ref(false);
        const telegramNotifySaving = ref(false);
        const telegramDialogsSaving = ref(false);
        const telegramSavedDialogsKey = ref('');
        const telegramCodeSending = ref(false);
        const telegramSigningIn = ref(false);
        const telegramLoggingOut = ref(false);
        const telegramDialogsLoading = ref(false);
        const telegramTransferDirBrowser = reactive({
            visible: false,
            loading: false,
            currentCid: '0',
            currentPath: '/',
            history: [],
            dirs: []
        });

        const telegramDialogsSelectionKey = (dialogs = []) => {
            if (!Array.isArray(dialogs)) return '[]';
            return JSON.stringify(dialogs.map(item => String(item?.id || '')).filter(Boolean).sort());
        };

        const markTelegramDialogsSaved = () => {
            telegramSavedDialogsKey.value = telegramDialogsSelectionKey(telegramNotifyConfig.selected_dialogs);
        };

        const fetchTelegramNotifyConfig = async () => {
            await fetchNotifyConfig('/api/telegram-notify/config', telegramNotifyConfig, telegramNotifyFields, '获取Telegram通知配置失败');
            if (!Array.isArray(telegramNotifyConfig.selected_dialogs)) telegramNotifyConfig.selected_dialogs = [];
            markTelegramDialogsSaved();
            await fetchTelegramStatus();
        };

        const toggleTelegramNotifyType = (typeKey) => {
            toggleNotifyTypeFor(telegramNotifyConfig, typeKey);
        };

        const saveTelegramNotifyConfig = async () => {
            const saved = await saveNotifyConfig({
                savingRef: telegramNotifySaving,
                endpoint: '/api/telegram-notify/config',
                config: telegramNotifyConfig,
                fields: telegramNotifyFields,
                successMessage: 'Telegram配置已保存'
            });
            if (saved) markTelegramDialogsSaved();
            await fetchTelegramStatus();
        };

        const saveTelegramTransferSettings = async () => {
            if (telegramNotifyConfig.transfer_dir_mode === 'custom' && !String(telegramNotifyConfig.transfer_dir || '').trim()) {
                showToast('请先选择或填写 Telegram 转存目录', 'error');
                return;
            }
            if (telegramNotifyConfig.transfer_dir_mode !== 'custom') {
                telegramNotifyConfig.transfer_dir = '';
            }
            const saved = await saveNotifyConfig({
                savingRef: telegramNotifySaving,
                endpoint: '/api/telegram-notify/config',
                config: telegramNotifyConfig,
                fields: telegramNotifyFields,
                successMessage: 'Telegram转存目录设置已保存'
            });
            if (saved) markTelegramDialogsSaved();
            await fetchTelegramStatus();
        };

        const testTelegramNotify = async () => {
            await testNotifyConnection({
                testingRef: telegramNotifyTesting,
                endpoint: '/api/telegram-notify/test'
            });
            await fetchTelegramStatus();
        };

        const applyTelegramStatus = (data) => {
            telegramStatus.authorized = !!data?.authorized;
            telegramStatus.monitor_running = !!data?.monitor_running;
            telegramStatus.user = data?.user || null;
            telegramStatus.message = data?.message || '';
        };

        const fetchTelegramStatus = async () => {
            try {
                const res = await axios.get('/api/telegram-notify/status');
                applyTelegramStatus(res.data || {});
            } catch (e) {
                applyTelegramStatus({ authorized: false, monitor_running: false, message: e.message });
            }
        };

        const sendTelegramLoginCode = async () => {
            if (!telegramNotifyConfig.api_id || !telegramNotifyConfig.api_hash || !telegramNotifyConfig.phone) {
                showToast('请先填写 API ID、API Hash 和手机号', 'error');
                return;
            }
            telegramCodeSending.value = true;
            try {
                const res = await axios.post('/api/telegram-notify/send-code', {
                    api_id: telegramNotifyConfig.api_id,
                    api_hash: telegramNotifyConfig.api_hash,
                    phone: telegramNotifyConfig.phone
                });
                showToast(res.data.message || (res.data.status === 'ok' ? '验证码已发送' : '发送失败'), res.data.status === 'ok' ? 'success' : 'error');
            } catch (e) {
                showToast('验证码发送失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                telegramCodeSending.value = false;
            }
        };

        const signInTelegramAccount = async () => {
            telegramSigningIn.value = true;
            try {
                const res = await axios.post('/api/telegram-notify/sign-in', {
                    code: telegramLoginForm.code,
                    password: telegramLoginForm.password
                });
                const status = res.data.status;
                if (status === 'ok') {
                    showToast(res.data.message || 'Telegram登录成功', 'success');
                    telegramLoginForm.code = '';
                    telegramLoginForm.password = '';
                    await fetchTelegramStatus();
                } else if (status === 'need_password') {
                    showToast(res.data.message || '请输入两步验证密码', 'info');
                } else {
                    showToast(res.data.message || '登录失败', 'error');
                }
            } catch (e) {
                showToast('登录失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                telegramSigningIn.value = false;
            }
        };

        const logoutTelegramAccount = async () => {
            telegramLoggingOut.value = true;
            try {
                const res = await axios.post('/api/telegram-notify/logout');
                showToast(res.data.message || '已退出Telegram登录', 'success');
                telegramDialogs.value = [];
                await fetchTelegramStatus();
            } catch (e) {
                showToast('退出失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                telegramLoggingOut.value = false;
            }
        };

        const fetchTelegramDialogs = async () => {
            telegramDialogsLoading.value = true;
            try {
                const res = await axios.get('/api/telegram-notify/dialogs');
                if (res.data.status === 'ok') {
                    telegramDialogs.value = res.data.dialogs || [];
                } else {
                    telegramDialogs.value = [];
                    showToast(res.data.message || '请先登录Telegram', 'info');
                }
            } catch (e) {
                showToast('加载群组/频道失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                telegramDialogsLoading.value = false;
            }
        };

        const openTelegramDialogPicker = async () => {
            telegramDialogPickerOpen.value = true;
            if (telegramStatus.authorized && telegramDialogs.value.length === 0) {
                await fetchTelegramDialogs();
            }
        };

        const loadTelegramTransferDir = async (cid = '0', path = '/') => {
            telegramTransferDirBrowser.loading = true;
            try {
                const res = await axios.post('/api/drive115_upload/browse115', { cid, drive_index: 0 });
                if (res.data?.status === 'ok') {
                    telegramTransferDirBrowser.dirs = res.data.dirs || [];
                    telegramTransferDirBrowser.currentCid = String(cid || '0');
                    telegramTransferDirBrowser.currentPath = path || '/';
                } else {
                    showToast(res.data?.message || '读取目录失败', 'error');
                }
            } catch (e) {
                showToast('浏览失败: ' + e.message, 'error');
            } finally {
                telegramTransferDirBrowser.loading = false;
            }
        };

        const browseTelegramTransferDir = () => {
            if (telegramTransferDirBrowser.visible) {
                telegramTransferDirBrowser.visible = false;
                return;
            }
            telegramTransferDirBrowser.visible = true;
            telegramTransferDirBrowser.history.splice(0);
            loadTelegramTransferDir('0', '/');
        };

        const selectTelegramTransferDir = (dir) => {
            telegramTransferDirBrowser.history.push({
                cid: telegramTransferDirBrowser.currentCid,
                path: telegramTransferDirBrowser.currentPath
            });
            const nextPath = telegramTransferDirBrowser.currentPath === '/' ? `/${dir.name}` : `${telegramTransferDirBrowser.currentPath}/${dir.name}`;
            loadTelegramTransferDir(dir.cid, nextPath);
        };

        const telegramTransferDirUp = () => {
            const prev = telegramTransferDirBrowser.history.pop();
            if (!prev) return;
            loadTelegramTransferDir(prev.cid, prev.path);
        };

        const applyTelegramTransferDir = () => {
            if (!telegramTransferDirBrowser.currentCid || telegramTransferDirBrowser.currentCid === '0') return showToast('不能选择根目录', 'error');
            telegramNotifyConfig.transfer_dir = telegramTransferDirBrowser.currentPath;
            telegramNotifyConfig.transfer_dir_mode = 'custom';
            telegramTransferDirBrowser.visible = false;
            telegramTransferDirBrowser.dirs = [];
            telegramTransferDirBrowser.history = [];
            showToast('已选择 Telegram 转存目录', 'success');
        };

        const isTelegramDialogSelected = (dialog) => {
            return telegramNotifyConfig.selected_dialogs.some(item => String(item.id) === String(dialog.id));
        };

        const saveTelegramDialogs = async (successMessage = '') => {
            telegramDialogsSaving.value = true;
            try {
                const res = await axios.post('/api/telegram-notify/dialogs', {
                    selected_dialogs: telegramNotifyConfig.selected_dialogs
                });
                if (Array.isArray(res.data?.selected_dialogs)) {
                    telegramNotifyConfig.selected_dialogs = res.data.selected_dialogs;
                }
                markTelegramDialogsSaved();
                await fetchTelegramStatus();
                if (successMessage) showToast(successMessage, 'success');
            } catch (e) {
                showToast('监听目标保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                telegramDialogsSaving.value = false;
            }
        };

        const toggleTelegramDialog = (dialog, event) => {
            const checked = !!event.target.checked;
            const id = String(dialog.id);
            if (checked && !isTelegramDialogSelected(dialog)) {
                    telegramNotifyConfig.selected_dialogs.push({
                        id,
                        title: dialog.title || '',
                        type: dialog.type || '',
                        username: dialog.username || '',
                        avatar_url: dialog.avatar_url || ''
                    });
            } else if (!checked) {
                telegramNotifyConfig.selected_dialogs = telegramNotifyConfig.selected_dialogs.filter(item => String(item.id) !== id);
            }
        };

        const selectedTelegramDialogs = computed(() => {
            if (!Array.isArray(telegramNotifyConfig.selected_dialogs)) return [];
            return telegramNotifyConfig.selected_dialogs.map((item) => {
                const matched = telegramDialogs.value.find(dialog => String(dialog.id) === String(item.id));
                return {
                    ...item,
                    title: item.title || matched?.title || `ID ${item.id}`,
                    type: item.type || matched?.type || '',
                    username: item.username || matched?.username || '',
                    avatar_url: item.avatar_url || matched?.avatar_url || ''
                };
            });
        });

        const removeTelegramSelectedDialog = (dialog) => {
            const id = String(dialog.id);
            telegramNotifyConfig.selected_dialogs = telegramNotifyConfig.selected_dialogs.filter(item => String(item.id) !== id);
        };

        const telegramDialogsDirty = computed(() => {
            return telegramDialogsSelectionKey(telegramNotifyConfig.selected_dialogs) !== telegramSavedDialogsKey.value;
        });

        const filteredTelegramDialogs = computed(() => {
            const query = telegramDialogSearch.value.trim().toLowerCase();
            if (!query) return telegramDialogs.value;
            return telegramDialogs.value.filter(dialog => {
                const haystack = `${dialog.title || ''} ${dialog.username || ''} ${dialog.type || ''}`.toLowerCase();
                return haystack.includes(query);
            });
        });

        const sendTelegramTestMsg = async () => {
            await sendNotifyTestMessage({
                sendingRef: telegramNotifySending,
                endpoint: '/api/telegram-notify/send'
            });
        };

        const telegramTemplateTesting = ref(false);
        const testTelegramTemplate = async () => {
            await testNotifyTemplate({
                templateTestingRef: telegramTemplateTesting,
                saveEndpoint: '/api/telegram-notify/config',
                testEndpoint: '/api/telegram-notify/test-template',
                config: telegramNotifyConfig,
                fields: telegramNotifyFields
            });
        };

        const resetWechatTemplate = (tplKey) => {
            resetNotifyTemplateFor(wechatNotifyConfig, tplKey);
        };

        const resetTelegramTemplate = (tplKey) => {
            resetNotifyTemplateFor(telegramNotifyConfig, tplKey);
        };

        const notificationChannels = computed(() => ([
            {
                key: 'telegram',
                title: 'Telegram 通知',
                iconClass: 'fa-brands fa-telegram icon-brand-telegram',
                config: telegramNotifyConfig,
                testing: telegramNotifyTesting,
                sending: telegramNotifySending,
                saving: telegramNotifySaving,
                templateTesting: telegramTemplateTesting,
                toggleType: toggleTelegramNotifyType,
                resetTemplate: resetTelegramTemplate,
                sendTest: sendTelegramTestMsg,
                testTemplate: testTelegramTemplate,
                save: saveTelegramNotifyConfig
            },
            {
                key: 'wechat',
                title: '微信通知',
                iconClass: 'fa-solid fa-comments icon-brand-wechat',
                config: wechatNotifyConfig,
                testing: wechatNotifyTesting,
                sending: wechatNotifySending,
                saving: wechatNotifySaving,
                templateTesting: wechatTemplateTesting,
                toggleType: toggleNotifyType,
                resetTemplate: resetWechatTemplate,
                sendTest: sendWechatTestMsg,
                testTemplate: testWechatTemplate,
                save: saveWechatNotifyConfig
            }
        ]));

        const wrapVar = (v) => '{{ ' + v + ' }}';

    return {
        notificationTypes,
        templateLabels,
        templateVars,
        wechatNotifyConfig,
        wechatNotifyTesting,
        wechatNotifySending,
        wechatNotifySaving,
        wechatTemplateTesting,
        fetchWechatNotifyConfig,
        saveWechatNotifyConfig,
        testWechatNotify,
        sendWechatTestMsg,
        testWechatTemplate,
        toggleNotifyType,
        resetWechatTemplate,
        resetTelegramTemplate,
        notificationChannels,
        telegramNotifyConfig,
        telegramStatus,
        telegramLoginForm,
        telegramDialogs,
        telegramDialogSearch,
        telegramDialogPickerOpen,
        selectedTelegramDialogs,
        filteredTelegramDialogs,
        telegramTransferDirBrowser,
        telegramNotifyTesting,
        telegramNotifySending,
        telegramNotifySaving,
        telegramTemplateTesting,
        telegramCodeSending,
        telegramSigningIn,
        telegramLoggingOut,
        telegramDialogsLoading,
        telegramDialogsSaving,
        telegramDialogsDirty,
        fetchTelegramNotifyConfig,
        saveTelegramNotifyConfig,
        testTelegramNotify,
        sendTelegramTestMsg,
        testTelegramTemplate,
        sendTelegramLoginCode,
        signInTelegramAccount,
        logoutTelegramAccount,
        fetchTelegramDialogs,
        openTelegramDialogPicker,
        isTelegramDialogSelected,
        toggleTelegramDialog,
        removeTelegramSelectedDialog,
        saveTelegramDialogs,
        toggleTelegramNotifyType,
        browseTelegramTransferDir,
        selectTelegramTransferDir,
        telegramTransferDirUp,
        applyTelegramTransferDir,
        saveTelegramTransferSettings,
        wrapVar,
    };
}
