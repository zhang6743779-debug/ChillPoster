# app/dependencies.py
import os
import json
import uuid
import time
import gc
import queue
import logging
from concurrent.futures import ThreadPoolExecutor

# 引入配置
from core.configs import CONFIG_FILE, TRANSLATIONS_FILE
import core.tmdb as tmdb_module
import core.douban as douban_module
from core.logger import logger

# 全局线程池
MAX_WORKERS = min(4, (os.cpu_count() or 1) + 1)
GLOBAL_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# 任务状态存储
ACTIVE_TASKS = {}
TERMINAL_TASK_RETENTION_SECONDS = 300

# RSS 任务队列
RSS_JOB_QUEUE = queue.Queue()

# 全局翻译缓存
global_translations = {}

def load_translations_data():
    """加载翻译数据"""
    global global_translations
    if os.path.exists(TRANSLATIONS_FILE):
        try:
            with open(TRANSLATIONS_FILE, "r", encoding="utf-8") as f:
                global_translations = json.load(f)
        except:
            pass
    return global_translations

# 初始化加载一次
load_translations_data()

def _infer_task_type(run_id, name=""):
    run_id_str = str(run_id or "")
    task_name = str(name or "")
    if run_id_str.startswith("organize_") or "整理" in task_name:
        return "media_organize"
    if run_id_str.startswith("rss_run_") or task_name.startswith("RSS"):
        return "rss"
    if run_id_str.startswith("upgrade_") or "升级" in task_name:
        return "upgrade"
    if task_name.startswith("STRM"):
        return "strm"
    if "备份" in task_name:
        return "backup"
    if task_name.startswith("任务:"):
        return "preset_task"
    return "generic"


def update_task_progress(run_id, name, percent, status="running"):
    """更新任务进度"""
    cancel_req = False
    detail = None
    task_type = _infer_task_type(run_id, name)
    if run_id in ACTIVE_TASKS:
        existing = ACTIVE_TASKS[run_id]
        cancel_req = existing.get("cancel_requested", False)
        detail = existing.get("detail")
        task_type = existing.get("task_type") or task_type

    ACTIVE_TASKS[run_id] = {
        "name": name,
        "percent": percent,
        "status": status,
        "updated_at": time.time(),
        "cancel_requested": cancel_req,
        "task_type": task_type,
    }
    if detail is not None:
        ACTIVE_TASKS[run_id]["detail"] = detail

def cleanup_stale_tasks():
    """清理过期任务记录"""
    now = time.time()
    to_remove = [
        k for k, v in ACTIVE_TASKS.items() 
        if (v['status'] in ['finished', 'error', 'stopped'] and (now - v.get('updated_at', 0)) > TERMINAL_TASK_RETENTION_SECONDS)
        or (now - v.get('updated_at', 0)) > 86400
    ]
    for k in to_remove:
        del ACTIVE_TASKS[k]
    if to_remove:
        gc.collect()

