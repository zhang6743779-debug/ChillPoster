import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request

from app.services.drive115_service import drive115_service
from app.services.hdhive_openapi_client import HDHiveAPIError, HDHiveOpenClient
from app.services.hdhive_service import hdhive_service
from app.services.media_organize_115_ops import _get_115_fs
from app.services.media_organize_state import VIDEO_EXTS
from app.services.transfer_service import transfer_service
from core.logger import logger


CONFIG_PATH = Path("config/forward_hdhive.json")


def _default_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "account_id": "",
        "widget_token": uuid.uuid4().hex,
        "public_base_url": "",
        "max_unlock_points": 4,
    }


class ForwardHDHiveService:
    def __init__(self) -> None:
        self.config = _default_config()
        self._resource_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._load_config()

    def _load_config(self) -> None:
        if not CONFIG_PATH.exists():
            self._save_config()
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = _default_config()
            merged.update(data if isinstance(data, dict) else {})
            if not merged.get("widget_token"):
                merged["widget_token"] = uuid.uuid4().hex
            self.config = {key: merged[key] for key in _default_config().keys()}
            if data != self.config:
                self._save_config()
        except Exception as e:
            logger.warning(f"[ForwardHDHive] 配置读取失败，使用默认配置: {e}")

    def _save_config(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_account_options(self) -> list[dict[str, Any]]:
        accounts = []
        for account in hdhive_service.config.accounts:
            accounts.append({
                "id": account.id,
                "name": account.name or account.id,
                "enabled": account.enabled,
                "status": account.status,
                "has_api_key": bool(account.api_key),
            })
        return accounts

    def get_public_base_url(self, request: Request | None = None) -> str:
        configured = str(self.config.get("public_base_url") or "").strip().rstrip("/")
        if configured:
            return configured
        if request is None:
            return ""
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if host:
            return f"{proto}://{host}".rstrip("/")
        return str(request.base_url).rstrip("/")

    def get_widget_url(self, request: Request | None = None) -> str:
        base = self.get_public_base_url(request)
        token = self.config.get("widget_token") or ""
        return f"{base}/api/forward/widget.js?token={token}" if base else f"/api/forward/widget.js?token={token}"

    def get_widget_path(self) -> str:
        token = self.config.get("widget_token") or ""
        return f"/api/forward/widget.js?token={token}"

    def get_config(self, request: Request | None = None) -> dict[str, Any]:
        return {
            "enabled": bool(self.config.get("enabled", True)),
            "account_id": str(self.config.get("account_id") or ""),
            "public_base_url": str(self.config.get("public_base_url") or ""),
            "max_unlock_points": int(self.config.get("max_unlock_points") or 0),
            "widget_path": self.get_widget_path(),
            "accounts": self.get_account_options(),
            "widget_url": self.get_widget_url(request),
        }

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "enabled",
            "account_id",
            "public_base_url",
            "max_unlock_points",
        }
        for key in allowed:
            if key not in payload:
                continue
            value = payload[key]
            if key in {"max_unlock_points"}:
                try:
                    value = int(value)
                except Exception:
                    value = _default_config()[key]
            if key == "max_unlock_points":
                value = max(0, int(value))
            if key == "public_base_url":
                value = str(value or "").strip().rstrip("/")
            self.config[key] = value
        if not self.config.get("widget_token"):
            self.config["widget_token"] = uuid.uuid4().hex
        self._save_config()
        return self.get_config()

    def verify_token(self, token: str | None) -> None:
        expected = str(self.config.get("widget_token") or "").strip()
        if expected and str(token or "").strip() != expected:
            raise HTTPException(status_code=403, detail="Forward 模块 Token 无效")

    def _get_api_key(self) -> str:
        account_id = str(self.config.get("account_id") or "").strip()
        accounts = list(hdhive_service.config.accounts)
        selected = None
        if account_id:
            selected = next((a for a in accounts if a.id == account_id), None)
        if selected is None:
            selected = next((a for a in accounts if a.enabled and a.api_key), None)
        if selected is None or not selected.api_key:
            raise HTTPException(status_code=400, detail="请先在影巢配置中填写可用 API Key")
        return selected.api_key

    def _cache_key(self, media_type: str, tmdb_id: str | int) -> str:
        return f"{media_type}:{tmdb_id}"

    def _resource_points(self, item: dict[str, Any]) -> int:
        try:
            return int(item.get("unlock_points") or 0)
        except Exception:
            return 0

    def _resource_allowed(self, item: dict[str, Any]) -> bool:
        item_pan = str(item.get("pan_type") or "").strip().lower()
        if item_pan != "115":
            return False
        max_points = int(self.config.get("max_unlock_points") or 0)
        return self._resource_points(item) <= max_points

    def _sort_resource_key(self, item: dict[str, Any]) -> tuple[int, int, str]:
        resolution = " ".join(str(v) for v in (item.get("video_resolution") or []))
        score = 0
        if "8k" in resolution.lower():
            score += 40
        if "4k" in resolution.lower():
            score += 30
        if "1080" in resolution.lower():
            score += 10
        return (self._resource_points(item), -score, str(item.get("title") or ""))

    def fetch_resources(self, media_type: str, tmdb_id: str | int, *, use_cache: bool = True) -> list[dict[str, Any]]:
        normalized_type = "tv" if str(media_type or "").lower() in {"tv", "series"} else "movie"
        key = self._cache_key(normalized_type, tmdb_id)
        now = time.time()
        cached = self._resource_cache.get(key)
        if use_cache and cached and cached[0] > now:
            return cached[1]
        api_key = self._get_api_key()
        try:
            with HDHiveOpenClient(api_key) as client:
                resources = client.get_resources(normalized_type, str(tmdb_id))
        except HDHiveAPIError as e:
            raise HTTPException(status_code=e.http_status or 502, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"影巢资源查询失败: {e}") from e
        if not isinstance(resources, list):
            resources = []
        self._resource_cache[key] = (now + 300, resources)
        return resources

    def filter_resources(self, resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        filtered = [item for item in resources if isinstance(item, dict) and self._resource_allowed(item)]
        filtered.sort(key=self._sort_resource_key)
        return filtered

    def _describe_resource(self, item: dict[str, Any]) -> str:
        parts = []
        for key in ("video_resolution", "source", "subtitle_language", "subtitle_type"):
            values = item.get(key)
            if isinstance(values, list):
                parts.extend(str(v) for v in values if v)
            elif values:
                parts.append(str(values))
        if item.get("share_size"):
            parts.append(str(item.get("share_size")))
        parts.append(f"解锁 {self._resource_points(item)} 积分")
        return " | ".join(parts)

    def build_forward_resources(
        self,
        request: Request,
        params: dict[str, Any],
        resources: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        base = self.get_public_base_url(request)
        token = self.config.get("widget_token") or ""
        media_type = "tv" if str(params.get("type") or "").lower() in {"tv", "series"} else "movie"
        tmdb_id = str(params.get("tmdbId") or params.get("tmdb_id") or "").strip()
        season = str(params.get("season") or "").strip()
        episode = str(params.get("episode") or "").strip()
        result = []
        for item in self.filter_resources(resources):
            slug = str(item.get("slug") or "").strip()
            if not slug:
                continue
            query = {
                "token": token,
                "slug": slug,
                "type": media_type,
                "tmdb_id": tmdb_id,
            }
            if season:
                query["season"] = season
            if episode:
                query["episode"] = episode
            qs = urlencode({k: v for k, v in query.items() if v})
            result.append({
                "name": f"{item.get('title') or '影巢资源'} · {item.get('share_size') or '未知大小'}",
                "description": self._describe_resource(item),
                "url": f"{base}/api/forward/play?{qs}",
            })
        return result

    def _find_resource_by_slug(self, resources: list[dict[str, Any]], slug: str) -> dict[str, Any] | None:
        normalized = str(slug or "").replace("-", "").lower()
        for item in resources:
            item_slug = str(item.get("slug") or "").replace("-", "").lower()
            if item_slug == normalized:
                return item
        return None

    def _unlock_resource(self, slug: str) -> dict[str, Any]:
        api_key = self._get_api_key()
        try:
            with HDHiveOpenClient(api_key) as client:
                return client.unlock_resources(slug=slug)
        except HDHiveAPIError as e:
            raise HTTPException(status_code=e.http_status or 502, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"影巢解锁失败: {e}") from e

    def _extract_unlock_url(self, data: dict[str, Any], slug: str) -> str:
        if not isinstance(data, dict):
            return ""
        if data.get("full_url") or data.get("url"):
            return str(data.get("full_url") or data.get("url") or "").strip()
        items = data.get("items")
        normalized = str(slug or "").replace("-", "").lower()
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_slug = str(item.get("slug") or "").replace("-", "").lower()
                if item_slug == normalized and item.get("success") is not False:
                    return str(item.get("full_url") or item.get("url") or "").strip()
        return ""

    def _is_dir(self, item: dict[str, Any]) -> bool:
        if item.get("is_dir") is True:
            return True
        if str(item.get("fc") or "") == "0":
            return True
        return bool(item.get("is_directory"))

    def _item_id(self, item: dict[str, Any]) -> str:
        return str(item.get("id") or item.get("cid") or item.get("fid") or item.get("file_id") or "")

    def _item_pickcode(self, item: dict[str, Any]) -> str:
        return str(item.get("pickcode") or item.get("pick_code") or item.get("pc") or "")

    def _item_size(self, item: dict[str, Any]) -> int:
        for key in ("size", "s", "file_size"):
            try:
                return int(item.get(key) or 0)
            except Exception:
                pass
        return 0

    def _episode_match_score(self, name: str, season: int | None, episode: int | None) -> int:
        if not episode:
            return 0
        text = str(name or "").lower()
        ep2 = f"{episode:02d}"
        ep_raw = str(episode)
        if season:
            season2 = f"{season:02d}"
            patterns = [
                rf"s0?{season}e0?{episode}(?!\d)",
                rf"{season2}x{ep2}(?!\d)",
                rf"第\s*{season}\s*季.*第\s*{episode}\s*[集话]",
            ]
            if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
                return 100
        if re.search(rf"(?:^|[^a-z0-9])e0?{episode}(?!\d)", text, re.IGNORECASE):
            return 70
        if re.search(rf"第\s*{episode}\s*[集话]", text):
            return 65
        if re.search(rf"(?:^|[ ._\-\[]){ep2}(?:[ ._\-\]]|$)", text):
            return 35
        if re.search(rf"(?:^|[ ._\-\[]){ep_raw}(?:[ ._\-\]]|$)", text):
            return 25
        return 0

    async def _list_children(self, client, cid: str | int) -> list[dict[str, Any]]:
        def _run():
            fs = _get_115_fs(client)
            return [dict(item) for item in fs.iterdir(int(cid))]

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            logger.debug(f"[ForwardHDHive] 扫描 115 目录失败 cid={cid}: {e}")
            return []

    async def _collect_video_candidates(
        self,
        client,
        root_cid: str | int,
        *,
        preferred_name: str = "",
        max_depth: int = 4,
        max_nodes: int = 600,
    ) -> list[dict[str, Any]]:
        queue: list[tuple[str | int, int, str]] = [(root_cid, 0, "")]
        videos: list[dict[str, Any]] = []
        visited = 0
        preferred = str(preferred_name or "").strip().lower()
        while queue and visited < max_nodes:
            cid, depth, path = queue.pop(0)
            visited += 1
            children = await self._list_children(client, cid)
            for child in children:
                name = str(child.get("name") or child.get("n") or "")
                child_path = f"{path}/{name}" if path else name
                if self._is_dir(child):
                    child_id = self._item_id(child)
                    if child_id and depth < max_depth:
                        if preferred and depth == 0 and preferred not in name.lower() and len(queue) > 20:
                            continue
                        queue.append((child_id, depth + 1, child_path))
                    continue
                ext = os.path.splitext(name)[1].lower()
                pickcode = self._item_pickcode(child)
                if ext in VIDEO_EXTS and pickcode:
                    videos.append({
                        "name": name,
                        "path": child_path,
                        "pickcode": pickcode,
                        "size": self._item_size(child),
                        "depth": depth,
                    })
        return videos

    async def _locate_video_pickcode(
        self,
        client,
        target_cid: str | int,
        *,
        transfer_name: str = "",
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any] | None:
        root_children = await self._list_children(client, target_cid)
        preferred = str(transfer_name or "").strip().lower()
        roots: list[str | int] = []
        for child in root_children:
            name = str(child.get("name") or child.get("n") or "")
            if preferred and preferred not in name.lower():
                continue
            if self._is_dir(child):
                child_id = self._item_id(child)
                if child_id:
                    roots.append(child_id)
            else:
                ext = os.path.splitext(name)[1].lower()
                pickcode = self._item_pickcode(child)
                if ext in VIDEO_EXTS and pickcode:
                    roots.append(target_cid)
                    break
        if not roots:
            roots = [target_cid]

        candidates: list[dict[str, Any]] = []
        for cid in roots[:8]:
            candidates.extend(await self._collect_video_candidates(client, cid, preferred_name=transfer_name))
        if not candidates:
            return None

        def _rank(item: dict[str, Any]) -> tuple[int, int, int]:
            ep_score = self._episode_match_score(item.get("name", ""), season, episode)
            return (ep_score, int(item.get("size") or 0), -int(item.get("depth") or 0))

        candidates.sort(key=_rank, reverse=True)
        if episode and self._episode_match_score(candidates[0].get("name", ""), season, episode) <= 0:
            logger.warning(f"[ForwardHDHive] 未精确匹配到 S{season}E{episode}，将返回最大视频: {candidates[0].get('name')}")
        return candidates[0]

    async def play_resource(
        self,
        request: Request,
        *,
        slug: str,
        media_type: str,
        tmdb_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> str:
        if not self.config.get("enabled", True):
            raise HTTPException(status_code=403, detail="Forward 影巢模块未启用")
        resources = self.fetch_resources(media_type, tmdb_id) if tmdb_id else []
        resource = self._find_resource_by_slug(resources, slug) if resources else None
        if resource is not None:
            if not self._resource_allowed(resource):
                raise HTTPException(
                    status_code=403,
                    detail=f"资源需要 {self._resource_points(resource)} 积分，超过当前上限 {self.config.get('max_unlock_points')}",
                )
            if str(resource.get("pan_type") or "").lower() != "115":
                raise HTTPException(status_code=400, detail="当前仅支持 115 资源播放")

        unlock_data = self._unlock_resource(slug)
        share_url = self._extract_unlock_url(unlock_data, slug)
        if not share_url:
            raise HTTPException(status_code=502, detail="影巢解锁成功但未返回资源链接")

        client, target_cid, client_error = await transfer_service._get_transfer_context()
        if not client:
            raise HTTPException(status_code=400, detail=client_error or "115 转存客户端未就绪")

        transfer_result = await transfer_service.process_link(
            share_url,
            source="forward_hdhive",
            source_meta={
                "source_key": "forward_hdhive",
                "source_label": "Forward 影巢",
                "source_kind": "forward",
                "source_detail": f"{media_type}:{tmdb_id}",
                "source_id": slug,
            },
        )
        if not transfer_result.get("success"):
            raise HTTPException(status_code=502, detail=transfer_result.get("message") or "115 转存失败")

        picked = await self._locate_video_pickcode(
            client,
            target_cid,
            transfer_name=transfer_result.get("name", ""),
            season=season,
            episode=episode,
        )
        if not picked or not picked.get("pickcode"):
            raise HTTPException(status_code=404, detail="转存成功，但未在转存目录定位到可播放视频")

        user_agent = request.headers.get("user-agent", "")
        direct_url = await drive115_service.get_direct_url_by_pickcode(
            picked["pickcode"],
            user_agent=user_agent,
            emby_index=0,
            filename=picked.get("name") or None,
            direct_link_context="forward_hdhive",
        )
        if not direct_url:
            raise HTTPException(status_code=502, detail="115 直链获取失败")
        logger.info(f"[ForwardHDHive] 播放直链已生成: {picked.get('name')} slug={slug}")
        return direct_url


forward_hdhive_service = ForwardHDHiveService()
