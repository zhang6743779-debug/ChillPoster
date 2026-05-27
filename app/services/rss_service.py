import os
import re
import time
import json
import threading
from apscheduler.triggers.cron import CronTrigger

from core.importer import UniversalImporter
from core.emby_client import EmbyClient
from app.routers.config_302 import get_emby_config_by_index_sync
from core.linker import HardLinkManager
from core.configs import RSS_TASKS_FILE, RSS_CONFIG_FILE, CONFIG_FILE
from core.logger import logger

from app.dependencies import (
    RSS_JOB_QUEUE,
    update_task_progress
)

RUNTIME_STATE_KEYS = {"last_entries", "entry_tmdb_map", "last_sync_at"}


def _notify_emby_path_updates(client: EmbyClient, created_paths: list[str], deleted_paths: list[str], log_prefix: str):
    created_paths = [path for path in created_paths or [] if path]
    deleted_paths = [path for path in deleted_paths or [] if path]
    if created_paths:
        if client.notify_media_updates(created_paths, update_type="Created"):
            logger.info(f"{log_prefix} 已按路径通知 Emby 更新: 新增/变更 {len(created_paths)} 个目录")
        else:
            logger.warning(f"{log_prefix} 按路径通知 Emby 更新失败: 新增/变更 {len(created_paths)} 个目录")
    if deleted_paths:
        if client.notify_media_updates(deleted_paths, update_type="Deleted"):
            logger.info(f"{log_prefix} 已按路径通知 Emby 删除: {len(deleted_paths)} 个目录")
        else:
            logger.warning(f"{log_prefix} 按路径通知 Emby 删除失败: {len(deleted_paths)} 个目录")


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _save_rss_tasks(tasks):
    tmp_path = f"{RSS_TASKS_FILE}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(tasks, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, RSS_TASKS_FILE)


def _update_task_runtime_state(task_id, last_entries, entry_tmdb_map):
    tasks = _read_json(RSS_TASKS_FILE, [])
    for task in tasks:
        if task.get('id') == task_id:
            task['last_entries'] = sorted(list(last_entries))
            task['entry_tmdb_map'] = entry_tmdb_map
            task['last_sync_at'] = time.time()
            break
    _save_rss_tasks(tasks)


def _normalize_title(title):
    if not title:
        return ""
    return re.sub(r"[\s:：·\-*'!,?.。]+", '', str(title)).lower()


def _normalize_year(year):
    y = str(year or '').strip()
    if re.fullmatch(r"(19\d{2}|20\d{2})", y):
        return y
    return 'unknown'


def _build_entry_key(item):
    title_key = _normalize_title(item.get('title')) or 'unknown'
    year_key = _normalize_year(item.get('year'))
    return f"{title_key}|{year_key}"


def _collect_current_entries(raw_items):
    entries = set()
    entry_to_raw = {}
    for item in raw_items:
        key = _build_entry_key(item)
        entries.add(key)
        if key not in entry_to_raw:
            entry_to_raw[key] = item
    return entries, entry_to_raw


def _recognize_entries(importer, entry_keys, entry_to_raw):
    recognized_map = {}
    for key in entry_keys:
        raw = entry_to_raw.get(key)
        if not raw:
            continue
        matched = _recognize_raw_entry(importer, raw)
        if matched:
            recognized_map[key] = matched
    return recognized_map


def _recognize_raw_entry(importer, raw):
    tmdb_id = str((raw or {}).get('tmdb_id') or '').strip()
    if tmdb_id:
        return [{
            'tmdb_id': tmdb_id,
            'type': (raw or {}).get('type') or 'Movie',
            'title': (raw or {}).get('title') or '',
            'year': (raw or {}).get('year') or '',
        }]
    return importer._process_items_with_precision([raw])


def _flatten_items(entry_tmdb_map, active_entry_keys):
    items = []
    seen = set()
    for key in active_entry_keys:
        for item in entry_tmdb_map.get(key, []):
            dedupe_key = f"{item.get('type')}-{item.get('tmdb_id')}-{item.get('season')}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append(item)
    return items


def _normalize_tmdb_id(value) -> str:
    return str(value or "").strip()


