import axios from 'axios';
import { computed, nextTick, reactive, ref, shallowRef, watch } from 'vue';

export function useConsoleLogs({ showToast }) {
    const consoleLogState = reactive({
        visible: false,
        content: '',
        autoRefresh: true,
        loading: false,
        streaming: false,
        levelFilter: 'INFO',
        categoryFilter: 'ALL',
        keywordFilter: '',
        keywordInput: '',
        maxLines: 1000,
        lastEventId: null,
        partialLineBuffer: '',
    });
    const parsedLogs = shallowRef([]);

    const normalizeLogLevel = (level) => {
        const raw = String(level || 'INFO').toUpperCase();
        if (raw === 'WARN') return 'WARNING';
        if (raw === 'ERR') return 'ERROR';
        if (!['INFO', 'DEBUG', 'WARNING', 'ERROR'].includes(raw)) return 'INFO';
        return raw;
    };

    const decorateLogLevel = (level) => {
        if (level === 'ERROR') return { icon: 'fa-times', statusClass: 'error', badgeClass: 'error' };
        if (level === 'WARNING') return { icon: 'fa-exclamation', statusClass: 'warning', badgeClass: 'warning' };
        if (level === 'DEBUG') return { icon: 'fa-bug', statusClass: 'debug', badgeClass: 'debug' };
        return { icon: 'fa-check', statusClass: 'success', badgeClass: 'info' };
    };

    const pickLogEmoji = (message, level = 'INFO') => {
        const normalizedLevel = normalizeLogLevel(level);
        if (normalizedLevel === 'ERROR') return '❌';
        if (normalizedLevel === 'WARNING') return '⚠️';
        if (normalizedLevel === 'DEBUG') return '🐞';

        const text = String(message || '').toLowerCase();

        const rules = [
            { test: /webhook|回调/, emoji: '🪝' },
            { test: /telegram|polling|getupdates/, emoji: '📨' },
            { test: /rss|订阅/, emoji: '📰' },
            { test: /定时任务|scheduler|cron/, emoji: '🔄' },
            { test: /播放|并发|锁定用户/, emoji: '🛰️' },
            { test: /115|life|网盘|转存/, emoji: '☁️' },
            { test: /清理|删除/, emoji: '🧹' },
            { test: /代理|proxy/, emoji: '🌐' },
            { test: /启动|初始化|已启动|已加载|恢复/, emoji: '🚀' },
            { test: /完成|成功|finished|ok/, emoji: '✅' },
            { test: /跳过/, emoji: '⏭️' }
        ];

        for (const rule of rules) {
            if (rule.test.test(text)) return rule.emoji;
        }
        return '📝';
    };

    const LOG_CATEGORY_KEYWORDS = {
        PLAYBACK_302: ['播放信息接口触发预加载', '后台预加载成功', '后台预加载失败', 'Pickcode模式检测', '从Path提取Pickcode成功', 'Pickcode提取成功', '开始获取直链', '直链获取成功', '命中直链缓存', '收到播放请求', '302重定向到115直链', '收到 STRM 直连请求', 'STRM 302重定向到115直链', '播放通知去重', '115直链获取失败，已降级反向代理', 'STRM 直链获取失败，已降级反向代理'],
        MEDIA_ORGANIZE: ['[MediaOrganize]', '[媒体库缓存]', '[Wash]', '[CategoryDir]', '[EmbyLib]', '整理:', '洗版'],
        DRIVE_115: ['[115]', '[115-', '[115Life]', '[Rapid]', '[Sync-', '[115风控', '网盘'],
        STRM: ['[STRM]', 'STRM', 'strm'],
        NOTIFY: ['微信', 'wechat', 'Telegram', 'telegram', '通知'],
        SCHEDULER: ['[Scheduler]', '定时任务', '任务', 'cron'],
        DIAGNOSTIC: ['失败', '异常', '超时', '990009', '风控', 'Traceback', '错误'],
        TMDB_SCRAPE: ['TMDb', 'TMDB', '刮削', '元数据', '图片下载'],
    };

    const detectLogCategory = (message) => {
        const text = String(message || '');
        for (const [category, keywords] of Object.entries(LOG_CATEGORY_KEYWORDS)) {
            if (keywords.some(keyword => text.includes(keyword))) return category;
        }
        return 'ALL';
    };

    const parseLogLine = (line) => {
        if (!line || !line.trim()) return null;

        const parts = line.split(' - ');
        let timestamp = '';
        let level = 'INFO';
        let message = line.trim();

        if (parts.length >= 3) {
            const timeParts = parts[0].trim().split(' ');
            if (timeParts.length > 1) {
                timestamp = timeParts[1];
            }
            level = normalizeLogLevel(parts[1].trim());
            message = parts.slice(2).join(' - ').trim();
            message = message.replace(/^\[[\w\s]+\]\s*/, '');
        }

        const decorated = decorateLogLevel(level);
        return {
            timestamp,
            level,
            category: detectLogCategory(message),
            message,
            emoji: pickLogEmoji(message, level),
            icon: decorated.icon,
            statusClass: decorated.statusClass,
            badgeClass: decorated.badgeClass
        };
    };

    // 解析日志内容为结构化数据（兜底全量）
    const parseLogContent = (content) => {
        if (!content || content.trim() === '') return [];
        const lines = content.split('\n');
        const parsed = [];
        for (const line of lines) {
            const row = parseLogLine(line);
            if (row) parsed.push(row);
        }
        return parsed;
    };

    // --- 虚拟列表状态 ---
    const LOG_ITEM_H = 26;     // 每行高度 px
    const LOG_OVERSCAN = 20;   // 上下多渲染的缓冲行数
    const logContainerRef = ref(null);
    const logScrollTop = ref(0);

    const filteredLogs = computed(() => {
        const level = consoleLogState.levelFilter;
        const category = consoleLogState.categoryFilter;
        const keyword = (consoleLogState.keywordFilter || '').toLowerCase();
        return parsedLogs.value.filter(item => {
            const levelMatch = level === 'ALL' || item.level === level;
            const categoryMatch = category === 'ALL' || item.category === category;
            const keywordMatch = !keyword || item.message.toLowerCase().includes(keyword) || item.level.toLowerCase().includes(keyword);
            return levelMatch && categoryMatch && keywordMatch;
        });
    });

    const logVirtualState = computed(() => {
        const items = filteredLogs.value;
        const total = items.length;
        const totalH = total * LOG_ITEM_H;
        const start = Math.max(0, Math.floor(logScrollTop.value / LOG_ITEM_H) - LOG_OVERSCAN);
        const containerH = logContainerRef.value ? logContainerRef.value.clientHeight : 600;
        const end = Math.min(total, Math.ceil((logScrollTop.value + containerH) / LOG_ITEM_H) + LOG_OVERSCAN);
        return { items: items.slice(start, end), start, totalH, offsetY: start * LOG_ITEM_H };
    });

    const onLogScroll = () => {
        const el = logContainerRef.value;
        if (el) logScrollTop.value = el.scrollTop;
    };

    const copyLogLine = (log) => {
        const text = `[${log.level}] ${log.timestamp} ${log.message}`;
        navigator.clipboard.writeText(text).then(() => showToast('已复制日志', 'success')).catch(() => {});
    };

    const scrollConsoleLogToBottom = () => {
        nextTick(() => {
            const el = logContainerRef.value;
            if (el) {
                logScrollTop.value = el.scrollHeight - el.clientHeight;
                el.scrollTop = el.scrollHeight;
            }
        });
    };

    let _logBatchTimer = null;
    let _logBatchBuffer = [];

    const _flushLogBatch = () => {
        if (_logBatchBuffer.length === 0) return;
        const batch = _logBatchBuffer.splice(0);
        const merged = [...parsedLogs.value, ...batch];
        const excess = merged.length - consoleLogState.maxLines;
        parsedLogs.value = excess > 0 ? merged.slice(excess) : merged;
        scrollConsoleLogToBottom();
    };

    const appendSystemLogChunk = (chunk) => {
        if (!chunk) return;

        consoleLogState.content = (consoleLogState.content || '') + chunk;
        if (consoleLogState.content.length > 1024 * 1024) {
            consoleLogState.content = consoleLogState.content.split('\n').slice(-consoleLogState.maxLines).join('\n');
        }
        consoleLogState.partialLineBuffer += chunk;

        const lines = consoleLogState.partialLineBuffer.split('\n');
        consoleLogState.partialLineBuffer = lines.pop() || '';

        for (const line of lines) {
            const row = parseLogLine(line);
            if (row) {
                _logBatchBuffer.push(Object.freeze(row));
            }
        }

        // 每 200ms 批量 flush 一次，避免逐条触发 Vue 重渲染
        if (!_logBatchTimer) {
            _logBatchTimer = setTimeout(() => {
                _logBatchTimer = null;
                _flushLogBatch();
            }, 200);
        }
    };

    const rebuildConsoleLogFromContent = () => {
        consoleLogState.partialLineBuffer = '';
        let items = parseLogContent(consoleLogState.content || '').map(Object.freeze);
        if (items.length > consoleLogState.maxLines) items = items.slice(-consoleLogState.maxLines);
        parsedLogs.value = items;
        scrollConsoleLogToBottom();
    };

    const loadSystemLogsFallback = async () => {
        try {
            const res = await axios.get('/api/system_logs', {
                params: {
                    level: consoleLogState.levelFilter,
                    keyword: (consoleLogState.keywordFilter || '').trim(),
                    category: consoleLogState.categoryFilter || 'ALL',
                    limit: consoleLogState.maxLines
                }
            });
            consoleLogState.content = res.data.logs || '';
            if (res.data.latest_id) {
                consoleLogState.lastEventId = Number(res.data.latest_id);
            }
            rebuildConsoleLogFromContent();
        } catch (e) {
            consoleLogState.content = '读取日志失败: ' + e.message;
            parsedLogs.value = [];
            consoleLogState.partialLineBuffer = '';
        }
    };

    const reconnectConsoleLogStream = () => {
        if (!consoleLogState.visible) return;
        consoleLogState.autoRefresh = true;
        consoleLogState.loading = true;
        stopConsoleLogStream();
        startConsoleLogStream();
        setTimeout(() => {
            consoleLogState.loading = false;
        }, 300);
    };

    let consoleLogEventSource = null;

    const stopConsoleLogStream = () => {
        if (consoleLogEventSource) {
            consoleLogEventSource.close();
            consoleLogEventSource = null;
        }
        consoleLogState.streaming = false;
    };

    const startConsoleLogStream = () => {
        stopConsoleLogStream();
        try {
            const params = new URLSearchParams();
            params.set('level', consoleLogState.levelFilter || 'ALL');
            const keyword = (consoleLogState.keywordFilter || '').trim();
            if (keyword) {
                params.set('keyword', keyword);
            }
            if (consoleLogState.categoryFilter && consoleLogState.categoryFilter !== 'ALL') {
                params.set('category', consoleLogState.categoryFilter);
            }
            if (consoleLogState.lastEventId) {
                params.set('last_event_id', String(consoleLogState.lastEventId));
            }

            consoleLogEventSource = new EventSource('/api/system_logs/stream?' + params.toString());
            consoleLogState.streaming = true;

            consoleLogEventSource.addEventListener('init', (e) => {
                try {
                    const data = JSON.parse(e.data || '{}');
                    if (!consoleLogState.lastEventId) {
                        consoleLogState.content = data.chunk || '';
                        rebuildConsoleLogFromContent();
                    }
                } catch (_) {}
            });

            consoleLogEventSource.addEventListener('reset', async () => {
                await loadSystemLogsFallback();
                stopConsoleLogStream();
                if (consoleLogState.visible && consoleLogState.autoRefresh) {
                    setTimeout(startConsoleLogStream, 100);
                }
            });

            consoleLogEventSource.onmessage = (e) => {
                try {
                    if (e.lastEventId) {
                        const eventId = Number(e.lastEventId);
                        if (!Number.isNaN(eventId)) {
                            consoleLogState.lastEventId = eventId;
                        }
                    }
                    const data = JSON.parse(e.data || '{}');
                    appendSystemLogChunk(data.chunk || '');
                } catch (_) {}
            };

            consoleLogEventSource.onerror = () => {
                stopConsoleLogStream();
                if (consoleLogState.visible && consoleLogState.autoRefresh) {
                    setTimeout(startConsoleLogStream, 1000);
                }
            };
        } catch (_) {
            stopConsoleLogStream();
        }
    };

    const toggleConsoleAutoScroll = () => {
        consoleLogState.autoRefresh = !consoleLogState.autoRefresh;
        if (consoleLogState.autoRefresh) {
            if (consoleLogState.visible) {
                startConsoleLogStream();
            }
        } else {
            stopConsoleLogStream();
        }
    };

    const openConsoleLog = () => {
        consoleLogState.visible = true;
        nextTick(async () => {
            const app = document.querySelector('#app');
            if (app) app.style.overflow = 'auto';
            document.body.style.overflow = 'hidden';
            document.body.style.position = 'fixed';
            document.body.style.width = '100%';

            await loadSystemLogsFallback();
            scrollConsoleLogToBottom();

            if (consoleLogState.autoRefresh) {
                startConsoleLogStream();
            }
        });
    };

    const closeConsoleLog = () => {
        consoleLogState.visible = false;
        const app = document.querySelector('#app');
        if (app) app.style.overflow = 'hidden';
        document.body.style.overflow = '';
        document.body.style.position = '';
        document.body.style.width = '';
        stopConsoleLogStream();
    };

    const changeConsoleLogLevel = (level) => {
        const target = String(level || 'ALL').toUpperCase();
        if (consoleLogState.levelFilter === target) return;
        consoleLogState.levelFilter = target;
        consoleLogState.lastEventId = null;
        consoleLogState.content = '';
        parsedLogs.value = [];
        consoleLogState.partialLineBuffer = '';
        if (consoleLogState.visible) {
            reconnectConsoleLogStream();
        }
    };

    const changeConsoleLogCategory = (category) => {
        const target = String(category || 'ALL').toUpperCase();
        if (consoleLogState.categoryFilter === target) return;
        consoleLogState.categoryFilter = target;
        consoleLogState.lastEventId = null;
        consoleLogState.content = '';
        parsedLogs.value = [];
        consoleLogState.partialLineBuffer = '';
        if (consoleLogState.visible) {
            reconnectConsoleLogStream();
        }
    };

    let _keywordDebounceTimer = null;
    watch(() => consoleLogState.keywordInput, (val) => {
        const nextKeyword = (val || '').trim();
        if (nextKeyword === consoleLogState.keywordFilter) return;
        consoleLogState.keywordFilter = nextKeyword;
        clearTimeout(_keywordDebounceTimer);
        _keywordDebounceTimer = setTimeout(() => {
            if (!consoleLogState.visible) return;
            consoleLogState.lastEventId = null;
            consoleLogState.content = '';
            parsedLogs.value = [];
            consoleLogState.partialLineBuffer = '';
            reconnectConsoleLogStream();
        }, 400);
    });

    const clearSystemLogs = async () => {
        try {
            const res = await axios.post('/api/clear_system_logs');
            if (res.data.status === 'ok') {
                consoleLogState.content = '';
                parsedLogs.value = [];
                consoleLogState.partialLineBuffer = '';
                consoleLogState.lastEventId = null;
                showToast('日志已清空', 'success');
            }
        } catch(e) {
            console.error('清空日志失败:', e);
            showToast('清空日志失败: ' + (e.response?.data?.detail || e.message), 'error');
        }
    };

    return {
        consoleLogState,
        filteredLogs,
        logVirtualState,
        logContainerRef,
        onLogScroll,
        copyLogLine,
        openConsoleLog,
        closeConsoleLog,
        reconnectConsoleLogStream,
        changeConsoleLogLevel,
        changeConsoleLogCategory,
        toggleConsoleAutoScroll,
        clearSystemLogs,
        stopConsoleLogStream,
    };
}
