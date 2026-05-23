import json
import os
import queue
import re
import threading
import time
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.dependencies import update_task_progress
from core.configs import CONFIG_DIR
from core.emby_client import EmbyClient
from core.importer import UniversalImporter
from core.linker import HardLinkManager
from core.logger import logger


REAL_LIBRARY_CONFIG_FILE = os.path.join(CONFIG_DIR, "real_library.json")
REAL_LIBRARY_TASKS_FILE = os.path.join(CONFIG_DIR, "real_library_tasks.json")
REAL_LIBRARY_JOB_QUEUE: queue.Queue[str] = queue.Queue()
RUNTIME_STATE_KEYS = {"last_entries", "entry_tmdb_map", "last_sync_at"}


def _read_json(path: str, default: Any):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data: Any):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp_path, path)


def _default_config() -> dict:
    return {
        "enabled": True,
        "emby_name": "独立真实库",
        "emby_url": "",
        "emby_key": "",
        "emby_public_host": "",
        "source_root": "",
        "link_root": "",
        "tmdb_key": "",
        "proxy_url": "",
    }


def load_config() -> dict:
    data = _read_json(REAL_LIBRARY_CONFIG_FILE, {})
    cfg = _default_config()
    if isinstance(data, dict):
        cfg.update(data)
    return cfg


def save_config(data: dict) -> dict:
    cfg = _default_config()
    if isinstance(data, dict):
        cfg.update(data)
    for key in ("emby_name", "emby_url", "emby_key", "emby_public_host", "source_root", "link_root", "tmdb_key", "proxy_url"):
        cfg[key] = str(cfg.get(key) or "").strip()
    cfg["enabled"] = bool(cfg.get("enabled", True))
    _write_json(REAL_LIBRARY_CONFIG_FILE, cfg)
    return cfg


def load_tasks() -> list[dict]:
    tasks = _read_json(REAL_LIBRARY_TASKS_FILE, [])
    return tasks if isinstance(tasks, list) else []


def save_tasks(tasks: list[dict]):
    _write_json(REAL_LIBRARY_TASKS_FILE, tasks)


def _normalize_title(title):
    if not title:
        return ""
    return re.sub(r"[\s:：·\-*'!,?.。]+", "", str(title)).lower()


def _normalize_year(year):
    y = str(year or "").strip()
    if re.fullmatch(r"(19\d{2}|20\d{2})", y):
        return y
    return "unknown"


def _build_entry_key(item):
    title_key = _normalize_title(item.get("title")) or "unknown"
    year_key = _normalize_year(item.get("year"))
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
        matched = importer._process_items_with_precision([raw])
        if matched:
            recognized_map[key] = matched
    return recognized_map


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


def _update_task_runtime_state(task_id, last_entries, entry_tmdb_map):
    tasks = load_tasks()
    for task in tasks:
        if task.get("id") == task_id:
            task["last_entries"] = sorted(list(last_entries))
            task["entry_tmdb_map"] = entry_tmdb_map
            task["last_sync_at"] = time.time()
            break
    save_tasks(tasks)


def _build_emby_client(config: dict) -> EmbyClient | None:
    url = str(config.get("emby_url") or "").strip()
    key = str(config.get("emby_key") or "").strip()
    if not url or not key:
        return None
    return EmbyClient(url, key, str(config.get("emby_public_host") or "").strip() or None)


def test_emby_connection(config: dict) -> dict:
    client = _build_emby_client(config)
    if not client:
        return {"status": "error", "message": "请填写 Emby 地址和 API Key"}
    try:
        libraries = client.get_libraries()
        return {
            "status": "success",
            "message": f"连接成功，读取到 {len(libraries or [])} 个媒体库",
            "libraries": libraries or [],
        }
    except Exception as e:
        return {"status": "error", "message": f"连接失败: {e}"}
    finally:
        try:
            client.close()
        except Exception:
            pass


def validate_paths(config: dict) -> dict:
    source_root = str(config.get("source_root") or "").strip()
    link_root = str(config.get("link_root") or "").strip()
    checks = {
        "source_exists": bool(source_root and os.path.exists(source_root)),
        "link_parent_exists": bool(link_root and os.path.exists(os.path.dirname(link_root) or link_root)),
        "link_exists": bool(link_root and os.path.exists(link_root)),
        "same_filesystem": False,
    }
    try:
        if source_root and link_root and os.path.exists(source_root):
            link_check_path = link_root if os.path.exists(link_root) else (os.path.dirname(link_root) or link_root)
            if os.path.exists(link_check_path):
                checks["same_filesystem"] = os.stat(source_root).st_dev == os.stat(link_check_path).st_dev
    except Exception:
        checks["same_filesystem"] = False
    status = "success" if checks["source_exists"] and checks["link_parent_exists"] and checks["same_filesystem"] else "warning"
    return {"status": status, "checks": checks}


