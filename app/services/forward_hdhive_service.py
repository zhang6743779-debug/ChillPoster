import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request
from p115client.util import share_extract_payload

from app.routers.config_302 import get_config_302
from app.services.drive115_service import drive115_service
from app.services.hdhive_openapi_client import HDHiveAPIError, HDHiveOpenClient
from app.services.hdhive_service import hdhive_service
from app.services.media_organize_115_ops import _get_115_fs, run_115_write_request
from app.services.media_organize_state import VIDEO_EXTS
from app.services.transfer_service import transfer_service
from core.logger import logger


CONFIG_PATH = Path("config/forward_hdhive.json")
MEDIA_ORGANIZE_CONFIG_PATH = Path("config/media_organize.json")
AIYING_API_URL = "http://api.ayclub.vip:5050/api/chill"


def _default_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "account_id": "",
        "widget_token": uuid.uuid4().hex,
        "public_base_url": "",
        "hdhive_enabled": True,
        "max_unlock_points": 4,
        "library_enabled": True,
        "transfer_mode": "series",
        "aiying_enabled": False,
        "aiying_tg_id": "",
        "aiying_chill_token": "",
        "aiying_rate_limit_per_minute": 6,
        "aiying_daily_limit": 500,
        "aiying_success_count": 0,
        "aiying_today_used": 0,
        "aiying_daily_date": "",
        "aiying_daily_start_times": None,
        "aiying_last_times": None,
        "aiying_last_message": "",
        "aiying_last_result_count": 0,
        "aiying_last_checked_at": "",
    }


