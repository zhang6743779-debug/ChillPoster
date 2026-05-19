import axios from 'axios';
import { computed, reactive } from 'vue';

export function useDockerManager({ tab, projectVersion, showToast, showConfirm }) {
    const upgradeStatus = reactive({
        loading: false,
        checking: false,
        upgrading: false,
        waitingRestart: false,
        enabled: true,
        available: false,
        mode: 'docker',
        selected_mode: 'docker',
        current_version: '',
        latest_version: '',
        update_available: false,
        image: '',
        docker_available: false,
        container_id: '',
        message: ''
    });
    const dockerManager = reactive({
        activeTab: 'containers',
        loading: false,
        actionLoading: '',
        logsLoading: false,
        imagePulling: false,
        updateChecking: false,
        pruneLoading: false,
        status: { available: false, message: '' },
        containers: [],
        images: [],
        updateMap: {},
        imageDrafts: {},
        search: '',
        imageSearch: '',
        imageFilter: 'all',
        pullImage: '',
        selectedContainer: null,
        logs: '',
        logsTail: 200,
        lastRefreshAt: 0,
        lastUpdateCheckAt: 0,
        updateDialog: {
            visible: false,
            runId: '',
            title: '',
            status: '',
            percent: 0,
            stepNo: 0,
            totalSteps: 6,
            message: '',
            logs: [],
            polling: false,
            image: '',
            originalImage: '',
            selfUpdate: false,
            restartSeen: false,
            restartStartedAt: 0,
        },
        versionDialog: {
            visible: false,
            container: null,
            value: '',
        },
    });
    const DOCKER_UPDATE_CACHE_KEY = 'chillposter-docker-update-cache';
    const DOCKER_UPDATE_CACHE_TTL_MS = 30 * 60 * 1000;
    let dockerSilentRefreshTimer = null;
    let dockerUpdatePollTimer = null;

    const loadProjectVersion = async () => {
        try {
            const res = await axios.get('/api/version');
            if (res.data?.version) projectVersion.value = res.data.version;
        } catch (e) { }
    };

    const fetchUpgradeStatus = async (force = false) => {
        upgradeStatus.loading = true;
        try {
            const res = force
                ? await axios.post('/api/upgrade/check', { force: true })
                : await axios.get('/api/upgrade/status');
            Object.assign(upgradeStatus, res.data || {});
        } catch (e) {
            upgradeStatus.available = false;
            upgradeStatus.message = e.response?.data?.detail || e.message || '升级状态获取失败';
        } finally {
            upgradeStatus.loading = false;
        }
    };

    const checkUpgrade = async () => {
        upgradeStatus.checking = true;
        try {
            await fetchUpgradeStatus(true);
            showToast(upgradeStatus.latest_version ? '版本检查完成' : '无法获取最新版本', upgradeStatus.latest_version ? 'success' : 'info');
        } finally {
            upgradeStatus.checking = false;
        }
    };

    const waitForUpgradeRestart = async (previousVersion, runId) => {
        upgradeStatus.waitingRestart = true;
        const startedAt = Date.now();
        let sawDisconnect = false;
        let taskFinished = false;
        let restartPhase = false;
        while (Date.now() - startedAt < 10 * 60 * 1000) {
            await new Promise(resolve => setTimeout(resolve, 3000));
            try {
                const progress = await axios.get('/api/progress', { timeout: 5000 });
                const task = runId ? progress.data?.[runId] : null;
                if (task?.status === 'error') {
                    upgradeStatus.waitingRestart = false;
                    upgradeStatus.upgrading = false;
                    showToast(task.detail?.message || '升级任务失败', 'error');
                    return;
                }
                if (task?.status === 'finished') taskFinished = true;
                if ((task?.percent || 0) >= 85 || ['recreate', 'restarting', 'done'].includes(task?.detail?.step)) {
                    restartPhase = true;
                }
            } catch (e) { }

            try {
                const res = await axios.get('/api/version', { timeout: 5000 });
                const nextVersion = res.data?.version || '';
                if (!nextVersion) continue;
                if (nextVersion !== previousVersion || sawDisconnect || taskFinished) {
                    projectVersion.value = nextVersion;
                    upgradeStatus.waitingRestart = false;
                    upgradeStatus.upgrading = false;
                    await fetchUpgradeStatus(true);
                    showToast(nextVersion !== previousVersion ? `升级完成: ${nextVersion}` : '服务已恢复，版本未变化', nextVersion !== previousVersion ? 'success' : 'info');
                    return;
                }
                if (!restartPhase) continue;
            } catch (e) {
                sawDisconnect = true;
                restartPhase = true;
            }
        }
        upgradeStatus.waitingRestart = false;
        upgradeStatus.upgrading = false;
        showToast('升级命令已发出，但等待服务恢复超时，请检查容器日志', 'error');
    };

    const startUpgrade = async () => {
        const ok = await showConfirm('系统升级', '将拉取最新镜像并使用 Docker 直接模式重建当前容器，页面会短暂断开。确定继续吗？', 'warning');
        if (!ok) return;
        upgradeStatus.upgrading = true;
        const previousVersion = projectVersion.value;
        try {
            const res = await axios.post('/api/upgrade/start', {});
            showToast(res.data?.message || '升级任务已启动', 'info');
            waitForUpgradeRestart(previousVersion, res.data?.run_id || '');
        } catch (e) {
            upgradeStatus.upgrading = false;
            showToast('升级启动失败: ' + (e.response?.data?.detail || e.message), 'error');
        }
    };

    const formatDockerBytes = (size) => {
        const value = Number(size || 0);
        if (value < 1024) return `${value} B`;
        if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
        if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
        return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
    };

    const formatDockerDate = (value) => {
        const ts = Number(value || 0);
        if (!ts) return '--';
        return new Date(ts * 1000).toLocaleString();
    };

    const isDockerImageUntagged = (image) => {
        return image?.name === '<none>:<none>' || !(image?.tags || []).length;
    };

    const dockerImageTagLabel = (image) => {
        if (isDockerImageUntagged(image)) return '无 Tag';
        const tag = (image?.tags || [])[0] || '';
        return tag.split(':').pop() || '无 Tag';
    };

    const filteredDockerContainers = computed(() => {
        const q = dockerManager.search.trim().toLowerCase();
        if (!q) return dockerManager.containers;
        return dockerManager.containers.filter(item =>
            [item.name, item.image, item.short_id, item.state, item.status].some(v => String(v || '').toLowerCase().includes(q))
        );
    });

    const filteredDockerImages = computed(() => {
        const filter = dockerManager.imageFilter || 'all';
        return dockerManager.images.filter(item => {
            const containers = Number(item.containers);
            const untagged = isDockerImageUntagged(item);
            if (filter === 'used') return containers > 0;
            if (filter === 'unused') return containers === 0;
            if (filter === 'untagged') return untagged;
            return true;
        });
    });

    const dockerUpdateCount = computed(() => dockerManager.containers.filter(item => {
        const info = dockerManager.updateMap[item.image];
        return !!info?.update_available;
    }).length);

    const dockerImageStats = computed(() => {
        const total = dockerManager.images.length;
        const unused = dockerManager.images.filter(item => Number(item.containers) === 0).length;
        const untagged = dockerManager.images.filter(item => isDockerImageUntagged(item)).length;
        const used = Math.max(0, total - unused);
        return { total, used, unused, untagged };
    });

    const setDockerImageFilter = (filter) => {
        dockerManager.imageFilter = filter || 'all';
    };

    const syncDockerImageDrafts = () => {
        const next = {};
        for (const item of dockerManager.containers) {
            next[item.id] = dockerManager.imageDrafts[item.id] || item.image || '';
        }
        dockerManager.imageDrafts = next;
    };

    const saveDockerUpdateCache = () => {
        try {
            localStorage.setItem(DOCKER_UPDATE_CACHE_KEY, JSON.stringify({
                updatedAt: dockerManager.lastUpdateCheckAt || Date.now(),
                updateMap: dockerManager.updateMap || {},
            }));
        } catch (e) {}
    };

    const restoreDockerUpdateCache = () => {
        try {
            const raw = localStorage.getItem(DOCKER_UPDATE_CACHE_KEY);
            if (!raw) return false;
            const payload = JSON.parse(raw);
            const updatedAt = Number(payload?.updatedAt || 0);
            const updateMap = payload?.updateMap;
            if (!updatedAt || !updateMap || typeof updateMap !== 'object') {
                localStorage.removeItem(DOCKER_UPDATE_CACHE_KEY);
                return false;
            }
            dockerManager.updateMap = updateMap;
            dockerManager.lastUpdateCheckAt = updatedAt;
            return true;
        } catch (e) {
            try { localStorage.removeItem(DOCKER_UPDATE_CACHE_KEY); } catch (_) {}
            return false;
        }
    };

    const pruneDockerUpdateCacheForContainers = () => {
        const images = new Set(dockerManager.containers.map(item => item.image).filter(Boolean));
        if (!images.size || !dockerManager.updateMap || typeof dockerManager.updateMap !== 'object') return;
        const next = {};
        for (const [image, info] of Object.entries(dockerManager.updateMap)) {
            if (images.has(image)) next[image] = info;
        }
        if (Object.keys(next).length !== Object.keys(dockerManager.updateMap).length) {
            dockerManager.updateMap = next;
            saveDockerUpdateCache();
        }
    };

    const clearDockerUpdateForImages = (images = []) => {
        const targets = [...new Set((images || []).map(item => String(item || '').trim()).filter(Boolean))];
        if (!targets.length || !dockerManager.updateMap || typeof dockerManager.updateMap !== 'object') return;
        let changed = false;
        const next = { ...dockerManager.updateMap };
        for (const image of targets) {
            if (Object.prototype.hasOwnProperty.call(next, image)) {
                delete next[image];
                changed = true;
            }
        }
        if (changed) {
            dockerManager.updateMap = next;
            dockerManager.lastUpdateCheckAt = Date.now();
            saveDockerUpdateCache();
        }
    };

    const fetchDockerStatus = async () => {
        try {
            const res = await axios.get('/api/docker/status');
            dockerManager.status = res.data || { available: false, message: '' };
        } catch (e) {
            dockerManager.status = { available: false, message: e.response?.data?.detail || e.message || 'Docker 状态获取失败' };
        }
    };

    const fetchDockerContainers = async (options = {}) => {
        const silent = !!options.silent;
        if (!silent) dockerManager.loading = true;
        try {
            await fetchDockerStatus();
            const res = await axios.get('/api/docker/containers');
            dockerManager.containers = res.data?.containers || [];
            syncDockerImageDrafts();
            pruneDockerUpdateCacheForContainers();
            dockerManager.lastRefreshAt = Date.now();
            if (!options.skipAutoCheck && (options.checkUpdates || (!dockerManager.lastUpdateCheckAt || Date.now() - dockerManager.lastUpdateCheckAt > DOCKER_UPDATE_CACHE_TTL_MS))) {
                checkDockerUpdates({ silent: true });
            }
        } catch (e) {
            if (!silent) showToast('获取容器失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            if (!silent) dockerManager.loading = false;
        }
    };

    const fetchDockerImages = async (options = {}) => {
        const silent = !!options.silent;
        if (!silent) dockerManager.loading = true;
        try {
            const res = await axios.get('/api/docker/images');
            dockerManager.images = res.data?.images || [];
        } catch (e) {
            if (!silent) showToast('获取镜像失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            if (!silent) dockerManager.loading = false;
        }
    };

    const refreshDockerManager = async () => {
        if (dockerManager.activeTab === 'images') {
            await fetchDockerImages();
        } else {
            if (!dockerManager.lastUpdateCheckAt) restoreDockerUpdateCache();
            await fetchDockerContainers();
        }
    };

    const checkDockerUpdates = async (options = {}) => {
        const silent = !!options.silent;
        const images = [...new Set(dockerManager.containers.map(item => item.image).filter(Boolean))];
        if (!images.length) return;
        if (!silent) dockerManager.updateChecking = true;
        try {
            const res = await axios.post('/api/docker/containers/check_updates', { images });
            dockerManager.updateMap = res.data?.images || {};
            dockerManager.lastUpdateCheckAt = Date.now();
            saveDockerUpdateCache();
            if (!silent) {
                const failedCount = Object.values(dockerManager.updateMap).filter(item => item?.message).length;
                const updateCount = dockerUpdateCount.value;
                if (failedCount) {
                    const firstFailure = Object.values(dockerManager.updateMap).find(item => item?.message);
                    const reason = firstFailure?.message ? `：${firstFailure.message}` : '';
                    showToast(`检查完成：${updateCount} 个有更新，${failedCount} 个检查失败${reason}`, updateCount > 0 ? 'warning' : 'error');
                } else {
                    showToast(updateCount > 0 ? `发现 ${updateCount} 个容器镜像有更新` : '容器镜像均为最新', updateCount > 0 ? 'success' : 'info');
                }
            }
        } catch (e) {
            if (!silent) showToast('检查更新失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            if (!silent) dockerManager.updateChecking = false;
        }
    };

    const startDockerSilentRefresh = () => {
        if (dockerSilentRefreshTimer) return;
        dockerSilentRefreshTimer = setInterval(() => {
            if (tab.value !== 'docker_manager') return;
            if (dockerManager.activeTab === 'images') {
                fetchDockerImages({ silent: true });
            } else {
                fetchDockerContainers({ silent: true });
            }
        }, 5 * 60 * 1000);
    };

    const stopDockerSilentRefresh = () => {
        if (dockerSilentRefreshTimer) {
            clearInterval(dockerSilentRefreshTimer);
            dockerSilentRefreshTimer = null;
        }
    };

    const applyDockerUpdateTask = (task) => {
        dockerManager.updateDialog.status = task.status || '';
        dockerManager.updateDialog.percent = Number(task.percent || 0);
        dockerManager.updateDialog.stepNo = Number(task.step_no || 0);
        dockerManager.updateDialog.totalSteps = Number(task.total_steps || 6);
        dockerManager.updateDialog.message = task.message || '';
        dockerManager.updateDialog.logs = Array.isArray(task.logs) ? task.logs : [];
        dockerManager.updateDialog.selfUpdate = !!task.self_update;
        if (task.status === 'restarting' || task.step === 'helper' || task.percent >= 85) {
            dockerManager.updateDialog.restartSeen = true;
            if (!dockerManager.updateDialog.restartStartedAt) dockerManager.updateDialog.restartStartedAt = Date.now();
        }
        if (task.container_name) {
            dockerManager.updateDialog.title = `更新容器 ${task.container_name}`;
        }
    };

    const stopDockerUpdatePolling = () => {
        if (dockerUpdatePollTimer) {
            clearInterval(dockerUpdatePollTimer);
            dockerUpdatePollTimer = null;
        }
        dockerManager.updateDialog.polling = false;
    };

    const pollDockerUpdateTask = async (runId) => {
        if (!runId) return;
        try {
            const res = await axios.get(`/api/docker/update_tasks/${encodeURIComponent(runId)}`);
            const task = res.data || {};
            applyDockerUpdateTask(task);
            if (['finished', 'error'].includes(task.status)) {
                stopDockerUpdatePolling();
                if (task.status === 'finished') {
                    clearDockerUpdateForImages([
                        task.image,
                        task.result?.image,
                        dockerManager.updateDialog.image,
                        dockerManager.updateDialog.originalImage,
                    ]);
                    await fetchDockerContainers({ skipAutoCheck: true });
                }
            }
        } catch (e) {
            if (dockerManager.updateDialog.selfUpdate) {
                const status = e.response?.status;
                const restartStartedAt = dockerManager.updateDialog.restartStartedAt || Date.now();
                dockerManager.updateDialog.restartStartedAt = restartStartedAt;
                dockerManager.updateDialog.restartSeen = true;
                if (status === 404 && Date.now() - restartStartedAt > 5000) {
                    dockerManager.updateDialog.status = 'finished';
                    dockerManager.updateDialog.percent = 100;
                    dockerManager.updateDialog.message = '服务已恢复，容器更新完成';
                    dockerManager.updateDialog.logs.push({
                        time: new Date().toLocaleTimeString(),
                        level: 'success',
                        message: '服务已恢复，容器更新完成',
                    });
                    stopDockerUpdatePolling();
                    clearDockerUpdateForImages([
                        dockerManager.updateDialog.image,
                        dockerManager.updateDialog.originalImage,
                    ]);
                    await fetchDockerContainers({ skipAutoCheck: true });
                    return;
                }
                if (Date.now() - restartStartedAt < 10 * 60 * 1000) {
                    dockerManager.updateDialog.status = 'restarting';
                    dockerManager.updateDialog.percent = Math.max(dockerManager.updateDialog.percent || 0, 90);
                    dockerManager.updateDialog.message = '服务正在重启，等待恢复连接';
                    return;
                }
            }
            dockerManager.updateDialog.status = 'error';
            dockerManager.updateDialog.message = e.response?.data?.detail || e.message || '更新任务状态获取失败';
            dockerManager.updateDialog.logs.push({
                time: new Date().toLocaleTimeString(),
                level: 'error',
                message: dockerManager.updateDialog.message,
            });
            stopDockerUpdatePolling();
        }
    };

    const openDockerUpdateDialog = (container, runId, image) => {
        stopDockerUpdatePolling();
        Object.assign(dockerManager.updateDialog, {
            visible: true,
            runId,
            title: `更新容器 ${container.name}`,
            status: 'running',
            percent: 1,
            stepNo: 0,
            totalSteps: 6,
            message: '更新任务已启动',
            image,
            originalImage: container.image || '',
            logs: [{
                time: new Date().toLocaleTimeString(),
                level: 'info',
                message: `准备使用镜像 ${image} 更新 ${container.name}`,
            }],
            polling: true,
            selfUpdate: false,
            restartSeen: false,
            restartStartedAt: 0,
        });
        pollDockerUpdateTask(runId);
        dockerUpdatePollTimer = setInterval(() => pollDockerUpdateTask(runId), 1500);
    };

    const closeDockerUpdateDialog = () => {
        if (dockerManager.updateDialog.status !== 'running') {
            dockerManager.updateDialog.visible = false;
            stopDockerUpdatePolling();
        }
    };

    const openDockerVersionDialog = (container) => {
        dockerManager.versionDialog.visible = true;
        dockerManager.versionDialog.container = container;
        dockerManager.versionDialog.value = dockerManager.imageDrafts[container.id] || container.image || '';
    };

    const closeDockerVersionDialog = () => {
        dockerManager.versionDialog.visible = false;
        dockerManager.versionDialog.container = null;
        dockerManager.versionDialog.value = '';
    };

    const saveDockerVersionDialog = () => {
        const container = dockerManager.versionDialog.container;
        const image = (dockerManager.versionDialog.value || '').trim();
        if (!container) return;
        if (!image) {
            showToast('请填写要使用的镜像版本', 'warning');
            return;
        }
        dockerManager.imageDrafts[container.id] = image;
        closeDockerVersionDialog();
        showToast(`已保存 ${container.name} 的镜像版本`, 'success');
    };

    const runDockerContainerAction = async (container, action) => {
        const actionLabel = { start: '启动', stop: '停止', restart: '重启', remove: '删除', update: '更新镜像并重建' }[action] || action;
        if (['stop', 'restart', 'remove', 'update'].includes(action)) {
            const ok = await showConfirm('Docker 管理', `确定要${actionLabel}容器「${container.name}」吗？`, action === 'remove' ? 'danger' : 'warning');
            if (!ok) return;
        }
        let image = '';
        if (action === 'update') {
            image = (dockerManager.imageDrafts[container.id] || container.image || '').trim();
            if (!image) return;
        }
        dockerManager.actionLoading = `${action}:${container.id}`;
        try {
            const res = await axios.post(`/api/docker/containers/${encodeURIComponent(container.id)}/action`, {
                action,
                force: action === 'remove',
                image,
            });
            showToast(res.data?.message || `${actionLabel}已完成`, 'success');
            if (action === 'update' && res.data?.run_id) {
                openDockerUpdateDialog(container, res.data.run_id, image);
            } else {
                await fetchDockerContainers({ checkUpdates: action === 'update' });
            }
        } catch (e) {
            showToast(`${actionLabel}失败: ` + (e.response?.data?.detail || e.message), 'error');
        } finally {
            dockerManager.actionLoading = '';
        }
    };

    const openDockerLogs = async (container) => {
        dockerManager.selectedContainer = container;
        dockerManager.logs = '';
        dockerManager.activeTab = 'logs';
        dockerManager.logsLoading = true;
        try {
            const res = await axios.get(`/api/docker/containers/${encodeURIComponent(container.id)}/logs`, {
                params: { tail: dockerManager.logsTail || 200 }
            });
            dockerManager.logs = res.data?.logs || '';
        } catch (e) {
            dockerManager.logs = e.response?.data?.detail || e.message || '日志获取失败';
        } finally {
            dockerManager.logsLoading = false;
        }
    };

    const closeDockerLogs = () => {
        dockerManager.selectedContainer = null;
        dockerManager.logs = '';
        dockerManager.activeTab = 'containers';
    };

    const pullDockerImage = async () => {
        const image = dockerManager.pullImage.trim();
        if (!image) return showToast('请填写镜像名称', 'error');
        dockerManager.imagePulling = true;
        try {
            const res = await axios.post('/api/docker/images/pull', { image });
            showToast(res.data?.message || '镜像拉取完成', 'success');
            dockerManager.pullImage = '';
            await fetchDockerImages();
        } catch (e) {
            showToast('拉取失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            dockerManager.imagePulling = false;
        }
    };

    const deleteDockerImage = async (image) => {
        const ok = await showConfirm('删除镜像', `确定要删除镜像「${image.name}」吗？若被容器占用会失败。`, 'danger');
        if (!ok) return;
        dockerManager.actionLoading = `image:${image.id}`;
        try {
            await axios.delete(`/api/docker/images/${encodeURIComponent(image.id)}`);
            showToast('镜像已删除', 'success');
            await fetchDockerImages();
        } catch (e) {
            showToast('删除镜像失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            dockerManager.actionLoading = '';
        }
    };

    const pruneUnusedDockerImages = async () => {
        const ok = await showConfirm('删除未使用镜像', `将删除当前未被容器使用的镜像，预计 ${dockerImageStats.value.unused} 个。确定继续吗？`, 'danger');
        if (!ok) return;
        dockerManager.pruneLoading = true;
        try {
            const res = await axios.post('/api/docker/images/prune_unused');
            const reclaimed = formatDockerBytes(res.data?.space_reclaimed || 0);
            showToast(`已清理未使用镜像，释放 ${reclaimed}`, 'success');
            await fetchDockerImages();
        } catch (e) {
            showToast('清理失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            dockerManager.pruneLoading = false;
        }
    };

    const pruneUntaggedDockerImages = async () => {
        const ok = await showConfirm('删除无 Tag 镜像', `将删除当前无 Tag 镜像，预计 ${dockerImageStats.value.untagged} 个。确定继续吗？`, 'danger');
        if (!ok) return;
        dockerManager.pruneLoading = true;
        try {
            const res = await axios.post('/api/docker/images/prune_untagged');
            const reclaimed = formatDockerBytes(res.data?.space_reclaimed || 0);
            showToast(`已清理无 Tag 镜像，释放 ${reclaimed}`, 'success');
            await fetchDockerImages();
        } catch (e) {
            showToast('清理无 Tag 失败: ' + (e.response?.data?.detail || e.message), 'error');
        } finally {
            dockerManager.pruneLoading = false;
        }
    };

    return {
        upgradeStatus,
        loadProjectVersion,
        fetchUpgradeStatus,
        checkUpgrade,
        startUpgrade,
        dockerManager,
        filteredDockerContainers,
        filteredDockerImages,
        dockerUpdateCount,
        dockerImageStats,
        fetchDockerStatus,
        fetchDockerContainers,
        fetchDockerImages,
        refreshDockerManager,
        checkDockerUpdates,
        startDockerSilentRefresh,
        stopDockerSilentRefresh,
        stopDockerUpdatePolling,
        runDockerContainerAction,
        openDockerLogs,
        closeDockerLogs,
        pullDockerImage,
        deleteDockerImage,
        pruneUnusedDockerImages,
        pruneUntaggedDockerImages,
        setDockerImageFilter,
        closeDockerUpdateDialog,
        openDockerVersionDialog,
        closeDockerVersionDialog,
        saveDockerVersionDialog,
        formatDockerBytes,
        formatDockerDate,
        isDockerImageUntagged,
        dockerImageTagLabel,
    };
}
