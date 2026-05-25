from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException, Query

from app.routers.config_302 import get_emby_config_by_index_sync
from core.emby_client import EmbyClient
from core.logger import logger

router = APIRouter(prefix="/api/emby_tasks", tags=["EmbyTasks"])

TICKS_PER_SECOND = 10_000_000
TICKS_PER_MINUTE = TICKS_PER_SECOND * 60
TICKS_PER_HOUR = TICKS_PER_MINUTE * 60


CATEGORY_LABELS = {
    "application": "应用程序",
    "database": "数据库",
    "downloads & conversions": "下载与转换",
    "internet channels": "互联网频道",
    "library": "媒体库扫描",
    "maintenance": "日常维护",
    "subtitle": "字幕",
    "subtitles": "字幕",
    "sync": "同步",
    "livetv": "电视直播",
    "live tv": "电视直播",
    "plugins": "插件",
    "plugin": "插件",
    "people": "人员",
}

TASK_NAME_LABELS = {
    "Build Douban Cache": "构建豆瓣缓存",
    "Cache file cleanup": "清理缓存文件",
    "Check for application updates": "检查应用更新",
    "Check for plugin updates": "检查插件更新",
    "Convert media": "转换媒体",
    "Delete Persons": "删除人员数据",
    "Detect Episode Intros": "检测剧集片头",
    "Download OCR Data": "下载 OCR 数据",
    "Download subtitles": "下载字幕",
    "Emby Server Backup": "Emby 服务器备份",
    "Extract Intro Fingerprint": "提取片头声纹",
    "Extract MediaInfo": "提取媒体信息",
    "Extract Video Thumbnail": "提取视频缩略图",
    "Hardware Detection": "硬件检测",
    "Log file cleanup": "清理日志文件",
    "Merge Multi Versions": "合并多版本",
    "Persist MediaInfo": "持久化媒体信息",
    "Refresh Chinese Actor": "刷新中文演员",
    "Refresh Custom Intros": "刷新自定义片头",
    "Refresh Emby Connect Data": "刷新 Emby Connect 数据",
    "Refresh Episode": "刷新剧集",
    "Refresh Guide": "刷新电视指南",
    "Refresh Internet Channels": "刷新互联网频道",
    "Refresh Users": "刷新用户",
    "Rotate log file": "轮转日志文件",
    "Scan External Tracks": "扫描外挂轨道",
    "Scan media library": "扫描媒体库",
    "Scan Metadata Folder": "扫描元数据文件夹",
    "Send Download Notifications": "发送下载通知",
    "Transfer media": "转移媒体",
    "Update Plugin": "更新插件",
    "Vacuum Database": "整理数据库",
    "Video preview thumbnail extraction": "提取视频预览缩略图",
}

TASK_DESCRIPTION_LABELS = {
    "Deletes cache files no longer needed by the system": "删除系统不再需要的缓存文件。",
    "Downloads and installs application updates.": "下载并安装应用程序更新。",
    "Downloads and installs updates for plugins that are configured to update automatically.": "下载并安装已配置自动更新的插件更新。",
    "Runs conversion jobs that were created using the convert media feature as well as downloads that require conversion to compatible formats.": "运行媒体转换任务，并处理需要转换为兼容格式的下载内容。",
    "Detect intro start and end times for episodes that it is enabled for.": "检测已启用剧集的片头开始和结束时间。",
    "Downloads OCR model data for subtitle conversion.": "下载字幕转换所需的 OCR 模型数据。",
    "Searches the internet for missing subtitles, if automatic subtitle downloading is enabled in Emby library setup.": "在媒体库启用自动字幕下载时，联网搜索缺失字幕。",
    "Scheduled backup from Emby Backup": "由 Emby Backup 执行计划备份。",
    "Detect available hardware acceleration devices.": "检测可用的硬件加速设备。",
    "Deletes log files that are more than 3 days old.": "删除超过 3 天的日志文件。",
    "Refreshes custom intro files.": "刷新自定义片头文件。",
    "Refresh Emby Connect Data": "刷新 Emby Connect 数据。",
    "Downloads channel information from live tv services.": "从电视直播服务下载频道信息。",
    "Refreshes internet channel information.": "刷新互联网频道信息。",
    "Refresh user infos": "刷新用户信息。",
    "Moves logging to a new file to help reduce log file sizes.": "将日志切换到新文件，降低单个日志文件体积。",
    "Scans your media library to check for new and updated files.": "扫描媒体库，检查新增和更新的文件。",
    "Run this task if you've modified the contents of Emby Server's internal metadata folder directly, so that Emby Server can discover the changes.": "直接修改 Emby 内部元数据文件夹后运行此任务，让 Emby 发现这些变更。",
    "Send download notifications": "发送下载通知。",
    "Transfers completed conversions to their final destination. Used by the Download and Conversion features.": "将已完成的转换结果转移到最终位置，供下载与转换功能使用。",
    "Schedules a database vacuum on the next server startup": "安排在下次服务器启动时整理数据库。",
    "Creates thumbnails for videos.": "为视频创建预览缩略图。",
}