def apply_proxy_settings():
    """应用代理设置"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            proxy_url = settings.get('proxy_url', '')
            tmdb_module.set_proxy(proxy_url)
            douban_module.DoubanApi.set_proxy(proxy_url)
            logger.info(f"[System] 代理配置已更新: {proxy_url if proxy_url else '关闭'}")
        except Exception as e:
            logger.error(f"[System] 应用代理失败: {e}")

# --- 追加到 app/dependencies.py 末尾 ---
import threading
from threading import Timer
from core.configs import DEVICE_ID_FILE

# Webhook 防抖管理器
class WebhookDebouncer:
    def __init__(self, delay=10):
        self.delay = delay 
        self.timers = {}
        self.lock = threading.Lock()

    def schedule(self, library_id, func, *args, display_name=None, **kwargs):
        with self.lock:
            if library_id in self.timers:
                self.timers[library_id].cancel()

            timer = Timer(self.delay, self._run, args=(library_id, func, args, kwargs))
            self.timers[library_id] = timer
            timer.start()
            lib_text = display_name or library_id
            logger.info(f"[Webhook] 已安排刷新封面 媒体库: {lib_text} ({self.delay}s后)")

    def _run(self, library_id, func, args, kwargs):
        with self.lock:
            if library_id in self.timers:
                del self.timers[library_id]
        func(*args[0], **kwargs)

# 全局防抖实例
webhook_debouncer = WebhookDebouncer(delay=10)


def format_episode_range(episodes: list) -> str:
    """将集数列表格式化为连续区间字符串。
    例如: [1,2,3,5,6,8] -> "E01-E03,E05-E06,E08"
         [1] -> "E01"
         [1,2] -> "E01-E02"
    """
    if not episodes:
        return ""
    # 只保留能转成整数的集数
    nums = sorted(set(int(e) for e in episodes if str(e).lstrip('-').isdigit()))
    if not nums:
        return ""

    ranges = []
    start = end = nums[0]
    for ep in nums[1:]:
        if ep == end + 1:
            end = ep
        else:
            ranges.append((start, end))
            start = end = ep
    ranges.append((start, end))

    parts = []
    for s, e in ranges:
        if s == e:
            parts.append(f"E{s:02d}")
        else:
            parts.append(f"E{s:02d}-E{e:02d}")
    return ",".join(parts)


class EpisodeNotifyAggregator:
    """剧集入库通知聚合器：将短时间内同一剧集同一季的多集合并成一条通知发送。"""

    def __init__(self, delay: int = 15):
        self.delay = delay
        self.lock = threading.Lock()
        self.buckets: dict = {}  # key -> {"timer": Timer, "episodes": list, "meta": dict}

    def add(self, key: str, episode_num, meta: dict):
        """将一集加入待聚合队列，重置该 key 的防抖计时器。"""
        with self.lock:
            if key in self.buckets:
                self.buckets[key]["timer"].cancel()
                self.buckets[key]["episodes"].append(episode_num)
            else:
                self.buckets[key] = {"episodes": [episode_num], "meta": meta}
            t = Timer(self.delay, self._flush, args=(key,))
            self.buckets[key]["timer"] = t
            t.start()

    def _flush(self, key: str):
        """计时器触发后，聚合该 key 的所有集数并发送一条通知。"""
        with self.lock:
            bucket = self.buckets.pop(key, None)
        if not bucket:
            return

        episodes = bucket["episodes"]
        meta = bucket["meta"]
        series_name = meta["series_name"]
        season = meta["season"]

        range_str = format_episode_range(episodes)
        if range_str:
            season_str = str(season).zfill(2) if str(season).isdigit() else str(season)
            media_name = f"{series_name} S{season_str} {range_str}"
        else:
            # fallback：直接拼原始集号
            season_str = str(season).zfill(2) if str(season).isdigit() else str(season)
            ep_str = str(episodes[0]).zfill(2) if str(episodes[0]).isdigit() else str(episodes[0])
            media_name = f"{series_name} S{season_str}E{ep_str}"

        notify_kwargs = dict(
            media_name=media_name,
            media_type="series",
            library_name=meta.get("library_name", ""),
            year=meta.get("year", ""),
            poster_url=meta.get("poster_url", ""),
            original_name=meta.get("original_name", ""),
            overview=meta.get("overview", ""),
            rating=meta.get("rating", ""),
            genres=meta.get("genres", ""),
            tagline=meta.get("tagline", ""),
            status=meta.get("status", ""),
            premiere_date=meta.get("premiere_date", ""),
            item_count=str(len(episodes)),
            server_name=meta.get("server_name", ""),
            tmdb_url=meta.get("tmdb_url", ""),
            original_title=meta.get("original_title", ""),
            server_idx=meta.get("server_idx", 0),
            item_id=meta.get("item_id", ""),
        )

        try:
            from app.services.wechat_service import wechat_notify_service
            from app.services.telegram_service import telegram_notify_service
            wechat_notify_service.notify_media_added(**notify_kwargs)
            telegram_notify_service.notify_media_added(**notify_kwargs)
            logger.info(f"[EpisodeNotify] 聚合通知已发送: {media_name}")
        except Exception as e:
            logger.error(f"[EpisodeNotify] 发送聚合通知失败: {e}")


# 全局剧集聚合器实例
episode_notify_aggregator = EpisodeNotifyAggregator(delay=15)

def get_device_fingerprint():
    """获取或生成设备指纹"""
    if os.path.exists(DEVICE_ID_FILE):
        try:
            with open(DEVICE_ID_FILE, 'r', encoding='utf-8') as f:
                code = f.read().strip()
                if code and len(code) >= 8: return code
        except: pass
    try: 
        new_code = uuid.uuid4().hex[:12].upper()
    except: 
        new_code = "UNKNOWN-DEVICE"
    try:
        with open(DEVICE_ID_FILE, 'w', encoding='utf-8') as f: f.write(new_code)
    except: pass
    return new_code