def _normalize_media_type(value) -> str:
    text = str(value or "").strip()
    if text in {"Series", "Episode", "Season", "tv", "tvshows", "series"}:
        return "Series"
    return "Movie"


def _task_accepts_media_type(task: dict, media_type: str) -> bool:
    content_type = str(task.get('content_type') or 'movies')
    is_series_task = content_type in {'tv', 'tvshows', 'series', 'Season', 'Episode'}
    return is_series_task if media_type == "Series" else not is_series_task


def _filter_items_by_tmdb(items: list[dict], tmdb_id: str, media_type: str) -> list[dict]:
    target_tmdb = _normalize_tmdb_id(tmdb_id)
    target_type = _normalize_media_type(media_type)
    matches = []
    seen = set()
    for item in items or []:
        if _normalize_tmdb_id(item.get('tmdb_id')) != target_tmdb:
            continue
        if _normalize_media_type(item.get('type')) != target_type:
            continue
        key = f"{item.get('type')}-{item.get('tmdb_id')}-{item.get('season')}"
        if key in seen:
            continue
        seen.add(key)
        matches.append(item)
    return matches


def _find_rss_matches_for_tmdb(task: dict, importer: UniversalImporter, tmdb_id: str, media_type: str):
    raw_items = importer._get_from_rss(
        task['rss_url'],
        default_type='Series' if media_type == "Series" else 'Movie',
    )
    current_entries, entry_to_raw = _collect_current_entries(raw_items)
    entry_tmdb_map = task.get('entry_tmdb_map') or {}
    if not isinstance(entry_tmdb_map, dict):
        entry_tmdb_map = {}

    stale_entries = set(entry_tmdb_map.keys()) - current_entries
    for entry_key in stale_entries:
        entry_tmdb_map.pop(entry_key, None)

    mapped_items = _flatten_items(entry_tmdb_map, sorted(list(current_entries)))
    matches = _filter_items_by_tmdb(mapped_items, tmdb_id, media_type)

    recognized_changed = bool(stale_entries)
    if not matches:
        missing_entries = [key for key in sorted(list(current_entries)) if key not in entry_tmdb_map]
        for entry_key in missing_entries:
            raw = entry_to_raw.get(entry_key)
            if not raw:
                continue
            matched = _recognize_raw_entry(importer, raw)
            if matched:
                entry_tmdb_map[entry_key] = matched
                recognized_changed = True
                matches = _filter_items_by_tmdb(matched, tmdb_id, media_type)
                if matches:
                    break

    return matches, current_entries, entry_tmdb_map, recognized_changed


def sync_webhook_rss_item(tmdb_id: str, media_type: str, server_idx: int | None = None) -> dict:
    tmdb_id = _normalize_tmdb_id(tmdb_id)
    media_type = _normalize_media_type(media_type)
    if not tmdb_id:
        return {"matched": 0, "synced": 0, "notified": 0}

    tasks = _read_json(RSS_TASKS_FILE, [])
    config = _read_json(RSS_CONFIG_FILE, {})
    link_root_base = config.get('link_root')
    settings = _read_json(CONFIG_FILE, {})
    tmdb_key = settings.get('tmdb_key', '')
    proxy_url = settings.get('proxy_url', '')

    if not isinstance(tasks, list) or not link_root_base:
        return {"matched": 0, "synced": 0, "notified": 0}

    matched_count = 0
    synced_count = 0
    notified_count = 0
    state_changed = False

    for task in tasks:
        if not task.get('enabled', True):
            continue
        if server_idx is not None and int(task.get('target_server_idx', 0) or 0) != int(server_idx):
            continue
        if not _task_accepts_media_type(task, media_type):
            continue

        try:
            importer = UniversalImporter(tmdb_api_key=tmdb_key, proxy_url=proxy_url)
            matches, current_entries, entry_tmdb_map, recognized_changed = _find_rss_matches_for_tmdb(
                task,
                importer,
                tmdb_id,
                media_type,
            )
            if recognized_changed:
                task['last_entries'] = sorted(list(current_entries))
                task['entry_tmdb_map'] = entry_tmdb_map
                task['last_sync_at'] = time.time()
                state_changed = True

            if not matches:
                continue

            server = get_emby_config_by_index_sync(task.get('target_server_idx', 0))
            if not server or not server.get('enabled', True):
                continue

            matched_count += len(matches)
            current_link_root = os.path.join(link_root_base, task['name'])
            client = EmbyClient(server['url'], server['key'], server.get('public_host'))
            try:
                linker = HardLinkManager(current_link_root)
                synced = linker.sync_items(matches, client, cleanup_stale=False)
                synced_count += synced
                if synced > 0:
                    target_lib_id, _ = client.ensure_library_exists(
                        name=task['name'],
                        path=current_link_root,
                        collection_type=task.get('content_type', 'movies'),
                        refresh_on_path_add=False,
                    )
                    if target_lib_id:
                        paths = getattr(linker, "last_synced_dirs", [])
                        _notify_emby_path_updates(client, paths, [], "[RSS Webhook]")
                        notified_count += len(paths or [])
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[RSS Webhook] 增量同步失败: task={task.get('name') or task.get('id')} tmdb={tmdb_id} error={e}")

    if state_changed:
        _save_rss_tasks(tasks)

    if matched_count:
        logger.info(f"[RSS Webhook] 命中 RSS 条目并增量同步: TMDB={tmdb_id} type={media_type} matched={matched_count} synced={synced_count}")
    return {"matched": matched_count, "synced": synced_count, "notified": notified_count}