STATUS_LABELS = {
    "completed": "执行成功",
    "failed": "执行失败",
    "cancelled": "已取消",
    "canceled": "已取消",
    "aborted": "已取消",
}

DAY_OF_WEEK_LABELS = {
    "Monday": "周一",
    "Tuesday": "周二",
    "Wednesday": "周三",
    "Thursday": "周四",
    "Friday": "周五",
    "Saturday": "周六",
    "Sunday": "周日",
}

TRIGGER_TYPE_LABELS = {
    "DailyTrigger": "每天",
    "WeeklyTrigger": "每周",
    "IntervalTrigger": "按间隔",
    "StartupTrigger": "服务器启动时",
    "SystemEventTrigger": "系统事件",
}


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _normalize_category(value: Any) -> str:
    raw = _safe_str(value, "其他任务")
    key = raw.lower().strip()
    return CATEGORY_LABELS.get(key, raw)


def _normalize_task_name(value: Any) -> str:
    raw = _safe_str(value, "未命名任务")
    return TASK_NAME_LABELS.get(raw, raw)


def _normalize_task_description(value: Any) -> str:
    raw = _safe_str(value)
    return TASK_DESCRIPTION_LABELS.get(raw, raw)


def _ticks_to_time(value: Any) -> str:
    try:
        total_minutes = int(int(value or 0) / TICKS_PER_MINUTE)
    except (TypeError, ValueError):
        total_minutes = 0
    total_minutes = max(0, min(total_minutes, 24 * 60 - 1))
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _hours_from_ticks(value: Any) -> float:
    try:
        ticks = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if ticks <= 0:
        return 0
    hours = ticks / TICKS_PER_HOUR
    return round(hours, 2)


def _ticks_from_hours(value: Any) -> int:
    try:
        hours = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if hours <= 0:
        return 0
    return int(hours * TICKS_PER_HOUR)


def _time_to_ticks(value: Any) -> int:
    text = _safe_str(value, "00:00")
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = max(0, min(23, int(hour_text)))
        minute = max(0, min(59, int(minute_text)))
    except Exception:
        hour, minute = 0, 0
    return (hour * 60 + minute) * TICKS_PER_MINUTE


def _format_interval_hours(hours: float) -> str:
    if hours <= 0:
        return "未设置间隔"
    if abs(hours - round(hours)) < 0.01:
        return f"每 {int(round(hours))} 小时"
    return f"每 {hours:g} 小时"


