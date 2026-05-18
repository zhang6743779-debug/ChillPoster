import axios from 'axios';
import { computed, nextTick, reactive, ref, watch } from 'vue';

export function useDiscover({ tab, isMobile, openPanels, focusedPanel, closeDockDrawers, mobileMenuVisible, mpConfig, showToast }) {
        // ==========================================
        // 14. 发现推荐页
        // ==========================================
        const MISSING_EPISODE_RENDER_LIMIT_DESKTOP = 80;
        const MISSING_EPISODE_RENDER_LIMIT_MOBILE = 24;
        const MISSING_EPISODE_RENDER_STEP_DESKTOP = 40;
        const MISSING_EPISODE_RENDER_STEP_MOBILE = 16;
        const missingEpisodeRenderPage = ref(0);

        const discoverRows = [
            { key: 'today_picks', title: '今日推荐', icon: 'fa-solid fa-gift', source: 'tmdb', endpoint: '/api/discover/today_picks' },
            { key: 'tmdb_trending', title: '本周热门', icon: 'fa-solid fa-fire', source: 'tmdb', endpoint: '/api/discover/tmdb/trending' },
            { key: 'tmdb_now_playing', title: '正在热映', icon: 'fa-solid fa-ticket', source: 'tmdb', endpoint: '/api/discover/tmdb/now_playing' },
            { key: 'tmdb_popular_movies', title: 'TMDB热门电影', icon: 'fa-solid fa-film', source: 'tmdb', endpoint: '/api/discover/tmdb/popular_movies' },
            { key: 'tmdb_popular_tv', title: 'TMDB热门剧集', icon: 'fa-solid fa-tv', source: 'tmdb', endpoint: '/api/discover/tmdb/popular_tv' },
            { key: 'douban_hot_movies', title: '豆瓣热门电影', icon: 'fa-solid fa-fire-flame-curved', source: 'douban', endpoint: '/api/discover/douban/hot_movies' },
            { key: 'douban_hot_tv', title: '豆瓣热门剧集', icon: 'fa-solid fa-tv', source: 'douban', endpoint: '/api/discover/douban/hot_tv' },
            { key: 'douban_hot_anime', title: '豆瓣热门动漫', icon: 'fa-solid fa-dragon', source: 'douban', endpoint: '/api/discover/douban/hot_anime' },
            { key: 'douban_showing', title: '豆瓣正在上映', icon: 'fa-solid fa-clapperboard', source: 'douban', endpoint: '/api/discover/douban/showing' },
            { key: 'douban_new_movies', title: '豆瓣最新电影', icon: 'fa-solid fa-sparkles', source: 'douban', endpoint: '/api/discover/douban/new_movies' },
            { key: 'douban_new_tv', title: '豆瓣热门国产剧', icon: 'fa-solid fa-list', source: 'douban', endpoint: '/api/discover/douban/new_tv' },
            { key: 'douban_top250', title: '豆瓣 Top 250', icon: 'fa-solid fa-trophy', source: 'douban', endpoint: '/api/discover/douban/top250' },
            { key: 'douban_chinese_weekly', title: '华语口碑剧集榜', icon: 'fa-solid fa-ranking-star', source: 'douban', endpoint: '/api/discover/douban/chinese_weekly' },
            { key: 'douban_global_weekly', title: '全球口碑剧集榜', icon: 'fa-solid fa-earth-americas', source: 'douban', endpoint: '/api/discover/douban/global_weekly' },
        ];

        const discoverData = reactive({});
        const discoverLoading = reactive({});
        const discoverErrors = reactive({});
        const detailModal = reactive({ visible: false, item: null, detail: null, loading: false, subscribed: false, selectedSeason: null, seasonExpanded: false, castExpanded: false, seasonSubscribed: false, seasonEpisodes: {}, seasonEpisodesLoading: {}, librarySeriesStatus: { exists: false, seasons: {} } });
        let detailHistoryActive = false;
        let suppressDetailPopstate = false;
        const gridModal = reactive({ visible: false, title: '', row: null, items: [], page: 1, totalPages: 1, loadingMore: false, noMore: false });
        const gridModalEl = ref(null);
        const gridSentinel = ref(null);
        let gridObserver = null;
        const discoverSearchQuery = ref('');
        const discoverSearchResults = ref([]);
        const searchMovieResults = ref([]);
        const searchTvResults = ref([]);
        const discoverSearchLoading = ref(false);
        const discoverHasSearched = ref(false);
        const searchPage = ref(1);
        const searchTotalPages = ref(1);

        // ===== 发现页状态 (MP 克隆) =====
        const LIBRARY_STATUS_FILTER_KEY = '__library_status';
        const LIBRARY_STATUS_FILTER_ROW = {
            key: LIBRARY_STATUS_FILTER_KEY,
            label: '状态',
            control: 'chips',
            default: '',
            options: [
                { label: '已入库', value: 'exists' },
                { label: '未入库', value: 'missing' },
            ],
            show: '',
            depends_on: [],
        };
        const discoverSourceTabs = ref([]);
        const discoverActiveSource = ref('themoviedb');
        const discoverSourceMap = computed(() => Object.fromEntries((discoverSourceTabs.value || []).map(item => [item.key, item])));
        const discoverFiltersBySource = reactive({});
        const genreList = ref([]);

        const discoverSourceSupported = computed(() => !!discoverSourceMap.value[discoverActiveSource.value]);
        const activeSourceDef = computed(() => discoverSourceMap.value[discoverActiveSource.value] || null);
        const activeSourceSchema = computed(() => activeSourceDef.value?.filter_schema || []);
        const activeSourceFilters = computed(() => {
            const key = discoverActiveSource.value;
            if (!discoverFiltersBySource[key]) discoverFiltersBySource[key] = {};
            return discoverFiltersBySource[key];
        });
        const discoverEmptyText = computed(() => discoverSourceSupported.value ? '暂无内容' : '该数据源暂未接入当前项目');

        const fetchGenreList = async () => {
            if (genreList.value.length) return;
            try {
                const res = await axios.get('/api/discover/genres');
                genreList.value = res.data.genres || [];
            } catch (e) {
                console.error('加载类型失败:', e);
            }
        };

        const patchTmdbGenreSchema = () => {
            const source = discoverSourceMap.value['themoviedb'];
            if (!source) return;
            const schema = Array.isArray(source.filter_schema) ? source.filter_schema : [];
            const genreRow = schema.find(item => item.key === 'with_genres');
            if (genreRow && (!genreRow.options || !genreRow.options.length)) {
                genreRow.options = (genreList.value || []).map(g => ({ label: g.name, value: String(g.id), media_type: g.media_type }));
            }
        };

        const ensureSourceFilters = (source) => {
            if (!source) return;
            const defaults = {};
            Object.entries(source.filter_params || {}).forEach(([key, value]) => {
                defaults[key] = value == null ? '' : String(value);
            });
            (source.filter_schema || []).forEach(row => {
                if (!(row.key in defaults)) defaults[row.key] = row.default == null ? '' : String(row.default);
            });
            defaults[LIBRARY_STATUS_FILTER_KEY] = '';
            discoverFiltersBySource[source.key] = { ...(discoverFiltersBySource[source.key] || {}), ...defaults };
        };

        const loadDiscoverSources = async () => {
            if (discoverSourceTabs.value.length) return;
            try {
                const res = await axios.get('/api/discover/sources');
                discoverSourceTabs.value = res.data.sources || [];
                discoverSourceTabs.value.forEach(source => ensureSourceFilters(source));
                await fetchGenreList();
                patchTmdbGenreSchema();
                if (!discoverSourceMap.value[discoverActiveSource.value] && discoverSourceTabs.value.length) {
                    discoverActiveSource.value = discoverSourceTabs.value[0].key;
                }
            } catch (e) {
                console.error('加载发现源失败:', e);
            }
        };

        const getNormalizedDisplayFilters = () => {
            const filters = { ...(activeSourceFilters.value || {}) };
            if (discoverActiveSource.value === 'bilibili' && filters.mtype === 'guochuang') {
                filters.mtype = 'guo';
            }
            return filters;
        };

        const isFilterRowVisible = (row) => {
            if (!row || !row.show) return true;
            let expr = row.show.trim();
            if (expr.startsWith('{{') && expr.endsWith('}}')) expr = expr.slice(2, -2).trim();
            expr = expr.replace(/\|\|/g, '||').replace(/&&/g, '&&');
            const filters = getNormalizedDisplayFilters();
            try {
                return !!Function('filters', `with (filters) { return (${expr}); }`)(filters);
            } catch {
                return false;
            }
        };

        const getFilterRowDefaultValue = (row) => {
            if (!row) return '';
            if (row.default != null) return String(row.default);
            return '';
        };

        const getActiveSourceSchemaRows = () => [...(activeSourceSchema.value || []), LIBRARY_STATUS_FILTER_ROW];
        const getActiveSourceSchemaMap = () => Object.fromEntries(getActiveSourceSchemaRows().map(row => [row.key, row]));

        const getOptionParentValues = (option) => {
            if (!option) return [];
            if (Array.isArray(option.parent_values)) return option.parent_values.map(v => String(v));
            if (option.parent_values != null && option.parent_values !== '') return [String(option.parent_values)];
            if (option.media_type != null && option.media_type !== '') return [String(option.media_type)];
            return [];
        };

        const getFilteredRowOptions = (row) => {
            const options = Array.isArray(row?.options) ? row.options : [];
            if (!options.length) return options;
            const parents = Array.isArray(row?.depends_on) ? row.depends_on : [];
            const filtered = (!parents.length && row?.key !== 'with_genres') ? options : options.filter(opt => {
                const parentValues = getOptionParentValues(opt);
                if (!parentValues.length) return true;
                const matches = parents.some(parentKey => {
                    const currentValue = String(activeSourceFilters.value?.[parentKey] ?? '');
                    return parentValues.includes(currentValue) || parentValues.includes('both');
                });
                if (matches) return true;
                return String(opt.value ?? '') === '';
            });
            const deduped = [];
            const seen = new Set();
            filtered.forEach(opt => {
                const normalizedValue = String(opt?.value ?? '');
                const normalizedLabel = String(opt?.label ?? '');
                const signature = normalizedValue === ''
                    ? `__all__${normalizedLabel}`
                    : `${normalizedValue}__${normalizedLabel}`;
                if (seen.has(signature)) return;
                seen.add(signature);
                deduped.push(opt);
            });
            return deduped;
        };

        const getResolvedRowLabel = (row) => {
            const variants = Array.isArray(row?.label_variants) ? row.label_variants : [];
            if (!variants.length) return row?.label || '';
            const matched = variants.find(variant => {
                const parentValues = getOptionParentValues(variant);
                if (!parentValues.length) return false;
                const show = String(variant.show || '').trim();
                if (show && !isFilterRowVisible({ ...row, show })) return false;
                return (row.depends_on || []).some(parentKey => {
                    const currentValue = String(activeSourceFilters.value?.[parentKey] ?? '');
                    return parentValues.includes(currentValue) || parentValues.includes('both');
                });
            });
            return matched?.label || row?.label || '';
        };

        const resetDependentFilters = (changedKey) => {
            const schemaMap = getActiveSourceSchemaMap();
            const queue = [changedKey];
            const visited = new Set(queue);
            while (queue.length) {
                const current = queue.shift();
                (getActiveSourceSchemaRows()).forEach(row => {
                    if (!(row.depends_on || []).includes(current)) return;
                    if (visited.has(row.key)) return;
                    activeSourceFilters.value[row.key] = getFilterRowDefaultValue(row);
                    visited.add(row.key);
                    queue.push(row.key);
                });
            }
        };

        const pruneHiddenOrInvalidFilters = () => {
            const schemaMap = getActiveSourceSchemaMap();
            (getActiveSourceSchemaRows()).forEach(row => {
                if (!isFilterRowVisible(row)) {
                    activeSourceFilters.value[row.key] = getFilterRowDefaultValue(row);
                    return;
                }
                if (row.control !== 'chips') return;
                const filteredOptions = getFilteredRowOptions(row);
                const currentValue = String(activeSourceFilters.value?.[row.key] ?? '');
                if (!currentValue) return;
                const valid = filteredOptions.some(opt => String(opt.value ?? '') === currentValue);
                if (!valid) activeSourceFilters.value[row.key] = getFilterRowDefaultValue(row);
            });
        };

        const commitSourceFilterChange = (filterKey, value) => {
            if (!activeSourceFilters.value) return;
            activeSourceFilters.value[filterKey] = value;
            resetDependentFilters(filterKey);
            pruneHiddenOrInvalidFilters();
            resetMainGrid();
        };

        const getVisibleFilterRows = computed(() => {
            return (getActiveSourceSchemaRows()).filter(row => isFilterRowVisible(row)).map(row => ({
                ...row,
                label: getResolvedRowLabel(row),
                options: row.control === 'chips' ? getFilteredRowOptions(row) : (row.options || []),
            }));
        });

        const switchDiscoverSource = async (key) => {
            discoverActiveSource.value = key;
            if (key === 'themoviedb') await fetchGenreList();
            pruneHiddenOrInvalidFilters();
            resetMainGrid();
        };

        const updateSourceFilter = (filterKey, value) => {
            commitSourceFilterChange(filterKey, value);
        };

        const toggleSourceChip = (filterKey, value) => {
            const current = String(activeSourceFilters.value?.[filterKey] ?? '');
            const nextValue = String(value ?? '');
            const canToggleOff = filterKey !== 'media_type' || filterKey === LIBRARY_STATUS_FILTER_KEY;
            const changedValue = canToggleOff && current === nextValue && nextValue !== '' ? '' : nextValue;
            commitSourceFilterChange(filterKey, changedValue);
        };

        const applyNumberFilter = (filterKey) => {
            const row = getActiveSourceSchemaMap()[filterKey] || {};
            let val = Number(activeSourceFilters.value?.[filterKey] ?? 0);
            if (Number.isNaN(val)) val = 0;
            const min = Number(row.min ?? 0);
            const max = Number(row.max ?? 10);
            if (val < min) val = min;
            if (val > max) val = max;
            commitSourceFilterChange(filterKey, String(val));
        };

        // ===== 主网格状态 =====
        const mainGridItems = ref([]);
        const mainGridPage = ref(1);
        const mainGridTotalPages = ref(1);
        const mainGridLoading = ref(false);
        const mainGridNoMore = ref(false);
        const mainGridSentinel = ref(null);
        const mainGridScrollRoot = ref(null);
        let mainGridObserver = null;
        let mainGridObserverRetryTimer = null;
        let _mainGridGen = 0;
        const mainGridPrefetch = reactive({ pages: {} });
        const MAIN_GRID_PREFETCH_AHEAD = 2;
        const emptyMissingEpisodeSummary = () => ({
            tvCount: 0,
            completeCount: 0,
            partialCount: 0,
            missingCount: 0,
            errorCount: 0,
            airingRecentMissingCount: 0,
            endedMissingCount: 0,
            otherMissingCount: 0,
            presentEpisodes: 0,
            totalEpisodes: 0,
            missingEpisodes: 0,
        });
        const missingEpisodeStats = reactive({
            loading: false,
            loaded: false,
            ready: true,
            error: '',
            message: '',
            items: [],
            libraries: [],
            activeLibraryKey: '',
            filter: 'all',
            statusFilter: 'problem',
            sortBy: 'year_desc',
            searchQuery: '',
            meta: {},
            summary: emptyMissingEpisodeSummary(),
            progress: { current: 0, total: 0 },
        });
        let missingEpisodeStatsRunId = 0;
        let missingEpisodeStatsPollTimer = null;
        const getMissingEpisodeLibraryKey = (lib = {}) => lib.libraryId || lib.libraryName || '';
        const missingEpisodeLibraries = computed(() => missingEpisodeStats.libraries || []);
        const missingEpisodeActiveLibrary = computed(() => {
            const libraries = missingEpisodeLibraries.value;
            if (!libraries.length) return null;
            return libraries.find(lib => getMissingEpisodeLibraryKey(lib) === missingEpisodeStats.activeLibraryKey) || libraries[0];
        });
        const missingEpisodeActiveSummary = computed(() => {
            return missingEpisodeActiveLibrary.value?.summary || missingEpisodeStats.summary || emptyMissingEpisodeSummary();
        });
        const missingEpisodeActiveErrorCount = computed(() => {
            const summary = missingEpisodeActiveSummary.value || {};
            return (Number(summary.errorCount) || 0) + (Number(summary.missingCount) || 0);
        });
        const missingEpisodeSearchActive = computed(() => !!String(missingEpisodeStats.searchQuery || '').trim());
        const missingEpisodeStatsProblemItems = computed(() => {
            const query = String(missingEpisodeStats.searchQuery || '').trim().toLowerCase();
            const sourceItems = query
                ? (missingEpisodeStats.items || [])
                : (missingEpisodeActiveLibrary.value?.items || missingEpisodeStats.items || []);
            const filteredItems = sourceItems.filter(item => {
                if (missingEpisodeStats.statusFilter === 'problem' && item.status !== 'partial') return false;
                if (missingEpisodeStats.statusFilter === 'error' && item.status !== 'error' && item.status !== 'missing') return false;
                if (missingEpisodeStats.statusFilter && !['all', 'problem', 'error'].includes(missingEpisodeStats.statusFilter) && item.status !== missingEpisodeStats.statusFilter) return false;
                if (missingEpisodeStats.filter === 'all') return true;
                if (missingEpisodeStats.filter === 'error') return item.missingCategory === 'error' || item.status === 'error' || item.status === 'missing';
                return item.missingCategory === missingEpisodeStats.filter;
            }).filter(item => {
                if (!query) return true;
                const haystack = [
                    item.title,
                    item.item?.original_title,
                    item.year,
                    item.tmdbId,
                    item.label,
                    item.categoryLabel,
                    item.seasonBrief,
                ].filter(Boolean).join(' ').toLowerCase();
                return haystack.includes(query);
            });
            const num = (value) => Number(value) || 0;
            const year = (item) => num(String(item.year || '').slice(0, 4));
            const missingRatio = (item) => {
                const total = num(item.totalEpisodes);
                return total ? num(item.missingEpisodes) / total : 0;
            };
            const sortedItems = [...filteredItems];
            sortedItems.sort((a, b) => {
                switch (missingEpisodeStats.sortBy) {
                    case 'year_desc':
                        return year(b) - year(a) || num(b.missingEpisodes) - num(a.missingEpisodes);
                    case 'year_asc':
                        return year(a) - year(b) || num(b.missingEpisodes) - num(a.missingEpisodes);
                    case 'missing_asc':
                        return num(a.missingEpisodes) - num(b.missingEpisodes) || year(b) - year(a);
                    case 'ratio_desc':
                        return missingRatio(b) - missingRatio(a) || num(b.missingEpisodes) - num(a.missingEpisodes);
                    case 'ratio_asc':
                        return missingRatio(a) - missingRatio(b) || num(b.missingEpisodes) - num(a.missingEpisodes);
                    case 'title_asc':
                        return String(a.title || '').localeCompare(String(b.title || ''), 'zh-Hans-CN') || year(b) - year(a);
                    case 'missing_desc':
                    default:
                        return num(b.missingEpisodes) - num(a.missingEpisodes) || year(b) - year(a);
                }
            });
            return sortedItems;
        });
        const missingEpisodeVisibleLimit = computed(() => {
            const base = isMobile.value ? MISSING_EPISODE_RENDER_LIMIT_MOBILE : MISSING_EPISODE_RENDER_LIMIT_DESKTOP;
            const step = isMobile.value ? MISSING_EPISODE_RENDER_STEP_MOBILE : MISSING_EPISODE_RENDER_STEP_DESKTOP;
            return base + Math.max(0, missingEpisodeRenderPage.value) * step;
        });
        const visibleMissingEpisodeStatsProblemItems = computed(() => {
            return missingEpisodeStatsProblemItems.value.slice(0, missingEpisodeVisibleLimit.value);
        });
        const missingEpisodeHasMoreVisibleItems = computed(() => {
            return visibleMissingEpisodeStatsProblemItems.value.length < missingEpisodeStatsProblemItems.value.length;
        });
        let missingEpisodeLazyLoadLastAt = 0;
        let missingEpisodeLazyEnsurePending = false;

        const loadMoreMissingEpisodeItems = () => {
            if (!missingEpisodeHasMoreVisibleItems.value) return;
            missingEpisodeRenderPage.value += 1;
            ensureMissingEpisodeLazyScrollable();
        };

        const ensureMissingEpisodeLazyScrollable = () => {
            if (missingEpisodeLazyEnsurePending) return;
            missingEpisodeLazyEnsurePending = true;
            nextTick(() => {
                requestAnimationFrame(() => {
                    missingEpisodeLazyEnsurePending = false;
                    if (tab.value !== 'missing_episode_stats') return;
                    if (!missingEpisodeHasMoreVisibleItems.value) return;
                    const el = document.querySelector('.missing-episode-poster-grid');
                    if (!el) return;
                    const hasVerticalScroll = el.scrollHeight > el.clientHeight + 4;
                    if (!hasVerticalScroll) loadMoreMissingEpisodeItems();
                });
            });
        };

        const resetMissingEpisodeRenderedItems = () => {
            missingEpisodeRenderPage.value = 0;
            ensureMissingEpisodeLazyScrollable();
        };

        const onMissingEpisodeLazyScroll = (event) => {
            const el = event?.currentTarget;
            if (!el || !missingEpisodeHasMoreVisibleItems.value) return;
            const remainingY = el.scrollHeight - el.clientHeight - el.scrollTop;
            if (remainingY >= 180) return;
            const now = Date.now();
            if (now - missingEpisodeLazyLoadLastAt < 180) return;
            missingEpisodeLazyLoadLastAt = now;
            loadMoreMissingEpisodeItems();
        };

        watch(isMobile, () => {
            resetMissingEpisodeRenderedItems();
        });

        watch(() => missingEpisodeStats.searchQuery, () => {
            resetMissingEpisodeRenderedItems();
        });

        watch(() => missingEpisodeStatsProblemItems.value.length, () => {
            resetMissingEpisodeRenderedItems();
        });

        const isDoubanMainGrid = () => discoverActiveSource.value === 'douban';

        const resetMainGridPrefetch = () => {
            mainGridPrefetch.pages = {};
        };

        const clearMissingEpisodeStatsForGridChange = () => {
            resetMissingEpisodeStats();
            missingEpisodeStats.loading = false;
        };

        const getProviderFilterParams = () => {
            const params = { ...(activeSourceFilters.value || {}) };
            delete params[LIBRARY_STATUS_FILTER_KEY];
            return params;
        };

        const fetchMainGridPage = async (source, page) => {
            const params = { ...getProviderFilterParams(), page };
            const res = await axios.get(`/api/discover/provider/${source}`, { params });
            return res.data || {};
        };

        const getItemTmdbId = (item = {}) => {
            if (item._tmdb_id || item.tmdb_id) return item._tmdb_id || item.tmdb_id;
            return ['tmdb', 'themoviedb'].includes(item.source) ? item.id : '';
        };

        const getItemExistenceKey = (item = {}) => {
            const tmdbId = getItemTmdbId(item);
            const mediaType = item.media_type || 'movie';
            if (tmdbId) return `${tmdbId}:${mediaType}`;
            const title = item.title || item.name || '';
            const year = item.year || '';
            if (!title) return '';
            return `title:${mediaType}:${String(title).trim()}:${String(year).slice(0, 4)}`;
        };

        const markLibraryExists = async (items = []) => {
            const candidates = (items || []).filter(item => getItemExistenceKey(item));
            if (!candidates.length) return;
            try {
                const payload = candidates.map(item => ({
                    tmdb_id: getItemTmdbId(item),
                    title: item.title || item.name || '',
                    year: item.year || '',
                    media_type: item.media_type || 'movie',
                    source: item.source || '',
                    id: item.id || '',
                    _existence_key: getItemExistenceKey(item),
                }));
                const res = await axios.post('/api/discover/library/exists', payload);
                const results = res.data?.results || {};
                candidates.forEach(item => {
                    item.exists_in_library = !!results[getItemExistenceKey(item)];
                });
            } catch (e) {
                console.error('检查媒体库存在状态失败:', e);
            }
        };

        const resetMissingEpisodeStats = () => {
            missingEpisodeStatsRunId += 1;
            if (missingEpisodeStatsPollTimer) {
                clearTimeout(missingEpisodeStatsPollTimer);
                missingEpisodeStatsPollTimer = null;
            }
            missingEpisodeStats.loaded = false;
            missingEpisodeStats.ready = true;
            missingEpisodeStats.error = '';
            missingEpisodeStats.message = '';
            missingEpisodeStats.items = [];
            missingEpisodeStats.libraries = [];
            missingEpisodeStats.activeLibraryKey = '';
            missingEpisodeStats.filter = 'all';
            missingEpisodeStats.statusFilter = 'problem';
            missingEpisodeStats.sortBy = 'year_desc';
            missingEpisodeStats.searchQuery = '';
            missingEpisodeStats.meta = {};
            missingEpisodeStats.summary = emptyMissingEpisodeSummary();
            missingEpisodeStats.progress = { current: 0, total: 0 };
        };

        const applyMissingEpisodeStatsData = (data = {}) => {
            const previousLibraryKey = missingEpisodeStats.activeLibraryKey;
            const summary = { ...emptyMissingEpisodeSummary(), ...(data.summary || {}) };
            missingEpisodeStats.ready = data.ready !== false;
            missingEpisodeStats.message = data.message || '';
            missingEpisodeStats.meta = data.meta || {};
            missingEpisodeStats.items = data.items || [];
            missingEpisodeStats.libraries = data.libraries || [];
            const stillExists = missingEpisodeStats.libraries.some(lib => getMissingEpisodeLibraryKey(lib) === previousLibraryKey);
            if (stillExists) {
                missingEpisodeStats.activeLibraryKey = previousLibraryKey;
            } else {
                const firstProblemLib = missingEpisodeStats.libraries.find(lib => (lib.summary?.missingEpisodes || 0) > 0);
                const firstLib = firstProblemLib || missingEpisodeStats.libraries[0];
                missingEpisodeStats.activeLibraryKey = firstLib ? getMissingEpisodeLibraryKey(firstLib) : '';
            }
            missingEpisodeStats.summary = summary;
            missingEpisodeStats.progress = data.progress || { current: summary.tvCount || 0, total: summary.tvCount || 0 };
            missingEpisodeStats.loaded = true;
            missingEpisodeStats.loading = !!data.running;
        };

        const pollMissingEpisodeStats = async (runId) => {
            try {
                const res = await axios.get('/api/discover/library/missing-episode-stats');
                if (runId !== missingEpisodeStatsRunId) return;
                const data = res.data || {};
                applyMissingEpisodeStatsData(data);
                if (data.running) {
                    missingEpisodeStatsPollTimer = setTimeout(() => pollMissingEpisodeStats(runId), 1200);
                } else {
                    missingEpisodeStatsPollTimer = null;
                    missingEpisodeStats.loading = false;
                }
            } catch (e) {
                if (runId === missingEpisodeStatsRunId) {
                    missingEpisodeStats.loading = false;
                    missingEpisodeStats.error = e.response?.data?.detail || e.message || '统计失败';
                }
            }
        };

        const loadMissingEpisodeStatsShell = async () => {
            if (missingEpisodeStats.loading || missingEpisodeStats.loaded) return;
            const runId = missingEpisodeStatsRunId;
            try {
                const res = await axios.get('/api/discover/library/missing-episode-stats');
                if (runId !== missingEpisodeStatsRunId) return;
                applyMissingEpisodeStatsData(res.data || {});
                if (res.data?.running) {
                    missingEpisodeStatsPollTimer = setTimeout(() => pollMissingEpisodeStats(runId), 1200);
                }
            } catch (e) {
                if (runId === missingEpisodeStatsRunId) {
                    missingEpisodeStats.error = e.response?.data?.detail || e.message || '获取媒体库失败';
                }
            }
        };

        const runMissingEpisodeStats = async (force = false) => {
            if (missingEpisodeStats.loading) return;
            if (missingEpisodeStats.loaded && !force) return;
            resetMissingEpisodeStats();
            const runId = missingEpisodeStatsRunId;
            missingEpisodeStats.loading = true;
            try {
                const res = await axios.get('/api/discover/library/missing-episode-stats', {
                    params: { start: 1, refresh: force ? 1 : 0 },
                });
                if (runId !== missingEpisodeStatsRunId) return;
                applyMissingEpisodeStatsData(res.data || {});
                if (res.data?.running) {
                    missingEpisodeStatsPollTimer = setTimeout(() => pollMissingEpisodeStats(runId), 1200);
                }
            } catch (e) {
                if (runId === missingEpisodeStatsRunId) {
                    missingEpisodeStats.error = e.response?.data?.detail || e.message || '统计失败';
                }
            } finally {
                if (runId === missingEpisodeStatsRunId && !missingEpisodeStatsPollTimer) {
                    missingEpisodeStats.loading = false;
                }
            }
        };

        const refreshMissingEpisodeStats = () => {
            runMissingEpisodeStats(true);
        };

        const setMissingEpisodeLibrary = (libraryKey) => {
            missingEpisodeStats.activeLibraryKey = libraryKey;
            resetMissingEpisodeRenderedItems();
        };

        const setMissingEpisodeFilter = (filter) => {
            missingEpisodeStats.filter = filter || 'all';
            if (missingEpisodeStats.filter === 'error') {
                missingEpisodeStats.statusFilter = 'error';
            } else if (missingEpisodeStats.statusFilter === 'error') {
                missingEpisodeStats.statusFilter = 'problem';
            }
            resetMissingEpisodeRenderedItems();
        };

        const setMissingEpisodeStatusFilter = (filter) => {
            missingEpisodeStats.statusFilter = filter || 'problem';
            if (missingEpisodeStats.statusFilter === 'exists' || missingEpisodeStats.statusFilter === 'all') {
                missingEpisodeStats.filter = 'all';
            } else if (missingEpisodeStats.statusFilter === 'error') {
                missingEpisodeStats.filter = 'error';
            }
            resetMissingEpisodeRenderedItems();
        };

        const setMissingEpisodeSort = (sortBy) => {
            missingEpisodeStats.sortBy = sortBy || 'year_desc';
            resetMissingEpisodeRenderedItems();
        };

        const openDiscoverFromMissingStats = () => {
            if (isMobile.value) {
                tab.value = 'media_subscribe';
                mobileMenuVisible.value = false;
                return;
            }
            openPanels.value = ['media_subscribe'];
            focusedPanel.value = 'media_subscribe';
            tab.value = 'media_subscribe';
            closeDockDrawers();
        };

        const applyLibraryStatusFilter = (items = []) => {
            const status = String(activeSourceFilters.value?.[LIBRARY_STATUS_FILTER_KEY] ?? '');
            if (!status) return items;
            return items.filter(item => status === 'exists' ? !!item.exists_in_library : !item.exists_in_library);
        };

        const getDisplayableMainGridItems = (items = []) => {
            return isDoubanMainGrid() ? items.filter(item => item?.poster_url) : items;
        };

        const prepareDisplayableMainGridItems = async (items = []) => {
            const displayable = getDisplayableMainGridItems(items);
            await markLibraryExists(displayable);
            return applyLibraryStatusFilter(displayable);
        };

        const mainGridPageHasMore = (data, page, rawItems) => {
            const totalPages = data.total_pages || 1;
            return !(data.has_more === false || page >= totalPages || !rawItems.length);
        };

        const pruneMainGridPrefetch = () => {
            Object.keys(mainGridPrefetch.pages).forEach(key => {
                const page = Number(key);
                const entry = mainGridPrefetch.pages[key];
                if (entry.gen !== _mainGridGen || page <= mainGridPage.value || page > mainGridPage.value + MAIN_GRID_PREFETCH_AHEAD + 1) {
                    delete mainGridPrefetch.pages[key];
                }
            });
        };

        const prefetchMainGridPage = (page, gen) => {
            if (!isDoubanMainGrid() || page < 1 || mainGridNoMore.value) return null;
            const cached = mainGridPrefetch.pages[page];
            if (cached && cached.gen === gen) {
                if (cached.ready) return Promise.resolve(cached.data);
                if (cached.loading) return cached.promise;
            }

            const source = discoverActiveSource.value;
            const entry = reactive({
                page,
                data: null,
                loading: true,
                ready: false,
                promise: null,
                gen,
            });
            mainGridPrefetch.pages[page] = entry;

            const promise = fetchMainGridPage(source, page)
                .then(async data => {
                    if (gen !== _mainGridGen || source !== discoverActiveSource.value) return null;
                    const rawItems = data.items || [];
                    const items = await prepareDisplayableMainGridItems(rawItems);
                    entry.data = { ...data, items, _rawItemCount: rawItems.length };
                    entry.ready = true;
                    return entry.data;
                })
                .catch(e => {
                    if (gen === _mainGridGen) console.error('预取发现网格失败:', e);
                    delete mainGridPrefetch.pages[page];
                    return null;
                })
                .finally(() => {
                    if (entry.gen === gen) entry.loading = false;
                    pruneMainGridPrefetch();
                });
            entry.promise = promise;
            return promise;
        };

        const prefetchMainGridAhead = (fromPage, gen) => {
            if (!isDoubanMainGrid() || mainGridNoMore.value) return;
            for (let offset = 1; offset <= MAIN_GRID_PREFETCH_AHEAD; offset += 1) {
                prefetchMainGridPage(fromPage + offset, gen);
            }
        };

        const consumeMainGridPrefetch = (page) => {
            const entry = mainGridPrefetch.pages[page];
            if (!entry || entry.gen !== _mainGridGen || !entry.ready || !entry.data) return false;
            const data = entry.data;
            const items = data.items || [];
            mainGridItems.value.push(...items);
            mainGridPage.value = page;
            mainGridTotalPages.value = data.total_pages || 1;
            mainGridNoMore.value = !mainGridPageHasMore(data, page, Array(data._rawItemCount ?? items.length).fill(null));
            delete mainGridPrefetch.pages[page];
            prefetchMainGridAhead(page, _mainGridGen);
            nextTick(() => setupMainGridObserver());
            return true;
        };


        const loadMainGrid = async (reset = true) => {
            if (reset) {
                resetMainGridPrefetch();
                mainGridItems.value = [];
                mainGridPage.value = 1;
                mainGridNoMore.value = false;
            }
            const gen = ++_mainGridGen;
            mainGridLoading.value = true;
            try {
                const source = discoverActiveSource.value;
                if (!source) {
                    mainGridItems.value = [];
                    mainGridTotalPages.value = 1;
                    mainGridNoMore.value = true;
                    return;
                }

                const page = mainGridPage.value;
                const data = await fetchMainGridPage(source, page);
                if (gen !== _mainGridGen) return;
                const rawItems = data.items || [];
                const items = await prepareDisplayableMainGridItems(rawItems);
                mainGridTotalPages.value = data.total_pages || 1;
                mainGridPage.value = page;
                mainGridNoMore.value = !mainGridPageHasMore(data, page, rawItems);
                if (reset) {
                    mainGridItems.value = items;
                } else {
                    mainGridItems.value.push(...items);
                }
                if (source === 'douban' && !mainGridNoMore.value) prefetchMainGridAhead(page, gen);
            } catch (e) {
                console.error('加载发现网格失败:', e);
            } finally {
                if (gen === _mainGridGen) {
                    mainGridLoading.value = false;
                    nextTick(() => setupMainGridObserver());
                }
            }
        };

        const loadNextMainGridPage = async () => {
            if (mainGridLoading.value || mainGridNoMore.value) return;
            const nextPage = mainGridPage.value + 1;
            if (isDoubanMainGrid()) {
                if (consumeMainGridPrefetch(nextPage)) return;
                const pending = mainGridPrefetch.pages[nextPage];
                if (pending?.loading && pending.promise) {
                    mainGridLoading.value = true;
                    await pending.promise;
                    mainGridLoading.value = false;
                    if (consumeMainGridPrefetch(nextPage)) return;
                }
            }
            mainGridPage.value = nextPage;
            await loadMainGrid(false);
        };

        const setupMainGridObserver = (attempt = 0) => {
            if (mainGridObserver) { mainGridObserver.disconnect(); mainGridObserver = null; }
            if (mainGridObserverRetryTimer) {
                clearTimeout(mainGridObserverRetryTimer);
                mainGridObserverRetryTimer = null;
            }
            if (mainGridNoMore.value) return;
            if (!mainGridSentinel.value) {
                if (attempt < 8) mainGridObserverRetryTimer = setTimeout(() => setupMainGridObserver(attempt + 1), 80);
                return;
            }
            const observerRoot = isMobile.value ? null : (mainGridScrollRoot.value || null);
            mainGridObserver = new IntersectionObserver((entries) => {
                if (!entries[0].isIntersecting || mainGridLoading.value || mainGridNoMore.value) return;
                if (isMobile.value) {
                    const scroller = document.scrollingElement || document.documentElement;
                    const remaining = scroller.scrollHeight - window.scrollY - window.innerHeight;
                    if (remaining > 260) return;
                }
                loadNextMainGridPage();
            }, { root: observerRoot, rootMargin: isMobile.value ? '80px 0px' : '900px 0px', threshold: 0.01 });
            mainGridObserver.observe(mainGridSentinel.value);
        };

        const resetMainGrid = () => {
            if (mainGridObserver) { mainGridObserver.disconnect(); mainGridObserver = null; }
            if (mainGridObserverRetryTimer) { clearTimeout(mainGridObserverRetryTimer); mainGridObserverRetryTimer = null; }
            resetMainGridPrefetch();
            clearMissingEpisodeStatsForGridChange();
            mainGridItems.value = [];
            mainGridPage.value = 1;
            mainGridNoMore.value = false;
            loadMainGrid(true);
        };

        watch(discoverActiveSource, async (val) => {
            if (val === 'themoviedb') {
                await fetchGenreList();
                patchTmdbGenreSchema();
            }
        });

        const getDetailSeasons = (detail = detailModal.detail) => {
            return (detail?.seasons || []).filter(season => season.season_number > 0);
        };

        const getLibrarySeasonEpisodes = (seasonNumber) => {
            return detailModal.librarySeriesStatus?.seasons?.[String(seasonNumber)] || [];
        };

        const isEpisodeInLibrary = (seasonNumber, episodeNumber) => {
            return getLibrarySeasonEpisodes(seasonNumber).includes(Number(episodeNumber));
        };

        const getSeasonLibraryState = (season = {}) => {
            const episodes = getLibrarySeasonEpisodes(season.season_number);
            const total = Number(season.episode_count || 0);
            const count = episodes.length;
            if (!count) return { status: 'missing', label: '未入库', count, total };
            if (total && count < total) return { status: 'partial', label: `部分入库 ${count}/${total}`, count, total };
            return { status: 'exists', label: total ? `已入库 ${count}/${total}` : '已入库', count, total };
        };

        const getDetailLibraryState = (detail = detailModal.detail) => {
            if (!detail) return { status: 'missing', label: '未入库' };
            if (detail.media_type !== 'tv') {
                return detail.exists_in_library ? { status: 'exists', label: '已入库' } : { status: 'missing', label: '未入库' };
            }
            const seasons = getDetailSeasons(detail);
            const total = seasons.reduce((sum, season) => sum + Number(season.episode_count || 0), 0);
            const count = seasons.reduce((sum, season) => sum + getLibrarySeasonEpisodes(season.season_number).length, 0);
            if (total > 0) {
                if (!count) return { status: 'missing', label: '未入库', count, total };
                if (count >= total) return { status: 'exists', label: '已入库', count, total };
                return { status: 'partial', label: '部分入库', count, total };
            }
            if (detailModal.librarySeriesStatus?.exists || detail.exists_in_library) return { status: 'exists', label: '已入库' };
            return { status: 'missing', label: '未入库' };
        };

        const buildTmdbImageUrl = (path) => path ? `/api/discover/tmdb_img?path=${path}` : '';

        const normalizeDetailCardItem = (entry = {}, fallbackType = 'movie') => {
            const entryType = entry.media_type || fallbackType || (entry.title ? 'movie' : 'tv');
            return {
                id: entry.id,
                _tmdb_id: entry.id,
                title: entry.title || entry.name || '',
                original_title: entry.original_title || entry.original_name || '',
                year: (entry.release_date || entry.first_air_date || '').toString().slice(0, 4),
                poster_url: entry.poster_url || buildTmdbImageUrl(entry.poster_path),
                backdrop_url: entry.backdrop_url || buildTmdbImageUrl(entry.backdrop_path),
                rating: entry.vote_average || 0,
                overview: entry.overview || '',
                media_type: entryType,
                genre_ids: entry.genre_ids || [],
                source: entry.source || 'tmdb',
                subscribed: false,
                exists_in_library: false,
            };
        };

        const normalizeMediaDetail = (detail = {}, item = {}) => {
            const mediaType = detail.media_type || item.media_type || 'movie';
            const externalIds = detail.external_ids && typeof detail.external_ids === 'object' ? detail.external_ids : {};
            const imdbId = detail.imdb_id || externalIds.imdb_id || '';
            const tvdbId = detail.tvdb_id || externalIds.tvdb_id || '';
            return {
                ...detail,
                tmdb_id: detail.tmdb_id || detail.id || item._tmdb_id || item.id,
                media_type: mediaType,
                title: detail.title || detail.name || item.title || '',
                original_title: detail.original_title || detail.original_name || item.original_title || '',
                year: (detail.release_date || detail.first_air_date || item.year || '').toString().slice(0, 4),
                poster_url: item.poster_url || detail.poster_url || buildTmdbImageUrl(detail.poster_path),
                backdrop_url: detail.backdrop_url || buildTmdbImageUrl(detail.backdrop_path),
                genres: detail.genres || [],
                vote_average: detail.vote_average || item.rating || 0,
                overview: detail.overview || item.overview || '',
                imdb_id: imdbId,
                tvdb_id: tvdbId,
                external_ids: {
                    ...externalIds,
                    imdb_id: imdbId,
                    tvdb_id: tvdbId,
                },
                recommendation_items: (detail.recommendations?.results || []).map(entry => normalizeDetailCardItem(entry, mediaType)).filter(item => item.poster_url),
                similar_items: (detail.similar?.results || []).map(entry => normalizeDetailCardItem(entry, mediaType)).filter(item => item.poster_url),
            };
        };

        const getImdbLink = (detail = detailModal.detail) => {
            const imdbId = detail?.imdb_id || detail?.external_ids?.imdb_id;
            return imdbId ? `https://www.imdb.com/title/${imdbId}` : '';
        };

        const getTvdbLink = (detail = detailModal.detail) => {
            const tvdbId = detail?.tvdb_id || detail?.external_ids?.tvdb_id;
            return tvdbId ? `https://www.thetvdb.com/series/${tvdbId}` : '';
        };

        const closeDetailModalInternal = () => {
            detailModal.visible = false;
            detailModal.item = null;
            detailModal.detail = null;
            detailModal.selectedSeason = null;
            detailModal.seasonSubscribed = false;
            detailModal.seasonExpanded = false;
            detailModal.castExpanded = false;
            detailModal.loading = false;
            detailModal.seasonEpisodes = {};
            detailModal.seasonEpisodesLoading = {};
            detailModal.librarySeriesStatus = { exists: false, seasons: {} };
            detailHistoryActive = false;
        };

        const handleDetailPopstate = () => {
            if (suppressDetailPopstate) {
                suppressDetailPopstate = false;
                return;
            }
            if (detailModal.visible && detailHistoryActive) {
                closeDetailModalInternal();
            }
        };

        const refreshDetailSubscriptionState = async (item = detailModal.item, season = detailModal.selectedSeason) => {
            if (!mpConfig.mp_url || !detailModal.detail) {
                detailModal.subscribed = false;
                detailModal.seasonSubscribed = false;
                return;
            }
            const tmdbId = detailModal.detail.tmdb_id || item?.id;
            const mediaType = detailModal.detail.media_type || item?.media_type || 'movie';
            try {
                const requests = [
                    axios.get('/api/moviepilot/subscribe/check', { params: { tmdbid: tmdbId, type_name: mediaType } })
                ];
                if (mediaType === 'tv' && season != null) {
                    requests.push(
                        axios.get('/api/moviepilot/subscribe/check', { params: { tmdbid: tmdbId, type_name: mediaType, season } })
                    );
                }
                const [mediaRes, seasonRes] = await Promise.all(requests);
                detailModal.subscribed = !!mediaRes?.data?.subscribed;
                detailModal.seasonSubscribed = mediaType === 'tv' && season != null
                    ? !!seasonRes?.data?.subscribed
                    : !!mediaRes?.data?.subscribed;
                if (item) item.subscribed = detailModal.subscribed;
            } catch (e) {
                detailModal.subscribed = false;
                detailModal.seasonSubscribed = false;
            }
        };

        const loadLibrarySeriesStatus = async (tmdbId) => {
            if (!tmdbId) {
                detailModal.librarySeriesStatus = { exists: false, seasons: {} };
                return;
            }
            try {
                const res = await axios.get(`/api/discover/library/series/${tmdbId}`);
                detailModal.librarySeriesStatus = res.data || { exists: false, seasons: {} };
            } catch (e) {
                detailModal.librarySeriesStatus = { exists: false, seasons: {} };
            }
        };

        const loadSeasonEpisodes = async (seasonNumber) => {
            const tmdbId = detailModal.detail?.tmdb_id;
            if (!tmdbId || detailModal.seasonEpisodes[seasonNumber] || detailModal.seasonEpisodesLoading[seasonNumber]) return;
            detailModal.seasonEpisodesLoading[seasonNumber] = true;
            try {
                const res = await axios.get(`/api/discover/tv/${tmdbId}/season/${seasonNumber}`);
                detailModal.seasonEpisodes[seasonNumber] = res.data?.episodes || [];
            } catch (e) {
                detailModal.seasonEpisodes[seasonNumber] = [];
            } finally {
                detailModal.seasonEpisodesLoading[seasonNumber] = false;
            }
        };

        const setDetailSeason = async (seasonNumber, expand = true) => {
            if (seasonNumber == null || seasonNumber === '') {
                detailModal.selectedSeason = null;
                detailModal.seasonExpanded = false;
                detailModal.seasonSubscribed = false;
                return;
            }
            detailModal.selectedSeason = Number(seasonNumber);
            detailModal.seasonExpanded = !!expand;
            if (detailModal.seasonExpanded) loadSeasonEpisodes(detailModal.selectedSeason);
            await refreshDetailSubscriptionState(detailModal.item, detailModal.selectedSeason);
        };

        const toggleDetailSeasonExpanded = async (seasonNumber) => {
            const nextSeason = Number(seasonNumber);
            const expand = detailModal.selectedSeason !== nextSeason || !detailModal.seasonExpanded;
            await setDetailSeason(nextSeason, expand);
        };

        const toggleDetailSeasonSubscription = async (seasonNumber) => {
            await setDetailSeason(seasonNumber, detailModal.seasonExpanded);
            if (detailModal.seasonSubscribed) {
                await unsubscribeMedia(detailModal.item);
            } else {
                await subscribeMedia(detailModal.item);
            }
        };

        const openMediaDetail = async (item) => {
            if (!detailModal.visible || !detailHistoryActive) {
                history.pushState({ ...(history.state || {}), detailModal: true }, '');
                detailHistoryActive = true;
            }
            detailModal.item = item;
            detailModal.visible = true;
            detailModal.loading = true;
            detailModal.subscribed = false;
            detailModal.selectedSeason = null;
            detailModal.seasonExpanded = false;
            detailModal.castExpanded = false;
            detailModal.seasonSubscribed = false;
            try {
                const mediaType = item.media_type || 'movie';
                let tmdbId = item._tmdb_id || item.id;

                if (item.source !== 'tmdb' && !item._tmdb_id) {
                    const resolveKey = getItemExistenceKey(item) || `${item.source || 'source'}:${item.id || item.title || ''}`;
                    const resolveRes = await axios.post('/api/discover/resolve_tmdb', [{
                        _key: resolveKey,
                        title: item.title || item.name || '',
                        year: item.year || '',
                        media_type: mediaType,
                        source: item.source || '',
                        id: item.id || '',
                    }]);
                    const resolvedId = resolveRes.data?.results?.[resolveKey];
                    if (resolvedId) {
                        tmdbId = resolvedId;
                    } else {
                        detailModal.detail = { ...item, overview: item.overview || '暂无简介' };
                        detailModal.loading = false;
                        return;
                    }
                }

                // 获取 TMDB 详情
                const res = await axios.get(`/api/discover/detail/${tmdbId}`, { params: { type: mediaType } });
                const detail = normalizeMediaDetail(res.data, item);
                detail.tmdb_id = tmdbId;
                item._tmdb_id = tmdbId;  // 缓存到 item 上，避免重复搜索
                await markLibraryExists([item, ...detail.recommendation_items, ...detail.similar_items]);
                detail.exists_in_library = !!item.exists_in_library;
                detailModal.detail = detail;
                if (mediaType === 'tv') {
                    await loadLibrarySeriesStatus(tmdbId);
                }

                const detailSeasons = getDetailSeasons(detail);
                if (mediaType === 'tv' && detailSeasons.length) {
                    await setDetailSeason(Number(detailSeasons[0].season_number), true);
                }

                await refreshDetailSubscriptionState(item, detailModal.selectedSeason);
            } catch (e) {
                console.error('加载详情失败:', e);
                detailModal.detail = normalizeMediaDetail({ overview: item.overview || '暂无简介' }, item);
            } finally {
                detailModal.loading = false;
            }
        };

        const closeDetailModal = () => {
            if (detailModal.visible && detailHistoryActive) {
                suppressDetailPopstate = true;
                history.back();
                closeDetailModalInternal();
                return;
            }
            closeDetailModalInternal();
        };

        const subscribeMedia = async (item) => {
            if (!mpConfig.mp_url) {
                showToast('请先配置 MoviePilot 连接信息', 'warning');
                return;
            }
            try {
                const tmdbId = (detailModal.detail && detailModal.detail.tmdb_id) || item.id;
                const mediaType = detailModal.detail?.media_type || item.media_type || 'movie';
                const body = {
                    tmdbid: tmdbId,
                    type_name: mediaType,
                    name: detailModal.detail?.title || item.title,
                    year: detailModal.detail?.year || item.year
                };
                if (mediaType === 'tv' && detailModal.selectedSeason != null) {
                    body.season = detailModal.selectedSeason;
                }
                await axios.post('/api/moviepilot/subscribe', body);
                detailModal.subscribed = true;
                detailModal.seasonSubscribed = true;
                if (detailModal.item) detailModal.item.subscribed = true;
                showToast(mediaType === 'tv' && detailModal.selectedSeason != null ? `已订阅第 ${detailModal.selectedSeason} 季` : '订阅成功', 'success');
            } catch (e) {
                showToast('订阅失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const unsubscribeMedia = async (item) => {
            try {
                const tmdbId = (detailModal.detail && detailModal.detail.tmdb_id) || item.id;
                const mediaType = detailModal.detail?.media_type || item.media_type || 'movie';
                const params = { tmdbid: tmdbId, type_name: mediaType };
                if (mediaType === 'tv' && detailModal.selectedSeason != null) {
                    params.season = detailModal.selectedSeason;
                }
                await axios.delete('/api/moviepilot/subscribe', { params });
                detailModal.subscribed = false;
                detailModal.seasonSubscribed = false;
                if (detailModal.item) detailModal.item.subscribed = false;
                showToast(mediaType === 'tv' && detailModal.selectedSeason != null ? `已取消第 ${detailModal.selectedSeason} 季订阅` : '已取消订阅', 'success');
            } catch (e) {
                showToast('取消失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const openRowGrid = async (row) => {
            gridModal.visible = true;
            gridModal.row = row;
            gridModal.title = row.title;
            gridModal.page = 1;
            gridModal.items = [];
            gridModal.totalPages = 1;
            gridModal.loadingMore = true;
            gridModal.noMore = false;
            try {
                const params = row.source === 'douban' ? { start: 0, count: 30 } : { page: 1 };
                const res = await axios.get(row.endpoint, { params });
                const items = res.data.items || [];
                await markLibraryExists(items);
                gridModal.items = applyLibraryStatusFilter(items);
                gridModal.totalPages = res.data.total_pages || 1;
            } catch (e) {
                showToast('加载失败', 'error');
            } finally {
                gridModal.loadingMore = false;
                if (gridModal.page >= gridModal.totalPages) gridModal.noMore = true;
                // Wait for DOM update then setup intersection observer
                nextTick(() => setupGridObserver());
            }
        };

        const loadGridNextPage = async () => {
            const row = gridModal.row;
            if (!row || gridModal.loadingMore || gridModal.noMore) return;
            gridModal.page++;
            gridModal.loadingMore = true;
            try {
                const params = row.source === 'douban' ? { start: (gridModal.page - 1) * 30, count: 30 } : { page: gridModal.page };
                const res = await axios.get(row.endpoint, { params });
                const newItems = res.data.items || [];
                await markLibraryExists(newItems);
                gridModal.totalPages = res.data.total_pages || 1;
                gridModal.items.push(...applyLibraryStatusFilter(newItems));
            } catch (e) {
                gridModal.page--;
            } finally {
                gridModal.loadingMore = false;
                if (gridModal.page >= gridModal.totalPages) gridModal.noMore = true;
            }
        };

        const setupGridObserver = () => {
            if (gridObserver) { gridObserver.disconnect(); gridObserver = null; }
            if (!gridSentinel.value || gridModal.noMore) return;
            gridObserver = new IntersectionObserver((entries) => {
                if (entries[0].isIntersecting && !gridModal.loadingMore && !gridModal.noMore) {
                    loadGridNextPage();
                }
            }, { root: gridModalEl.value, threshold: 0.1 });
            gridObserver.observe(gridSentinel.value);
            // sentinel 已在可视区域内时，IntersectionObserver 不会回调，需立即检查
            if (gridModal.items.length > 0) {
                const rect = gridSentinel.value.getBoundingClientRect();
                const rootRect = gridModalEl.value?.getBoundingClientRect();
                if (rootRect && rect.top < rootRect.bottom && !gridModal.noMore) {
                    loadGridNextPage();
                }
            }
        };

        const closeGridModal = () => {
            if (gridObserver) { gridObserver.disconnect(); gridObserver = null; }
            gridModal.visible = false;
            gridModal.row = null;
            gridModal.items = [];
        };

        const searchDiscover = async (append = false) => {
            const q = discoverSearchQuery.value.trim();
            if (!q) return;
            if (!append) {
                searchPage.value = 1;
                searchMovieResults.value = [];
                searchTvResults.value = [];
            }
            discoverSearchLoading.value = true;
            discoverHasSearched.value = true;
            try {
                const page = searchPage.value;
                // 同时搜电影+剧集
                const [movieRes, tvRes] = await Promise.all([
                    axios.get('/api/discover/search', { params: { query: q, type: 'movie', page } }),
                    axios.get('/api/discover/search', { params: { query: q, type: 'tv', page } })
                ]);
                const movieItems = movieRes.data.items || [];
                const tvItems = tvRes.data.items || [];

                // 各自按标题相似度排序
                const sortFn = (a, b) => {
                    const qLower = q.toLowerCase();
                    const aTitle = (a.title || '').toLowerCase();
                    const bTitle = (b.title || '').toLowerCase();
                    const aExact = aTitle === qLower ? 0 : 1;
                    const bExact = bTitle === qLower ? 0 : 1;
                    if (aExact !== bExact) return aExact - bExact;
                    const aStarts = aTitle.startsWith(qLower) ? 0 : 1;
                    const bStarts = bTitle.startsWith(qLower) ? 0 : 1;
                    if (aStarts !== bStarts) return aStarts - bStarts;
                    const aIncludes = aTitle.includes(qLower) ? 0 : 1;
                    const bIncludes = bTitle.includes(qLower) ? 0 : 1;
                    if (aIncludes !== bIncludes) return aIncludes - bIncludes;
                    return (b.rating || 0) - (a.rating || 0);
                };
                movieItems.sort(sortFn);
                tvItems.sort(sortFn);
                await markLibraryExists([...movieItems, ...tvItems]);

                searchTotalPages.value = Math.max(movieRes.data.total_pages || 1, tvRes.data.total_pages || 1);
                const filteredMovieItems = applyLibraryStatusFilter(movieItems);
                const filteredTvItems = applyLibraryStatusFilter(tvItems);
                if (append) {
                    searchMovieResults.value.push(...filteredMovieItems);
                    searchTvResults.value.push(...filteredTvItems);
                } else {
                    searchMovieResults.value = filteredMovieItems;
                    searchTvResults.value = filteredTvItems;
                }
            } catch (e) {
                showToast('搜索失败', 'error');
            } finally {
                discoverSearchLoading.value = false;
            }
        };

        const loadMoreSearch = () => {
            if (searchPage.value < searchTotalPages.value && !discoverSearchLoading.value) {
                searchPage.value++;
                searchDiscover(true);
            }
        };

        const clearDiscoverSearch = () => {
            discoverSearchQuery.value = '';
            searchMovieResults.value = [];
            searchTvResults.value = [];
            discoverHasSearched.value = false;
            searchPage.value = 1;
            searchTotalPages.value = 1;
        };

    return {
        detailModal,
        openMediaDetail,
        closeDetailModal,
        handleDetailPopstate,
        missingEpisodeStats,
        missingEpisodeLibraries,
        missingEpisodeActiveLibrary,
        missingEpisodeActiveSummary,
        missingEpisodeActiveErrorCount,
        missingEpisodeSearchActive,
        missingEpisodeStatsProblemItems,
        visibleMissingEpisodeStatsProblemItems,
        missingEpisodeHasMoreVisibleItems,
        onMissingEpisodeLazyScroll,
        loadMissingEpisodeStatsShell,
        runMissingEpisodeStats,
        refreshMissingEpisodeStats,
        setMissingEpisodeLibrary,
        setMissingEpisodeFilter,
        setMissingEpisodeStatusFilter,
        setMissingEpisodeSort,
        openDiscoverFromMissingStats,
        setDetailSeason,
        toggleDetailSeasonExpanded,
        toggleDetailSeasonSubscription,
        loadSeasonEpisodes,
        getSeasonLibraryState,
        getDetailLibraryState,
        isEpisodeInLibrary,
        subscribeMedia,
        unsubscribeMedia,
        getImdbLink,
        getTvdbLink,
        gridModal,
        gridModalEl,
        gridSentinel,
        openRowGrid,
        closeGridModal,
        searchMovieResults,
        searchTvResults,
        discoverSearchLoading,
        discoverSearchQuery,
        discoverHasSearched,
        searchPage,
        searchTotalPages,
        searchDiscover,
        loadMoreSearch,
        clearDiscoverSearch,
        genreList,
        discoverSourceTabs,
        discoverActiveSource,
        discoverSourceSupported,
        discoverEmptyText,
        activeSourceDef,
        activeSourceSchema,
        activeSourceFilters,
        getVisibleFilterRows,
        switchDiscoverSource,
        updateSourceFilter,
        toggleSourceChip,
        applyNumberFilter,
        loadDiscoverSources,
        mainGridItems,
        mainGridPage,
        mainGridTotalPages,
        mainGridLoading,
        mainGridNoMore,
        mainGridSentinel,
        mainGridScrollRoot,
        loadMainGrid,
        resetMainGrid,
    };
}
