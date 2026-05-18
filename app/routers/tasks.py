# app/routers/tasks.py
import os
import json
import uuid
import asyncio
import threading
from collections import deque
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse
from apscheduler.triggers.cron import CronTrigger

from app.schemas import CreateTaskRequest, UpdateTaskRequest, RunTaskRequest, RunSavedTaskRequest, ToggleTaskRequest
from app.dependencies import ACTIVE_TASKS, update_task_progress
from app.services.task_service import execute_task_logic, task_service_instance
from core.configs import TASKS_FILE, APP_LOG_FILE, TEMPLATES_DIR
from core.logger import logger, should_hide_console_log_line

router = APIRouter(tags=["Tasks"])

MAX_LOG_LINES = 5000
VALID_LOG_LEVELS = {"ALL", "INFO", "DEBUG", "WARNING", "ERROR"}
LOG_CATEGORY_KEYWORDS = {
    "PLAYBACK_302": (
        "播放信息接口触发预加载",
        "后台预加载成功",
        "后台预加载失败",
        "Pickcode模式检测",
        "从Path提取Pickcode成功",
        "Pickcode提取成功",
        "开始获取直链",
        "直链获取成功",
        "命中直链缓存",
        "收到播放请求",
        "302重定向到115直链",
        "收到 STRM 直连请求",
        "STRM 302重定向到115直链",
        "播放通知去重",
        "115直链获取失败，已降级反向代理",
        "STRM 直链获取失败，已降级反向代理",
    ),
    "MEDIA_ORGANIZE": (
        "[MediaOrganize]",
        "[媒体库缓存]",
        "[Wash]",
        "[CategoryDir]",
        "[EmbyLib]",
        "整理:",
        "洗版",
    ),
    "DRIVE_115": (
        "[115]",
        "[115-",
        "[115Life]",
        "[Rapid]",
        "[Sync-",
        "[115风控",
        "网盘",
    ),
    "STRM": (
        "[STRM]",
        "STRM",
        "strm",
    ),
    "NOTIFY": (
        "微信",
        "wechat",
        "Telegram",
        "telegram",
        "通知",
    ),
    "SCHEDULER": (
        "[Scheduler]",
        "定时任务",
        "任务",
        "cron",
    ),
    "DIAGNOSTIC": (
        "失败",
        "异常",
        "超时",
        "990009",
        "风控",
        "Traceback",
        "错误",
    ),
    "TMDB_SCRAPE": (
        "TMDb",
        "TMDB",
        "刮削",
        "元数据",
        "图片下载",
    ),
}
VALID_LOG_CATEGORIES = {"ALL", *LOG_CATEGORY_KEYWORDS.keys()}

LOG_SUBSCRIBERS = []
LOG_BUFFER = deque(maxlen=MAX_LOG_LINES)
LOG_STREAM_LOCK = threading.Lock()
NEXT_LOG_EVENT_ID = 1
NEXT_SUBSCRIBER_ID = 1

def normalize_level(level: str) -> str:
    raw = (level or "INFO").strip().upper()
    if raw == "WARN":
        raw = "WARNING"
    elif raw == "ERR":
        raw = "ERROR"
    if raw not in VALID_LOG_LEVELS:
        return "INFO"
    return raw

def parse_filter_level(level: str) -> str:
    raw = (level or "ALL").strip().upper()
    if raw == "WARN":
        raw = "WARNING"
    elif raw == "ERR":
        raw = "ERROR"
    if raw not in VALID_LOG_LEVELS:
        return "ALL"
    return raw

def extract_level_from_line(line: str) -> str:
    parts = line.split(" - ", 2)
    if len(parts) >= 3:
        return normalize_level(parts[1])
    return "INFO"

def is_level_match(entry_level: str, filter_level: str) -> bool:
    return filter_level == "ALL" or entry_level == filter_level

def normalize_keyword(keyword: str | None) -> str:
    if keyword is None:
        return ""
    return keyword.strip()[:200]


def parse_filter_category(category: str | None) -> str:
    raw = (category or "ALL").strip().upper()
    return raw if raw in VALID_LOG_CATEGORIES else "ALL"


def is_category_match(line: str, category: str) -> bool:
    if category == "ALL":
        return True
    keywords = LOG_CATEGORY_KEYWORDS.get(category)
    if not keywords:
        return True
    return any(keyword in line for keyword in keywords)