class ForwardHDHiveService:
    def __init__(self) -> None:
        self.config = _default_config()
        self._resource_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._aiying_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._aiying_play_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._aiying_rate_lock = asyncio.Lock()
        self._aiying_rate_times: list[float] = []
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

    def get_search_source_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        accounts = self.get_account_options()
        account_id = str(self.config.get("account_id") or "").strip()
        selected_account = next((item for item in accounts if str(item.get("id") or "") == account_id), None) if account_id else None
        has_hdhive_account = bool(
            selected_account.get("has_api_key") if selected_account
            else any(item.get("has_api_key") for item in accounts)
        )
        if has_hdhive_account:
            options.append({
                "key": "hdhive",
                "label": "影巢",
                "name": "影巢",
                "description": "使用 Forward 模块中的影巢账号查询资源",
            })
        if self._aiying_configured(require_enabled=False):
            options.append({
                "key": "aiying",
                "label": "爱影",
                "name": "爱影",
                "description": "使用 Forward 模块中的爱影 Token 查询资源",
            })
        return options

    def get_config(self, request: Request | None = None, *, telegram_user_id: str = "") -> dict[str, Any]:
        aiying_tg_id = str(self.config.get("aiying_tg_id") or "").strip()
        if not aiying_tg_id and telegram_user_id:
            aiying_tg_id = str(telegram_user_id).strip()
        return {
            "enabled": bool(self.config.get("enabled", True)),
            "account_id": str(self.config.get("account_id") or ""),
            "public_base_url": str(self.config.get("public_base_url") or ""),
            "hdhive_enabled": bool(self.config.get("hdhive_enabled", True)),
            "max_unlock_points": int(self.config.get("max_unlock_points") or 0),
            "library_enabled": bool(self.config.get("library_enabled", True)),
            "transfer_mode": str(self.config.get("transfer_mode") or "series"),
            "aiying_enabled": bool(self.config.get("aiying_enabled", False)),
            "aiying_tg_id": aiying_tg_id,
            "aiying_chill_token": str(self.config.get("aiying_chill_token") or ""),
            "aiying_rate_limit_per_minute": int(self.config.get("aiying_rate_limit_per_minute") or 6),
            "aiying_daily_limit": int(self.config.get("aiying_daily_limit") or 500),
            "aiying_success_count": int(self.config.get("aiying_success_count") or 0),
            "aiying_today_used": int(self.config.get("aiying_today_used") or 0),
            "aiying_last_times": self.config.get("aiying_last_times"),
            "aiying_last_message": str(self.config.get("aiying_last_message") or ""),
            "aiying_last_result_count": int(self.config.get("aiying_last_result_count") or 0),
            "aiying_last_checked_at": str(self.config.get("aiying_last_checked_at") or ""),
            "telegram_user_id": str(telegram_user_id or ""),
            "widget_path": self.get_widget_path(),
            "accounts": self.get_account_options(),
            "widget_url": self.get_widget_url(request),
        }

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "enabled",
            "account_id",
            "public_base_url",
            "hdhive_enabled",
            "max_unlock_points",
            "library_enabled",
            "transfer_mode",
            "aiying_enabled",
            "aiying_tg_id",
            "aiying_chill_token",
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
            if key == "library_enabled":
                value = bool(value)
            if key == "hdhive_enabled":
                value = bool(value)
            if key == "transfer_mode":
                value = str(value or "series").strip().lower()
                if value not in {"series", "episode"}:
                    value = "series"
            if key == "aiying_enabled":
                value = bool(value)
            if key in {"aiying_tg_id", "aiying_chill_token"}:
                value = str(value or "").strip()
            self.config[key] = value
        if not self.config.get("widget_token"):
            self.config["widget_token"] = uuid.uuid4().hex
        self._save_config()
        return self.get_config()

    def refresh_widget_token(self) -> None:
        self.config["widget_token"] = uuid.uuid4().hex
        self._save_config()

    def verify_token(self, token: str | None) -> None:
        expected = str(self.config.get("widget_token") or "").strip()
        if expected and str(token or "").strip() != expected:
            raise HTTPException(status_code=403, detail="Forward 模块 Token 无效")

    def _get_api_key(self, *, require_enabled: bool = True) -> str:
        account_id = str(self.config.get("account_id") or "").strip()
        accounts = list(hdhive_service.config.accounts)
        selected = None
        if account_id:
            selected = next((a for a in accounts if a.id == account_id and (a.enabled or not require_enabled)), None)
        if selected is None:
            selected = next((a for a in accounts if (a.enabled or not require_enabled) and a.api_key), None)
        if selected is None or not selected.api_key:
            raise HTTPException(status_code=400, detail="请先在影巢配置中填写可用 API Key")
        return selected.api_key

    def _cache_key(self, media_type: str, tmdb_id: str | int) -> str:
        return f"{media_type}:{tmdb_id}"

    def _aiying_configured(self, *, require_enabled: bool = True) -> bool:
        return bool(
            (self.config.get("aiying_enabled") or not require_enabled)
            and str(self.config.get("aiying_tg_id") or "").strip()
            and str(self.config.get("aiying_chill_token") or "").strip()
        )

    async def _aiying_rate_wait(self) -> None:
        async with self._aiying_rate_lock:
            while True:
                now = time.monotonic()
                rate_limit = max(1, int(self.config.get("aiying_rate_limit_per_minute") or 6))
                self._aiying_rate_times = [t for t in self._aiying_rate_times if now - t < 60]
                if len(self._aiying_rate_times) < rate_limit:
                    self._aiying_rate_times.append(now)
                    return
                wait_seconds = max(0.1, 60 - (now - self._aiying_rate_times[0]))
                logger.info(f"[Forward爱影] 触发 {rate_limit}/min 限频，等待 {wait_seconds:.1f}s")
                await asyncio.sleep(wait_seconds)

    def _aiying_daily_limited(self) -> bool:
        daily_limit = int(self.config.get("aiying_daily_limit") or 0)
        if daily_limit <= 0:
            return False
        today = time.strftime("%Y-%m-%d")
        if str(self.config.get("aiying_daily_date") or "") != today:
            return False
        try:
            return int(self.config.get("aiying_today_used") or 0) >= daily_limit
        except Exception:
            return False

    def _aiying_call_increment(self, current_times: int) -> int:
        try:
            previous_times = int(self.config.get("aiying_last_times"))
        except Exception:
            previous_times = None
        if previous_times is None:
            return 1
        if current_times < previous_times:
            return max(1, previous_times - current_times)
        # 剩余调用变多通常是充值；本次成功查询仍计 1 次。
        return 1

    def _update_aiying_stats(self, *, message: str = "", times: Any = None, result_count: int = 0) -> None:
        self.config["aiying_last_message"] = str(message or "")
        self.config["aiying_last_result_count"] = int(result_count or 0)
        self.config["aiying_last_checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        today = time.strftime("%Y-%m-%d")
        if times is not None:
            try:
                current_times = int(times)
                increment = self._aiying_call_increment(current_times)
                self.config["aiying_success_count"] = int(self.config.get("aiying_success_count") or 0) + increment
                if str(self.config.get("aiying_daily_date") or "") != today:
                    self.config["aiying_daily_date"] = today
                    self.config["aiying_today_used"] = 0
                    self.config["aiying_daily_start_times"] = current_times + increment
                self.config["aiying_today_used"] = int(self.config.get("aiying_today_used") or 0) + increment
                self.config["aiying_last_times"] = current_times
            except Exception:
                self.config["aiying_success_count"] = int(self.config.get("aiying_success_count") or 0) + 1
                if str(self.config.get("aiying_daily_date") or "") != today:
                    self.config["aiying_daily_date"] = today
                    self.config["aiying_today_used"] = 0
                self.config["aiying_today_used"] = int(self.config.get("aiying_today_used") or 0) + 1
                self.config["aiying_last_times"] = times
        else:
            self.config["aiying_success_count"] = int(self.config.get("aiying_success_count") or 0) + 1
            if str(self.config.get("aiying_daily_date") or "") != today:
                self.config["aiying_daily_date"] = today
                self.config["aiying_today_used"] = 0
            self.config["aiying_today_used"] = int(self.config.get("aiying_today_used") or 0) + 1
        self._save_config()

    async def fetch_aiying_resources(
        self,
        media_type: str,
        tmdb_id: str | int,
        *,
        use_cache: bool = True,
        require_enabled: bool = True,
    ) -> list[dict[str, Any]]:
        if not self._aiying_configured(require_enabled=require_enabled):
            return []
        normalized_type = "tv" if str(media_type or "").lower() in {"tv", "series"} else "movie"
        key = f"aiying:{normalized_type}:{tmdb_id}"
        now = time.time()
        cached = self._aiying_cache.get(key)
        if use_cache and cached and cached[0] > now:
            return cached[1]
        if self._aiying_daily_limited():
            raise HTTPException(status_code=429, detail="爱影查询已达到今日 API 调用上限")

        payload = {
            "tg_id": str(self.config.get("aiying_tg_id") or "").strip(),
            "type": normalized_type,
            "tmdb_id": str(tmdb_id or "").strip(),
            "chill_token": str(self.config.get("aiying_chill_token") or "").strip(),
        }
        await self._aiying_rate_wait()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(AIYING_API_URL, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"爱影查询失败: HTTP {e.response.status_code}") from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"爱影查询失败: {e}") from e

        state = data.get("state") if isinstance(data, dict) else None
        if state not in {0, "0", True}:
            message = str(data.get("message") or data.get("error") or "爱影查询未返回成功状态") if isinstance(data, dict) else "爱影查询响应异常"
            raise HTTPException(status_code=502, detail=message)

        raw_items = data.get("data") if isinstance(data, dict) else []
        items = [dict(item) for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
        for item in items:
            item["source_key"] = "aiying"
        self._aiying_cache[key] = (now + 300, items)
        if items:
            self._update_aiying_stats(
                message=str(data.get("message") or ""),
                times=data.get("times"),
                result_count=len(items),
            )
        return items

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

    def _is_already_transferred_message(self, message: str) -> bool:
        text = str(message or "")
        return any(pattern in text for pattern in ("已经转存过", "文件已接收", "无需重复接收"))

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

    def fetch_resources(
        self,
        media_type: str,
        tmdb_id: str | int,
        *,
        use_cache: bool = True,
        require_enabled: bool = True,
    ) -> list[dict[str, Any]]:
        normalized_type = "tv" if str(media_type or "").lower() in {"tv", "series"} else "movie"
        key = self._cache_key(normalized_type, tmdb_id)
        now = time.time()
        cached = self._resource_cache.get(key)
        if use_cache and cached and cached[0] > now:
            return cached[1]
        api_key = self._get_api_key(require_enabled=require_enabled)
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

    def _append_tag(self, tags: list[str], value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        normalized = {
            "4k": "4K",
            "8k": "8K",
            "1080p": "1080P",
            "720p": "720P",
            "2160p": "2160P",
            "webrip": "WEBRip",
            "web-dl": "WEB-DL",
            "webdl": "WEB-DL",
            "web": "WEB",
            "hdr": "HDR",
            "dv": "DV",
            "h265": "H265",
            "h.265": "H265",
            "hevc": "HEVC",
            "h264": "H264",
            "h.264": "H264",
            "avc": "AVC",
            "remux": "REMUX",
        }.get(text.lower(), text)
        if normalized not in tags:
            tags.append(normalized)

    def _join_tags(self, tags: list[str]) -> str:
        result: list[str] = []
        for tag in tags:
            self._append_tag(result, tag)
        return "|".join(result)

    def _resource_tags(self, item: dict[str, Any]) -> str:
        tags: list[str] = []
        values = item.get("video_resolution")
        if isinstance(values, list):
            for value in values:
                self._append_tag(tags, value)
        elif values:
            self._append_tag(tags, values)

        values = item.get("source")
        if isinstance(values, list):
            for value in values:
                for part in re.split(r"[/,，\s]+", str(value or "")):
                    self._append_tag(tags, part)
        elif values:
            for part in re.split(r"[/,，\s]+", str(values or "")):
                self._append_tag(tags, part)

        for key in ("subtitle_language", "subtitle_type"):
            values = item.get(key)
            if isinstance(values, list):
                for value in values:
                    self._append_tag(tags, value)
            elif values:
                self._append_tag(tags, values)
        self._append_tag(tags, "HDHive")
        return self._join_tags(tags)

    def _describe_resource(self, item: dict[str, Any]) -> str:
        parts = []
        tag_parts = []
        for key in ("video_resolution", "source", "subtitle_language", "subtitle_type"):
            values = item.get(key)
            if isinstance(values, list):
                parts.extend(str(v) for v in values if v)
                if key == "source":
                    tag_parts.extend(str(v).split("/")[0] for v in values if v)
            elif values:
                parts.append(str(values))
                if key == "source":
                    tag_parts.append(str(values).split("/")[0])
        if item.get("share_size"):
            parts.append(f"整包 {item.get('share_size')}")
        parts.append(f"解锁 {self._resource_points(item)} 积分")
        tag_parts.append("HDHive")
        tag_line = "|".join(dict.fromkeys(tag.strip() for tag in tag_parts if tag and tag.strip()))
        return " | ".join(parts) + (f"\n{tag_line}" if tag_line else "")

    def _aiying_resource_id(self, item: dict[str, Any]) -> str:
        seed = "|".join(
            str(item.get(key) or "")
            for key in ("link", "name", "tmdb_id", "category", "notes")
        )
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]

    def _describe_aiying_resource(self, item: dict[str, Any]) -> str:
        parts = []
        for key in ("category", "notes", "release_date"):
            value = str(item.get(key) or "").strip()
            if value:
                parts.append(value)
        size = str(item.get("size") or "").strip()
        if size:
            parts.append(f"单集 {size}GB")
        return " | ".join(parts) + "\nAY|SVIP"

    def _aiying_tags(self, item: dict[str, Any]) -> str:
        text = " ".join(
            str(item.get(key) or "")
            for key in ("category", "name", "notes")
        )
        tags: list[str] = []
        patterns = [
            (r"\b8k\b", "8K"),
            (r"\b4k\b|uhd", "4K"),
            (r"2160p", "2160P"),
            (r"1080p", "1080P"),
            (r"720p", "720P"),
            (r"web[- ]?dl|webdl", "WEB-DL"),
            (r"webrip", "WEBRip"),
            (r"\bweb\b", "WEB"),
            (r"blu[- ]?ray|bluray", "BluRay"),
            (r"remux", "REMUX"),
            (r"\bhdr\b", "HDR"),
            (r"dolby vision|\bdv\b", "DV"),
            (r"h[ .]?265", "H265"),
            (r"hevc", "HEVC"),
            (r"h[ .]?264", "H264"),
            (r"\bavc\b", "AVC"),
            (r"\baac\b", "AAC"),
            (r"\bdts\b", "DTS"),
            (r"atmos", "Atmos"),
            (r"truehd", "TrueHD"),
            (r"简中|简体", "简中"),
            (r"繁中|繁体", "繁中"),
            (r"简英", "简英"),
            (r"双语", "双语"),
            (r"国语", "国语"),
            (r"粤语", "粤语"),
            (r"内封", "内封"),
            (r"外挂", "外挂"),
            (r"特效", "特效"),
        ]
        for pattern, label in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                self._append_tag(tags, label)
        self._append_tag(tags, "AY")
        self._append_tag(tags, "SVIP")
        return self._join_tags(tags)

    def filter_aiying_resources(
        self,
        resources: list[dict[str, Any]],
        *,
        media_type: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[dict[str, Any]]:
        items = [item for item in resources if isinstance(item, dict) and str(item.get("link") or "").strip()]
        is_tv = str(media_type or "").lower() in {"tv", "series"}
        if is_tv and episode:
            matched = [
                item for item in items
                if self._episode_match_score(str(item.get("name") or ""), season, episode) > 0
            ]
            if matched:
                items = matched
        return items

    def build_aiying_forward_resources(
        self,
        request: Request,
        params: dict[str, Any],
        resources: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        base = self.get_public_base_url(request)
        token = self.config.get("widget_token") or ""
        media_type = "tv" if str(params.get("type") or "").lower() in {"tv", "series"} else "movie"
        tmdb_id = str(params.get("tmdbId") or params.get("tmdb_id") or "").strip()
        season = self._to_optional_int(params.get("season"))
        episode = self._to_optional_int(params.get("episode"))
        ignore_enabled = bool(params.get("ignoreEnabled") or params.get("ignore_enabled"))
        result = []
        for item in self.filter_aiying_resources(resources, media_type=media_type, season=season, episode=episode):
            resource_id = self._aiying_resource_id(item)
            self._aiying_play_cache[resource_id] = (time.time() + 21600, dict(item))
            query = {
                "token": token,
                "source": "aiying",
                "resource_id": resource_id,
                "type": media_type,
                "tmdb_id": tmdb_id,
            }
            if season:
                query["season"] = str(season)
            if episode:
                query["episode"] = str(episode)
            if ignore_enabled:
                query["ignore_enabled"] = "1"
            qs = urlencode({k: v for k, v in query.items() if v})
            play_url = f"{base}/api/forward/play?{qs}"
            size = str(item.get("size") or "").strip()
            size_label = f"单集 {size}GB" if size else "单集大小未知"
            title = f"{item.get('name') or '爱影资源'} · {size_label}"
            result.append({
                "id": play_url,
                "type": "url",
                "title": title,
                "name": title,
                "description": self._describe_aiying_resource(item),
                "genreTitle": self._aiying_tags(item),
                "sourceKey": "aiying",
                "sourceName": "爱影",
                "resourceId": resource_id,
                "url": play_url,
                "videoUrl": play_url,
                "link": play_url,
                "mediaType": media_type,
                "playerType": "system",
            })
        return result

    def _to_optional_int(self, value: Any) -> int | None:
        try:
            if value in (None, ""):
                return None
            return int(value)
        except Exception:
            return None

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
        ignore_enabled = bool(params.get("ignoreEnabled") or params.get("ignore_enabled"))
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
            if ignore_enabled:
                query["ignore_enabled"] = "1"
            qs = urlencode({k: v for k, v in query.items() if v})
            play_url = f"{base}/api/forward/play?{qs}"
            size_label = str(item.get("share_size") or "未知大小").strip()
            title = f"{item.get('title') or '影巢资源'} · {size_label}"
            result.append({
                "id": play_url,
                "type": "url",
                "title": title,
                "name": title,
                "description": self._describe_resource(item),
                "genreTitle": self._resource_tags(item),
                "sourceKey": "hdhive",
                "sourceName": "影巢",
                "slug": slug,
                "url": play_url,
                "videoUrl": play_url,
                "link": play_url,
                "mediaType": media_type,
                "playerType": "system",
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

    def _share_item_id(self, item: dict[str, Any]) -> str:
        return str(item.get("fid") or item.get("file_id") or item.get("id") or item.get("cid") or "")

    def _share_item_name(self, item: dict[str, Any]) -> str:
        return str(item.get("n") or item.get("fn") or item.get("file_name") or item.get("name") or "")

    def _share_item_size(self, item: dict[str, Any]) -> int:
        for key in ("s", "size", "file_size"):
            try:
                return int(item.get(key) or 0)
            except Exception:
                pass
        return 0

    def _share_item_is_dir(self, item: dict[str, Any]) -> bool:
        if item.get("is_dir") is True or item.get("is_directory") is True:
            return True
        if str(item.get("fc") or "") == "0":
            return True
        if item.get("cid") and not item.get("fid"):
            return True
        icon = str(item.get("ico") or item.get("icon") or "").lower()
        return icon in {"folder", "dir"}

    def _extract_share_items(self, resp: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(resp, dict):
            return []
        data = resp.get("data")
        if isinstance(data, list):
            return [dict(item) for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("list", "items", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return [dict(item) for item in value if isinstance(item, dict)]
        for key in ("list", "items"):
            value = resp.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
        return []

    async def _list_share_children(
        self,
        client,
        *,
        share_code: str,
        receive_code: str,
        cid: str | int = 0,
    ) -> list[dict[str, Any]]:
        try:
            resp = await run_115_write_request(
                client,
                "查询115分享文件",
                lambda write_client: write_client.share_snap_app({
                    "share_code": share_code,
                    "receive_code": receive_code,
                    "cid": cid,
                    "limit": 1000,
                    "offset": 0,
                }),
                raise_on_state_false=False,
            )
            return self._extract_share_items(resp)
        except Exception as e:
            logger.warning(f"[ForwardHDHive] 查询 115 分享文件失败 cid={cid}: {e}")
            return []

    async def _collect_share_video_candidates(
        self,
        client,
        *,
        share_code: str,
        receive_code: str,
        root_cid: str | int = 0,
        max_depth: int = 4,
        max_nodes: int = 600,
    ) -> list[dict[str, Any]]:
        queue: list[tuple[str | int, int, str]] = [(root_cid, 0, "")]
        videos: list[dict[str, Any]] = []
        visited = 0
        while queue and visited < max_nodes:
            cid, depth, path = queue.pop(0)
            visited += 1
            for child in await self._list_share_children(
                client,
                share_code=share_code,
                receive_code=receive_code,
                cid=cid,
            ):
                name = self._share_item_name(child)
                child_path = f"{path}/{name}" if path else name
                item_id = self._share_item_id(child)
                if self._share_item_is_dir(child):
                    if item_id and depth < max_depth:
                        queue.append((item_id, depth + 1, child_path))
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext in VIDEO_EXTS and item_id:
                    videos.append({
                        "id": item_id,
                        "name": name,
                        "path": child_path,
                        "size": self._share_item_size(child),
                        "depth": depth,
                    })
        return videos

    async def _select_share_file_id(
        self,
        client,
        share_url: str,
        *,
        media_type: str,
        season: int | None,
        episode: int | None,
    ) -> dict[str, Any] | None:
        try:
            payload = share_extract_payload(share_url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"115 分享链接解析失败: {e}") from e
        share_code = str(payload.get("share_code") or "")
        receive_code = str(payload.get("receive_code") or "")
        candidates = await self._collect_share_video_candidates(
            client,
            share_code=share_code,
            receive_code=receive_code,
        )
        if not candidates:
            return None

        is_tv = str(media_type or "").lower() in {"tv", "series"}

        def _rank(item: dict[str, Any]) -> tuple[int, int, int]:
            ep_score = self._episode_match_score(item.get("name", ""), season, episode) if is_tv else 0
            return (ep_score, int(item.get("size") or 0), -int(item.get("depth") or 0))

        candidates.sort(key=_rank, reverse=True)
        picked = candidates[0]
        if is_tv and episode and self._episode_match_score(picked.get("name", ""), season, episode) <= 0:
            raise HTTPException(status_code=404, detail=f"分享中未匹配到 S{season or 1:02d}E{episode:02d} 对应视频")
        return picked

    async def _get_instant_transfer_dir(self) -> str:
        cfg = await get_config_302()
        drives = cfg.get("drives", []) if isinstance(cfg, dict) else []
        if isinstance(drives, list) and drives:
            upload_dir = str(drives[0].get("upload_dir") or "").strip()
            if upload_dir:
                return upload_dir
        topology = cfg.get("standard_topology", {}) if isinstance(cfg, dict) else {}
        if isinstance(topology, dict):
            instant_dir = str(topology.get("instant_dir") or "").strip()
            if instant_dir:
                return instant_dir
        raise HTTPException(status_code=400, detail="未配置 115 秒传目录，请先在 302/115 配置中设置")

    async def _get_forward_target_dir(self) -> str | None:
        if bool(self.config.get("library_enabled", True)):
            return None
        return await self._get_instant_transfer_dir()

    def _get_organize_source_dir(self) -> str:
        if not MEDIA_ORGANIZE_CONFIG_PATH.exists():
            raise HTTPException(status_code=400, detail="未配置媒体整理目录，请先在媒体整理中选择网盘转存源目录")
        try:
            data = json.loads(MEDIA_ORGANIZE_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"媒体整理配置读取失败: {e}") from e
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="媒体整理配置异常，请重新保存媒体整理目录")
        source_cid = str(data.get("source_cid") or "").strip()
        if source_cid and source_cid != "0":
            return source_cid
        source_name = str(data.get("source_name") or "").strip()
        if source_name and source_name != "根目录":
            return source_name
        raise HTTPException(status_code=400, detail="未配置媒体整理的网盘转存源目录")

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

    async def _play_share_url(
        self,
        request: Request,
        *,
        share_url: str,
        media_type: str,
        tmdb_id: str,
        source_key: str,
        source_label: str,
        source_id: str,
        source_detail_mode: str,
        log_prefix: str,
        preferred_name: str = "",
        season: int | None = None,
        episode: int | None = None,
    ) -> str:
        target_dir = await self._get_forward_target_dir()
        client, target_cid, client_error = await transfer_service._get_transfer_context(target_dir=target_dir)
        if not client:
            raise HTTPException(status_code=400, detail=client_error or "115 转存客户端未就绪")

        transfer_mode = str(self.config.get("transfer_mode") or "series").strip().lower()
        if transfer_mode not in {"series", "episode"}:
            transfer_mode = "series"
        selected_share_item: dict[str, Any] | None = None
        share_file_id: str | None = None
        if transfer_mode == "episode":
            selected_share_item = await self._select_share_file_id(
                client,
                share_url,
                media_type=media_type,
                season=season,
                episode=episode,
            )
            if not selected_share_item or not selected_share_item.get("id"):
                raise HTTPException(status_code=404, detail="分享中未找到可转存的视频文件")
            share_file_id = str(selected_share_item["id"])
            logger.info(f"{log_prefix} 单集转存命中: {selected_share_item.get('path') or selected_share_item.get('name')} file_id={share_file_id}")

        transfer_result = await transfer_service.process_link(
            share_url,
            source=source_key,
            target_dir=target_dir,
            share_file_id=share_file_id,
            source_meta={
                "source_key": source_key,
                "source_label": source_label,
                "source_kind": "forward",
                "source_detail": f"{media_type}:{tmdb_id}:{'library' if self.config.get('library_enabled', True) else 'instant'}:{source_detail_mode}:{transfer_mode}",
                "source_id": source_id,
            },
        )
        already_transferred = self._is_already_transferred_message(transfer_result.get("message", ""))
        if not transfer_result.get("success") and not already_transferred:
            raise HTTPException(status_code=502, detail=transfer_result.get("message") or "115 转存失败")
        if already_transferred:
            logger.info(f"{log_prefix} 分享已在转存目录中，跳过重复转存: id={source_id}")

        picked = await self._locate_video_pickcode(
            client,
            target_cid,
            transfer_name=(
                selected_share_item.get("name", "")
                if selected_share_item
                else (transfer_result.get("name", "") if transfer_result.get("success") else preferred_name)
            ),
            season=season,
            episode=episode,
        )
        if not picked and already_transferred:
            alternate_target_dirs: list[str | None] = [None]
            try:
                instant_dir = await self._get_instant_transfer_dir()
                alternate_target_dirs.append(instant_dir)
            except HTTPException:
                pass
            seen_cids = {str(target_cid)}
            for alternate_dir in alternate_target_dirs:
                alt_client, alt_cid, _ = await transfer_service._get_transfer_context(target_dir=alternate_dir)
                if not alt_client or str(alt_cid) in seen_cids:
                    continue
                seen_cids.add(str(alt_cid))
                picked = await self._locate_video_pickcode(
                    alt_client,
                    alt_cid,
                    transfer_name=(
                        selected_share_item.get("name", "")
                        if selected_share_item
                        else (transfer_result.get("name", "") if transfer_result.get("success") else preferred_name)
                    ),
                    season=season,
                    episode=episode,
                )
                if picked:
                    client = alt_client
                    logger.info(f"{log_prefix} 重复转存资源在备用目录中命中: cid={alt_cid} name={picked.get('name')}")
                    break
        if not picked and already_transferred and transfer_mode == "series":
            selected_share_item = await self._select_share_file_id(
                client,
                share_url,
                media_type=media_type,
                season=season,
                episode=episode,
            )
            if selected_share_item and selected_share_item.get("id"):
                share_file_id = str(selected_share_item["id"])
                logger.info(f"{log_prefix} 整剧重复但目标目录缺失，降级接收当前单集: {selected_share_item.get('path') or selected_share_item.get('name')} file_id={share_file_id}")
                transfer_result = await transfer_service.process_link(
                    share_url,
                    source=source_key,
                    target_dir=target_dir,
                    share_file_id=share_file_id,
                    source_meta={
                        "source_key": source_key,
                        "source_label": source_label,
                        "source_kind": "forward",
                        "source_detail": f"{media_type}:{tmdb_id}:{'library' if self.config.get('library_enabled', True) else 'instant'}:{source_detail_mode}:series_fallback_episode",
                        "source_id": source_id,
                    },
                )
                if not transfer_result.get("success") and not self._is_already_transferred_message(transfer_result.get("message", "")):
                    raise HTTPException(status_code=502, detail=transfer_result.get("message") or "115 单集补转失败")
                picked = await self._locate_video_pickcode(
                    client,
                    target_cid,
                    transfer_name=selected_share_item.get("name", ""),
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
        logger.info(f"{log_prefix} 播放直链已生成: {picked.get('name')} id={source_id}")
        return direct_url

    async def _transfer_share_url_to_organize(
        self,
        *,
        share_url: str,
        media_type: str,
        tmdb_id: str,
        source_key: str,
        source_label: str,
        source_id: str,
        source_detail_mode: str,
        log_prefix: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        target_dir = self._get_organize_source_dir()
        client, target_cid, client_error = await transfer_service._get_transfer_context(target_dir=target_dir)
        if not client:
            raise HTTPException(status_code=400, detail=client_error or "115 转存客户端未就绪")

        transfer_mode = str(self.config.get("transfer_mode") or "series").strip().lower()
        if transfer_mode not in {"series", "episode"}:
            transfer_mode = "series"
        selected_share_item: dict[str, Any] | None = None
        share_file_id: str | None = None
        if transfer_mode == "episode":
            selected_share_item = await self._select_share_file_id(
                client,
                share_url,
                media_type=media_type,
                season=season,
                episode=episode,
            )
            if not selected_share_item or not selected_share_item.get("id"):
                raise HTTPException(status_code=404, detail="分享中未找到可转存的视频文件")
            share_file_id = str(selected_share_item["id"])
            logger.info(f"{log_prefix} 整理目录单集转存命中: {selected_share_item.get('path') or selected_share_item.get('name')} file_id={share_file_id}")

        transfer_result = await transfer_service.process_link(
            share_url,
            source=source_key,
            target_dir=target_dir,
            share_file_id=share_file_id,
            source_meta={
                "source_key": source_key,
                "source_label": source_label,
                "source_kind": "forward",
                "source_detail": f"{media_type}:{tmdb_id}:organize:{source_detail_mode}:{transfer_mode}",
                "source_id": source_id,
            },
        )
        already_transferred = self._is_already_transferred_message(transfer_result.get("message", ""))
        if not transfer_result.get("success") and not already_transferred:
            raise HTTPException(status_code=502, detail=transfer_result.get("message") or "115 转存失败")
        if already_transferred:
            transfer_result = dict(transfer_result)
            transfer_result["success"] = True
            transfer_result["status"] = "已在整理目录"
            transfer_result["message"] = "资源已在整理目录，无需重复转存"
        transfer_result["target_cid"] = str(transfer_result.get("target_cid") or target_cid)
        transfer_result["target_dir"] = str(target_dir)
        transfer_result["message"] = transfer_result.get("message") or "已转存到整理目录"
        logger.info(f"{log_prefix} 已转存到整理目录: id={source_id} cid={transfer_result.get('target_cid')}")
        return transfer_result

    async def play_resource(
        self,
        request: Request,
        *,
        slug: str,
        media_type: str,
        tmdb_id: str,
        season: int | None = None,
        episode: int | None = None,
        require_enabled: bool = True,
    ) -> str:
        if require_enabled and not self.config.get("enabled", True):
            raise HTTPException(status_code=403, detail="Forward 模块未启用")
        if require_enabled and not self.config.get("hdhive_enabled", True):
            raise HTTPException(status_code=403, detail="影巢资源源未启用")
        resources = self.fetch_resources(media_type, tmdb_id, require_enabled=require_enabled) if tmdb_id else []
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

        direct_url = await self._play_share_url(
            request,
            share_url=share_url,
            media_type=media_type,
            tmdb_id=tmdb_id,
            source_key="forward_hdhive",
            source_label="Forward 影巢",
            source_id=slug,
            source_detail_mode="hdhive",
            log_prefix="[ForwardHDHive]",
            season=season,
            episode=episode,
        )
        logger.info(f"[ForwardHDHive] 播放直链已生成: slug={slug}")
        return direct_url

    async def transfer_resource_to_organize(
        self,
        request: Request,
        *,
        slug: str,
        media_type: str,
        tmdb_id: str,
        season: int | None = None,
        episode: int | None = None,
        require_enabled: bool = True,
    ) -> dict[str, Any]:
        if require_enabled and not self.config.get("enabled", True):
            raise HTTPException(status_code=403, detail="Forward 模块未启用")
        if require_enabled and not self.config.get("hdhive_enabled", True):
            raise HTTPException(status_code=403, detail="影巢资源源未启用")
        resources = self.fetch_resources(media_type, tmdb_id, require_enabled=require_enabled) if tmdb_id else []
        resource = self._find_resource_by_slug(resources, slug) if resources else None
        if resource is not None:
            if not self._resource_allowed(resource):
                raise HTTPException(
                    status_code=403,
                    detail=f"资源需要 {self._resource_points(resource)} 积分，超过当前上限 {self.config.get('max_unlock_points')}",
                )
            if str(resource.get("pan_type") or "").lower() != "115":
                raise HTTPException(status_code=400, detail="当前仅支持 115 资源转存")

        unlock_data = self._unlock_resource(slug)
        share_url = self._extract_unlock_url(unlock_data, slug)
        if not share_url:
            raise HTTPException(status_code=502, detail="影巢解锁成功但未返回资源链接")

        return await self._transfer_share_url_to_organize(
            share_url=share_url,
            media_type=media_type,
            tmdb_id=tmdb_id,
            source_key="forward_hdhive",
            source_label="Forward 影巢",
            source_id=slug,
            source_detail_mode="hdhive",
            log_prefix="[ForwardHDHive]",
            season=season,
            episode=episode,
        )

    async def play_aiying_resource(
        self,
        request: Request,
        *,
        resource_id: str,
        media_type: str,
        tmdb_id: str,
        season: int | None = None,
        episode: int | None = None,
        require_enabled: bool = True,
    ) -> str:
        if require_enabled and not self.config.get("enabled", True):
            raise HTTPException(status_code=403, detail="Forward 模块未启用")
        if not self._aiying_configured(require_enabled=require_enabled):
            raise HTTPException(status_code=403, detail="爱影未启用或未配置 Token/TG ID")

        now = time.time()
        cached = self._aiying_play_cache.get(resource_id)
        item = cached[1] if cached and cached[0] > now else None
        if item is None and tmdb_id:
            resources = await self.fetch_aiying_resources(media_type, tmdb_id, require_enabled=require_enabled)
            for candidate in self.filter_aiying_resources(resources, media_type=media_type, season=season, episode=episode):
                candidate_id = self._aiying_resource_id(candidate)
                self._aiying_play_cache[candidate_id] = (now + 21600, dict(candidate))
                if candidate_id == resource_id:
                    item = candidate
                    break
        if not item:
            raise HTTPException(status_code=404, detail="爱影资源已过期，请返回详情页重新查询")

        share_url = str(item.get("link") or "").strip()
        if not share_url:
            raise HTTPException(status_code=502, detail="爱影资源未返回 115 分享链接")
        direct_url = await self._play_share_url(
            request,
            share_url=share_url,
            media_type=media_type,
            tmdb_id=tmdb_id,
            source_key="forward_aiying",
            source_label="Forward 爱影",
            source_id=resource_id,
            source_detail_mode="aiying",
            log_prefix="[Forward爱影]",
            preferred_name=str(item.get("name") or ""),
            season=season,
            episode=episode,
        )
        logger.info(f"[Forward爱影] 播放直链已生成: {item.get('name')} id={resource_id}")
        return direct_url

    async def transfer_aiying_resource_to_organize(
        self,
        request: Request,
        *,
        resource_id: str,
        media_type: str,
        tmdb_id: str,
        season: int | None = None,
        episode: int | None = None,
        require_enabled: bool = True,
    ) -> dict[str, Any]:
        if require_enabled and not self.config.get("enabled", True):
            raise HTTPException(status_code=403, detail="Forward 模块未启用")
        if not self._aiying_configured(require_enabled=require_enabled):
            raise HTTPException(status_code=403, detail="爱影未启用或未配置 Token/TG ID")

        now = time.time()
        cached = self._aiying_play_cache.get(resource_id)
        item = cached[1] if cached and cached[0] > now else None
        if item is None and tmdb_id:
            resources = await self.fetch_aiying_resources(media_type, tmdb_id, require_enabled=require_enabled)
            for candidate in self.filter_aiying_resources(resources, media_type=media_type, season=season, episode=episode):
                candidate_id = self._aiying_resource_id(candidate)
                self._aiying_play_cache[candidate_id] = (now + 21600, dict(candidate))
                if candidate_id == resource_id:
                    item = candidate
                    break
        if not item:
            raise HTTPException(status_code=404, detail="爱影资源已过期，请返回详情页重新查询")

        share_url = str(item.get("link") or "").strip()
        if not share_url:
            raise HTTPException(status_code=502, detail="爱影资源未返回 115 分享链接")
        return await self._transfer_share_url_to_organize(
            share_url=share_url,
            media_type=media_type,
            tmdb_id=tmdb_id,
            source_key="forward_aiying",
            source_label="Forward 爱影",
            source_id=resource_id,
            source_detail_mode="aiying",
            log_prefix="[Forward爱影]",
            season=season,
            episode=episode,
        )


forward_hdhive_service = ForwardHDHiveService()
