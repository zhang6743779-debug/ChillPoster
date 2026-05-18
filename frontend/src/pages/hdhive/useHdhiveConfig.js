import axios from 'axios';
import { reactive, ref } from 'vue';

export function useHdhiveConfig({ showToast, showConfirm }) {
    const hdhiveConfig = reactive({
        accounts: []
    });
    const hdhiveChecking = ref(false);

    const fetchHdhiveConfig = async () => {
        try {
            const res = await axios.get('/api/hdhive/config');
            Object.assign(hdhiveConfig, res.data);
            hdhiveConfig.accounts.forEach(acc => {
                if (acc.showPassword === undefined) acc.showPassword = false;
                if (acc.showToken === undefined) acc.showToken = false;
                if (acc.showApiKey === undefined) acc.showApiKey = false;
                if (acc.saving === undefined) acc.saving = false;
                if (acc.checkin_type === undefined) {
                    if (acc.auto_checkin === true) {
                        acc.checkin_type = 'normal';
                    } else {
                        acc.checkin_type = 'none';
                    }
                    delete acc.auto_checkin;
                }
                if (!acc.checkin_cron || acc.checkin_cron === '0 8 * * *') {
                    acc.checkin_cron = '1 0 * * *';
                }
                if (acc.expanded === undefined) acc.expanded = !acc.user_info;
            });
        } catch (e) {
            console.error('加载影巢配置失败:', e);
        }
    };

    const saveHdhiveAccount = async (accountId) => {
        const account = hdhiveConfig.accounts.find(a => a.id === accountId);
        if (!account) return;

        account.saving = true;
        try {
            await axios.post('/api/hdhive/account/update?account_id=' + accountId, {
                name: account.name,
                password: account.password,
                token: account.token,
                api_key: account.api_key,
                enabled: account.enabled,
                checkin_type: account.checkin_type,
                checkin_cron: account.checkin_cron
            });

            if (account.password && !account.token) {
                try {
                    const loginRes = await axios.post('/api/hdhive/login', { account_id: accountId });
                    if (loginRes.data.status === 'ok') {
                        account.token = loginRes.data.token;
                    }
                } catch (e) {
                    console.log('自动获取Token失败:', e);
                }
            }

            if (account.token) {
                try {
                    await axios.post('/api/hdhive/user-info', { account_id: accountId });
                } catch (e) {
                    console.log('自动获取用户信息失败:', e);
                }
            }

            if (account.api_key) {
                try {
                    await axios.post('/api/hdhive/usage', { account_id: accountId });
                } catch (e) {
                    console.log('自动获取用量信息失败:', e);
                }
            }

            await fetchHdhiveConfig();

            account.expanded = false;
            showToast('账号配置已保存', 'success');
        } catch (e) {
            showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            account.saving = false;
        }
    };

    const toggleHdhiveCheckin = async (accountId, enabled) => {
        const account = hdhiveConfig.accounts.find(a => a.id === accountId);
        if (!account) return;
        const newType = enabled ? 'normal' : 'none';
        account.checkin_type = newType;
        try {
            await axios.post('/api/hdhive/account/update?account_id=' + accountId, {
                checkin_type: newType,
                checkin_cron: account.checkin_cron
            });
        } catch (e) {
            showToast('保存签到设置失败', 'error');
            account.checkin_type = enabled ? 'none' : 'normal';
        }
    };

    const addHdhiveAccount = async () => {
        try {
            const res = await axios.post('/api/hdhive/account/add', {
                name: '',
                password: '',
                token: ''
            });
            const newAccount = res.data.account;
            newAccount.showPassword = false;
            newAccount.showToken = false;
            newAccount.showApiKey = false;
            newAccount.saving = false;
            newAccount.checkin_type = newAccount.checkin_type || 'none';
            newAccount.checkin_cron = '1 0 * * *';
            newAccount.expanded = true;
            hdhiveConfig.accounts.push(newAccount);
            showToast('账号已添加', 'success');
        } catch (e) {
            showToast('添加失败', 'error');
        }
    };

    const removeHdhiveAccount = async (accountId) => {
        const ok = await showConfirm('删除账号', '确定删除此影巢账号吗？', 'danger');
        if (!ok) return;
        try {
            await axios.post('/api/hdhive/account/remove?account_id=' + accountId);
            const idx = hdhiveConfig.accounts.findIndex(a => a.id === accountId);
            if (idx > -1) hdhiveConfig.accounts.splice(idx, 1);
            showToast('账号已删除', 'success');
        } catch (e) {
            showToast('删除失败', 'error');
        }
    };

    const testHdhiveAccount = async (accountId) => {
        const account = hdhiveConfig.accounts.find(a => a.id === accountId);
        if (!account) return;
        account.testing = true;
        try {
            await axios.post('/api/hdhive/account/update?account_id=' + accountId, {
                name: account.name,
                password: account.password,
                token: account.token
            });
            const res = await axios.post('/api/hdhive/account/test', { account_id: accountId });
            if (res.data.success) {
                account.status = 'ok';
                showToast(res.data.message || '连接成功', 'success');
            } else {
                account.status = 'error';
                showToast(res.data.message || '连接失败', 'error');
            }
        } catch (e) {
            account.status = 'error';
            showToast('测试失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            account.testing = false;
        }
    };

    const loginHdhive = async (accountId) => {
        const account = hdhiveConfig.accounts.find(a => a.id === accountId);
        if (!account) return;
        if (!account.name || !account.password) {
            return showToast('请先填写账号和密码', 'error');
        }
        account.logging = true;
        try {
            const res = await axios.post('/api/hdhive/login', { account_id: accountId });
            if (res.data.status === 'ok') {
                account.token = res.data.token;
                account.status = 'ok';
                showToast('Token 获取成功', 'success');
            } else {
                showToast(res.data.message || '登录失败', 'error');
                if (res.data.hint) {
                    console.log('提示:', res.data.hint);
                }
            }
        } catch (e) {
            showToast('登录失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            account.logging = false;
        }
    };

    const checkinHdhive = async (accountId) => {
        const account = hdhiveConfig.accounts.find(a => a.id === accountId);
        if (!account) return;
        if (!account.token) {
            return showToast('请先获取 Token', 'error');
        }
        account.checking = true;
        try {
            await axios.post('/api/hdhive/account/update?account_id=' + accountId, {
                token: account.token
            });
            const res = await axios.post('/api/hdhive/checkin', { account_id: accountId });
            if (res.data.success) {
                if (!res.data.already_checked_in) {
                    account.checkin_count = (account.checkin_count || 0) + 1;
                }
                account.last_checkin = new Date().toLocaleString();
                showToast(res.data.message || '签到成功', 'success');
            } else {
                showToast(res.data.message || '签到失败', 'error');
            }
        } catch (e) {
            showToast('签到失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            account.checking = false;
        }
    };

    const gamblerCheckinHdhive = async (accountId) => {
        const account = hdhiveConfig.accounts.find(a => a.id === accountId);
        if (!account) return;
        if (!account.token) {
            return showToast('请先获取 Token', 'error');
        }
        account.gambler_checking = true;
        try {
            const res = await axios.post('/api/hdhive/gambler-checkin', { account_id: accountId });
            if (res.data.success) {
                if (!res.data.already_checked_in) {
                    account.checkin_count = (account.checkin_count || 0) + 1;
                }
                account.last_checkin = new Date().toLocaleString();
                showToast(res.data.message || '赌狗签到成功', 'success');
                await fetchHdhiveConfig();
            } else {
                showToast(res.data.message || '赌狗签到失败', 'error');
            }
        } catch (e) {
            showToast('赌狗签到失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            account.gambler_checking = false;
        }
    };

    const checkinAllHdhive = async () => {
        hdhiveChecking.value = true;
        try {
            const res = await axios.post('/api/hdhive/checkin', {});
            const results = res.data.results || [];
            const successCount = results.filter(r => r.success).length;
            showToast(`签到完成: ${successCount}/${results.length} 成功`, successCount > 0 ? 'success' : 'error');
            await fetchHdhiveConfig();
        } catch (e) {
            showToast('批量签到失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            hdhiveChecking.value = false;
        }
    };

    const refreshHdhiveUserInfo = async (accountId) => {
        const account = hdhiveConfig.accounts.find(a => a.id === accountId);
        if (!account) return;
        account.refreshingInfo = true;
        try {
            const res = await axios.post('/api/hdhive/user-info', { account_id: accountId });
            if (res.data.status === 'ok') {
                account.user_info = res.data.user_info;
                showToast('获取用户信息成功', 'success');
            } else {
                showToast(res.data.message || '获取用户信息失败', 'error');
            }
        } catch (e) {
            showToast('获取用户信息失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            account.refreshingInfo = false;
        }
    };

    const refreshHdhiveUsage = async (accountId) => {
        const account = hdhiveConfig.accounts.find(a => a.id === accountId);
        if (!account) return;
        account.refreshingUsage = true;
        try {
            const res = await axios.post('/api/hdhive/usage', { account_id: accountId });
            if (res.data.status === 'ok') {
                account.usage = res.data.usage;

                if (res.data.vip_required) {
                    showToast('API用量已更新（详细用户信息需要VIP会员）', 'success');
                } else {
                    if (res.data.user_detail && account.user_info) {
                        const detail = res.data.user_detail;
                        account.user_info.id = detail.id;
                        account.user_info.nickname = detail.nickname;
                        account.user_info.username = detail.username;
                        account.user_info.email = detail.email;
                        account.user_info.avatar_url = detail.avatar_url;
                        account.user_info.is_vip = detail.is_vip;
                        account.user_info.vip_expiration_date = detail.vip_expiration_date;
                        account.user_info.last_active_at = detail.last_active_at;
                        account.user_info.created_at = detail.created_at;
                        account.user_info.telegram_user = detail.telegram_user;
                        account.user_info.points = detail.points;
                        account.user_info.signin_days_total = detail.signin_days_total;
                        account.user_info.share_num = detail.share_num;
                        account.user_info.is_activate = detail.is_activate;
                        account.user_info.notification_method = detail.notification_method;
                    }
                    showToast('获取用量信息成功', 'success');
                }
            } else {
                showToast(res.data.message || '获取用量信息失败', 'error');
            }
        } catch (e) {
            showToast('获取用量信息失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            account.refreshingUsage = false;
        }
    };

    return {
        hdhiveConfig,
        hdhiveChecking,
        fetchHdhiveConfig,
        saveHdhiveAccount,
        toggleHdhiveCheckin,
        addHdhiveAccount,
        removeHdhiveAccount,
        testHdhiveAccount,
        loginHdhive,
        checkinHdhive,
        gamblerCheckinHdhive,
        checkinAllHdhive,
        refreshHdhiveUserInfo,
        refreshHdhiveUsage,
    };
}