def is_keyword_match(line: str, keyword: str) -> bool:
    if not keyword:
        return True
    return keyword.lower() in line.lower()

def parse_event_id(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except Exception:
        return None

def queue_put_nowait_safe(queue: asyncio.Queue, item):
    try:
        queue.put_nowait(item)
    except Exception:
        pass

def format_sse_event(data: dict, event: str | None = None, event_id: int | None = None) -> str:
    lines = []
    if event:
        lines.append(f"event: {event}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"

def publish_log_line(line: str):
    global NEXT_LOG_EVENT_ID

    if not line or should_hide_console_log_line(line):
        return

    with LOG_STREAM_LOCK:
        entry = {
            "id": NEXT_LOG_EVENT_ID,
            "line": line,
            "level": extract_level_from_line(line)
        }
        NEXT_LOG_EVENT_ID += 1
        LOG_BUFFER.append(entry)
        subscribers = list(LOG_SUBSCRIBERS)

    dead_ids = set()
    for sub in subscribers:
        if not is_level_match(entry["level"], sub["level"]):
            continue
        if not is_category_match(entry["line"], sub.get("category", "ALL")):
            continue
        if not is_keyword_match(entry["line"], sub["keyword"]):
            continue
        try:
            sub["loop"].call_soon_threadsafe(queue_put_nowait_safe, sub["queue"], entry)
        except Exception:
            dead_ids.add(sub["id"])

    if dead_ids:
        with LOG_STREAM_LOCK:
            LOG_SUBSCRIBERS[:] = [s for s in LOG_SUBSCRIBERS if s["id"] not in dead_ids]

def snapshot_recent_logs(level: str = "ALL", keyword: str = "", category: str = "ALL") -> str:
    filter_level = parse_filter_level(level)
    filter_keyword = normalize_keyword(keyword)
    filter_category = parse_filter_category(category)
    with LOG_STREAM_LOCK:
        return "".join(
            entry["line"]
            for entry in LOG_BUFFER
            if is_level_match(entry["level"], filter_level)
            and not should_hide_console_log_line(entry["line"])
            and is_category_match(entry["line"], filter_category)
            and is_keyword_match(entry["line"], filter_keyword)
        )

def get_log_id_bounds() -> tuple[int, int]:
    with LOG_STREAM_LOCK:
        if not LOG_BUFFER:
            return 0, 0
        return LOG_BUFFER[0]["id"], LOG_BUFFER[-1]["id"]

def load_log_buffer_from_file():
    global NEXT_LOG_EVENT_ID

    if not os.path.exists(APP_LOG_FILE):
        return

    try:
        with open(APP_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-MAX_LOG_LINES:]

        with LOG_STREAM_LOCK:
            LOG_BUFFER.clear()
            next_id = 1
            for line in lines:
                if should_hide_console_log_line(line):
                    continue
                LOG_BUFFER.append({
                    "id": next_id,
                    "line": line,
                    "level": extract_level_from_line(line)
                })
                next_id += 1
            NEXT_LOG_EVENT_ID = next_id
    except Exception:
        pass

# --- 辅助函数：添加任务到调度器 ---
def add_job_to_scheduler(task):
    try:
        cron_str = task.get("cron", "")
        if len(cron_str.split()) != 5: return
        # 使用 lambda 延迟绑定参数
        job_fn = lambda: execute_task_logic(task["preset"], task["targets"], task.get("mode", "random"), task["name"])
        task_service_instance.scheduler.add_job(job_fn, CronTrigger.from_crontab(cron_str), id=task["id"], name=task["name"], replace_existing=True)
        logger.info(f"[Scheduler] 已装载任务: {task['name']} ({cron_str})")
    except Exception as e:
        logger.error(f"[Scheduler] 装载任务失败 {task['name']}: {e}")

@router.get("/api/progress")
def get_progress(): 
    return ACTIVE_TASKS

@router.get("/api/system_logs")
def get_system_logs(level: str = "ALL", keyword: str = "", category: str = "ALL"):
    filter_level = parse_filter_level(level)
    filter_keyword = normalize_keyword(keyword)
    filter_category = parse_filter_category(category)
    logs = snapshot_recent_logs(filter_level, filter_keyword, filter_category)
    _, latest_id = get_log_id_bounds()
    if logs:
        return {"logs": logs, "latest_id": latest_id, "level": filter_level, "keyword": filter_keyword, "category": filter_category}

    if os.path.exists(APP_LOG_FILE):
        try:
            with open(APP_LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()[-MAX_LOG_LINES:]

            filtered_lines = []
            for line in lines:
                line_level = extract_level_from_line(line)
                if (
                    is_level_match(line_level, filter_level)
                    and not should_hide_console_log_line(line)
                    and is_category_match(line, filter_category)
                    and is_keyword_match(line, filter_keyword)
                ):
                    filtered_lines.append(line)
            logs = "".join(filtered_lines)
        except Exception:
            logs = "Read logs failed."
    else:
        logs = "No log file found."

    return {"logs": logs, "latest_id": latest_id, "level": filter_level, "keyword": filter_keyword, "category": filter_category}

@router.get("/api/system_logs/stream")
async def stream_system_logs(request: Request, level: str = "ALL", keyword: str = "", category: str = "ALL", last_event_id: int | None = None):
    async def event_generator():
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        filter_level = parse_filter_level(level)
        filter_keyword = normalize_keyword(keyword)
        filter_category = parse_filter_category(category)
        cursor = parse_event_id(last_event_id)
        if cursor is None:
            cursor = parse_event_id(request.headers.get("last-event-id"))

        loop = asyncio.get_running_loop()

        global NEXT_SUBSCRIBER_ID
        with LOG_STREAM_LOCK:
            subscriber_id = NEXT_SUBSCRIBER_ID
            NEXT_SUBSCRIBER_ID += 1

            LOG_SUBSCRIBERS.append({
                "id": subscriber_id,
                "queue": q,
                "level": filter_level,
                "keyword": filter_keyword,
                "category": filter_category,
                "loop": loop
            })
            snapshot = list(LOG_BUFFER)

        try:
            first_id = snapshot[0]["id"] if snapshot else 0
            if cursor is not None and snapshot and cursor < first_id - 1:
                yield format_sse_event({
                    "reason": "cursor_too_old",
                    "min_id": first_id,
                    "latest_id": snapshot[-1]["id"],
                    "level": filter_level,
                    "keyword": filter_keyword,
                    "category": filter_category
                }, event="reset")
                replay_entries = []
            elif cursor is not None:
                replay_entries = [
                    e for e in snapshot
                    if e["id"] > cursor
                    and is_level_match(e["level"], filter_level)
                    and not should_hide_console_log_line(e["line"])
                    and is_category_match(e["line"], filter_category)
                    and is_keyword_match(e["line"], filter_keyword)
                ]
            else:
                replay_entries = [
                    e for e in snapshot
                    if is_level_match(e["level"], filter_level)
                    and not should_hide_console_log_line(e["line"])
                    and is_category_match(e["line"], filter_category)
                    and is_keyword_match(e["line"], filter_keyword)
                ]

            if replay_entries:
                init_chunk = "".join(e["line"] for e in replay_entries)
                yield format_sse_event({"chunk": init_chunk, "level": filter_level, "keyword": filter_keyword, "category": filter_category}, event="init")

            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15)
                    if should_hide_console_log_line(entry["line"]):
                        continue
                    yield format_sse_event({
                        "chunk": entry["line"],
                        "id": entry["id"],
                        "level": entry["level"],
                        "category": filter_category
                    }, event_id=entry["id"])
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            with LOG_STREAM_LOCK:
                LOG_SUBSCRIBERS[:] = [s for s in LOG_SUBSCRIBERS if s["id"] != subscriber_id]

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no"
    })

