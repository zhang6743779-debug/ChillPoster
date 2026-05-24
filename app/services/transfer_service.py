# app/services/transfer_service.py
import re
import json
import os
from datetime import datetime
from urllib.parse import unquote
from p115client.util import share_extract_payload
from app.services.drive115_service import drive115_service
from app.routers.config_302 import get_config_302
from core.logger import logger
from app.services.media_organize_115_ops import run_115_write_request

RE_115_LINK = re.compile(
    r'https?://(?:115\.com/s/|115cdn\.com/s/|share\.115\.com/|anxia\.com/\S*?)'
    r'[a-zA-Z0-9]+(?:\?\s*password\s*=\s*[a-zA-Z0-9]+)?'
)
RE_ED2K_LINK = re.compile(r'ed2k://\|file\|.*?\|/', re.IGNORECASE)
RE_115_SHARE_CODE = re.compile(r'[a-zA-Z0-9]{6,20}-[a-zA-Z0-9]{4,20}')

SOURCE_NAMES = {
    "wechat": "企业微信",
    "telegram": "Telegram",
    "telegram_monitor": "Telegram 监听",
    "telegram_bot": "转存机器人",
    "manual": "手动",
}

HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config", "transfer_history.json"
)


class TransferService:
    def __init__(self):
        self._max_history = 200
        self._history = self._load_history()
        self._transfer_cid_cache: dict[str, int] = {}

    def _load_history(self) -> list:
        """从文件加载转存记录"""
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data[:self._max_history]
            except Exception as e:
                logger.error(f"[转存] 加载历史记录失败: {e}")
        return []

    def _save_history(self):
        """持久化转存记录到文件"""
        try:
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[转存] 保存历史记录失败: {e}")

    def extract_links(self, text: str) -> list[str]:
        """从消息文本中提取所有支持的资源链接"""
        if not text:
            return []

        links = []
        links.extend(re.sub(r"\s+", "", link) for link in RE_115_LINK.findall(text))
        links.extend(match.group(0).strip() for match in RE_ED2K_LINK.finditer(text))

        stripped = text.strip()
        if not links and RE_115_SHARE_CODE.fullmatch(stripped):
            links = [stripped]

        return self._dedupe_links(links)

    async def process_links(
        self,
        links: list[str],
        source: str = "manual",
        target_dir: str | None = None,
        source_meta: dict | None = None,
    ) -> list[dict]:
        """批量处理资源链接，ed2k 链接会合并为一次 115 离线任务请求。"""
        normalized = self._dedupe_links([str(link or "").strip() for link in links if str(link or "").strip()])
        if not normalized:
            return []

        results: list[dict | None] = [None] * len(normalized)
        ed2k_links = []
        ed2k_positions = []

        for idx, link in enumerate(normalized):
            if self._is_ed2k_link(link):
                ed2k_links.append(link)
                ed2k_positions.append(idx)
            else:
                results[idx] = await self._process_115_share_link(link, source=source, target_dir=target_dir, source_meta=source_meta)

        if ed2k_links:
            ed2k_results = await self._process_ed2k_links(ed2k_links, source=source, target_dir=target_dir, source_meta=source_meta)
            for idx, result in zip(ed2k_positions, ed2k_results):
                results[idx] = result

        return [result for result in results if result is not None]

    async def process_link(
        self,
        link: str,
        source: str = "manual",
        target_dir: str | None = None,
        source_meta: dict | None = None,
        share_file_id: str | int | None = None,
    ) -> dict:
        """
        处理单条资源链接。

        Returns:
            {success: bool, status: str, name: str, link: str, share_code: str, message: str}
        """
        link = str(link or "").strip()
        if self._is_ed2k_link(link):
            results = await self._process_ed2k_links([link], source=source, target_dir=target_dir, source_meta=source_meta)
            return results[0]
        return await self._process_115_share_link(
            link,
            source=source,
            target_dir=target_dir,
            source_meta=source_meta,
            share_file_id=share_file_id,
        )

    async def _process_115_share_link(
        self,
        link: str,
        source: str = "manual",
        target_dir: str | None = None,
        source_meta: dict | None = None,
        share_file_id: str | int | None = None,
    ) -> dict:
        try:
            payload = share_extract_payload(link)
        except ValueError as e:
            result = {
                "success": False,
                "status": "解析失败",
                "name": "",
                "link": link,
                "share_code": "",
                "link_type": "115",
                "message": f"链接解析失败: {e}",
            }
            self._add_history(result, source, link, source_meta=source_meta)
            return result

        share_code = payload.get("share_code", "")
        receive_code = payload.get("receive_code") or ""

        client, cid, client_error = await self._get_transfer_context(target_dir=target_dir)
        if not client:
            result = {
                "success": False,
                "status": "客户端错误",
                "name": "",
                "link": link,
                "share_code": share_code,
                "link_type": "115",
                "message": client_error,
            }
            self._add_history(result, source, link, source_meta=source_meta)
            return result

        try:
            receive_file_id = str(share_file_id or "0").strip() or "0"
            resp = await run_115_write_request(
                client,
                "接收115分享",
                lambda write_client: write_client.share_receive({
                    "share_code": share_code,
                    "receive_code": receive_code,
                    "file_id": receive_file_id,
                    "cid": cid,
                }),
                raise_on_state_false=False,
            )
            logger.info(f"[转存] share_receive 返回: {json.dumps(resp, ensure_ascii=False)}")
        except Exception as e:
            logger.error(f"[转存] share_receive 调用失败: {e}")
            result = {
                "success": False,
                "status": "转存失败",
                "name": "",
                "link": link,
                "share_code": share_code,
                "link_type": "115",
                "message": f"115 接口调用失败: {e}",
            }
            self._add_history(result, source, link, source_meta=source_meta)
            return result

        state = resp.get("state", False) if isinstance(resp, dict) else False
        error_msg = self._response_error(resp)
        data = resp.get("data", {}) or resp.get("result", {}) or {} if isinstance(resp, dict) else {}

        name = ""
        if isinstance(data, dict):
            name = (data.get("receive_title", "")
                    or data.get("file_name", "")
                    or data.get("name", "")
                    or "")
        if not name and isinstance(data, list) and data:
            first = data[0] if isinstance(data[0], dict) else {}
            name = first.get("file_name", "") or first.get("name", "") or ""
        if not name:
            name = share_code

        if state:
            result = {
                "success": True,
                "status": "转存成功",
                "name": name,
                "link": link,
                "share_code": share_code,
                "share_file_id": str(share_file_id or ""),
                "target_cid": str(cid),
                "link_type": "115",
                "message": f"转存成功 (115)\n名称: {name}\n链接: {link}",
            }
        else:
            result = {
                "success": False,
                "status": "转存失败",
                "name": "",
                "link": link,
                "share_code": share_code,
                "share_file_id": str(share_file_id or ""),
                "target_cid": str(cid),
                "link_type": "115",
                "message": f"转存失败 (115)\n链接: {link}\n原因: {error_msg or '未知错误'}",
            }

        self._add_history(result, source, link, source_meta=source_meta)
        return result

    async def _process_ed2k_links(
        self,
        links: list[str],
        source: str = "manual",
        target_dir: str | None = None,
        source_meta: dict | None = None,
    ) -> list[dict]:
        links = self._dedupe_links([str(link or "").strip() for link in links if str(link or "").strip()])
        if not links:
            return []

        client, cid, client_error = await self._get_transfer_context(target_dir=target_dir)
        if not client:
            return self._build_ed2k_results(links, False, "客户端错误", client_error, source, source_meta=source_meta)

        payload = {f"url[{idx}]": link for idx, link in enumerate(links)}
        payload["wp_path_id"] = cid

        try:
            resp = await run_115_write_request(
                client,
                f"添加{len(links)}个离线任务",
                lambda write_client: write_client.offline_add_urls(payload, async_=False),
                raise_on_state_false=False,
            )
            logger.info(f"[转存] 115 离线添加接口返回(offline_add_urls): {json.dumps(resp, ensure_ascii=False)}")
        except Exception as e:
            logger.error(f"[转存] 115 离线添加接口调用失败(offline_add_urls): {e}")
            return self._build_ed2k_results(links, False, "离线任务添加失败", f"115 离线接口调用失败: {e}", source, source_meta=source_meta)

        success = self._response_success(resp)
        if success:
            return self._build_ed2k_results(links, True, "离线任务已添加", "", source, source_meta=source_meta)

        error_msg = self._response_error(resp) or "未知错误"
        return self._build_ed2k_results(links, False, "离线任务添加失败", error_msg, source, source_meta=source_meta)

    async def _get_transfer_context(self, target_dir: str | None = None):
        cfg = await get_config_302()
        drives = cfg.get("drives", [])
        drive_index = 0
        if isinstance(drives, list) and len(drives) > 0:
            drive_index = drives[0].get("transfer_drive_index", 0)

        client, _drive_cfg = await drive115_service.get_client(drive_index)
        if not client:
            return None, 0, "115 客户端未配置"

        try:
            cid = await self._resolve_transfer_cid(client, cfg, drives, drive_index, target_dir=target_dir)
        except Exception as e:
            logger.warning(f"[转存] 解析转存目录失败: {e}")
            return None, 0, f"转存目录解析失败: {e}"
        return client, cid, ""

    def _normalize_transfer_dir(self, transfer_dir: str) -> str:
        transfer_dir = str(transfer_dir or "").strip()
        if not transfer_dir or transfer_dir.isdigit():
            return transfer_dir
        return "/" + "/".join(part for part in transfer_dir.strip("/").split("/") if part)

    def _configured_transfer_cid(self, cfg: dict, drives: list, transfer_dir: str, target_dir: str | None = None) -> int:
        if target_dir:
            return 0

        normalized_dir = self._normalize_transfer_dir(transfer_dir)
        candidates: list[tuple[str, str]] = []
        if isinstance(drives, list) and drives:
            drive_cfg = drives[0] if isinstance(drives[0], dict) else {}
            candidates.append((
                self._normalize_transfer_dir(str(drive_cfg.get("transfer_dir", "") or "")),
                str(drive_cfg.get("transfer_dir_cid", "") or ""),
            ))

        topology = cfg.get("standard_topology", {}) if isinstance(cfg, dict) else {}
        if isinstance(topology, dict):
            candidates.append((
                self._normalize_transfer_dir(str(topology.get("transfer_dir", "") or "")),
                str(topology.get("transfer_dir_cid", "") or ""),
            ))

        for candidate_dir, candidate_cid in candidates:
            if candidate_dir == normalized_dir and candidate_cid.isdigit():
                return int(candidate_cid)
        return 0

    async def _resolve_transfer_cid(self, client, cfg, drives, drive_index: int = 0, target_dir: str | None = None) -> int:
        cid = 0
        transfer_dir = str(target_dir or "").strip()
        if not transfer_dir and isinstance(drives, list) and len(drives) > 0:
            transfer_dir = str(drives[0].get("transfer_dir", "")).strip()

        if not transfer_dir:
            return cid
        if transfer_dir.isdigit():
            return int(transfer_dir)

        transfer_dir = self._normalize_transfer_dir(transfer_dir)
        cache_key = f"{int(drive_index)}:{transfer_dir}"
        cached_cid = self._transfer_cid_cache.get(cache_key)
        if cached_cid:
            return cached_cid

        configured_cid = self._configured_transfer_cid(cfg, drives, transfer_dir, target_dir=target_dir)
        if configured_cid:
            self._transfer_cid_cache[cache_key] = configured_cid
            logger.info(f"[转存] 使用配置缓存目录: {transfer_dir} (CID={configured_cid})")
            return configured_cid

        parts = [p for p in transfer_dir.strip("/").split("/") if p]
        if not parts:
            return cid

        try:
            for i, part in enumerate(parts):
                current_path = "/" + "/".join(parts[:i + 1])
                dir_info = await run_115_write_request(
                    client,
                    "查询转存目录",
                    lambda write_client, current_path=current_path: write_client.fs_dir_getid_app(current_path),
                    raise_on_state_false=False,
                )
                if dir_info and dir_info.get("id"):
                    cid = dir_info["id"]
                else:
                    mkdir_resp = await run_115_write_request(
                        client,
                        "创建转存目录",
                        lambda write_client, part=part: write_client.fs_mkdir_app(part, app="android", async_=False),
                        raise_on_state_false=False,
                    )
                    if mkdir_resp and mkdir_resp.get("state"):
                        dir_info = await run_115_write_request(
                            client,
                            "查询转存目录",
                            lambda write_client, current_path=current_path: write_client.fs_dir_getid_app(current_path),
                            raise_on_state_false=False,
                        )
                        cid = dir_info.get("id", 0) if dir_info else 0
                    else:
                        err = mkdir_resp.get("error", "未知错误") if mkdir_resp else "无响应"
                        logger.warning(f"[转存] 创建目录失败 {current_path}: {err}")
                        cid = 0
                        break
            if not cid:
                raise RuntimeError(f"{transfer_dir}: 未能获取目标目录 CID")
            self._transfer_cid_cache[cache_key] = int(cid)
            logger.info(f"[转存] 目录就绪: {transfer_dir} (CID={cid})")
        except Exception as e:
            raise RuntimeError(f"{transfer_dir}: {e}") from e
        return cid

    def _build_ed2k_results(
        self,
        links: list[str],
        success: bool,
        status: str,
        error_msg: str,
        source: str,
        source_meta: dict | None = None,
    ) -> list[dict]:
        results = []
        batch_suffix = f"\n批量任务: {len(links)} 个" if len(links) > 1 else ""
        for link in links:
            name = self._ed2k_name(link)
            if success:
                message = f"离线任务已添加 (ed2k)\n名称: {name}\n链接: {link}{batch_suffix}"
            else:
                message = f"离线任务添加失败 (ed2k)\n链接: {link}\n原因: {error_msg or '未知错误'}{batch_suffix}"
            result = {
                "success": success,
                "status": status,
                "name": name,
                "link": link,
                "share_code": "",
                "link_type": "ed2k",
                "message": message,
            }
            self._add_history(result, source, link, source_meta=source_meta)
            results.append(result)
        return results

    def _ed2k_name(self, link: str) -> str:
        parts = link.split("|")
        if len(parts) >= 3 and parts[1].lower() == "file":
            return unquote(parts[2]) or "ed2k"
        return "ed2k"

    def _response_success(self, resp) -> bool:
        if not isinstance(resp, dict):
            return False
        state = resp.get("state")
        if isinstance(state, bool):
            return state
        if isinstance(state, str):
            return state.lower() in {"true", "1", "success", "ok"}
        errno = resp.get("errno", resp.get("errNo"))
        return str(errno) == "0" if errno is not None else False

    def _response_error(self, resp) -> str:
        if not isinstance(resp, dict):
            return str(resp or "")
        return str(
            resp.get("error_msg", "")
            or resp.get("error", "")
            or resp.get("message", "")
            or resp.get("msg", "")
            or resp.get("errno", "")
            or resp.get("errNo", "")
            or ""
        )

    def _is_ed2k_link(self, link: str) -> bool:
        return str(link or "").lower().startswith("ed2k://")

    def is_successful_115_transfer(self, result: dict) -> bool:
        """Whether a transfer result means a 115 share was actually received."""
        if not isinstance(result, dict):
            return False
        return bool(result.get("success")) and str(result.get("link_type", "115") or "115").lower() == "115"

    def _dedupe_links(self, links: list[str]) -> list[str]:
        seen = set()
        result = []
        for link in links:
            if not link or link in seen:
                continue
            seen.add(link)
            result.append(link)
        return result

    def _normalize_source_meta(self, source: str, source_meta: dict | None = None) -> dict:
        meta = dict(source_meta or {})
        source_key = str(meta.get("source_key") or source or "manual").strip() or "manual"
        source_label = str(meta.get("source_label") or SOURCE_NAMES.get(source_key) or SOURCE_NAMES.get(source) or source_key).strip()
        source_kind = str(meta.get("source_kind") or source_key).strip()
        source_detail = str(meta.get("source_detail") or "").strip()
        source_id = str(meta.get("source_id") or "").strip()
        return {
            "source_key": source_key,
            "source_label": source_label,
            "source_kind": source_kind,
            "source_detail": source_detail,
            "source_id": source_id,
        }

    def _add_history(self, result: dict, source: str, link: str, source_meta: dict | None = None):
        """添加转存记录"""
        normalized_source = self._normalize_source_meta(source, source_meta)
        self._history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": normalized_source["source_label"],
            "source_key": normalized_source["source_key"],
            "source_kind": normalized_source["source_kind"],
            "source_detail": normalized_source["source_detail"],
            "source_id": normalized_source["source_id"],
            "link": link,
            "status": result.get("status", ""),
            "name": result.get("name", ""),
            "success": result.get("success", False),
            "share_code": result.get("share_code", ""),
            "link_type": result.get("link_type", "115"),
        })
        if len(self._history) > self._max_history:
            self._history = self._history[:self._max_history]
        self._save_history()

    def get_history(self) -> list:
        """获取转存历史记录"""
        return self._history

    def clear_history(self):
        """清空转存记录"""
        self._history = []
        self._save_history()


# 单例
transfer_service = TransferService()
