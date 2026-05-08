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
from app.services.media_organize_115_ops import _run_115_serial_request
from p115pickcode import to_id
from p115client.tool.attr import get_attr
from app.routers.config_302 import get_config_302
from app.services.wechat_service import wechat_notify_service
from app.services.telegram_service import telegram_notify_service
from core.logger import logger

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
        # 这既是直链缓存，也是"并发锁"的依据。
        # 如果能在这个缓存里找到某个 pickcode 的记录，说明该文件当前"正在被播放"
        self._url_cache = TTLCache(maxsize=1000, ttl=1200) # 20分钟有效
        self._url_cache_hit_log_dedupe = TTLCache(maxsize=2000, ttl=10)

        # === [第五级缓存] Pickcode -> SHA1信息（用于秒传） ===
        self._sha1_cache = TTLCache(maxsize=1000, ttl=3600)  # SHA1 缓存

        # === [小号池轮询计数器] ===
        self._rapid_account_index = 0  # 当前轮询到的账号索引

        # 并发锁
        self._item_locks = {}
        self._locks_cleanup_lock = asyncio.Lock()
        self._direct_url_batch_lock = threading.Lock()
        self._gateway_playback_direct_url_lock = asyncio.Lock()

        self._last_direct_url_batch_at = 0.0
        self._last_gateway_playback_direct_url_at = 0.0

        # 全局 HTTP 客户端
        self._http_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True, verify=False)

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
                logger.info(f"[115] 客户端已就绪: {drive_name}")
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

    async def _run_gateway_playback_direct_url_request(self, request_name: str, request_factory):
        async with self._gateway_playback_direct_url_lock:
            now = time.monotonic()
            pacing_seconds = random.uniform(1.0, 1.5)
            wait_seconds = pacing_seconds - (now - self._last_gateway_playback_direct_url_at)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            try:
                return await _run_115_serial_request(request_name, request_factory)
            finally:
                self._last_gateway_playback_direct_url_at = time.monotonic()

    async def get_secondary_client(self):
        """获取或初始化小号 P115Client（用于秒传）- 支持多账号池

        返回: (client, drive_cfg, rapid_cookie) 或 (None, {}, None)
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

        if not rapid_accounts:
            return None, {}, None

        # 根据调度策略选择小号
        selected_account = None
        account_index = 0

        if rapid_mode == "auto":
            # 自动轮询：按顺序循环使用小号
            account_index = self._rapid_account_index % len(rapid_accounts)
            self._rapid_account_index += 1  # 为下次请求递增
            selected_account = rapid_accounts[account_index]
        elif rapid_mode in [str(i) for i in range(len(rapid_accounts))]:
            # 指定账号（固定使用某个账号）
            account_index = int(rapid_mode)
            selected_account = rapid_accounts[account_index]
        else:
            # 无效的 rapid_mode，回退到第一个账号
            account_index = 0
            selected_account = rapid_accounts[0]

        rapid_cookie = selected_account.get("cookie", "")
        account_name = selected_account.get("name", f"小号{account_index + 1}")

        if not rapid_cookie:
            return None, {}, None

        try:
            client = P115Client(rapid_cookie)
            logger.debug(f"[Rapid] 使用小号: {account_name} (模式: {rapid_mode}, 索引: {account_index}/{len(rapid_accounts)})")
            return client, drive_cfg, rapid_cookie
        except Exception as e:
            logger.error(f"[Rapid] 小号登录失败 ({account_name}): {e}")
            return None, {}, None

    # ==========================================================
    # [修改] 增加 item_name 参数，用于优化日志显示
    # ==========================================================
    async def get_direct_url(self, item_id: str, media_source_id: str = None, user_agent: str = "", item_name: str = None, emby_index: int = 0, direct_link_context: str = "default"):
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

                return await self._get_direct_url_core(
                    client=client,
                    drive_cfg=drive_cfg,
                    pickcode=pickcode,
                    user_agent=user_agent,
                    log_name=item_name if item_name else f"ID: {item_id}",
                    emby_index=emby_index,
                    filename_resolver=lambda: self._resolve_filename_for_item(item_id, media_source_id, emby_index),
                    direct_link_context=direct_link_context,
                )
            except Exception as e:
                logger.error(f"[115] 获取直链异常: {e}")
                traceback.print_exc()
                return None

    async def get_direct_url_by_pickcode(self, pickcode: str, user_agent: str = "", emby_index: int = 0, filename: str | None = None, direct_link_context: str = "default"):
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

                return await self._get_direct_url_core(
                    client=client,
                    drive_cfg=drive_cfg,
                    pickcode=normalized_pickcode,
                    user_agent=user_agent,
                    log_name=filename or normalized_pickcode,
                    emby_index=emby_index,
                    filename_resolver=lambda: filename or f"{normalized_pickcode}.mkv",
                    direct_link_context=direct_link_context,
                )
            except Exception as e:
                logger.error(f"[115] 按 Pickcode 获取直链异常: {e}")
                traceback.print_exc()
                return None

    async def _get_direct_url_core(self, client, drive_cfg: dict, pickcode: str, user_agent: str = "", log_name: str | None = None, emby_index: int = 0, filename_resolver=None, direct_link_context: str = "default"):
        drive_name = drive_cfg.get("name", f"drives[{emby_index}]")
        display_name = log_name if log_name else pickcode

        enable_sync = drive_cfg.get('enable_sync', False)
        enable_rapid = drive_cfg.get('enable_rapid', False)

        cache_ua = user_agent if user_agent else "NoUA"
        cache_key = f"{pickcode}_{cache_ua}"
        is_busy = self._is_file_busy(pickcode)

        if cache_key in self._url_cache and not (enable_rapid and is_busy):
            if cache_key not in self._url_cache_hit_log_dedupe:
                self._url_cache_hit_log_dedupe[cache_key] = True
                logger.debug(f"[Cache-{drive_name}] 命中直链缓存: {display_name}")
            return self._url_cache[cache_key]

        if enable_rapid:
            rapid_cache_key = f"rapid_{pickcode}"
            rapid_cache_entry = self._url_cache.get(rapid_cache_key)
            if isinstance(rapid_cache_entry, dict):
                rapid_cache_ua = str(rapid_cache_entry.get("ua") or "")
                rapid_cache_url = str(rapid_cache_entry.get("url") or "")
                if rapid_cache_url and rapid_cache_ua == cache_ua:
                    logger.debug(f"[Rapid-{drive_name}] 命中秒传缓存: {display_name}")
                    return rapid_cache_url
            elif rapid_cache_entry is not None:
                self._url_cache.pop(rapid_cache_key, None)

            logger.debug(f"[Rapid-{drive_name}] 尝试秒传: {display_name}")
            result = await self.get_secondary_client()
            if not result or not result[0]:
                secondary_client = None
                sec_cfg = {}
                rapid_cookie = None
            else:
                secondary_client, sec_cfg, rapid_cookie = result

            if not secondary_client:
                logger.warning("[Rapid] 小号客户端未就绪，已跳过秒传")
            else:
                sha1_info = await self._get_file_sha1_and_preupload_info(client, pickcode, user_agent, emby_index, direct_link_context=direct_link_context)
                if not sha1_info:
                    logger.warning("[Rapid] 无法获取文件 SHA1 信息")
                else:
                    logger.debug(f"[Rapid] SHA1获取成功: {sha1_info['sha1'][:16]}... (size: {sha1_info['size']})")
                    filename = await self._resolve_rapid_filename(filename_resolver, pickcode)
                    target_dir = sec_cfg.get('upload_dir', '/ChillPoster')
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
                            }
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

        logger.debug(f"[115-{drive_name}] 开始获取直链: {display_name}")
        final_url = await self._fetch_download_url(pickcode, user_agent, emby_index=emby_index, direct_link_context=direct_link_context)

        if final_url:
            self._url_cache[cache_key] = final_url
            logger.debug(f"[115-{drive_name}] 直链获取成功: {display_name}")
            return final_url

        logger.warning(f"[115-{drive_name}] 直链获取失败: {display_name}")
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

    def _is_file_busy(self, pickcode: str) -> bool:
        """检查缓存中是否有该 pickcode 的活跃记录"""
        prefix = f"{pickcode}_"
        for key in self._url_cache.keys():
            if key.startswith(prefix):
                return True
        return False

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
        target_dir = config.get('upload_dir', '/ChillPoster')
        logger.debug(f"[Sync-{drive_name}] 目标目录: {target_dir}")
        try:
            # 1. [修复] 使用 to_id 直接获取 src_file_id (需要文件头部 import to_id)
            src_file_id = to_id(src_pickcode)

            # 2. 获取目标目录 CID
            target_cid = None
            target_cid_info = client.fs_dir_getid_app(target_dir)
            logger.debug(f"[Sync-{drive_name}] 查询目标目录: {target_dir}")

            if target_cid_info and target_cid_info.get('id'):
                target_cid = target_cid_info.get('id')
                logger.debug(f"[Sync-{drive_name}] 目标目录已存在: CID={target_cid}")
            else:
                logger.debug(f"[Sync-{drive_name}] 目标目录不存在，准备创建: {target_dir}")
                
                # [修复] fs_mkdir 需要传入名称而不是路径
                # 假设目标是在根目录下，去掉开头的 "/"
                dir_name = target_dir.strip("/")
                
                # 如果配置的是多级路径 (e.g. /A/B)，简单处理取最后一级，或者默认建在根目录
                # 这里为了稳妥，直接在根目录创建该名称的文件夹
                if "/" in dir_name:
                    dir_name = dir_name.split("/")[-1]

                make_resp = client.fs_mkdir(dir_name)  # 默认 pid=0 (根目录)
                
                if make_resp and make_resp.get('state'):
                    # 尝试直接从创建结果中拿 ID
                    target_cid = make_resp.get('data', {}).get('id')
                    
                    # 如果没拿到，再查一次
                    if not target_cid:
                        target_cid_info = client.fs_dir_getid_app(target_dir)
                        if target_cid_info:
                            target_cid = target_cid_info.get('id')
                else:
                    logger.error(f"[Sync-{drive_name}] 创建目录失败: {dir_name}, 响应: {make_resp}")
                    return None

            if not target_cid: 
                logger.error(f"[Sync-{drive_name}] 无法获取目标目录 CID: {target_dir}")
                return None

            # 3. 执行复制
            logger.debug(f"[Sync-{drive_name}] 开始复制: src_id={src_file_id} -> target_cid={target_cid}")
            resp = client.fs_copy(src_file_id, target_cid)
            logger.debug(f"[Sync-{drive_name}] 复制响应: state={resp.get('state')}")
            if not resp.get('state'):
                logger.error(f"[Sync-{drive_name}] 复制失败: state={resp.get('state')}")
                return None

            # 4. 获取新文件信息 (参照 r302 逻辑)
            # 列出目标目录文件，按修改时间倒序排列 (o=user_ptime, asc=0)
            list_params = {
                "cid": target_cid,
                "o": "user_ptime",
                "asc": 0,
                "limit": 1
            }
            list_resp = client.fs_files(list_params)
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
                client.fs_delete(file_id)
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
            # 如果缓存中有直链但没有使用正确的 UA，需要重新获取
            if cached.get('direct_url'):
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
                'direct_url': direct_url
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
            # 1. 获取/创建目标目录
            target_cid_info = secondary_client.fs_dir_getid_app(target_dir)
            if not target_cid_info or not target_cid_info.get('id'):
                # 创建目录
                dir_name = target_dir.strip("/").split("/")[-1]
                make_resp = secondary_client.fs_mkdir(dir_name)
                if make_resp and make_resp.get('state'):
                    target_cid_info = secondary_client.fs_dir_getid_app(target_dir)

            if not target_cid_info or not target_cid_info.get('id'):
                logger.error(f"[Rapid] 无法获取目标目录: {target_dir}")
                return None

            target_cid = target_cid_info['id']

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
                        timeout=60.0,
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

            result = secondary_client.upload_file_init(
                filename=filename,
                filesize=sha1_info['size'],
                filesha1=sha1_info['sha1'],
                read_range_bytes_or_hash=read_range_callback,  # 传入范围读取回调
                pid=target_cid,
                async_=True
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
                    list_resp = secondary_client.fs_files(list_params)

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
                secondary_client.fs_delete(file_id)
                logger.debug(f"[Rapid] 小号副本已清理: file_id={file_id}")
            elif pickcode:
                # p115client 的 fs_delete 也支持 pickcode
                from p115client import P115Client
                file_id_to_delete = P115Client.to_id(pickcode)
                secondary_client.fs_delete(file_id_to_delete)
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

    async def _download_urls_via_client(self, pickcodes, user_agent: str = "", emby_index=None, cookie=None, direct_link_context: str = "default") -> dict[str, str]:
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
            try:
                request_factory = lambda: asyncio.to_thread(
                    client.download_url_app,
                    {"pickcode": batch_pickcodes},
                    user_agent=user_agent or "Mozilla/5.0",
                    app="chrome",
                    async_=False,
                )
                if direct_link_context == "gateway_playback":
                    result = await self._run_gateway_playback_direct_url_request("获取直链", request_factory)
                else:
                    result = await _run_115_serial_request("获取直链", request_factory)
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

    async def _fetch_download_url(self, pickcode, user_agent, cookie=None, emby_index=None, direct_link_context: str = "default"):
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
        )
        return urls.get(normalized_pickcode)

    async def _get_emby_media_source(self, emby_cfg, item_id, media_source_id=None):
        base_url = emby_cfg.get("url", "").rstrip("/")
        api_key = emby_cfg.get("key", "")
        if not base_url or not api_key:
            return None

        url = f"{base_url}/emby/Items/{item_id}/PlaybackInfo?api_key={api_key}"
        resp = await self._http_client.post(url, json={"Profile": "Unknown"}, timeout=5.0)
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
                    strm_resp = await self._http_client.get(path, timeout=5.0)
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
                        file_resp = await self._http_client.get(endpoint, timeout=5.0)
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
            logger.warning(f"[115] Pickcode 模式解析异常: {e}")
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
            upload_dir = drive_config.get('upload_dir', '/ChillPoster')
            recycle_code = drive_config.get('recycle_code', '')
        else:
            # 小号配置
            rapid_accounts = drive_config.get('rapid_accounts', [])
            if account_index >= len(rapid_accounts):
                return
            account = rapid_accounts[account_index]
            name = account.get('name', f'小号{account_index + 1}')
            cookie = account.get('cookie', '')
            # 小号使用独立配置，如果没有则使用主号配置作为默认值
            upload_dir = account.get('upload_dir', drive_config.get('upload_dir', '/ChillPoster'))
            recycle_code = account.get('recycle_code', drive_config.get('recycle_code', ''))

        if not cookie: return

        try:
            logger.info(f"[CleanUp] 开始清理账号: {name} (类型: {account_type})")
            client = P115Client(cookie)
            dir_info = client.fs_dir_getid_app(upload_dir)
            target_cid = dir_info.get('id') if dir_info else None
            if not target_cid:
                logger.warning(f"[CleanUp] 目录不存在: {upload_dir}")
                return

            deleted_count = 0
            max_iterations = 100
            for _ in range(max_iterations):
                resp = client.fs_files({'cid': target_cid, 'limit': 1000})
                if not resp.get('data'): break
                file_list = resp['data']
                if not file_list: break
                fids = [item['fid'] for item in file_list]
                client.fs_delete(fids)
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

    async def collect_115_folder_child_ids(self, client, folder_cid: str, folder_label: str) -> dict:
        normalized_cid = str(folder_cid or "").strip()
        if not normalized_cid.isdigit() or normalized_cid == "0":
            return {"status": "error", "cid": normalized_cid, "label": folder_label, "ids": [], "message": "禁止清空根目录或无效目录"}

        ids: list[int] = []
        try:
            from p115client.tool.iterdir import traverse_tree_with_path
            items = await asyncio.to_thread(
                lambda: list(traverse_tree_with_path(
                    client,
                    cid=int(normalized_cid),
                    with_ancestors=True,
                    app="android",
                    max_workers=0,
                ))
            )
            for item in items:
                item_id = self._extract_cleanup_item_id(item)
                if item_id and str(item_id) != normalized_cid:
                    ids.append(item_id)
        except Exception as e:
            logger.warning(f"[CleanUp] 全量遍历目录失败，回退直接子项列表: {folder_label} | {e}")
            resp = await asyncio.to_thread(client.fs_files, {"cid": int(normalized_cid), "limit": 10000})
            file_list = resp.get("data") if isinstance(resp, dict) else []
            for item in file_list:
                item_id = self._extract_cleanup_item_id(item)
                if item_id:
                    ids.append(item_id)

        unique_ids = list(dict.fromkeys(ids))
        logger.info(f"[CleanUp] 已收集清空目标: {folder_label} | 待删 {len(unique_ids)} 项")
        return {"status": "ok", "cid": normalized_cid, "label": folder_label, "ids": unique_ids, "message": ""}

    async def _delete_115_ids_once_with_retry(self, client, ids: list[int], log_label: str):
        if not ids:
            return True, None
        last_error = None
        for attempt in range(2):
            if attempt > 0:
                logger.warning(f"[CleanUp] 批量删除失败，1秒后重试: {log_label}")
                await asyncio.sleep(1)
            try:
                resp = await asyncio.to_thread(client.fs_delete, ids, async_=False)
                if isinstance(resp, dict) and resp.get("state") is False:
                    raise RuntimeError(resp)
                return True, None
            except Exception as e:
                last_error = e
        return False, last_error

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

        all_ids: list[int] = []
        folder_results = []
        for folder in folders:
            cid = str((folder or {}).get("cid", "") or "").strip()
            label = str((folder or {}).get("path") or (folder or {}).get("name") or cid).strip()
            if not cid.isdigit() or cid == "0" or label in {"", "/", "根目录"}:
                message = f"跳过无效目录: {label or cid}"
                folder_results.append({"cid": cid, "label": label, "status": "error", "deleted_count": 0, "message": message})
                logger.warning(f"[CleanUp] {message}")
                continue
            result = await self.collect_115_folder_child_ids(client, cid, label)
            folder_results.append({
                "cid": cid,
                "label": label,
                "status": result.get("status"),
                "deleted_count": len(result.get("ids") or []),
                "message": result.get("message", ""),
            })
            all_ids.extend(result.get("ids") or [])

        all_ids = list(dict.fromkeys(all_ids))
        if not all_ids:
            logger.info(f"[CleanUp] 定时清空完成: {task_name} | 没有待删除项")
            return {"status": "ok", "deleted_count": 0, "folders": folder_results, "message": "没有待删除项"}

        logger.info(f"[CleanUp] 准备批量删除: {task_name} | 总计 {len(all_ids)} 项")
        ok, error = await self._delete_115_ids_once_with_retry(client, all_ids, f"{task_name} size={len(all_ids)}")
        if not ok:
            message = f"批量删除失败: {error}"
            logger.error(f"[CleanUp] {message}")
            return {"status": "error", "deleted_count": 0, "folders": folder_results, "message": message}

        deleted_count = len(all_ids)
        logger.info(f"[CleanUp] 定时清空完成: {task_name} | 已删除 {deleted_count} 项")
        if task.get("clear_recycle_bin", True):
            await self._clear_recycle_bin(cookie, recycle_code)
        return {"status": "ok", "deleted_count": deleted_count, "folders": folder_results, "message": f"清理完成，共删除 {deleted_count} 项"}

    async def close(self):
        if self._http_client:
            await self._http_client.aclose()
            logger.info("[115Service] HTTP 客户端已关闭")

drive115_service = Drive115Service()