@router.post("/api/clear_system_logs")
def clear_system_logs():
    """清空系统日志"""
    try:
        if os.path.exists(APP_LOG_FILE):
            with open(APP_LOG_FILE, "w", encoding="utf-8") as f:
                f.write("")
        with LOG_STREAM_LOCK:
            LOG_BUFFER.clear()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/clear_task_progress")
def clear_task_progress(payload: dict = Body(...)):
    run_id = payload.get("run_id")
    if run_id in ACTIVE_TASKS: del ACTIVE_TASKS[run_id]
    return {"status": "ok"}

@router.post("/api/stop_task")
def stop_task(payload: dict = Body(...)):
    run_id = payload.get("run_id")
    if not run_id:
        return {"status": "not_found", "message": "任务不存在或已结束"}

    task = ACTIVE_TASKS.get(run_id)
    if not task:
        logger.warning(f"[Tasks] 取消任务失败，任务不存在: run_id={run_id}")
        return {"status": "not_found", "message": "任务不存在或已结束"}

    if task.get("status") in ("finished", "error", "stopped"):
        return {"status": "not_found", "message": "任务已结束"}

    task["cancel_requested"] = True
    logger.info(f"[Tasks] 已请求取消任务: run_id={run_id}, name={task.get('name', '')}")
    return {"status": "ok"}