# ==========================================
# 1. 核心业务逻辑
# ==========================================
def execute_rss_job(task_id):
    """
    RSS 自动化榜单同步任务逻辑
    """
    logger.info(f"[RSS] 开始执行任务: {task_id}")
    run_id = f"rss_run_{task_id}_{int(time.time())}"

    try:
        tasks = _read_json(RSS_TASKS_FILE, [])
        task = next((t for t in tasks if t.get('id') == task_id), None)
        if not task:
            return

        update_task_progress(run_id, f"初始化: {task['name']}", 0, "running")

        config = _read_json(RSS_CONFIG_FILE, {})
        link_root_base = config.get('link_root')

        if not link_root_base:
            update_task_progress(run_id, "错误: 硬链根目录为空", 100, "error")
            return

        target_dir_name = task['name']
        current_link_root = os.path.join(link_root_base, target_dir_name)

        settings = _read_json(CONFIG_FILE, {})
        tmdb_key = settings.get('tmdb_key', '')
        proxy_url = settings.get('proxy_url', '')

        server = get_emby_config_by_index_sync(task.get('target_server_idx', 0))
        if not server or not server.get('enabled', True):
            update_task_progress(run_id, "错误: 未配置目标 Emby 服务器", 100, "error")
            return

        if not tmdb_key:
            logger.warning("[RSS] TMDb Key 未配置，匹配准确率将受严重影响")

        importer = UniversalImporter(tmdb_api_key=tmdb_key, proxy_url=proxy_url)

        rss_content_type = task.get('content_type', 'movies')
        importer_type = 'Movie'
        if rss_content_type in ['tv', 'tvshows', 'series', 'Season', 'Episode']:
            importer_type = 'Series'

        update_task_progress(run_id, "抓取 RSS 条目...", 10, "running")
        raw_items = importer._get_from_rss(task['rss_url'], default_type=importer_type)

        current_entries, entry_to_raw = _collect_current_entries(raw_items)

        last_entries = set(task.get('last_entries') or [])
        entry_tmdb_map = task.get('entry_tmdb_map') or {}
        if not isinstance(entry_tmdb_map, dict):
            entry_tmdb_map = {}

        first_run = len(last_entries) == 0 and len(entry_tmdb_map) == 0

        added_entries = set(current_entries) if first_run else (current_entries - last_entries)
        removed_entries = set() if first_run else (last_entries - current_entries)
        missing_mapped_entries = {k for k in current_entries if k not in entry_tmdb_map}

        to_recognize_entries = sorted(list(added_entries | missing_mapped_entries))

        update_task_progress(
            run_id,
            f"差分完成: 新增{len(added_entries)} 减少{len(removed_entries)} 识别{len(to_recognize_entries)}",
            25,
            "running"
        )

        if to_recognize_entries:
            recognized_map = _recognize_entries(importer, to_recognize_entries, entry_to_raw)
            for entry_key, matched_items in recognized_map.items():
                entry_tmdb_map[entry_key] = matched_items

        for entry_key in removed_entries:
            entry_tmdb_map.pop(entry_key, None)

        target_items = _flatten_items(entry_tmdb_map, sorted(list(current_entries)))
        logger.info(
            f"[RSS] 条目差分: 当前={len(current_entries)} 新增={len(added_entries)} "
            f"减少={len(removed_entries)} 目标项={len(target_items)}"
        )

        if not target_items:
            logger.info("[RSS] 当前无可同步目标，任务结束")
            _update_task_runtime_state(task_id, current_entries, entry_tmdb_map)
            update_task_progress(run_id, "完成 (无可同步目标)", 100, "finished")
            return

        update_task_progress(run_id, "执行硬链同步...", 40, "running")

        client = EmbyClient(server['url'], server['key'], server.get('public_host'))
        linker = HardLinkManager(current_link_root)
        success_count = linker.sync_items(target_items, client)

        logger.info(f"[RSS] 同步完成: 成功链接 {success_count} 项")

        update_task_progress(run_id, "通知 Emby 更新媒体目录...", 90, "running")

        c_type = task.get('content_type', 'movies')
        target_lib_id, _ = client.ensure_library_exists(
            name=target_dir_name,
            path=current_link_root,
            collection_type=c_type,
            refresh_on_path_add=False,
        )

        if target_lib_id:
            _notify_emby_path_updates(
                client,
                getattr(linker, "last_synced_dirs", []),
                getattr(linker, "last_removed_dirs", []),
                "[RSS]",
            )
        else:
            logger.warning("[RSS] 无法获取/创建库 ID，跳过 Emby 路径通知")

        _update_task_runtime_state(task_id, current_entries, entry_tmdb_map)
        update_task_progress(run_id, f"完成 (同步 {success_count} 项)", 100, "finished")

    except Exception as e:
        logger.error(f"[RSS Error] {e}")
        update_task_progress(run_id, f"错误: {str(e)}", 100, "error")


