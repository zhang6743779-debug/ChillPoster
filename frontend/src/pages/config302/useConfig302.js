import axios from 'axios';
import { computed, reactive, watch } from 'vue';

export function useConfig302({ tab, isMobile, jumpToItem, closeMobileMenu, syncServersFrom302, showToast, showConfirm, refreshLinkedConfigs }) {
        // ==========================================
        // [新增] 302 配置对象
        // ==========================================
        // ==========================================
        // [修改] 302 配置对象 (改为数组结构支持多配置)
        // ==========================================
        const config302 = reactive({
            drives: [],
            embys: [],
            standard_topology: null
        });

        const hasPrimary115Cookie = computed(() => {
            const drive = Array.isArray(config302.drives) ? config302.drives[0] : null;
            return !!String(drive?.cookie || '').trim();
        });

        const needs115Setup = computed(() => !hasPrimary115Cookie.value);
        const standardTopologyEnabled = computed(() => {
            const drive = Array.isArray(config302.drives) ? config302.drives[0] : null;
            if (drive && drive.enable_standard_topology === false) return false;
            return true;
        });

        const open115ConfigPanel = () => {
            if (isMobile.value) {
                tab.value = 'config_115';
                if (typeof closeMobileMenu === 'function') closeMobileMenu();
                return;
            }
            jumpToItem('config_115');
        };

        const notify115SetupRequired = () => {
            showToast('请先完成 115 配置', 'info');
        };

        // 定义默认模板
        const defaultDrive115 = {
            name: '',
            cookie: '',
            enable_sync: true,
            enable_rapid: false,
            enable_standard_topology: true,
            remote_root_name: '影视库',

            rapid_mode: 'auto',
            rapid_concurrency_limit: 0,
            rapid_accounts: [],

            auto_delete: true,
            delete_cron: '0 */2 * * *',
            recycle_code: '',
            upload_dir: '',
            status: 'unknown',
            login_app: '',
            login_app_label: '',
            testing: false,
            qr_loading: false
        };

        const qr115AppOptions = [
            { value: '115android', label: '115网盘(Android端)' },
            { value: 'web', label: '网页版' },
            { value: 'android', label: '115生活(Android端)' },
            { value: 'ios', label: '115生活(iOS端)' },
            { value: 'ipad', label: '115生活(iPad端)' },
            { value: '115ios', label: '115网盘(iOS端)' },
            { value: '115ipad', label: '115网盘(iPad端)' },
            { value: 'tv', label: '115生活(Android电视端)' },
            { value: 'apple_tv', label: '115生活(Apple TV端)' },
            { value: 'wechatmini', label: '115生活(微信小程序)' },
            { value: 'alipaymini', label: '115生活(支付宝小程序)' },
            { value: 'windows', label: '115生活(Windows端)' },
            { value: 'mac', label: '115生活(macOS端)' },
            { value: 'linux', label: '115生活(Linux端)' },
            { value: 'qandroid', label: '115管理(Android端)' },
            { value: 'qios', label: '115管理(iOS端)' },
            { value: 'qipad', label: '115管理(iPad端)' },
            { value: 'harmony', label: '115网盘(鸿蒙端)' }
        ];

        const qrcode115State = reactive({
            visible: false,
            driveIndex: -1,
            driveRef: null,
            app: '115android',
            appOptions: qr115AppOptions,
            loading: false,
            polling: false,
            token: null,
            qrcode: '',
            qrcodeUrl: '',
            status: 'idle',
            statusText: '',
            error: '',
            autoTest: true,
            pollTimer: null,
            resultFetching: false,
            mode: 'config',
            fetchedCookie: '',
            copied: false
        });

        const manual115CookieState = reactive({
            visible: false,
            driveIndex: -1,
            driveRef: null,
            value: '',
            saving: false,
            error: ''
        });

        const playbackTopology = reactive({
            loading: false,
            loaded: false,
            error: '',
            polling: false,
            updated_at: 0,
            total_sessions: 0,
            accounts: []
        });

        const defaultEmby302 = {
            name: '',
            url: '',
            key: '',
            public_host: '',
            proxy_port: '',
            drive_index: -1,
            modes: {
                pickcode: true
            },
            preload: true,
            enabled: true,
            status: 'unknown',
            testing: false
        };

        const ensureSingle302Drive = () => {
            if (!Array.isArray(config302.drives) || config302.drives.length === 0) {
                config302.drives = [JSON.parse(JSON.stringify(defaultDrive115))];
            } else {
                config302.drives = [ensure302DriveUiFields(config302.drives[0])];
            }
            return config302.drives[0];
        };

        const ensureSingle302Emby = () => {
            if (!Array.isArray(config302.embys) || config302.embys.length === 0) {
                config302.embys = [JSON.parse(JSON.stringify(defaultEmby302))];
            } else {
                const emby = JSON.parse(JSON.stringify(config302.embys[0] || defaultEmby302));
                normalizeEmbyModes(emby);
                emby.drive_index = 0;
                emby.enabled = true;
                emby.preload = true;
                config302.embys = [emby];
            }
            return config302.embys[0];
        };

        const add302Drive = () => ensureSingle302Drive();
        const remove302Drive = async () => showToast('已固定为单个主 115 配置', 'warning');
        const add302Emby = () => ensureSingle302Emby();
        const remove302Emby = async () => showToast('已固定为单个 Emby 配置', 'warning');

        const test115Cookie = async (drive) => {
            if (!drive.cookie) return showToast('请先填写 Cookie', 'error');

            drive.testing = true;
            drive.status = 'unknown';

            try {
                const res = await axios.post('/api/config_302/test_115', { cookie: drive.cookie });

                if (res.data.status === 'ok') {
                    drive.status = 'ok';
                    drive.login_app = res.data.login_app || '';
                    drive.login_app_label = res.data.login_app_label || '';
                    showToast(res.data.message, 'success');
                } else {
                    drive.status = 'error';
                    drive.login_app = '';
                    drive.login_app_label = '';
                    showToast(res.data.message, 'error');
                }
            } catch (e) {
                drive.status = 'error';
                drive.login_app = '';
                drive.login_app_label = '';
                showToast('请求失败: ' + (e.response?.data?.message || e.message), 'error');
            } finally {
                drive.testing = false;
            }
        };

        const clear115QrPollTimer = () => {
            if (qrcode115State.pollTimer) {
                clearTimeout(qrcode115State.pollTimer);
                qrcode115State.pollTimer = null;
            }
        };

        const reset115QrState = () => {
            clear115QrPollTimer();
            qrcode115State.loading = false;
            qrcode115State.polling = false;
            qrcode115State.token = null;
            qrcode115State.qrcode = '';
            qrcode115State.qrcodeUrl = '';
            qrcode115State.status = 'idle';
            qrcode115State.statusText = '';
            qrcode115State.error = '';
            qrcode115State.autoTest = true;
            qrcode115State.resultFetching = false;
            qrcode115State.mode = 'config';
            qrcode115State.fetchedCookie = '';
            qrcode115State.copied = false;
        };

        const mark115QrDriveLoading = (loading) => {
            const drive = qrcode115State.driveRef;
            if (drive) {
                drive.qr_loading = !!loading;
            }
        };

        const close115QrLogin = () => {
            mark115QrDriveLoading(false);
            qrcode115State.visible = false;
            qrcode115State.driveRef = null;
            qrcode115State.driveIndex = -1;
            reset115QrState();
        };

        const fetch115QrResult = async () => {
            if (!qrcode115State.visible || !qrcode115State.token?.uid || qrcode115State.resultFetching) return;
            qrcode115State.resultFetching = true;
            qrcode115State.loading = true;
            qrcode115State.polling = false;
            qrcode115State.status = 'confirmed';
            qrcode115State.statusText = '扫码确认成功，正在获取 Cookie';
            qrcode115State.error = '';
            clear115QrPollTimer();

            try {
                const res = await axios.post('/api/config_302/115_qrcode/result', {
                    uid: qrcode115State.token.uid,
                    app: qrcode115State.app
                });
                if (res.data.status !== 'ok' || !res.data.cookie) {
                    qrcode115State.status = 'error';
                    qrcode115State.statusText = '获取 Cookie 失败';
                    qrcode115State.error = res.data.message || '未能提取 Cookie';
                    showToast(qrcode115State.error, 'error');
                    return;
                }

                if (qrcode115State.mode === 'tool' || !qrcode115State.driveRef) {
                    qrcode115State.fetchedCookie = res.data.cookie;
                    qrcode115State.copied = false;
                    qrcode115State.status = 'success';
                    qrcode115State.statusText = '扫码成功，CK 已获取';
                    qrcode115State.polling = false;
                    showToast('扫码成功，CK 已获取', 'success');
                    return;
                }

                qrcode115State.driveRef.cookie = res.data.cookie;
                qrcode115State.driveRef.status = 'unknown';
                qrcode115State.status = 'success';
                qrcode115State.statusText = '扫码登录成功，Cookie 已写入';

                const payload = build302Payload();
                try {
                    await axios.post('/api/config_302/save', payload);
                    showToast('扫码登录成功，Cookie 已写入后台配置', 'success');
                } catch (saveError) {
                    qrcode115State.status = 'error';
                    qrcode115State.statusText = '保存 Cookie 失败';
                    qrcode115State.error = saveError.response?.data?.detail || saveError.response?.data?.message || saveError.message;
                    showToast('Cookie 已获取，但保存后台配置失败: ' + qrcode115State.error, 'error');
                    return;
                }

                if (qrcode115State.autoTest) {
                    await test115Cookie(qrcode115State.driveRef);
                }

                close115QrLogin();
            } catch (e) {
                qrcode115State.status = 'error';
                qrcode115State.statusText = '获取 Cookie 失败';
                qrcode115State.error = e.response?.data?.message || e.response?.data?.detail || e.message;
                showToast('获取扫码结果失败: ' + qrcode115State.error, 'error');
            } finally {
                qrcode115State.loading = false;
                qrcode115State.resultFetching = false;
                if (qrcode115State.visible && qrcode115State.status !== 'success') {
                    mark115QrDriveLoading(false);
                }
            }
        };

        const poll115QrStatus = async () => {
            if (!qrcode115State.visible || !qrcode115State.token) return;
            qrcode115State.polling = true;
            try {
                const res = await axios.post('/api/config_302/115_qrcode/status', qrcode115State.token);
                if (res.data.status !== 'ok') {
                    qrcode115State.status = 'error';
                    qrcode115State.statusText = '查询扫码状态失败';
                    qrcode115State.error = res.data.message || '状态查询失败';
                    qrcode115State.polling = false;
                    mark115QrDriveLoading(false);
                    return;
                }

                const scanStatus = res.data.scan_status || 'error';
                qrcode115State.status = scanStatus;
                qrcode115State.statusText = res.data.message || '';
                qrcode115State.error = '';

                if (scanStatus === 'confirmed') {
                    await fetch115QrResult();
                    return;
                }

                if (scanStatus === 'expired' || scanStatus === 'cancelled' || scanStatus === 'error') {
                    qrcode115State.polling = false;
                    mark115QrDriveLoading(false);
                    return;
                }

                qrcode115State.pollTimer = setTimeout(poll115QrStatus, 2500);
            } catch (e) {
                qrcode115State.status = 'error';
                qrcode115State.statusText = '查询扫码状态失败';
                qrcode115State.error = e.response?.data?.message || e.response?.data?.detail || e.message;
                qrcode115State.polling = false;
                mark115QrDriveLoading(false);
            }
        };

        const create115QrCode = async () => {
            if (!qrcode115State.visible) return;
            qrcode115State.loading = true;
            qrcode115State.error = '';
            qrcode115State.status = 'loading';
            qrcode115State.statusText = '正在生成二维码...';
            qrcode115State.qrcode = '';
            qrcode115State.qrcodeUrl = '';
            qrcode115State.token = null;
            qrcode115State.fetchedCookie = '';
            qrcode115State.copied = false;
            clear115QrPollTimer();
            mark115QrDriveLoading(true);

            try {
                const res = await axios.post('/api/config_302/115_qrcode/start', { app: qrcode115State.app });
                if (res.data.status !== 'ok') {
                    qrcode115State.status = 'error';
                    qrcode115State.statusText = '生成二维码失败';
                    qrcode115State.error = res.data.message || '生成二维码失败';
                    mark115QrDriveLoading(false);
                    return;
                }
                qrcode115State.token = res.data.token;
                qrcode115State.qrcode = res.data.qrcode || '';
                qrcode115State.qrcodeUrl = res.data.qrcode_url || '';
                qrcode115State.status = 'waiting';
                qrcode115State.statusText = '请使用 115 App 扫描二维码';
                qrcode115State.polling = true;
                qrcode115State.pollTimer = setTimeout(poll115QrStatus, 1500);
            } catch (e) {
                qrcode115State.status = 'error';
                qrcode115State.statusText = '生成二维码失败';
                qrcode115State.error = e.response?.data?.message || e.response?.data?.detail || e.message;
                mark115QrDriveLoading(false);
            } finally {
                qrcode115State.loading = false;
            }
        };

        const open115QrLogin = (drive, idx) => {
            reset115QrState();
            qrcode115State.visible = true;
            qrcode115State.driveRef = drive;
            qrcode115State.driveIndex = idx;
            qrcode115State.mode = 'config';
            qrcode115State.app = '115android';
            drive.qr_loading = false;
        };

        const open115CkTool = () => {
            reset115QrState();
            qrcode115State.visible = true;
            qrcode115State.driveRef = null;
            qrcode115State.driveIndex = -1;
            qrcode115State.mode = 'tool';
            qrcode115State.app = '115android';
            qrcode115State.statusText = '选择客户端后生成二维码，扫码确认即可获取 CK';
        };

        const copy115FetchedCookie = async () => {
            const cookie = String(qrcode115State.fetchedCookie || '');
            if (!cookie) return;
            try {
                await navigator.clipboard.writeText(cookie);
                qrcode115State.copied = true;
                showToast('CK 已复制', 'success');
            } catch (e) {
                showToast('复制失败，请手动选择 CK', 'error');
            }
        };

        const openManual115CookieDialog = (drive, idx) => {
            manual115CookieState.visible = true;
            manual115CookieState.driveRef = drive;
            manual115CookieState.driveIndex = idx;
            manual115CookieState.value = drive?.cookie || '';
            manual115CookieState.error = '';
            manual115CookieState.saving = false;
        };

        const closeManual115CookieDialog = () => {
            if (manual115CookieState.saving) return;
            manual115CookieState.visible = false;
            manual115CookieState.driveRef = null;
            manual115CookieState.driveIndex = -1;
            manual115CookieState.value = '';
            manual115CookieState.error = '';
        };

        const saveManual115Cookie = async () => {
            const drive = manual115CookieState.driveRef;
            const cookie = String(manual115CookieState.value || '').trim().replace(/;+$/, '');
            if (!drive) return;
            if (!cookie) {
                manual115CookieState.error = '请粘贴 115 Cookie';
                showToast('请粘贴 115 Cookie', 'warning');
                return;
            }

            manual115CookieState.saving = true;
            manual115CookieState.error = '';
            drive.cookie = cookie;
            drive.status = 'unknown';
            drive.login_app = '';
            drive.login_app_label = '';

            try {
                await axios.post('/api/config_302/save', build302Payload());
                showToast('Cookie 已写入后台配置', 'success');
                manual115CookieState.saving = false;
                closeManual115CookieDialog();
                await test115Cookie(drive);
            } catch (e) {
                manual115CookieState.error = e.response?.data?.detail || e.response?.data?.message || e.message;
                showToast('保存 Cookie 失败: ' + manual115CookieState.error, 'error');
            } finally {
                manual115CookieState.saving = false;
            }
        };

        // 手动清理 115 目录和回收站
        const manualCleanup115 = async (drive, driveIndex, accountType, accountIndex) => {
            const accountTypeName = accountType === 'main' ? '主号' : '小号';
            const ok = await showConfirm(
                `手动清理 ${accountTypeName}`,
                `确定要清理 ${accountTypeName} 的秒传目录和回收站吗？此操作不可撤销！`,
                'warning'
            );
            if (!ok) return;

            // 设置 cleaning 状态
            const target = accountType === 'main' ? drive : drive.rapid_accounts[accountIndex];
            target.cleaning = true;

            try {
                const res = await axios.post('/api/config_302/manual_cleanup', {
                    drive_index: 0,
                    account_type: accountType,
                    account_index: accountIndex
                });

                if (res.data.status === 'ok') {
                    showToast(res.data.message, 'success');
                } else {
                    showToast(res.data.message, 'error');
                }
            } catch (e) {
                showToast('清理失败: ' + (e.response?.data?.message || e.message), 'error');
            } finally {
                target.cleaning = false;
            }
        };

        const normalizeEmbyModes = (emby) => {
            if (!emby.modes || typeof emby.modes !== 'object') {
                emby.modes = { pickcode: true };
                return;
            }
            emby.modes = {
                pickcode: emby.modes.pickcode !== undefined ? !!emby.modes.pickcode : true
            };
        };

        const ensure302DriveUiFields = (drive) => ({
            ...drive,
            enable_standard_topology: true,
            remote_root_name: drive?.remote_root_name || '影视库',
            rapid_concurrency_limit: Math.max(0, parseInt(drive?.rapid_concurrency_limit ?? 0, 10) || 0),
            testing: false,
            qr_loading: false,
            status: drive?.status || 'unknown',
            login_app: drive?.login_app || '',
            login_app_label: drive?.login_app_label || ''
        });

        const build302Payload = () => {
            const drive = JSON.parse(JSON.stringify(ensureSingle302Drive()));
            delete drive.qr_loading;
            drive.transfer_drive_index = 0;
            drive.enable_standard_topology = true;
            drive.rapid_concurrency_limit = Math.max(0, parseInt(drive.rapid_concurrency_limit ?? 0, 10) || 0);
            drive.rapid_accounts = Array.isArray(drive.rapid_accounts)
                ? drive.rapid_accounts.map((acc) => ({
                    name: acc?.name || '',
                    cookie: acc?.cookie || '',
                    recycle_code: acc?.recycle_code || ''
                }))
                : [];
            const sourceEmby = ensureSingle302Emby();
            const modes = sourceEmby.modes || {};
            const emby = {
                name: sourceEmby.name || '',
                url: sourceEmby.url || '',
                key: sourceEmby.key || '',
                public_host: sourceEmby.public_host || '',
                proxy_port: sourceEmby.proxy_port || '',
                modes: {
                    pickcode: modes.pickcode !== undefined ? !!modes.pickcode : true
                },
                preload: true,
                rapid_play: !!sourceEmby.rapid_play,
                enabled: true,
                drive_index: 0,
            };
            return { drives: [drive], embys: [emby] };
        };

        // 获取 302 配置 (兼容旧数据结构)
        const fetch302Config = async () => {
            try {
                const res = await axios.get('/api/config_302/get');
                if (res.data) {
                    const rawDrives = Array.isArray(res.data.drives)
                        ? res.data.drives
                        : (res.data.drive ? [res.data.drive] : []);
                    const rawEmbys = Array.isArray(res.data.embys)
                        ? res.data.embys
                        : (res.data.emby ? [res.data.emby] : []);
                    config302.drives = rawDrives.slice(0, 1).map(ensure302DriveUiFields);
                    config302.embys = rawEmbys.slice(0, 1);
                    config302.standard_topology = res.data.standard_topology || null;
                    ensureSingle302Drive();
                    ensureSingle302Emby();
                    syncServersFrom302();
                }
                fetchPlaybackTopology();
            } catch (e) {
                ensureSingle302Drive();
                ensureSingle302Emby();
                syncServersFrom302();
            }
        };

        const fetchPlaybackTopology = async () => {
            playbackTopology.loading = true;
            playbackTopology.error = '';
            try {
                const res = await axios.get('/api/config_302/playback_topology');
                const data = res.data || {};
                const nextAccounts = Array.isArray(data.accounts) ? data.accounts : [];
                playbackTopology.loaded = true;
                playbackTopology.polling = !!data.polling;
                playbackTopology.updated_at = data.updated_at || 0;
                playbackTopology.total_sessions = data.total_sessions || 0;
                playbackTopology.accounts = nextAccounts;
            } catch (e) {
                playbackTopology.error = e.response?.data?.detail || e.message;
            } finally {
                playbackTopology.loading = false;
            }
        };

        const formatTopologyUpdatedAt = (timestamp) => {
            if (!timestamp) return '尚未刷新';
            const date = new Date(Number(timestamp) * 1000);
            if (Number.isNaN(date.getTime())) return '尚未刷新';
            return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        };

        let topologyRefreshTimer = null;
        const clearTopologyRefreshTimer = () => {
            if (topologyRefreshTimer) {
                clearTimeout(topologyRefreshTimer);
                topologyRefreshTimer = null;
            }
        };
        const scheduleTopologyRefresh = () => {
            clearTopologyRefreshTimer();
            if (tab.value !== 'config_115') return;
            const delay = (playbackTopology.polling || playbackTopology.total_sessions) ? 5000 : 10000;
            topologyRefreshTimer = setTimeout(async () => {
                await fetchPlaybackTopology();
                scheduleTopologyRefresh();
            }, delay);
        };

        watch(() => tab.value, async (value) => {
            if (value === 'config_115') {
                await fetchPlaybackTopology();
                scheduleTopologyRefresh();
            } else {
                clearTopologyRefreshTimer();
            }
        });

        let topologyEventSource = null;
        let topologyEventRefreshTimer = null;
        const schedulePlaybackTopologyEventRefresh = () => {
            if (tab.value !== 'config_115') return;
            if (topologyEventRefreshTimer) clearTimeout(topologyEventRefreshTimer);
            topologyEventRefreshTimer = setTimeout(async () => {
                topologyEventRefreshTimer = null;
                await fetchPlaybackTopology();
                scheduleTopologyRefresh();
            }, 250);
        };
        const setupPlaybackTopologyEvents = () => {
            if (topologyEventSource || typeof EventSource === 'undefined') return;
            topologyEventSource = new EventSource('/api/config_302/events');
            topologyEventSource.addEventListener('playback_topology_updated', schedulePlaybackTopologyEventRefresh);
            topologyEventSource.onerror = () => {
                topologyEventSource.close();
                topologyEventSource = null;
                setTimeout(setupPlaybackTopologyEvents, 5000);
            };
        };
        setupPlaybackTopologyEvents();

        // 保存 302 配置
        const save302Config = async () => {
            // 保存前先获取服务端当前配置，用于检测端口变更
            let oldPort = '';
            try {
                const res = await axios.get('/api/config_302/get');
                const oldEmbys = Array.isArray(res.data?.embys)
                    ? res.data.embys
                    : (res.data?.emby ? [res.data.emby] : []);
                oldPort = String(oldEmbys[0]?.proxy_port || '').trim();
            } catch (e) { /* 忽略 */ }

            try {
                const payload = build302Payload();
                const saveRes = await axios.post('/api/config_302/save', payload);
                if (saveRes.data?.standard_topology) {
                    config302.standard_topology = saveRes.data.standard_topology;
                    if (typeof refreshLinkedConfigs === 'function') {
                        refreshLinkedConfigs();
                    }
                }
                showToast(saveRes.data?.message || '302 配置已保存', 'success');
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
                return;
            }

            // 检测端口是否变更
            const newPort = String((config302.embys[0]?.proxy_port || '')).trim();
            const portChanged = oldPort !== '' && newPort !== oldPort;
            if (portChanged) {
                const confirmed = await showConfirm('需要重启', '网关端口号已变更，重启后生效。是否现在重启？', 'warning');
                if (confirmed) {
                    try {
                        await axios.post('/api/server/restart');
                        showToast('正在重启...', 'info');
                    } catch (e) {
                        showToast('重启请求失败: ' + e.message, 'error');
                    }
                }
            }
        };

        const saveEmbyConfig = async () => {
            const sourceEmby = ensureSingle302Emby();
            const modes = sourceEmby.modes || {};
            const emby = {
                name: sourceEmby.name || '',
                url: sourceEmby.url || '',
                key: sourceEmby.key || '',
                public_host: sourceEmby.public_host || '',
                proxy_port: sourceEmby.proxy_port || '',
                modes: { pickcode: modes.pickcode !== undefined ? !!modes.pickcode : true },
                preload: true,
                rapid_play: !!sourceEmby.rapid_play,
                enabled: true,
                drive_index: 0,
            };
            try {
                const saveRes = await axios.post('/api/config_302/save_emby', { embys: [emby] });
                showToast(saveRes.data?.message || 'Emby 配置已保存', 'success');
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const toggle302Switch = async (event, obj, field) => {
            const newState = event.target.checked;
            const oldState = obj[field];
            obj[field] = newState;
            try {
                ensureSingle302Emby();
                const payload = build302Payload();
                await axios.post('/api/config_302/save', payload);
                showToast('配置已保存', 'success');
            } catch (e) {
                obj[field] = oldState;
                event.target.checked = oldState;
                showToast('状态切换失败', 'error');
            }
        };


    return {
        config302,
        hasPrimary115Cookie,
        needs115Setup,
        standardTopologyEnabled,
        open115ConfigPanel,
        notify115SetupRequired,
        qrcode115State,
        manual115CookieState,
        add302Drive,
        remove302Drive,
        add302Emby,
        remove302Emby,
        test115Cookie,
        close115QrLogin,
        create115QrCode,
        open115QrLogin,
        open115CkTool,
        copy115FetchedCookie,
        openManual115CookieDialog,
        closeManual115CookieDialog,
        saveManual115Cookie,
        manualCleanup115,
        playbackTopology,
        fetchPlaybackTopology,
        formatTopologyUpdatedAt,
        build302Payload,
        fetch302Config,
        save302Config,
        saveEmbyConfig,
        toggle302Switch,
    };
}
