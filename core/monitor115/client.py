import json
import os
import threading
from time import time

from p115client import P115Client, check_response
from p115client.tool.attr import get_path
from p115client.tool.life import BEHAVIOR_NAME_TO_TYPE, IGNORE_BEHAVIOR_TYPES

from core.logger import logger


class LifeClient:

    def __init__(self, client: P115Client, monitor_dirs: list[str],
                 path_map_file: str = ""):
        self._client = client
        self._monitor_dirs = [d.rstrip("/") for d in monitor_dirs if d]
        self._id_to_dirnode: dict[int, tuple[str, int]] = {}
        self._path_cache: dict[int, str] = {}
        self._prev_path_cache: dict[int, str] = {}

        # 持久化 file_id → last_known_path 映射
        self._path_map_lock = threading.Lock()
        self._file_id_path_map: dict[str, dict] = {}  # {file_id: {"path": str, "ts": float}}
        if path_map_file:
            self._path_map_file = path_map_file
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            self._path_map_file = os.path.join(base_dir, "config", "115_file_path_map.json")
        self._load_path_map()

    # ──────────────────────────────────────────────
    # 持久化路径映射
    # ──────────────────────────────────────────────

    def _load_path_map(self):
        try:
            if os.path.exists(self._path_map_file):
                with open(self._path_map_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    self._file_id_path_map = raw
                    logger.trace(f"[115Life] 已加载路径映射: {len(self._file_id_path_map)} 条")
        except Exception as e:
            logger.warning(f"[115Life] 路径映射加载失败: {e}")
            self._file_id_path_map = {}

    def save_path_map(self):
        """持久化路径映射到磁盘（由外部定期调用）"""
        with self._path_map_lock:
            try:
                # LRU: 超过 50000 条时保留最新的一半
                data = self._file_id_path_map
                if len(data) > 50000:
                    sorted_items = sorted(data.items(), key=lambda x: x[1].get("ts", 0), reverse=True)
                    data = dict(sorted_items[:30000])
                    self._file_id_path_map = data
                    logger.info(f"[115Life] 路径映射 LRU 裁剪至 {len(data)} 条")

                os.makedirs(os.path.dirname(self._path_map_file), exist_ok=True)
                tmp = self._path_map_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, self._path_map_file)
            except Exception as e:
                logger.debug(f"[115Life] 路径映射保存失败: {e}")

    def record_path(self, file_id: int, path: str):
        """记录 file_id → path 映射（仅在路径变化时写入）"""
        if not file_id or not path:
            return
        fid = str(file_id)
        with self._path_map_lock:
            old = self._file_id_path_map.get(fid, {})
            if old.get("path") == path:
                return  # 路径未变，跳过
            self._file_id_path_map[fid] = {"path": path, "ts": time()}

    def get_known_path(self, file_id: int) -> str:
        """获取 file_id 的历史路径"""
        if not file_id:
            return ""
        entry = self._file_id_path_map.get(str(file_id))
        return entry.get("path", "") if entry else ""

    def remove_path(self, file_id: int):
        """删除 file_id 的路径映射"""
        if not file_id:
            return
        with self._path_map_lock:
            self._file_id_path_map.pop(str(file_id), None)

    # ──────────────────────────────────────────────
    # 115 API 交互
    # ──────────────────────────────────────────────

    def check_status(self) -> bool:
        try:
            resp = self._client.life_calendar_getoption(app="web", timeout=10)
            check_response(resp)
            return True
        except Exception as e:
            logger.error(f"[115Life] 生活事件状态检查失败: {e}")
            return False

    def once_pull(self, from_time: float = 0, from_id: int = 0,
                  stop_event=None) -> tuple[list[dict], float, int]:
        """使用 life_list 接口单次拉取生活事件，返回 (events, new_from_time, new_from_id)"""
        end_time = int(time())
        if from_time == 0:
            from_time = float(end_time - 2)

        events_batch: list[dict] = []

        for attempt in range(3, -1, -1):
            try:
                events_batch = []
                resp = self._client.life_list(
                    {"show_type": 0, "start_time": int(from_time), "end_time": end_time},
                    app="web",
                )
                data = check_response(resp)["data"]
                if data.get("count"):
                    for group in data.get("list", []):
                        if "items" not in group:
                            ut = group.get("update_time", 0)
                            if ut and from_time < ut < end_time:
                                events_batch.append(group)
                            continue
                        behavior_type = group["behavior_type"]
                        date = group["date"]
                        behavior_type_code = BEHAVIOR_NAME_TO_TYPE.get(behavior_type, 0)
                        if behavior_type_code in IGNORE_BEHAVIOR_TYPES:
                            continue
                        for item in group["items"]:
                            item_id = int(item.get("id", 0))
                            if item_id and item_id <= from_id:
                                continue
                            item["behavior_type"] = behavior_type
                            item["event_name"] = behavior_type
                            if behavior_type_code:
                                item["type"] = behavior_type_code
                            item["date"] = date
                            events_batch.append(item)
                break
            except Exception as e:
                if attempt <= 0:
                    raise
                logger.warning(f"[115Life] 拉取数据失败，剩余重试次数 {attempt} 次: {e}")
                if stop_event and stop_event.wait(timeout=2):
                    return [], from_time, from_id

        if not events_batch:
            return [], float(end_time), from_id

        new_from_time = from_time
        new_from_id = from_id
        for event in reversed(events_batch):
            eid = int(event.get("id", 0))
            ut = event.get("update_time")
            if eid > new_from_id:
                new_from_id = eid
            if ut and float(ut) > new_from_time:
                new_from_time = float(ut)

        return events_batch, new_from_time, new_from_id

    def _resolve_path_by_id(self, node_id: int, refresh: bool = False) -> str:
        if not refresh and node_id in self._path_cache:
            return self._path_cache[node_id]
        try:
            path = get_path(self._client, node_id, id_to_dirnode=self._id_to_dirnode, escape=False)
            if path:
                self._path_cache[node_id] = path
            return path
        except Exception:
            return self._path_cache.get(node_id, "")

    def resolve_path(self, event: dict) -> str:
        event_name = str(event.get("event_name", "") or "")
        move_like_events = {"move_file", "move_image_file"}
        delete_like_events = {"delete_file", "delete_image_file"}

        file_id = int(event.get("file_id", 0) or 0)

        # ── 1. 从持久化映射获取旧路径（在任何更新之前读取）──
        old_path_from_map = self.get_known_path(file_id) if file_id else ""
        if old_path_from_map:
            event["_old_path_from_map"] = old_path_from_map

        # ── 2. 直接从事件字段获取路径 ──
        if event.get("file_path"):
            path = event["file_path"]
            if file_id:
                self.record_path(file_id, path)
                self._prev_path_cache[file_id] = path
                self._path_cache[file_id] = path
            return path

        if event.get("path"):
            path = event["path"]
            if file_id:
                self.record_path(file_id, path)
                self._prev_path_cache[file_id] = path
                self._path_cache[file_id] = path
            return path

        # ── 3. 从缓存和 API 解析路径 ──
        prev_path = self._prev_path_cache.get(file_id, "") if file_id else ""
        cached_file_path = self._path_cache.get(file_id, "") if file_id else ""

        # 如果内存缓存没有旧路径，但持久化映射有，使用持久化映射的
        if not prev_path and not cached_file_path and old_path_from_map:
            prev_path = old_path_from_map

        parent_id = int(event.get("cid", event.get("parent_id", 0)) or 0)
        parent_name = str(event.get("parent_name", "") or "")
        file_name = str(event.get("file_name", "") or "").strip("/")

        refresh_for_move = event_name in move_like_events

        parent_path = ""
        guessed_path = ""
        if parent_id:
            parent_path = self._resolve_path_by_id(parent_id, refresh=refresh_for_move)
            if parent_path:
                if not file_name:
                    guessed_path = parent_path
                elif parent_path == "/":
                    guessed_path = f"/{file_name}"
                else:
                    guessed_path = f"{parent_path.rstrip('/')}/{file_name}"
        elif refresh_for_move:
            parent_path = "/"
            guessed_path = f"/{file_name}" if file_name else "/"

        file_path_by_id = ""
        if file_id:
            file_path_by_id = self._resolve_path_by_id(file_id, refresh=refresh_for_move)

        current_path = ""
        if refresh_for_move and guessed_path:
            current_path = guessed_path
            if file_path_by_id and file_path_by_id != guessed_path:
                event["_resolved_file_path_by_id"] = file_path_by_id
            event["_resolved_current_path_source"] = "parent"
        elif file_path_by_id and guessed_path and file_path_by_id != guessed_path:
            current_path = file_path_by_id
        elif file_path_by_id:
            current_path = file_path_by_id
        elif guessed_path:
            current_path = guessed_path

        if current_path:
            if file_id:
                # _resolved_prev_path: 从内存缓存获取旧路径
                prev_candidates = [p for p in (prev_path, cached_file_path) if p]
                prev_candidate = prev_candidates[0] if prev_candidates else ""
                if prev_candidate and prev_candidate != current_path:
                    event["_resolved_prev_path"] = prev_candidate
                # 补充: 如果内存缓存旧路径与持久化映射不同，也记录持久化映射的
                if old_path_from_map and old_path_from_map != current_path and old_path_from_map != prev_candidate:
                    if not event.get("_resolved_prev_path"):
                        event["_resolved_prev_path"] = old_path_from_map
                    event["_old_path_from_map"] = old_path_from_map

            event["_resolved_current_path"] = current_path
            if file_id:
                self._prev_path_cache[file_id] = current_path
                self._path_cache[file_id] = current_path
                self.record_path(file_id, current_path)
            return current_path

        if file_id and event_name in (move_like_events | delete_like_events):
            if prev_path:
                event["_resolved_prev_path"] = prev_path
                return prev_path

        if file_id:
            logger.debug(
                f"[115Life] 路径解析失败: file_id={file_id}, event_name={event_name}, "
                f"file_name={event.get('file_name', '')}, parent_id={parent_id}, parent_name={parent_name}"
            )
        return ""

    def resolve_move_direction(self, prev_path: str, current_path: str) -> str:
        """根据旧路径和当前路径判断移动方向

        Returns:
            "outside_to_inside" | "inside_to_outside" | "inside_to_inside" | "outside" | "unknown"
        """
        prev_in = prev_path and self.is_in_monitor_dirs(prev_path)
        curr_in = current_path and self.is_in_monitor_dirs(current_path)

        if prev_in and curr_in:
            return "inside_to_inside"
        elif prev_in and not curr_in:
            return "inside_to_outside"
        elif not prev_in and curr_in:
            return "outside_to_inside"
        elif prev_path and current_path:
            return "outside"
        else:
            return "unknown"

    def is_in_monitor_dirs(self, file_path: str) -> bool:
        for d in self._monitor_dirs:
            if file_path == d or file_path.startswith(d + "/"):
                return True
        return False

    @staticmethod
    def classify_path(path: str, source_dir: str, target_dir: str) -> str:
        """将路径分类为 'media_lib' / 'source' / 'other'"""
        dirs = sorted(
            [(d, cat) for d, cat in [(target_dir, "media_lib"), (source_dir, "source")] if d],
            key=lambda x: len(x[0]),
            reverse=True,
        )
        for d, cat in dirs:
            if path == d or path.startswith(d + "/"):
                return cat
        return "other"

    @staticmethod
    def resolve_direction_9(file_id: str, curr_path: str, source_dir: str, target_dir: str) -> str:
        """返回 'media_lib→source' 等方向标签，移动前分类仅按 file_id 查缓存"""
        if not curr_path:
            return ""
        prev_cat = "other"
        if file_id:
            from core.media_library_cache import get_item_by_id
            cached = get_item_by_id(file_id)
            cached_path = str(((cached or {}).get("item") or {}).get("path", "") or "")
            if cached_path:
                prev_cat = LifeClient.classify_path(cached_path, source_dir, target_dir)
        curr_cat = LifeClient.classify_path(curr_path, source_dir, target_dir)
        return f"{prev_cat}→{curr_cat}"

    def clear_path_cache(self):
        self._path_cache.clear()
        self._prev_path_cache.clear()
