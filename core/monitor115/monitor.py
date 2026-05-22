import json
import os
import threading
from time import time
from typing import Callable, Optional

from p115client.exception import P115AuthenticationError

from core.logger import logger
from app.services.media_organize_state import _is_recent_created_target_dir_id, _record_self_organized_event_skip
from .client import LifeClient
from .models import LifeEvent


life_event_monitor: Optional["LifeEventMonitor"] = None


class LifeEventMonitor:

    def __init__(
        self,
        client,
        source_dir: str,
        target_dir: str,
        callback: Optional[Callable[[str, str, str, str], None]] = None,
        start_mode: str = "latest",
        state_file: Optional[str] = None,
        poll_interval: float = 20,
    ):
        monitor_dirs = [d for d in [source_dir, target_dir] if d]

        if state_file is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            state_file = os.path.join(base_dir, "config", "115_life_monitor_state.json")
        self._state_file = state_file
        path_map_file = os.path.join(os.path.dirname(state_file), "115_file_path_map.json")

        self._life_client = LifeClient(client, monitor_dirs, path_map_file=path_map_file)
        self._source_dir = source_dir
        self._target_dir = target_dir
        self._callback = callback
        self._start_mode = start_mode
        self._poll_interval = poll_interval

        self._running = False
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._guard_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self._from_id: int = 0
        self._from_time: float = 0.0
        self._consecutive_failures: int = 0

    def start(self) -> bool:
        with self._lock:
            if self._running:
                logger.warning("[115Life] 监控已在运行中")
                return True

            logger.debug("[115Life] 生活事件状态检查中...")
            if not self._life_client.check_status():
                logger.error("[115Life] 生活事件状态检查失败，无法启动监控")
                return False
            logger.debug("[115Life] 生活事件状态检查通过")

            self._from_id = 0
            self._from_time = 0.0
            if self._start_mode == "last":
                self._load_state()
            elif self._start_mode == "latest":
                self._from_time = time()

            if self._start_mode == "latest":
                logger.trace("[115Life] 监控起始点: 从当前时间开始，只处理新事件")
            elif self._start_mode == "last":
                logger.trace("[115Life] 监控起始点: 从上次保存的位置继续")
            else:
                logger.debug(f"[115Life] 监控起始点: mode={self._start_mode}, from_id={self._from_id}, from_time={self._from_time}")

            self._running = True
            self._stop_event.clear()
            self._consecutive_failures = 0

            self._worker_thread = threading.Thread(target=self._worker_loop, name="115-life-poll", daemon=True)
            self._worker_thread.start()

            self._guard_thread = threading.Thread(target=self._guard_loop, name="115-life-guard", daemon=True)
            self._guard_thread.start()

            logger.info(f"[115Life] 监控已启动: 整理目录={self._source_dir}, 媒体库目录={self._target_dir}")
            return True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            logger.info("[115Life] 正在停止监控...")
            self._running = False
            self._stop_event.set()
            self._save_state()
            self._life_client.save_path_map()
            if self._worker_thread and self._worker_thread.is_alive():
                self._worker_thread.join(timeout=10)
            if self._guard_thread and self._guard_thread.is_alive():
                self._guard_thread.join(timeout=5)
            self._worker_thread = None
            self._guard_thread = None
            logger.info("[115Life] 监控已停止")

    def _worker_loop(self) -> None:
        logger.debug("[115Life] 轮询线程已启动")
        try:
            while not self._stop_event.is_set():
                try:
                    events, new_from_time, new_from_id = self._life_client.once_pull(
                        from_time=self._from_time,
                        from_id=self._from_id,
                        stop_event=self._stop_event,
                    )
                    if events:
                        logger.debug(f"[115Life] 本轮拉取到 {len(events)} 条事件")
                    self._from_time = new_from_time
                    if new_from_id > self._from_id:
                        self._from_id = new_from_id

                    for event in events:
                        event_name = str(event.get("event_name", "") or "")
                        event_type = int(event.get("type", 0) or 0)
                        parent_id = str(event.get("parent_id", "") or "")

                        try:
                            logger.trace(f"[115Life] 原始事件: {json.dumps(event, ensure_ascii=False, sort_keys=True)}")
                        except Exception:
                            logger.trace(f"[115Life] 原始事件(无法序列化): {event}")

                        if event_name == "delete_file":
                            event_kind = "delete"
                        elif event_name in ("move_file", "move_image_file") or event_type in (5, 6):
                            event_kind = "move"
                        elif event_name in ("folder_rename", "file_rename") or event_type in (20, 24):
                            event_kind = "rename"
                        elif event_name in ("upload_file", "upload_image_file", "receive_files", "new_folder", "add_folder", "copy_folder", "copy_file") or event_type in (1, 2, 14, 17, 18, 23):
                            event_kind = "add"
                        else:
                            event_kind = "other"

                        is_delete_like = event_kind == "delete"
                        is_move_like = event_kind == "move"

                        if event_kind == "other":
                            continue

                        if event_name == "new_folder" and _is_recent_created_target_dir_id(str(event.get("file_id", "") or "")):
                            _record_self_organized_event_skip(event_name)
                            continue

                        if is_delete_like:
                            life_event = LifeEvent.from_raw(event, "")
                            self._dispatch(life_event)
                            continue

                        if is_move_like:
                            effective_path = str(event.get("file_path") or event.get("path") or "")
                            event["_effective_path"] = effective_path
                            life_event = LifeEvent.from_raw(event, effective_path)
                            self._dispatch(life_event)
                            continue

                        resolved_path = self._life_client.resolve_path(event)
                        if not resolved_path:
                            # 路径解析失败：尝试从持久化映射获取旧路径来判定方向
                            old_from_map = str(event.get("_old_path_from_map", "") or "")

                            if is_move_like and parent_id in ("", "0"):
                                if old_from_map:
                                    direction = self._life_client.resolve_move_direction(old_from_map, "")
                                    if direction in ("inside_to_outside", "inside_to_inside", "outside_to_inside"):
                                        event["_path_direction"] = direction
                                        event["_effective_path"] = old_from_map
                                        logger.info(
                                            f"[115Life] 根目录移动事件通过持久化映射判定方向: "
                                            f"direction={direction}, _resolved_prev_path={old_from_map}, "
                                            f"file_id={event.get('file_id', '')}, file_name={event.get('file_name', '')}, "
                                            f"sha1={str(event.get('sha1', '') or '')[:12]}"
                                        )
                                        life_event = LifeEvent.from_raw(event, event["_effective_path"])
                                        self._dispatch(life_event)
                                        continue

                                event["_path_direction"] = "root_move"
                                event["_effective_path"] = ""
                                logger.debug(
                                    f"[115Life] 根目录移动事件路径解析失败(无历史映射)，交给回调优先路径再按需SHA1判定: "
                                    f"event={event_name}, file_id={event.get('file_id', '')}, "
                                    f"sha1={str(event.get('sha1', '') or '')[:12]}"
                                )
                                life_event = LifeEvent.from_raw(event, event["_effective_path"])
                                self._dispatch(life_event)
                                continue

                            logger.debug(
                                f"[115Life] 路径解析为空: event_id={event.get('id')}, "
                                f"event_name={event.get('event_name', '')}, "
                                f"type={event.get('type', '')}, "
                                f"cid={event.get('cid', event.get('parent_id', ''))}, "
                                f"file_id={event.get('file_id', '')}, file_name={event.get('file_name', '')}"
                            )
                            continue

                        current_path = str(event.get("_resolved_current_path", "") or resolved_path)
                        prev_path = str(event.get("_resolved_prev_path", "") or "")
                        old_from_map = str(event.get("_old_path_from_map", "") or "")

                        # 对 move 事件，优先用持久化映射的旧路径（比内存缓存更可靠）
                        effective_prev_path = prev_path
                        if is_move_like and old_from_map and old_from_map != current_path:
                            effective_prev_path = old_from_map
                            if not prev_path:
                                event["_resolved_prev_path"] = old_from_map

                        in_current = self._life_client.is_in_monitor_dirs(current_path)
                        in_prev = effective_prev_path and self._life_client.is_in_monitor_dirs(effective_prev_path)

                        if not in_current and not in_prev:
                            # 路径不在任何监控目录内，跳过（9类方向已覆盖所有有效变动）
                            if is_move_like and parent_id in ("", "0"):
                                # 用持久化映射做最后的方向判定尝试
                                if old_from_map:
                                    direction = self._life_client.resolve_move_direction(old_from_map, current_path)
                                    if direction in ("inside_to_outside", "inside_to_inside", "outside_to_inside"):
                                        event["_path_direction"] = direction
                                        event["_effective_path"] = current_path or old_from_map
                                        logger.info(
                                            f"[115Life] 根目录移动(路径不在监控区)通过持久化映射判定方向: "
                                            f"direction={direction}, _resolved_prev_path={old_from_map}, "
                                            f"_resolved_current_path={current_path}, "
                                            f"file_id={event.get('file_id', '')}, file_name={event.get('file_name', '')}"
                                        )
                                        life_event = LifeEvent.from_raw(event, event["_effective_path"])
                                        self._dispatch(life_event)
                                        continue

                                event["_path_direction"] = "root_move"
                                event["_effective_path"] = current_path or resolved_path
                                logger.debug(
                                    f"[115Life] 移动到根目录事件(无历史映射)，交给回调优先路径再按需SHA1判定: "
                                    f"resolved={resolved_path}, current={current_path}, file_id={event.get('file_id', '')}, "
                                    f"sha1={str(event.get('sha1', '') or '')[:12]}"
                                )
                                life_event = LifeEvent.from_raw(event, event["_effective_path"])
                                self._dispatch(life_event)
                                continue

                            logger.debug(
                                f"[115Life] 跳过非监控目录删除事件: resolved={resolved_path}, file_id={event.get('file_id', '')}, "
                                f"file_name={event.get('file_name', '')}, parent_id={event.get('parent_id', '')}"
                            )
                            continue

                        event["_effective_path"] = current_path if in_current else effective_prev_path
                        if not is_move_like:
                            if in_current and in_prev:
                                event["_path_direction"] = "inside_to_inside"
                            elif in_prev and not in_current:
                                event["_path_direction"] = "inside_to_outside"
                            elif in_current and not in_prev:
                                event["_path_direction"] = "outside_to_inside"
                            else:
                                event["_path_direction"] = "inside"

                        # 对 move 事件计算9类精准方向标签
                        if is_move_like:
                            dir9 = self._life_client.resolve_direction_9(
                                str(event.get("file_id", "") or ""),
                                current_path,
                                self._source_dir,
                                self._target_dir,
                            )
                            if dir9:
                                event["_path_direction"] = dir9

                        if is_move_like:
                            logger.debug(
                                f"[115Life] 移动方向判定: path_direction={event.get('_path_direction')}, "
                                f"_resolved_current_path={current_path}, _resolved_prev_path={effective_prev_path}, "
                                f"event={event_name}, file_id={event.get('file_id', '')}, file_name={event.get('file_name', '')}"
                            )

                        life_event = LifeEvent.from_raw(event, event["_effective_path"])
                        self._dispatch(life_event)

                except P115AuthenticationError:
                    logger.error("[115Life] 登录已过期，停止监控")
                    return
                except Exception as e:
                    logger.error(f"[115Life] 轮询异常: {e}")
                    if self._stop_event.wait(timeout=30):
                        return
                    continue

                if self._stop_event.wait(timeout=self._poll_interval):
                    return

            logger.info("[115Life] 轮询线程已退出")
        except Exception as e:
            logger.error(f"[115Life] 轮询线程运行异常: {e}")
            raise

    def _dispatch(self, event: LifeEvent) -> None:
        if self._callback:
            try:
                try:
                    self._callback(
                        event.file_path,
                        event.file_id,
                        event.event_name,
                        event.event_type_cn,
                        event.file_cid,
                        event.file_name,
                        event.raw,
                    )
                except TypeError:
                    try:
                        self._callback(
                            event.file_path,
                            event.file_id,
                            event.event_name,
                            event.event_type_cn,
                            event.file_cid,
                            event.file_name,
                        )
                    except TypeError:
                        self._callback(event.file_path, event.file_id, event.event_name, event.event_type_cn)
            except Exception as e:
                logger.error(f"[115Life] 回调执行异常: {e}")

    def _guard_loop(self) -> None:
        logger.debug("[115Life] 守护线程已启动")
        save_counter = 0
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=60):
                break
            if not self._running:
                continue
            if self._worker_thread and self._worker_thread.is_alive():
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                logger.warning(f"[115Life] Worker 线程已停止 (连续 {self._consecutive_failures} 次检测)")
                if self._consecutive_failures >= 5:
                    logger.warning("[115Life] Worker 挂掉超过 5 分钟，尝试自动重启...")
                    try:
                        self._stop_event.clear()
                        self._worker_thread = threading.Thread(target=self._worker_loop, name="115-life-poll", daemon=True)
                        self._worker_thread.start()
                        self._consecutive_failures = 0
                        logger.info("[115Life] Worker 已自动重启")
                    except Exception as e:
                        logger.error(f"[115Life] 自动重启失败: {e}")
            save_counter += 1
            if save_counter >= 5:
                save_counter = 0
                self._save_state()
                self._life_client.save_path_map()
        logger.info("[115Life] 守护线程已退出")

    def _save_state(self) -> None:
        try:
            state = {"from_id": self._from_id, "from_time": self._from_time, "saved_at": int(time())}
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[115Life] 状态保存失败: {e}")

    def _load_state(self) -> None:
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                saved_from_id = state.get("from_id", 0)
                saved_from_time = state.get("from_time", 0)
                if saved_from_id or saved_from_time:
                    self._from_id = int(saved_from_id)
                    self._from_time = float(saved_from_time)
                    logger.debug(f"[115Life] 已恢复上次状态: from_id={self._from_id}, from_time={self._from_time}")
        except Exception as e:
            logger.warning(f"[115Life] 状态加载失败: {e}")

    @property
    def is_running(self) -> bool:
        return self._running

    def set_callback(self, callback: Callable[[str, str, str], None]) -> None:
        self._callback = callback


def create_monitor(client, source_dir: str, target_dir: str, **kwargs) -> LifeEventMonitor:
    global life_event_monitor
    life_event_monitor = LifeEventMonitor(client=client, source_dir=source_dir, target_dir=target_dir, **kwargs)
    return life_event_monitor
