import os
import json
import httpx
import asyncio
import traceback
import inspect
import re
import time
import random
import threading
from cachetools import TTLCache
from p115client import P115Client
from app.services.media_organize_115_ops import (
    _run_115_serial_request,
    run_115_read_request,
    run_115_write_request,
    DIRECT_URL_PRIORITY_DEFAULT,
    DIRECT_URL_PRIORITY_DIRECT,
    DIRECT_URL_PRIORITY_PLAYBACK,
)
from p115pickcode import to_id
from p115client.tool.attr import get_attr
from app.routers.config_302 import get_config_302
from app.services.wechat_service import wechat_notify_service
from app.services.telegram_service import telegram_notify_service
from core.logger import logger
from app.services.realtime_events import publish_realtime_event

_115_READ_REQUEST_TIMEOUT_SECONDS = 10
_DIRECT_URL_DOWNLOAD_TIMEOUT_SECONDS = 10
_RAPID_RANGE_READ_TIMEOUT_SECONDS = 15
_RAPID_UPLOAD_INIT_TIMEOUT_SECONDS = 30
_GATEWAY_DIRECT_URL_REQUEST_TIMEOUT_SECONDS = 12
_GATEWAY_DIRECT_URL_TIMEOUT_MAX_RETRIES = 1
_GATEWAY_DIRECT_LOW_PRIORITY_GRACE_SECONDS = 0.15
_GATEWAY_DIRECT_PRIORITY_PLAYBACK = 0
_GATEWAY_DIRECT_PRIORITY_DIRECT = 10
_115_APP_LIST_SAFE_PAGE_SIZE = 7000
_115_CLEANUP_DELETE_BATCH_SIZE = 1150
_115_CLEANUP_DELETE_BATCH_PAUSE_SECONDS = 3.0
_115_CLEANUP_DELETE_TIMEOUT_SECONDS = 300
_115_CLEANUP_DELETE_RETRY_DELAYS_SECONDS = (0.0, 3.0, 10.0)
_115_CLEANUP_PRE_DELETE_DELAY_SECONDS = 5.0
_115_CLEANUP_VERIFY_DELAYS_SECONDS = (3.0, 15.0, 30.0, 60.0)

