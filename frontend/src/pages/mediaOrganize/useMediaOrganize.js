import axios from 'axios';
import { computed, nextTick, reactive, ref, watch } from 'vue';

export function useMediaOrganize({ tab, needs115Setup, notify115SetupRequired, showToast, showNumberDialog }) {
        // ==========================================
        // 媒体整理模块 (115 网盘)
        // ==========================================

        const mediaOrganizeConfig = reactive({
            drive_index: 0,
            source_cid: '0',
            source_name: '根目录',
            target_cid: '0',
            target_name: '根目录',
            failed_cid: '0',
            failed_name: '',
            movie_enabled: true,
            tv_enabled: true,
            scrape_enabled: true,
            emby_local_scrape: true,
            scrape_nfo: true,
            scrape_poster: true,
            scrape_fanart: true,
            scrape_logo: true,
            scrape_banner: true,
            scrape_thumb: true,
            scrape_season_poster: true,
            scrape_episode_thumb: true,
            policy_nfo: 'missing_only',
            policy_poster: 'missing_only',
            policy_fanart: 'missing_only',
            policy_logo: 'missing_only',
            policy_banner: 'missing_only',
            policy_thumb: 'missing_only',
            policy_season_poster: 'missing_only',
            policy_episode_thumb: 'missing_only',
            auto_detect_bluray: true,
            life_monitor_enabled: true,
            monitor_dirs: [],
            auto_sync_strm: true,
            emby_scrapers_enabled: false,
            emby_locale_defaults_fixed: false,
            wash_enabled: true,
            wash_by_equivalent_size: true,
            wash_tolerance_ratio: 0,
            wash_reserved_1: false,
            wash_reserved_2: false,
            organize_parse_mode: 'ffprobe',
            movie_folder_format: '{title} ({year}) {tmdb-{tmdb_id}}',
            movie_rename_format: '{en_title}.{year}.{resource_pix}.{web_source}.{resource_type}.{resource_effect}.{video_encode}.{color_depth}.{video_effect}.{fps}.{audio_encode}-{resource_team}',
            tv_folder_format: '{title} ({year}) {tmdb-{tmdb_id}}',
            tv_episode_format: '{en_title}.{season_episode}.{year}.{resource_pix}.{web_source}.{resource_type}.{video_encode}.{color_depth}.{video_effect}.{fps}.{audio_encode}-{resource_team}',
        });

        const ORGANIZE_RUN_ID_STORAGE_KEY = 'media_organize_run_id';
        const organizeLoading = ref(false);
        const organizeResult = ref(null);
        const organizeRunId = ref(localStorage.getItem(ORGANIZE_RUN_ID_STORAGE_KEY) || null);
        const organizeProgress = reactive({ percent: 0, status_text: '', detail: null });
        const identifyTest = reactive({
            visible: false,
            folder_name: '',
            file_name: '',
            loading: false,
            result: null,
            error: '',
        });
        const isOrganizeTerminalStatus = (status) => ['finished', 'error', 'stopped', 'interrupted'].includes(status);
        const formatOrganizeProgressText = (task = {}) => {
            const detail = task.detail || {};
            if (detail.task === 'collection_backfill' && detail.processed !== undefined && detail.total !== undefined) {
                return `合集补齐: ${detail.processed}/${detail.total}`;
            }
            if (detail.processed !== undefined && detail.total !== undefined) {
                return `已处理: ${detail.processed}/${detail.total}`;
            }
            return task.name || '';
        };
        const buildOrganizeResultMessage = (task = {}, label = '') => {
            const detail = task.detail || {};
            if (task.status === 'interrupted') {
                return task.resume_message || task.name || '整理已中断';
            }
            if (detail.task === 'collection_backfill') {
                return `电影合集补齐${label}`;
            }
            return `整理${label}`;
        };

        const syncOrganizeTaskFromTaskMap = (tasks, { adoptRunning = false } = {}) => {
            const taskMap = tasks || {};
            let currentRunId = organizeRunId.value;
            let currentTask = currentRunId ? taskMap[currentRunId] : null;

            if ((!currentTask || currentTask.status !== 'running') && adoptRunning) {
                const runningEntry = Object.entries(taskMap).find(([id, task]) => {
                    if (!task || task.status !== 'running') return false;
                    return task.task_type === 'media_organize' || id.startsWith('organize_');
                });
                if (runningEntry) {
                    currentRunId = runningEntry[0];
                    currentTask = runningEntry[1];
                    if (organizeRunId.value !== currentRunId) {
                        organizeRunId.value = currentRunId;
                        localStorage.setItem(ORGANIZE_RUN_ID_STORAGE_KEY, currentRunId);
                    }
                }
            }

            if (!currentTask) {
                if (adoptRunning && organizeRunId.value && !taskMap[organizeRunId.value]) {
                    organizeRunId.value = null;
                    localStorage.removeItem(ORGANIZE_RUN_ID_STORAGE_KEY);
                }
                return null;
            }

            organizeLoading.value = currentTask.status === 'running';
            organizeProgress.percent = Math.round(currentTask.percent || 0);
            organizeProgress.status_text = currentTask.cancel_requested ? '正在取消...' : formatOrganizeProgressText(currentTask);
            organizeProgress.detail = currentTask.detail || null;
            return currentTask;
        };

        const openIdentifyTest = () => {
            identifyTest.visible = true;
        };

        const closeIdentifyTest = () => {
            if (identifyTest.loading) return;
            identifyTest.visible = false;
        };

        const runIdentifyTest = async () => {
            const folderName = String(identifyTest.folder_name || '').trim();
            const fileName = String(identifyTest.file_name || '').trim();
            if (!folderName && !fileName) {
                identifyTest.error = '请输入文件夹名或文件名';
                showToast('请输入文件夹名或文件名', 'warning');
                return;
            }
            identifyTest.loading = true;
            identifyTest.error = '';
            identifyTest.result = null;
            try {
                const res = await axios.post('/api/media_organize/identify_test', {
                    folder_name: folderName,
                    file_name: fileName,
                    media_type: 'auto',
                });
                const data = res.data || {};
                if (data.status === 'success') {
                    identifyTest.result = data;
                    showToast('识别测试完成', 'success');
                } else {
                    identifyTest.error = data.message || '识别失败';
                    identifyTest.result = data;
                    showToast(identifyTest.error, 'error');
                }
            } catch (e) {
                identifyTest.error = e.response?.data?.detail || e.message || '识别测试失败';
                showToast('识别测试失败', 'error');
            } finally {
                identifyTest.loading = false;
            }
        };

        const restoreRunningOrganizeTask = async () => {
            try {
                const res = await axios.get('/api/progress');
                const task = syncOrganizeTaskFromTaskMap(res.data || {}, { adoptRunning: true });
                if (task && task.status === 'running') {
                    startOrganizePolling();
                } else if (task && isOrganizeTerminalStatus(task.status)) {
                    organizeLoading.value = false;
                    if (task.status === 'interrupted') {
                        organizeResult.value = {
                            status: 'error',
                            message: task.resume_message || task.name || '整理已中断',
                            detail: task.detail || {},
                        };
                    }
                    organizeRunId.value = null;
                    localStorage.removeItem(ORGANIZE_RUN_ID_STORAGE_KEY);
                }
            } catch (_) { }
        };
        let organizePollTimer = null;

        // 二级分类规则
        const categoryRulesEditor = reactive({ activeType: 'movie', movie: [], tv: [] });
        const categoryRulesSaving = ref(false);
        const subClassify = reactive({
            movie: { enabled: true, levels: ['year_decade'] },
            tv:    { enabled: true, levels: ['year_decade'] },
            sync_emby_library: true,
            emby_server_idx: 0,
            emby_library_level: 'level3',
        });
        const subClassifyVars = [
            { key: 'year_decade', label: '年代' },
            { key: 'rating_tier', label: '评分段' },
            { key: 'origin_country', label: '国家' },
            { key: 'genre_label', label: '类型' },
        ];
        const subClassifyVarExamples = {
            year_decade: '2010s',
            rating_tier: '8-9分',
            origin_country: '日本',
            genre_label: '动画',
        };
        const subClassifyBaseExamples = {
            movie: '电影/日本电影',
            tv: '剧集/日本动漫',
        };
        const subClassifyPreviewSegments = (mtype) => {
            return (subClassify[mtype]?.levels || []).map(key => {
                const meta = subClassifyVars.find(x => x.key === key);
                return {
                    key,
                    label: meta?.label || key,
                    example: subClassifyVarExamples[key] || (meta?.label || key),
                };
            });
        };
        const ruleListEl = ref(null);
        let _ruleDragState = null;

        const fetchCategoryRules = async () => {
            try {
                const res = await axios.get('/api/media_organize/category_rules/get');
                categoryRulesEditor.movie = JSON.parse(JSON.stringify(res.data.movie || []));
                categoryRulesEditor.tv = JSON.parse(JSON.stringify(res.data.tv || []));
                const sc = res.data.sub_classify || {};
                for (const t of ['movie', 'tv']) {
                    subClassify[t].enabled = sc[t]?.enabled ?? true;
                    subClassify[t].levels = JSON.parse(JSON.stringify(sc[t]?.levels || ['year_decade']));
                }
                subClassify.sync_emby_library = sc.sync_emby_library ?? true;
                subClassify.emby_server_idx = 0;
                subClassify.emby_library_level = sc.emby_library_level || 'level3';
            } catch (e) {
                console.error('获取分类规则失败', e);
            }
        };

        const saveCategoryRules = async () => {
            categoryRulesSaving.value = true;
            try {
                const res = await axios.post('/api/media_organize/category_rules/save', {
                    movie: categoryRulesEditor.movie,
                    tv: categoryRulesEditor.tv,
                });
                const data = res.data || {};
                showToast('分类规则已保存');

                const removedPaths = Array.isArray(data.removed_paths)
                    ? data.removed_paths
                    : (Array.isArray(data.diff?.removed_paths) ? data.diff.removed_paths : []);
                const warnings = Array.isArray(data.warnings) ? data.warnings : [];
                if (removedPaths.length) {
                    const warningText = warnings[0] || '这些旧分类路径已删除，但对应 Emby 媒体库不会自动删除，请自行到 Emby 手动清理';
                    setTimeout(() => {
                        alert(`${warningText}\n\n已删除路径：\n- ${removedPaths.join('\n- ')}`);
                    }, 80);
                }
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
            } finally {
                categoryRulesSaving.value = false;
            }
        };

        let _subClassifyTimer = null;
        const saveSubClassify = () => {
            clearTimeout(_subClassifyTimer);
            _subClassifyTimer = setTimeout(async () => {
                try {
                    await axios.post('/api/media_organize/category_rules/sub_classify/save', {
                        movie: { enabled: subClassify.movie.enabled, levels: subClassify.movie.levels },
                        tv:    { enabled: subClassify.tv.enabled,    levels: subClassify.tv.levels },
                        sync_emby_library: subClassify.sync_emby_library,
                        emby_server_idx: 0,
                        emby_library_level: subClassify.emby_library_level,
                    });
                } catch (e) {
                    console.error('子分类设置保存失败', e);
                }
            }, 500);
        };

        const addRule = (type) => {
            categoryRulesEditor[type].push({ path: '', conditions: [] });
        };
        const removeRule = (type, idx) => {
            categoryRulesEditor[type].splice(idx, 1);
        };
        const addCondition = (type, rIdx) => {
            categoryRulesEditor[type][rIdx].conditions.push({ field: '', logic: 'AND', value: '' });
        };
        const removeCondition = (type, rIdx, cIdx) => {
            categoryRulesEditor[type][rIdx].conditions.splice(cIdx, 1);
        };
        const resetCategoryRules = async () => {
            if (!confirm('确定要恢复出厂默认分类规则吗？当前分类规则将被覆盖，子分类设置不受影响。')) return;
            try {
                const res = await axios.get('/api/media_organize/category_rules/defaults');
                categoryRulesEditor.movie = JSON.parse(JSON.stringify(res.data.movie || []));
                categoryRulesEditor.tv = JSON.parse(JSON.stringify(res.data.tv || []));
                showToast('已加载出厂默认分类规则，点击保存生效');
            } catch (e) {
                showToast('加载默认规则失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };
        const subClassifyToggleLevel = (mtype, key) => {
            const levels = subClassify[mtype].levels;
            const idx = levels.indexOf(key);
            if (idx === -1) levels.push(key);
            else levels.splice(idx, 1);
            saveSubClassify();
        };
        const embyLibCount = (level) => {
            const paths = new Set();
            for (const r of (categoryRulesEditor.movie || [])) { if (r.path) paths.add(r.path); }
            for (const r of (categoryRulesEditor.tv || [])) { if (r.path) paths.add(r.path); }
            if (level === 'rule') return paths.size;
            const n = parseInt(level.replace('level', ''));
            const groups = new Set();
            for (const p of paths) {
                const parts = p.split('/');
                groups.add(parts.length >= n ? parts.slice(0, n).join('/') : p);
            }
            return groups.size;
        };
        const embyLibLevelOptions = () => {
            const paths = new Set();
            for (const r of (categoryRulesEditor.movie || [])) { if (r.path) paths.add(r.path); }
            for (const r of (categoryRulesEditor.tv || [])) { if (r.path) paths.add(r.path); }
            let maxDepth = 1;
            for (const p of paths) { const d = p.split('/').length; if (d > maxDepth) maxDepth = d; }
            const opts = [{ value: 'rule', label: `每条规则一个库（${paths.size}个）` }];
            for (let i = maxDepth; i >= 1; i--) {
                opts.push({ value: `level${i}`, label: `按${i}级目录合并（${embyLibCount(`level${i}`)}个）` });
            }
            return opts;
        };
        const embyCacheRefreshing = ref(false);
        async function refreshEmbyCache() {
            embyCacheRefreshing.value = true;
            try {
                const resp = await fetch('/api/media_organize/emby_lib_cache/refresh', { method: 'POST' });
                const data = await resp.json();
                if (data.status === 'success') {
                    showToast(`缓存已刷新（${data.count} 个媒体库）`, 'success');
                }
            } catch (e) {
                showToast('刷新失败: ' + e.message, 'error');
            } finally {
                embyCacheRefreshing.value = false;
            }
        }
        let _levelDragState = null;
        const onLevelDragStart = (e, mtype, idx) => { _levelDragState = { mtype, from: idx }; e.dataTransfer.effectAllowed = 'move'; };
        const onLevelDragOver = (e, mtype, idx) => { if (_levelDragState?.mtype === mtype) _levelDragState.to = idx; };
        const onLevelDrop = (e, mtype, idx) => {
            if (!_levelDragState || _levelDragState.mtype !== mtype || _levelDragState.from === idx) return;
            const levels = subClassify[mtype].levels;
            const [moved] = levels.splice(_levelDragState.from, 1);
            levels.splice(idx, 0, moved);
            _levelDragState = null;
            saveSubClassify();
        };
        const onLevelDragEnd = () => { _levelDragState = null; };

        // 拖拽排序（原生 HTML5 drag，无需额外依赖）
        const onRuleDragStart = (e, type, idx) => {
            _ruleDragState = { type, from: idx };
            e.dataTransfer.effectAllowed = 'move';
        };
        const onRuleDragOver = (e, type, idx) => {
            e.preventDefault();
            if (_ruleDragState && _ruleDragState.type === type && _ruleDragState.from !== idx) _ruleDragState.to = idx;
        };
        const onRuleDrop = (e, type, idx) => {
            e.preventDefault();
            if (!_ruleDragState || _ruleDragState.type !== type || _ruleDragState.from === idx) return;
            const list = categoryRulesEditor[type];
            const [moved] = list.splice(_ruleDragState.from, 1);
            list.splice(idx, 0, moved);
            _ruleDragState = null;
        };
        const onRuleDragEnd = () => { _ruleDragState = null; };

        // Refs for rename template textareas (for cursor-aware token insertion)
        const movieFormatRef = ref(null);
        const movieFolderFormatRef = ref(null);
        const tvFolderFormatRef = ref(null);
        const tvEpisodeFormatRef = ref(null);
        const activeRenameTemplateType = ref('movie');
        let _renameTemplateDragState = null;

        // Preview example variables
        const MOVIE_PREVIEW_VARS = {
            title: '流浪地球', en_title: 'The.Wandering.Earth', original_title: '',
            year: '2019', tmdb_id: '521777',
            resource_pix: '2160p', resource_type: 'BluRay',
            video_encode: 'HEVC', audio_encode: 'DTS-HD.MA.7.1',
            web_source: 'UHD', resource_effect: 'REMUX',
            video_effect: 'DV.HDR',
            resource_team: 'CHD', fps: '60fps', part: '',
            color_depth: '10bit',
            first_letter: 'T', ext: 'mkv',
            season_episode: '', season_num: '', episode_num: '',
        };
        const TV_PREVIEW_VARS = {
            title: '怪奇物语', en_title: 'Stranger.Things', original_title: '',
            year: '2022', tmdb_id: '66732',
            season_episode: 'S04E01', season_num: '04', episode_num: '01',
            resource_pix: '2160p', resource_type: 'WEB-DL',
            video_encode: 'HEVC', audio_encode: 'Atmos.5.1',
            web_source: 'NF', resource_effect: '',
            video_effect: 'DV.HDR',
            resource_team: 'CHD', fps: '23.976fps', part: '',
            color_depth: '10bit',
            first_letter: 'S', ext: 'mkv',
        };

        /**
         * 渲染重命名模板（前端版，与后端 _render_template 逻辑一致）
         */
        function renderPreview(template, vars) {
            if (!template) return '';
            let result = template;
            for (const [key, value] of Object.entries(vars)) {
                result = result.replaceAll('{' + key + '}', value || '');
            }
            // 清理多余分隔符
            result = result.replace(/\.{2,}/g, '.');
            result = result.replace(/-{2,}/g, '-');
            result = result.replace(/_{2,}/g, '_');
            result = result.replace(/ {2,}/g, ' ');
            result = result.replace(/\(\s*\)/g, '');
            result = result.replace(/\[\s*\]/g, '');
            result = result.replace(/\.\./g, '.');
            result = result.replace(/^[.\-_ ]+|[.\-_ ]+$/g, '');
            return result;
        }

        const MOVIE_FOLDER_DISPLAY_FORMAT = '{中文标题} ({公映年份}) {TMDB编号}';
        const TV_FOLDER_DISPLAY_FORMAT = '{中文剧名} ({首播年份}) {TMDB编号}';
        const MOVIE_DISPLAY_FORMAT = '{英文片名}.{公映年份}.{分辨率}.{介质来源}.{处理方式}.{视频编码}.{色深}.{动态范围}.{帧率}.{音频规格}-{制作组}.mkv';
        const TV_DISPLAY_FORMAT = '{英文剧名}.{季数集数}.{首播年份}.{分辨率}.{来源平台}.{介质类型}.{视频编码}.{色深}.{动态范围}.{帧率}.{音频规格}-{制作组}.mkv';

        const MOVIE_TEMPLATE_LABELS = [
            ['{audio_encode}', '{音频规格}'],
            ['{resource_effect}', '{处理方式}'],
            ['{resource_team}', '{制作组}'],
            ['{resource_type}', '{介质来源}'],
            ['{video_effect}', '{动态范围}'],
            ['{video_encode}', '{视频编码}'],
            ['{color_depth}', '{色深}'],
            ['{resource_pix}', '{分辨率}'],
            ['{en_title}', '{英文片名}'],
            ['{tmdb-{tmdb_id}}', '{TMDB编号}'],
            ['{tmdb_id}', '{TMDB编号}'],
            ['{title}', '{中文标题}'],
            ['{year}', '{公映年份}'],
            ['{part}', '{分Part}'],
            ['{fps}', '{帧率}'],
            ['{ext}', '{文件后缀}'],
        ];
        const TV_TEMPLATE_LABELS = [
            ['{season_episode}', '{季数集数}'],
            ['{audio_encode}', '{音频规格}'],
            ['{resource_team}', '{制作组}'],
            ['{resource_type}', '{介质类型}'],
            ['{video_effect}', '{动态范围}'],
            ['{video_encode}', '{视频编码}'],
            ['{color_depth}', '{色深}'],
            ['{resource_pix}', '{分辨率}'],
            ['{episode_num}', '{集号}'],
            ['{season_num}', '{季号}'],
            ['{web_source}', '{来源平台}'],
            ['{en_title}', '{英文剧名}'],
            ['{tmdb-{tmdb_id}}', '{TMDB编号}'],
            ['{tmdb_id}', '{TMDB编号}'],
            ['{title}', '{中文剧名}'],
            ['{year}', '{首播年份}'],
            ['{part}', '{分Part}'],
            ['{fps}', '{帧率}'],
            ['{ext}', '{文件后缀}'],
        ];

        function templateToDisplay(template, labels) {
            let result = template || '';
            for (const [raw, display] of labels) {
                result = result.replaceAll(raw, display);
            }
            return result;
        }

        function displayToTemplate(display, labels) {
            let result = display || '';
            result = result.replaceAll('{tmdb-{TMDB编号}}', '{tmdb-{tmdb_id}}');
            for (const [raw, displayLabel] of labels) {
                const nextRaw = raw === '{tmdb_id}' ? '{tmdb-{tmdb_id}}' : raw;
                result = result.replaceAll(displayLabel, nextRaw);
            }
            return result;
        }

        function movieFolderTemplateToDisplay(template) {
            return templateToDisplay(template, MOVIE_TEMPLATE_LABELS);
        }

        function movieFolderDisplayToTemplate(display) {
            return displayToTemplate(display, MOVIE_TEMPLATE_LABELS);
        }

        function tvFolderTemplateToDisplay(template) {
            return templateToDisplay(template, TV_TEMPLATE_LABELS);
        }

        function tvFolderDisplayToTemplate(display) {
            return displayToTemplate(display, TV_TEMPLATE_LABELS);
        }

        function movieTemplateToDisplay(template) {
            let result = template || '';
            result = result.replaceAll('{web_source}.{resource_type}.{resource_effect}', '{介质来源}.{处理方式}');
            result = result.replaceAll('{web_source}.{resource_effect}', '{介质来源}.{处理方式}');
            result = templateToDisplay(result, MOVIE_TEMPLATE_LABELS);
            return result.includes('{文件后缀}') ? result : `${result}.{文件后缀}`;
        }

        function movieDisplayToTemplate(display) {
            let result = display || '';
            result = result.replaceAll('.{文件后缀}', '');
            result = result.replaceAll('{文件后缀}', '');
            result = result.replace(/\.mkv$/i, '');
            result = displayToTemplate(result, MOVIE_TEMPLATE_LABELS);
            result = result.replaceAll('{介质来源}.{处理方式}', '{web_source}.{resource_type}.{resource_effect}');
            return result;
        }

        function tvTemplateToDisplay(template) {
            let result = template || '';
            result = templateToDisplay(result, TV_TEMPLATE_LABELS);
            return result.includes('{文件后缀}') ? result : `${result}.{文件后缀}`;
        }

        function tvDisplayToTemplate(display) {
            let result = display || '';
            result = result.replaceAll('.{文件后缀}', '');
            result = result.replaceAll('{文件后缀}', '');
            result = result.replace(/\.mkv$/i, '');
            return displayToTemplate(result, TV_TEMPLATE_LABELS);
        }

        const movieFolderFormatDisplay = computed({
            get: () => movieFolderTemplateToDisplay(mediaOrganizeConfig.movie_folder_format),
            set: (value) => {
                mediaOrganizeConfig.movie_folder_format = movieFolderDisplayToTemplate(value);
            }
        });
        const tvFolderFormatDisplay = computed({
            get: () => tvFolderTemplateToDisplay(mediaOrganizeConfig.tv_folder_format),
            set: (value) => {
                mediaOrganizeConfig.tv_folder_format = tvFolderDisplayToTemplate(value);
            }
        });
        const movieFormatDisplay = computed({
            get: () => movieTemplateToDisplay(mediaOrganizeConfig.movie_rename_format),
            set: (value) => {
                mediaOrganizeConfig.movie_rename_format = movieDisplayToTemplate(value);
            }
        });
        const tvEpisodeFormatDisplay = computed({
            get: () => tvTemplateToDisplay(mediaOrganizeConfig.tv_episode_format),
            set: (value) => {
                mediaOrganizeConfig.tv_episode_format = tvDisplayToTemplate(value);
            }
        });

        function setActiveRenameTemplate(type) {
            activeRenameTemplateType.value = type;
        }

        function getRenameDisplayRef(type) {
            return {
                movie: movieFormatDisplay,
                movieFolder: movieFolderFormatDisplay,
                tvFolder: tvFolderFormatDisplay,
                tvEpisode: tvEpisodeFormatDisplay,
            }[type] || null;
        }

        function getRenameTemplateDisplay(type) {
            return getRenameDisplayRef(type)?.value || '';
        }

        function setRenameTemplateDisplay(type, value) {
            const displayRef = getRenameDisplayRef(type);
            if (!displayRef) return;
            displayRef.value = value;
        }

        function activeRenameTemplateForGroup(group) {
            const active = activeRenameTemplateType.value || '';
            if (group === 'movie') return active.startsWith('movie') ? active : 'movie';
            if (group === 'tv') return active.startsWith('tv') ? active : 'tvEpisode';
            return active || 'movie';
        }

        function renameTemplateSegments(type) {
            const value = getRenameTemplateDisplay(type);
            if (!value) return [];
            const rawSegments = value
                .split(/(\{[^{}]+\})/g)
                .filter(part => part !== '')
                .map(part => ({
                    type: part.startsWith('{') && part.endsWith('}') ? 'token' : 'text',
                    value: part,
                    label: part.startsWith('{') && part.endsWith('}') ? part.slice(1, -1) : part,
                }));
            const segments = [];
            for (let i = 0; i < rawSegments.length; i += 1) {
                const prev = rawSegments[i];
                const current = rawSegments[i + 1];
                const next = rawSegments[i + 2];
                if (prev?.type === 'text' && current?.type === 'token' && next?.type === 'text') {
                    const open = prev.value.match(/^(.*)([([])\s*$/);
                    const close = next.value.match(/^\s*([)\]])(.*)$/);
                    const pairs = { '(': ')', '[': ']' };
                    if (open && close && pairs[open[2]] === close[1]) {
                        if (open[1]) {
                            segments.push({ type: 'text', value: open[1], label: open[1] });
                        }
                        segments.push({
                            type: 'token',
                            value: `${open[2]}${current.value}${close[1]}`,
                            label: current.label,
                        });
                        if (close[2]) {
                            segments.push({ type: 'text', value: close[2], label: close[2] });
                        }
                        i += 2;
                        continue;
                    }
                }
                segments.push(rawSegments[i]);
            }
            return segments.flatMap((segment) => {
                if (segment.type !== 'text') return [segment];
                return Array.from(segment.value).map(char => ({
                    type: 'text',
                    value: char,
                    label: char,
                }));
            });
        }

        function rebuildRenameTemplateFromSegments(type, updater) {
            const segments = renameTemplateSegments(type);
            const nextSegments = updater(segments);
            setRenameTemplateDisplay(type, nextSegments.map(segment => segment.value).join(''));
            activeRenameTemplateType.value = type;
        }

        function updateRenameTemplateSegment(type, index, value) {
            rebuildRenameTemplateFromSegments(type, (segments) => {
                if (!segments[index]) return segments;
                segments[index] = { ...segments[index], value: value || '', label: value || '' };
                return segments;
            });
        }

        function renameTemplateLiteralLabel(value) {
            if (value === ' ') return '空格';
            return value || '空';
        }

        function renameTokenClass(label) {
            const token = String(label || '');
            if (token === '文件后缀') return 'rename-token-ext';
            if (['中文标题', '中文剧名', '英文片名', '英文剧名'].includes(token)) return 'rename-token-title';
            if (['公映年份', '首播年份'].includes(token)) return 'rename-token-year';
            if (['TMDB编号'].includes(token)) return 'rename-token-id';
            if (['介质来源', '来源平台', '介质类型'].includes(token)) return 'rename-token-source';
            if (['处理方式', '动态范围'].includes(token)) return 'rename-token-effect';
            if (['分辨率', '视频编码', '色深', '帧率'].includes(token)) return 'rename-token-video';
            if (['音频规格'].includes(token)) return 'rename-token-audio';
            if (['制作组'].includes(token)) return 'rename-token-team';
            if (['季数集数', '季号', '集号'].includes(token)) return 'rename-token-episode';
            if (['分Part'].includes(token)) return 'rename-token-part';
            return 'rename-token-default';
        }

        function removeRenameTemplateSegment(type, index) {
            rebuildRenameTemplateFromSegments(type, (segments) => {
                if (segments[index]?.label === '文件后缀') return segments;
                if (segments[index]?.type === 'token') {
                    const prev = segments[index - 1];
                    const next = segments[index + 1];
                    const isPrevSeparator = prev?.type === 'text' && /^[\s._-]+$/.test(prev.value);
                    const isNextSeparator = next?.type === 'text' && /^[\s._-]+$/.test(next.value);
                    if (isPrevSeparator && isNextSeparator && prev.value === next.value) {
                        segments.splice(index, 2);
                        return segments;
                    }
                    if (isNextSeparator && index === 0) {
                        segments.splice(index, 2);
                        return segments;
                    }
                    if (isPrevSeparator) {
                        segments.splice(index - 1, 2);
                        return segments;
                    }
                }
                segments.splice(index, 1);
                return segments;
            });
        }

        function onRenameTemplateDragStart(event, type, index) {
            _renameTemplateDragState = { type, index };
            activeRenameTemplateType.value = type;
            event.dataTransfer.effectAllowed = 'move';
        }

        function onRenameTemplateDragOver(event, type) {
            if (_renameTemplateDragState?.type === type) {
                event.preventDefault();
                event.dataTransfer.dropEffect = 'move';
            }
        }

        function onRenameTemplateDrop(event, type, index) {
            event.preventDefault();
            if (!_renameTemplateDragState || _renameTemplateDragState.type !== type) return;
            const from = _renameTemplateDragState.index;
            rebuildRenameTemplateFromSegments(type, (segments) => {
                if (from < 0 || from >= segments.length || from === index) return segments;
                const [moved] = segments.splice(from, 1);
                const target = index > from ? index - 1 : index;
                segments.splice(Math.max(0, Math.min(target, segments.length)), 0, moved);
                return segments;
            });
            _renameTemplateDragState = null;
        }

        function insertRenameLiteral(type, literal) {
            const displayRef = getRenameDisplayRef(type);
            if (!displayRef) return;
            displayRef.value = `${displayRef.value || ''}${literal}`;
            activeRenameTemplateType.value = type;
        }

        function insertRenameTokenForGroup(group, token) {
            insertToken(activeRenameTemplateForGroup(group), token);
        }

        function insertRenameLiteralForGroup(group, literal) {
            insertRenameLiteral(activeRenameTemplateForGroup(group), literal);
        }

        function onRenameTemplateDragEnd() {
            _renameTemplateDragState = null;
        }

        // 实时预览计算属性
        const moviePreviewName = computed(() =>
            renderPreview(mediaOrganizeConfig.movie_rename_format, MOVIE_PREVIEW_VARS) || '（请输入模板）'
        );
        const movieFolderPreviewName = computed(() =>
            renderPreview(mediaOrganizeConfig.movie_folder_format, MOVIE_PREVIEW_VARS) || '（请输入模板）'
        );
        const tvFolderPreviewName = computed(() =>
            renderPreview(mediaOrganizeConfig.tv_folder_format, TV_PREVIEW_VARS) || '（请输入模板）'
        );
        const tvEpisodePreviewName = computed(() =>
            renderPreview(mediaOrganizeConfig.tv_episode_format, TV_PREVIEW_VARS) || '（请输入模板）'
        );

        /**
         * 在光标位置插入 token
         * @param {string} type - 'movie' | 'tvFolder' | 'tvEpisode'
         * @param {string} token - 要插入的 token 字符串
         */
        function insertToken(type, token) {
            const refMap = {
                movie: movieFormatRef,
                movieFolder: movieFolderFormatRef,
                tvFolder: tvFolderFormatRef,
                tvEpisode: tvEpisodeFormatRef,
            };
            const el = refMap[type]?.value;
            const displayRef = getRenameDisplayRef(type);
            if (!displayRef) return;

            if (!el) {
                displayRef.value = `${displayRef.value || ''}${token}`;
                activeRenameTemplateType.value = type;
                return;
            }

            const start = el.selectionStart ?? el.value.length;
            const end = el.selectionEnd ?? el.value.length;
            const current = displayRef.value || '';
            displayRef.value = current.slice(0, start) + token + current.slice(end);
            activeRenameTemplateType.value = type;

            // 恢复光标到插入后的位置
            nextTick(() => {
                el.focus();
                const pos = start + token.length;
                el.setSelectionRange(pos, pos);
            });
        }

        async function resetMovieFormat() {
            const res = await axios.get('/api/media_organize/defaults');
            mediaOrganizeConfig.movie_folder_format = res.data.movie_folder_format;
            mediaOrganizeConfig.movie_rename_format = res.data.movie_rename_format;
        }

        async function resetTvFormat() {
            const res = await axios.get('/api/media_organize/defaults');
            mediaOrganizeConfig.tv_folder_format = res.data.tv_folder_format;
            mediaOrganizeConfig.tv_episode_format = res.data.tv_episode_format;
        }

        // 整理专用表单
        const organizeForm = reactive({
            media_type: '',
            overwrite: false,
            is_bluray: false,
        });

        // 源目录浏览器
        const orgSourceBrowser = reactive({
            dirs: [],
            path: '',
            history: [],
            currentCid: '0',
            opened: false
        });

        // 目标目录浏览器
        const orgTargetBrowser = reactive({
            dirs: [],
            path: '',
            history: [],
            currentCid: '0',
            opened: false
        });

        // 失败目录浏览器
        const orgFailedBrowser = reactive({
            dirs: [],
            path: '',
            history: [],
            currentCid: '0',
            opened: false
        });

        const monitorDirBrowser = reactive({
            visible: false,
            loading: false,
            currentCid: '0',
            currentPath: '/',
            history: [],
            dirs: []
        });
        const monitorDirsSaving = ref(false);

        const normalizeOrganizeParseMode = () => {
            const mode = (mediaOrganizeConfig.organize_parse_mode || '').toLowerCase();
            if (mode === 'filename' || mode === 'ffprobe' || mode === 'ffprobe_full' || mode === '') {
                mediaOrganizeConfig.organize_parse_mode = mode;
                return;
            }
            mediaOrganizeConfig.organize_parse_mode = 'filename';
        };

        const applyDefaultScrapeSettings = () => {
            mediaOrganizeConfig.scrape_enabled = true;
            mediaOrganizeConfig.emby_local_scrape = true;
            mediaOrganizeConfig.scrape_nfo = true;
            mediaOrganizeConfig.scrape_poster = true;
            mediaOrganizeConfig.scrape_fanart = true;
            mediaOrganizeConfig.scrape_logo = true;
            mediaOrganizeConfig.scrape_banner = true;
            mediaOrganizeConfig.scrape_thumb = true;
            mediaOrganizeConfig.scrape_season_poster = true;
            mediaOrganizeConfig.scrape_episode_thumb = true;
            mediaOrganizeConfig.policy_nfo = 'missing_only';
            mediaOrganizeConfig.policy_poster = 'missing_only';
            mediaOrganizeConfig.policy_fanart = 'missing_only';
            mediaOrganizeConfig.policy_logo = 'missing_only';
            mediaOrganizeConfig.policy_banner = 'missing_only';
            mediaOrganizeConfig.policy_thumb = 'missing_only';
            mediaOrganizeConfig.policy_season_poster = 'missing_only';
            mediaOrganizeConfig.policy_episode_thumb = 'missing_only';
        };

        const fetchMediaOrganizeConfig = async () => {
            try {
                const res = await axios.get('/api/media_organize/get');
                if (res.data) {
                    Object.assign(mediaOrganizeConfig, res.data);
                    if (!Array.isArray(mediaOrganizeConfig.monitor_dirs)) {
                        mediaOrganizeConfig.monitor_dirs = [];
                    }
                    mediaOrganizeConfig.drive_index = 0;
                }
            } catch (e) { /* first load, use defaults */ }
            normalizeOrganizeParseMode();
            applyDefaultScrapeSettings();
        };

        const saveMediaOrganizeConfig = async () => {
            if (needs115Setup.value) {
                notify115SetupRequired();
                return false;
            }
            try {
                normalizeOrganizeParseMode();
                applyDefaultScrapeSettings();
                await axios.post('/api/media_organize/save', { ...mediaOrganizeConfig, drive_index: 0 });
                showToast('媒体整理配置已保存', 'success');
                return true;
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
                return false;
            }
        };

        const toggleAutoSyncStrm = async (event) => {
            const nextChecked = !!event?.target?.checked;
            if (!nextChecked) {
                mediaOrganizeConfig.auto_sync_strm = false;
                return;
            }
            try {
                const res = await axios.get('/api/strm/get');
                const tasks = res.data?.sync_tasks || [];
                const valid = tasks.some(t => t.remote_path && t.local_path);
                if (!valid) {
                    showToast('请先在 STRM生成 页面配置好同步任务（远程路径、本地路径）', 'error');
                    mediaOrganizeConfig.auto_sync_strm = false;
                } else {
                    mediaOrganizeConfig.auto_sync_strm = true;
                }
            } catch (e) {
                showToast('验证 STRM 配置失败: ' + e.message, 'error');
                mediaOrganizeConfig.auto_sync_strm = false;
            }
        };

        const toggleEmbyScrapers = async (event) => {
            const nextChecked = !!event?.target?.checked;
            mediaOrganizeConfig.emby_scrapers_enabled = nextChecked;
            if (nextChecked) {
                showToast('建议关闭，你会更快乐~', 'warning');
                if (!mediaOrganizeConfig.emby_locale_defaults_fixed) {
                    try {
                        const resp = await axios.post('/api/media_organize/emby_libraries/fix_locale_defaults', {
                            overwrite: false,
                            once_only: true,
                        });
                        const data = resp.data || {};
                        if (data.status === 'success' || data.status === 'partial_success') {
                            mediaOrganizeConfig.emby_locale_defaults_fixed = true;
                            showToast(data.message || '已修复已有 Emby 媒体库语言设置', data.failed ? 'warning' : 'success');
                        } else if (data.status === 'skipped') {
                            mediaOrganizeConfig.emby_locale_defaults_fixed = true;
                        }
                    } catch (e) {
                        showToast('修复已有 Emby 媒体库语言设置失败: ' + (e.response?.data?.detail || e.message), 'error');
                    }
                }
            }
        };

        const toggleFilenameOnlyMode = () => {
            const mode = mediaOrganizeConfig.organize_parse_mode;

            if (mode === 'filename') {
                mediaOrganizeConfig.organize_parse_mode = '';
                showToast('已关闭纯文件名整理', 'success');
                return;
            }
            if (mode === 'ffprobe') {
                showToast('当前已开启智能ffprobe整理，请先关闭再切换', 'warning');
                return;
            }
            if (mode === 'ffprobe_full') {
                showToast('当前已开启全量ffprobe整理，请先关闭再切换', 'warning');
                return;
            }
            mediaOrganizeConfig.organize_parse_mode = 'filename';
            showToast('已开启纯文件名整理', 'success');
        };

        const toggleFfprobeMode = () => {
            const mode = mediaOrganizeConfig.organize_parse_mode;

            if (mode === 'ffprobe') {
                mediaOrganizeConfig.organize_parse_mode = '';
                showToast('已关闭智能ffprobe整理', 'success');
                return;
            }
            if (mode === 'filename') {
                showToast('当前已开启纯文件名整理，请先关闭再切换', 'warning');
                return;
            }
            if (mode === 'ffprobe_full') {
                showToast('当前已开启全量ffprobe整理，请先关闭再切换', 'warning');
                return;
            }
            mediaOrganizeConfig.organize_parse_mode = 'ffprobe';
            showToast('已开启智能ffprobe整理', 'success');
        };

        const toggleFullFfprobeMode = () => {
            const mode = mediaOrganizeConfig.organize_parse_mode;

            if (mode === 'ffprobe_full') {
                mediaOrganizeConfig.organize_parse_mode = '';
                showToast('已关闭全量ffprobe整理', 'success');
                return;
            }
            if (mode === 'filename') {
                showToast('当前已开启纯文件名整理，请先关闭再切换', 'warning');
                return;
            }
            if (mode === 'ffprobe') {
                showToast('当前已开启智能ffprobe整理，请先关闭再切换', 'warning');
                return;
            }
            mediaOrganizeConfig.organize_parse_mode = 'ffprobe_full';
            showToast('已开启全量ffprobe整理', 'success');
        };

        const toggleWashByEquivalentSize = async (event) => {
            const nextChecked = !!event?.target?.checked;
            if (!nextChecked) {
                mediaOrganizeConfig.wash_by_equivalent_size = false;
                return;
            }
            const input = await showNumberDialog(
                '等效体积洗版容差',
                '请输入容差百分比。填写 2 表示当新文件等效体积大于旧文件的 0.98 倍时，也允许替换。适合在画质接近时优先保留较新的资源。',
                0,
                '例如 2 或 2.5',
                (value) => {
                    const normalized = String(value).trim();
                    const parsed = Number(normalized);
                    if (!normalized || !Number.isFinite(parsed) || parsed < 0 || parsed >= 100) {
                        return '请输入 0 到 100 之间的数字，且不能等于 100';
                    }
                    return '';
                }
            );
            if (input === null) {
                mediaOrganizeConfig.wash_by_equivalent_size = false;
                return;
            }
            mediaOrganizeConfig.wash_tolerance_ratio = Number(String(input).trim());
            mediaOrganizeConfig.wash_by_equivalent_size = true;
        };

        watch(() => mediaOrganizeConfig.emby_local_scrape, val => {
            mediaOrganizeConfig.scrape_enabled = !!val;
        });

        // --- 源目录 115 浏览 ---
        const browseOrganizeSource = async () => {
            if (needs115Setup.value) {
                notify115SetupRequired();
                return;
            }
            orgSourceBrowser.history = [];
            orgSourceBrowser.path = '';
            orgSourceBrowser.opened = true;
            await loadOrgSourceDir('0');
        };

        const loadOrgSourceDir = async (cid) => {
            try {
                const res = await axios.post('/api/media_organize/browse115', {
                    cid: cid,
                    drive_index: 0
                });
                if (res.data.status === 'ok') {
                    orgSourceBrowser.dirs = res.data.dirs || [];
                    orgSourceBrowser.currentCid = cid;
                } else {
                    showToast(res.data.message, 'error');
                    orgSourceBrowser.dirs = [];
                }
            } catch (e) {
                showToast('浏览失败: ' + e.message, 'error');
                orgSourceBrowser.dirs = [];
            }
        };

        const selectOrgSourceDir = async (dir) => {
            orgSourceBrowser.history.push({ cid: orgSourceBrowser.currentCid, path: orgSourceBrowser.path });
            orgSourceBrowser.path = (orgSourceBrowser.path ? orgSourceBrowser.path + '/' : '/') + dir.name;
            await loadOrgSourceDir(dir.cid);
        };

        const orgSourceUp = async () => {
            if (orgSourceBrowser.history.length > 0) {
                const prev = orgSourceBrowser.history.pop();
                orgSourceBrowser.path = prev.path;
                await loadOrgSourceDir(prev.cid);
            }
        };

        const applyOrgSourcePath = () => {
            mediaOrganizeConfig.source_cid = orgSourceBrowser.currentCid;
            mediaOrganizeConfig.source_name = orgSourceBrowser.path || '根目录';
            orgSourceBrowser.dirs = [];
            orgSourceBrowser.path = '';
            orgSourceBrowser.history = [];
            orgSourceBrowser.opened = false;
        };

        // --- 目标目录 115 浏览 ---
        const browseOrganizeTarget = async () => {
            if (needs115Setup.value) {
                notify115SetupRequired();
                return;
            }
            orgTargetBrowser.history = [];
            orgTargetBrowser.path = '';
            orgTargetBrowser.opened = true;
            await loadOrgTargetDir('0');
        };

        const loadOrgTargetDir = async (cid) => {
            try {
                const res = await axios.post('/api/media_organize/browse115', {
                    cid: cid,
                    drive_index: 0
                });
                if (res.data.status === 'ok') {
                    orgTargetBrowser.dirs = res.data.dirs || [];
                    orgTargetBrowser.currentCid = cid;
                } else {
                    showToast(res.data.message, 'error');
                    orgTargetBrowser.dirs = [];
                }
            } catch (e) {
                showToast('浏览失败: ' + e.message, 'error');
                orgTargetBrowser.dirs = [];
            }
        };

        const selectOrgTargetDir = async (dir) => {
            orgTargetBrowser.history.push({ cid: orgTargetBrowser.currentCid, path: orgTargetBrowser.path });
            orgTargetBrowser.path = (orgTargetBrowser.path ? orgTargetBrowser.path + '/' : '/') + dir.name;
            await loadOrgTargetDir(dir.cid);
        };

        const orgTargetUp = async () => {
            if (orgTargetBrowser.history.length > 0) {
                const prev = orgTargetBrowser.history.pop();
                orgTargetBrowser.path = prev.path;
                await loadOrgTargetDir(prev.cid);
            }
        };

        const applyOrgTargetPath = () => {
            mediaOrganizeConfig.target_cid = orgTargetBrowser.currentCid;
            mediaOrganizeConfig.target_name = orgTargetBrowser.path || '根目录';
            orgTargetBrowser.dirs = [];
            orgTargetBrowser.path = '';
            orgTargetBrowser.history = [];
            orgTargetBrowser.opened = false;
        };

        // --- 失败目录 115 浏览 ---
        const browseOrganizeFailed = async () => {
            if (needs115Setup.value) {
                notify115SetupRequired();
                return;
            }
            orgFailedBrowser.history = [];
            orgFailedBrowser.path = '';
            orgFailedBrowser.opened = true;
            await loadOrgFailedDir('0');
        };

        const loadOrgFailedDir = async (cid) => {
            try {
                const res = await axios.post('/api/media_organize/browse115', {
                    cid: cid,
                    drive_index: 0
                });
                if (res.data.status === 'ok') {
                    orgFailedBrowser.dirs = res.data.dirs || [];
                    orgFailedBrowser.currentCid = cid;
                } else {
                    showToast(res.data.message, 'error');
                    orgFailedBrowser.dirs = [];
                }
            } catch (e) {
                showToast('浏览失败: ' + e.message, 'error');
                orgFailedBrowser.dirs = [];
            }
        };

        const selectOrgFailedDir = async (dir) => {
            orgFailedBrowser.history.push({ cid: orgFailedBrowser.currentCid, path: orgFailedBrowser.path });
            orgFailedBrowser.path = (orgFailedBrowser.path ? orgFailedBrowser.path + '/' : '/') + dir.name;
            await loadOrgFailedDir(dir.cid);
        };

        const orgFailedUp = async () => {
            if (orgFailedBrowser.history.length > 0) {
                const prev = orgFailedBrowser.history.pop();
                orgFailedBrowser.path = prev.path;
                await loadOrgFailedDir(prev.cid);
            }
        };

        const applyOrgFailedPath = () => {
            mediaOrganizeConfig.failed_cid = orgFailedBrowser.currentCid;
            mediaOrganizeConfig.failed_name = orgFailedBrowser.path || '根目录';
            orgFailedBrowser.dirs = [];
            orgFailedBrowser.path = '';
            orgFailedBrowser.history = [];
            orgFailedBrowser.opened = false;
        };

        const loadMonitorDir = async (cid = '0', path = '/') => {
            monitorDirBrowser.loading = true;
            try {
                const res = await axios.post('/api/media_organize/browse115', { cid, drive_index: 0 });
                if (res.data?.status !== 'ok') throw new Error(res.data?.message || '读取目录失败');
                monitorDirBrowser.currentCid = String(cid || '0');
                monitorDirBrowser.currentPath = path || '/';
                monitorDirBrowser.dirs = res.data.dirs || [];
            } catch (e) {
                showToast('浏览失败: ' + (e.message || e), 'error');
                monitorDirBrowser.dirs = [];
            } finally {
                monitorDirBrowser.loading = false;
            }
        };

        const openMonitorDirBrowser = () => {
            if (needs115Setup.value) {
                notify115SetupRequired();
                return;
            }
            if (monitorDirBrowser.visible) {
                monitorDirBrowser.visible = false;
                return;
            }
            monitorDirBrowser.visible = true;
            monitorDirBrowser.history.splice(0);
            loadMonitorDir('0', '/');
        };

        const selectMonitorDir = (dir) => {
            monitorDirBrowser.history.push({
                cid: monitorDirBrowser.currentCid,
                path: monitorDirBrowser.currentPath,
            });
            const nextPath = monitorDirBrowser.currentPath === '/' ? `/${dir.name}` : `${monitorDirBrowser.currentPath}/${dir.name}`;
            loadMonitorDir(dir.cid, nextPath);
        };

        const monitorDirUp = () => {
            const prev = monitorDirBrowser.history.pop();
            if (!prev) return;
            loadMonitorDir(prev.cid, prev.path);
        };

        const addCurrentMonitorDir = () => {
            const cid = String(monitorDirBrowser.currentCid || '');
            if (!cid || cid === '0') return showToast('不能选择根目录', 'error');
            if (cid === String(mediaOrganizeConfig.source_cid || '')) return showToast('主整理目录已自动监控，无需重复添加', 'info');
            if (cid === String(mediaOrganizeConfig.target_cid || '')) return showToast('媒体库目录不能作为整理监控目录', 'error');
            if ((mediaOrganizeConfig.monitor_dirs || []).some(item => String(item.cid) === cid)) return showToast('该目录已添加', 'info');
            const path = monitorDirBrowser.currentPath || cid;
            const name = path.split('/').filter(Boolean).pop() || path;
            mediaOrganizeConfig.monitor_dirs.push({ cid, name, path, enabled: true });
            monitorDirBrowser.visible = false;
            showToast('已添加监控目录', 'success');
        };

        const removeMonitorDir = (cid) => {
            const idx = (mediaOrganizeConfig.monitor_dirs || []).findIndex(item => String(item.cid) === String(cid));
            if (idx >= 0) mediaOrganizeConfig.monitor_dirs.splice(idx, 1);
        };

        const saveMonitorDirs = async () => {
            if (needs115Setup.value) {
                notify115SetupRequired();
                return false;
            }
            monitorDirsSaving.value = true;
            try {
                const payload = {
                    monitor_dirs: (mediaOrganizeConfig.monitor_dirs || []).map(item => ({
                        cid: String(item.cid || ''),
                        name: item.name || '',
                        path: item.path || item.name || '',
                        enabled: item.enabled !== false,
                    })),
                };
                const res = await axios.post('/api/media_organize/monitor_dirs', payload);
                mediaOrganizeConfig.monitor_dirs = res.data?.monitor_dirs || payload.monitor_dirs;
                showToast('监控目录已保存', 'success');
                return true;
            } catch (e) {
                showToast('保存失败: ' + (e.response?.data?.detail || e.message), 'error');
                return false;
            } finally {
                monitorDirsSaving.value = false;
            }
        };

        // 执行整理
        const runOrganize = async () => {
            if (needs115Setup.value) {
                notify115SetupRequired();
                return;
            }
            if (!mediaOrganizeConfig.source_cid || mediaOrganizeConfig.source_cid === '0') { showToast('请先配置源目录', 'error'); return; }
            if (!mediaOrganizeConfig.target_cid || mediaOrganizeConfig.target_cid === '0') { showToast('请先配置目标目录', 'error'); return; }

            organizeLoading.value = true;
            organizeResult.value = null;
            organizeProgress.percent = 0;
            organizeProgress.status_text = '启动中...';
            organizeProgress.detail = null;
            try {
                const saved = await saveMediaOrganizeConfig();
                if (!saved) {
                    organizeLoading.value = false;
                    return;
                }
                const res = await axios.post('/api/media_organize/organize', {
                    media_type: organizeForm.media_type,
                    is_bluray: organizeForm.is_bluray,
                    overwrite: organizeForm.overwrite,
                    drive_index: 0,
                });
                if (res.data.status === 'ok') {
                    organizeRunId.value = res.data.run_id;
                    localStorage.setItem(ORGANIZE_RUN_ID_STORAGE_KEY, organizeRunId.value);
                    showToast('整理任务已启动', 'success');
                    startOrganizePolling();
                } else {
                    showToast(res.data.message || '整理启动失败', 'error');
                    organizeLoading.value = false;
                }
            } catch (e) {
                organizeResult.value = { status: 'error', message: e.response?.data?.detail || e.message };
                showToast('整理请求失败', 'error');
                organizeLoading.value = false;
            }
        };

        const runCollectionBackfill = async () => {
            organizeLoading.value = true;
            organizeResult.value = null;
            organizeProgress.percent = 0;
            organizeProgress.status_text = '启动中...';
            organizeProgress.detail = { task: 'collection_backfill', total: 0, processed: 0, success: 0, failed: 0, skipped: 0 };
            try {
                const res = await axios.post('/api/media_organize/collections/backfill');
                if (res.data.status === 'ok') {
                    organizeRunId.value = res.data.run_id;
                    localStorage.setItem(ORGANIZE_RUN_ID_STORAGE_KEY, organizeRunId.value);
                    showToast('电影合集补齐任务已启动', 'success');
                    startOrganizePolling();
                } else {
                    showToast(res.data.message || '电影合集补齐启动失败', res.data.status === 'busy' ? 'warning' : 'error');
                    organizeLoading.value = false;
                }
            } catch (e) {
                organizeResult.value = { status: 'error', message: e.response?.data?.detail || e.message };
                showToast('电影合集补齐请求失败', 'error');
                organizeLoading.value = false;
            }
        };

        const requestStopOrganizeRun = async (runId) => {
            if (!runId) return { status: 'not_found', message: '任务不存在或已结束' };
            const res = await axios.post('/api/stop_task', { run_id: runId });
            return res.data || {};
        };

        const cancelOrganize = async () => {
            try {
                let runId = organizeRunId.value;
                if (!runId) {
                    const progressRes = await axios.get('/api/progress');
                    syncOrganizeTaskFromTaskMap(progressRes.data || {}, { adoptRunning: true });
                    runId = organizeRunId.value;
                }
                if (!runId) {
                    showToast('未找到正在运行的整理任务', 'warning');
                    return;
                }

                let stopResult = await requestStopOrganizeRun(runId);
                if (stopResult.status !== 'ok') {
                    const oldRunId = runId;
                    const progressRes = await axios.get('/api/progress');
                    const task = syncOrganizeTaskFromTaskMap(progressRes.data || {}, { adoptRunning: true });
                    runId = organizeRunId.value;
                    if (task && runId && runId !== oldRunId) {
                        stopResult = await requestStopOrganizeRun(runId);
                    }
                }

                if (stopResult.status === 'ok') {
                    organizeProgress.status_text = '正在取消...';
                    showToast('已发送取消请求', 'info');
                    startOrganizePolling();
                } else {
                    showToast(stopResult.message || '未找到正在运行的整理任务', 'warning');
                }
            } catch (e) {
                showToast('取消失败: ' + (e.response?.data?.detail || e.message), 'error');
            }
        };

        const startOrganizePolling = () => {
            if (!organizeRunId.value) return;
            organizeLoading.value = true;
            stopOrganizePolling();
            organizePollTimer = setInterval(async () => {
                try {
                    const res = await axios.get('/api/progress');
                    const tasks = res.data || {};
                    const task = syncOrganizeTaskFromTaskMap(tasks, { adoptRunning: true });
                    if (!task) return;

                    if (isOrganizeTerminalStatus(task.status)) {
                        organizeLoading.value = false;
                        const detail = task.detail || {};
                        const label = task.status === 'finished'
                            ? '完成'
                            : (task.status === 'stopped' ? '已取消' : (task.status === 'interrupted' ? '已中断' : '异常'));
                        const resultStatus = task.status === 'finished'
                            ? 'success'
                            : (task.status === 'error' ? 'error' : 'warning');
                        organizeResult.value = {
                            status: resultStatus,
                            message: buildOrganizeResultMessage(task, label),
                            detail: detail,
                        };
                        showToast(`整理${label}`, resultStatus);
                        stopOrganizePolling();
                        const finishedRunId = organizeRunId.value;
                        organizeRunId.value = null;
                        localStorage.removeItem(ORGANIZE_RUN_ID_STORAGE_KEY);
                        if (finishedRunId && task.status !== 'interrupted') {
                            setTimeout(() => axios.post('/api/clear_task_progress', { run_id: finishedRunId }), 3000);
                        }
                    }
                } catch (e) { /* ignore */ }
            }, 2000);
        };

        const stopOrganizePolling = () => {
            if (organizePollTimer) {
                clearInterval(organizePollTimer);
                organizePollTimer = null;
            }
        };

        // tab 切换时加载配置
        watch(tab, (v) => {
            if (v === 'media_organize') {
                fetchMediaOrganizeConfig();
                restoreRunningOrganizeTask();
            }
            if (v === 'organize_monitor_dirs') fetchMediaOrganizeConfig();
            if (v === 'media_organize_rules') fetchCategoryRules();
        });

    return {
        mediaOrganizeConfig,
        organizeForm,
        organizeLoading,
        organizeResult,
        organizeProgress,
        runOrganize,
        runCollectionBackfill,
        cancelOrganize,
        identifyTest,
        openIdentifyTest,
        closeIdentifyTest,
        runIdentifyTest,
        categoryRulesEditor,
        categoryRulesSaving,
        ruleListEl,
        subClassify,
        subClassifyVars,
        subClassifyVarExamples,
        subClassifyBaseExamples,
        subClassifyPreviewSegments,
        subClassifyToggleLevel,
        embyLibCount,
        embyLibLevelOptions,
        embyCacheRefreshing,
        refreshEmbyCache,
        onLevelDragStart,
        onLevelDragOver,
        onLevelDrop,
        onLevelDragEnd,
        fetchCategoryRules,
        saveCategoryRules,
        saveSubClassify,
        addRule,
        removeRule,
        addCondition,
        removeCondition,
        resetCategoryRules,
        onRuleDragStart,
        onRuleDragOver,
        onRuleDrop,
        onRuleDragEnd,
        orgSourceBrowser,
        orgTargetBrowser,
        orgFailedBrowser,
        monitorDirBrowser,
        monitorDirsSaving,
        fetchMediaOrganizeConfig,
        saveMediaOrganizeConfig,
        saveMonitorDirs,
        restoreRunningOrganizeTask,
        toggleAutoSyncStrm,
        toggleEmbyScrapers,
        toggleFilenameOnlyMode,
        toggleFfprobeMode,
        toggleFullFfprobeMode,
        toggleWashByEquivalentSize,
        browseOrganizeSource,
        selectOrgSourceDir,
        orgSourceUp,
        applyOrgSourcePath,
        browseOrganizeTarget,
        selectOrgTargetDir,
        orgTargetUp,
        applyOrgTargetPath,
        browseOrganizeFailed,
        selectOrgFailedDir,
        orgFailedUp,
        applyOrgFailedPath,
        openMonitorDirBrowser,
        selectMonitorDir,
        monitorDirUp,
        addCurrentMonitorDir,
        removeMonitorDir,
        movieFormatRef,
        movieFolderFormatRef,
        tvFolderFormatRef,
        tvEpisodeFormatRef,
        movieFolderFormatDisplay,
        tvFolderFormatDisplay,
        movieFormatDisplay,
        tvEpisodeFormatDisplay,
        moviePreviewName,
        movieFolderPreviewName,
        tvFolderPreviewName,
        tvEpisodePreviewName,
        insertToken,
        insertRenameTokenForGroup,
        insertRenameLiteralForGroup,
        resetMovieFormat,
        resetTvFormat,
        setActiveRenameTemplate,
        activeRenameTemplateType,
        renameTemplateSegments,
        renameTemplateLiteralLabel,
        renameTokenClass,
        updateRenameTemplateSegment,
        removeRenameTemplateSegment,
        insertRenameLiteral,
        onRenameTemplateDragStart,
        onRenameTemplateDragOver,
        onRenameTemplateDrop,
        onRenameTemplateDragEnd,
    };
}