def execute_real_library_job(task_id: str):
    logger.info(f"[RealLibrary] 开始执行任务: {task_id}")
    run_id = f"real_library_run_{task_id}_{int(time.time())}"

    try:
        tasks = load_tasks()
        task = next((t for t in tasks if t.get("id") == task_id), None)
        if not task:
            return

        config = load_config()
        update_task_progress(run_id, f"真实库: {task.get('name', task_id)}", 0, "running")

        if not config.get("enabled", True):
            update_task_progress(run_id, "真实库已停用", 100, "stopped")
            return

        link_root_base = str(config.get("link_root") or "").strip()
        if not link_root_base:
            update_task_progress(run_id, "错误: 真实库输出路径为空", 100, "error")
            return

        client = _build_emby_client(config)
        if not client:
            update_task_progress(run_id, "错误: Emby 连接未配置", 100, "error")
            return

        current_link_root = os.path.join(link_root_base, task["name"])
        tmdb_key = str(config.get("tmdb_key") or "").strip()
        proxy_url = str(config.get("proxy_url") or "").strip()
        if not tmdb_key:
            logger.warning("[RealLibrary] TMDb Key 未配置，RSS 条目匹配准确率会下降")

        importer = UniversalImporter(tmdb_api_key=tmdb_key, proxy_url=proxy_url)
        rss_content_type = task.get("content_type", "movies")
        importer_type = "Series" if rss_content_type in ["tv", "tvshows", "series", "Season", "Episode"] else "Movie"

        update_task_progress(run_id, "抓取 RSS 条目", 10, "running")
        raw_items = importer._get_from_rss(task["rss_url"], default_type=importer_type)
        current_entries, entry_to_raw = _collect_current_entries(raw_items)

        last_entries = set(task.get("last_entries") or [])
        entry_tmdb_map = task.get("entry_tmdb_map") or {}
        if not isinstance(entry_tmdb_map, dict):
            entry_tmdb_map = {}

        first_run = len(last_entries) == 0 and len(entry_tmdb_map) == 0
        added_entries = set(current_entries) if first_run else (current_entries - last_entries)
        removed_entries = set() if first_run else (last_entries - current_entries)
        missing_mapped_entries = {k for k in current_entries if k not in entry_tmdb_map}
        to_recognize_entries = sorted(list(added_entries | missing_mapped_entries))

        update_task_progress(
            run_id,
            f"差分完成: 新增 {len(added_entries)}，移除 {len(removed_entries)}，待识别 {len(to_recognize_entries)}",
            25,
            "running",
        )

        if to_recognize_entries:
            recognized_map = _recognize_entries(importer, to_recognize_entries, entry_to_raw)
            for entry_key, matched_items in recognized_map.items():
                entry_tmdb_map[entry_key] = matched_items

        for entry_key in removed_entries:
            entry_tmdb_map.pop(entry_key, None)

        target_items = _flatten_items(entry_tmdb_map, sorted(list(current_entries)))
        logger.info(
            f"[RealLibrary] 条目差分: 当前={len(current_entries)} 新增={len(added_entries)} "
            f"移除={len(removed_entries)} 目标项={len(target_items)}"
        )

        if not target_items:
            _update_task_runtime_state(task_id, current_entries, entry_tmdb_map)
            update_task_progress(run_id, "完成: 无可同步目标", 100, "finished")
            return

        update_task_progress(run_id, "执行硬链接同步", 45, "running")
        linker = HardLinkManager(current_link_root)
        success_count = linker.sync_items(target_items, client)

        update_task_progress(run_id, "刷新 Emby 媒体库", 90, "running")
        target_lib_id, _ = client.ensure_library_exists(
            name=task["name"],
            path=current_link_root,
            collection_type=task.get("content_type", "movies"),
        )
        if target_lib_id:
            client.refresh_library(target_lib_id)
        else:
            logger.warning("[RealLibrary] 无法获取或创建 Emby 媒体库，跳过刷新")

        _update_task_runtime_state(task_id, current_entries, entry_tmdb_map)
        update_task_progress(run_id, f"完成: 已同步 {success_count} 项", 100, "finished")
    except Exception as e:
        logger.error(f"[RealLibrary] 任务失败: {e}", exc_info=True)
        update_task_progress(run_id, f"错误: {e}", 100, "error")
    finally:
        try:
            client.close()
        except Exception:
            pass


def real_library_worker_loop():
    logger.trace("[RealLibrary] 队列处理器已启动")
    while True:
        try:
            task_id = REAL_LIBRARY_JOB_QUEUE.get()
            try:
                execute_real_library_job(task_id)
            except Exception as e:
                logger.error(f"[RealLibrary] 队列任务异常: {e}", exc_info=True)
            finally:
                REAL_LIBRARY_JOB_QUEUE.task_done()
        except Exception as e:
            logger.error(f"[RealLibrary] 队列监控异常: {e}")


class RealLibraryService:
    def __init__(self):
        self.thread = threading.Thread(target=real_library_worker_loop, daemon=True)
        self.thread.start()

    def enqueue(self, task_id: str):
        REAL_LIBRARY_JOB_QUEUE.put(task_id)

    def load_active_jobs(self):
        tasks = load_tasks()
        active_count = 0
        for task in tasks:
            if task.get("enabled", True):
                self.add_job(task)
                active_count += 1
        logger.trace(f"[RealLibrary] 已恢复 {active_count} 个独立真实库任务")

    def add_job(self, task: dict):
        from app.services.task_service import task_service_instance

        task_id = task.get("id")
        if not task_id:
            return

        def job_wrapper():
            logger.debug(f"[RealLibrary] 触发定时任务: {task.get('name', task_id)}")
            REAL_LIBRARY_JOB_QUEUE.put(task_id)

        try:
            task_service_instance.scheduler.add_job(
                job_wrapper,
                CronTrigger.from_crontab(task["cron"]),
                id=f"real_library_{task_id}",
                name=f"真实库: {task.get('name', task_id)}",
                replace_existing=True,
            )
        except Exception as e:
            logger.error(f"[RealLibrary] 调度添加失败 {task.get('name', task_id)}: {e}")

    def remove_job(self, task_id: str):
        from app.services.task_service import task_service_instance

        try:
            task_service_instance.scheduler.remove_job(f"real_library_{task_id}")
        except Exception:
            pass


real_library_service_instance = RealLibraryService()