class Drive115Service:
    def __init__(self):
        # 支持多个 115 账号的 client 缓存
        self._clients = {}  # {cookie: client}
        self._cookies = {}  # {cookie: drive_index}

        # === [第一级缓存] ID -> Pickcode (超级直通车) ===
        # 命中这个，直接跳过 Emby 路径查询和 115 文件搜索
        self._id_cache = TTLCache(maxsize=5000, ttl=3600)

        # === [第二级缓存] ID -> Emby物理路径 ===
        # 路径基本不变，缓存24小时
        self._emby_path_cache = TTLCache(maxsize=5000, ttl=86400)

        # === [第三级缓存] Path -> Pickcode ===
        # 知道了路径，不用去 115 搜索，直接拿 Pickcode
        self._path_cache = TTLCache(maxsize=5000, ttl=3600)

        # === [第四级缓存] Pickcode_UA -> 直链URL ===
        self._url_cache = TTLCache(maxsize=1000, ttl=1200) # 20分钟有效
        self._url_cache_hit_log_dedupe = TTLCache(maxsize=2000, ttl=10)

        # === [第五级缓存] Pickcode -> SHA1信息（用于秒传） ===
        self._sha1_cache = TTLCache(maxsize=1000, ttl=3600)  # SHA1 缓存

        # === [秒传播放调度] 原始 Pickcode + Emby 用户 -> 小号线路 ===
        self._rapid_assignments = TTLCache(maxsize=2000, ttl=7200)

        # === [小号池轮询计数器] ===
        self._rapid_account_index = 0  # 当前轮询到的账号索引

        # 并发锁
        self._item_locks = {}
        self._locks_cleanup_lock = asyncio.Lock()
        self._direct_url_batch_lock = threading.Lock()
        self._gateway_direct_url_sequence = 0
        self._gateway_direct_url_sequence_lock = threading.Lock()
        self._gateway_direct_url_queues: dict[int, asyncio.PriorityQueue] = {}
        self._gateway_direct_url_workers: dict[int, asyncio.Task] = {}
        self._last_gateway_direct_url_at: dict[int, float] = {}
        self._topology_sessions: dict[int, dict] = {}
        self._topology_poll_tasks: dict[int, asyncio.Task] = {}

        self._last_direct_url_batch_at = 0.0

        # 每个事件循环单独持有 HTTP 客户端，避免 UI loop 和网关 loop 交叉使用同一个 AsyncClient。
        self._http_clients: dict[int, httpx.AsyncClient] = {}
        self._http_clients_lock = threading.Lock()

    async def get_client(self, emby_index: int = 0):
        """获取或初始化主 115 账号客户端。"""
        cfg = await get_config_302()

        drives = cfg.get("drives", [])
        if isinstance(drives, list) and drives:
            drive_cfg = drives[emby_index] if 0 <= emby_index < len(drives) else drives[0]
        else:
            drive_cfg = cfg.get("drive", {})

        cookie = str(drive_cfg.get("cookie", "") or "").strip() if isinstance(drive_cfg, dict) else ""
        drive_name = drive_cfg.get("name", "115") if isinstance(drive_cfg, dict) else "115"

        if not cookie:
            logger.warning(f"[115] {drive_name} 未配置Cookie")
            return None, {}

        if cookie not in self._clients:
            try:
                self._clients[cookie] = P115Client(cookie)
                self._cookies[cookie] = 0
                logger.trace(f"[115] 客户端已就绪: {drive_name}")
            except Exception as e:
                logger.error(f"[115] 客户端登录失败 ({drive_name}): {e}")
                return None, {}

        return self._clients[cookie], drive_cfg if isinstance(drive_cfg, dict) else {}

    def invalidate_clients(self, drive_index: int | None = None, cookies: list[str] | None = None):
        """清理 115 客户端缓存，强制后续请求按最新 Cookie 重建客户端。"""
        explicit_cookies = {str(cookie or "").strip() for cookie in (cookies or []) if str(cookie or "").strip()}
        removed = []

        for cookie, mapped_drive_index in list(self._cookies.items()):
            if drive_index is not None and mapped_drive_index == drive_index:
                explicit_cookies.add(cookie)

        for cookie in list(explicit_cookies):
            if cookie in self._clients:
                self._clients.pop(cookie, None)
                removed.append(cookie)
            self._cookies.pop(cookie, None)

        if removed:
            logger.info(f"[115] 已清理客户端缓存: {len(removed)} 个")
        elif explicit_cookies:
            logger.info("[115] 已请求清理客户端缓存，但未命中现有缓存")

        return len(removed)

    async def _run_115_read_request(self, request_name: str, request_factory, *, timeout: float = _115_READ_REQUEST_TIMEOUT_SECONDS):
        started_at = time.monotonic()
        try:
            return await run_115_read_request(request_name, request_factory, timeout=timeout)
        except asyncio.TimeoutError as e:
            elapsed = time.monotonic() - started_at
            logger.warning(f"[115] {request_name}超时: 耗时={elapsed:.2f}s")
            raise TimeoutError(f"{request_name}超时: {elapsed:.2f}s") from e

    def _normalize_115_remote_path(self, path: str) -> str:
        text = str(path or "").strip().replace("\\", "/")
        if not text:
            return ""
        if not text.startswith("/"):
            text = "/" + text
        while "//" in text:
            text = text.replace("//", "/")
        return text.rstrip("/") or "/"

    def _extract_115_list_items(self, resp) -> list:
        if not isinstance(resp, dict):
            return []
        raw_items = resp.get("data", [])
        if isinstance(raw_items, dict):
            raw_items = (
                raw_items.get("list")
                or raw_items.get("files")
                or raw_items.get("data")
                or raw_items.get("items")
                or []
            )
        return raw_items if isinstance(raw_items, list) else []

    def _extract_115_folder_cid(self, item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        if item.get("fid") and str(item.get("fc", "") or "") != "0":
            return ""
        cid = str(item.get("cid") or item.get("id") or item.get("category_id") or item.get("fid") or "").strip()
        return cid if cid and cid != "0" else ""

    def _extract_115_created_dir_cid(self, resp: dict) -> str:
        if not isinstance(resp, dict):
            return ""
        data = resp.get("data") or {}
        if isinstance(data, list) and data:
            data = data[0] if isinstance(data[0], dict) else {}
        if not isinstance(data, dict):
            data = {}
        cid = str(
            data.get("cid")
            or data.get("id")
            or data.get("category_id")
            or resp.get("cid")
            or resp.get("id")
            or resp.get("category_id")
            or ""
        ).strip()
        return cid if cid and cid != "0" else ""

    async def _find_115_child_dir_cid(self, client, parent_cid: str, name: str, *, request_label: str = "115目录") -> str:
        offset = 0
        page_index = 0
        while True:
            page_index += 1
            resp = await self._run_115_read_request(
                f"{request_label}逐级查找:{name}",
                lambda _cid=parent_cid, _offset=offset: client.fs_files_app(
                    {
                        "cid": int(_cid),
                        "limit": _115_APP_LIST_SAFE_PAGE_SIZE,
                        "offset": _offset,
                        "fc_mix": 0,
                    },
                    app="android",
                ),
            )
            items = self._extract_115_list_items(resp)
            for item in items:
                item_name = str(item.get("n") or item.get("fn") or item.get("name") or "").strip()
                if item_name != name:
                    continue
                next_cid = self._extract_115_folder_cid(item)
                if next_cid:
                    return next_cid

            if len(items) < _115_APP_LIST_SAFE_PAGE_SIZE:
                return ""
            offset += len(items)
            if page_index % 5 == 0:
                logger.info(
                    f"[115] {request_label}逐级查找仍在翻页: "
                    f"parent={parent_cid}, name={name}, pages={page_index}, scanned={offset}"
                )

    async def _resolve_115_dir_id_by_path(self, client, path: str, *, request_label: str = "115目录") -> str | None:
        normalized_path = self._normalize_115_remote_path(path)
        if not normalized_path:
            return None
        if normalized_path == "/":
            return "0"

        try:
            resp = await self._run_115_read_request(
                f"{request_label}按路径查询",
                lambda: client.fs_dir_getid(normalized_path),
            )
            if isinstance(resp, dict) and resp.get("state"):
                cid = str(resp.get("id") or resp.get("cid") or "").strip()
                if cid:
                    return cid
        except Exception as e:
            err_text = str(e)
            if len(err_text) > 300:
                err_text = err_text[:300] + "..."
            logger.warning(f"[115] {request_label}按路径查询失败，改用逐级查找: {normalized_path} | {err_text}")

        parent_cid = "0"
        for part in [item for item in normalized_path.strip("/").split("/") if item]:
            try:
                next_cid = await self._find_115_child_dir_cid(
                    client,
                    parent_cid,
                    part,
                    request_label=request_label,
                )
            except Exception as e:
                err_text = str(e)
                if len(err_text) > 300:
                    err_text = err_text[:300] + "..."
                logger.warning(f"[115] {request_label}逐级查找失败: parent={parent_cid}, name={part} | {err_text}")
                return None

            if not next_cid:
                return None
            parent_cid = next_cid

        return parent_cid

    async def _ensure_115_dir_id_by_path(self, client, path: str, *, request_label: str = "115目录") -> str | None:
        normalized_path = self._normalize_115_remote_path(path)
        if not normalized_path:
            return None
        if normalized_path == "/":
            return "0"

        existing_cid = await self._resolve_115_dir_id_by_path(client, normalized_path, request_label=request_label)
        if existing_cid:
            return existing_cid

        parent_cid = "0"
        current_path = ""
        for part in [item for item in normalized_path.strip("/").split("/") if item]:
            current_path = f"{current_path}/{part}" if current_path else f"/{part}"
            existing_child_cid = await self._find_115_child_dir_cid(
                client,
                parent_cid,
                part,
                request_label=request_label,
            )
            if existing_child_cid:
                parent_cid = existing_child_cid
                continue

            resp = await run_115_write_request(
                client,
                f"创建{request_label}",
                lambda write_client, _part=part, _parent_cid=parent_cid: write_client.fs_mkdir_app(
                    _part,
                    pid=int(_parent_cid),
                    app="android",
                    async_=False,
                ),
                raise_on_state_false=False,
            )

            if isinstance(resp, dict) and not resp.get("state", True):
                error_text = str(resp.get("error") or resp.get("message") or "")
                if "已存在" in error_text or "exist" in error_text.lower():
                    existing_child_cid = await self._find_115_child_dir_cid(
                        client,
                        parent_cid,
                        part,
                        request_label=request_label,
                    )
                    if existing_child_cid:
                        logger.debug(f"[Rapid] {request_label}已存在，复用目录: {current_path} cid={existing_child_cid}")
                        parent_cid = existing_child_cid
                        continue
                raise RuntimeError(f"创建{request_label}失败: {resp}")

            created_cid = self._extract_115_created_dir_cid(resp)
            if not created_cid:
                created_cid = await self._find_115_child_dir_cid(
                    client,
                    parent_cid,
                    part,
                    request_label=request_label,
                )
            if not created_cid:
                raise RuntimeError(f"创建{request_label}未返回目录ID: path={current_path}, resp={resp}")

            parent_cid = created_cid
            logger.debug(f"[Rapid] 已创建{request_label}: {current_path} cid={created_cid}")

        return parent_cid

    def _get_http_client(self) -> httpx.AsyncClient:
        loop_id = id(asyncio.get_running_loop())
        with self._http_clients_lock:
            client = self._http_clients.get(loop_id)
            if client is None or client.is_closed:
                client = httpx.AsyncClient(timeout=10.0, follow_redirects=True, verify=False)
                self._http_clients[loop_id] = client
            return client

    async def _run_gateway_playback_direct_url_request(self, request_name: str, request_factory):
        return await self._run_gateway_direct_url_request(
            request_name,
            request_factory,
            priority=_GATEWAY_DIRECT_PRIORITY_PLAYBACK,
        )

    async def _run_gateway_direct_pickcode_url_request(self, request_name: str, request_factory):
        return await self._run_gateway_direct_url_request(
            request_name,
            request_factory,
            priority=_GATEWAY_DIRECT_PRIORITY_DIRECT,
        )

    def _next_gateway_direct_url_sequence(self) -> int:
        with self._gateway_direct_url_sequence_lock:
            self._gateway_direct_url_sequence += 1
            return self._gateway_direct_url_sequence

    def _get_gateway_direct_url_queue(self) -> asyncio.PriorityQueue:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        queue = self._gateway_direct_url_queues.get(loop_id)
        if queue is None:
            queue = asyncio.PriorityQueue()
            self._gateway_direct_url_queues[loop_id] = queue
            self._last_gateway_direct_url_at[loop_id] = 0.0
        worker = self._gateway_direct_url_workers.get(loop_id)
        if worker is None or worker.done():
            self._gateway_direct_url_workers[loop_id] = loop.create_task(
                self._gateway_direct_url_worker(loop_id, queue)
            )
        return queue

    async def _run_gateway_direct_url_request(self, request_name: str, request_factory, *, priority: int):
        loop = asyncio.get_running_loop()
        queue = self._get_gateway_direct_url_queue()
        future = loop.create_future()
        await queue.put((
            int(priority),
            self._next_gateway_direct_url_sequence(),
            request_name,
            request_factory,
            future,
        ))
        return await future

    @staticmethod
    def _gateway_queue_has_higher_priority(queue: asyncio.PriorityQueue, priority: int) -> bool:
        try:
            next_item = queue._queue[0] if queue._queue else None
        except Exception:
            return False
        return bool(next_item and int(next_item[0]) < int(priority))

    async def _gateway_direct_url_worker(self, loop_id: int, queue: asyncio.PriorityQueue):
        while True:
            priority, sequence, request_name, request_factory, future = await queue.get()
            request_executed = False
            try:
                if future.cancelled():
                    continue

                if priority > _GATEWAY_DIRECT_PRIORITY_PLAYBACK:
                    await asyncio.sleep(_GATEWAY_DIRECT_LOW_PRIORITY_GRACE_SECONDS)
                    if self._gateway_queue_has_higher_priority(queue, priority):
                        await queue.put((priority, sequence, request_name, request_factory, future))
                        continue

                pacing_seconds = random.uniform(1.0, 1.5)
                wait_seconds = pacing_seconds - (time.monotonic() - self._last_gateway_direct_url_at.get(loop_id, 0.0))
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                    if priority > _GATEWAY_DIRECT_PRIORITY_PLAYBACK and self._gateway_queue_has_higher_priority(queue, priority):
                        await queue.put((priority, sequence, request_name, request_factory, future))
                        continue

                request_executed = True
                result = await self._execute_gateway_direct_url_request(request_name, request_factory)
                if not future.cancelled():
                    future.set_result(result)
            except Exception as e:
                if not future.cancelled():
                    future.set_exception(e)
            finally:
                if request_executed:
                    self._last_gateway_direct_url_at[loop_id] = time.monotonic()
                queue.task_done()

    async def _execute_gateway_direct_url_request(self, request_name: str, request_factory):
        for attempt in range(_GATEWAY_DIRECT_URL_TIMEOUT_MAX_RETRIES + 1):
            request_started_at = time.monotonic()
            try:
                result = request_factory()
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(
                        result,
                        timeout=_GATEWAY_DIRECT_URL_REQUEST_TIMEOUT_SECONDS,
                    )
                if isinstance(result, dict) and not result.get("state", True):
                    raise RuntimeError(f"{request_name}失败: {result}")
                elapsed = time.monotonic() - request_started_at
                if request_name.startswith("获取直链"):
                    label = request_name.split(":", 1)[1].strip() if ":" in request_name else ""
                    label_text = f": {label}" if label else ""
                    logger.debug(f"[115] 直链获取完成{label_text} | 请求耗时={elapsed:.2f}s")
                return result
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - request_started_at
                if request_name.startswith("获取直链"):
                    label = request_name.split(":", 1)[1].strip() if ":" in request_name else ""
                    label_text = f": {label}" if label else ""
                    request_name_for_log = f"直链获取{label_text}"
                else:
                    request_name_for_log = request_name
                if attempt < _GATEWAY_DIRECT_URL_TIMEOUT_MAX_RETRIES:
                    logger.warning(f"[115] {request_name_for_log}超时: 请求耗时={elapsed:.2f}s，准备重试 ({attempt + 1}/{_GATEWAY_DIRECT_URL_TIMEOUT_MAX_RETRIES})")
                    continue
                logger.warning(f"[115] {request_name_for_log}超时: 请求耗时={elapsed:.2f}s")
                return {}

    def _rapid_assignment_key(self, pickcode: str, rapid_context: dict | None = None) -> str:
        user_key = ""
        if isinstance(rapid_context, dict):
            user_key = str(rapid_context.get("user_key") or "").strip()
        return f"{pickcode}:{user_key or 'global'}"

    def _rapid_context_values(self, rapid_context: dict | None) -> tuple[str, str, str]:
        if not isinstance(rapid_context, dict):
            return "", "", ""
        return (
            str(rapid_context.get("user_key") or "").strip(),
            str(rapid_context.get("user_name") or "").strip(),
            str(rapid_context.get("item_id") or "").strip(),
        )

    def _remember_rapid_assignment(
        self,
        assignment_key: str,
        *,
        cache_ua: str,
        rapid_url: str,
        rapid_pickcode: str,
        rapid_file_id: str | int | None,
        rapid_cookie: str | None,
        account_index,
        account_name: str,
        source_pickcode: str,
        rapid_context: dict | None,
    ) -> dict:
        rapid_user_key, rapid_user_name, rapid_item_id = self._rapid_context_values(rapid_context)
        assignment = {
            "ua": cache_ua,
            "url": rapid_url,
            "pickcode": rapid_pickcode,
            "file_id": rapid_file_id,
            "cookie": rapid_cookie,
            "account_index": account_index,
            "account_name": account_name,
            "user_key": rapid_user_key,
            "user_name": rapid_user_name,
            "item_id": rapid_item_id,
            "source_pickcode": source_pickcode,
            "updated_at": time.time(),
        }
        self._rapid_assignments[assignment_key] = assignment
        return assignment

    def _active_rapid_assignment_counts(self, rapid_accounts: list, rapid_context: dict | None = None) -> dict[int, int]:
        active_user_keys = set()
        active_item_ids = set()
        if isinstance(rapid_context, dict):
            active_user_keys = {
                str(item or "").strip()
                for item in (rapid_context.get("active_user_keys") or [])
                if str(item or "").strip()
            }
            active_item_ids = {
                str(item or "").strip()
                for item in (rapid_context.get("active_item_ids") or [])
                if str(item or "").strip()
            }

        if active_user_keys:
            counts = {idx: 0 for idx in range(len(rapid_accounts))}
            for assignment in self._rapid_assignments.values():
                if not isinstance(assignment, dict):
                    continue
                user_key = str(assignment.get("user_key") or "").strip()
                item_id = str(assignment.get("item_id") or "").strip()
                account_index = assignment.get("account_index")
                if user_key not in active_user_keys:
                    continue
                if active_item_ids and item_id and item_id not in active_item_ids:
                    continue
                try:
                    account_index = int(account_index)
                except (TypeError, ValueError):
                    continue
                if account_index in counts:
                    counts[account_index] += 1
            return counts
        return {idx: 0 for idx in range(len(rapid_accounts))}

    def _is_file_busy(self, pickcode: str, rapid_context: dict | None = None) -> bool:
        """基于 Emby Sessions 判断当前文件是否已有活跃播放。"""
        if not isinstance(rapid_context, dict):
            return False

        current_item_id = str(rapid_context.get("item_id") or "").strip()
        if not current_item_id:
            return False

        active_playbacks = rapid_context.get("active_playbacks") or []
        if not isinstance(active_playbacks, list):
            return False

        for playback in active_playbacks:
            if not isinstance(playback, dict):
                continue
            active_item_id = str(playback.get("item_id") or "").strip()
            if active_item_id != current_item_id:
                continue
            return True
        return False

    def _select_rapid_account_index(self, rapid_accounts: list, rapid_mode: str, rapid_context: dict | None = None, concurrency_limit: int = 0) -> int | None:
        counts = self._active_rapid_assignment_counts(rapid_accounts, rapid_context)
        limited = int(concurrency_limit or 0)
        available = [
            idx
            for idx in range(len(rapid_accounts))
            if limited <= 0 or counts.get(idx, 0) < limited
        ]
        if not available:
            return None

        if rapid_mode in [str(i) for i in range(len(rapid_accounts))]:
            selected = int(rapid_mode)
            return selected if selected in available else None

        if counts:
            min_count = min(counts.get(idx, 0) for idx in available)
            candidates = [idx for idx in available if counts.get(idx, 0) == min_count]
            return random.choice(candidates) if candidates else available[0]

        if rapid_mode == "auto":
            return random.choice(available)

        return available[0]

    def _topology_session_user_key(self, session: dict) -> str:
        user_id = str((session or {}).get("UserId") or "").strip()
        if user_id:
            return f"user:{user_id}"
        user_name = str((session or {}).get("UserName") or "").strip()
        if user_name:
            return f"name:{user_name}"
        user = (session or {}).get("User")
        if isinstance(user, dict):
            user_name = str(user.get("Name") or user.get("UserName") or "").strip()
            if user_name:
                return f"name:{user_name}"
        return ""

    def _session_now_playing_item_id(self, session: dict) -> str:
        item = (session or {}).get("NowPlayingItem") or {}
        return str(item.get("Id") or "").strip() if isinstance(item, dict) else ""

    def _has_active_playback_sessions(self, sessions: list) -> bool:
        return any(self._session_now_playing_item_id(sess) for sess in (sessions or []) if isinstance(sess, dict))

    def update_playback_topology_sessions(self, emby_index: int, emby_cfg: dict, sessions: list):
        idx = int(emby_index or 0)
        safe_sessions = sessions if isinstance(sessions, list) else []
        active_count = len([sess for sess in safe_sessions if isinstance(sess, dict) and self._session_now_playing_item_id(sess)])
        self._topology_sessions[idx] = {
            "sessions": safe_sessions,
            "emby_cfg": dict(emby_cfg or {}),
            "updated_at": time.time(),
        }
        publish_realtime_event("playback_topology_updated", {
            "emby_index": idx,
            "emby_name": (emby_cfg or {}).get("name") or f"Emby[{idx}]",
            "active_sessions": active_count,
        })
        if active_count:
            self._ensure_topology_polling(idx)

    def _ensure_topology_polling(self, emby_index: int):
        task = self._topology_poll_tasks.get(emby_index)
        if task is None or task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._topology_poll_tasks[emby_index] = loop.create_task(
                self._poll_topology_sessions(emby_index)
            )

    async def _poll_topology_sessions(self, emby_index: int):
        while True:
            await asyncio.sleep(300)
            snapshot = self._topology_sessions.get(emby_index) or {}
            emby_cfg = snapshot.get("emby_cfg") or {}
            sessions = await self._fetch_topology_emby_sessions(emby_cfg)
            self._topology_sessions[emby_index] = {
                "sessions": sessions,
                "emby_cfg": dict(emby_cfg or {}),
                "updated_at": time.time(),
            }
            if not self._has_active_playback_sessions(sessions):
                self._topology_poll_tasks.pop(emby_index, None)
                return

    async def _fetch_topology_emby_sessions(self, emby_cfg: dict) -> list:
        base_url = str((emby_cfg or {}).get("url") or "").rstrip("/")
        api_key = str((emby_cfg or {}).get("key") or "")
        if not base_url or not api_key:
            return []
        try:
            resp = await self._get_http_client().get(
                f"{base_url}/emby/Sessions",
                headers={"X-Emby-Token": api_key},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json() or []
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"[Topology] 刷新 Emby Sessions 失败: {type(e).__name__} {repr(e)}")
        return []

    async def refresh_playback_topology_from_emby_async(self, emby_index: int, emby_cfg: dict, delay: float = 0.0) -> int:
        if delay > 0:
            await asyncio.sleep(delay)
        sessions = await self._fetch_topology_emby_sessions(emby_cfg)
        self.update_playback_topology_sessions(emby_index, emby_cfg, sessions)
        return len([sess for sess in sessions if isinstance(sess, dict) and self._session_now_playing_item_id(sess)])

    def _format_topology_time(self, ticks) -> str:
        try:
            total_seconds = int(float(ticks or 0) / 10_000_000)
        except (TypeError, ValueError):
            total_seconds = 0
        if total_seconds <= 0:
            return "0:00"
        hours, rem = divmod(total_seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def _build_topology_image_url(self, emby_cfg: dict, item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        host = str((emby_cfg or {}).get("public_host") or (emby_cfg or {}).get("url") or "").rstrip("/")
        item_id = str(item.get("Id") or "").strip()
        if not host or not item_id:
            return ""
        backdrop_tags = item.get("BackdropImageTags") or []
        if isinstance(backdrop_tags, list) and backdrop_tags:
            return f"{host}/emby/Items/{item_id}/Images/Backdrop/0?tag={backdrop_tags[0]}&quality=90&maxWidth=640"
        image_tags = item.get("ImageTags") or {}
        if isinstance(image_tags, dict) and image_tags.get("Backdrop"):
            return f"{host}/emby/Items/{item_id}/Images/Backdrop/0?tag={image_tags.get('Backdrop')}&quality=90&maxWidth=640"
        if isinstance(image_tags, dict) and image_tags.get("Primary"):
            return f"{host}/emby/Items/{item_id}/Images/Primary?tag={image_tags.get('Primary')}&quality=90&maxHeight=360"
        return ""

    def _topology_item_title(self, item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        item_type = item.get("Type")
        if item_type == "Episode":
            series = item.get("SeriesName") or item.get("Name") or "剧集"
            season = item.get("ParentIndexNumber")
            episode = item.get("IndexNumber")
            suffix = ""
            if season is not None and episode is not None:
                suffix = f" S{season}E{episode}"
            return f"{series}{suffix}"
        return str(item.get("Name") or "").strip()

    def _format_topology_bitrate(self, bitrate) -> str:
        try:
            value = int(float(bitrate or 0))
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            return ""
        mbps = value / 1_000_000
        if mbps >= 10:
            return f"{mbps:.0f} mbps"
        return f"{mbps:.1f}".rstrip("0").rstrip(".") + " mbps"

    def _topology_media_streams(self, session: dict, item: dict) -> list:
        streams = []
        if isinstance(item, dict):
            streams = item.get("MediaStreams") or []
            if not streams:
                sources = item.get("MediaSources") or []
                if isinstance(sources, list) and sources:
                    first_source = sources[0] if isinstance(sources[0], dict) else {}
                    streams = first_source.get("MediaStreams") or []
        if not streams and isinstance(session, dict):
            source = session.get("NowPlayingItem") or {}
            if isinstance(source, dict):
                streams = source.get("MediaStreams") or []
        return streams if isinstance(streams, list) else []

    def _topology_media_source(self, item: dict) -> dict:
        if not isinstance(item, dict):
            return {}
        sources = item.get("MediaSources") or []
        if isinstance(sources, list) and sources:
            source = sources[0]
            return source if isinstance(source, dict) else {}
        return {}

    def _topology_stream_method(self, session: dict, stream: dict | None = None) -> str:
        transcoding = session.get("TranscodingInfo") if isinstance(session, dict) else None
        if isinstance(transcoding, dict) and transcoding:
            method = str(transcoding.get("TranscodeReasons") or "").strip()
            return "转码" if method else "转码"
        if isinstance(stream, dict):
            method = str(stream.get("DeliveryMethod") or "").strip()
            if method:
                return "直接播放" if method.lower() in {"directplay", "directstream"} else method
        return "直接播放"

    def _topology_video_label(self, stream: dict | None) -> str:
        if not isinstance(stream, dict):
            return ""
        parts = []
        height = stream.get("Height")
        width = stream.get("Width")
        if height:
            try:
                h = int(height)
                parts.append("4K" if h >= 2000 else f"{h}p")
            except (TypeError, ValueError):
                if width:
                    parts.append(f"{width}x{height}")
        codec = str(stream.get("Codec") or "").strip().upper()
        if codec:
            codec_map = {"H264": "H.264", "HEVC": "HEVC", "H265": "HEVC", "AV1": "AV1", "VC1": "VC-1"}
            parts.append(codec_map.get(codec.replace(".", ""), codec))
        dynamic_range = str(stream.get("VideoRange") or stream.get("VideoRangeType") or stream.get("Profile") or "").strip()
        if dynamic_range:
            lower = dynamic_range.lower()
            if "dolby" in lower:
                parts.append("Dolby Vision")
            elif "hdr" in lower:
                parts.append(dynamic_range.upper() if dynamic_range.islower() else dynamic_range)
        return " ".join(dict.fromkeys([part for part in parts if part]))

    def _topology_audio_label(self, stream: dict | None) -> str:
        if not isinstance(stream, dict):
            return ""
        parts = []
        language = str(stream.get("DisplayLanguage") or stream.get("Language") or "").strip()
        if language:
            parts.append(language)
        codec = str(stream.get("Codec") or "").strip().upper()
        if codec:
            codec_map = {"EAC3": "EAC3", "AC3": "AC3", "AAC": "AAC", "TRUEHD": "TrueHD", "DTS": "DTS", "FLAC": "FLAC"}
            parts.append(codec_map.get(codec.replace("-", ""), codec))
        layout = str(stream.get("ChannelLayout") or "").strip()
        channels = stream.get("Channels")
        if layout:
            parts.append(layout)
        elif channels:
            channel_map = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}
            try:
                parts.append(channel_map.get(int(channels), f"{channels} ch"))
            except (TypeError, ValueError):
                pass
        if stream.get("IsDefault"):
            parts.append("(默认)")
        return " ".join(parts)

    def _topology_playback_details(self, session: dict, item: dict) -> dict:
        streams = self._topology_media_streams(session, item)
        video = next((stream for stream in streams if isinstance(stream, dict) and stream.get("Type") == "Video"), None)
        audio = next((stream for stream in streams if isinstance(stream, dict) and stream.get("Type") == "Audio" and stream.get("IsDefault")), None)
        if audio is None:
            audio = next((stream for stream in streams if isinstance(stream, dict) and stream.get("Type") == "Audio"), None)
        source = self._topology_media_source(item)
        container = str(source.get("Container") or item.get("Container") or "").strip().upper()
        bitrate = source.get("Bitrate") or item.get("Bitrate") or (video or {}).get("BitRate")
        return {
            "media": " ".join(part for part in [container, f"({self._format_topology_bitrate(bitrate)})" if self._format_topology_bitrate(bitrate) else ""] if part),
            "video": self._topology_video_label(video),
            "audio": self._topology_audio_label(audio),
            "media_method": self._topology_stream_method(session),
            "video_method": self._topology_stream_method(session, video),
            "audio_method": self._topology_stream_method(session, audio),
        }

    def _topology_account_key(self, assignment: dict | None) -> str:
        if isinstance(assignment, dict) and assignment.get("account_index") is not None:
            try:
                return f"rapid:{int(assignment.get('account_index'))}"
            except (TypeError, ValueError):
                pass
        return "main:0"

    async def get_playback_topology_async(self) -> dict:
        cfg = await get_config_302()
        drives = cfg.get("drives") if isinstance(cfg.get("drives"), list) else []
        drive = drives[0] if drives else cfg.get("drive", {})
        drive = drive if isinstance(drive, dict) else {}
        rapid_accounts = drive.get("rapid_accounts") if isinstance(drive.get("rapid_accounts"), list) else []

        accounts = [{
            "key": "main:0",
            "type": "main",
            "index": 0,
            "name": drive.get("name") or "115 主账号",
            "icon": "fa-cloud",
            "sessions": [],
        }]
        for idx, account in enumerate(rapid_accounts):
            account = account if isinstance(account, dict) else {}
            accounts.append({
                "key": f"rapid:{idx}",
                "type": "rapid",
                "index": idx,
                "name": account.get("name") or f"小号 {idx + 1}",
                "icon": "fa-user",
                "sessions": [],
            })

        account_map = {account["key"]: account for account in accounts}
        total_sessions = 0
        latest_updated_at = 0.0

        for emby_index, snapshot in list(self._topology_sessions.items()):
            emby_cfg = snapshot.get("emby_cfg") or {}
            sessions = await self._fetch_topology_emby_sessions(emby_cfg)
            snapshot = {
                "sessions": sessions,
                "emby_cfg": dict(emby_cfg or {}),
                "updated_at": time.time(),
            }
            self._topology_sessions[emby_index] = snapshot
            latest_updated_at = max(latest_updated_at, float(snapshot.get("updated_at") or 0))
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                item = session.get("NowPlayingItem") or {}
                item_id = str(item.get("Id") or "").strip() if isinstance(item, dict) else ""
                if not item_id:
                    continue

                user_key = self._topology_session_user_key(session)
                assignment = None
                for candidate in self._rapid_assignments.values():
                    if not isinstance(candidate, dict):
                        continue
                    if str(candidate.get("item_id") or "") == item_id and str(candidate.get("user_key") or "") == user_key:
                        assignment = candidate
                        break

                account_key = self._topology_account_key(assignment)
                account = account_map.get(account_key) or account_map["main:0"]
                position_ticks = session.get("PlayState", {}).get("PositionTicks") if isinstance(session.get("PlayState"), dict) else 0
                runtime_ticks = item.get("RunTimeTicks") if isinstance(item, dict) else 0
                percent = 0
                try:
                    if runtime_ticks:
                        percent = max(0, min(100, round(float(position_ticks or 0) / float(runtime_ticks) * 100, 1)))
                except Exception:
                    percent = 0

                details = self._topology_playback_details(session, item)
                account["sessions"].append({
                    "id": str(session.get("Id") or f"{emby_index}:{item_id}:{user_key}"),
                    "emby_index": emby_index,
                    "emby_name": emby_cfg.get("name") or f"Emby[{emby_index}]",
                    "user_key": user_key,
                    "user_name": self._topology_session_user_key(session).replace("name:", "") if not session.get("UserName") else session.get("UserName"),
                    "client": session.get("Client") or "",
                    "device": session.get("DeviceName") or "",
                    "remote_endpoint": session.get("RemoteEndPoint") or "",
                    "item_id": item_id,
                    "title": self._topology_item_title(item) or item_id,
                    "year": item.get("ProductionYear") if isinstance(item, dict) else "",
                    "image_url": self._build_topology_image_url(emby_cfg, item),
                    "position": self._format_topology_time(position_ticks),
                    "duration": self._format_topology_time(runtime_ticks),
                    "percent": percent,
                    "media": details.get("media") or "",
                    "video": details.get("video") or "",
                    "audio": details.get("audio") or "",
                    "media_method": details.get("media_method") or "",
                    "video_method": details.get("video_method") or "",
                    "audio_method": details.get("audio_method") or "",
                    "account_key": account_key,
                    "account_name": account.get("name"),
                })
                total_sessions += 1

        return {
            "status": "ok",
            "updated_at": int(latest_updated_at or time.time()),
            "polling": any(task and not task.done() for task in self._topology_poll_tasks.values()),
            "total_sessions": total_sessions,
            "accounts": accounts,
        }

    async def get_secondary_client(self, rapid_context: dict | None = None):
        """获取或初始化小号 P115Client（用于秒传）- 支持多账号池

        返回: (client, drive_cfg, rapid_cookie, account_index, account_name) 或 (None, {}, None, None, None)
        """
        cfg = await get_config_302()

        drives = cfg.get("drives", [])
        drive_index = 0
        if isinstance(drives, list) and drives:
            drive_cfg = drives[drive_index] if 0 <= drive_index < len(drives) else drives[0]
        else:
            drive_cfg = cfg.get("drive", {})

        # 获取小号池
        rapid_accounts = drive_cfg.get("rapid_accounts", [])
        rapid_mode = drive_cfg.get("rapid_mode", "auto")
        try:
            rapid_concurrency_limit = max(0, int(drive_cfg.get("rapid_concurrency_limit") or 0))
        except (TypeError, ValueError):
            rapid_concurrency_limit = 0

        if not rapid_accounts:
            return None, {}, None, None, None

        # 根据调度策略选择小号
        account_index = self._select_rapid_account_index(
            rapid_accounts,
            rapid_mode,
            rapid_context,
            concurrency_limit=rapid_concurrency_limit,
        )
        if account_index is None:
            logger.warning(f"[Rapid] 小号并发已达上限，跳过秒传: limit={rapid_concurrency_limit}")
            return None, {}, None, None, None
        selected_account = rapid_accounts[account_index]

        rapid_cookie = selected_account.get("cookie", "")
        account_name = selected_account.get("name", f"小号{account_index + 1}")

        if not rapid_cookie:
            return None, {}, None, None, None

        try:
            client = P115Client(rapid_cookie)
            logger.debug(f"[Rapid] 使用小号: {account_name} (模式: {rapid_mode}, 索引: {account_index}/{len(rapid_accounts)})")
            return client, drive_cfg, rapid_cookie, account_index, account_name
        except Exception as e:
            logger.error(f"[Rapid] 小号登录失败 ({account_name}): {e}")
            return None, {}, None, None, None

    # ==========================================================
    # [修改] 增加 item_name 参数，用于优化日志显示
    # ==========================================================
    async def get_direct_url(self, item_id: str, media_source_id: str = None, user_agent: str = "", item_name: str = None, emby_index: int = 0, direct_link_context: str = "default", rapid_context: dict | None = None):
        """[核心入口] 获取播放直链

        Args:
            emby_index: Emby 配置索引，用于确定使用哪个 115 账号
        """

        # 防止并发重复请求同一 Item
        async with self._locks_cleanup_lock:
            if item_id not in self._item_locks:
                self._item_locks[item_id] = asyncio.Lock()
            item_lock = self._item_locks[item_id]

        async with item_lock:
            try:
                client, drive_cfg = await self.get_client(emby_index)
                if not client:
                    return None

                pickcode = None

                # 1. 尝试从 ID 缓存获取 Pickcode
                if item_id in self._id_cache:
                    pickcode = self._id_cache[item_id]
                else:
                    # 2. 如果缓存没有，走完整解析流程
                    pickcode = await self._resolve_pickcode_flow(client, item_id, media_source_id, emby_index)

                if not pickcode:
                    return None

                rapid_lock_key = f"rapid:{self._rapid_assignment_key(pickcode, rapid_context)}"
                async with self._locks_cleanup_lock:
                    if rapid_lock_key not in self._item_locks:
                        self._item_locks[rapid_lock_key] = asyncio.Lock()
                    rapid_lock = self._item_locks[rapid_lock_key]

                async with rapid_lock:
                    return await self._get_direct_url_core(
                        client=client,
                        drive_cfg=drive_cfg,
                        pickcode=pickcode,
                        user_agent=user_agent,
                        log_name=item_name if item_name else f"ID: {item_id}",
                        emby_index=emby_index,
                        filename_resolver=lambda: self._resolve_filename_for_item(item_id, media_source_id, emby_index),
                        direct_link_context=direct_link_context,
                        rapid_context=rapid_context,
                    )
            except Exception as e:
                logger.error(f"[115] 获取直链异常: {e}")
                traceback.print_exc()
                return None

    async def get_direct_url_by_pickcode(self, pickcode: str, user_agent: str = "", emby_index: int = 0, filename: str | None = None, direct_link_context: str = "default", rapid_context: dict | None = None):
        normalized_pickcode = str(pickcode or "").strip()
        if not normalized_pickcode:
            return None

        async with self._locks_cleanup_lock:
            if normalized_pickcode not in self._item_locks:
                self._item_locks[normalized_pickcode] = asyncio.Lock()
            item_lock = self._item_locks[normalized_pickcode]

        async with item_lock:
            try:
                client, drive_cfg = await self.get_client(emby_index)
                if not client:
                    return None

                rapid_lock_key = f"rapid:{self._rapid_assignment_key(normalized_pickcode, rapid_context)}"
                async with self._locks_cleanup_lock:
                    if rapid_lock_key not in self._item_locks:
                        self._item_locks[rapid_lock_key] = asyncio.Lock()
                    rapid_lock = self._item_locks[rapid_lock_key]

                async with rapid_lock:
                    return await self._get_direct_url_core(
                        client=client,
                        drive_cfg=drive_cfg,
                        pickcode=normalized_pickcode,
                        user_agent=user_agent,
                        log_name=filename or normalized_pickcode,
                        emby_index=emby_index,
                        filename_resolver=lambda: filename or f"{normalized_pickcode}.mkv",
                        direct_link_context=direct_link_context,
                        rapid_context=rapid_context,
                    )
            except Exception as e:
                logger.error(f"[115] 按 Pickcode 获取直链异常: {e}")
                traceback.print_exc()
                return None

    async def _get_direct_url_core(self, client, drive_cfg: dict, pickcode: str, user_agent: str = "", log_name: str | None = None, emby_index: int = 0, filename_resolver=None, direct_link_context: str = "default", rapid_context: dict | None = None):
        drive_name = drive_cfg.get("name", f"drives[{emby_index}]")
        display_name = log_name if log_name else pickcode

        enable_sync = drive_cfg.get('enable_sync', False)
        enable_rapid = drive_cfg.get('enable_rapid', False)

        cache_ua = user_agent if user_agent else "NoUA"
        cache_key = f"{pickcode}_{cache_ua}"
        is_playback_context = direct_link_context in {"gateway_playback", "gateway_direct"}
        is_busy = self._is_file_busy(pickcode, rapid_context) if is_playback_context else False

        if cache_key in self._url_cache and not enable_rapid and not (enable_sync and is_busy):
            if cache_key not in self._url_cache_hit_log_dedupe:
                self._url_cache_hit_log_dedupe[cache_key] = True
                logger.trace(f"[Cache-{drive_name}] 命中直链缓存: {display_name}")
            return self._url_cache[cache_key]

        if enable_rapid:
            rapid_cache_key = f"rapid_{pickcode}"
            rapid_assignment_key = self._rapid_assignment_key(pickcode, rapid_context)
            has_user_context = isinstance(rapid_context, dict) and bool(str(rapid_context.get("user_key") or "").strip())
            rapid_assignment = self._rapid_assignments.get(rapid_assignment_key)
            if isinstance(rapid_assignment, dict):
                rapid_pickcode = str(rapid_assignment.get("pickcode") or "")
                rapid_cookie = str(rapid_assignment.get("cookie") or "")
                rapid_cache_ua = str(rapid_assignment.get("ua") or "")
                rapid_cache_url = str(rapid_assignment.get("url") or "")
                if rapid_cache_url and rapid_cache_ua == cache_ua:
                    logger.trace(f"[Rapid-{drive_name}] 命中用户秒传缓存: {display_name}")
                    return rapid_cache_url
                if rapid_pickcode and rapid_cookie:
                    logger.debug(f"[Rapid-{drive_name}] 命中用户秒传文件缓存，按当前UA重新获取直链: {display_name}")
                    rapid_url = await self._fetch_download_url(
                        rapid_pickcode,
                        user_agent,
                        cookie=rapid_cookie,
                        direct_link_context=direct_link_context,
                    )
                    if rapid_url:
                        rapid_assignment.update({"ua": cache_ua, "url": rapid_url})
                        self._rapid_assignments[rapid_assignment_key] = rapid_assignment
                        self._url_cache[cache_key] = rapid_url
                        return rapid_url
                    self._rapid_assignments.pop(rapid_assignment_key, None)

            rapid_cache_entry = self._url_cache.get(rapid_cache_key)
            if isinstance(rapid_cache_entry, dict):
                rapid_cache_ua = str(rapid_cache_entry.get("ua") or "")
                rapid_cache_url = str(rapid_cache_entry.get("url") or "")
                rapid_pickcode = str(rapid_cache_entry.get("pickcode") or "")
                rapid_cookie = str(rapid_cache_entry.get("cookie") or "")
                if rapid_cache_url and rapid_cache_ua == cache_ua:
                    logger.trace(f"[Rapid-{drive_name}] 命中秒传缓存: {display_name}")
                    if has_user_context:
                        self._remember_rapid_assignment(
                            rapid_assignment_key,
                            cache_ua=cache_ua,
                            rapid_url=rapid_cache_url,
                            rapid_pickcode=rapid_pickcode,
                            rapid_file_id=rapid_cache_entry.get("file_id"),
                            rapid_cookie=rapid_cookie,
                            account_index=rapid_cache_entry.get("account_index"),
                            account_name=str(rapid_cache_entry.get("account_name") or ""),
                            source_pickcode=pickcode,
                            rapid_context=rapid_context,
                        )
                    return rapid_cache_url
                if rapid_pickcode and rapid_cookie:
                    logger.debug(f"[Rapid-{drive_name}] 命中秒传文件缓存，按当前UA重新获取直链: {display_name}")
                    rapid_url = await self._fetch_download_url(
                        rapid_pickcode,
                        user_agent,
                        cookie=rapid_cookie,
                        direct_link_context=direct_link_context,
                    )
                    if rapid_url:
                        self._url_cache[cache_key] = rapid_url
                        rapid_cache_entry.update({
                            "ua": cache_ua,
                            "url": rapid_url,
                        })
                        self._url_cache[rapid_cache_key] = rapid_cache_entry
                        if has_user_context:
                            self._remember_rapid_assignment(
                                rapid_assignment_key,
                                cache_ua=cache_ua,
                                rapid_url=rapid_url,
                                rapid_pickcode=rapid_pickcode,
                                rapid_file_id=rapid_cache_entry.get("file_id"),
                                rapid_cookie=rapid_cookie,
                                account_index=rapid_cache_entry.get("account_index"),
                                account_name=str(rapid_cache_entry.get("account_name") or ""),
                                source_pickcode=pickcode,
                                rapid_context=rapid_context,
                            )
                        return rapid_url
                    logger.warning(f"[Rapid-{drive_name}] 秒传文件缓存直链获取失败，重新尝试秒传: {display_name}")
                    self._url_cache.pop(rapid_cache_key, None)
            elif rapid_cache_entry is not None:
                self._url_cache.pop(rapid_cache_key, None)

            logger.debug(f"[Rapid-{drive_name}] 尝试秒传: {display_name}")
            result = await self.get_secondary_client(rapid_context=rapid_context)
            if not result or not result[0]:
                secondary_client = None
                sec_cfg = {}
                rapid_cookie = None
                rapid_account_index = None
                rapid_account_name = ""
            else:
                secondary_client, sec_cfg, rapid_cookie, rapid_account_index, rapid_account_name = result

            if not secondary_client:
                logger.warning("[Rapid] 小号客户端未就绪，已跳过秒传")
            else:
                sha1_info = await self._get_file_sha1_and_preupload_info(client, pickcode, user_agent, emby_index, direct_link_context=direct_link_context)
                if not sha1_info:
                    logger.warning("[Rapid] 无法获取文件 SHA1 信息")
                else:
                    logger.debug(f"[Rapid] SHA1获取成功: {sha1_info['sha1'][:16]}... (size: {sha1_info['size']})")
                    filename = await self._resolve_rapid_filename(filename_resolver, pickcode)
                    remote_root_name = str(sec_cfg.get("remote_root_name") or "影视库").strip().strip("/") or "影视库"
                    target_dir = str(sec_cfg.get("upload_dir") or f"/{remote_root_name}/秒传目录").strip()
                    rapid_result = await self._rapid_transfer_to_secondary(
                        secondary_client, client, pickcode, sha1_info, filename, target_dir, user_agent
                    )

                    if rapid_result:
                        rapid_pickcode = rapid_result['pickcode']
                        rapid_url = await self._fetch_download_url(
                            rapid_pickcode, user_agent,
                            cookie=rapid_cookie,
                            direct_link_context=direct_link_context,
                        )

                        if rapid_url:
                            self._url_cache[cache_key] = rapid_url
                            self._url_cache[rapid_cache_key] = {
                                "ua": cache_ua,
                                "url": rapid_url,
                                "pickcode": rapid_result.get('pickcode'),
                                "file_id": rapid_result.get('file_id'),
                                "cookie": rapid_cookie,
                                "account_index": rapid_account_index,
                                "account_name": rapid_account_name,
                            }
                            self._remember_rapid_assignment(
                                rapid_assignment_key,
                                cache_ua=cache_ua,
                                rapid_url=rapid_url,
                                rapid_pickcode=rapid_result.get('pickcode'),
                                rapid_file_id=rapid_result.get('file_id'),
                                rapid_cookie=rapid_cookie,
                                account_index=rapid_account_index,
                                account_name=rapid_account_name,
                                source_pickcode=pickcode,
                                rapid_context=rapid_context,
                            )
                            asyncio.create_task(
                                self._delayed_remove_secondary(
                                    secondary_client,
                                    file_id=rapid_result.get('file_id'),
                                    pickcode=rapid_result.get('pickcode')
                                )
                            )
                            logger.debug(f"[Rapid] 返回小号直链: {display_name}")
                            return rapid_url
                        else:
                            logger.warning("[Rapid] 无法获取小号直链")
                    else:
                        logger.warning("[Rapid] 秒传接口返回失败")

            logger.warning("[Rapid] 秒传失败，已降级为常规直链")

        if enable_sync and is_busy:
            logger.debug(f"[Sync-{drive_name}] 检测到并发播放，触发写时复制: {display_name}")
            new_url_data = await self._sync_copy_and_get_link(client, drive_cfg, pickcode, user_agent, emby_index, direct_link_context=direct_link_context)

            if new_url_data:
                new_url = new_url_data['url']
                new_file_id = new_url_data['file_id']
                self._url_cache[cache_key] = new_url
                asyncio.create_task(self._delayed_remove(new_file_id, delay=60))
                logger.info(f"[Sync-{drive_name}] 写时复制成功: {display_name}")
                return new_url
            else:
                logger.warning(f"[Sync-{drive_name}] 复制失败，降级使用原文件")

        direct_log_label = f"媒体/路径={display_name}"
        logger.debug(f"[115-{drive_name}] 开始获取直链: {direct_log_label}")
        final_url = await self._fetch_download_url(
            pickcode,
            user_agent,
            emby_index=emby_index,
            direct_link_context=direct_link_context,
            log_label=direct_log_label,
        )

        if final_url:
            self._url_cache[cache_key] = final_url
            logger.trace(f"[115-{drive_name}] 直链获取成功: {direct_log_label}")
            return final_url

        logger.warning(f"[115-{drive_name}] 直链获取失败: {direct_log_label}")
        return None

    async def _resolve_filename_for_item(self, item_id: str, media_source_id: str | None, emby_index: int) -> str:
        cfg = await get_config_302()
        embys = cfg.get("embys", [])
        if isinstance(embys, list) and len(embys) > emby_index:
            emby_cfg = embys[emby_index]
        else:
            emby_cfg = next((e for e in embys if e.get('enabled')), embys[0]) if embys else {}
        file_path = await self._get_emby_file_path(emby_cfg, item_id, media_source_id)
        return os.path.basename(file_path) if file_path else "video.mkv"

    async def _resolve_rapid_filename(self, filename_resolver, pickcode: str) -> str:
        if filename_resolver is None:
            return f"{pickcode}.mkv"
        try:
            filename = filename_resolver()
            if inspect.isawaitable(filename):
                filename = await filename
        except Exception as e:
            logger.debug(f"[Rapid] 解析文件名失败: {e}")
            filename = None
        normalized = str(filename or "").strip()
        return normalized or f"{pickcode}.mkv"

    async def _resolve_pickcode_flow(self, client, item_id, media_source_id, emby_index: int = 0):
        """解析 Pickcode 的完整流程"""
        cfg = await get_config_302()
        embys = cfg.get("embys", [])

        # 使用传入的 emby_index 获取正确的配置
        if isinstance(embys, list) and len(embys) > emby_index:
            emby_cfg = embys[emby_index]
        else:
            emby_cfg = next((e for e in embys if e.get('enabled')), embys[0]) if embys else {}

        emby_name = emby_cfg.get("name", f"Emby[{emby_index}]")
        pickcode = await self._resolve_pickcode_from_strm(emby_cfg, item_id, media_source_id)
        if pickcode:
            self._id_cache[item_id] = pickcode
            logger.debug(f"[115-{emby_name}] Pickcode提取成功: {pickcode}")
            return pickcode

        logger.debug(f"[115-{emby_name}] Pickcode未命中")
        return None

    async def _sync_copy_and_get_link(self, client, config, src_pickcode, user_agent, emby_index=None, direct_link_context: str = "default"):
        """[同步] 复制文件 -> 通过文件列表获取新 Pickcode -> 获取直链

        Args:
            emby_index: Emby 配置索引，用于确定使用哪个 115 账号的 cookie
        """
        drive_name = config.get('name', '115')
        remote_root_name = str(config.get("remote_root_name") or "影视库").strip().strip("/") or "影视库"
        target_dir = str(config.get('upload_dir') or f"/{remote_root_name}/秒传目录").strip()
        logger.debug(f"[Sync-{drive_name}] 目标目录: {target_dir}")
        try:
            # 1. [修复] 使用 to_id 直接获取 src_file_id (需要文件头部 import to_id)
            src_file_id = to_id(src_pickcode)

            # 2. 获取/创建项目统一的复制/秒传目录。
            logger.debug(f"[Sync-{drive_name}] 查询目标目录: {target_dir}")
            target_cid = await self._ensure_115_dir_id_by_path(
                client,
                target_dir,
                request_label="复制目标目录",
            )
            if not target_cid:
                logger.error(f"[Sync-{drive_name}] 无法获取目标目录 CID: {target_dir}")
                return None

            # 3. 执行复制
            logger.debug(f"[Sync-{drive_name}] 开始复制: src_id={src_file_id} -> target_cid={target_cid}")
            resp = await run_115_write_request(
                client,
                "复制文件",
                lambda write_client: write_client.fs_copy(src_file_id, target_cid),
            )
            logger.debug(f"[Sync-{drive_name}] 复制响应: state={(resp or {}).get('state')}")
            if not isinstance(resp, dict) or not resp.get('state'):
                logger.error(f"[Sync-{drive_name}] 复制失败: state={(resp or {}).get('state')}")
                return None

            # 4. 获取新文件信息 (参照 r302 逻辑)
            # 列出目标目录文件，按修改时间倒序排列 (o=user_ptime, asc=0)
            list_params = {
                "cid": target_cid,
                "o": "user_ptime",
                "asc": 0,
                "limit": 1
            }
            list_resp = await self._run_115_read_request(
                "复制后获取文件列表",
                lambda: client.fs_files(list_params),
            )
            logger.debug(f"[Sync-{drive_name}] 目标目录文件数: {len(list_resp.get('data', []))}")
            
            if not list_resp.get('state') or not list_resp.get('data'):
                logger.error(f"[Sync-{drive_name}] 复制后获取文件列表失败")
                return None

            # 取列表第一个（即最新的）文件
            new_file_data = list_resp['data'][0]
            new_pickcode = new_file_data.get('pc')
            new_file_id = new_file_data.get('fid')
            file_name = new_file_data.get('n', '')  # 获取文件名

            if not new_pickcode:
                return None

            # 6. 获取直链
            logger.debug(f"[Sync-{drive_name}] 副本就绪: {file_name}")
            logger.debug(f"[Sync-{drive_name}] 开始获取副本直链")
            direct_url = await self._fetch_download_url(new_pickcode, user_agent, emby_index=emby_index, direct_link_context=direct_link_context)

            if direct_url:
                url_preview = direct_url[:50] + "..." if len(direct_url) > 50 else direct_url
                logger.debug(f"[Sync-{drive_name}] 副本直链获取成功: {url_preview}")
                return {
                    "url": direct_url,
                    "file_id": new_file_id
                }
            else:
                logger.error(f"[Sync-{drive_name}] 副本直链获取失败")
            return None
        except Exception as e:
            logger.error(f"[Sync-{drive_name}] 复制流程异常: {e}")
            traceback.print_exc()
            return None

    async def _delayed_remove(self, file_id, delay=60):
        """延迟删除副本"""
        logger.debug(f"[CopyOnWrite] {delay}秒后清理副本: {file_id}")
        await asyncio.sleep(delay)
        logger.debug(f"[CopyOnWrite] 开始删除副本: {file_id}")
        try:
            client, _ = await self.get_client()
            if client:
                await run_115_write_request(
                    client,
                    "删除播放副本",
                    lambda write_client: write_client.fs_delete(file_id),
                    raise_on_state_false=False,
                )
                logger.debug(f"[CopyOnWrite] 副本已清理: {file_id}")
            else:
                logger.warning(f"[CopyOnWrite] 删除失败: 无法获取 115 客户端")
        except Exception as e:
            logger.warning(f"[CopyOnWrite] 副本清理失败: {e}")

    # ==========================================================
    # 🚀 115 秒传功能
    # ==========================================================

    async def _get_file_sha1_and_preupload_info(self, client, pickcode: str, user_agent: str = "", emby_index=None, direct_link_context: str = "default"):
        """
        获取文件的 SHA1 信息和预上传所需的验证数据（并行优化）

        Args:
            emby_index: Emby 配置索引，用于确定使用哪个 115 账号的 cookie

        返回: {
            'sha1': '完整文件SHA1',
            'size': 文件大小,
            'direct_url': '大号直链'
        } 或 None
        """
        # 1. 检查缓存
        if pickcode in self._sha1_cache:
            cached = self._sha1_cache[pickcode]
            # 115 直链签名和 User-Agent 相关；只有 UA 一致时才能复用直链。
            if cached.get('direct_url') and cached.get('direct_url_ua') == (user_agent or ""):
                return cached
            if cached.get('sha1') and cached.get('size'):
                cached = dict(cached)
                direct_url = await self._fetch_download_url(
                    pickcode,
                    user_agent,
                    emby_index=emby_index,
                    direct_link_context=direct_link_context,
                )
                if not direct_url:
                    logger.error(f"[Rapid] 无法获取大号直链: {pickcode}")
                    return None
                cached['direct_url'] = direct_url
                cached['direct_url_ua'] = user_agent or ""
                self._sha1_cache[pickcode] = cached
                return cached

        try:
            # 2. 先获取 SHA1 信息
            file_id = to_id(pickcode)
            attr = get_attr(client, file_id)
            if asyncio.iscoroutine(attr):
                attr = await attr
            if not attr or not attr.get('sha1'):
                logger.error(f"[Rapid] 无法获取文件 SHA1: {pickcode}")
                return None

            sha1 = attr['sha1'].upper()
            size = attr.get('size', 0)

            # 3. 获取大号直链
            direct_url = await self._fetch_download_url(pickcode, user_agent, emby_index=emby_index, direct_link_context=direct_link_context)
            if not direct_url:
                logger.error(f"[Rapid] 无法获取大号直链: {pickcode}")
                return None

            result = {
                'sha1': sha1,
                'size': size,
                'direct_url': direct_url,
                'direct_url_ua': user_agent or "",
            }

            # 缓存结果
            self._sha1_cache[pickcode] = result
            return result

        except Exception as e:
            logger.error(f"[Rapid] 获取 SHA1 信息异常: {e}")
            traceback.print_exc()
            return None

    async def _rapid_transfer_to_secondary(self, secondary_client, main_client, pickcode: str,
                                           sha1_info: dict, filename: str, target_dir: str, user_agent: str = ""):
        """
        执行秒传：用 SHA1 信息在小号创建文件引用

        Args:
            secondary_client: 小号 P115Client
            main_client: 大号 P115Client (用于下载验证数据)
            pickcode: 大号文件 pickcode
            sha1_info: SHA1 信息字典
            filename: 文件名
            target_dir: 目标目录

        返回: {
            'pickcode': '小号文件pickcode',
            'file_id': 小号文件ID
        } 或 None
        """
        try:
            # 1. 获取/创建项目统一的秒传目录，例如 /影视库/秒传目录。
            target_cid = await self._ensure_115_dir_id_by_path(
                secondary_client,
                target_dir,
                request_label="秒传目标目录",
            )
            if not target_cid:
                logger.error(f"[Rapid] 无法获取目标目录: {target_dir}")
                return None

            # 2. 【优化】复用已获取的直链，避免重复请求（节省约 300ms）
            download_url = sha1_info.get('direct_url')
            if not download_url:
                logger.error(f"[Rapid] SHA1 信息中缺少直链")
                return None

            logger.debug(f"[Rapid] 复用已获取的直链: {user_agent[:50] if user_agent else 'EMPTY'}...")

            # 3. 定义范围读取回调函数（用于二次验证）
            # 这个回调会在 status=7 时被调用，需要返回指定范围的 SHA1
            def read_range_callback(sign_check: str) -> str:
                """
                回调函数：接收范围字符串，返回该范围数据的 SHA1
                sign_check 格式: "0-131071" 或 "5110676549-5110864075"

                关键：使用与获取直链时完全相同的 User-Agent
                """
                try:
                    logger.debug(f"[Rapid] 开始下载验证数据: 范围={sign_check}")

                    # 使用与获取直链时完全相同的 User-Agent
                    headers = {
                        "Range": f"bytes={sign_check}",
                        "User-Agent": user_agent or "Mozilla/5.0",
                    }

                    # 使用 httpx 发起请求
                    resp = httpx.get(
                        download_url,
                        headers=headers,
                        timeout=_RAPID_RANGE_READ_TIMEOUT_SECONDS,
                        verify=False,
                        follow_redirects=True
                    )

                    logger.debug(f"[Rapid] HTTP响应状态: {resp.status_code}")

                    if resp.status_code in (200, 206):
                        import hashlib
                        data = resp.content
                        sha1_hash = hashlib.sha1(data).hexdigest().upper()
                        logger.debug(f"[Rapid] 验证数据SHA1计算成功: {sha1_hash[:16]}... (长度: {len(data)}字节)")
                        return sha1_hash
                    else:
                        logger.error(f"[Rapid] HTTP请求失败: 状态码={resp.status_code}, 响应={resp.text[:200] if resp.text else 'N/A'}")
                        return ""

                except Exception as e:
                    logger.error(f"[Rapid] 范围读取异常: {e}")
                    traceback.print_exc()
                    return ""

            # 4. 使用 p115client 的 upload_file_init 方法
            # 这个方法会自动处理 status=7 的验证流程
            logger.debug(f"[Rapid] 发起秒传请求: SHA1={sha1_info['sha1'][:16]}...")

            result = await self._run_115_read_request(
                "秒传初始化",
                lambda: secondary_client.upload_file_init(
                    filename=filename,
                    filesize=sha1_info['size'],
                    filesha1=sha1_info['sha1'],
                    read_range_bytes_or_hash=read_range_callback,  # 传入范围读取回调
                    pid=target_cid,
                    async_=False,
                ),
                timeout=_RAPID_UPLOAD_INIT_TIMEOUT_SECONDS,
            )

            if asyncio.iscoroutine(result):
                result = await result

            if not result or not result.get('state'):
                logger.error(f"[Rapid] upload_file_init 失败: {result}")
                return None

            # 5. 检查秒传结果
            # result['reuse'] = True 表示秒传成功
            if result.get('reuse'):
                # 注意：pickcode 直接在 result 根级别，不在 data 字段中
                pickcode = result.get('pickcode')
                logger.info(f"[Rapid] 秒传成功: pickcode={pickcode}")

                # 重要：API 返回的 fileid=0 不是真实文件ID
                # 需要像同播复制一样，列出目标目录获取真实的 fid
                try:
                    list_params = {
                        "cid": target_cid,
                        "o": "user_ptime",  # 按修改时间倒序
                        "asc": 0,
                        "limit": 1
                    }
                    list_resp = await self._run_115_read_request(
                        "秒传后获取文件列表",
                        lambda: secondary_client.fs_files(list_params),
                    )

                    if list_resp.get('state') and list_resp.get('data'):
                        new_file_data = list_resp['data'][0]
                        real_file_id = new_file_data.get('fid')  # 真正的文件ID
                        logger.debug(f"[Rapid] 获取真实文件ID: {real_file_id}")
                    else:
                        real_file_id = None
                        logger.warning(f"[Rapid] 无法获取真实文件ID")
                except Exception as e:
                    logger.warning(f"[Rapid] 获取真实文件ID异常: {e}")
                    real_file_id = None

                return {
                    'pickcode': pickcode,
                    'file_id': real_file_id  # 使用真实的文件ID
                }
            else:
                # 秒传未命中，需要完整上传
                status = result.get('status', 0)
                logger.warning(f"[Rapid] 秒传未命中 (status={status})，需要完整上传")
                return None

        except Exception as e:
            logger.error(f"[Rapid] 秒传异常: {e}")
            traceback.print_exc()
            return None

    async def _delayed_remove_secondary(self, secondary_client, file_id=None, pickcode=None, delay=60):
        """延迟删除小号上的副本

        Args:
            secondary_client: 小号客户端
            file_id: 文件ID（优先使用）
            pickcode: 文件pickcode（file_id 为 None 时使用）
            delay: 延迟秒数
        """
        await asyncio.sleep(delay)
        try:
            # 优先使用 file_id，其次使用 pickcode
            if file_id is not None:
                await run_115_write_request(
                    secondary_client,
                    "删除小号副本",
                    lambda write_client: write_client.fs_delete(file_id),
                    raise_on_state_false=False,
                )
                logger.debug(f"[Rapid] 小号副本已清理: file_id={file_id}")
            elif pickcode:
                # p115client 的 fs_delete 也支持 pickcode
                from p115client import P115Client
                file_id_to_delete = P115Client.to_id(pickcode)
                await run_115_write_request(
                    secondary_client,
                    "删除小号副本",
                    lambda write_client: write_client.fs_delete(file_id_to_delete),
                    raise_on_state_false=False,
                )
                logger.debug(f"[Rapid] 小号副本已清理: pickcode={pickcode}")
            else:
                logger.warning(f"[Rapid] 无法删除副本：缺少 file_id 和 pickcode")
        except Exception as e:
            logger.warning(f"[Rapid] 副本清理失败: {e}")

    async def _resolve_client_for_direct_link(self, emby_index: int | None = None, cookie: str | None = None):
        """解析用于获取直链的 115 客户端。"""
        explicit_cookie = str(cookie or "").strip()
        if explicit_cookie:
            client = self._clients.get(explicit_cookie)
            if client is None:
                client = P115Client(explicit_cookie)
                self._clients[explicit_cookie] = client
            return client

        resolved_emby_index = int(emby_index or 0)
        client, _ = await self.get_client(resolved_emby_index)
        return client

    async def _download_urls_via_client(self, pickcodes, user_agent: str = "", emby_index=None, cookie=None,
                                        direct_link_context: str = "default", log_label: str | None = None) -> dict[str, str]:
        normalized = []
        seen = set()
        for pickcode in (pickcodes or []):
            value = str(pickcode or "").strip()
            if value and value not in seen:
                seen.add(value)
                normalized.append(value)
        if not normalized:
            return {}

        try:
            client = await self._resolve_client_for_direct_link(emby_index=emby_index, cookie=cookie)
            if not client:
                logger.error("[115] 获取直链失败: 客户端不可用")
                return {}

            urls: dict[str, str] = {}
            batch_pickcodes = ",".join(normalized)
            label = str(log_label or "").strip()
            if not label and len(normalized) == 1:
                label = normalized[0]
            request_name = f"获取直链: {label}" if label else f"获取直链: {len(normalized)} 个文件"
            try:
                request_factory = lambda: asyncio.to_thread(
                    client.download_url_app,
                    {"pickcode": batch_pickcodes},
                    user_agent=user_agent or "Mozilla/5.0",
                    app="chrome",
                    async_=False,
                    timeout=_DIRECT_URL_DOWNLOAD_TIMEOUT_SECONDS,
                )
                if direct_link_context == "gateway_playback":
                    priority = DIRECT_URL_PRIORITY_PLAYBACK
                elif direct_link_context == "gateway_direct":
                    priority = DIRECT_URL_PRIORITY_DIRECT
                else:
                    priority = DIRECT_URL_PRIORITY_DEFAULT
                result = await _run_115_serial_request(
                    request_name,
                    request_factory,
                    priority=priority,
                )
            except Exception as e:
                logger.debug(f"[115] 批量获取直链失败: count={len(normalized)}, err={e}")
                return {}

            if not isinstance(result, dict) or not result.get("state"):
                logger.debug(f"[115] 批量获取直链失败: count={len(normalized)}, resp={result}")
                return {}

            data = result.get("data") or {}
            if isinstance(data, dict) and "url" in data and len(normalized) == 1:
                final_url = str(data.get("url") or "").strip()
                if final_url:
                    urls[normalized[0]] = final_url
                return urls

            for item in data.values() if isinstance(data, dict) else []:
                if not isinstance(item, dict):
                    continue
                item_pickcode = str(item.get("pick_code") or item.get("pickcode") or "").strip()
                item_url = item.get("url") or {}
                final_url = ""
                if isinstance(item_url, dict):
                    final_url = str(item_url.get("url") or "").strip()
                elif isinstance(item_url, str):
                    final_url = item_url.strip()
                if item_pickcode and final_url:
                    urls[item_pickcode] = final_url
            return urls
        except Exception as e:
            logger.error(f"[115] 批量获取直链异常: {e}")
            return {}

    async def _fetch_download_url(self, pickcode, user_agent, cookie=None, emby_index=None,
                                  direct_link_context: str = "default", log_label: str | None = None):
        """通过 p115client download_url(s) 获取直链。"""
        normalized_pickcode = str(pickcode or "").strip()
        if not normalized_pickcode:
            return None
        urls = await self._download_urls_via_client(
            [normalized_pickcode],
            user_agent=user_agent,
            emby_index=emby_index,
            cookie=cookie,
            direct_link_context=direct_link_context,
            log_label=log_label,
        )
        return urls.get(normalized_pickcode)

    async def _get_emby_media_source(self, emby_cfg, item_id, media_source_id=None):
        base_url = emby_cfg.get("url", "").rstrip("/")
        api_key = emby_cfg.get("key", "")
        if not base_url or not api_key:
            return None

        url = f"{base_url}/emby/Items/{item_id}/PlaybackInfo?api_key={api_key}"
        resp = await self._get_http_client().post(url, json={"Profile": "Unknown"}, timeout=5.0)
        if resp.status_code != 200:
            return None

        data = resp.json()
        media_sources = data.get("MediaSources", [])
        if not isinstance(media_sources, list) or not media_sources:
            return None

        if media_source_id:
            for source in media_sources:
                if source.get("Id") == media_source_id:
                    return source

        return media_sources[0]

    async def _get_emby_file_path(self, emby_cfg, item_id, media_source_id=None):
        try:
            target_source = await self._get_emby_media_source(emby_cfg, item_id, media_source_id)
            if not target_source:
                return None
            path = target_source.get("Path", "")
            return str(path or "").strip() or None
        except Exception as e:
            logger.debug(f"[115] 读取 Emby 媒体路径失败: {e}")
            return None

    async def _resolve_pickcode_from_strm(self, emby_cfg, item_id, media_source_id):
        """
        Pickcode 模式：从 strm 文件内容提取 pickcode
        strm 文件格式示例：
        http://192.168.31.185:3032/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=dhk4epvs9d3lxx225
        或
        http://xxx.xxx/api/xxx?pickcode=xxxxx

        返回: pickcode 字符串 或 None
        """
        try:
            base_url = emby_cfg.get("url", "").rstrip("/")
            api_key = emby_cfg.get("key", "")
            if not base_url or not api_key:
                return None

            target_source = await self._get_emby_media_source(emby_cfg, item_id, media_source_id)
            if not target_source:
                return None

            # 2. 检查是否为 strm 文件或包含 pickcode 的 URL
            media_type = target_source.get("Container", "").lower()
            path = target_source.get("Path", "")

            logger.debug(f"[115] Pickcode模式检测: media_type={media_type}, path={path}")

            # 判断条件：
            # 1. media_type 是 strm
            # 2. path 以 .strm 结尾
            # 3. path 中包含 pickcode= 参数（直接在 Path 中）
            is_strm = (
                media_type == "strm" or
                path.endswith(".strm") or
                "pickcode=" in path or
                "fileId=" in path or
                "/d/" in path or
                "/api/strm/play/" in path
            )

            if not is_strm:
                logger.debug("[115] 非Pickcode STRM，回退路径解析")
                return None

            # 3. 如果 path 直接包含 pickcode，直接提取（不需要读取文件内容）
            # 支持 ?pickcode=xxx, &pickcode=xxx, ?fileId=xxx, /d/{pickcode}, /api/strm/play/{pickcode} 等格式
            if "pickcode=" in path or "fileId=" in path or "/d/" in path or "/api/strm/play/" in path:
                extract_patterns = [
                    r"[?&]pickcode=([^&\s]+)",      # ?pickcode=xxx 或 &pickcode=xxx
                    r"[?&]fileId=([^&\s]+)",        # ?fileId=xxx 或 &fileId=xxx
                    r"/pickcode/([^/\s]+)",         # /pickcode/xxx
                    r"/d/([a-zA-Z0-9]+)",           # /d/{pickcode}
                    r"/api/strm/play/([^/?&\s]+)", # /api/strm/play/{pickcode}
                ]
                for pattern in extract_patterns:
                    match = re.search(pattern, path)
                    if match:
                        extracted_pickcode = match.group(1)
                        if extracted_pickcode and len(extracted_pickcode) >= 6:
                            logger.debug(f"[115] 从Path提取Pickcode成功: {extracted_pickcode}")
                            return extracted_pickcode

            # 4. 否则，读取 strm 文件内容
            # strm 文件路径可能是本地路径或网络路径
            if path.startswith("http://") or path.startswith("https://"):
                # 网络路径，直接获取内容
                try:
                    strm_resp = await self._get_http_client().get(path, timeout=5.0)
                    if strm_resp.status_code == 200:
                        strm_content = strm_resp.text.strip()
                    else:
                        return None
                except:
                    return None
            else:
                # 本地路径，尝试通过 Emby API 读取文件内容
                # 尝试多个可能的 API 端点
                strm_content = None
                api_endpoints = [
                    f"{base_url}/emby/Items/{item_id}/File?api_key={api_key}",
                    f"{base_url}/emby/Items/{item_id}/Download?api_key={api_key}",
                    f"{base_url}/Videos/{item_id}/stream?static=true&api_key={api_key}",
                ]

                for endpoint in api_endpoints:
                    try:
                        logger.debug(f"[115] 尝试通过Emby API读取strm: {endpoint}")
                        file_resp = await self._get_http_client().get(endpoint, timeout=5.0)
                        logger.debug(f"[115] API响应: status={file_resp.status_code}, content-type={file_resp.headers.get('content-type', 'N/A')}")
                        if file_resp.status_code == 200:
                            strm_content = file_resp.text.strip()
                            logger.debug(f"[115] 读取strm内容成功，长度={len(strm_content)}")
                            break
                        else:
                            logger.debug(f"[115] API返回状态码: {file_resp.status_code}")
                    except Exception as e:
                        logger.debug(f"[115] API请求异常: {e}")

                if not strm_content:
                    logger.warning("[115] 所有API端点均无法读取strm内容")
                    return None

            # 5. 从 URL 中提取 pickcode
            # 支持多种 URL 格式：
            # - http://xxx/api/xxx?pickcode=xxxxx
            # - http://xxx/api/xxx&pickcode=xxxxx
            # - http://xxx/api/xxx/pickcode/xxxxx
            # - http://xxx/api/?fileId=xxxxx
            # - http://host:port/d/{pickcode}
            # - http://host:port/d/{pickcode}?/电影.mkv
            # - http://host:port/api/strm/play/{pickcode}
            pickcode_patterns = [
                r"[?&]pickcode=([^&\s]+)",      # ?pickcode=xxx 或 &pickcode=xxx
                r"[?&]fileId=([^&\s]+)",        # ?fileId=xxx 或 &fileId=xxx
                r"/pickcode/([^/\s]+)",         # /pickcode/xxx
                r"/d/([a-zA-Z0-9]+)",           # /d/{pickcode}
                r"/api/strm/play/([^/?&\s]+)", # /api/strm/play/{pickcode}
            ]

            for pattern in pickcode_patterns:
                match = re.search(pattern, strm_content)
                if match:
                    extracted_pickcode = match.group(1)
                    # 验证 pickcode 格式（通常是字母和数字的组合，长度约 6-20）
                    if extracted_pickcode and len(extracted_pickcode) >= 6:
                        logger.debug(f"[115] 从strm提取Pickcode成功: {extracted_pickcode}")
                        return extracted_pickcode

            logger.debug(f"[115] strm内容未包含有效Pickcode: {strm_content[:100]}")
            return None

        except Exception as e:
            logger.debug(f"[115] Pickcode 模式解析异常: {repr(e)}")
            return None


    async def execute_all_signin_tasks(self, trigger: str = "manual"):
        cfg = await get_config_302()
        drives = cfg.get("drives", [])
        drive_config = drives[0] if isinstance(drives, list) and drives else cfg.get("drive", {})

        if not isinstance(drive_config, dict) or not drive_config:
            logger.info(f"[SignIn] 未找到已绑定的 115 账号，跳过签到 ({trigger})")
            return []

        results = []
        logger.info(f"[SignIn] 开始批量签到 ({trigger})")

        if drive_config.get("cookie"):
            result = await self.execute_signin_task(
                drive_config,
                account_type="main",
                account_index=0,
                trigger=trigger,
                drive_index=0
            )
            if result:
                results.append(result)

        rapid_accounts = drive_config.get("rapid_accounts", [])
        for account_index, account in enumerate(rapid_accounts):
            if not account.get("cookie"):
                continue
            result = await self.execute_signin_task(
                drive_config,
                account_type="rapid",
                account_index=account_index,
                trigger=trigger,
                drive_index=0
            )
            if result:
                results.append(result)

        total_accounts = len(results)
        if total_accounts == 0:
            logger.info(f"[SignIn] 未找到有效 Cookie，跳过签到 ({trigger})")
            return []

        success_count = sum(1 for item in results if item.get("status") == "success")
        already_count = sum(1 for item in results if item.get("status") == "already")
        failed_count = sum(1 for item in results if item.get("status") == "failed")
        overall_status = "success" if failed_count == 0 else "error"
        summary = f"成功 {success_count}，已签 {already_count}，失败 {failed_count}"
        detail_lines = []
        for item in results[:8]:
            status_text = {
                "success": "签到成功",
                "already": "今日已签到",
                "failed": "签到失败",
            }.get(item.get("status"), "未知状态")
            line = f"• {item.get('account_name', '未知账号')}：{status_text}"
            if item.get("message"):
                line += f"（{item['message']}）"
            detail_lines.append(line)
        if len(results) > 8:
            detail_lines.append(f"• 其余 {len(results) - 8} 个账号结果已省略")

        notify_kwargs = {
            "task_name": "115签到",
            "status": overall_status,
            "task_category": "signin",
            "trigger": trigger,
            "total_count": total_accounts,
            "success_count": success_count,
            "already_count": already_count,
            "failed": failed_count,
            "summary": summary,
            "detail": summary,
            "accounts_text": "\n".join(detail_lines),
        }
        wechat_notify_service.notify_task_complete(**notify_kwargs)
        telegram_notify_service.notify_task_complete(**notify_kwargs)

        logger.info(f"[SignIn] 批量签到结束，共处理 {total_accounts} 个账号 ({trigger})")
        return results

    async def execute_signin_task(self, drive_config: dict, account_type: str = "main", account_index: int = 0, trigger: str = "manual", drive_index: int = 0):
        if account_type == "main":
            name = drive_config.get("name", f"主号{drive_index + 1}")
            cookie = drive_config.get("cookie", "")
        else:
            rapid_accounts = drive_config.get("rapid_accounts", [])
            if account_index >= len(rapid_accounts):
                return None
            account = rapid_accounts[account_index]
            name = account.get("name", f"小号{account_index + 1}")
            cookie = account.get("cookie", "")

        if not cookie:
            return None

        try:
            logger.info(f"[SignIn] 开始签到账号: {name} (类型: {account_type}, 触发: {trigger})")
            client = P115Client(cookie)
            before = await asyncio.to_thread(client.user_points_sign, app="android")
            result = await asyncio.to_thread(client.user_points_sign_post, app="android")

            if result.get("state"):
                reward = result.get("data") or result.get("reward") or result.get("continuous_day") or result.get("score")
                suffix = ""
                if isinstance(reward, (int, float, str)) and reward not in (None, ""):
                    suffix = f" | 返回: {reward}"
                logger.info(f"[SignIn] 签到成功: {name}{suffix}")
                return {
                    "account_name": name,
                    "account_type": account_type,
                    "status": "success",
                    "message": "",
                }

            detail = result.get("error") or result.get("message") or result.get("msg") or "未知错误"
            before_text = json.dumps(before, ensure_ascii=False) if isinstance(before, dict) else str(before)
            detail_text = str(detail)
            combined = f"{detail_text} | before={before_text}"
            if "已签到" in combined or "already" in combined.lower() or "today" in combined.lower():
                logger.info(f"[SignIn] 今日已签到: {name}")
                return {
                    "account_name": name,
                    "account_type": account_type,
                    "status": "already",
                    "message": "今日已签到",
                }

            logger.warning(f"[SignIn] 签到失败: {name} - {detail_text}")
            return {
                "account_name": name,
                "account_type": account_type,
                "status": "failed",
                "message": detail_text,
            }
        except Exception as e:
            logger.error(f"[SignIn] 任务执行异常: {name} - {e}")
            return {
                "account_name": name,
                "account_type": account_type,
                "status": "failed",
                "message": str(e),
            }

    async def execute_cleanup_task(self, drive_config: dict, account_type: str = "main", account_index: int = 0):
        """执行单个 115 账号的清理任务

        Args:
            drive_config: 驱动配置
            account_type: 账号类型 ("main" 主号 或 "rapid" 小号)
            account_index: 账号索引（用于小号池）
        """
        if account_type == "main":
            name = drive_config.get('name', '主号')
            cookie = drive_config.get('cookie')
            remote_root_name = str(drive_config.get("remote_root_name") or "影视库").strip().strip("/") or "影视库"
            upload_dir = drive_config.get('upload_dir') or f"/{remote_root_name}/秒传目录"
            recycle_code = drive_config.get('recycle_code', '')
        else:
            # 小号配置
            rapid_accounts = drive_config.get('rapid_accounts', [])
            if account_index >= len(rapid_accounts):
                return
            account = rapid_accounts[account_index]
            name = account.get('name', f'小号{account_index + 1}')
            cookie = account.get('cookie', '')
            # 小号统一清理项目创建的秒传目录，不再支持单独配置目录。
            upload_dir = drive_config.get('upload_dir', '/影视库/秒传目录')
            recycle_code = account.get('recycle_code', drive_config.get('recycle_code', ''))

        if not cookie: return

        try:
            logger.info(f"[CleanUp] 开始清理账号: {name} (类型: {account_type})")
            client = P115Client(cookie)
            target_cid = await self._resolve_115_dir_id_by_path(
                client,
                upload_dir,
                request_label="查询清理目录",
            )
            if not target_cid or str(target_cid) == "0":
                logger.warning(f"[CleanUp] 目录不存在: {upload_dir}")
                return

            deleted_count = 0
            max_iterations = 100
            for _ in range(max_iterations):
                resp = await self._run_115_read_request(
                    "列出清理目录",
                    lambda: client.fs_files({'cid': target_cid, 'limit': 1000}),
                )
                file_list = self._extract_115_list_items(resp)
                if not file_list: break
                fids = []
                for item in file_list:
                    item_id = self._extract_cleanup_item_id(item)
                    if item_id:
                        fids.append(item_id)
                if not fids: break
                await run_115_write_request(
                    client,
                    "清理复制目录",
                    lambda write_client: write_client.fs_delete(fids),
                    raise_on_state_false=False,
                )
                deleted_count += len(fids)
                logger.debug(f"[CleanUp] 本轮已删除 {len(fids)} 个文件")
                await asyncio.sleep(0.5)

            if deleted_count > 0:
                logger.info(f"[CleanUp] 目录清理完成: 共删除 {deleted_count} 个文件")
            else:
                logger.info(f"[CleanUp] 目录为空")

            # 清空回收站
            try:
                headers = {
                    "Cookie": cookie,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": "https://115.com",
                    "Referer": "https://115.com/"
                }
                data = {"password": recycle_code} if recycle_code else {}
                async with httpx.AsyncClient(timeout=10.0) as http_client:
                    resp = await http_client.post("https://webapi.115.com/rb/clean", data=data, headers=headers)
                    if resp.json().get("state"):
                        logger.info("[CleanUp] 回收站已清空")
                    else:
                        logger.warning(f"[CleanUp] 回收站清空失败: {resp.json().get('error')}")
            except Exception as ex_raw:
                 logger.warning(f"[CleanUp] 回收站清空异常: {ex_raw}")

        except Exception as e:
            logger.error(f"[CleanUp] 任务执行异常: {e}")

    async def _clear_recycle_bin(self, cookie: str, recycle_code: str = "") -> bool:
        try:
            headers = {
                "Cookie": cookie,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://115.com",
                "Referer": "https://115.com/",
            }
            data = {"password": recycle_code} if recycle_code else {}
            async with httpx.AsyncClient(timeout=10.0) as http_client:
                resp = await http_client.post("https://webapi.115.com/rb/clean", data=data, headers=headers)
                payload = resp.json()
            if payload.get("state"):
                logger.info("[CleanUp] 回收站已清空")
                return True
            logger.warning(f"[CleanUp] 回收站清空失败: {payload.get('error')}")
            return False
        except Exception as ex_raw:
            logger.warning(f"[CleanUp] 回收站清空异常: {ex_raw}")
            return False

    def _extract_cleanup_item_id(self, item: dict) -> int | None:
        for key in ("fid", "cid", "id"):
            value = item.get(key)
            if value is None or value == "":
                continue
            try:
                item_id = int(value)
                return item_id if item_id > 0 else None
            except (TypeError, ValueError):
                continue
        return None

    def _is_cleanup_dir_item(self, item: dict) -> bool:
        if not isinstance(item, dict):
            return False
        if str(item.get("fc", "") or "") == "0":
            return True
        return bool(item.get("is_dir") or item.get("is_directory"))

    async def collect_115_folder_child_ids(self, client, folder_cid: str, folder_label: str) -> dict:
        """Collect only the selected folder's direct children.

        Folder children are submitted as folder IDs and are not expanded into their
        nested files, matching the source cleanup behavior of deleting outermost
        entries and letting 115 handle recursive directory deletion.
        """
        normalized_cid = str(folder_cid or "").strip()
        if not normalized_cid.isdigit() or normalized_cid == "0":
            return {"status": "error", "cid": normalized_cid, "label": folder_label, "ids": [], "message": "禁止清空根目录或无效目录"}

        try:
            def collect_ids() -> dict[str, list[int]]:
                from p115client.tool.fs_files import iter_fs_files
                dir_ids: list[int] = []
                file_ids: list[int] = []
                for page in iter_fs_files(
                    client,
                    {"cid": int(normalized_cid), "show_dir": 1, "fc_mix": 1},
                    page_size=_115_APP_LIST_SAFE_PAGE_SIZE,
                    max_workers=0,
                    app="android",
                ):
                    for item in self._extract_115_list_items(page):
                        item_id = self._extract_cleanup_item_id(item)
                        if item_id:
                            if self._is_cleanup_dir_item(item):
                                dir_ids.append(item_id)
                            else:
                                file_ids.append(item_id)
                return {"dir_ids": dir_ids, "file_ids": file_ids}

            collected = await run_115_read_request("收集清空目标", collect_ids, timeout=1800)
        except Exception as e:
            message = f"列出目录失败: {folder_label} | {e}"
            logger.warning(f"[CleanUp] {message}")
            return {"status": "error", "cid": normalized_cid, "label": folder_label, "ids": [], "message": message}

        unique_dir_ids = list(dict.fromkeys((collected or {}).get("dir_ids") or []))
        unique_file_ids = list(dict.fromkeys((collected or {}).get("file_ids") or []))
        unique_ids = list(dict.fromkeys(unique_dir_ids + unique_file_ids))
        logger.info(
            f"[CleanUp] 已收集清空目标: {folder_label} | 最外层待删 {len(unique_ids)} 项 "
            f"(目录 {len(unique_dir_ids)}，文件 {len(unique_file_ids)})"
        )
        return {
            "status": "ok",
            "cid": normalized_cid,
            "label": folder_label,
            "ids": unique_ids,
            "dir_ids": unique_dir_ids,
            "file_ids": unique_file_ids,
            "message": "",
        }

    async def _delete_115_ids_batched_with_retry(self, client, ids: list[int], log_label: str):
        if not ids:
            return True, None
        last_error = None
        unique_ids = list(dict.fromkeys(int(item_id) for item_id in ids if int(item_id or 0) > 0))
        if not unique_ids:
            return True, None
        batches = [
            unique_ids[index:index + _115_CLEANUP_DELETE_BATCH_SIZE]
            for index in range(0, len(unique_ids), _115_CLEANUP_DELETE_BATCH_SIZE)
        ]
        for batch_index, batch_ids in enumerate(batches, start=1):
            batch_label = f"{log_label} batch={batch_index}/{len(batches)} size={len(batch_ids)}"
            for attempt, delay in enumerate(_115_CLEANUP_DELETE_RETRY_DELAYS_SECONDS, start=1):
                if delay > 0:
                    logger.warning(f"[CleanUp] 批量删除失败，{delay:.0f}秒后重试: {batch_label}")
                    await asyncio.sleep(delay)
                try:
                    resp = await run_115_write_request(
                        client,
                        "清空指定目录",
                        lambda write_client, _ids=batch_ids: write_client.fs_delete_app(_ids, async_=False),
                        raise_on_state_false=False,
                        timeout=_115_CLEANUP_DELETE_TIMEOUT_SECONDS,
                    )
                    if isinstance(resp, dict) and resp.get("state") is False:
                        raise RuntimeError(resp)
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    if attempt < len(_115_CLEANUP_DELETE_RETRY_DELAYS_SECONDS):
                        continue
            if last_error is not None:
                return False, last_error
            if batch_index < len(batches):
                await asyncio.sleep(_115_CLEANUP_DELETE_BATCH_PAUSE_SECONDS)
        return True, None

    async def _wait_115_cleanup_folder_state(self, client, folder_cid: str, folder_label: str, *, remaining_key: str, remaining_label: str):
        last_result = {"status": "ok", "ids": [], "message": ""}
        for delay in _115_CLEANUP_VERIFY_DELAYS_SECONDS:
            await asyncio.sleep(delay)
            last_result = await self.collect_115_folder_child_ids(client, folder_cid, folder_label)
            if last_result.get("status") != "ok":
                return False, last_result
            remaining_ids = list(dict.fromkeys(last_result.get(remaining_key) or []))
            if not remaining_ids:
                return True, last_result
            logger.info(
                f"[CleanUp] 目录仍在删除中，继续等待复查: {folder_label} | "
                f"剩余{remaining_label} {len(remaining_ids)} 项 | 本轮已等待 {int(delay)} 秒"
            )
        return False, last_result

    async def execute_selected_folder_cleanup_task(self, task: dict, manual: bool = False) -> dict:
        task_name = str(task.get("name") or "115定时清空").strip() or "115定时清空"
        drive_index = int(task.get("drive_index") or 0)
        folders = task.get("folders") if isinstance(task.get("folders"), list) else []
        if not folders:
            return {"status": "error", "deleted_count": 0, "message": "未选择清空目录"}

        cfg = await get_config_302()
        drives = cfg.get("drives", [])
        if isinstance(drives, list) and drives:
            drive_cfg = drives[drive_index] if 0 <= drive_index < len(drives) else drives[0]
        else:
            drive_cfg = cfg.get("drive", {})
        cookie = str((drive_cfg or {}).get("cookie", "") or "").strip()
        recycle_code = str((drive_cfg or {}).get("recycle_code", "") or "")
        if not cookie:
            return {"status": "error", "deleted_count": 0, "message": "115 Cookie 未配置"}

        client = P115Client(cookie)
        logger.info(f"[CleanUp] 开始执行定时清空: {task_name} | 目录 {len(folders)} 个 | 触发={'手动' if manual else '定时'}")

        total_deleted_count = 0
        folder_results = []
        for folder_index, folder in enumerate(folders, start=1):
            cid = str((folder or {}).get("cid", "") or "").strip()
            label = str((folder or {}).get("path") or (folder or {}).get("name") or cid).strip()
            if not cid.isdigit() or cid == "0" or label in {"", "/", "根目录"}:
                message = f"跳过无效目录: {label or cid}"
                folder_results.append({"cid": cid, "label": label, "status": "error", "deleted_count": 0, "message": message})
                logger.warning(f"[CleanUp] {message}")
                continue

            logger.info(f"[CleanUp] 开始清空目录: {task_name} | {label} | {folder_index}/{len(folders)}")
            deleted_count = 0
            result = await self.collect_115_folder_child_ids(client, cid, label)
            if result.get("status") != "ok":
                message = result.get("message", "") or f"列出目录失败: {label}"
                folder_results.append({"cid": cid, "label": label, "status": "error", "deleted_count": deleted_count, "message": message})
                logger.error(f"[CleanUp] {message}")
                return {"status": "error", "deleted_count": total_deleted_count, "folders": folder_results, "message": message}

            dir_ids = list(dict.fromkeys(result.get("dir_ids") or []))
            if dir_ids:
                logger.info(f"[CleanUp] 准备按批删除目录最外层文件夹: {task_name} | {label} | {len(dir_ids)} 项")
                await asyncio.sleep(_115_CLEANUP_PRE_DELETE_DELAY_SECONDS)
                ok, error = await self._delete_115_ids_batched_with_retry(client, dir_ids, f"{task_name} {label} dirs={len(dir_ids)}")
                if not ok:
                    message = f"按批删除文件夹失败: {label} | {error}"
                    folder_results.append({"cid": cid, "label": label, "status": "error", "deleted_count": deleted_count, "message": message})
                    logger.error(f"[CleanUp] {message}")
                    return {"status": "error", "deleted_count": total_deleted_count, "folders": folder_results, "message": message}

                deleted_count += len(dir_ids)
                dirs_done, verify_result = await self._wait_115_cleanup_folder_state(
                    client,
                    cid,
                    label,
                    remaining_key="dir_ids",
                    remaining_label="最外层文件夹",
                )
                if not dirs_done:
                    if verify_result.get("status") != "ok":
                        message = verify_result.get("message", "") or f"复查目录失败: {label}"
                    else:
                        remaining = len(list(dict.fromkeys(verify_result.get("dir_ids") or [])))
                        message = f"目录清空未完成: {label} | 最后一次等待 60 秒后仍剩余最外层文件夹 {remaining} 项"
                    folder_results.append({"cid": cid, "label": label, "status": "error", "deleted_count": deleted_count, "message": message})
                    logger.error(f"[CleanUp] {message}")
                    return {"status": "error", "deleted_count": total_deleted_count, "folders": folder_results, "message": message}
                result = verify_result

            file_ids = list(dict.fromkeys(result.get("file_ids") or []))
            if file_ids:
                logger.info(f"[CleanUp] 准备按批删除目录最外层文件: {task_name} | {label} | {len(file_ids)} 项")
                await asyncio.sleep(_115_CLEANUP_PRE_DELETE_DELAY_SECONDS)
                ok, error = await self._delete_115_ids_batched_with_retry(client, file_ids, f"{task_name} {label} files={len(file_ids)}")
                if not ok:
                    message = f"按批删除文件失败: {label} | {error}"
                    folder_results.append({"cid": cid, "label": label, "status": "error", "deleted_count": deleted_count, "message": message})
                    logger.error(f"[CleanUp] {message}")
                    return {"status": "error", "deleted_count": total_deleted_count, "folders": folder_results, "message": message}

                deleted_count += len(file_ids)
                empty, verify_result = await self._wait_115_cleanup_folder_state(
                    client,
                    cid,
                    label,
                    remaining_key="ids",
                    remaining_label="最外层条目",
                )
                if not empty:
                    if verify_result.get("status") != "ok":
                        message = verify_result.get("message", "") or f"复查目录失败: {label}"
                    else:
                        remaining = len(list(dict.fromkeys(verify_result.get("ids") or [])))
                        message = f"目录清空未完成: {label} | 最后一次等待 60 秒后仍剩余最外层 {remaining} 项"
                    folder_results.append({"cid": cid, "label": label, "status": "error", "deleted_count": deleted_count, "message": message})
                    logger.error(f"[CleanUp] {message}")
                    return {"status": "error", "deleted_count": total_deleted_count, "folders": folder_results, "message": message}

            if deleted_count <= 0:
                folder_results.append({
                    "cid": cid,
                    "label": label,
                    "status": result.get("status"),
                    "deleted_count": 0,
                    "message": result.get("message", "") or "没有待删除项",
                })
                continue
            total_deleted_count += deleted_count
            folder_results.append({"cid": cid, "label": label, "status": "ok", "deleted_count": deleted_count, "message": "清理完成"})
            logger.info(f"[CleanUp] 目录内容删除完成: {task_name} | {label} | 已删除 {deleted_count} 项")

        if total_deleted_count <= 0:
            logger.info(f"[CleanUp] 定时清空完成: {task_name} | 没有待删除项")
            return {"status": "ok", "deleted_count": 0, "folders": folder_results, "message": "没有待删除项"}

        logger.info(f"[CleanUp] 定时清空完成: {task_name} | 已删除 {total_deleted_count} 项")
        if task.get("clear_recycle_bin", True):
            await self._clear_recycle_bin(cookie, recycle_code)
        return {"status": "ok", "deleted_count": total_deleted_count, "folders": folder_results, "message": f"清理完成，共删除 {total_deleted_count} 项"}

    async def close(self):
        loop_id = id(asyncio.get_running_loop())
        with self._http_clients_lock:
            client = self._http_clients.pop(loop_id, None)
        if client and not client.is_closed:
            await client.aclose()
            logger.info("[115Service] HTTP 客户端已关闭")

drive115_service = Drive115Service()