def rss_worker_loop():
    """
    RSS 队列消费者线程
    """
    logger.trace("[启动] RSS 串行处理器已启动")
    while True:
        try:
            task_id = RSS_JOB_QUEUE.get()
            try:
                execute_rss_job(task_id)
            except Exception as e:
                logger.error(f"[RSS Worker Error] 任务执行异常: {e}")
            RSS_JOB_QUEUE.task_done()
        except Exception as e:
            logger.error(f"[RSS Queue Error] 队列监控异常: {e}")
            time.sleep(1)


# ==========================================
# 2. 定义 RssService 类
# ==========================================
class RssService:
    def __init__(self):
        # 在初始化时启动消费者线程
        self.thread = threading.Thread(target=rss_worker_loop, daemon=True)
        self.thread.start()

    def load_active_jobs(self):
        """加载并调度所有启用的 RSS 任务"""
        if not os.path.exists(RSS_TASKS_FILE):
            return

        try:
            active_count = 0
            with open(RSS_TASKS_FILE, 'r', encoding='utf-8') as f:
                tasks = json.load(f)

            for task in tasks:
                if task.get('enabled', True):
                    self.add_rss_job(task)
                    active_count += 1

            logger.trace(f"[RSS] 已恢复 {active_count} 个订阅监控")
        except Exception as e:
            logger.error(f"[RSS] 加载任务列表失败: {e}")

    def add_rss_job(self, task):
        """将任务添加到调度器 (复用 task_service 的调度器)"""
        from app.services.task_service import task_service_instance

        def job_wrapper():
            logger.debug(f"[RSS Scheduler] 触发订阅检查: {task['name']}")
            RSS_JOB_QUEUE.put(task['id'])

        try:
            task_service_instance.scheduler.add_job(
                job_wrapper,
                CronTrigger.from_crontab(task['cron']),
                id=f"rss_{task['id']}",
                replace_existing=True
            )
        except Exception as e:
            logger.error(f"[RSS] 调度添加失败 {task['name']}: {e}")

    def remove_rss_job(self, task_id):
        from app.services.task_service import task_service_instance
        try:
            task_service_instance.scheduler.remove_job(f"rss_{task_id}")
        except Exception:
            pass


# ==========================================
# 3. 实例化 (必须在类定义之后)
# ==========================================
rss_service_instance = RssService()
