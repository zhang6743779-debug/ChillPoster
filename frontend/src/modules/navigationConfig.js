export const dockItems = [
    { id: 'media_subscribe', icon: 'fa-compass', label: '发现推荐', group: '网盘一条龙' },
    { id: 'missing_episode_stats', icon: 'fa-list-check', label: '缺集统计', group: '网盘一条龙' },
    { id: 'organize_history', icon: 'fa-clock-rotate-left', label: '整理记录', group: '网盘一条龙' },
];

export const embyTasksDockItem = { id: 'emby_tasks', icon: 'fa-bolt-lightning', label: 'Emby任务中心', group: '工具箱' };
export const dockerDockItem = { id: 'docker_manager', icon: 'fa-cubes', label: 'Docker管理', group: '工具箱' };
export const utilityDockItems = [embyTasksDockItem, dockerDockItem];

export const storageItems = [
    { id: 'resource_transfer', icon: 'fa-cloud-arrow-down', label: '资源转存', group: '网盘一条龙' },
    { id: 'media_organize', icon: 'fa-box-archive', label: '一条龙菜单', group: '网盘一条龙' },
    { id: 'rename_template', icon: 'fa-pen-fancy', label: '重命名模板', group: '网盘一条龙' },
    { id: 'media_organize_rules', icon: 'fa-sitemap', label: '二级分类', group: '网盘一条龙' },
];

export const coverItems = [
    { id: 'manual', icon: 'fa-pen-ruler', label: '手动封面', group: '封面系统' },
    { id: 'custom', icon: 'fa-paintbrush', label: '封面设计', group: '封面系统' },
    { id: 'auto', icon: 'fa-robot', label: '自动封面', group: '封面系统' },
    { id: 'library_preview', icon: 'fa-images', label: '封面备份', group: '封面系统' },
    { id: 'fonts', icon: 'fa-font', label: '字体管理', group: '封面系统' },
    { id: 'templates', icon: 'fa-swatchbook', label: '模板管理', group: '封面系统' },
    { id: 'translations', icon: 'fa-language', label: '翻译配置', group: '封面系统' },
];

export const toolboxItems = [
    { id: 'drive115_ck_tool', icon: 'fa-qrcode', label: '扫码获取115CK', group: '工具箱', action: 'open115CkTool' },
    { id: 'real_library', icon: 'fa-hard-drive', label: '独立真实库', group: '工具箱' },
    { id: 'rss', icon: 'fa-rss', label: 'RSS真实库', group: '工具箱' },
    { id: 'drive115_cleanup', icon: 'fa-broom', label: '云盘定时清空', group: '工具箱' },
    { id: 'drive115_upload', icon: 'fa-cloud-arrow-up', label: '云盘上传监听', group: '工具箱' },
    { id: 'organize_monitor_dirs', icon: 'fa-folder-plus', label: '整理监控目录', group: '工具箱' },
    { id: 'forward_aiying', icon: 'fa-tower-broadcast', label: 'Forward模块', group: '工具箱' },
    { id: 'webhook', icon: 'fa-bolt-lightning', label: 'Webhook', group: '工具箱' },
];

export const settingsItems = [
    { id: 'server', icon: 'fa-server', label: 'Emby 配置', group: '核心配置' },
    { id: 'config_115', icon: 'fa-cloud', label: '云盘配置', group: '核心配置' },
    { id: 'telegram_monitor', icon: 'fa-satellite-dish', label: 'Telegram 监听', group: '核心配置' },
    { id: 'config_notification', icon: 'fa-bell', label: '通知配置', group: '核心配置' },
    { id: 'config_moviepilot', icon: 'fa-plane', label: 'MoviePilot', group: '核心配置' },
    { id: 'config_proxy', icon: 'fa-globe', label: '代理配置', group: '核心配置' },
    { id: 'config_tmdb', icon: 'fa-database', label: 'TMDB 配置', group: '核心配置' },
    { id: 'upgrade', icon: 'fa-cloud-arrow-up', label: '系统升级', group: '核心配置' },
    { id: 'account', icon: 'fa-user-gear', label: '账户管理', group: '核心配置' },
];

export const allSearchItems = [
    { id: 'dashboard', icon: 'fa-house', label: '仪表盘', group: '首页' },
    ...dockItems,
    ...storageItems,
    ...coverItems,
    ...toolboxItems.filter(item => !item.action),
    ...utilityDockItems,
    ...settingsItems,
];

export const allValidTabs = new Set([
    'dashboard',
    'manual',
    'custom',
    'auto',
    'library_preview',
    'fonts',
    'templates',
    'translations',
    'emby_tasks',
    'real_library',
    'rss',
    'docker_manager',
    'drive115_cleanup',
    'drive115_upload',
    'organize_monitor_dirs',
    'forward_aiying',
    'webhook',
    'media_subscribe',
    'missing_episode_stats',
    'resource_transfer',
    'media_organize',
    'organize_history',
    'rename_template',
    'media_organize_rules',
    'server',
    'config_115',
    'telegram_monitor',
    'config_notification',
    'config_moviepilot',
    'config_proxy',
    'config_tmdb',
    'upgrade',
    'account',
]);

export const getPanelIcon = (id) => {
    const item = allSearchItems.find((entry) => entry.id === id);
    return item ? item.icon : 'fa-circle';
};

export const getPanelLabel = (id) => {
    const item = allSearchItems.find((entry) => entry.id === id);
    return item ? item.label : id;
};
