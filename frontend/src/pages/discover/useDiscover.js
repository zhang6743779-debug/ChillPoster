import axios from 'axios';
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue';

export function useDiscover({ tab, isMobile, openPanels, focusedPanel, closeDockDrawers, mobileMenuVisible, mpConfig, config302, servers, ensureDashboardServerId, showToast }) {
        // ==========================================
        // 14. 发现推荐页
        // ==========================================
        const RESOURCE_SEARCH_SOURCE_STORAGE_KEY = 'chillposter-discover-resource-search-sources';
        const DISCOVER_SOURCES_CACHE_KEY = 'cp_discover_sources';
        const DISCOVER_SOURCES_CACHE_VERSION = 1;
        const DISCOVER_MAIN_GRID_CACHE_KEY = 'cp_discover_main_grid';
        const DISCOVER_MAIN_GRID_CACHE_VERSION = 1;
        const DISCOVER_MAIN_GRID_CACHE_MAX_ENTRIES = 12;
        const DISCOVER_MAIN_GRID_CACHE_MAX_CHARS = 2000000;
        const MISSING_EPISODE_STATS_CACHE_KEY = 'cp_missing_episode_stats';
        const MISSING_EPISODE_STATS_CACHE_VERSION = 1;
        const MISSING_EPISODE_STATS_CACHE_MAX_ENTRIES = 4;
        const MISSING_EPISODE_STATS_CACHE_MAX_CHARS = 3500000;
        const MISSING_EPISODE_FULL_CACHE_ITEM_LIMIT = 1600;
        const MISSING_EPISODE_RENDER_LIMIT_DESKTOP = 36;
        const MISSING_EPISODE_RENDER_LIMIT_MOBILE = 16;
        const MISSING_EPISODE_RENDER_STEP_DESKTOP = 24;
        const MISSING_EPISODE_RENDER_STEP_MOBILE = 12;
        const missingEpisodeRenderPage = ref(0);
        const missingEpisodePosterGridRef = ref(null);
        const missingEpisodeLoadMoreSentinel = ref(null);

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
        const missingEpisodeCompareModal = reactive({ visible: false, row: null });
        const mpSubscribeModal = reactive({
            visible: false,
            loading: false,
            title: '',
            year: '',
            tmdbId: '',
            mediaType: 'tv',
            seasons: [],
            selectedMode: 'all',
            selectedSeasons: [],
        });
        const resourceSearchSources = ref([]);
        const resourceSearchSourceLoading = ref(false);
        const resourceSearchSourceMenuOpen = ref(false);
        const selectedResourceSearchSources = ref([]);
        const resourceSearchModal = reactive({
            visible: false,
            context: '',
            loading: false,
            items: [],
            error: '',
            title: '',
            mediaType: 'movie',
            tmdbId: '',
            sources: [],
            season: null,
            episode: null,
            transferringId: '',
            previewingId: '',
        });
        let resourceSearchSourcesPromise = null;

        const readLocalJson = (key) => {
            try {
                const raw = localStorage.getItem(key);
                return raw ? JSON.parse(raw) : null;
            } catch (_) {
                return null;
            }
        };

        const writeLocalJson = (key, payload, maxChars = 0) => {
            try {
                const serialized = JSON.stringify(payload);
                if (maxChars > 0 && serialized.length > maxChars) return false;
                localStorage.setItem(key, serialized);
                return true;
            } catch (_) {
                return false;
            }
        };

        const getLocalCacheBucket = (storageKey, version) => {
            const bucket = readLocalJson(storageKey);
            if (!bucket || bucket.version !== version || !bucket.entries || typeof bucket.entries !== 'object') {
                return { version, entries: {}, order: [] };
            }
            return {
                version,
                entries: bucket.entries || {},
                order: Array.isArray(bucket.order) ? bucket.order : Object.keys(bucket.entries || {}),
            };
        };

        const getLocalCacheEntry = (storageKey, version, entryKey) => {
            if (!entryKey) return null;
            const bucket = getLocalCacheBucket(storageKey, version);
            const entry = bucket.entries?.[entryKey] || null;
            return entry && typeof entry === 'object' ? entry : null;
        };

        const setLocalCacheEntry = (storageKey, version, entryKey, entry, options = {}) => {
            if (!entryKey || !entry) return false;
            const maxEntries = Number(options.maxEntries || 8);
            const maxChars = Number(options.maxChars || 0);
            const bucket = getLocalCacheBucket(storageKey, version);
            bucket.entries[entryKey] = entry;
            bucket.order = [entryKey, ...bucket.order.filter(key => key !== entryKey && bucket.entries[key])];
            while (bucket.order.length > maxEntries) {
                const staleKey = bucket.order.pop();
                if (staleKey) delete bucket.entries[staleKey];
            }
            if (writeLocalJson(storageKey, bucket, maxChars)) return true;
            while (bucket.order.length > 1) {
                const staleKey = bucket.order.pop();
                if (staleKey) delete bucket.entries[staleKey];
                if (writeLocalJson(storageKey, bucket, maxChars)) return true;
            }
            delete bucket.entries[entryKey];
            bucket.order = bucket.order.filter(key => key !== entryKey);
            writeLocalJson(storageKey, bucket, maxChars);
            return false;
        };

        const stableCacheStringify = (value) => {
            const normalize = (input) => {
                if (Array.isArray(input)) return input.map(normalize);
                if (input && typeof input === 'object') {
                    return Object.keys(input).sort().reduce((acc, key) => {
                        const nextValue = input[key];
                        if (nextValue !== undefined) acc[key] = normalize(nextValue);
                        return acc;
                    }, {});
                }
                return input == null ? '' : input;
            };
            try {
                return JSON.stringify(normalize(value));
            } catch (_) {
                return '';
            }
        };

        const getPrimaryServerFingerprint = () => {
            const svr = servers?.value?.[0] || (Array.isArray(config302?.embys) ? config302.embys[0] : null) || {};
            const keyTail = String(svr.key || '').slice(-8);
            return [svr.url || '', svr.public_host || '', keyTail].join('|') || 'no-server';
        };

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
        const selectedResourceSearchSourceLabels = computed(() => {
            const selected = new Set(selectedResourceSearchSources.value || []);
            return (resourceSearchSources.value || [])
                .filter(source => selected.has(source.key))
                .map(source => source.label || source.name || source.key);
        });
        const resourceSearchSourceButtonText = computed(() => {
            const labels = selectedResourceSearchSourceLabels.value;
            return labels.length ? `搜索源: ${labels.join(' / ')}` : '搜索源';
        });
        const resourceSearchSourceReady = computed(() => {
            return resourceSearchSources.value.length > 0 && selectedResourceSearchSources.value.length > 0;
        });

        const readSavedResourceSearchSources = () => {
            try {
                const raw = localStorage.getItem(RESOURCE_SEARCH_SOURCE_STORAGE_KEY);
                const parsed = JSON.parse(raw || '[]');
                return Array.isArray(parsed) ? parsed.map(item => String(item || '').trim()).filter(Boolean) : [];
            } catch (_) {
                return [];
            }
        };

        const saveSelectedResourceSearchSources = () => {
            try {
                localStorage.setItem(RESOURCE_SEARCH_SOURCE_STORAGE_KEY, JSON.stringify(selectedResourceSearchSources.value || []));
            } catch (_) {}
        };

        const reconcileResourceSearchSelection = () => {
            const availableKeys = (resourceSearchSources.value || []).map(source => source.key).filter(Boolean);
            const availableSet = new Set(availableKeys);
            const current = (selectedResourceSearchSources.value || []).filter(key => availableSet.has(key));
            const saved = readSavedResourceSearchSources().filter(key => availableSet.has(key));
            const next = current.length ? current : (saved.length ? saved : availableKeys);
            selectedResourceSearchSources.value = Array.from(new Set(next));
            saveSelectedResourceSearchSources();
        };

        const loadResourceSearchSources = async (force = false) => {
            if (resourceSearchSourceLoading.value && resourceSearchSourcesPromise) return resourceSearchSourcesPromise;
            if (!force && resourceSearchSources.value.length) return;
            resourceSearchSourceLoading.value = true;
            resourceSearchSourcesPromise = (async () => {
                try {
                    const res = await axios.get('/api/forward/search_sources');
                    resourceSearchSources.value = res.data?.sources || [];
                    reconcileResourceSearchSelection();
                } catch (e) {
                    console.error('加载资源搜索源失败:', e);
                    resourceSearchSources.value = [];
                    selectedResourceSearchSources.value = [];
                } finally {
                    resourceSearchSourceLoading.value = false;
                    resourceSearchSourcesPromise = null;
                }
            })();
            return resourceSearchSourcesPromise;
        };

        const toggleResourceSearchSourceMenu = async () => {
            const nextOpen = !resourceSearchSourceMenuOpen.value;
            resourceSearchSourceMenuOpen.value = nextOpen;
            if (nextOpen) await loadResourceSearchSources(true);
        };

        const closeResourceSearchSourceMenu = () => {
            resourceSearchSourceMenuOpen.value = false;
        };

        const handleResourceSearchSourceOutsideClick = () => {
            if (resourceSearchSourceMenuOpen.value) closeResourceSearchSourceMenu();
        };

        const toggleResourceSearchSource = (key) => {
            key = String(key || '').trim();
            if (!key) return;
            const selected = new Set(selectedResourceSearchSources.value || []);
            if (selected.has(key)) {
                if (selected.size <= 1) {
                    showToast?.('至少保留一个资源搜索源', 'warning');
                    return;
                }
                selected.delete(key);
            } else {
                selected.add(key);
            }
            selectedResourceSearchSources.value = Array.from(selected);
            saveSelectedResourceSearchSources();
        };

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

        const applyDiscoverSourcesData = async (data = {}, options = {}) => {
            const sources = Array.isArray(data.sources) ? data.sources : [];
            if (!sources.length) return false;
            discoverSourceTabs.value = sources;
            discoverSourceTabs.value.forEach(source => ensureSourceFilters(source));
            if (Array.isArray(data.genres) && data.genres.length) {
                genreList.value = data.genres;
                patchTmdbGenreSchema();
            }
            loadResourceSearchSources();
            if (options.fetchGenres !== false) {
                await fetchGenreList();
                patchTmdbGenreSchema();
            }
            if (!discoverSourceMap.value[discoverActiveSource.value] && discoverSourceTabs.value.length) {
                discoverActiveSource.value = discoverSourceTabs.value[0].key;
            }
            return true;
        };

        let discoverSourcesRefreshPromise = null;

        const refreshDiscoverSources = async () => {
            if (discoverSourcesRefreshPromise) return discoverSourcesRefreshPromise;
            discoverSourcesRefreshPromise = (async () => {
                const res = await axios.get('/api/discover/sources');
                await applyDiscoverSourcesData({ sources: res.data.sources || [] });
                writeLocalJson(DISCOVER_SOURCES_CACHE_KEY, {
                    version: DISCOVER_SOURCES_CACHE_VERSION,
                    updatedAt: Date.now(),
                    sources: discoverSourceTabs.value,
                    genres: genreList.value,
                }, 600000);
            })().catch(e => {
                console.error('加载发现源失败:', e);
            }).finally(() => {
                discoverSourcesRefreshPromise = null;
            });
            return discoverSourcesRefreshPromise;
        };

        const loadDiscoverSources = async () => {
            if (discoverSourceTabs.value.length) return;
            const cached = readLocalJson(DISCOVER_SOURCES_CACHE_KEY);
            if (cached?.version === DISCOVER_SOURCES_CACHE_VERSION && Array.isArray(cached.sources) && cached.sources.length) {
                await applyDiscoverSourcesData(cached, { fetchGenres: false });
                refreshDiscoverSources();
                return;
            }
            await refreshDiscoverSources();
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
        let discoverRealtimeEventSource = null;
        let _mainGridGen = 0;
        const mainGridPrefetch = reactive({ pages: {} });
        const MAIN_GRID_PREFETCH_AHEAD = 2;
        const emptyMissingEpisodeSummary = () => ({
            tvCount: 0,
            completeCount: 0,
            manualCompleteCount: 0,
            partialCount: 0,
            missingCount: 0,
            errorCount: 0,
            airingRecentMissingCount: 0,
            airingAiredMissingCount: 0,
            endedMissingCount: 0,
            otherMissingCount: 0,
            presentEpisodes: 0,
            totalEpisodes: 0,
            missingEpisodes: 0,
            actionableMissingEpisodes: 0,
        });
        const missingEpisodeStats = reactive({
            loading: false,
            loaded: false,
            ready: true,
            error: '',
            message: '',
            items: [],
            libraries: [],
            loadingAction: '',
            manualCompleteUpdating: {},
            activeLibraryKey: '',
            filter: 'all',
            sortBy: 'year_desc',
            searchQuery: '',
            meta: {},
            summary: emptyMissingEpisodeSummary(),
            progress: { current: 0, total: 0 },
        });
        let missingEpisodeStatsRunId = 0;
        let missingEpisodeStatsPollTimer = null;
        const MISSING_EPISODE_ALL_LIBRARY_KEY = '__all_libraries__';
        const getMissingEpisodeLibraryKey = (lib = {}) => lib.libraryId || lib.libraryName || '';
        const missingEpisodeLibraries = computed(() => {
            const libraries = missingEpisodeStats.libraries || [];
            if (!libraries.length) return [];
            return [
                {
                    libraryId: MISSING_EPISODE_ALL_LIBRARY_KEY,
                    libraryName: '全部媒体库',
                    summary: missingEpisodeStats.summary || emptyMissingEpisodeSummary(),
                    items: missingEpisodeStats.items || [],
                },
                ...libraries,
            ];
        });
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
            return Number(summary.errorCount) || 0;
        });
        const missingEpisodeActionableMissingCount = computed(() => {
            const summary = missingEpisodeActiveSummary.value || {};
            return (Number(summary.airingAiredMissingCount) || 0) + (Number(summary.endedMissingCount) || 0);
        });
        const missingEpisodeActionableEpisodeCount = computed(() => {
            const summary = missingEpisodeActiveSummary.value || {};
            return Number(summary.actionableMissingEpisodes) || 0;
        });
        const missingEpisodeSearchActive = computed(() => !!String(missingEpisodeStats.searchQuery || '').trim());
        const isMissingEpisodeActionableMissing = (item = {}) => {
            const category = String(item.missingCategory || '').trim().toLowerCase();
            return category === 'ended_missing' || category === 'airing_aired_missing';
        };
        const missingEpisodeStatsProblemItems = computed(() => {
            const query = String(missingEpisodeStats.searchQuery || '').trim().toLowerCase();
            const sourceItems = query
                ? (missingEpisodeStats.items || [])
                : (missingEpisodeActiveLibrary.value?.items || missingEpisodeStats.items || []);
            const filteredItems = sourceItems.filter(item => {
                if (missingEpisodeStats.filter === 'all') return isMissingEpisodeActionableMissing(item);
                if (missingEpisodeStats.filter === 'manual_complete') return !!item.manualComplete;
                if (missingEpisodeStats.filter === 'error') return item.missingCategory === 'error' || item.status === 'error' || item.status === 'missing';
                if (missingEpisodeStats.filter === 'complete') return item.missingCategory === 'complete' || !!item.manualComplete;
                if (missingEpisodeStats.filter === 'airing_aired_missing') return item.missingCategory === 'airing_aired_missing';
                if (missingEpisodeStats.filter === 'airing_recent_missing') return item.missingCategory === 'airing_recent_missing';
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
            const releaseTime = (item) => {
                const raw = item.releaseDate || item.item?.first_air_date || item.item?.release_date || '';
                const timestamp = raw ? Date.parse(String(raw).slice(0, 10)) : NaN;
                if (Number.isFinite(timestamp)) return timestamp;
                const fallbackYear = year(item);
                return fallbackYear ? Date.parse(`${fallbackYear}-01-01`) : 0;
            };
            const missingRatio = (item) => {
                const total = num(item.totalEpisodes);
                return total ? num(item.missingEpisodes) / total : 0;
            };
            const sortedItems = [...filteredItems];
            sortedItems.sort((a, b) => {
                switch (missingEpisodeStats.sortBy) {
                    case 'year_desc':
                        return releaseTime(b) - releaseTime(a) || num(b.missingEpisodes) - num(a.missingEpisodes);
                    case 'year_asc':
                        return releaseTime(a) - releaseTime(b) || num(b.missingEpisodes) - num(a.missingEpisodes);
                    case 'missing_asc':
                        return num(a.missingEpisodes) - num(b.missingEpisodes) || releaseTime(b) - releaseTime(a);
                    case 'ratio_desc':
                        return missingRatio(b) - missingRatio(a) || num(b.missingEpisodes) - num(a.missingEpisodes);
                    case 'ratio_asc':
                        return missingRatio(a) - missingRatio(b) || num(b.missingEpisodes) - num(a.missingEpisodes);
                    case 'title_asc':
                        return String(a.title || '').localeCompare(String(b.title || ''), 'zh-Hans-CN') || releaseTime(b) - releaseTime(a);
                    case 'missing_desc':
                    default:
                        return num(b.missingEpisodes) - num(a.missingEpisodes) || releaseTime(b) - releaseTime(a);
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
        let missingEpisodeLazyEnsurePending = false;
        let missingEpisodeLoadMoreObserver = null;
        let missingEpisodeObserverRetryTimer = null;
        let missingEpisodeScrollFallbackTicking = false;
        let missingEpisodeScrollFallbackLastAt = 0;

        const getMissingEpisodePosterKey = (row = {}) => [
            row.libraryId || '',
            row.tmdbId || row.item?.tmdb_id || row.item?.id || row.title || '',
            row.poster_url || '',
        ].join('|');

        const isMissingEpisodeAiringStatus = (status = '') => {
            const normalized = String(status || '').trim().toLowerCase();
            return normalized === 'returning series'
                || normalized === 'in production'
                || normalized === 'planned'
                || normalized === 'pilot';
        };

        const isMissingEpisodeErrorRow = (row = {}) => {
            return row?.missingCategory === 'error'
                || row?.status === 'error'
                || row?.status === 'missing';
        };

        const isMissingEpisodeManualComplete = (row = {}) => !!row?.manualComplete;

        const getMissingEpisodeManualUpdateKey = (row = {}) => [
            row.libraryId || '',
            row.tmdbId || row.item?.tmdb_id || row.item?.id || '',
        ].join('|');

        const isMissingEpisodeManualCompleteUpdating = (row = {}) => {
            return !!missingEpisodeStats.manualCompleteUpdating[getMissingEpisodeManualUpdateKey(row)];
        };

        const getMissingEpisodeOriginalStatus = (row = {}) => {
            return row?.manualCompleteOriginal?.status || row?.status || '';
        };

        const getMissingEpisodeOriginalCategory = (row = {}) => {
            return row?.manualCompleteOriginal?.missingCategory || row?.missingCategory || '';
        };

        const shouldShowMissingEpisodeTmdbCompare = (row = missingEpisodeCompareModal.row) => {
            if (isMissingEpisodeManualComplete(row)) {
                return ['error', 'missing'].includes(String(getMissingEpisodeOriginalStatus(row)).toLowerCase())
                    || getMissingEpisodeOriginalCategory(row) === 'error'
                    || (Number(row?.extraLocalEpisodes) || Number(row?.manualCompleteOriginal?.extraLocalEpisodes) || 0) > 0;
            }
            return isMissingEpisodeErrorRow(row);
        };

        const getMissingEpisodePosterCategoryLabel = (row = {}) => {
            if (isMissingEpisodeManualComplete(row)) {
                const localCount = Number(row.manualCompleteLocalEpisodes) || countLocalEpisodes(row.localItem?.seasons);
                return localCount ? `已标记完整 ${localCount}集` : '已标记完整';
            }
            const category = String(row.missingCategory || '').trim().toLowerCase();
            const total = Number(row.totalEpisodes) || 0;
            const present = Number(row.presentEpisodes) || 0;
            const isComplete = String(row.status || '').trim().toLowerCase() === 'exists';
            const isAiring = isMissingEpisodeAiringStatus(row.tmdbStatus);
            const withDetail = (baseLabel) => total > 0 ? `${baseLabel} ${present}/${total}` : baseLabel;
            if (category === 'airing_recent_missing') {
                return withDetail((Number(row.airedMissingEpisodes) || 0) > 0 ? '已播缺集' : '连载未缺集');
            }
            if (category === 'airing_aired_missing') return withDetail(isComplete ? '连载' : '已播缺集');
            if (category === 'ended_missing') return withDetail(isComplete ? '完结' : '完结缺集');
            if (category === 'error') return '异常入库';
            if (category === 'partial_missing') return withDetail(isAiring ? '连载缺集' : '完结缺集');
            if (isComplete || category === 'complete') return withDetail('完整入库');
            const categoryLabel = String(row.categoryLabel || '');
            if (categoryLabel.includes('连载')) return withDetail(isComplete ? '连载' : '连载缺集');
            if (categoryLabel.includes('完结')) return withDetail(isComplete ? '完结' : '完结缺集');
            return withDetail(isAiring ? (isComplete ? '连载' : '连载缺集') : (isComplete ? '完结' : '完结缺集'));
        };

        const isMissingEpisodePosterReady = (row = {}) => {
            return !!row.poster_url;
        };

        const countLocalEpisodes = (seasons = {}) => {
            return Object.values(seasons || {}).reduce((total, episodes) => {
                return total + (Array.isArray(episodes) ? episodes.length : 0);
            }, 0);
        };

        const formatLocalSeasonBrief = (seasons = {}) => {
            const entries = Object.entries(seasons || {})
                .map(([season, episodes]) => ({ season: Number(season), episodes: Array.isArray(episodes) ? episodes : [] }))
                .filter(item => item.season > 0)
                .sort((a, b) => a.season - b.season);
            if (!entries.length) return '暂无本地季集信息';
            return entries.slice(0, 4).map(item => `S${String(item.season).padStart(2, '0')} ${item.episodes.length} 集`).join(' / ')
                + (entries.length > 4 ? ` / 另 ${entries.length - 4} 季` : '');
        };

        const normalizeEpisodeList = (episodes = []) => {
            if (!Array.isArray(episodes)) return [];
            return Array.from(new Set(episodes
                .map(ep => Number(ep))
                .filter(ep => Number.isFinite(ep) && ep > 0)))
                .sort((a, b) => a - b);
        };

        const formatEpisodeNumber = (episode) => {
            const ep = Number(episode);
            return Number.isFinite(ep) ? String(ep).padStart(2, '0') : String(episode || '');
        };

        const getLocalSeasonRows = (row = missingEpisodeCompareModal.row) => {
            const seasons = row?.localItem?.seasons || {};
            const showTmdbCompare = shouldShowMissingEpisodeTmdbCompare(row);
            const tmdbSeasonMap = new Map((row?.seasons || [])
                .map(season => [Number(season.seasonNumber), season])
                .filter(([season]) => Number.isFinite(season) && season > 0));
            const extraSeasonMap = new Map((row?.extraLocalSeasons || [])
                .map(season => [Number(season.seasonNumber), normalizeEpisodeList(season.episodes)])
                .filter(([season, episodes]) => Number.isFinite(season) && season > 0 && episodes.length > 0));
            return Object.entries(seasons)
                .map(([season, episodes]) => {
                    const seasonNumber = Number(season);
                    const episodeList = normalizeEpisodeList(episodes);
                    const tmdbSeason = tmdbSeasonMap.get(seasonNumber);
                    const hasExplicitExtra = Array.isArray(tmdbSeason?.extraEpisodes);
                    const explicitExtra = normalizeEpisodeList(tmdbSeason?.extraEpisodes);
                    const extraEpisodes = !showTmdbCompare ? [] : extraSeasonMap.get(seasonNumber)
                        || (hasExplicitExtra ? explicitExtra : (tmdbSeason
                            ? episodeList.filter(ep => ep > (Number(tmdbSeason.total) || 0))
                            : episodeList));
                    return {
                        season: seasonNumber,
                        episodes: episodeList,
                        extraEpisodes: normalizeEpisodeList(extraEpisodes),
                    };
                })
                .filter(item => item.season > 0 && item.episodes.length > 0)
                .sort((a, b) => a.season - b.season);
        };

        const getTmdbSeasonRows = (row = missingEpisodeCompareModal.row) => {
            return (row?.seasons || [])
                .map(season => {
                    const seasonNumber = Number(season.seasonNumber);
                    const total = Number(season.total) || 0;
                    return {
                        season: seasonNumber,
                        total,
                        present: Number(season.present) || 0,
                        missing: Number(season.missing) || 0,
                        episodes: normalizeEpisodeList(season.episodeNumbers).length
                            ? normalizeEpisodeList(season.episodeNumbers)
                            : Array.from({ length: total }, (_, idx) => idx + 1),
                        presentEpisodes: normalizeEpisodeList(season.presentEpisodes),
                        missingEpisodes: normalizeEpisodeList(season.missingEpisodes),
                        airedMissingEpisodes: normalizeEpisodeList(season.airedMissingEpisodes),
                        extraEpisodes: normalizeEpisodeList(season.extraEpisodes),
                    };
                })
                .filter(item => item.season > 0 && item.total > 0)
                .sort((a, b) => a.season - b.season);
        };

        const isEpisodeListed = (episodes = [], episode) => {
            return Array.isArray(episodes) && episodes.includes(Number(episode));
        };

        const openMissingEpisodeCompare = (row = {}) => {
            missingEpisodeCompareModal.row = row;
            missingEpisodeCompareModal.visible = true;
        };

        const closeMissingEpisodeCompare = () => {
            missingEpisodeCompareModal.visible = false;
            missingEpisodeCompareModal.row = null;
            if (resourceSearchModal.context === 'missing_episode') {
                resourceSearchModal.visible = false;
                resourceSearchModal.context = '';
                resourceSearchModal.loading = false;
                resourceSearchModal.items = [];
                resourceSearchModal.error = '';
                resourceSearchModal.transferringId = '';
            }
        };

        const isMissingEpisodeCompareTopLayer = () => (
            missingEpisodeCompareModal.visible &&
            !mpSubscribeModal.visible &&
            !resourceSearchModal.visible &&
            !detailModal.visible &&
            !gridModal.visible
        );

        const handleMissingEpisodeCompareOutsidePointerDown = (event) => {
            if (!isMissingEpisodeCompareTopLayer()) return;
            const panel = document.querySelector('.missing-episode-compare-panel');
            if (!panel || panel.contains(event.target)) return;
            closeMissingEpisodeCompare();
        };

        const handleMissingEpisodeCompareKeydown = (event) => {
            if (event.key !== 'Escape' || !isMissingEpisodeCompareTopLayer()) return;
            event.preventDefault();
            closeMissingEpisodeCompare();
        };

        const openMissingEpisodeCard = (row = {}) => {
            openMissingEpisodeCompare(row);
        };

        const teardownMissingEpisodeLoadMoreObserver = () => {
            if (missingEpisodeLoadMoreObserver) {
                missingEpisodeLoadMoreObserver.disconnect();
                missingEpisodeLoadMoreObserver = null;
            }
            if (missingEpisodeObserverRetryTimer) {
                clearTimeout(missingEpisodeObserverRetryTimer);
                missingEpisodeObserverRetryTimer = null;
            }
        };

        const loadMoreMissingEpisodeItems = () => {
            if (!missingEpisodeHasMoreVisibleItems.value) return;
            missingEpisodeRenderPage.value += 1;
            ensureMissingEpisodeLazyScrollable();
            nextTick(() => setupMissingEpisodeLoadMoreObserver());
        };

        const setupMissingEpisodeLoadMoreObserver = (attempt = 0) => {
            if (missingEpisodeLoadMoreObserver) {
                missingEpisodeLoadMoreObserver.disconnect();
                missingEpisodeLoadMoreObserver = null;
            }
            if (missingEpisodeObserverRetryTimer) {
                clearTimeout(missingEpisodeObserverRetryTimer);
                missingEpisodeObserverRetryTimer = null;
            }
            if (tab.value !== 'missing_episode_stats' || !missingEpisodeHasMoreVisibleItems.value) return;
            const sentinel = missingEpisodeLoadMoreSentinel.value || document.querySelector('.missing-episode-lazy-more');
            if (!sentinel) {
                if (attempt < 8) missingEpisodeObserverRetryTimer = setTimeout(() => setupMissingEpisodeLoadMoreObserver(attempt + 1), 80);
                return;
            }
            const observerRoot = isMobile.value ? null : (missingEpisodePosterGridRef.value || null);
            const loadNextFromSentinel = () => {
                if (!missingEpisodeHasMoreVisibleItems.value) return;
                if (missingEpisodeLoadMoreObserver) {
                    missingEpisodeLoadMoreObserver.disconnect();
                    missingEpisodeLoadMoreObserver = null;
                }
                loadMoreMissingEpisodeItems();
            };
            missingEpisodeLoadMoreObserver = new IntersectionObserver((entries) => {
                if (!entries[0]?.isIntersecting || !missingEpisodeHasMoreVisibleItems.value) return;
                loadNextFromSentinel();
            }, { root: observerRoot, rootMargin: isMobile.value ? '80px 0px' : '900px 0px', threshold: 0.01 });
            missingEpisodeLoadMoreObserver.observe(sentinel);
            requestAnimationFrame(() => {
                if (!missingEpisodeHasMoreVisibleItems.value) return;
                const rect = sentinel.getBoundingClientRect();
                const rootRect = observerRoot?.getBoundingClientRect?.();
                const rootBottom = rootRect ? rootRect.bottom : window.innerHeight;
                if (rect.top < rootBottom + (isMobile.value ? 80 : 900) && rect.bottom > -80) {
                    loadNextFromSentinel();
                }
            });
        };

        const onMissingEpisodeScrollFallback = () => {
            if (missingEpisodeScrollFallbackTicking) return;
            missingEpisodeScrollFallbackTicking = true;
            requestAnimationFrame(() => {
                missingEpisodeScrollFallbackTicking = false;
                if (tab.value !== 'missing_episode_stats' || !missingEpisodeHasMoreVisibleItems.value) return;
                const body = document.body;
                const doc = document.documentElement;
                const bodyRemain = body ? (body.scrollHeight - (body.scrollTop + window.innerHeight)) : Number.POSITIVE_INFINITY;
                const docRemain = doc ? (doc.scrollHeight - (doc.scrollTop + window.innerHeight)) : Number.POSITIVE_INFINITY;
                const remaining = Math.min(bodyRemain, docRemain);
                if (remaining > 220) return;
                const now = Date.now();
                if (now - missingEpisodeScrollFallbackLastAt < 320) return;
                missingEpisodeScrollFallbackLastAt = now;
                loadMoreMissingEpisodeItems();
            });
        };

        const ensureMissingEpisodeLazyScrollable = () => {
            if (missingEpisodeLazyEnsurePending) return;
            missingEpisodeLazyEnsurePending = true;
            nextTick(() => {
                requestAnimationFrame(() => {
                    missingEpisodeLazyEnsurePending = false;
                    if (tab.value !== 'missing_episode_stats') return;
                    if (!missingEpisodeHasMoreVisibleItems.value) return;
                    if (isMobile.value) return;
                    const el = document.querySelector('.missing-episode-poster-grid');
                    if (!el) return;
                    const hasVerticalScroll = el.scrollHeight > el.clientHeight + 4;
                    if (!hasVerticalScroll) loadMoreMissingEpisodeItems();
                });
            });
        };

        const resetMissingEpisodeRenderedItems = () => {
            missingEpisodeRenderPage.value = 0;
            teardownMissingEpisodeLoadMoreObserver();
            ensureMissingEpisodeLazyScrollable();
            nextTick(() => setupMissingEpisodeLoadMoreObserver());
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

        watch(() => visibleMissingEpisodeStatsProblemItems.value.length, () => {
            nextTick(() => setupMissingEpisodeLoadMoreObserver());
        });

        window.addEventListener('scroll', onMissingEpisodeScrollFallback, { passive: true });
        document.addEventListener('click', handleResourceSearchSourceOutsideClick);
        document.addEventListener('pointerdown', handleMissingEpisodeCompareOutsidePointerDown, true);
        document.addEventListener('keydown', handleMissingEpisodeCompareKeydown);

        onBeforeUnmount(() => {
            window.removeEventListener('scroll', onMissingEpisodeScrollFallback);
            document.removeEventListener('click', handleResourceSearchSourceOutsideClick);
            document.removeEventListener('pointerdown', handleMissingEpisodeCompareOutsidePointerDown, true);
            document.removeEventListener('keydown', handleMissingEpisodeCompareKeydown);
            teardownMissingEpisodeLoadMoreObserver();
            if (discoverRealtimeEventSource) {
                discoverRealtimeEventSource.close();
                discoverRealtimeEventSource = null;
            }
            if (missingEpisodeStatsPollTimer) {
                clearTimeout(missingEpisodeStatsPollTimer);
                missingEpisodeStatsPollTimer = null;
            }
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

        const getMainGridCacheEntryKey = (source = discoverActiveSource.value) => stableCacheStringify({
            source,
            filters: activeSourceFilters.value || {},
            library: getPrimaryServerFingerprint(),
        });

        const getMainGridCacheEntry = () => {
            const entry = getLocalCacheEntry(
                DISCOVER_MAIN_GRID_CACHE_KEY,
                DISCOVER_MAIN_GRID_CACHE_VERSION,
                getMainGridCacheEntryKey(),
            );
            if (!entry?.data || !Array.isArray(entry.data.items)) return null;
            return entry;
        };

        const applyMainGridCacheEntry = (entry) => {
            const data = entry?.data || {};
            mainGridItems.value = data.items || [];
            mainGridPage.value = Number(data.page || 1);
            mainGridTotalPages.value = Number(data.total_pages || 1);
            mainGridNoMore.value = !!data.no_more;
            nextTick(() => setupMainGridObserver());
        };

        const writeMainGridCache = (data = {}) => {
            const source = discoverActiveSource.value;
            if (!source || !Array.isArray(data.items)) return false;
            const entryKey = getMainGridCacheEntryKey(source);
            return setLocalCacheEntry(
                DISCOVER_MAIN_GRID_CACHE_KEY,
                DISCOVER_MAIN_GRID_CACHE_VERSION,
                entryKey,
                {
                    updatedAt: Date.now(),
                    source,
                    data: {
                        items: data.items,
                        page: Number(data.page || 1),
                        total_pages: Number(data.total_pages || 1),
                        no_more: !!data.no_more,
                    },
                },
                {
                    maxEntries: DISCOVER_MAIN_GRID_CACHE_MAX_ENTRIES,
                    maxChars: DISCOVER_MAIN_GRID_CACHE_MAX_CHARS,
                },
            );
        };

        const writeMainGridCacheFromCurrent = () => writeMainGridCache({
            items: mainGridItems.value || [],
            page: mainGridPage.value || 1,
            total_pages: mainGridTotalPages.value || 1,
            no_more: mainGridNoMore.value,
        });

        const mergeMainGridCachedTail = (freshItems = [], cachedItems = []) => {
            const tail = (cachedItems || []).slice(freshItems.length);
            const getKey = item => [
                item?.source || '',
                item?.id || item?._tmdb_id || item?.tmdb_id || '',
                item?.media_type || '',
                item?.title || item?.name || '',
                item?.year || '',
            ].join('|');
            const seen = new Set((freshItems || []).map(getKey));
            return [
                ...(freshItems || []),
                ...tail.filter(item => {
                    const key = getKey(item);
                    if (seen.has(key)) return false;
                    seen.add(key);
                    return true;
                }),
            ];
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

        const shouldResolveLibraryStatus = () => !!String(activeSourceFilters.value?.[LIBRARY_STATUS_FILTER_KEY] ?? '');

        const markLibraryExists = async (items = [], options = {}) => {
            const candidates = (items || []).filter(item => getItemExistenceKey(item));
            if (!candidates.length) return;
            try {
                const resolveMissing = !!options.resolveMissing;
                const payload = candidates.map(item => ({
                    tmdb_id: getItemTmdbId(item),
                    title: item.title || item.name || '',
                    year: item.year || '',
                    media_type: item.media_type || 'movie',
                    source: item.source || '',
                    id: item.id || '',
                    _existence_key: getItemExistenceKey(item),
                }));
                const res = await axios.post(
                    '/api/discover/library/exists',
                    payload,
                    resolveMissing ? { params: { resolve_missing: 1 } } : undefined,
                );
                const results = res.data?.results || {};
                const tmdbIds = res.data?.tmdb_ids || {};
                candidates.forEach(item => {
                    const key = getItemExistenceKey(item);
                    item.exists_in_library = !!results[key];
                    if (tmdbIds[key]) {
                        item._tmdb_id = tmdbIds[key];
                        if (!item.tmdb_id) item.tmdb_id = tmdbIds[key];
                    }
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
            missingEpisodeStats.loadingAction = '';
            missingEpisodeStats.manualCompleteUpdating = {};
            missingEpisodeStats.activeLibraryKey = '';
            missingEpisodeStats.filter = 'all';
            missingEpisodeStats.sortBy = 'year_desc';
            missingEpisodeStats.searchQuery = '';
            missingEpisodeStats.meta = {};
            missingEpisodeStats.summary = emptyMissingEpisodeSummary();
            missingEpisodeStats.progress = { current: 0, total: 0 };
        };

        const getMissingEpisodeStatsCacheEntryKey = (data = null) => stableCacheStringify({
            library: getPrimaryServerFingerprint(),
            serverIdx: data?.meta?.server_idx ?? missingEpisodeStats.meta?.server_idx ?? 0,
        });

        const getMissingEpisodeStatsCacheEntry = () => {
            const entry = getLocalCacheEntry(
                MISSING_EPISODE_STATS_CACHE_KEY,
                MISSING_EPISODE_STATS_CACHE_VERSION,
                getMissingEpisodeStatsCacheEntryKey(),
            );
            if (!entry?.data || !Array.isArray(entry.data.items) || !Array.isArray(entry.data.libraries)) return null;
            return entry;
        };

        const shouldCacheMissingEpisodeStatsPayload = (data = {}, summaryOnly = false) => {
            const meta = data.meta || {};
            return !summaryOnly
                && data.ready !== false
                && !data.running
                && !!meta.missing_stats_cache_key
                && Array.isArray(data.items)
                && Array.isArray(data.libraries);
        };

        const compactMissingEpisodeItemForCache = (item = {}) => {
            const keys = [
                'tmdbId',
                'libraryId',
                'libraryName',
                'title',
                'year',
                'releaseDate',
                'poster_url',
                'status',
                'label',
                'missingCategory',
                'categoryLabel',
                'tmdbStatus',
                'presentEpisodes',
                'totalEpisodes',
                'missingEpisodes',
                'airedMissingEpisodes',
                'extraLocalEpisodes',
                'seasonBrief',
                'manualComplete',
                'manualCompleteAt',
                'manualCompleteLocalEpisodes',
            ];
            return keys.reduce((acc, key) => {
                if (item[key] !== undefined) acc[key] = item[key];
                return acc;
            }, { _cachedDisplayOnly: true });
        };

        const stripMissingEpisodeLibraryItems = (libraries = []) => {
            return (libraries || []).map(({ items, ...lib }) => lib);
        };

        const buildMissingEpisodeStatsCacheData = (data = {}) => {
            const allItems = Array.isArray(data.items) ? data.items : [];
            const shouldStoreFull = allItems.length <= MISSING_EPISODE_FULL_CACHE_ITEM_LIMIT;
            const cachedItems = shouldStoreFull
                ? allItems
                : allItems
                    .filter(item => isMissingEpisodeActionableMissing(item))
                    .map(compactMissingEpisodeItemForCache);
            return {
                ready: data.ready !== false,
                running: false,
                message: data.message || '',
                meta: data.meta || {},
                summary: data.summary || emptyMissingEpisodeSummary(),
                items: cachedItems,
                libraries: shouldStoreFull ? (data.libraries || []) : stripMissingEpisodeLibraryItems(data.libraries || []),
                progress: data.progress || { current: data.summary?.tvCount || 0, total: data.summary?.tvCount || 0 },
                cachedDisplayMode: shouldStoreFull ? 'full' : 'actionable',
            };
        };

        const writeMissingEpisodeStatsCache = (data = {}, summaryOnly = false) => {
            if (!shouldCacheMissingEpisodeStatsPayload(data, summaryOnly)) return false;
            const entryKey = getMissingEpisodeStatsCacheEntryKey(data);
            return setLocalCacheEntry(
                MISSING_EPISODE_STATS_CACHE_KEY,
                MISSING_EPISODE_STATS_CACHE_VERSION,
                entryKey,
                {
                    updatedAt: Date.now(),
                    data: buildMissingEpisodeStatsCacheData(data),
                },
                {
                    maxEntries: MISSING_EPISODE_STATS_CACHE_MAX_ENTRIES,
                    maxChars: MISSING_EPISODE_STATS_CACHE_MAX_CHARS,
                },
            );
        };

        const writeMissingEpisodeStatsCacheFromCurrent = () => writeMissingEpisodeStatsCache({
            ready: missingEpisodeStats.ready,
            running: false,
            message: missingEpisodeStats.message,
            meta: missingEpisodeStats.meta,
            summary: missingEpisodeStats.summary,
            items: missingEpisodeStats.items,
            libraries: missingEpisodeStats.libraries,
            progress: missingEpisodeStats.progress,
        });

        const applyMissingEpisodeStatsData = (data = {}, options = {}) => {
            const summaryOnly = options.summaryOnly || data.summaryOnly;
            const previousLibraryKey = missingEpisodeStats.activeLibraryKey;
            const summary = { ...emptyMissingEpisodeSummary(), ...(data.summary || {}) };
            missingEpisodeStats.ready = data.ready !== false;
            missingEpisodeStats.message = data.message || '';
            missingEpisodeStats.meta = data.meta || {};
            if (!summaryOnly) {
                missingEpisodeStats.items = data.items || [];
            }
            missingEpisodeStats.libraries = summaryOnly
                ? (data.libraries || []).map(({ items, ...lib }) => lib)
                : (data.libraries || []);
            const stillExists = previousLibraryKey === MISSING_EPISODE_ALL_LIBRARY_KEY
                || missingEpisodeStats.libraries.some(lib => getMissingEpisodeLibraryKey(lib) === previousLibraryKey);
            if (previousLibraryKey && stillExists) {
                missingEpisodeStats.activeLibraryKey = previousLibraryKey;
            } else {
                missingEpisodeStats.activeLibraryKey = missingEpisodeStats.libraries.length ? MISSING_EPISODE_ALL_LIBRARY_KEY : '';
            }
            missingEpisodeStats.summary = summary;
            missingEpisodeStats.progress = data.progress || { current: summary.tvCount || 0, total: summary.tvCount || 0 };
            missingEpisodeStats.loaded = true;
            missingEpisodeStats.loading = summaryOnly ? true : !!data.running;
            if (!summaryOnly && !data.running) {
                missingEpisodeStats.loadingAction = '';
            }
            writeMissingEpisodeStatsCache(data, summaryOnly);
        };

        const buildMissingEpisodeSummaryFromItems = (items = [], tvCountOverride = null) => {
            const summary = emptyMissingEpisodeSummary();
            summary.tvCount = tvCountOverride == null ? items.length : Number(tvCountOverride) || 0;
            const num = (value) => Number(value) || 0;
            (items || []).forEach(item => {
                const status = String(item?.status || '').toLowerCase();
                const manualComplete = !!item?.manualComplete;
                const category = String(item?.missingCategory || '').trim().toLowerCase();
                if (status === 'exists' && !['airing_recent_missing', 'airing_aired_missing'].includes(category)) summary.completeCount += 1;
                else if (status === 'partial') summary.partialCount += 1;
                else if (status === 'missing' || status === 'error') summary.errorCount += 1;
                if (manualComplete) summary.manualCompleteCount += 1;

                const missingEpisodes = num(item?.missingEpisodes);
                if (missingEpisodes > 0) summary.missingCount += 1;

                if (category === 'airing_aired_missing') summary.airingAiredMissingCount += 1;
                if (category === 'airing_recent_missing') summary.airingRecentMissingCount += 1;
                else if (category === 'ended_missing') summary.endedMissingCount += 1;
                else if (category === 'partial_missing') summary.otherMissingCount += 1;

                summary.actionableMissingEpisodes += missingEpisodes;
                if (manualComplete) {
                    const localCount = num(item?.manualCompleteLocalEpisodes) || countLocalEpisodes(item?.localItem?.seasons) || num(item?.presentEpisodes) || num(item?.totalEpisodes);
                    summary.presentEpisodes += localCount;
                    summary.totalEpisodes += localCount;
                } else {
                    summary.presentEpisodes += num(item?.presentEpisodes);
                    summary.totalEpisodes += num(item?.totalEpisodes);
                    summary.extraLocalEpisodes += num(item?.extraLocalEpisodes);
                }
                summary.missingEpisodes += missingEpisodes;
            });
            return summary;
        };

        const getMissingEpisodeRowIdentity = (row = {}) => [
            String(row.tmdbId || row.item?.tmdb_id || row.item?.id || ''),
            String(row.libraryId || ''),
        ].join('|');

        const replaceMissingEpisodeRowInList = (list = [], nextRow = {}) => {
            const targetKey = getMissingEpisodeRowIdentity(nextRow);
            const index = list.findIndex(item => getMissingEpisodeRowIdentity(item) === targetKey);
            if (index >= 0) list.splice(index, 1, nextRow);
            return index >= 0;
        };

        const rebuildMissingEpisodeSummaries = () => {
            missingEpisodeStats.summary = buildMissingEpisodeSummaryFromItems(
                missingEpisodeStats.items || [],
                missingEpisodeStats.summary?.tvCount,
            );
            (missingEpisodeStats.libraries || []).forEach(lib => {
                if (!Array.isArray(lib.items)) return;
                lib.summary = buildMissingEpisodeSummaryFromItems(lib.items, lib.summary?.tvCount);
            });
        };

        const buildManualCompleteRow = (row = {}, enabled = true) => {
            const nextRow = { ...row };
            if (enabled) {
                const original = row.manualCompleteOriginal || {
                    status: row.status,
                    label: row.label,
                    missingCategory: row.missingCategory,
                    categoryLabel: row.categoryLabel,
                    presentEpisodes: row.presentEpisodes,
                    totalEpisodes: row.totalEpisodes,
                    missingEpisodes: row.missingEpisodes,
                    rawTotalEpisodes: row.rawTotalEpisodes,
                    rawMissingEpisodes: row.rawMissingEpisodes,
                    airedMissingEpisodes: row.airedMissingEpisodes,
                    extraLocalEpisodes: row.extraLocalEpisodes,
                    extraLocalSeasons: row.extraLocalSeasons,
                    seasonBrief: row.seasonBrief,
                    releaseDate: row.releaseDate,
                };
                const localCount = countLocalEpisodes(row.localItem?.seasons);
                nextRow.manualComplete = true;
                nextRow.manualCompleteAt = Math.floor(Date.now() / 1000);
                nextRow.manualCompleteLocalEpisodes = localCount;
                nextRow.manualCompleteOriginal = original;
                nextRow.status = 'exists';
                nextRow.label = localCount ? `已标记完整 ${localCount} 集` : '已标记完整';
                nextRow.missingCategory = 'manual_complete';
                nextRow.categoryLabel = '已标记完整';
                nextRow.missingEpisodes = 0;
                nextRow.airedMissingEpisodes = 0;
                return nextRow;
            }
            const original = row.manualCompleteOriginal || {};
            Object.assign(nextRow, original);
            delete nextRow.manualComplete;
            delete nextRow.manualCompleteAt;
            delete nextRow.manualCompleteLocalEpisodes;
            delete nextRow.manualCompleteOriginal;
            return nextRow;
        };

        const replaceMissingEpisodeRowEverywhere = (nextRow = {}) => {
            replaceMissingEpisodeRowInList(missingEpisodeStats.items || [], nextRow);
            (missingEpisodeStats.libraries || []).forEach(lib => {
                if (Array.isArray(lib.items)) replaceMissingEpisodeRowInList(lib.items, nextRow);
            });
            if (missingEpisodeCompareModal.row && getMissingEpisodeRowIdentity(missingEpisodeCompareModal.row) === getMissingEpisodeRowIdentity(nextRow)) {
                missingEpisodeCompareModal.row = nextRow;
            }
            rebuildMissingEpisodeSummaries();
        };

        const applyMissingEpisodeManualCompleteEvent = (data = {}) => {
            const action = data.action || '';
            if (!['manual_complete', 'manual_complete_removed'].includes(action)) return false;
            const targetKey = [
                String(data.tmdb_id || data.tmdbId || ''),
                String(data.library_id || data.libraryId || ''),
            ].join('|');
            if (!targetKey || targetKey === '|') return true;
            const row = (missingEpisodeStats.items || []).find(item => getMissingEpisodeRowIdentity(item) === targetKey);
            if (!row) return true;
            replaceMissingEpisodeRowEverywhere(buildManualCompleteRow(row, !!data.manual_complete));
            writeMissingEpisodeStatsCacheFromCurrent();
            return true;
        };

        const toggleMissingEpisodeManualComplete = async (row = missingEpisodeCompareModal.row) => {
            if (!row) return;
            const enabled = !isMissingEpisodeManualComplete(row);
            const updateKey = getMissingEpisodeManualUpdateKey(row);
            if (missingEpisodeStats.manualCompleteUpdating[updateKey]) return;
            missingEpisodeStats.manualCompleteUpdating[updateKey] = true;
            const optimisticRow = buildManualCompleteRow(row, enabled);
            replaceMissingEpisodeRowEverywhere(optimisticRow);
            try {
                const res = await axios.post('/api/discover/library/missing-episode-stats/manual-complete', {
                    tmdb_id: row.tmdbId || row.item?.tmdb_id || row.item?.id || '',
                    library_id: row.libraryId || '',
                    title: row.title || row.item?.title || row.localItem?.title || '',
                    year: row.year || row.localItem?.year || '',
                    server_idx: missingEpisodeStats.meta?.server_idx ?? 0,
                    enabled,
                });
                if (res.data?.payload) applyMissingEpisodeStatsData(res.data.payload);
                showToast(enabled ? '已标记为完整' : '已取消完整标记', 'success');
            } catch (e) {
                showToast('标记失败: ' + (e.response?.data?.detail || e.message), 'error');
                refreshMissingEpisodeStatsFromCache();
            } finally {
                delete missingEpisodeStats.manualCompleteUpdating[updateKey];
            }
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
                    missingEpisodeStats.loadingAction = '';
                }
            } catch (e) {
                if (runId === missingEpisodeStatsRunId) {
                    missingEpisodeStats.loading = false;
                    missingEpisodeStats.loadingAction = '';
                    missingEpisodeStats.error = e.response?.data?.detail || e.message || '统计失败';
                }
            }
        };

        const loadMissingEpisodeStatsFull = async (runId) => {
            try {
                const res = await axios.get('/api/discover/library/missing-episode-stats');
                if (runId !== missingEpisodeStatsRunId) return;
                applyMissingEpisodeStatsData(res.data || {});
                if (res.data?.running) {
                    missingEpisodeStatsPollTimer = setTimeout(() => pollMissingEpisodeStats(runId), 1200);
                }
            } catch (e) {
                if (runId === missingEpisodeStatsRunId) {
                    missingEpisodeStats.loading = false;
                    missingEpisodeStats.loadingAction = '';
                    missingEpisodeStats.error = e.response?.data?.detail || e.message || '获取媒体库失败';
                }
            }
        };

        const loadMissingEpisodeStatsShell = async () => {
            if (missingEpisodeStats.loading) return;
            if (missingEpisodeStats.loaded) {
                const runId = ++missingEpisodeStatsRunId;
                missingEpisodeStats.loadingAction = 'load';
                loadMissingEpisodeStatsFull(runId);
                return;
            }
            const cached = getMissingEpisodeStatsCacheEntry();
            const renderedCached = !!cached;
            if (cached) {
                applyMissingEpisodeStatsData(cached.data || {});
                const runId = ++missingEpisodeStatsRunId;
                missingEpisodeStats.loadingAction = 'load';
                loadMissingEpisodeStatsFull(runId);
                return;
            }
            const runId = ++missingEpisodeStatsRunId;
            missingEpisodeStats.loading = true;
            missingEpisodeStats.loadingAction = 'load';
            try {
                const res = await axios.get('/api/discover/library/missing-episode-stats', {
                    params: { summary_only: 1 },
                });
                if (runId !== missingEpisodeStatsRunId) return;
                applyMissingEpisodeStatsData(res.data || {}, { summaryOnly: true });
                loadMissingEpisodeStatsFull(runId);
            } catch (e) {
                if (runId === missingEpisodeStatsRunId) {
                    missingEpisodeStats.loading = false;
                    missingEpisodeStats.loadingAction = '';
                    if (!renderedCached) {
                        missingEpisodeStats.error = e.response?.data?.detail || e.message || '获取媒体库失败';
                    }
                }
            }
        };

        const runMissingEpisodeStats = async (refreshIndex = false, forceRun = refreshIndex, action = refreshIndex ? 'calibrate' : 'refresh') => {
            if (missingEpisodeStats.loading) return;
            if (missingEpisodeStats.loaded && !forceRun) return;
            resetMissingEpisodeStats();
            const runId = missingEpisodeStatsRunId;
            missingEpisodeStats.loading = true;
            missingEpisodeStats.loadingAction = action;
            try {
                const res = await axios.get('/api/discover/library/missing-episode-stats', {
                    params: { start: 1, refresh: refreshIndex ? 1 : 0 },
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
                    missingEpisodeStats.loadingAction = '';
                }
            }
        };

        const refreshMissingEpisodeStats = () => {
            runMissingEpisodeStats(false, true, 'refresh');
        };

        const calibrateMissingEpisodeStats = () => {
            runMissingEpisodeStats(true, true, 'calibrate');
        };

        const setMissingEpisodeLibrary = (libraryKey) => {
            missingEpisodeStats.activeLibraryKey = libraryKey;
            resetMissingEpisodeRenderedItems();
        };

        const setMissingEpisodeFilter = (filter) => {
            missingEpisodeStats.filter = filter || 'all';
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
            await markLibraryExists(displayable, { resolveMissing: shouldResolveLibraryStatus() });
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
            if (!discoverActiveSource.value || page < 1 || mainGridNoMore.value) return null;
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
            if (!discoverActiveSource.value || mainGridNoMore.value) return;
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
            writeMainGridCacheFromCurrent();
            prefetchMainGridAhead(page, _mainGridGen);
            nextTick(() => setupMainGridObserver());
            return true;
        };


        const loadMainGrid = async (reset = true) => {
            const source = discoverActiveSource.value;
            let cachedGridSnapshot = null;
            if (reset) {
                resetMainGridPrefetch();
                mainGridPage.value = 1;
                mainGridNoMore.value = false;
                const cached = source ? getMainGridCacheEntry() : null;
                if (cached) {
                    const cachedData = cached.data || {};
                    cachedGridSnapshot = {
                        items: Array.isArray(cachedData.items) ? [...cachedData.items] : [],
                        page: Number(cachedData.page || 1),
                        totalPages: Number(cachedData.total_pages || 1),
                        noMore: !!cachedData.no_more,
                    };
                    applyMainGridCacheEntry(cached);
                } else {
                    mainGridItems.value = [];
                    mainGridTotalPages.value = 1;
                }
            }
            const gen = ++_mainGridGen;
            mainGridLoading.value = true;
            try {
                if (!source) {
                    mainGridItems.value = [];
                    mainGridTotalPages.value = 1;
                    mainGridNoMore.value = true;
                    return;
                }

                const page = reset ? 1 : mainGridPage.value;
                const data = await fetchMainGridPage(source, page);
                if (gen !== _mainGridGen) return;
                const rawItems = data.items || [];
                const items = await prepareDisplayableMainGridItems(rawItems);
                const freshTotalPages = data.total_pages || 1;
                const freshNoMore = !mainGridPageHasMore(data, page, rawItems);
                if (reset && cachedGridSnapshot?.items?.length && rawItems.length) {
                    mainGridItems.value = mergeMainGridCachedTail(items, cachedGridSnapshot.items);
                    mainGridPage.value = Math.max(page, cachedGridSnapshot.page || 1);
                    mainGridTotalPages.value = Math.max(freshTotalPages, cachedGridSnapshot.totalPages || 1);
                    mainGridNoMore.value = (
                        (cachedGridSnapshot.noMore && freshTotalPages <= (cachedGridSnapshot.totalPages || 1))
                        || mainGridPage.value >= mainGridTotalPages.value
                    );
                } else {
                    mainGridTotalPages.value = freshTotalPages;
                    mainGridPage.value = page;
                    mainGridNoMore.value = freshNoMore;
                    if (reset) {
                        mainGridItems.value = items;
                    } else {
                        mainGridItems.value.push(...items);
                    }
                }
                writeMainGridCacheFromCurrent();
                if (!mainGridNoMore.value) prefetchMainGridAhead(mainGridPage.value, gen);
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
            if (!count) return { status: 'missing', label: total ? `未入库 ${count}/${total}` : '未入库', count, total };
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
            resourceSearchModal.visible = false;
            resourceSearchModal.loading = false;
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

        const openMissingEpisodeCompareDetail = () => {
            const item = missingEpisodeCompareModal.row?.item;
            if (!item) return;
            openMediaDetail(item);
        };

        const normalizeMpSubscribeSeasons = (seasons = []) => {
            const map = new Map();
            (Array.isArray(seasons) ? seasons : []).forEach(season => {
                const number = Number(season?.season_number ?? season?.seasonNumber ?? season?.season);
                if (!Number.isFinite(number) || number <= 0) return;
                if (!map.has(number)) {
                    map.set(number, {
                        season: number,
                        episodeCount: Number(season?.episode_count ?? season?.total ?? season?.episodes?.length ?? 0) || 0,
                        name: String(season?.name || '').trim(),
                    });
                }
            });
            return [...map.values()].sort((a, b) => a.season - b.season);
        };

        const subscribeMoviePilotMedia = async ({ tmdbId, mediaType = 'movie', title = '', year = '', season = null } = {}) => {
            if (!mpConfig.mp_url) {
                showToast('请先配置 MoviePilot 连接信息', 'warning');
                return false;
            }
            const normalizedTmdbId = Number(tmdbId);
            if (!Number.isFinite(normalizedTmdbId) || normalizedTmdbId <= 0) {
                showToast('未获取到 TMDB ID，无法订阅', 'warning');
                return false;
            }
            try {
                const body = {
                    tmdbid: normalizedTmdbId,
                    type_name: mediaType,
                    name: title,
                    year,
                };
                if (mediaType === 'tv' && season != null && season !== 'all') {
                    body.season = Number(season);
                }
                await axios.post('/api/moviepilot/subscribe', body);
                const currentTmdbId = String(detailModal.detail?.tmdb_id || detailModal.item?._tmdb_id || detailModal.item?.tmdb_id || detailModal.item?.id || '');
                if (currentTmdbId && currentTmdbId === String(normalizedTmdbId)) {
                    if (mediaType === 'tv' && body.season) {
                        detailModal.seasonSubscribed = true;
                    } else {
                        detailModal.subscribed = true;
                    }
                    if (detailModal.item) detailModal.item.subscribed = true;
                }
                showToast(mediaType === 'tv' && body.season ? `MP已订阅第 ${body.season} 季` : 'MP订阅成功', 'success');
                return true;
            } catch (e) {
                showToast('MP订阅失败: ' + (e.response?.data?.detail || e.message), 'error');
                return false;
            }
        };

        const closeMpSubscribeModal = () => {
            mpSubscribeModal.visible = false;
            mpSubscribeModal.loading = false;
        };

        const openMpSubscribeModalForMedia = async ({
            tmdbId,
            mediaType = 'movie',
            title = '',
            year = '',
            seasons = [],
            defaultSeason = null,
        } = {}) => {
            const normalizedMediaType = String(mediaType || '').toLowerCase() === 'movie' ? 'movie' : 'tv';
            if (normalizedMediaType === 'movie') {
                await subscribeMoviePilotMedia({ tmdbId, mediaType: 'movie', title, year });
                return;
            }
            const seasonOptions = normalizeMpSubscribeSeasons(seasons);
            mpSubscribeModal.visible = true;
            mpSubscribeModal.loading = false;
            mpSubscribeModal.title = title || '';
            mpSubscribeModal.year = year || '';
            mpSubscribeModal.tmdbId = String(tmdbId || '');
            mpSubscribeModal.mediaType = 'tv';
            mpSubscribeModal.seasons = seasonOptions;
            const defaultNumber = Number(defaultSeason);
            if (Number.isFinite(defaultNumber) && defaultNumber > 0) {
                mpSubscribeModal.selectedMode = 'seasons';
                mpSubscribeModal.selectedSeasons = [defaultNumber];
            } else {
                mpSubscribeModal.selectedMode = 'all';
                mpSubscribeModal.selectedSeasons = [];
            }
        };

        const toggleMpSubscribeSeason = (season) => {
            const seasonNumber = Number(season);
            if (!Number.isFinite(seasonNumber) || seasonNumber <= 0) return;
            mpSubscribeModal.selectedMode = 'seasons';
            const current = new Set((mpSubscribeModal.selectedSeasons || []).map(value => Number(value)));
            if (current.has(seasonNumber)) {
                current.delete(seasonNumber);
            } else {
                current.add(seasonNumber);
            }
            mpSubscribeModal.selectedSeasons = [...current].sort((a, b) => a - b);
        };

        const confirmMpSubscribe = async () => {
            if (mpSubscribeModal.loading) return;
            if (mpSubscribeModal.selectedMode === 'seasons' && !mpSubscribeModal.selectedSeasons.length) {
                showToast('请选择要订阅的季，或切换为全剧订阅', 'warning');
                return;
            }
            mpSubscribeModal.loading = true;
            let ok = false;
            if (mpSubscribeModal.selectedMode === 'all') {
                ok = await subscribeMoviePilotMedia({
                    tmdbId: mpSubscribeModal.tmdbId,
                    mediaType: mpSubscribeModal.mediaType,
                    title: mpSubscribeModal.title,
                    year: mpSubscribeModal.year,
                    season: null,
                });
            } else {
                ok = true;
                for (const season of mpSubscribeModal.selectedSeasons) {
                    const seasonOk = await subscribeMoviePilotMedia({
                        tmdbId: mpSubscribeModal.tmdbId,
                        mediaType: mpSubscribeModal.mediaType,
                        title: mpSubscribeModal.title,
                        year: mpSubscribeModal.year,
                        season,
                    });
                    ok = ok && seasonOk;
                    if (!seasonOk) break;
                }
            }
            mpSubscribeModal.loading = false;
            if (ok) closeMpSubscribeModal();
        };

        const openDetailMpSubscribe = async () => {
            const detail = detailModal.detail || {};
            const item = detailModal.item || {};
            await openMpSubscribeModalForMedia({
                tmdbId: detail.tmdb_id || item._tmdb_id || item.tmdb_id || item.id || '',
                mediaType: detail.media_type || item.media_type || 'movie',
                title: detail.title || item.title || '',
                year: detail.year || item.year || '',
                seasons: detail.seasons || [],
                defaultSeason: detailModal.selectedSeason,
            });
        };

        const openMissingEpisodeMpSubscribe = async () => {
            const row = missingEpisodeCompareModal.row || {};
            const item = row.item || {};
            const localSeasons = getLocalSeasonRows(row).map(season => ({
                season_number: season.season,
                episode_count: season.episodes.length,
            }));
            await openMpSubscribeModalForMedia({
                tmdbId: row.tmdbId || item.tmdb_id || item._tmdb_id || item.id || '',
                mediaType: 'tv',
                title: row.title || item.title || row.localItem?.title || '',
                year: row.year || item.year || row.localItem?.year || '',
                seasons: (row.seasons && row.seasons.length) ? row.seasons : localSeasons,
            });
        };

        const getMissingEpisodeEmbyContext = (row = missingEpisodeCompareModal.row) => {
            const embyId = String(row?.localItem?.embyId || '').trim();
            const dashboardServer = Array.isArray(servers?.value) ? (servers.value[0] || {}) : {};
            const embyConfig = Array.isArray(config302?.embys) ? (config302.embys[0] || {}) : {};
            const baseUrl = String(dashboardServer.public_host || dashboardServer.url || embyConfig.public_host || embyConfig.url || '').trim().replace(/\/+$/, '');
            const serverId = String(dashboardServer.server_id || '').trim();
            return { embyId, baseUrl, serverId };
        };

        const canOpenMissingEpisodeEmby = (row = missingEpisodeCompareModal.row) => {
            const { embyId, baseUrl } = getMissingEpisodeEmbyContext(row);
            return !!embyId && !!baseUrl;
        };

        const getMissingEpisodeEmbyUrl = (row = missingEpisodeCompareModal.row) => {
            const { embyId, baseUrl, serverId } = getMissingEpisodeEmbyContext(row);
            if (!embyId || !baseUrl || !serverId) return '';
            return `${baseUrl}/web/index.html#!/item?id=${encodeURIComponent(embyId)}&serverId=${encodeURIComponent(serverId)}`;
        };

        const openMissingEpisodeEmby = async () => {
            if (!canOpenMissingEpisodeEmby()) {
                showToast?.('缺少 Emby 外网地址或 Emby ID', 'warning');
                return;
            }
            if (typeof ensureDashboardServerId === 'function') {
                await ensureDashboardServerId();
            }
            const url = getMissingEpisodeEmbyUrl();
            if (!url) {
                showToast?.('未获取到 Emby serverId，请重启服务后重试', 'error');
                return;
            }
            window.open(url, '_blank', 'noopener,noreferrer');
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

        const closeResourceSearchModal = () => {
            resourceSearchModal.visible = false;
            resourceSearchModal.loading = false;
        };

        const buildForwardResourcePayload = (item = {}) => {
            const source = String(item.sourceKey || item.source || 'hdhive').trim().toLowerCase();
            const payload = {
                source,
                slug: item.slug || '',
                resource_id: item.resourceId || item.resource_id || '',
                type: resourceSearchModal.mediaType || item.mediaType || 'movie',
                tmdb_id: resourceSearchModal.tmdbId || item.tmdbId || item.tmdb_id || '',
            };
            if (resourceSearchModal.season != null) payload.season = resourceSearchModal.season;
            if (resourceSearchModal.episode != null) payload.episode = resourceSearchModal.episode;
            return payload;
        };

        const openForwardResource = async (item = {}) => {
            const transferId = String(item.id || item.url || item.title || Date.now());
            const payload = buildForwardResourcePayload(item);
            if (payload.source === 'aiying' && !payload.resource_id) {
                showToast?.('该爱影资源缺少转存 ID，请重新搜索', 'warning');
                return;
            }
            if (payload.source !== 'aiying' && !payload.slug) {
                showToast?.('该影巢资源缺少转存标识，请重新搜索', 'warning');
                return;
            }
            resourceSearchModal.transferringId = transferId;
            try {
                const res = await axios.post('/api/forward/transfer_resource', payload);
                item.transferStatus = res.data?.status || '转存成功';
                showToast?.(res.data?.message || '已转存到整理目录', 'success');
            } catch (e) {
                const message = e.response?.data?.detail || e.message || '转存失败';
                showToast?.('转存失败: ' + message, 'error');
            } finally {
                if (resourceSearchModal.transferringId === transferId) {
                    resourceSearchModal.transferringId = '';
                }
            }
        };

        const previewForwardResource = async (item = {}) => {
            if (Array.isArray(item.previewItems) && item.previewItems.length && !item.previewError) {
                item.previewOpen = !item.previewOpen;
                return;
            }
            const previewId = String(item.id || item.url || item.title || Date.now());
            const payload = buildForwardResourcePayload(item);
            if (payload.source === 'aiying' && !payload.resource_id) {
                showToast?.('该爱影资源缺少预览 ID，请重新搜索', 'warning');
                return;
            }
            if (payload.source !== 'aiying' && !payload.slug) {
                showToast?.('该影巢资源缺少预览标识，请重新搜索', 'warning');
                return;
            }
            resourceSearchModal.previewingId = previewId;
            item.previewOpen = true;
            item.previewLoading = true;
            item.previewError = '';
            try {
                const res = await axios.post('/api/forward/preview_resource', payload);
                item.previewItems = res.data?.items || [];
                item.previewCount = res.data?.count || item.previewItems.length;
                item.previewMatchedCount = res.data?.matchedCount || 0;
                item.previewTotalSizeLabel = res.data?.totalSizeLabel || '';
                if (!item.previewItems.length) {
                    item.previewError = '分享内未找到可预览内容';
                }
            } catch (e) {
                item.previewItems = [];
                item.previewError = e.response?.data?.detail || e.message || '预览失败';
                showToast?.('预览失败: ' + item.previewError, 'error');
            } finally {
                item.previewLoading = false;
                if (resourceSearchModal.previewingId === previewId) {
                    resourceSearchModal.previewingId = '';
                }
            }
        };

        const openResourceSearchForMedia = async ({
            tmdbId,
            mediaType = 'movie',
            title = '',
            season = null,
            episode = null,
            context = 'modal',
            inline = false,
        } = {}) => {
            await loadResourceSearchSources(true);
            if (!resourceSearchSources.value.length) {
                showToast?.('请先在 Forward 模块配置影巢或爱影资源源', 'warning');
                return;
            }
            if (!selectedResourceSearchSources.value.length) {
                reconcileResourceSearchSelection();
            }
            const normalizedTmdbId = String(tmdbId || '').trim();
            if (!normalizedTmdbId) {
                showToast?.('未获取到 TMDB ID，无法搜索资源', 'warning');
                return;
            }
            const normalizedMediaType = String(mediaType || '').toLowerCase() === 'movie' ? 'movie' : 'tv';
            const sources = [...selectedResourceSearchSources.value];
            resourceSearchModal.visible = !inline;
            resourceSearchModal.context = context;
            resourceSearchModal.loading = true;
            resourceSearchModal.items = [];
            resourceSearchModal.error = '';
            resourceSearchModal.title = title || '';
            resourceSearchModal.mediaType = normalizedMediaType;
            resourceSearchModal.tmdbId = normalizedTmdbId;
            resourceSearchModal.sources = sources;
            resourceSearchModal.season = normalizedMediaType === 'tv' && season != null ? season : null;
            resourceSearchModal.episode = normalizedMediaType === 'tv' && episode != null ? episode : null;
            resourceSearchModal.transferringId = '';
            try {
                const payload = {
                    tmdb_id: normalizedTmdbId,
                    type: normalizedMediaType,
                    sources,
                };
                if (resourceSearchModal.season != null) payload.season = resourceSearchModal.season;
                if (resourceSearchModal.episode != null) payload.episode = resourceSearchModal.episode;
                const res = await axios.post('/api/forward/search_resources', payload);
                resourceSearchModal.items = res.data || [];
                if (!resourceSearchModal.items.length) {
                    resourceSearchModal.error = '未搜索到可用资源';
                }
            } catch (e) {
                resourceSearchModal.error = e.response?.data?.detail || e.message || '资源搜索失败';
                showToast?.('资源搜索失败: ' + resourceSearchModal.error, 'error');
            } finally {
                resourceSearchModal.loading = false;
            }
        };

        const openDetailResourceSearch = async () => {
            const detail = detailModal.detail || {};
            const item = detailModal.item || {};
            await openResourceSearchForMedia({
                tmdbId: detail.tmdb_id || item._tmdb_id || item.id || '',
                mediaType: detail.media_type || item.media_type || 'movie',
                title: detail.title || item.title || '',
                season: detailModal.selectedSeason,
                context: 'detail',
            });
        };

        const openMissingEpisodeResourceSearch = async () => {
            const row = missingEpisodeCompareModal.row || {};
            const item = row.item || {};
            await openResourceSearchForMedia({
                tmdbId: row.tmdbId || item.tmdb_id || item._tmdb_id || item.id || '',
                mediaType: 'tv',
                title: row.title || item.title || row.localItem?.title || '',
                context: 'missing_episode',
                inline: true,
            });
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
                await markLibraryExists(items, { resolveMissing: shouldResolveLibraryStatus() });
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
                await markLibraryExists(newItems, { resolveMissing: shouldResolveLibraryStatus() });
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

        const mediaTypesMatch = (left, right) => {
            const normalize = (value) => String(value || 'movie') === 'series' ? 'tv' : String(value || 'movie');
            return normalize(left) === normalize(right);
        };

        const applyLibraryEventToItems = (items = [], payload = {}) => {
            const tmdbId = String(payload.tmdb_id || '');
            if (!tmdbId) return false;
            const mediaType = payload.media_type || 'movie';
            const exists = payload.exists !== false;
            let changed = false;
            (items || []).forEach(item => {
                if (!item || String(getItemTmdbId(item) || '') !== tmdbId) return;
                if (!mediaTypesMatch(item.media_type || 'movie', mediaType)) return;
                item.exists_in_library = exists;
                changed = true;
            });
            return changed;
        };

        const filterItemsByActiveLibraryStatus = (items = []) => {
            const status = String(activeSourceFilters.value?.[LIBRARY_STATUS_FILTER_KEY] ?? '');
            if (!status) return items;
            return items.filter(item => status === 'exists' ? !!item.exists_in_library : !item.exists_in_library);
        };

        const refreshDetailLibraryStateFromEvent = async (payload = {}) => {
            if (!detailModal.visible) return;
            const tmdbId = String(payload.tmdb_id || '');
            const mediaType = payload.media_type || 'movie';
            const exists = payload.exists !== false;
            const detailTmdbId = String(detailModal.detail?.tmdb_id || detailModal.item?._tmdb_id || detailModal.item?.id || '');
            const detailMediaType = detailModal.detail?.media_type || detailModal.item?.media_type || 'movie';
            if (!tmdbId || tmdbId !== detailTmdbId || !mediaTypesMatch(detailMediaType, mediaType)) return;
            if (detailModal.item) detailModal.item.exists_in_library = exists;
            if (detailModal.detail) detailModal.detail.exists_in_library = exists;
            if (mediaTypesMatch(mediaType, 'tv')) await loadLibrarySeriesStatus(tmdbId);
        };

        const handleDiscoverIndexUpdated = async (payload = {}) => {
            const hasStatusFilter = !!String(activeSourceFilters.value?.[LIBRARY_STATUS_FILTER_KEY] ?? '');
            if (tab.value === 'media_subscribe' && hasStatusFilter && !mainGridLoading.value) {
                loadMainGrid(true);
            } else {
                applyLibraryEventToItems(mainGridItems.value, payload);
                writeMainGridCacheFromCurrent();
            }

            applyLibraryEventToItems(gridModal.items, payload);
            gridModal.items = filterItemsByActiveLibraryStatus(gridModal.items);

            applyLibraryEventToItems(searchMovieResults.value, payload);
            applyLibraryEventToItems(searchTvResults.value, payload);
            searchMovieResults.value = filterItemsByActiveLibraryStatus(searchMovieResults.value);
            searchTvResults.value = filterItemsByActiveLibraryStatus(searchTvResults.value);

            if (detailModal.detail) {
                applyLibraryEventToItems(detailModal.detail.recommendation_items || [], payload);
                applyLibraryEventToItems(detailModal.detail.similar_items || [], payload);
            }
            await refreshDetailLibraryStateFromEvent(payload);
        };

        const refreshMissingEpisodeStatsFromCache = async () => {
            if (!missingEpisodeStats.loaded || missingEpisodeStats.loading) return;
            const runId = ++missingEpisodeStatsRunId;
            try {
                const res = await axios.get('/api/discover/library/missing-episode-stats');
                if (runId !== missingEpisodeStatsRunId) return;
                applyMissingEpisodeStatsData(res.data || {});
                missingEpisodeStats.loading = false;
            } catch (e) {
                if (runId === missingEpisodeStatsRunId) {
                    missingEpisodeStats.error = e.response?.data?.detail || e.message || '刷新缓存失败';
                }
            }
        };

        const setupDiscoverRealtimeEvents = () => {
            if (discoverRealtimeEventSource || typeof EventSource === 'undefined') return;
            discoverRealtimeEventSource = new EventSource('/api/discover/events');
            discoverRealtimeEventSource.addEventListener('discover_index_updated', (event) => {
                try {
                    handleDiscoverIndexUpdated(JSON.parse(event.data || '{}')).catch(e => {
                        console.error('处理发现页索引事件失败:', e);
                    });
                } catch (e) {
                console.error('处理发现页索引事件失败:', e);
                }
            });
            discoverRealtimeEventSource.addEventListener('missing_episode_stats_updated', (event) => {
                try {
                    const data = JSON.parse(event.data || '{}');
                    if (applyMissingEpisodeManualCompleteEvent(data)) return;
                } catch (e) {
                    console.error('处理缺集统计事件失败:', e);
                }
                refreshMissingEpisodeStatsFromCache();
            });
        };

        setupDiscoverRealtimeEvents();

    return {
        detailModal,
        missingEpisodeCompareModal,
        mpSubscribeModal,
        openMediaDetail,
        closeDetailModal,
        handleDetailPopstate,
        missingEpisodeStats,
        missingEpisodeLibraries,
        missingEpisodeActiveLibrary,
        missingEpisodeActiveSummary,
        missingEpisodeActiveErrorCount,
        missingEpisodeActionableMissingCount,
        missingEpisodeActionableEpisodeCount,
        missingEpisodeSearchActive,
        missingEpisodeStatsProblemItems,
        visibleMissingEpisodeStatsProblemItems,
        missingEpisodeHasMoreVisibleItems,
        missingEpisodePosterGridRef,
        missingEpisodeLoadMoreSentinel,
        getMissingEpisodePosterKey,
        getMissingEpisodePosterCategoryLabel,
        isMissingEpisodeErrorRow,
        shouldShowMissingEpisodeTmdbCompare,
        isMissingEpisodeManualComplete,
        isMissingEpisodeManualCompleteUpdating,
        isMissingEpisodePosterReady,
        countLocalEpisodes,
        formatLocalSeasonBrief,
        getLocalSeasonRows,
        getTmdbSeasonRows,
        formatEpisodeNumber,
        isEpisodeListed,
        openMissingEpisodeCard,
        toggleMissingEpisodeManualComplete,
        openMissingEpisodeCompareDetail,
        openMissingEpisodeResourceSearch,
        openMissingEpisodeMpSubscribe,
        closeMpSubscribeModal,
        confirmMpSubscribe,
        toggleMpSubscribeSeason,
        openDetailMpSubscribe,
        canOpenMissingEpisodeEmby,
        getMissingEpisodeEmbyUrl,
        openMissingEpisodeEmby,
        closeMissingEpisodeCompare,
        loadMissingEpisodeStatsShell,
        runMissingEpisodeStats,
        refreshMissingEpisodeStats,
        calibrateMissingEpisodeStats,
        setMissingEpisodeLibrary,
        setMissingEpisodeFilter,
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
        resourceSearchSources,
        resourceSearchSourceLoading,
        resourceSearchSourceMenuOpen,
        selectedResourceSearchSources,
        selectedResourceSearchSourceLabels,
        resourceSearchSourceButtonText,
        resourceSearchSourceReady,
        toggleResourceSearchSourceMenu,
        closeResourceSearchSourceMenu,
        toggleResourceSearchSource,
        loadResourceSearchSources,
        resourceSearchModal,
        openDetailResourceSearch,
        closeResourceSearchModal,
        openForwardResource,
        previewForwardResource,
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