@router.get("/api/tasks")
def get_tasks():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            return {"tasks": tasks}
        except: pass
    return {"tasks": []}

@router.post("/api/run_task")
def run_task_batch(req: RunTaskRequest):
    # 手动批量运行（不保存）
    threading.Thread(target=execute_task_logic, args=(req.preset_filename, req.targets, req.mode, "Manual Batch")).start()
    return {"status": "ok", "message": "任务已在后台启动"}

@router.post("/api/run_saved_task")
def run_saved_task_endpoint(req: RunSavedTaskRequest):
    tasks = []
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f: tasks = json.load(f)
        except: pass

    target = next((t for t in tasks if t.get("id") == req.id), None)
    if not target: raise HTTPException(status_code=404, detail="Task not found")

    threading.Thread(target=execute_task_logic, args=(target["preset"], target["targets"], target.get("mode", "random"), target["name"])).start()
    return {"status": "ok", "message": "任务已在后台启动"}

@router.post("/api/create_task")
def create_task_endpoint(req: CreateTaskRequest):
    tasks = []
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f: tasks = json.load(f)
        except: pass

    task_id = str(uuid.uuid4())
    new_task = {
        "id": task_id, "name": req.name, "cron": req.cron,
        "preset": req.preset_filename,
        "targets": [
            {
                "server_idx": int(t.server_idx or 0),
                "library_id": t.library_id,
                "library_name": t.library_name,
            }
            for t in req.targets
        ],
        "mode": req.mode, "enabled": req.enabled
    }
    tasks.append(new_task)
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=4, ensure_ascii=False)

    if req.enabled:
        add_job_to_scheduler(new_task)
    return {"status": "saved", "task_id": task_id}

@router.post("/api/update_task")
def update_task_endpoint(req: UpdateTaskRequest):
    tasks = []
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f: tasks = json.load(f)
        except: pass

    for i, t in enumerate(tasks):
        if t.get("id") == req.id:
            tasks[i] = {
                "id": req.id, "name": req.name, "cron": req.cron,
                "preset": req.preset_filename,
                "targets": [
                    {
                        "server_idx": int(target.server_idx or 0),
                        "library_id": target.library_id,
                        "library_name": target.library_name,
                    }
                    for target in req.targets
                ],
                "mode": req.mode, "enabled": req.enabled
            }
            with open(TASKS_FILE, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=4, ensure_ascii=False)

            # 更新调度器
            if task_service_instance.scheduler.get_job(req.id): task_service_instance.scheduler.remove_job(req.id)
            if req.enabled:
                add_job_to_scheduler(tasks[i])
            return {"status": "updated"}

    raise HTTPException(status_code=404, detail="Task not found")

@router.post("/api/delete_task")
def delete_task_endpoint(payload: dict = Body(...)):
    task_id = payload.get("id")
    if not os.path.exists(TASKS_FILE): return {"status": "ok"}
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f: tasks = json.load(f)
        tasks = [t for t in tasks if t.get("id") != task_id]
        with open(TASKS_FILE, "w", encoding="utf-8") as f: 
            json.dump(tasks, f, indent=4, ensure_ascii=False)
        if task_service_instance.scheduler.get_job(task_id): task_service_instance.scheduler.remove_job(task_id)
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/toggle_task")
def toggle_task_endpoint(req: ToggleTaskRequest):
    if not os.path.exists(TASKS_FILE): raise HTTPException(status_code=404)
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f: tasks = json.load(f)
        target = next((t for t in tasks if t.get("id") == req.id), None)
        if not target: raise HTTPException(status_code=404)
        
        target['enabled'] = req.enabled
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, indent=4, ensure_ascii=False)
            
        if req.enabled:
            add_job_to_scheduler(target)
        else:
            if task_service_instance.scheduler.get_job(req.id):
                task_service_instance.scheduler.remove_job(req.id)
                
        return {"status": "ok", "enabled": req.enabled}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