def _normalize_trigger(trigger: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(trigger, dict):
        trigger = {}
    trigger_type = _safe_str(trigger.get("Type"), "DailyTrigger")
    day = _safe_str(trigger.get("DayOfWeek"))
    time_text = _ticks_to_time(trigger.get("TimeOfDayTicks"))
    interval_hours = _hours_from_ticks(trigger.get("IntervalTicks"))
    max_runtime_hours = _hours_from_ticks(trigger.get("MaxRuntimeTicks"))
    system_event = _safe_str(trigger.get("SystemEvent"))

    if trigger_type == "DailyTrigger":
        summary = f"每天 {time_text}"
    elif trigger_type == "WeeklyTrigger":
        summary = f"{DAY_OF_WEEK_LABELS.get(day, day or '每周')} {time_text}"
    elif trigger_type == "IntervalTrigger":
        summary = _format_interval_hours(interval_hours)
    elif trigger_type == "StartupTrigger":
        summary = "服务器启动时"
    elif trigger_type == "SystemEventTrigger":
        summary = f"系统事件：{system_event or '未指定'}"
    else:
        summary = TRIGGER_TYPE_LABELS.get(trigger_type, trigger_type or "触发器")

    if max_runtime_hours > 0:
        summary = f"{summary}，最多运行 {max_runtime_hours:g} 小时"

    return {
        "type": trigger_type,
        "type_label": TRIGGER_TYPE_LABELS.get(trigger_type, trigger_type),
        "time": time_text,
        "day_of_week": day,
        "day_label": DAY_OF_WEEK_LABELS.get(day, day),
        "interval_hours": interval_hours,
        "max_runtime_hours": max_runtime_hours,
        "system_event": system_event,
        "summary": summary,
        "raw": trigger,
    }


def _normalize_triggers(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_normalize_trigger(item) for item in value if isinstance(item, dict)]


def _build_trigger_summary(triggers: List[Dict[str, Any]]) -> str:
    if not triggers:
        return "未设置计划"
    summaries = [_safe_str(item.get("summary")) for item in triggers if _safe_str(item.get("summary"))]
    if not summaries:
        return "未设置计划"
    if len(summaries) <= 2:
        return "；".join(summaries)
    return f"{'；'.join(summaries[:2])} 等 {len(summaries)} 个计划"


def _normalize_trigger_for_emby(trigger: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(trigger, dict):
        raise ValueError("触发器格式不正确")
    trigger_type = _safe_str(trigger.get("type") or trigger.get("Type"), "DailyTrigger")
    max_runtime_ticks = _ticks_from_hours(trigger.get("max_runtime_hours") or trigger.get("MaxRuntimeHours"))
    result: Dict[str, Any] = {"Type": trigger_type}

    if trigger_type == "DailyTrigger":
        result["TimeOfDayTicks"] = _time_to_ticks(trigger.get("time") or trigger.get("Time"))
    elif trigger_type == "WeeklyTrigger":
        result["TimeOfDayTicks"] = _time_to_ticks(trigger.get("time") or trigger.get("Time"))
        result["DayOfWeek"] = _safe_str(trigger.get("day_of_week") or trigger.get("DayOfWeek"), "Monday")
    elif trigger_type == "IntervalTrigger":
        interval_ticks = _ticks_from_hours(trigger.get("interval_hours") or trigger.get("IntervalHours"))
        if interval_ticks <= 0:
            raise ValueError("间隔触发器需要填写大于 0 的小时数")
        result["IntervalTicks"] = interval_ticks
    elif trigger_type == "StartupTrigger":
        pass
    elif trigger_type == "SystemEventTrigger":
        result["SystemEvent"] = _safe_str(trigger.get("system_event") or trigger.get("SystemEvent"))
    else:
        raise ValueError(f"不支持的触发类型: {trigger_type}")

    if max_runtime_ticks > 0:
        result["MaxRuntimeTicks"] = max_runtime_ticks
    return result


def _parse_datetime(value: Any):
    text = _safe_str(value)
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except Exception:
        return None


def _format_task_time(value: Any) -> str:
    dt = _parse_datetime(value)
    if not dt:
        return ""
    now = datetime.now(dt.tzinfo)
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return f"{dt.month}/{dt.day} {dt.strftime('%H:%M')}"


def _task_sort_key(task: Dict[str, Any]):
    running_rank = 0 if task.get("is_running") else 1
    name = _safe_str(task.get("name")).lower()
    return (running_rank, name)


def _category_sort_key(category: Dict[str, Any]):
    name = _safe_str(category.get("name"))
    if name.startswith("神医助手"):
        return (0, name)
    if name == "媒体库扫描":
        return (1, name)
    return (2, name)


def _normalize_task(task: Dict[str, Any]) -> Dict[str, Any]:
    last = task.get("LastExecutionResult") or {}
    state = _safe_str(task.get("State"), "Idle")
    last_status = _safe_str(last.get("Status"))
    last_status_key = last_status.lower()
    is_running = state.lower() == "running"
    progress = task.get("CurrentProgressPercentage")
    try:
        progress_value = round(float(progress or 0), 1)
    except (TypeError, ValueError):
        progress_value = 0.0

    last_end = last.get("EndTimeUtc") or last.get("StartTimeUtc")
    status_label = "运行中" if is_running else STATUS_LABELS.get(last_status_key, "无记录")
    status_type = "running" if is_running else {
        "completed": "success",
        "failed": "error",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "aborted": "cancelled",
    }.get(last_status_key, "idle")
    triggers = _normalize_triggers(task.get("Triggers"))

    return {
        "id": _safe_str(task.get("Id") or task.get("Key") or task.get("Name")),
        "key": _safe_str(task.get("Key")),
        "name": _normalize_task_name(task.get("Name")),
        "description": _normalize_task_description(task.get("Description")),
        "category": _normalize_category(task.get("Category")),
        "raw_category": _safe_str(task.get("Category"), "其他任务"),
        "state": state,
        "is_running": is_running,
        "progress": progress_value,
        "last_status": last_status,
        "status_label": status_label,
        "status_type": status_type,
        "last_run_time": _format_task_time(last_end),
        "last_run_time_raw": _safe_str(last_end),
        "error_message": _safe_str(last.get("ErrorMessage")),
        "triggers": triggers,
        "trigger_summary": _build_trigger_summary(triggers),
    }


def _get_client(server_idx: int = 0) -> EmbyClient:
    cfg = get_emby_config_by_index_sync(server_idx)
    if not cfg or not cfg.get("url") or not cfg.get("key"):
        raise HTTPException(status_code=400, detail="请先在 Emby 配置中填写地址和接口密钥")
    return EmbyClient(cfg.get("url"), cfg.get("key"), cfg.get("public_host") or cfg.get("url"))


def _build_payload(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = [_normalize_task(task) for task in tasks if isinstance(task, dict)]
    normalized.sort(key=_task_sort_key)
    categories = []
    for task in normalized:
        category = task["category"]
        group = next((item for item in categories if item["name"] == category), None)
        if not group:
            group = {"name": category, "count": 0, "tasks": []}
            categories.append(group)
        group["tasks"].append(task)
        group["count"] += 1
    categories.sort(key=_category_sort_key)
    running = [task for task in normalized if task.get("is_running")]
    return {
        "tasks": normalized,
        "categories": categories,
        "running": running,
        "running_count": len(running),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


@router.get("")
def list_emby_tasks(server_idx: int = Query(default=0, ge=0)):
    client = _get_client(server_idx)
    try:
        return _build_payload(client.get_scheduled_tasks())
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[EmbyTasks] 获取计划任务失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取 Emby 任务失败: {str(e)}")
    finally:
        client.close()


@router.get("/{task_id}/triggers")
def get_emby_task_triggers(task_id: str, server_idx: int = Query(default=0, ge=0)):
    client = _get_client(server_idx)
    try:
        task = client.get_scheduled_task(task_id)
        normalized = _normalize_task(task if isinstance(task, dict) else {})
        return {
            "task": normalized,
            "triggers": normalized.get("triggers", []),
            "trigger_summary": normalized.get("trigger_summary", "未设置计划"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[EmbyTasks] 获取触发器失败 task_id={task_id}: {e}")
        raise HTTPException(status_code=500, detail=f"获取触发器失败: {str(e)}")
    finally:
        client.close()


@router.post("/{task_id}/triggers")
def update_emby_task_triggers(task_id: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    server_idx = int((payload or {}).get("server_idx") or 0)
    raw_triggers = (payload or {}).get("triggers")
    if not isinstance(raw_triggers, list):
        raise HTTPException(status_code=400, detail="触发器列表格式不正确")
    try:
        triggers = [_normalize_trigger_for_emby(item) for item in raw_triggers]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    client = _get_client(server_idx)
    try:
        client.update_scheduled_task_triggers(task_id, triggers)
        task = client.get_scheduled_task(task_id)
        normalized = _normalize_task(task if isinstance(task, dict) else {})
        return {
            "status": "success",
            "message": "触发器已保存",
            "task": normalized,
            "triggers": normalized.get("triggers", []),
            "trigger_summary": normalized.get("trigger_summary", "未设置计划"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[EmbyTasks] 保存触发器失败 task_id={task_id}: {e}")
        raise HTTPException(status_code=500, detail=f"保存触发器失败: {str(e)}")
    finally:
        client.close()


@router.post("/{task_id}/run")
def run_emby_task(task_id: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    server_idx = int((payload or {}).get("server_idx") or 0)
    client = _get_client(server_idx)
    try:
        client.run_scheduled_task(task_id)
        return {"status": "success", "message": "任务已启动"}
    except Exception as e:
        logger.error(f"[EmbyTasks] 启动任务失败 task_id={task_id}: {e}")
        raise HTTPException(status_code=500, detail=f"启动任务失败: {str(e)}")
    finally:
        client.close()


@router.post("/{task_id}/stop")
def stop_emby_task(task_id: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    server_idx = int((payload or {}).get("server_idx") or 0)
    client = _get_client(server_idx)
    try:
        client.stop_scheduled_task(task_id)
        return {"status": "success", "message": "停止命令已发送"}
    except Exception as e:
        logger.error(f"[EmbyTasks] 停止任务失败 task_id={task_id}: {e}")
        raise HTTPException(status_code=500, detail=f"停止任务失败: {str(e)}")
    finally:
        client.close()
