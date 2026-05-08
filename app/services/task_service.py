import os
import json
import uuid
import base64
import gc
import copy
import asyncio
import traceback
import random
from datetime import datetime, timedelta
import time
from concurrent.futures import as_completed
from io import BytesIO

# 调度器相关
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# 引入核心模块
from core.emby_client import EmbyClient
from core.engine import PosterEngine
from core.configs import TEMPLATES_DIR, FONTS_DIR, LAYOUTS_DIR, TASKS_FILE
from core.logger import logger
from app.routers.config_302 import get_emby_config_by_index_sync
from PIL import Image, ImageFilter

# 引入 115 服务 (用于清理任务)
from app.services.drive115_service import drive115_service
from app.services.wechat_service import wechat_notify_service
from app.services.telegram_service import telegram_notify_service

# 引入依赖
from app.dependencies import (
    ACTIVE_TASKS, 
    GLOBAL_EXECUTOR, 
    update_task_progress, 
    global_translations
)

# ==========================================
# 1. 任务执行逻辑 (封面生成)
# ==========================================
def _build_wechat_cover_preview(image_data: bytes, width: int = 900, height: int = 383) -> bytes:
    source = Image.open(BytesIO(image_data)).convert("RGB")
    bg = source.copy()
    bg_ratio = width / height
    src_ratio = bg.width / bg.height
    if src_ratio > bg_ratio:
        crop_w = int(bg.height * bg_ratio)
        left = max((bg.width - crop_w) // 2, 0)
        bg = bg.crop((left, 0, left + crop_w, bg.height))
    else:
        crop_h = int(bg.width / bg_ratio)
        top = max((bg.height - crop_h) // 2, 0)
        bg = bg.crop((0, top, bg.width, top + crop_h))
    bg = bg.resize((width, height), Image.LANCZOS).filter(ImageFilter.GaussianBlur(18))

    fg = source.copy()
    fg.thumbnail((width, height), Image.LANCZOS)
    x = (width - fg.width) // 2
    y = (height - fg.height) // 2
    bg.paste(fg, (x, y))

    output = BytesIO()
    bg.save(output, format="JPEG", quality=85, optimize=True)
    return output.getvalue()


def _normalize_task_target(target_obj):
    t = target_obj if isinstance(target_obj, dict) else target_obj.model_dump()
    normalized = dict(t)
    server_idx = normalized.get("server_idx")
    try:
        server_idx = int(server_idx) if server_idx is not None else 0
    except Exception:
        server_idx = 0
    normalized["server_idx"] = server_idx
    return normalized


def _hydrate_task_target(target_obj):
    normalized = _normalize_task_target(target_obj)
    server = get_emby_config_by_index_sync(normalized.get("server_idx", 0))
    if server and server.get("enabled", True):
        normalized["url"] = server.get("url", "")
        normalized["key"] = server.get("key", "")
        normalized["public_host"] = server.get("public_host")
    return normalized


def execute_task_logic(preset_filename, targets, mode="random", task_name="Unknown"):
    """
    通用任务执行逻辑 (生成封面)
    """
    started_at = datetime.now()
    run_id = str(uuid.uuid4())
    update_task_progress(run_id, f"任务: {task_name}", 0, "running")

    is_webhook_task = task_name.startswith("Webhook:")
    if is_webhook_task:
        webhook_lib_name = task_name.split(":", 1)[1].strip()
        display_task_name = f"Webhook 刷新{webhook_lib_name}封面"
        logger.info(f"[任务] {display_task_name} 开始")
    else:
        logger.info(f">>> [任务: {task_name}] 开始执行...")

    notify_task_name = f"海报任务 · {display_task_name if is_webhook_task else task_name}"

    def send_task_notification(status: str, detail: str, total: int = 0, generated_count: int = 0, failed_count: int = 0, poster_url: str = ""):
        elapsed = str(datetime.now() - started_at).split(".")[0]
        notify_kwargs = {
            "task_name": notify_task_name,
            "status": status,
            "task_category": "poster",
            "elapsed": elapsed,
            "posters_count": total,
            "generated": generated_count,
            "failed": failed_count,
            "detail": detail,
            "summary": detail,
        }
        if is_webhook_task and poster_url:
            notify_kwargs["poster_url"] = poster_url
        wechat_notify_service.notify_task_complete(**notify_kwargs)
        telegram_notify_service.notify_task_complete(**notify_kwargs)

    preset_path = os.path.join(TEMPLATES_DIR, preset_filename)
    if not os.path.exists(preset_path):
        update_task_progress(run_id, f"任务失败: 预设丢失", 100, "error")
        send_task_notification("error", "预设丢失", total=len(targets), failed_count=len(targets))
        return {"status": "error"}

    try:
        with open(preset_path, "r", encoding="utf-8") as f:
            preset_data = json.load(f)
            base_config = preset_data.get("config", {})
            base_config['engine'] = preset_data.get("engine", "classic")
    except Exception:
        update_task_progress(run_id, f"任务失败: 预设损坏", 100, "error")
        send_task_notification("error", "预设损坏", total=len(targets), failed_count=len(targets))
        return {"status": "error"}

    engine = PosterEngine(fonts_dir=FONTS_DIR, layouts_dir=LAYOUTS_DIR)
    total = len(targets)

    def process_target(target_obj):
        t = _hydrate_task_target(target_obj)
        try:
            run_config = copy.deepcopy(base_config)
            run_config['title'] = t.get('library_name', 'Unknown')

            # 使用全局翻译
            if run_config['title'] in global_translations:
                run_config['subtitle'] = global_translations[run_config['title']]
            else:
                run_config['subtitle'] = f"Collection {run_config['title']}"

            client = EmbyClient(t.get('url', ''), t.get('key', ''), t.get('public_host'))
            p_limit = int(run_config.get('poster_count', 6))
            b_limit = int(run_config.get('backdrop_count', 1))

            assets = client.get_assets(t['library_id'], mode=mode, poster_limit=p_limit, backdrop_limit=b_limit)

            if not assets or (not assets.get('posters') and not assets.get('bg_url') and not assets.get('backdrops')):
                return {"success": False, "poster_url": ""}

            img_b64 = engine.draw(run_config, assets)
            img_data = base64.b64decode(img_b64)

            if client.upload_cover(t['library_id'], img_data):
                if is_webhook_task:
                    from app.routers.discover import put_task_cover_preview
                    from core.configs import global_config
                    base = global_config.app_public_base_url
                    poster_url = ""
                    if base:
                        preview_key = uuid.uuid4().hex
                        put_task_cover_preview(preview_key, _build_wechat_cover_preview(img_data))
                        poster_url = f"{base}/api/discover/task_cover?key={preview_key}"
                    if poster_url:
                        logger.info(f"[任务] Webhook封面通知图片: {t.get('library_name', '')} -> {poster_url}")
                    else:
                        logger.warning(f"[任务] Webhook封面通知图片为空: base={'已配置' if base else '未配置'}")
                else:
                    poster_url = ""
                return {"success": True, "poster_url": poster_url}
        except Exception as e:
            logger.error(f"[任务] {t.get('library_name')}: 执行异常 {e}")
        return {"success": False, "poster_url": ""}

    success_count = 0
    fail_count = 0
    webhook_poster_url = ""

    futures = [GLOBAL_EXECUTOR.submit(process_target, t) for t in targets]

    for i, future in enumerate(as_completed(futures)):
        # 检查取消信号
        if ACTIVE_TASKS.get(run_id, {}).get("cancel_requested"):
            for f in futures:
                f.cancel()
            update_task_progress(run_id, f"任务: {task_name} (已停止)", int(((i) / total) * 100), "error")
            send_task_notification("stopped", "任务已停止", total=total, generated_count=success_count, failed_count=fail_count)
            return {"status": "stopped"}

        result = future.result()
        if result.get("success"):
            success_count += 1
            if is_webhook_task and not webhook_poster_url:
                webhook_poster_url = result.get("poster_url", "")
        else:
            fail_count += 1

        percent = int(((i + 1) / total) * 100)
        update_task_progress(run_id, f"任务: {task_name}", percent, "running")

    if is_webhook_task:
        logger.info(f"[任务] {display_task_name} 完成")
    else:
        logger.info(f">>> [任务: {task_name}] 执行结束. 成功: {success_count}, 失败: {fail_count}")

    update_task_progress(run_id, f"任务: {task_name}", 100, "finished")
    final_status = "success" if fail_count == 0 else "error"
    send_task_notification(
        final_status,
        f"成功 {success_count} / 失败 {fail_count}",
        total=total,
        generated_count=success_count,
        failed_count=fail_count,
        poster_url=webhook_poster_url,
    )
    gc.collect()
    return {"status": "ok"}

# ==========================================
# 2. 定义 TaskService 类
# ==========================================
class TaskService:
    def __init__(self):
        # 初始化后台调度器
        self.scheduler = BackgroundScheduler()
        self.active_tasks = {} # 内存中记录任务配置，用于前端展示

        # 专门用于存储 115 清理任务的 ID，防止刷新配置时重复添加
        self.cleanup_job_ids = []
        self.selected_cleanup_job_ids = []
        self.selected_cleanup_running = set()
        self.daily_signin_job_id = "daily_115_signin_random"

    def get_tasks(self):
        """获取当前所有任务"""
        return list(self.active_tasks.values())

    def load_active_jobs(self):
        """启动时从 tasks.json 恢复已启用的定时任务到调度器"""
        if not os.path.exists(TASKS_FILE):
            return
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                tasks = json.load(f)
        except Exception as e:
            logger.error(f"[TaskService] 读取 tasks.json 失败: {e}")
            return

        loaded = 0
        for task in tasks:
            if not task.get("enabled"):
                continue
            cron_str = task.get("cron", "")
            if len(cron_str.split()) != 5:
                continue
            try:
                preset = task["preset"]
                targets = [_normalize_task_target(target) for target in task["targets"]]
                mode = task.get("mode", "random")
                name = task.get("name", "Unknown")
                task_id = task["id"]

                def job_wrapper(p=preset, t=targets, m=mode, n=name):
                    execute_task_logic(preset_filename=p, targets=t, mode=m, task_name=n)

                self.scheduler.add_job(
                    job_wrapper,
                    CronTrigger.from_crontab(cron_str),
                    id=str(task_id),
                    replace_existing=True
                )
                task["targets"] = targets
                self.active_tasks[str(task_id)] = task
                loaded += 1
            except Exception as e:
                logger.error(f"[TaskService] 恢复任务失败 {task.get('name')}: {e}")

        if loaded > 0:
            logger.info(f"[启动] 自动封面任务已恢复: {loaded} 个")

    def add_task(self, task_id, cron_str, preset_filename, targets, mode, name="Unknown"):
        """添加新任务到调度器 (封面生成任务)"""
        try:
            # 定义调度器触发时要执行的包装函数
            def job_wrapper():
                # 调用上面的 execute_task_logic 函数
                execute_task_logic(
                    preset_filename=preset_filename,
                    targets=targets,
                    mode=mode,
                    task_name=name
                )

            # 添加任务到 APScheduler
            self.scheduler.add_job(
                job_wrapper,
                CronTrigger.from_crontab(cron_str),
                id=str(task_id),
                replace_existing=True
            )
            
            # 更新内存缓存 (用于 API 返回列表)
            self.active_tasks[str(task_id)] = {
                "id": task_id,
                "name": name,
                "cron": cron_str,
                "preset": preset_filename,
                "targets": targets,
                "mode": mode,
                "enabled": True
            }
            
            logger.info(f"[TaskService] 任务添加成功: {name} - {cron_str}")
        except Exception as e:
            logger.error(f"[TaskService] 任务添加失败: {e}")
            traceback.print_exc()

    def remove_task(self, task_id):
        """删除任务"""
        try:
            self.scheduler.remove_job(str(task_id))
            if str(task_id) in self.active_tasks:
                del self.active_tasks[str(task_id)]
            logger.info(f"[TaskService] 任务删除成功: {task_id}")
        except Exception as e:
            logger.error(f"[TaskService] 删除任务时出错 {task_id}: {e}")

    # ==========================================
    # 🆕 115 清理任务调度逻辑
    # ==========================================
    def refresh_cleanup_jobs(self):
        """
        读取 302 配置，重新注册所有 115 清理任务（主号 + 小号）
        """
        # 1. 先移除所有旧的清理任务
        for job_id in self.cleanup_job_ids:
            try:
                self.scheduler.remove_job(job_id)
            except: pass
        self.cleanup_job_ids = []

        # 2. 读取配置
        config_path = "config/config_302.json"
        if not os.path.exists(config_path):
            return

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            drives = data.get("drives", [])

            logger.info(f"[启动] 刷新 115 清理任务: 配置 {len(drives)} 个")

            job_counter = 0
            for idx, drive in enumerate(drives):
                # 检查是否启用自动删除
                if not drive.get("auto_delete", False):
                    continue

                cron_exp = drive.get("delete_cron", "30 3 * * *") # 默认凌晨3:30
                drive_name = drive.get("name", f"主号{idx + 1}")

                # ===== 主号清理任务 =====
                job_id = f"cleanup_main_{idx}"
                self._add_cleanup_job(job_id, drive, "main", 0, cron_exp, drive_name)
                job_counter += 1

                # ===== 小号清理任务（如果启用了秒传）=====
                rapid_accounts = drive.get("rapid_accounts", [])
                if rapid_accounts and drive.get("enable_rapid", False):
                    for r_idx, rapid_acc in enumerate(rapid_accounts):
                        # 只有配置了 cookie 的小号才添加清理任务
                        if rapid_acc.get("cookie"):
                            r_job_id = f"cleanup_rapid_{idx}_{r_idx}"
                            r_name = rapid_acc.get("name", f"小号{r_idx + 1}")
                            self._add_cleanup_job(r_job_id, drive, "rapid", r_idx, cron_exp, r_name)
                            job_counter += 1

            logger.info(f"[启动] 115 清理任务已加载: {job_counter} 个")

        except Exception as e:
            logger.error(f"[启动] 读取 302 配置失败: {e}")

    def _add_cleanup_job(self, job_id: str, drive_config: dict, account_type: str, account_index: int, cron_exp: str, name: str):
        """添加单个清理任务到调度器"""
        def cleanup_wrapper():
            try:
                # 尝试获取当前线程的事件循环
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                # 执行异步任务
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        drive115_service.execute_cleanup_task(drive_config, account_type, account_index),
                        loop
                    )
                else:
                    loop.run_until_complete(drive115_service.execute_cleanup_task(drive_config, account_type, account_index))

            except Exception as e:
                logger.error(f"[Cleanup] 任务执行异常: {e}")

        try:
            self.scheduler.add_job(
                cleanup_wrapper,
                CronTrigger.from_crontab(cron_exp),
                id=job_id,
                replace_existing=True
            )
            self.cleanup_job_ids.append(job_id)
        except Exception as e:
            logger.error(f"[启动] 清理任务添加失败: {e}")

    def refresh_selected_cleanup_jobs(self):
        for job_id in self.selected_cleanup_job_ids:
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass
        self.selected_cleanup_job_ids = []

        config_path = "config/drive115_cleanup_tasks.json"
        if not os.path.exists(config_path):
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            if not isinstance(tasks, list):
                tasks = []

            loaded = 0
            for task in tasks:
                if not task.get("enabled", True):
                    continue
                task_id = str(task.get("id") or "").strip()
                cron_exp = str(task.get("cron") or "").strip()
                if not task_id or not cron_exp:
                    continue
                self._add_selected_cleanup_job(f"selected_cleanup_{task_id}", task)
                loaded += 1
            logger.info(f"[启动] 115 定时清空任务已加载: {loaded} 个")
        except Exception as e:
            logger.error(f"[启动] 读取 115 定时清空任务失败: {e}")

    def _update_selected_cleanup_task_result(self, task_id: str, result: dict):
        config_path = "config/drive115_cleanup_tasks.json"
        if not os.path.exists(config_path):
            return
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            if not isinstance(tasks, list):
                return
            for task in tasks:
                if str(task.get("id") or "") == str(task_id):
                    task["last_run_at"] = int(time.time())
                    task["last_status"] = result.get("status")
                    task["last_message"] = result.get("message")
                    task["last_deleted_count"] = int(result.get("deleted_count") or 0)
                    break
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[CleanUp] 更新定时清空任务结果失败: {e}")

    def run_selected_cleanup_task(self, task: dict, manual: bool = False):
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            return {"status": "error", "message": "任务 ID 为空", "deleted_count": 0}
        if task_id in self.selected_cleanup_running:
            logger.warning(f"[CleanUp] 定时清空任务正在运行，跳过: {task.get('name') or task_id}")
            return {"status": "skipped", "message": "任务正在运行", "deleted_count": 0}

        self.selected_cleanup_running.add(task_id)
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    drive115_service.execute_selected_folder_cleanup_task(task, manual=manual),
                    loop,
                )
                result = future.result()
            else:
                result = loop.run_until_complete(drive115_service.execute_selected_folder_cleanup_task(task, manual=manual))
            self._update_selected_cleanup_task_result(task_id, result)
            return result
        except Exception as e:
            logger.error(f"[CleanUp] 定时清空任务执行异常: {e}")
            result = {"status": "error", "message": str(e), "deleted_count": 0}
            self._update_selected_cleanup_task_result(task_id, result)
            return result
        finally:
            self.selected_cleanup_running.discard(task_id)

    def _add_selected_cleanup_job(self, job_id: str, task: dict):
        def cleanup_wrapper():
            self.run_selected_cleanup_task(task, manual=False)

        try:
            self.scheduler.add_job(
                cleanup_wrapper,
                CronTrigger.from_crontab(str(task.get("cron") or "")),
                id=job_id,
                replace_existing=True,
            )
            self.selected_cleanup_job_ids.append(job_id)
        except Exception as e:
            logger.error(f"[启动] 115 定时清空任务添加失败: {e}")

    def schedule_daily_signin_job(self):
        next_run_time = self._compute_next_signin_datetime()

        try:
            self.scheduler.remove_job(self.daily_signin_job_id)
        except Exception:
            pass

        self.scheduler.add_job(
            self._run_daily_signin_and_reschedule,
            trigger="date",
            run_date=next_run_time,
            id=self.daily_signin_job_id,
            replace_existing=True
        )
        logger.info(f"[SignIn] 下次 115 自动签到时间: {next_run_time.strftime('%Y-%m-%d %H:%M:%S')}")

    def _compute_next_signin_datetime(self):
        now = datetime.now()
        base_date = now.date()

        for day_offset in (0, 1):
            candidate_date = base_date + timedelta(days=day_offset)
            candidate = datetime.combine(candidate_date, datetime.min.time()) + timedelta(
                minutes=random.randint(0, 59),
                seconds=random.randint(0, 59)
            )
            if candidate > now:
                return candidate

        candidate_date = base_date + timedelta(days=1)
        return datetime.combine(candidate_date, datetime.min.time()) + timedelta(
            minutes=random.randint(0, 59),
            seconds=random.randint(0, 59)
        )

    def _run_daily_signin_and_reschedule(self):
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    drive115_service.execute_all_signin_tasks(trigger="scheduler"),
                    loop
                )
                future.result()
            else:
                loop.run_until_complete(drive115_service.execute_all_signin_tasks(trigger="scheduler"))
        except Exception as e:
            logger.error(f"[SignIn] 定时签到执行异常: {e}")
        finally:
            try:
                self.schedule_daily_signin_job()
            except Exception as e:
                logger.error(f"[SignIn] 重新注册下一次签到失败: {e}")

# ==========================================
# 3. 实例化对象 (供 main.py 导入)
# ==========================================
task_service_instance = TaskService()