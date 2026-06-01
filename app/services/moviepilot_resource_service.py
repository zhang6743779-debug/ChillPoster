import asyncio
import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException

from core.logger import logger


CONFIG_PATH = Path("config/moviepilot.json")
SETTINGS_PATH = Path("config/settings.json")


class MoviePilotResourceService:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires = 0.0
        self._login_lock = asyncio.Lock()
        self._torrent_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _load_config(self) -> dict[str, Any]:
        if not CONFIG_PATH.exists():
            return {}
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[MoviePilot资源] 配置读取失败: {e}")
            return {}

    def _load_settings(self) -> dict[str, Any]:
        if not SETTINGS_PATH.exists():
            return {}
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[MoviePilot资源] 全局配置读取失败: {e}")
            return {}

    def is_configured(self) -> bool:
        cfg = self._load_config()
        return bool(
            str(cfg.get("mp_url") or "").strip()
            and str(cfg.get("mp_username") or "").strip()
            and str(cfg.get("mp_password") or "").strip()
        )

    def _base_url(self) -> str:
        return str(self._load_config().get("mp_url") or "").strip().rstrip("/")

    async def _login(self) -> str:
        async with self._login_lock:
            if self._token and time.time() < self._token_expires:
                return self._token

            cfg = self._load_config()
            base_url = str(cfg.get("mp_url") or "").strip().rstrip("/")
            username = str(cfg.get("mp_username") or "").strip()
            password = str(cfg.get("mp_password") or "")
            if not base_url or not username or not password:
                raise HTTPException(status_code=400, detail="MoviePilot 未配置地址、用户名或密码")

            try:
                async with httpx.AsyncClient(verify=False, timeout=20) as client:
                    resp = await client.post(
                        f"{base_url}/api/v1/login/access-token",
                        data={"username": username, "password": password},
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
            except httpx.TimeoutException as e:
                raise HTTPException(status_code=504, detail="MoviePilot 登录超时") from e
            except httpx.HTTPError as e:
                raise HTTPException(status_code=502, detail=f"MoviePilot 登录失败: {e}") from e

            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=f"MoviePilot 登录失败: HTTP {resp.status_code}")
            data = resp.json() if resp.text else {}
            token = data.get("access_token") or data.get("token")
            if not token:
                raise HTTPException(status_code=502, detail="MoviePilot 登录成功但未返回 token")
            self._token = str(token)
            self._token_expires = time.time() + 23 * 60 * 60
            return self._token

    async def _prepare_resource_cookie(self, client: httpx.AsyncClient, token: str) -> None:
        resp = await client.get(
            f"{self._base_url()}/api/v1/user/current",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401:
            self._token = None
            token = await self._login()
            resp = await client.get(
                f"{self._base_url()}/api/v1/user/current",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=f"MoviePilot 资源令牌获取失败: HTTP {resp.status_code}")
        if not client.cookies:
            logger.debug("[MoviePilot资源] user/current 未显式返回资源 Cookie，将继续尝试流式接口")

    def _mp_type(self, media_type: str) -> str:
        return "电影" if str(media_type or "").lower() == "movie" else "电视剧"

    def _search_params(self, *, media_type: str, season: int | None = None, sites: str | None = None) -> dict[str, str]:
        params = {"mtype": self._mp_type(media_type), "area": "title"}
        if str(media_type or "").lower() != "movie" and season is not None:
            params["season"] = str(season)
        if sites:
            params["sites"] = str(sites)
        return params

    def _title_keyword(self, title: str | None = None, year: str | None = None) -> str:
        keyword = str(title or "").strip()
        if not keyword:
            return ""
        year_text = str(year or "").strip()[:4]
        if year_text and year_text.isdigit() and year_text not in keyword:
            return f"{keyword} {year_text}"
        return keyword

    async def search_resources(
        self,
        *,
        media_type: str,
        tmdb_id: str,
        title: str | None = None,
        year: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        sites: str | None = None,
    ) -> list[dict[str, Any]]:
        title_results = await self.search_resources_by_title(
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
            season=season,
            episode=episode,
            sites=sites,
        )
        if title_results:
            return title_results

        base_url = self._base_url()
        token = await self._login()
        params = self._search_params(media_type=media_type, season=season, sites=sites)
        timeout = httpx.Timeout(300.0, connect=20.0)
        try:
            async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
                resp = await client.get(
                    f"{base_url}/api/v1/search/media/tmdb:{tmdb_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                if resp.status_code == 401:
                    self._token = None
                    token = await self._login()
                    resp = await client.get(
                        f"{base_url}/api/v1/search/media/tmdb:{tmdb_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        params=params,
                    )
        except httpx.TimeoutException as e:
            raise HTTPException(status_code=504, detail="MoviePilot 搜索超时") from e
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"MoviePilot 搜索失败: {e}") from e

        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=f"MoviePilot 搜索失败: HTTP {resp.status_code}")
        payload = resp.json() if resp.text else {}
        if isinstance(payload, dict) and payload.get("success") is False:
            return []
        contexts = payload.get("data") if isinstance(payload, dict) else []
        return self.build_forward_resources(contexts if isinstance(contexts, list) else [], media_type=media_type, tmdb_id=tmdb_id, season=season, episode=episode)

    async def search_resources_by_title(
        self,
        *,
        media_type: str,
        tmdb_id: str,
        title: str | None = None,
        year: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        sites: str | None = None,
    ) -> list[dict[str, Any]]:
        keyword = self._title_keyword(title, year)
        if not keyword:
            return []
        base_url = self._base_url()
        token = await self._login()
        params: dict[str, Any] = {"keyword": keyword, "page": 0}
        if sites:
            params["sites"] = str(sites)
        timeout = httpx.Timeout(300.0, connect=20.0)
        try:
            async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
                resp = await client.get(
                    f"{base_url}/api/v1/search/title",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                if resp.status_code == 401:
                    self._token = None
                    token = await self._login()
                    resp = await client.get(
                        f"{base_url}/api/v1/search/title",
                        headers={"Authorization": f"Bearer {token}"},
                        params=params,
                    )
        except httpx.TimeoutException as e:
            raise HTTPException(status_code=504, detail="MoviePilot 标题搜索超时") from e
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"MoviePilot 标题搜索失败: {e}") from e

        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=f"MoviePilot 标题搜索失败: HTTP {resp.status_code}")
        payload = resp.json() if resp.text else {}
        if isinstance(payload, dict) and payload.get("success") is False:
            return []
        contexts = payload.get("data") if isinstance(payload, dict) else []
        return self.build_forward_resources(contexts if isinstance(contexts, list) else [], media_type=media_type, tmdb_id=tmdb_id, season=season, episode=episode)

    async def stream_resources(
        self,
        *,
        media_type: str,
        tmdb_id: str,
        title: str | None = None,
        year: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        sites: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        base_url = self._base_url()
        token = await self._login()
        params = self._search_params(media_type=media_type, season=season, sites=sites)
        timeout = httpx.Timeout(None, connect=20.0, write=20.0, pool=20.0)
        yield {
            "type": "progress",
            "stage": "connecting",
            "sourceKey": "moviepilot",
            "sourceName": "MoviePilot",
            "text": "正在连接 MoviePilot ...",
            "items": [],
        }
        keyword = self._title_keyword(title, year)
        try:
            async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
                await self._prepare_resource_cookie(client, token)
                if keyword:
                    async for event in self._stream_title_resources(
                        client=client,
                        keyword=keyword,
                        media_type=media_type,
                        tmdb_id=tmdb_id,
                        season=season,
                        episode=episode,
                        sites=sites,
                    ):
                        yield event
                    return
                async with client.stream(
                    "GET",
                    f"{base_url}/api/v1/search/media/tmdb:{tmdb_id}/stream",
                    params=params,
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", errors="replace")[:240]
                        raise HTTPException(status_code=resp.status_code, detail=f"MoviePilot 流式搜索失败: HTTP {resp.status_code} {text}")
                    async for event in self._iter_sse_json(resp):
                        event_type = str(event.get("type") or "progress")
                        items = event.get("items") if isinstance(event.get("items"), list) else []
                        converted = self.build_forward_resources(items, media_type=media_type, tmdb_id=tmdb_id, season=season, episode=episode)
                        next_event = {
                            **event,
                            "type": event_type,
                            "sourceKey": "moviepilot",
                            "sourceName": "MoviePilot",
                            "items": converted,
                            "total_items": len(converted) if event_type in {"replace", "done"} else event.get("total_items", len(converted)),
                        }
                        yield next_event
        except HTTPException:
            raise
        except httpx.TimeoutException as e:
            raise HTTPException(status_code=504, detail="MoviePilot 流式搜索超时") from e
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"MoviePilot 流式搜索失败: {e}") from e

    async def _stream_title_resources(
        self,
        *,
        client: httpx.AsyncClient,
        keyword: str,
        media_type: str,
        tmdb_id: str,
        season: int | None = None,
        episode: int | None = None,
        sites: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        params: dict[str, Any] = {"keyword": keyword, "page": 0}
        if sites:
            params["sites"] = str(sites)
        yield {
            "type": "progress",
            "stage": "searching",
            "sourceKey": "moviepilot",
            "sourceName": "MoviePilot",
            "text": f"正在搜索 {keyword} ...",
            "items": [],
        }
        async with client.stream(
            "GET",
            f"{self._base_url()}/api/v1/search/title/stream",
            params=params,
            headers={"Accept": "text/event-stream"},
        ) as resp:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode("utf-8", errors="replace")[:240]
                raise HTTPException(status_code=resp.status_code, detail=f"MoviePilot 标题流式搜索失败: HTTP {resp.status_code} {text}")
            async for event in self._iter_sse_json(resp):
                event_type = str(event.get("type") or "progress")
                items = event.get("items") if isinstance(event.get("items"), list) else []
                converted = self.build_forward_resources(items, media_type=media_type, tmdb_id=tmdb_id, season=season, episode=episode)
                yield {
                    **event,
                    "type": event_type,
                    "sourceKey": "moviepilot",
                    "sourceName": "MoviePilot",
                    "items": converted,
                    "total_items": len(converted) if event_type in {"replace", "done"} else event.get("total_items", len(converted)),
                }

    async def _iter_sse_json(self, resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        buffer = ""
        async for chunk in resp.aiter_text():
            if not chunk:
                continue
            buffer += chunk.replace("\r\n", "\n")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                data = self._extract_sse_data(block)
                if not data:
                    continue
                try:
                    parsed = json.loads(data)
                    if isinstance(parsed, dict):
                        yield parsed
                except json.JSONDecodeError:
                    logger.debug(f"[MoviePilot资源] 忽略无法解析的 SSE 数据: {data[:120]}")
        data = self._extract_sse_data(buffer)
        if data:
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    yield parsed
            except json.JSONDecodeError:
                logger.debug(f"[MoviePilot资源] 忽略无法解析的 SSE 尾包: {data[:120]}")

    def _extract_sse_data(self, block: str) -> str:
        lines = []
        for raw_line in str(block or "").split("\n"):
            line = raw_line.strip("\r")
            if line.startswith("data:"):
                lines.append(line[5:].lstrip())
        return "\n".join(lines).strip()

    def build_forward_resources(
        self,
        contexts: list[dict[str, Any]],
        *,
        media_type: str,
        tmdb_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for context in contexts:
            if not isinstance(context, dict):
                continue
            torrent = context.get("torrent_info") if isinstance(context.get("torrent_info"), dict) else {}
            if not torrent:
                continue
            title = str(torrent.get("title") or "").strip()
            description = str(torrent.get("description") or "").strip()
            if episode and self._episode_match_score(f"{title} {description}", season, episode) <= 0:
                continue
            resource_id = self._resource_id(torrent)
            if resource_id in seen:
                continue
            seen.add(resource_id)
            self._torrent_cache[resource_id] = (time.time() + 21600, dict(torrent))
            size_bytes = self._safe_int(torrent.get("size"))
            size_label = self._format_size(torrent.get("size"))
            site_name = str(torrent.get("site_name") or torrent.get("site") or "MoviePilot").strip()
            tags = self._tags(torrent, size_label=size_label, site_name=site_name)
            promotion = self._promotion_info(torrent)
            desc_parts = []
            if site_name:
                desc_parts.append(site_name)
            if description:
                desc_parts.append(description)
            if size_label:
                desc_parts.append(size_label)
            pubdate = str(torrent.get("pubdate") or "").strip()
            if pubdate:
                desc_parts.append(pubdate)
            seeders = torrent.get("seeders")
            peers = torrent.get("peers")
            if seeders is not None or peers is not None:
                desc_parts.append(f"做种 {seeders or 0} / 下载 {peers or 0}")
            display_title = title or description or "MoviePilot 资源"
            result.append({
                "id": f"moviepilot:{resource_id}",
                "type": "moviepilot",
                "title": display_title,
                "name": display_title,
                "description": " | ".join(part for part in desc_parts if part),
                "genreTitle": "|".join(tags),
                "tags": tags,
                "sourceKey": "moviepilot",
                "sourceName": "MoviePilot",
                "resourceId": resource_id,
                "mediaType": "movie" if str(media_type or "").lower() == "movie" else "tv",
                "tmdbId": str(tmdb_id or ""),
                "season": season,
                "episode": episode,
                "siteName": site_name,
                "size": size_bytes,
                "sizeLabel": size_label,
                "seeders": seeders,
                "peers": peers,
                "pubdate": pubdate,
                "promotionLabel": promotion.get("label", ""),
                "promotionKey": promotion.get("key", "normal"),
                "promotionClass": promotion.get("class", ""),
                "promotionText": promotion.get("text", ""),
                "downloadFactor": promotion.get("download_factor"),
                "uploadFactor": promotion.get("upload_factor"),
                "freeDate": promotion.get("free_date", ""),
                "previewDisabled": False,
                "transferDisabled": True,
                "actionLabel": "仅展示",
            })
        result.sort(
            key=lambda item: (
                self._safe_int(item.get("seeders")),
                self._safe_int(item.get("peers")),
                str(item.get("pubdate") or ""),
            ),
            reverse=True,
        )
        return result

    async def preview_torrent_resource(
        self,
        *,
        resource_id: str,
        media_type: str,
        tmdb_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        torrent = await self._get_cached_torrent(
            resource_id=resource_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            season=season,
            episode=episode,
        )
        enclosure = str(torrent.get("enclosure") or "").strip()
        if not enclosure:
            raise HTTPException(status_code=502, detail="MoviePilot 资源缺少种子下载链接")

        content = await self._download_torrent_file(enclosure, torrent)
        items = self._parse_torrent_preview_items(content, season=season, episode=episode, media_type=media_type)
        total_size = sum(int(item.get("size") or 0) for item in items if item.get("type") == "file")
        matched_count = sum(1 for item in items if item.get("episodeMatched"))
        title = str(torrent.get("title") or torrent.get("description") or "MoviePilot 资源")
        logger.info(f"[MoviePilot资源] 种子预览: id={resource_id} items={len(items)} tmdb={tmdb_id}")
        return {
            "source": "moviepilot",
            "sourceName": "MoviePilot",
            "sourceId": resource_id,
            "title": title,
            "tmdbId": tmdb_id,
            "mediaType": "movie" if str(media_type or "").lower() == "movie" else "tv",
            "season": season,
            "episode": episode,
            "count": len(items),
            "matchedCount": matched_count,
            "totalSize": total_size,
            "totalSizeLabel": self._format_size(total_size),
            "items": items,
        }

    async def add_torrent_to_downloader(
        self,
        *,
        resource_id: str,
        media_type: str,
        tmdb_id: str,
        title: str | None = None,
        year: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        fill_mode: str = "full",
        existing_episodes_by_season: dict[str, list[int]] | None = None,
    ) -> dict[str, Any]:
        normalized_media_type = "movie" if str(media_type or "").lower() == "movie" else "tv"
        torrent = await self._get_cached_torrent(
            resource_id=resource_id,
            media_type=normalized_media_type,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
            season=season,
            episode=episode,
        )
        downloader_error = ""
        try:
            downloaders = await self._get_mp_downloaders()
        except HTTPException as e:
            logger.warning(f"[MoviePilot资源] 下载器配置读取失败，将由 MoviePilot 自动选择下载器: {e.detail}")
            downloaders = []
            downloader_error = str(e.detail)
        downloader = self._select_mp_downloader(torrent, downloaders)
        downloader_name = str(downloader.get("name") or "").strip() if downloader else ""
        torrent_in = self._build_torrent_payload(torrent)
        media_in = self._build_media_payload(
            media_type=normalized_media_type,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
            season=season,
        )

        try:
            mp_payload = await self._post_mp_download({
                "media_in": media_in,
                "torrent_in": torrent_in,
                "downloader": downloader_name or None,
            })
        except HTTPException as primary_error:
            logger.warning(f"[MoviePilot资源] 含媒体信息添加下载失败，尝试自动识别: {primary_error.detail}")
            fallback_body: dict[str, Any] = {
                "torrent_in": torrent_in,
                "downloader": downloader_name or None,
            }
            tmdb_int = self._safe_int(tmdb_id)
            if tmdb_int:
                fallback_body["tmdbid"] = tmdb_int
            mp_payload = await self._post_mp_download(fallback_body, path="/api/v1/download/add")

        data = mp_payload.get("data") if isinstance(mp_payload, dict) else {}
        download_id = str((data or {}).get("download_id") or "").strip()
        if not download_id:
            raise HTTPException(status_code=502, detail="MoviePilot 已响应但未返回下载任务 ID")

        normalized_fill_mode = "missing" if str(fill_mode or "").lower() == "missing" else "full"
        selection: dict[str, Any] = {"applied": False, "mode": normalized_fill_mode}
        if normalized_fill_mode == "missing" and normalized_media_type == "tv":
            try:
                if downloader_error:
                    raise HTTPException(status_code=502, detail=f"无法读取下载器配置：{downloader_error}")
                selection = await self._apply_existing_episode_unwanted(
                    download_id=download_id,
                    downloader=downloader,
                    existing_episodes_by_season=existing_episodes_by_season or {},
                    default_season=season,
                )
            except HTTPException as e:
                selection = {
                    "applied": False,
                    "mode": normalized_fill_mode,
                    "error": str(e.detail),
                }
            except Exception as e:
                logger.warning(f"[MoviePilot资源] 缺集文件选择失败: {e}")
                selection = {
                    "applied": False,
                    "mode": normalized_fill_mode,
                    "error": f"缺集文件选择失败: {e}",
                }

        message = "已添加到 MoviePilot 下载器"
        if normalized_fill_mode == "missing" and normalized_media_type == "tv":
            if selection.get("error"):
                message = f"已添加到 MoviePilot，但缺集文件选择失败：{selection.get('error')}"
            elif selection.get("applied"):
                message = f"已添加到 MoviePilot，已取消 {selection.get('unwantedCount') or 0} 个已存在文件"
            else:
                message = f"已添加到 MoviePilot，{selection.get('message') or '没有需要取消的已存在文件'}"

        return {
            "success": True,
            "message": message,
            "source": "moviepilot",
            "downloadId": download_id,
            "downloader": downloader_name,
            "downloaderType": str(downloader.get("type") or "") if downloader else "",
            "fillMode": normalized_fill_mode,
            "selection": selection,
        }

    async def _get_cached_torrent(
        self,
        *,
        resource_id: str,
        media_type: str,
        tmdb_id: str,
        title: str | None = None,
        year: str | None = None,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        cached = self._torrent_cache.get(resource_id)
        if cached and cached[0] > now:
            return cached[1]
        if tmdb_id:
            await self.search_resources(media_type=media_type, tmdb_id=tmdb_id, title=title, year=year, season=season, episode=episode)
            cached = self._torrent_cache.get(resource_id)
            if cached and cached[0] > now:
                return cached[1]
        raise HTTPException(status_code=404, detail="MoviePilot 资源已过期，请返回详情页重新查询")

    async def _mp_api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> dict[str, Any]:
        base_url = self._base_url()
        if not base_url:
            raise HTTPException(status_code=400, detail="MoviePilot 未配置地址")
        url = f"{base_url}{path if path.startswith('/') else '/' + path}"
        for attempt in range(2):
            token = await self._login()
            try:
                async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
                    resp = await client.request(
                        method.upper(),
                        url,
                        params=params,
                        json=json_body,
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.TimeoutException as e:
                raise HTTPException(status_code=504, detail="MoviePilot 请求超时") from e
            except httpx.HTTPError as e:
                raise HTTPException(status_code=502, detail=f"MoviePilot 请求失败: {e}") from e
            if resp.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if resp.status_code >= 400:
                detail = resp.text[:240] if resp.text else f"HTTP {resp.status_code}"
                raise HTTPException(status_code=resp.status_code, detail=f"MoviePilot 请求失败: {detail}")
            if not resp.text:
                return {}
            try:
                data = resp.json()
                return data if isinstance(data, dict) else {"data": data}
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"MoviePilot 返回非 JSON: {e}") from e
        raise HTTPException(status_code=401, detail="MoviePilot 登录状态已失效")

    async def _post_mp_download(self, body: dict[str, Any], *, path: str = "/api/v1/download/") -> dict[str, Any]:
        payload = await self._mp_api_request("POST", path, json_body=body, timeout=120)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise HTTPException(status_code=502, detail=str(payload.get("message") or "MoviePilot 添加下载失败"))
        return payload

    async def _get_mp_downloaders(self) -> list[dict[str, Any]]:
        payload = await self._mp_api_request("GET", "/api/v1/system/setting/Downloaders", timeout=30)
        value = (payload.get("data") or {}).get("value") if isinstance(payload, dict) else []
        return value if isinstance(value, list) else []

    def _select_mp_downloader(self, torrent: dict[str, Any], downloaders: list[dict[str, Any]]) -> dict[str, Any]:
        enabled = [
            item for item in downloaders
            if isinstance(item, dict) and item.get("enabled") and str(item.get("type") or "").lower() in {"qbittorrent", "transmission"}
        ]
        if not enabled:
            return {}
        preferred = str(torrent.get("site_downloader") or "").strip()
        if preferred:
            matched = next((item for item in enabled if str(item.get("name") or "") == preferred), None)
            if matched:
                return matched
        return next((item for item in enabled if item.get("default")), None) or enabled[0]

    def _build_media_payload(
        self,
        *,
        media_type: str,
        tmdb_id: str,
        title: str | None = None,
        year: str | None = None,
        season: int | None = None,
    ) -> dict[str, Any]:
        media_title = str(title or "").strip()
        media_year = str(year or "").strip()[:4]
        payload: dict[str, Any] = {
            "source": "themoviedb",
            "type": self._mp_type(media_type),
            "title": media_title,
            "year": media_year,
            "tmdb_id": self._safe_int(tmdb_id) or None,
        }
        if media_title and media_year:
            payload["title_year"] = f"{media_title} ({media_year})"
        if str(media_type or "").lower() != "movie" and season is not None:
            payload["season"] = season
        return payload

    def _build_torrent_payload(self, torrent: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "site",
            "site_name",
            "site_cookie",
            "site_ua",
            "site_proxy",
            "site_order",
            "site_downloader",
            "title",
            "description",
            "imdbid",
            "enclosure",
            "page_url",
            "size",
            "seeders",
            "peers",
            "grabs",
            "pubdate",
            "date_elapsed",
            "freedate",
            "uploadvolumefactor",
            "downloadvolumefactor",
            "hit_and_run",
            "labels",
            "pri_order",
            "volume_factor",
            "freedate_diff",
        }
        payload = {key: torrent.get(key) for key in fields if key in torrent}
        if not self._safe_int(payload.get("site")):
            payload.pop("site", None)
        for key in ("site_order", "seeders", "peers", "grabs", "pri_order"):
            if key in payload:
                payload[key] = self._safe_int(payload.get(key))
        for key in ("size", "uploadvolumefactor", "downloadvolumefactor"):
            if key in payload and payload.get(key) not in (None, ""):
                try:
                    payload[key] = float(payload.get(key) or 0)
                except Exception:
                    payload.pop(key, None)
        if not isinstance(payload.get("labels"), list):
            payload["labels"] = []
        return payload

    async def _apply_existing_episode_unwanted(
        self,
        *,
        download_id: str,
        downloader: dict[str, Any],
        existing_episodes_by_season: dict[str, list[int]],
        default_season: int | None = None,
    ) -> dict[str, Any]:
        existing_map = self._normalize_existing_episode_map(existing_episodes_by_season)
        if not existing_map:
            return {"applied": False, "mode": "missing", "message": "没有已存在集数"}
        if not downloader:
            raise HTTPException(status_code=502, detail="MoviePilot 未返回可用下载器配置")
        downloader_type = str(downloader.get("type") or "").lower()
        if downloader_type == "qbittorrent":
            return await self._apply_qb_unwanted(download_id, downloader, existing_map, default_season=default_season)
        if downloader_type == "transmission":
            return await self._apply_transmission_unwanted(download_id, downloader, existing_map, default_season=default_season)
        raise HTTPException(status_code=502, detail=f"暂不支持 {downloader_type or '未知'} 下载器的文件选择")

    async def _apply_qb_unwanted(
        self,
        download_id: str,
        downloader: dict[str, Any],
        existing_map: dict[int, set[int]],
        *,
        default_season: int | None = None,
    ) -> dict[str, Any]:
        config = downloader.get("config") if isinstance(downloader.get("config"), dict) else {}
        base_url = self._downloader_base_url(config)
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            await self._qb_login(client, base_url, config)
            files: list[dict[str, Any]] = []
            for _ in range(30):
                resp = await client.get(self._join_base_url(base_url, "api/v2/torrents/files"), params={"hash": download_id})
                if resp.status_code < 400:
                    data = resp.json() if resp.text else []
                    files = data if isinstance(data, list) else []
                    if files:
                        break
                await asyncio.sleep(0.5)
            if not files:
                raise HTTPException(status_code=504, detail="下载器暂未返回种子文件列表")
            unwanted_ids = self._unwanted_file_ids_from_qb(files, existing_map, default_season=default_season)
            if unwanted_ids:
                resp = await client.post(
                    self._join_base_url(base_url, "api/v2/torrents/filePrio"),
                    data={"hash": download_id, "id": "|".join(str(item) for item in unwanted_ids), "priority": 0},
                )
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code, detail=f"qBittorrent 文件优先级设置失败: HTTP {resp.status_code}")
            return {
                "applied": bool(unwanted_ids),
                "mode": "missing",
                "downloaderType": "qbittorrent",
                "fileCount": len(files),
                "unwantedCount": len(unwanted_ids),
                "message": "没有命中已存在集数的文件" if not unwanted_ids else "",
            }

    async def _qb_login(self, client: httpx.AsyncClient, base_url: str, config: dict[str, Any]) -> None:
        username = str(config.get("username") or "")
        password = str(config.get("password") or "")
        if not username and not password:
            return
        resp = await client.post(
            self._join_base_url(base_url, "api/v2/auth/login"),
            data={"username": username, "password": password},
        )
        if resp.status_code >= 400 or resp.text.strip().lower().startswith("fails"):
            raise HTTPException(status_code=502, detail="qBittorrent 登录失败")

    async def _apply_transmission_unwanted(
        self,
        download_id: str,
        downloader: dict[str, Any],
        existing_map: dict[int, set[int]],
        *,
        default_season: int | None = None,
    ) -> dict[str, Any]:
        config = downloader.get("config") if isinstance(downloader.get("config"), dict) else {}
        rpc_url = self._transmission_rpc_url(config)
        auth = self._downloader_auth(config)
        async with httpx.AsyncClient(verify=False, timeout=30, auth=auth) as client:
            session_id = ""
            torrent: dict[str, Any] = {}
            for _ in range(30):
                payload = {
                    "method": "torrent-get",
                    "arguments": {
                        "ids": [download_id],
                        "fields": ["id", "hashString", "name", "files", "fileStats"],
                    },
                }
                data, session_id = await self._transmission_rpc(client, rpc_url, payload, session_id=session_id)
                torrents = ((data.get("arguments") or {}).get("torrents") or []) if isinstance(data, dict) else []
                torrent = torrents[0] if torrents else {}
                if torrent.get("files"):
                    break
                await asyncio.sleep(0.5)
            files = torrent.get("files") if isinstance(torrent.get("files"), list) else []
            if not files:
                raise HTTPException(status_code=504, detail="Transmission 暂未返回种子文件列表")
            unwanted_ids = self._unwanted_file_ids_from_transmission(files, existing_map, default_season=default_season)
            if unwanted_ids:
                payload = {
                    "method": "torrent-set",
                    "arguments": {
                        "ids": [torrent.get("id") or download_id],
                        "files-unwanted": unwanted_ids,
                    },
                }
                await self._transmission_rpc(client, rpc_url, payload, session_id=session_id)
            return {
                "applied": bool(unwanted_ids),
                "mode": "missing",
                "downloaderType": "transmission",
                "fileCount": len(files),
                "unwantedCount": len(unwanted_ids),
                "message": "没有命中已存在集数的文件" if not unwanted_ids else "",
            }

    async def _transmission_rpc(
        self,
        client: httpx.AsyncClient,
        rpc_url: str,
        payload: dict[str, Any],
        *,
        session_id: str = "",
    ) -> tuple[dict[str, Any], str]:
        headers = {"X-Transmission-Session-Id": session_id} if session_id else {}
        resp = await client.post(rpc_url, json=payload, headers=headers)
        if resp.status_code == 409:
            session_id = str(resp.headers.get("X-Transmission-Session-Id") or "")
            resp = await client.post(rpc_url, json=payload, headers={"X-Transmission-Session-Id": session_id})
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=f"Transmission RPC 失败: HTTP {resp.status_code}")
        data = resp.json() if resp.text else {}
        if isinstance(data, dict) and data.get("result") not in (None, "success"):
            raise HTTPException(status_code=502, detail=f"Transmission RPC 失败: {data.get('result')}")
        return data if isinstance(data, dict) else {}, session_id

    def _normalize_existing_episode_map(self, raw_map: dict[str, list[int]]) -> dict[int, set[int]]:
        result: dict[int, set[int]] = {}
        if not isinstance(raw_map, dict):
            return result
        for raw_season, raw_episodes in raw_map.items():
            season = self._safe_int(raw_season)
            if season <= 0 or not isinstance(raw_episodes, list):
                continue
            episodes = {self._safe_int(item) for item in raw_episodes}
            episodes = {item for item in episodes if item > 0}
            if episodes:
                result[season] = episodes
        return result

    def _unwanted_file_ids_from_qb(
        self,
        files: list[dict[str, Any]],
        existing_map: dict[int, set[int]],
        *,
        default_season: int | None = None,
    ) -> list[int]:
        unwanted: list[int] = []
        for file_item in files:
            file_id = file_item.get("index")
            if file_id is None:
                file_id = file_item.get("id")
            if file_id is None:
                continue
            name = str(file_item.get("name") or "")
            refs = self._episode_refs(name, default_season=default_season)
            if self._all_episode_refs_exist(refs, existing_map, default_season=default_season):
                unwanted.append(self._safe_int(file_id))
        return unwanted

    def _unwanted_file_ids_from_transmission(
        self,
        files: list[dict[str, Any]],
        existing_map: dict[int, set[int]],
        *,
        default_season: int | None = None,
    ) -> list[int]:
        unwanted: list[int] = []
        for index, file_item in enumerate(files):
            name = str(file_item.get("name") or "")
            refs = self._episode_refs(name, default_season=default_season)
            if self._all_episode_refs_exist(refs, existing_map, default_season=default_season):
                unwanted.append(index)
        return unwanted

    def _episode_refs(self, text: str, *, default_season: int | None = None) -> list[tuple[int, int]]:
        source = str(text or "")
        refs: set[tuple[int, int]] = set()

        def add_range(raw_season: Any, raw_start: Any, raw_end: Any = None) -> None:
            season = self._safe_int(raw_season) or self._safe_int(default_season)
            start = self._safe_int(raw_start)
            end = self._safe_int(raw_end) or start
            if season <= 0 or start <= 0 or end <= 0:
                return
            low, high = sorted((start, end))
            if high - low > 120:
                return
            for episode_num in range(low, high + 1):
                refs.add((season, episode_num))

        for match in re.finditer(r"s(?:eason)?\s*0?(\d{1,2})\s*e(?:p(?:isode)?)?\s*0?(\d{1,3})(?:\s*(?:-|~|至|到)\s*(?:e(?:p(?:isode)?)?)?\s*0?(\d{1,3}))?", source, re.I):
            add_range(match.group(1), match.group(2), match.group(3))
        for match in re.finditer(r"(?<!\d)(\d{1,2})x0?(\d{1,3})(?:\s*(?:-|~|至|到)\s*0?(\d{1,3}))?", source, re.I):
            add_range(match.group(1), match.group(2), match.group(3))
        for match in re.finditer(r"第\s*0?(\d{1,2})\s*季.*?第\s*0?(\d{1,3})(?:\s*(?:-|~|至|到)\s*0?(\d{1,3}))?\s*[集话話]", source):
            add_range(match.group(1), match.group(2), match.group(3))
        if default_season:
            for match in re.finditer(r"(?<![a-z0-9])e(?:p(?:isode)?)?\s*0?(\d{1,3})(?:\s*(?:-|~|至|到)\s*(?:e(?:p(?:isode)?)?)?\s*0?(\d{1,3}))?", source, re.I):
                add_range(default_season, match.group(1), match.group(2))
            for match in re.finditer(r"第\s*0?(\d{1,3})(?:\s*(?:-|~|至|到)\s*0?(\d{1,3}))?\s*[集话話]", source):
                add_range(default_season, match.group(1), match.group(2))
        return sorted(refs)

    def _all_episode_refs_exist(
        self,
        refs: list[tuple[int, int]],
        existing_map: dict[int, set[int]],
        *,
        default_season: int | None = None,
    ) -> bool:
        if not refs:
            return False
        fallback_season = self._safe_int(default_season)
        if not fallback_season and len(existing_map) == 1:
            fallback_season = next(iter(existing_map.keys()))
        for season, episode_num in refs:
            season_key = season or fallback_season
            if season_key <= 0 or episode_num not in existing_map.get(season_key, set()):
                return False
        return True

    def _downloader_base_url(self, config: dict[str, Any]) -> str:
        host = str(config.get("host") or "").strip()
        port = self._safe_int(config.get("port"))
        if not host:
            raise HTTPException(status_code=502, detail="下载器配置缺少 host")
        if not re.match(r"^https?://", host, re.I):
            host = f"http://{host}"
        parsed = urlparse(host)
        if port and parsed.hostname and not parsed.port:
            host = parsed._replace(netloc=f"{parsed.hostname}:{port}").geturl()
        return host.rstrip("/")

    def _join_base_url(self, base_url: str, path: str) -> str:
        return urljoin(f"{str(base_url or '').rstrip('/')}/", str(path or "").lstrip("/"))

    def _downloader_auth(self, config: dict[str, Any]) -> tuple[str, str] | None:
        username = str(config.get("username") or "")
        password = str(config.get("password") or "")
        if username or password:
            return username, password
        return None

    def _transmission_rpc_url(self, config: dict[str, Any]) -> str:
        base_url = self._downloader_base_url(config)
        parsed = urlparse(base_url)
        if parsed.path and parsed.path != "/" and parsed.path.endswith("/rpc"):
            return base_url
        return self._join_base_url(base_url, "transmission/rpc")

    async def _download_torrent_file(self, url: str, torrent: dict[str, Any]) -> bytes:
        raw_url = str(url or "").strip()
        site_cookie = str(torrent.get("site_cookie") or "").strip()
        if raw_url.startswith("["):
            download_url = await self._resolve_encoded_torrent_url(raw_url, torrent, site_cookie=site_cookie)
            site_cookie = ""
        else:
            download_url = self._absolute_torrent_url(raw_url, torrent)
        proxy_url = self._torrent_proxy_url()
        headers: dict[str, str] = {
            "User-Agent": str(torrent.get("site_ua") or "Mozilla/5.0").strip(),
            "Accept": "application/x-bittorrent,*/*",
        }
        if site_cookie:
            headers["Cookie"] = site_cookie
        referer = str(torrent.get("referer") or torrent.get("page_url") or "").strip()
        if urlparse(referer).scheme in {"http", "https"}:
            headers["Referer"] = referer
        client_kwargs: dict[str, Any] = {
            "verify": False,
            "timeout": 60,
            "follow_redirects": False,
        }
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(download_url, headers=headers)
                for _ in range(8):
                    if resp.status_code not in {301, 302, 303, 307, 308}:
                        break
                    location = str(resp.headers.get("location") or "").strip()
                    if not location:
                        break
                    if location.lower().startswith("magnet:"):
                        raise HTTPException(status_code=502, detail="MoviePilot 种子下载返回磁力链接，无法预览文件列表")
                    download_url = self._absolute_torrent_url(location, torrent, fallback_base=str(resp.url))
                    resp = await client.get(download_url, headers=headers)
        except httpx.TimeoutException as e:
            raise HTTPException(status_code=504, detail="MoviePilot 种子下载超时") from e
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"MoviePilot 种子下载失败: {e}") from e
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=f"MoviePilot 种子下载失败: HTTP {resp.status_code}")
        content = resp.content or b""
        if not content:
            raise HTTPException(status_code=502, detail="MoviePilot 种子下载为空")
        if content[:1] != b"d":
            sample = content[:120].decode("utf-8", errors="replace")
            raise HTTPException(status_code=502, detail=f"MoviePilot 未返回有效种子文件: {sample}")
        return content

    async def _resolve_encoded_torrent_url(self, url: str, torrent: dict[str, Any], *, site_cookie: str) -> str:
        match = re.match(r"^\[(.*?)](.*)$", str(url or "").strip(), re.S)
        if not match:
            raise HTTPException(status_code=502, detail="MoviePilot 种子下载链接格式无效")
        encoded_params = match.group(1)
        request_url = self._normalize_url_scheme(match.group(2))
        request_url = self._absolute_torrent_url(request_url, torrent)
        if not encoded_params:
            return request_url
        try:
            decoded = base64.b64decode(encoded_params.encode("utf-8")).decode("utf-8")
            request_params = json.loads(decoded)
            if not isinstance(request_params, dict):
                raise ValueError("配置不是对象")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"MoviePilot 种子下载链接解析失败: {e}") from e

        headers = request_params.get("header") if isinstance(request_params.get("header"), dict) else None
        request_headers = {str(key): str(value) for key, value in (headers or {}).items()}
        if self._truthy(request_params.get("cookie")) and site_cookie:
            request_headers["Cookie"] = site_cookie

        request_kwargs: dict[str, Any] = {"headers": request_headers}
        if isinstance(request_params.get("params"), dict):
            request_kwargs["params"] = request_params.get("params")
        if request_params.get("json") is not None:
            request_kwargs["json"] = request_params.get("json")
        if request_params.get("data") is not None:
            request_kwargs["data"] = request_params.get("data")

        proxy_url = self._torrent_proxy_url()
        client_kwargs: dict[str, Any] = {
            "verify": False,
            "timeout": 60,
            "follow_redirects": True,
        }
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
            client_kwargs["trust_env"] = False

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                method = str(request_params.get("method") or "get").lower()
                if method == "get":
                    resp = await client.get(request_url, **request_kwargs)
                else:
                    resp = await client.post(request_url, **request_kwargs)
        except httpx.TimeoutException as e:
            raise HTTPException(status_code=504, detail="MoviePilot 种子下载地址解析超时") from e
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"MoviePilot 种子下载地址解析失败: {e}") from e

        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=f"MoviePilot 种子下载地址解析失败: HTTP {resp.status_code}")

        result_path = str(request_params.get("result") or "").strip()
        if not result_path:
            resolved_url = resp.text.strip()
        else:
            try:
                data: Any = resp.json()
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"MoviePilot 种子下载地址解析返回非 JSON: {e}") from e
            for key in result_path.split("."):
                if not isinstance(data, dict):
                    data = None
                    break
                data = data.get(key)
                if data in (None, ""):
                    break
            resolved_url = str(data or "").strip()
        if not resolved_url:
            raise HTTPException(status_code=502, detail="MoviePilot 种子下载地址解析为空")
        return self._absolute_torrent_url(self._normalize_url_scheme(resolved_url), torrent, fallback_base=request_url)

    def _torrent_proxy_url(self) -> str:
        candidates = [
            self._load_config().get("proxy_url"),
            self._load_config().get("proxy"),
            self._load_config().get("http_proxy"),
            self._load_config().get("https_proxy"),
            self._load_settings().get("proxy_url"),
            self._load_settings().get("network_http_proxy"),
            os.environ.get("CHILLPOSTER_PROXY_URL"),
            os.environ.get("HTTPS_PROXY"),
            os.environ.get("https_proxy"),
            os.environ.get("HTTP_PROXY"),
            os.environ.get("http_proxy"),
            os.environ.get("ALL_PROXY"),
            os.environ.get("all_proxy"),
        ]
        for value in candidates:
            proxy_url = str(value or "").strip()
            if not proxy_url:
                continue
            if proxy_url.startswith(("http://", "https://", "socks4://", "socks5://")):
                return proxy_url
            logger.debug(f"[MoviePilot资源] 忽略格式无效的代理地址: {proxy_url}")
        return ""

    def _absolute_torrent_url(
        self,
        url: str,
        torrent: dict[str, Any],
        *,
        fallback_base: str | None = None,
    ) -> str:
        raw_url = self._normalize_url_scheme(url)
        if not raw_url:
            raise HTTPException(status_code=502, detail="MoviePilot 资源缺少种子下载链接")
        parsed = urlparse(raw_url)
        if parsed.scheme in {"http", "https"}:
            return raw_url
        if parsed.scheme == "magnet":
            raise HTTPException(status_code=502, detail="MoviePilot 资源返回磁力链接，无法预览文件列表")
        if raw_url.startswith("//"):
            return f"https:{raw_url}"

        for base in self._torrent_url_bases(torrent, fallback_base=fallback_base):
            return urljoin(base, raw_url)

        first_part = raw_url.split("/", 1)[0]
        if "." in first_part and " " not in first_part:
            return f"https://{raw_url.lstrip('/')}"
        raise HTTPException(status_code=502, detail="MoviePilot 种子下载链接缺少协议，且无法从详情页补全")

    def _normalize_url_scheme(self, url: Any) -> str:
        text = str(url or "").strip()
        text = re.sub(r"^(https?):/([^/])", r"\1://\2", text, flags=re.IGNORECASE)
        return text

    def _torrent_url_bases(self, torrent: dict[str, Any], *, fallback_base: str | None = None) -> list[str]:
        values = [
            fallback_base,
            torrent.get("page_url"),
            torrent.get("referer"),
            torrent.get("detail_url"),
            torrent.get("details_url"),
            torrent.get("site_url"),
            torrent.get("site_domain"),
            torrent.get("domain"),
        ]
        bases: list[str] = []
        for value in values:
            base = self._normal_url_base(value)
            if base and base not in bases:
                bases.append(base)
        return bases

    def _normal_url_base(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("//"):
            return f"https:{text}"
        parsed = urlparse(text)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return text
        first_part = text.split("/", 1)[0]
        if "." in first_part and " " not in first_part:
            return f"https://{text.lstrip('/')}"
        return ""

    def _truthy(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}

    def _parse_torrent_preview_items(
        self,
        content: bytes,
        *,
        season: int | None = None,
        episode: int | None = None,
        media_type: str = "movie",
    ) -> list[dict[str, Any]]:
        try:
            payload, index = self._bdecode(content, 0)
            if index > len(content):
                raise ValueError("bencode 越界")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"种子文件解析失败: {e}") from e
        if not isinstance(payload, dict):
            raise HTTPException(status_code=502, detail="种子文件结构无效")
        info = payload.get(b"info")
        if not isinstance(info, dict):
            raise HTTPException(status_code=502, detail="种子文件缺少 info 信息")

        root_name = self._decode_torrent_text(info.get(b"name") or b"")
        raw_files = info.get(b"files")
        items: list[dict[str, Any]] = []
        is_tv = str(media_type or "").lower() in {"tv", "series"}
        if isinstance(raw_files, list):
            for file_item in raw_files:
                if not isinstance(file_item, dict):
                    continue
                path_parts = file_item.get(b"path") or []
                if not isinstance(path_parts, list):
                    path_parts = []
                names = [self._decode_torrent_text(part) for part in path_parts if part not in (None, b"")]
                if not names:
                    continue
                path = "/".join([part for part in ([root_name] if root_name else []) + names if part])
                size = self._safe_int(file_item.get(b"length"))
                items.append(self._build_preview_file_item(path, size=size, is_tv=is_tv, season=season, episode=episode))
        else:
            size = self._safe_int(info.get(b"length"))
            name = root_name or "torrent-file"
            items.append(self._build_preview_file_item(name, size=size, is_tv=is_tv, season=season, episode=episode))
        return items[:500]

    def _build_preview_file_item(
        self,
        path: str,
        *,
        size: int,
        is_tv: bool,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        name = str(path or "").split("/")[-1] or str(path or "")
        ext = Path(name).suffix.lower()
        ep_score = self._episode_match_score(name, season, episode) if is_tv else 0
        return {
            "id": hashlib.sha1(f"{path}|{size}".encode("utf-8")).hexdigest()[:16],
            "name": name,
            "path": path,
            "type": "file",
            "depth": max(0, len([part for part in str(path).split("/") if part]) - 1),
            "size": size,
            "sizeLabel": self._format_size(size),
            "isVideo": ext in {".mp4", ".mkv", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".flv", ".webm", ".iso"},
            "episodeMatched": bool(ep_score > 0),
            "episodeScore": ep_score,
        }

    def _decode_torrent_text(self, value: Any) -> str:
        if isinstance(value, bytes):
            for encoding in ("utf-8", "gb18030", "big5"):
                try:
                    return value.decode(encoding).strip()
                except UnicodeDecodeError:
                    continue
            return value.decode("utf-8", errors="replace").strip()
        return str(value or "").strip()

    def _bdecode(self, data: bytes, index: int = 0) -> tuple[Any, int]:
        if index >= len(data):
            raise ValueError("数据不完整")
        marker = data[index:index + 1]
        if marker == b"i":
            end = data.index(b"e", index)
            return int(data[index + 1:end]), end + 1
        if marker == b"l":
            index += 1
            result = []
            while data[index:index + 1] != b"e":
                value, index = self._bdecode(data, index)
                result.append(value)
            return result, index + 1
        if marker == b"d":
            index += 1
            result = {}
            while data[index:index + 1] != b"e":
                key, index = self._bdecode(data, index)
                value, index = self._bdecode(data, index)
                result[key] = value
            return result, index + 1
        if marker.isdigit():
            colon = data.index(b":", index)
            length = int(data[index:colon])
            start = colon + 1
            end = start + length
            if end > len(data):
                raise ValueError("字符串长度越界")
            return data[start:end], end
        raise ValueError(f"未知 bencode 标记: {marker!r}")

    def _resource_id(self, torrent: dict[str, Any]) -> str:
        seed = "|".join(
            str(torrent.get(key) or "")
            for key in ("site", "site_name", "title", "description", "size", "pubdate", "page_url", "enclosure")
        )
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]

    def _format_size(self, value: Any) -> str:
        try:
            size = int(value or 0)
        except Exception:
            return ""
        if size <= 0:
            return ""
        units = ("B", "KB", "MB", "GB", "TB")
        amount = float(size)
        index = 0
        while amount >= 1024 and index < len(units) - 1:
            amount /= 1024
            index += 1
        if index <= 1:
            return f"{amount:.0f}{units[index]}"
        return f"{amount:.2f}{units[index]}".rstrip("0").rstrip(".")

    def _safe_int(self, value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    def _safe_float(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except Exception:
            return None

    def _promotion_info(self, torrent: dict[str, Any]) -> dict[str, Any]:
        download_factor = self._safe_float(torrent.get("downloadvolumefactor"))
        upload_factor = self._safe_float(torrent.get("uploadvolumefactor"))
        volume_factor = str(torrent.get("volume_factor") or "").strip()
        labels = torrent.get("labels")
        if isinstance(labels, list):
            label_text = " ".join(str(item or "") for item in labels)
        else:
            label_text = str(labels or "")
        raw_text = " ".join(
            part for part in (
                volume_factor,
                label_text,
                str(torrent.get("freedate") or ""),
                str(torrent.get("freedate_diff") or ""),
            )
            if part
        )
        parts: list[str] = []
        key = "normal"
        css_class = ""
        if download_factor == 0 or re.search(r"free|免费", raw_text, re.IGNORECASE):
            parts.append("免费")
            key = "free"
            css_class = "free"
        elif download_factor is not None and 0 < download_factor < 1:
            parts.append(f"{download_factor * 100:g}%下载")
            key = "discount"
            css_class = "discount"
        if upload_factor is not None and upload_factor > 1:
            parts.append(f"{upload_factor:g}x上传")
            if key == "normal":
                key = "upload"
                css_class = "upload"
        if not parts and volume_factor and volume_factor not in {"1", "1.0", "1.00"}:
            parts.append(volume_factor)
            key = "other"
            css_class = "other"
        label = " ".join(parts)
        return {
            "label": label,
            "key": key,
            "class": css_class,
            "text": " ".join(part for part in (label, raw_text) if part).strip(),
            "download_factor": download_factor,
            "upload_factor": upload_factor,
            "free_date": str(torrent.get("freedate") or ""),
        }

    def _tags(self, torrent: dict[str, Any], *, size_label: str, site_name: str) -> list[str]:
        text = " ".join(
            str(torrent.get(key) or "")
            for key in ("title", "description", "category")
        )
        tags: list[str] = []
        if site_name:
            self._append_tag(tags, site_name)
        patterns = [
            (r"\b8k\b", "8K"),
            (r"\b4k\b|uhd", "4K"),
            (r"2160p", "2160P"),
            (r"1080p", "1080P"),
            (r"720p", "720P"),
            (r"web[- ]?dl|webdl", "WEB-DL"),
            (r"webrip", "WEBRip"),
            (r"blu[- ]?ray|bluray", "BluRay"),
            (r"remux", "REMUX"),
            (r"\bhdr\b|hdrvivid", "HDR"),
            (r"dolby vision|\bdovi\b|\bdv\b", "DV"),
            (r"h[ .]?265|hevc", "H265"),
            (r"h[ .]?264|\bavc\b", "H264"),
            (r"\baac\b", "AAC"),
            (r"\bddp\b|eac-?3", "DDP"),
            (r"atmos", "Atmos"),
            (r"60fps|60帧", "60FPS"),
            (r"简中|简体|中字", "中字"),
            (r"国语", "国语"),
            (r"粤语", "粤语"),
        ]
        for pattern, label in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                self._append_tag(tags, label)
        if size_label:
            self._append_tag(tags, size_label)
        self._append_tag(tags, "MP")
        return tags

    def _append_tag(self, tags: list[str], label: str) -> None:
        label = str(label or "").strip()
        if label and label not in tags:
            tags.append(label)

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


moviepilot_resource_service = MoviePilotResourceService()
