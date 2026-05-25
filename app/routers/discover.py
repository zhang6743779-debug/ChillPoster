# app/routers/discover.py
# 发现推荐页 API — 对接 TMDB / 豆瓣 / 扩展数据源

import os
import re
import sys
import time
import types
import json
import asyncio
import hashlib
import logging
import random
import queue
import importlib.util
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, Dict, List, Any

import httpx
import requests
from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import Response, RedirectResponse, StreamingResponse

from core.configs import global_config, MISSING_EPISODE_STATS_CACHE_FILE, RSS_TASKS_FILE
from core.cache_db import cache_db
from app.services.emby_library_cache import (
    build_discover_index,
    discover_tmdb_id_exists,
    get_discover_index_meta,
    get_discover_index_ready,
    get_discover_series_entries,
    get_discover_series_status,
    lookup_discover_tmdb_id,
)
from app.services.realtime_events import format_sse_event, publish_realtime_event, subscribe_realtime_events
from core import tmdb
from core.douban import DoubanApi

logger = logging.getLogger("Discover")
router = APIRouter(prefix="/api/discover", tags=["Discover"])


@router.get("/events")
async def discover_realtime_events():
    async def event_generator():
        with subscribe_realtime_events() as q:
            yield "event: init\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.to_thread(q.get, True, 25)
                    yield format_sse_event(event)
                except queue.Empty:
                    yield ": ping\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

# ========== 内存缓存 ==========
_cache: Dict[str, tuple] = {}  # key -> (data, expiry_timestamp)
CACHE_TTL = 1800  # 30 分钟

def _cache_get(key: str):
    if key in _cache:
        data, expires = _cache[key]
        if time.time() < expires:
            return data
        del _cache[key]
    return None

def _cache_set(key: str, data, ttl: int = CACHE_TTL):
    _cache[key] = (data, time.time() + ttl)

_LIBRARY_TITLE_NOISE_RE = re.compile(r"(?i)(tmdb(?:id)?[-=: ]*\d+|tmdb-\d+|imdb[-=: ]*tt\d+|douban[-=: ]*\d+|S\d{1,2}E\d{1,3}|S\d{1,2}(?:\b|[^A-Za-z0-9])|Season\s*\d+|第\s*\d+\s*季|第\s*[一二三四五六七八九十百两0-9]+\s*季|[一二三四五六七八九十百两0-9]+\s*季|粤语|国语|普通话|闽南语|台语|英语|日语|韩语|泰语|原声|中字|字幕|配音|高清|超清|蓝光|\d{3,4}p|BluRay|WEB[-_. ]?DL|WEBRip|HDTV|REMUX|x264|x265|H\.?264|H\.?265|HEVC|AAC|DDP?\d?(?:\.\d)?|Atmos|NF|AMZN|BILI|TX|Tencent)")

# ========== 工具函数 ==========

def _normalize_discover_media_type(value: Any) -> str:
    value = str(value or "").strip().lower()
    if value in {"tv", "series", "show", "电视剧", "剧集", "番剧", "动漫", "动画", "综艺", "纪录片", "少儿"}:
        return "tv"
    return "movie"


def _normalize_library_title(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _LIBRARY_TITLE_NOISE_RE.sub(" ", text)
    text = re.sub(r"[\[【(（]\s*(?:粤语|国语|普通话|闽南语|台语|英语|日语|韩语|泰语|原声|中字|字幕|配音|高清|超清|蓝光)\s*[\]】)）]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）", " ", text)
    text = re.sub(r"[._\-+/\\:：·,，。!！?？'\"“”‘’~]+", " ", text)
    text = re.sub(r"\s+", "", text).lower()
    return text


def _chinese_number_to_int(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    digit_map = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digit_map.get(left, 1) if left else 1
        ones = digit_map.get(right, 0) if right else 0
        return tens * 10 + ones
    return digit_map.get(text)


def _extract_season_from_title(value: Any) -> tuple[str, int | None]:
    text = str(value or "").strip()
    if not text:
        return "", None
    patterns = (
        r"(?:第\s*)?([一二三四五六七八九十两0-9]+)\s*季",
        r"(?i)Season\s*(\d{1,2})",
        r"(?i)(?:^|[\s._\-\[(（])S(\d{1,2})(?:$|[\s._\-\])）])",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        season = _chinese_number_to_int(match.group(1))
        title = (text[:match.start()] + " " + text[match.end():]).strip()
        return title, season
    return text, None


def _item_exists_in_discover_index(item: dict) -> bool:
    media_type = _normalize_discover_media_type(item.get("media_type"))
    tmdb_id = item.get("_tmdb_id") or item.get("tmdb_id")
    if not tmdb_id and item.get("source") in {"tmdb", "themoviedb"}:
        tmdb_id = item.get("id")
    if discover_tmdb_id_exists(tmdb_id, media_type):
        return True
    raw_title = item.get("title") or item.get("name") or ""
    title, _ = _extract_season_from_title(raw_title)
    title = title or raw_title
    year = str(item.get("year") or "")[:4]
    return bool(lookup_discover_tmdb_id(title, year, media_type))


def _mark_library_exists_on_items(items: list[dict]):
    if not items:
        return
    for item in items:
        if isinstance(item, dict):
            item["exists_in_library"] = _item_exists_in_discover_index(item)


# 内置 TMDB API Key（MoviePilot 同款默认 key，用户可在设置中覆盖）
_BUILTIN_TMDB_KEY = "db55323b8d3e4154498498a75642b381"

def _get_proxy_url() -> str:
    """获取用户配置的代理地址"""
    global_config.load()
    return global_config.proxy_url or ""

def _get_tmdb_key() -> str:
    global_config.load()
    return global_config.tmdb_key or _BUILTIN_TMDB_KEY

_douban_api_instance = None

def _get_douban_api():
    global _douban_api_instance
    if _douban_api_instance is None:
        # 豆瓣不走代理，直连即可
        _douban_api_instance = DoubanApi(proxies=None)
    return _douban_api_instance

def _normalize_tmdb_item(item: dict, source_key: str = "tmdb") -> dict:
    """统一 TMDB 数据格式"""
    poster_path = item.get("poster_path")
    # 通过后端代理转发 TMDB 图片，解决国内无法直接访问 image.tmdb.org 的问题
    poster_url = f"/api/discover/tmdb_img?path={poster_path}" if poster_path else ""
    backdrop = item.get("backdrop_path")
    backdrop_url = f"/api/discover/tmdb_img?path={backdrop}" if backdrop else ""
    media_type = item.get("media_type") or ("movie" if item.get("title") else "tv")
    tmdb_id = item.get("id")
    return {
        "id": tmdb_id,
        "_tmdb_id": tmdb_id,
        "title": item.get("title") or item.get("name", ""),
        "original_title": item.get("original_title") or item.get("original_name", ""),
        "year": (item.get("release_date") or item.get("first_air_date", ""))[:4],
        "poster_url": poster_url,
        "backdrop_url": backdrop_url,
        "rating": item.get("vote_average"),
        "overview": item.get("overview", ""),
        "media_type": media_type,
        "genre_ids": item.get("genre_ids", []),
        "source": source_key,
        "subscribed": False,
        "exists_in_library": False,
    }


def _normalize_tmdb_detail(data: dict, media_type: str, tmdb_id: int) -> dict:
    """统一 TMDB 详情字段，给前端提供稳定出口。"""
    detail = dict(data or {})
    external_ids = detail.get("external_ids") or {}
    if not isinstance(external_ids, dict):
        external_ids = {}

    poster_path = detail.get("poster_path")
    backdrop_path = detail.get("backdrop_path")
    detail["tmdb_id"] = detail.get("tmdb_id") or detail.get("id") or tmdb_id
    detail["media_type"] = detail.get("media_type") or media_type or ("movie" if detail.get("title") else "tv")
    detail["title"] = detail.get("title") or detail.get("name") or ""
    detail["original_title"] = detail.get("original_title") or detail.get("original_name") or ""
    detail["year"] = (detail.get("release_date") or detail.get("first_air_date") or "")[:4]
    detail["poster_url"] = detail.get("poster_url") or (f"/api/discover/tmdb_img?path={poster_path}" if poster_path else "")
    detail["backdrop_url"] = detail.get("backdrop_url") or (f"/api/discover/tmdb_img?path={backdrop_path}" if backdrop_path else "")
    detail["imdb_id"] = detail.get("imdb_id") or external_ids.get("imdb_id") or ""
    detail["tvdb_id"] = detail.get("tvdb_id") or external_ids.get("tvdb_id") or ""
    detail["external_ids"] = {
        **external_ids,
        "imdb_id": detail["imdb_id"],
        "tvdb_id": detail["tvdb_id"],
    }
    return detail


def _normalize_douban_item(item: dict) -> dict | None:
    """统一豆瓣数据格式"""
    # 豆瓣 collection items 结构: { subject: { id, title, cover: {url}, rating: {value}, ... } }
    subject = item.get("subject", item)
    subject_type = subject.get("type")
    if subject_type not in {"movie", "tv"}:
        return None

    # 豆瓣图片 URL：兼容 cover.url (新版) 和 pic.large/pic.normal (旧版)
    cover = ""
    cover_data = subject.get("cover", {})
    if isinstance(cover_data, dict):
        cover = cover_data.get("url", "")
    elif isinstance(cover_data, str):
        cover = cover_data
    if not cover:
        pic = subject.get("pic", {})
        if isinstance(pic, dict):
            cover = pic.get("large", pic.get("normal", ""))
        elif isinstance(pic, str):
            cover = pic

    rating_info = subject.get("rating", {})
    rating_val = None
    if isinstance(rating_info, dict):
        rating_val = rating_info.get("value")
    elif isinstance(rating_info, (int, float)):
        rating_val = rating_info

    _DOUBAN_PLACEHOLDER = {"movie_large.jpg", "tv_normal.png", "tv_normal.jpg", "tv_large.jpg"}
    if not cover or any(p in cover for p in _DOUBAN_PLACEHOLDER):
        return None

    # 豆瓣图片需要通过后端代理，否则浏览器直接访问会被防盗链拦截
    poster_url = f"/api/discover/douban_img?url={cover}" if cover else ""

    return {
        "id": subject.get("id"),
        "title": subject.get("title", ""),
        "original_title": subject.get("original_title", ""),
        "year": subject.get("year", ""),
        "poster_url": poster_url,
        "backdrop_url": "",
        "rating": rating_val,
        "overview": subject.get("card_subtitle", "") or subject.get("intro", ""),
        "media_type": "tv" if subject_type == "tv" else "movie",
        "genre_ids": [],
        "source": "douban",
        "subscribed": False,
        "exists_in_library": False,
    }


class _RefMediaInfo:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _RefDiscoverMediaSource:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _RefLogger:
    def error(self, *args, **kwargs):
        logger.error(*args, **kwargs)

    def warning(self, *args, **kwargs):
        message = " ".join(str(arg) for arg in args)
        if "芒果TV UI初始化异常" in message or "获取芒果TV UI失败" in message:
            logger.info(*args, **kwargs)
            return
        logger.warning(*args, **kwargs)

    def info(self, *args, **kwargs):
        logger.info(*args, **kwargs)


class _RefSettings:
    API_TOKEN = "local"
    SECURITY_IMAGE_DOMAINS: list[str] = []


class _RefRequestUtils:
    def __init__(self, headers=None):
        self.headers = headers or {}

    def get_res(self, url, params=None):
        proxy_url = _get_proxy_url()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        try:
            return requests.get(url, params=params, headers=self.headers, timeout=20, proxies=proxies)
        except requests.RequestException as proxy_error:
            if not proxies:
                raise
            logger.warning(f"[discover] 插件代理请求失败，回退直连: {proxy_error}")
            return requests.get(url, params=params, headers=self.headers, timeout=20)


def _load_reference_module(module_name: str):
    ref_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "discover_plugins")
    file_path = os.path.join(ref_dir, f"{module_name}.py")
    if not os.path.exists(file_path):
        file_path = file_path + "c"
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)

    app_mod = types.ModuleType("app")
    schemas_mod = types.ModuleType("app.schemas")
    core_mod = types.ModuleType("app.core")
    config_mod = types.ModuleType("app.core.config")
    log_mod = types.ModuleType("app.log")
    utils_mod = types.ModuleType("app.utils")
    http_mod = types.ModuleType("app.utils.http")

    schemas_mod.MediaInfo = _RefMediaInfo
    schemas_mod.DiscoverMediaSource = _RefDiscoverMediaSource
    config_mod.settings = _RefSettings()
    log_mod.logger = _RefLogger()
    http_mod.RequestUtils = _RefRequestUtils

    previous = {name: sys.modules.get(name) for name in [
        "app", "app.schemas", "app.core", "app.core.config", "app.log", "app.utils", "app.utils.http"
    ]}
    try:
        sys.modules["app"] = app_mod
        sys.modules["app.schemas"] = schemas_mod
        sys.modules["app.core"] = core_mod
        sys.modules["app.core.config"] = config_mod
        sys.modules["app.log"] = log_mod
        sys.modules["app.utils"] = utils_mod
        sys.modules["app.utils.http"] = http_mod

        spec = importlib.util.spec_from_file_location(f"discover_ref_{module_name}", file_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def _extract_ref_source(module_name: str) -> dict:
    module = _load_reference_module(module_name)
    event = types.SimpleNamespace(extra_sources=[])
    module.discover_source(None, event)
    if not event.extra_sources:
        raise RuntimeError(f"参考模块 {module_name} 未暴露数据源")
    source = event.extra_sources[0]
    return {
        "module_name": module_name,
        "name": getattr(source, "name", module_name),
        "key": getattr(source, "mediaid_prefix", module_name),
        "filter_params": getattr(source, "filter_params", {}) or {},
        "filter_ui": getattr(source, "filter_ui", []) or [],
        "depends": getattr(source, "depends", {}) or {},
        "module": module,
    }


def _safe_eval_show(expr: str, filters: dict) -> bool:
    if not expr:
        return True
    expr = expr.strip()
    if expr.startswith("{{") and expr.endswith("}}"):
        expr = expr[2:-2].strip()
    if not expr:
        return True
    expr = expr.replace("||", " or ").replace("&&", " and ")
    allowed_names = {k: v for k, v in filters.items()}
    try:
        return bool(eval(expr, {"__builtins__": {}}, allowed_names))
    except Exception:
        return True


def _normalize_filter_model(source_key: str, model: str, label_text: str = "") -> str:
    model = (model or "").strip()
    if source_key == "bilibili" and model == "copyright":
        return "_copyright"
    return model



def _append_missing_schema_rows(source_key: str, schema: list[dict], filter_params: dict, depends: dict):
    existing_keys = {row["key"] for row in schema}
    if source_key == "migudiscover" and "payType" not in existing_keys and "payType" in (filter_params or {}):
        schema.insert(4, {
            "key": "payType",
            "label": "资费",
            "control": "chips",
            "options": [
                {"label": "免费", "value": "0"},
                {"label": "付费", "value": "1"},
            ],
            "default": "" if filter_params.get("payType") is None else str(filter_params.get("payType")),
            "show": "",
            "depends_on": depends.get("payType", []),
        })



def _normalize_show_expr(expr: str) -> str:
    expr = (expr or "").strip()
    if expr.startswith("{{") and expr.endswith("}}"):
        expr = expr[2:-2].strip()
    return expr



def _merge_show_expr(existing: str, new: str) -> str:
    existing_expr = _normalize_show_expr(existing)
    new_expr = _normalize_show_expr(new)
    if not existing_expr or not new_expr:
        merged = existing_expr or new_expr
    elif existing_expr == new_expr:
        merged = existing_expr
    else:
        parts = []
        for expr in (existing_expr, new_expr):
            if expr and expr not in parts:
                parts.append(expr)
        merged = " || ".join(parts)
    return f"{{{{{merged}}}}}" if merged else ""



def _extract_show_parent_values(show_expr: str, depends_on: list[str]) -> list[str]:
    if len(depends_on) != 1:
        return []
    parent_key = depends_on[0]
    expr = _normalize_show_expr(show_expr)
    if not expr:
        return []
    matches = re.findall(rf"{re.escape(parent_key)}\s*==\s*['\"]([^'\"]+)['\"]", expr)
    values = []
    for value in matches:
        if value not in values:
            values.append(value)
    return values



def _append_label_variant(row: dict, label_text: str, show_expr: str, parent_values: list[str]):
    variants = row.setdefault("label_variants", [])
    signature = (label_text, show_expr or "", tuple(parent_values or []))
    existing = {
        (variant.get("label"), variant.get("show", ""), tuple(variant.get("parent_values") or []))
        for variant in variants
    }
    if signature not in existing:
        variant = {"label": label_text, "show": show_expr or ""}
        if parent_values:
            variant["parent_values"] = parent_values
        variants.append(variant)



def _normalize_filter_schema(filter_ui: list, filter_params: dict, depends: dict, source_key: str = "") -> list:
    schema = []
    index_by_key: dict[str, int] = {}
    option_signatures: dict[str, set[tuple]] = {}
    for row in filter_ui or []:
        content = row.get("content") or []
        if len(content) < 2:
            continue
        label_block = content[0]
        group_block = content[1]
        label_content = label_block.get("content") or []
        label_text = "筛选"
        if label_content:
            label_text = label_content[0].get("text") or label_text

        raw_model = ((group_block.get("props") or {}).get("model") or "").strip()
        model = _normalize_filter_model(source_key, raw_model, label_text)
        if not model:
            continue

        show_expr = (row.get("props") or {}).get("show", "")
        depends_on = depends.get(model, [])
        parent_values = _extract_show_parent_values(show_expr, depends_on)

        options = []
        for option in group_block.get("content") or []:
            props = option.get("props") or {}
            value = props.get("value")
            option_data = {
                "label": option.get("text") or str(value or "全部"),
                "value": "" if value is None else str(value),
            }
            if parent_values:
                option_data["parent_values"] = parent_values
            options.append(option_data)

        if model in index_by_key:
            existing = schema[index_by_key[model]]
            existing["show"] = _merge_show_expr(existing.get("show", ""), show_expr)
            _append_label_variant(existing, label_text, show_expr, parent_values)
            signatures = option_signatures[model]
            for option in options:
                signature = (
                    option.get("label"),
                    option.get("value"),
                    tuple(option.get("parent_values") or []),
                )
                if signature in signatures:
                    continue
                signatures.add(signature)
                existing["options"].append(option)
            continue

        schema.append({
            "key": model,
            "label": label_text,
            "control": "chips",
            "options": options,
            "default": "" if filter_params.get(model) is None else str(filter_params.get(model)),
            "show": show_expr,
            "depends_on": depends_on,
            "label_variants": [{
                "label": label_text,
                "show": show_expr or "",
                **({"parent_values": parent_values} if parent_values else {}),
            }],
        })
        index_by_key[model] = len(schema) - 1
        option_signatures[model] = {
            (option.get("label"), option.get("value"), tuple(option.get("parent_values") or []))
            for option in options
        }
    _append_missing_schema_rows(source_key, schema, filter_params, depends)
    return schema


def _normalize_provider_item(item: Any, source_key: str) -> dict:
    raw = item if isinstance(item, dict) else getattr(item, "__dict__", {})
    media_id = raw.get("media_id") or raw.get("id") or raw.get("tmdb_id") or raw.get("bangumi_id") or raw.get("title")
    title = raw.get("title") or raw.get("name") or raw.get("name_cn") or ""
    poster_url = raw.get("poster_url") or raw.get("poster_path") or raw.get("image") or ""
    if not poster_url:
        images = raw.get("images") or {}
        if isinstance(images, dict):
            poster_url = images.get("large") or images.get("common") or images.get("medium") or images.get("grid") or images.get("small") or ""
    if source_key == "bilibili" and isinstance(poster_url, str) and poster_url:
        if poster_url.startswith("http://"):
            poster_url = "https://" + poster_url[len("http://"):]
        poster_url = f"/api/discover/bili_img?url={poster_url}"
    elif source_key in {"bangumi", "bangumidaily"} and isinstance(poster_url, str) and poster_url:
        if poster_url.startswith("http://"):
            poster_url = "https://" + poster_url[len("http://"):]
        poster_url = f"/api/discover/bangumi_img?url={poster_url}"
    raw_type = raw.get("media_type") or raw.get("type") or ""
    media_type = "tv"
    if raw_type in ("movie", "电影"):
        media_type = "movie"
    elif raw_type in ("tv", "电视剧", "剧集", "番剧", "动画片", "综艺", "纪录片", "特别节目", "少儿", "教育", "纪实", "动漫"):
        media_type = "tv"
    elif source_key in {"bangumidaily", "bangumi"}:
        media_type = "tv"

    rating = raw.get("vote_average")
    if rating in (None, ""):
        rating_info = raw.get("rating") or {}
        if isinstance(rating_info, dict):
            rating = rating_info.get("score")
    try:
        rating = float(rating) if rating not in (None, "") else None
    except Exception:
        rating = None

    year = raw.get("year") or ""
    if not year:
        year = str(raw.get("first_air_date") or raw.get("release_date") or raw.get("date") or "")[:4]

    return {
        "id": str(media_id),
        "title": title,
        "original_title": raw.get("original_title") or raw.get("name") or title,
        "year": str(year or ""),
        "poster_url": poster_url,
        "backdrop_url": "",
        "rating": rating,
        "overview": raw.get("overview") or raw.get("summary") or raw.get("description") or "",
        "media_type": media_type,
        "genre_ids": [],
        "source": source_key,
        "subscribed": False,
        "exists_in_library": False,
        "_source_key": source_key,
        "_source_media_id": str(media_id),
        "_source_media_type": media_type,
        "_raw_title_year": raw.get("title_year") or title,
    }


def _normalize_source_filter_value(source_key: str, key: str, value: Any):
    if source_key == "bilibili" and key == "mtype" and value == "guo":
        return "guochuang"
    return value



def _provider_filter_value(source_key: str, key: str, value: Any):
    if source_key == "bilibili" and key == "mtype" and value == "guochuang":
        return "guo"
    return value



def _normalize_source_filter_params(source_key: str, filter_params: dict) -> dict:
    normalized = {}
    for key, value in (filter_params or {}).items():
        normalized_key = _normalize_filter_model(source_key, key)
        normalized[normalized_key] = _normalize_source_filter_value(source_key, normalized_key, value)
    return normalized



def _normalize_source_depends(source_key: str, depends: dict) -> dict:
    normalized = {}
    for key, parent_keys in (depends or {}).items():
        normalized_key = _normalize_filter_model(source_key, key)
        normalized[normalized_key] = [
            _normalize_filter_model(source_key, parent_key)
            for parent_key in (parent_keys or [])
        ]
    return normalized



def _get_reference_sources() -> list[dict]:
    cache_key = "discover_reference_sources_v3"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    modules = ["tencentvideo", "bilibili", "cctv", "mangguo", "bangumidaily", "migu"]
    providers = []
    for module_name in modules:
        ref = _extract_ref_source(module_name)
        source_key = ref["key"]
        filter_params = _normalize_source_filter_params(source_key, ref["filter_params"])
        depends = _normalize_source_depends(source_key, ref["depends"])
        filter_schema = _dedupe_schema_options(_normalize_filter_schema(ref["filter_ui"], filter_params, depends, source_key))
        if source_key == "mangguodiscover" and not filter_schema:
            filter_schema = _get_mangguo_fallback_schema()
            logger.warning("芒果TV UI 初始化失败，已回退到内置筛选配置")
        providers.append({
            "key": source_key,
            "name": ref["name"],
            "module_name": module_name,
            "filter_params": filter_params,
            "depends": depends,
            "filter_schema": filter_schema,
            "module": ref["module"],
        })
    _cache_set(cache_key, providers, ttl=3600)
    return providers


def _dedupe_schema_options(schema: list[dict]) -> list[dict]:
    deduped_schema = []
    for row in schema or []:
        options = row.get("options") or []
        seen = set()
        deduped_options = []
        for option in options:
            parent_values = tuple(option.get("parent_values") or [])
            signature = (str(option.get("label", "")), str(option.get("value", "")), parent_values)
            if signature in seen:
                continue
            seen.add(signature)
            deduped_options.append(option)
        deduped_row = dict(row)
        deduped_row["options"] = deduped_options
        deduped_schema.append(deduped_row)
    return deduped_schema


def _get_mangguo_fallback_schema() -> list[dict]:
    return [
        {
            "key": "mtype",
            "label": "种类",
            "control": "chips",
            "default": "电视剧",
            "options": [
                {"label": "全部", "value": ""},
                {"label": "电视剧", "value": "电视剧"},
                {"label": "电影", "value": "电影"},
                {"label": "动漫", "value": "动漫"},
                {"label": "少儿", "value": "少儿"},
                {"label": "综艺", "value": "综艺"},
                {"label": "纪录片", "value": "纪录片"},
                {"label": "教育", "value": "教育"},
            ],
            "show": "",
            "depends_on": [],
        },
        {
            "key": "chargeInfo",
            "label": "资费",
            "control": "chips",
            "default": "",
            "options": [
                {"label": "全部", "value": ""},
                {"label": "免费", "value": "免费", "parent_values": ["电视剧", "综艺", "少儿", "纪录片"]},
                {"label": "付费", "value": "付费", "parent_values": ["电视剧", "综艺", "少儿", "纪录片"]},
                {"label": "会员", "value": "会员", "parent_values": ["电视剧", "电影", "动漫", "少儿", "综艺", "纪录片", "教育"]},
            ],
            "show": "",
            "depends_on": ["mtype"],
        },
        {
            "key": "sort",
            "label": "排序",
            "control": "chips",
            "default": "",
            "options": [
                {"label": "全部", "value": ""},
                {"label": "最新", "value": "2"},
                {"label": "最热", "value": "1"},
                {"label": "好评", "value": "3"},
            ],
            "show": "",
            "depends_on": ["mtype"],
        },
        {
            "key": "year",
            "label": "年份",
            "control": "chips",
            "default": "",
            "options": [{"label": "全部", "value": ""}],
            "show": "",
            "depends_on": ["mtype"],
        },
    ]


def _get_builtin_sources() -> list[dict]:
    return [
        {
            "key": "themoviedb",
            "name": "TheMovieDb",
            "builtin": True,
            "filter_params": {
                "media_type": "movie",
                "sort_by": "popularity.desc",
                "with_genres": "",
                "with_original_language": "",
                "with_keywords": "",
                "with_watch_providers": "",
                "vote_average": "0",
                "release_date": "",
            },
            "depends": {},
            "filter_schema": [
                {
                    "key": "media_type",
                    "label": "类型",
                    "control": "chips",
                    "default": "movie",
                    "options": [
                        {"label": "电影", "value": "movie"},
                        {"label": "电视剧", "value": "tv"},
                    ],
                    "show": "",
                    "depends_on": [],
                },
                {
                    "key": "sort_by",
                    "label": "排序",
                    "control": "chips",
                    "default": "popularity.desc",
                    "options": [
                        {"label": "热度降序", "value": "popularity.desc"},
                        {"label": "热度升序", "value": "popularity.asc"},
                        {"label": "评分降序", "value": "vote_average.desc"},
                        {"label": "评分升序", "value": "vote_average.asc"},
                        {"label": "上映日期降序", "value": "release_date.desc", "media_type": "movie"},
                        {"label": "上映日期升序", "value": "release_date.asc", "media_type": "movie"},
                        {"label": "首播日期降序", "value": "first_air_date.desc", "media_type": "tv"},
                        {"label": "首播日期升序", "value": "first_air_date.asc", "media_type": "tv"},
                    ],
                    "show": "",
                    "depends_on": ["media_type"],
                },
                {
                    "key": "with_genres",
                    "label": "风格",
                    "control": "chips",
                    "default": "",
                    "options": [],
                    "show": "",
                    "depends_on": ["media_type"],
                },
                {
                    "key": "with_original_language",
                    "label": "语言",
                    "control": "chips",
                    "default": "",
                    "options": [
                        {"label": "中文", "value": "zh"},
                        {"label": "英语", "value": "en"},
                        {"label": "日语", "value": "ja"},
                        {"label": "韩语", "value": "ko"},
                        {"label": "法语", "value": "fr"},
                        {"label": "德语", "value": "de"},
                        {"label": "西班牙语", "value": "es"},
                        {"label": "意大利语", "value": "it"},
                        {"label": "俄语", "value": "ru"},
                        {"label": "葡萄牙语", "value": "pt"},
                        {"label": "阿拉伯语", "value": "ar"},
                        {"label": "印地语", "value": "hi"},
                        {"label": "泰语", "value": "th"},
                    ],
                    "show": "",
                    "depends_on": [],
                },
                {
                    "key": "vote_average",
                    "label": "评分",
                    "control": "number",
                    "default": "0",
                    "min": 0,
                    "max": 10,
                    "step": 1,
                    "show": "",
                    "depends_on": [],
                    "options": [],
                },
            ],
        },
        {
            "key": "douban",
            "name": "豆瓣",
            "builtin": True,
            "filter_params": {
                "media_type": "movie",
                "sort": "U",
                "style": "",
                "region": "",
                "year": "",
            },
            "depends": {},
            "filter_schema": [
                {
                    "key": "media_type",
                    "label": "类型",
                    "control": "chips",
                    "default": "movie",
                    "options": [
                        {"label": "电影", "value": "movie"},
                        {"label": "电视剧", "value": "tv"},
                    ],
                    "show": "",
                    "depends_on": [],
                },
                {
                    "key": "sort",
                    "label": "排序",
                    "control": "chips",
                    "default": "U",
                    "options": [
                        {"label": "综合排序", "value": "U"},
                        {"label": "上映时间", "value": "R"},
                        {"label": "近期热度", "value": "T"},
                        {"label": "高分优先", "value": "S"},
                    ],
                    "show": "",
                    "depends_on": [],
                },
                {
                    "key": "style",
                    "label": "风格",
                    "control": "chips",
                    "default": "",
                    "options": [
                        {"label": "喜剧", "value": "喜剧"},
                        {"label": "爱情", "value": "爱情"},
                        {"label": "悬疑", "value": "悬疑"},
                        {"label": "动画", "value": "动画"},
                        {"label": "武侠", "value": "武侠"},
                        {"label": "古装", "value": "古装"},
                        {"label": "家庭", "value": "家庭"},
                        {"label": "犯罪", "value": "犯罪"},
                        {"label": "科幻", "value": "科幻"},
                        {"label": "恐怖", "value": "恐怖"},
                        {"label": "历史", "value": "历史"},
                        {"label": "战争", "value": "战争"},
                        {"label": "动作", "value": "动作"},
                        {"label": "冒险", "value": "冒险"},
                        {"label": "传记", "value": "传记"},
                        {"label": "剧情", "value": "剧情"},
                        {"label": "奇幻", "value": "奇幻"},
                        {"label": "惊悚", "value": "惊悚"},
                        {"label": "灾难", "value": "灾难"},
                        {"label": "歌舞", "value": "歌舞"},
                        {"label": "音乐", "value": "音乐"},
                    ],
                    "show": "",
                    "depends_on": [],
                },
                {
                    "key": "region",
                    "label": "地区",
                    "control": "chips",
                    "default": "",
                    "options": [
                        {"label": "华语", "value": "华语"},
                        {"label": "欧美", "value": "欧美"},
                        {"label": "韩国", "value": "韩国"},
                        {"label": "日本", "value": "日本"},
                        {"label": "中国大陆", "value": "中国大陆"},
                        {"label": "美国", "value": "美国"},
                        {"label": "中国香港", "value": "中国香港"},
                        {"label": "中国台湾", "value": "中国台湾"},
                        {"label": "英国", "value": "英国"},
                        {"label": "法国", "value": "法国"},
                        {"label": "德国", "value": "德国"},
                        {"label": "意大利", "value": "意大利"},
                        {"label": "西班牙", "value": "西班牙"},
                        {"label": "印度", "value": "印度"},
                        {"label": "泰国", "value": "泰国"},
                    ],
                    "show": "",
                    "depends_on": [],
                },
                {
                    "key": "year",
                    "label": "年代",
                    "control": "chips",
                    "default": "",
                    "options": [
                        {"label": "2026", "value": "2026"},
                        {"label": "2025", "value": "2025"},
                        {"label": "2024", "value": "2024"},
                        {"label": "2023", "value": "2023"},
                        {"label": "2022", "value": "2022"},
                        {"label": "2021", "value": "2021"},
                        {"label": "2020年代", "value": "2020年代"},
                        {"label": "2010年代", "value": "2010年代"},
                        {"label": "2000年代", "value": "2000年代"},
                        {"label": "90年代", "value": "90年代"},
                        {"label": "80年代", "value": "80年代"},
                        {"label": "70年代", "value": "70年代"},
                        {"label": "60年代", "value": "60年代"},
                    ],
                    "show": "",
                    "depends_on": [],
                }
            ],
        },
        {
            "key": "bangumi",
            "name": "Bangumi",
            "builtin": True,
            "filter_params": {
                "type": "2",
                "cat": "",
                "sort": "rank",
                "year": "",
            },
            "depends": {},
            "filter_schema": [
                {
                    "key": "type",
                    "label": "类型",
                    "control": "chips",
                    "default": "2",
                    "options": [
                        {"label": "动画", "value": "2"},
                        {"label": "书籍", "value": "1"},
                        {"label": "音乐", "value": "3"},
                        {"label": "游戏", "value": "4"},
                        {"label": "三次元", "value": "6"},
                    ],
                    "show": "",
                    "depends_on": [],
                },
                {
                    "key": "cat",
                    "label": "分类",
                    "control": "chips",
                    "default": "",
                    "options": [
                        {"label": "其他", "value": "0"},
                        {"label": "TV", "value": "1"},
                        {"label": "OVA", "value": "2"},
                        {"label": "Movie", "value": "3"},
                        {"label": "WEB", "value": "5"},
                    ],
                    "show": "",
                    "depends_on": [],
                },
                {
                    "key": "sort",
                    "label": "排序",
                    "control": "chips",
                    "default": "rank",
                    "options": [
                        {"label": "排名", "value": "rank"},
                        {"label": "日期", "value": "date"},
                    ],
                    "show": "",
                    "depends_on": [],
                },
                {
                    "key": "year",
                    "label": "年代",
                    "control": "chips",
                    "default": "",
                    "options": [
                        {"label": "2026", "value": "2026"},
                        {"label": "2025", "value": "2025"},
                        {"label": "2024", "value": "2024"},
                        {"label": "2023", "value": "2023"},
                        {"label": "2022", "value": "2022"},
                        {"label": "2021", "value": "2021"},
                        {"label": "2020", "value": "2020"},
                        {"label": "2019", "value": "2019"},
                        {"label": "2018", "value": "2018"},
                        {"label": "2017", "value": "2017"},
                    ],
                    "show": "",
                    "depends_on": [],
                },
            ],
        },
    ]


def _get_all_discover_sources() -> list[dict]:
    return _get_builtin_sources() + _get_reference_sources()


def _find_discover_source(source_key: str) -> dict | None:
    return next((item for item in _get_all_discover_sources() if item["key"] == source_key), None)



def _normalize_douban_year_value(year: str) -> str:
    year = (year or "").strip()
    year_aliases = {
        "2020年代": "2020",
        "2010年代": "2010",
        "2000年代": "2000",
        "90年代": "1990",
        "80年代": "1980",
        "70年代": "1970",
        "60年代": "1960",
    }
    return year_aliases.get(year, year)




def _fetch_bangumi_discover(type_value: str = "2", cat: str = "", sort: str = "rank", year: str = "", page: int = 1, count: int = 30) -> dict:
    cache_key = f"bangumi_{type_value}_{cat}_{sort}_{year}_{page}_{count}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    offset = max(page - 1, 0) * count
    params = {
        "type": int(type_value or 2),
        "sort": sort or "rank",
        "limit": count,
        "offset": offset,
    }
    if cat:
        params["cat"] = cat
    if year and year.isdigit() and len(year) == 4:
        params["year"] = year

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    proxies = None
    proxy_url = _get_proxy_url()
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    try:
        try:
            response = requests.get("https://api.bgm.tv/v0/subjects", params=params, headers=headers, timeout=20, proxies=proxies)
            response.raise_for_status()
        except requests.RequestException as proxy_error:
            if not proxies:
                raise
            logger.warning(f"[bangumi] 代理请求失败，回退直连: {proxy_error}")
            response = requests.get("https://api.bgm.tv/v0/subjects", params=params, headers=headers, timeout=20)
            response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        logger.error(f"[bangumi] 请求失败: {e}")
        raise HTTPException(502, "Bangumi 请求失败")
    except ValueError as e:
        logger.error(f"[bangumi] 响应解析失败: {e}")
        raise HTTPException(502, "Bangumi 返回数据无效")

    items = [_normalize_provider_item(item, "bangumi") for item in data.get("data", [])]
    total = data.get("total")
    has_more = len(items) >= count
    total_pages = page + 1 if has_more else page
    if isinstance(total, int) and total > 0:
        total_pages = max((total + count - 1) // count, 1)
        has_more = page < total_pages

    result = {
        "source": "bangumi",
        "items": items,
        "total": total or len(items),
        "total_pages": total_pages,
        "page": page,
        "has_more": has_more,
    }
    _cache_set(cache_key, result)
    return result



@router.get("/sources")
def get_discover_sources():
    return {
        "sources": [
            {
                "key": source["key"],
                "name": source["name"],
                "filter_params": source.get("filter_params", {}),
                "depends": source.get("depends", {}),
                "filter_schema": source.get("filter_schema", []),
            }
            for source in _get_all_discover_sources()
        ]
    }


@router.get("/provider/{source_key}")
def discover_provider(source_key: str, request: Request):
    source = _find_discover_source(source_key)
    if not source:
        raise HTTPException(404, "未知数据源")

    if source_key == "themoviedb":
        media_type = request.query_params.get("media_type", "movie")
        sort_by = request.query_params.get("sort_by", "popularity.desc")
        with_genres = request.query_params.get("with_genres", "")
        with_original_language = request.query_params.get("with_original_language", "")
        with_watch_providers = request.query_params.get("with_watch_providers", "")
        vote_average = float(request.query_params.get("vote_average", "0") or 0)
        release_date = request.query_params.get("release_date", "")
        page = int(request.query_params.get("page", "1") or 1)
        return _tmdb_discover_impl(
            media_type=media_type,
            sort_by=sort_by,
            with_genres=with_genres,
            with_original_language=with_original_language,
            with_watch_providers=with_watch_providers,
            vote_average=vote_average,
            release_date=release_date,
            page=page,
        )

    if source_key == "douban":
        media_type = request.query_params.get("media_type", "movie")
        sort = request.query_params.get("sort", "U")
        style = request.query_params.get("style", "")
        region = request.query_params.get("region", "")
        year = _normalize_douban_year_value(request.query_params.get("year", ""))
        tags = request.query_params.get("tags", "")
        page = int(request.query_params.get("page", "1") or 1)
        count = int(request.query_params.get("count", "30") or 30)
        start = (page - 1) * count
        if not tags:
            tags = ",".join([tag for tag in [style, region, year] if tag])
        cache_key = f"douban_discover_{media_type}_{sort}_{tags}_{page}_{count}"
        cached = _cache_get(cache_key)
        if cached:
            return cached
        api = _get_douban_api()
        if media_type == "tv":
            data = api.discover_tv(start=start, count=count, tags=tags, sort=sort)
        else:
            data = api.discover_movies(start=start, count=count, tags=tags, sort=sort)
        if data.get("error"):
            raise HTTPException(500, f"豆瓣请求失败: {data.get('message', '')}")
        raw_items = data.get("items", data.get("subject_collection_items", []))
        items = [item for item in (_normalize_douban_item(i) for i in raw_items) if item]
        for item in items:
            item["source"] = source_key
        total = data.get("total")
        has_more = len(raw_items) >= count
        total_pages = page + 1 if has_more else page
        if isinstance(total, int) and total > 0:
            total_pages = max((total + count - 1) // count, 1)
            has_more = page < total_pages
        if media_type == "tv" and sort == "T" and not tags and page == 1 and raw_items:
            has_more = True
            total_pages = max(total_pages, page + 1)
        result = {
            "source": source_key,
            "items": items,
            "total": total or len(items),
            "total_pages": total_pages,
            "page": page,
            "has_more": has_more,
        }
        _cache_set(cache_key, result)
        return result

    if source_key == "bangumi":
        type_value = request.query_params.get("type", "2")
        cat = request.query_params.get("cat", "")
        sort = request.query_params.get("sort", "rank")
        year = request.query_params.get("year", "")
        page = int(request.query_params.get("page", "1") or 1)
        count = int(request.query_params.get("count", "30") or 30)
        return _fetch_bangumi_discover(
            type_value=type_value,
            cat=cat,
            sort=sort,
            year=year,
            page=page,
            count=count,
        )

    cache_key = f"provider_{source_key}_" + str(sorted(request.query_params.multi_items()))
    cached = _cache_get(cache_key)
    if cached:
        return cached

    filters = dict(source.get("filter_params", {}))
    for key, value in request.query_params.items():
        if key in {"page", "count"}:
            continue
        filters[key] = value
    filters = {
        key: _provider_filter_value(source_key, key, value)
        for key, value in filters.items()
    }
    page = int(request.query_params.get("page", "1") or 1)
    count = int(request.query_params.get("count", "30") or 30)
    filters["page"] = page
    filters["count"] = count

    module = source["module"]
    discover_func = getattr(module, f"{source['module_name']}_discover")
    try:
        raw_items = discover_func(**filters)
    except TypeError:
        allowed = discover_func.__code__.co_varnames[:discover_func.__code__.co_argcount]
        safe_filters = {k: v for k, v in filters.items() if k in allowed}
        raw_items = discover_func(**safe_filters)
    except Exception as e:
        logger.error(f"[{source_key}] discover 调用失败: {e}")
        raise HTTPException(500, f"{source['name']} 请求失败")

    items = [_normalize_provider_item(item, source_key) for item in (raw_items or [])]
    _mark_library_exists_on_items(items)
    page_size = len(items)
    result = {
        "source": source_key,
        "items": items,
        "page": page,
        "total_pages": page + 1 if page_size > 0 else page,
        "has_more": page_size > 0,
    }
    _cache_set(cache_key, result)
    return result


# ========== TMDB 端点 ==========

@router.get("/tmdb/trending")
def tmdb_trending(page: int = Query(1, ge=1)):
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    cache_key = f"tmdb_trending_{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = tmdb.get_trending_tmdb(api_key, page=page)
    if not data:
        raise HTTPException(500, "TMDB 请求失败")
    items = [_normalize_tmdb_item(i) for i in data.get("results", [])]
    _mark_library_exists_on_items(items)
    result = {"items": items, "total_pages": data.get("total_pages", 1), "page": page}
    _cache_set(cache_key, result)
    return result

@router.get("/tmdb/now_playing")
def tmdb_now_playing(page: int = Query(1, ge=1)):
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    cache_key = f"tmdb_now_playing_{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = tmdb.get_now_playing_tmdb(api_key, page=page)
    if not data:
        raise HTTPException(500, "TMDB 请求失败")
    items = [_normalize_tmdb_item(i) for i in data.get("results", [])]
    for i in items:
        i["media_type"] = "movie"
    _mark_library_exists_on_items(items)
    result = {"items": items, "total_pages": data.get("total_pages", 1), "page": page}
    _cache_set(cache_key, result)
    return result

@router.get("/tmdb/popular_movies")
def tmdb_popular_movies(page: int = Query(1, ge=1)):
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    cache_key = f"tmdb_popular_movies_{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = tmdb.get_popular_movies_tmdb(api_key, {"page": page})
    if not data:
        raise HTTPException(500, "TMDB 请求失败")
    items = [_normalize_tmdb_item(i) for i in data.get("results", [])]
    for i in items:
        i["media_type"] = "movie"
    _mark_library_exists_on_items(items)
    result = {"items": items, "total_pages": data.get("total_pages", 1), "page": page}
    _cache_set(cache_key, result)
    return result

@router.get("/tmdb/popular_tv")
def tmdb_popular_tv(page: int = Query(1, ge=1)):
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    cache_key = f"tmdb_popular_tv_{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = tmdb.get_popular_tv_tmdb(api_key, page=page)
    if not data:
        raise HTTPException(500, "TMDB 请求失败")
    items = [_normalize_tmdb_item(i) for i in data.get("results", [])]
    for i in items:
        i["media_type"] = "tv"
    _mark_library_exists_on_items(items)
    result = {"items": items, "total_pages": data.get("total_pages", 1), "page": page}
    _cache_set(cache_key, result)
    return result

def _tmdb_discover_impl(
    media_type: str = "movie",
    sort_by: str = "popularity.desc",
    with_genres: str = "",
    with_keywords: str = "",
    with_original_language: str = "",
    with_watch_providers: str = "",
    vote_average: float = 0.0,
    release_date: str = "",
    page: int = 1,
):
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    cache_key = f"tmdb_discover_v4_{media_type}_{sort_by}_{with_genres}_{with_keywords}_{with_original_language}_{with_watch_providers}_{vote_average}_{release_date}_{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    params = {"sort_by": sort_by, "page": page}
    if with_genres:
        params["with_genres"] = with_genres
    if with_original_language:
        params["with_original_language"] = with_original_language
    if with_keywords:
        params["with_keywords"] = with_keywords
    if with_watch_providers:
        params["with_watch_providers"] = with_watch_providers
    if vote_average > 0:
        params["vote_average.gte"] = vote_average
    if release_date:
        if media_type == "tv":
            params["first_air_date.gte"] = release_date
        else:
            params["primary_release_date.gte"] = release_date
    if media_type == "tv":
        data = tmdb.discover_tv_tmdb(api_key, params)
    else:
        data = tmdb.discover_movie_tmdb(api_key, params)
    if not data:
        raise HTTPException(500, "TMDB 请求失败")
    items = [_normalize_tmdb_item(i) for i in data.get("results", [])]
    for i in items:
        i["media_type"] = media_type
    _mark_library_exists_on_items(items)
    result = {"items": items, "total_pages": data.get("total_pages", 1), "page": page}
    _cache_set(cache_key, result)
    return result


@router.get("/tmdb/discover")
def tmdb_discover(
    media_type: str = Query("movie"),
    sort_by: str = Query("popularity.desc"),
    with_genres: str = Query(""),
    with_keywords: str = Query(""),
    with_original_language: str = Query(""),
    with_watch_providers: str = Query(""),
    vote_average: float = Query(0.0),
    release_date: str = Query(""),
    page: int = Query(1, ge=1),
):
    """TMDB 通用发现接口，支持完整筛选参数"""
    return _tmdb_discover_impl(
        media_type=media_type,
        sort_by=sort_by,
        with_genres=with_genres,
        with_keywords=with_keywords,
        with_original_language=with_original_language,
        with_watch_providers=with_watch_providers,
        vote_average=vote_average,
        release_date=release_date,
        page=page,
    )

# ========== 豆瓣端点 ==========

def _fetch_douban_collection(method_name: str, start: int, count: int, cache_suffix: str):
    """通用豆瓣集合拉取"""
    page = start // count + 1
    cache_key = f"douban_{cache_suffix}_{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    api = _get_douban_api()
    method = getattr(api, method_name, None)
    if not method:
        raise HTTPException(500, f"豆瓣方法 {method_name} 不存在")
    data = method(start=start, count=count)
    if data.get("error"):
        raise HTTPException(500, f"豆瓣请求失败: {data.get('message', '')}")
    raw_items = data.get("subject_collection_items", data.get("items", []))
    items = [item for item in (_normalize_douban_item(i) for i in raw_items) if item]
    _mark_library_exists_on_items(items)
    total = data.get("total", len(items))
    total_pages = (total + count - 1) // count if count else 1
    result = {"items": items, "total": total, "total_pages": total_pages, "page": page}
    _cache_set(cache_key, result)
    return result

@router.get("/douban/hot_movies")
def douban_hot_movies(start: int = Query(0, ge=0), count: int = Query(20, ge=1, le=50)):
    return _fetch_douban_collection("get_hot_movies", start, count, "hot_movies")

@router.get("/douban/hot_tv")
def douban_hot_tv(start: int = Query(0, ge=0), count: int = Query(20, ge=1, le=50)):
    return _fetch_douban_collection("get_hot_tv", start, count, "hot_tv")

@router.get("/douban/hot_anime")
def douban_hot_anime(start: int = Query(0, ge=0), count: int = Query(20, ge=1, le=50)):
    return _fetch_douban_collection("get_hot_anime", start, count, "hot_anime")

@router.get("/douban/showing")
def douban_showing(start: int = Query(0, ge=0), count: int = Query(20, ge=1, le=50)):
    return _fetch_douban_collection("get_showing", start, count, "showing")

@router.get("/douban/new_movies")
def douban_new_movies(start: int = Query(0, ge=0), count: int = Query(20, ge=1, le=50)):
    return _fetch_douban_collection("get_new_movies", start, count, "new_movies")

@router.get("/douban/new_tv")
def douban_new_tv(start: int = Query(0, ge=0), count: int = Query(20, ge=1, le=50)):
    return _fetch_douban_collection("get_new_tv", start, count, "new_tv")

@router.get("/douban/top250")
def douban_top250(start: int = Query(0, ge=0), count: int = Query(20, ge=1, le=50)):
    return _fetch_douban_collection("get_top250", start, count, "top250")

@router.get("/douban/chinese_weekly")
def douban_chinese_weekly(start: int = Query(0, ge=0), count: int = Query(20, ge=1, le=50)):
    return _fetch_douban_collection("get_chinese_tv_weekly", start, count, "chinese_weekly")

@router.get("/douban/global_weekly")
def douban_global_weekly(start: int = Query(0, ge=0), count: int = Query(20, ge=1, le=50)):
    return _fetch_douban_collection("get_global_tv_weekly", start, count, "global_weekly")

# ========== 媒体详情 ==========

@router.get("/detail/{tmdb_id}")
async def media_detail(tmdb_id: int, type: str = Query("movie")):
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    cache_key = f"detail_v2_{tmdb_id}_{type}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    append_to_response = "credits,videos,images,keywords,external_ids,translations,alternative_titles,recommendations,similar"
    if type == "tv":
        append_to_response = "credits,videos,images,keywords,external_ids,translations,content_ratings,alternative_titles,recommendations,similar"
        data = tmdb.get_tv_details(tmdb_id, api_key, append_to_response=append_to_response)
    else:
        append_to_response = "credits,videos,images,keywords,external_ids,translations,release_dates,alternative_titles,recommendations,similar"
        data = tmdb.get_movie_details(tmdb_id, api_key, append_to_response=append_to_response)
    if not data:
        raise HTTPException(404, "未找到媒体信息")
    detail = _normalize_tmdb_detail(data, type, tmdb_id)
    _cache_set(cache_key, detail, ttl=3600)
    return detail

@router.get("/library/series/{tmdb_id}")
def library_series_status(tmdb_id: int):
    return get_discover_series_status(tmdb_id)


def _empty_missing_episode_summary() -> dict:
    return {
        "tvCount": 0,
        "completeCount": 0,
        "partialCount": 0,
        "missingCount": 0,
        "errorCount": 0,
        "airingRecentMissingCount": 0,
        "airingAiredMissingCount": 0,
        "endedMissingCount": 0,
        "otherMissingCount": 0,
        "presentEpisodes": 0,
        "totalEpisodes": 0,
        "missingEpisodes": 0,
        "actionableMissingEpisodes": 0,
        "extraLocalEpisodes": 0,
    }


def _is_missing_episode_active_status(status: str) -> bool:
    normalized = str(status or "").strip().lower()
    return normalized in {"returning series", "in production", "planned", "pilot"}


def _build_missing_episode_season_brief(seasons: list[dict]) -> str:
    missing_seasons = [season for season in seasons if int(season.get("missing") or 0) > 0]
    parts = []
    for season in missing_seasons[:3]:
        missing_eps = season.get("missingEpisodes") or []
        if missing_eps:
            parts.append(f"S{season.get('seasonNumber')} 缺 {_format_episode_ranges(missing_eps)}")
        else:
            parts.append(f"S{season.get('seasonNumber')} 缺 {season.get('missing')}")
    if len(missing_seasons) > 3:
        parts.append(f"另 {len(missing_seasons) - 3} 季")
    return " / ".join(parts)


def _format_episode_ranges(episodes: list[int]) -> str:
    nums = sorted({int(ep) for ep in episodes if str(ep).isdigit()})
    if not nums:
        return ""
    ranges = []
    start = end = nums[0]
    for ep in nums[1:]:
        if ep == end + 1:
            end = ep
        else:
            ranges.append((start, end))
            start = end = ep
    ranges.append((start, end))
    parts = []
    for start, end in ranges:
        if start == end:
            parts.append(f"E{start:02d}")
        else:
            parts.append(f"E{start:02d}-E{end:02d}")
    return ",".join(parts)


def _positive_episode_set(episodes) -> set[int]:
    result = set()
    for ep in episodes or []:
        try:
            ep_num = int(ep)
        except Exception:
            continue
        if ep_num > 0:
            result.add(ep_num)
    return result


def _get_missing_episode_tmdb_season_detail(tmdb_id: int, season_number: int, api_key: str) -> dict | None:
    detail_cache_key = f"missing_episode_tv_season_{tmdb_id}_{season_number}"
    season_detail = _cache_get(detail_cache_key)
    if not season_detail:
        season_detail = tmdb.get_tv_season_details(tmdb_id, season_number, api_key)
        if season_detail:
            _cache_set(detail_cache_key, season_detail, ttl=24 * 60 * 60)
    return season_detail if isinstance(season_detail, dict) else None


def _tmdb_episode_numbers_from_season_detail(season_detail: dict | None) -> list[int]:
    episode_numbers = _positive_episode_set(
        episode.get("episode_number")
        for episode in ((season_detail or {}).get("episodes") or [])
        if isinstance(episode, dict)
    )
    return sorted(episode_numbers)


def _classify_missing_episode_item(tmdb_status: str, seasons: list[dict], missing_episodes: int) -> tuple[str, str]:
    if missing_episodes <= 0:
        return "complete", "完整入库"
    normalized_status = str(tmdb_status or "").strip().lower()
    if normalized_status == "ended":
        return "ended_missing", "已完结但缺集"

    if _is_missing_episode_active_status(normalized_status):
        numbered_seasons = [s for s in seasons if int(s.get("total") or 0) > 0]
        if numbered_seasons:
            latest = max(numbered_seasons, key=lambda s: int(s.get("seasonNumber") or 0))
            total = int(latest.get("total") or 0)
            missing_set = {int(ep) for ep in latest.get("missingEpisodes") or []}
            episode_numbers = sorted(_positive_episode_set(latest.get("episodeNumbers") or [])) or list(range(1, total + 1))
            recent_known = set(episode_numbers[-3:])
            if missing_set & recent_known:
                return "airing_recent_missing", "正在连载但缺最近集"
    return "partial_missing", "有缺集"


def _parse_tmdb_air_date(value: str):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _build_aired_missing_episode_sets(tmdb_id: int, seasons: list[dict], api_key: str) -> dict[int, set[int]]:
    today = datetime.now().date()
    aired_missing_by_season: dict[int, set[int]] = {}
    for season in seasons:
        season_number = int(season.get("seasonNumber") or 0)
        if season_number <= 0 or int(season.get("missing") or 0) <= 0:
            continue
        season_detail = _get_missing_episode_tmdb_season_detail(tmdb_id, season_number, api_key)
        if not season_detail:
            continue
        missing_set = {int(ep) for ep in season.get("missingEpisodes") or []}
        aired_missing = set()
        aired_episode_numbers = set()
        for episode in season_detail.get("episodes") or []:
            try:
                ep_num = int(episode.get("episode_number") or 0)
            except Exception:
                continue
            air_date = _parse_tmdb_air_date(episode.get("air_date") or "")
            if air_date and air_date <= today:
                aired_episode_numbers.add(ep_num)
            if ep_num not in missing_set:
                continue
            if air_date and air_date <= today:
                aired_missing.add(ep_num)
        if aired_episode_numbers:
            max_aired_episode = max(aired_episode_numbers)
            aired_missing.update(ep for ep in missing_set if ep <= max_aired_episode)
        if aired_missing:
            aired_missing_by_season[season_number] = aired_missing
    return aired_missing_by_season


def _build_missing_episode_stat(entry: dict, api_key: str) -> dict:
    tmdb_id = str(entry.get("tmdb_id") or "").strip()
    title = entry.get("title") or entry.get("original_title") or ""
    year = str(entry.get("year") or "")[:4]
    library_id = str(entry.get("library_id") or "")
    library_name = entry.get("library_name") or "未分类媒体库"
    local_item = {
        "tmdbId": tmdb_id,
        "embyId": entry.get("emby_id", ""),
        "title": title,
        "originalTitle": entry.get("original_title") or "",
        "year": year,
        "libraryId": library_id,
        "libraryName": library_name,
        "seasons": entry.get("seasons") or {},
    }
    base_item = {
        "id": int(tmdb_id) if tmdb_id.isdigit() else tmdb_id,
        "_tmdb_id": int(tmdb_id) if tmdb_id.isdigit() else tmdb_id,
        "tmdb_id": int(tmdb_id) if tmdb_id.isdigit() else tmdb_id,
        "title": title,
        "original_title": entry.get("original_title") or "",
        "year": year,
        "poster_url": "",
        "backdrop_url": "",
        "rating": 0,
        "overview": "",
        "media_type": "tv",
        "genre_ids": [],
        "source": "tmdb",
        "subscribed": False,
        "exists_in_library": True,
    }
    try:
        detail_cache_key = f"missing_episode_tv_detail_{tmdb_id}"
        detail = _cache_get(detail_cache_key)
        if not detail:
            detail = tmdb.get_tv_details(int(tmdb_id), api_key, append_to_response="seasons")
            if detail:
                _cache_set(detail_cache_key, detail, ttl=24 * 60 * 60)
        if not detail:
            raise ValueError("未找到 TMDB 剧集详情")
        normalized = _normalize_tmdb_detail(detail, "tv", int(tmdb_id))
        local_seasons = entry.get("seasons") or {}
        tmdb_numbered_seasons = [
            season for season in (normalized.get("seasons") or [])
            if int(season.get("season_number") or 0) > 0 and int(season.get("episode_count") or 0) > 0
        ]
        seasons = []
        tmdb_season_numbers = set()
        extra_local_episodes = 0
        extra_local_seasons = []
        for season in sorted(tmdb_numbered_seasons, key=lambda item: int(item.get("season_number") or 0)):
            season_number = season.get("season_number")
            season_number = int(season_number)
            tmdb_season_numbers.add(season_number)
            season_detail = _get_missing_episode_tmdb_season_detail(int(tmdb_id), season_number, api_key)
            episode_numbers = _tmdb_episode_numbers_from_season_detail(season_detail)
            if not episode_numbers:
                episode_numbers = list(range(1, int(season.get("episode_count") or 0) + 1))
            expected = set(episode_numbers)
            total = len(expected)
            present_set = set()
            extra_set = set()
            for ep_num in _positive_episode_set(local_seasons.get(str(season_number), [])):
                if ep_num in expected:
                    present_set.add(ep_num)
                else:
                    extra_set.add(ep_num)
            missing_set = expected - present_set
            extra_local_episodes += len(extra_set)
            seasons.append({
                "seasonNumber": season_number,
                "present": len(present_set),
                "total": total,
                "missing": len(missing_set),
                "episodeNumbers": sorted(expected),
                "episodeNumberSource": "tmdb" if season_detail else "episode_count",
                "presentEpisodes": sorted(present_set),
                "missingEpisodes": sorted(missing_set),
                "extraEpisodes": sorted(extra_set),
            })
        for season_key, episodes in local_seasons.items():
            try:
                season_number = int(season_key)
            except Exception:
                continue
            if season_number <= 0 or season_number in tmdb_season_numbers:
                continue
            extra_eps = []
            for ep in episodes or []:
                try:
                    ep_num = int(ep)
                except Exception:
                    continue
                if ep_num > 0:
                    extra_eps.append(ep_num)
            extra_eps = sorted(set(extra_eps))
            if extra_eps:
                extra_local_episodes += len(extra_eps)
                extra_local_seasons.append({
                    "seasonNumber": season_number,
                    "episodes": extra_eps,
                    "count": len(extra_eps),
                })
        if not seasons:
            raise ValueError("TMDB 无有效季集信息")
        tmdb_status = detail.get("status") or normalized.get("status") or ""
        is_active_series = _is_missing_episode_active_status(tmdb_status)
        raw_total_episodes = sum(season["total"] for season in seasons)
        present_episodes = sum(season["present"] for season in seasons)
        raw_missing_episodes = sum(season["missing"] for season in seasons)
        aired_missing_episodes = 0
        if is_active_series and raw_missing_episodes > 0:
            aired_missing_sets = _build_aired_missing_episode_sets(int(tmdb_id), seasons, api_key)
            for season in seasons:
                season_number = int(season.get("seasonNumber") or 0)
                raw_missing = sorted({int(ep) for ep in season.get("missingEpisodes") or []})
                aired_missing = sorted(aired_missing_sets.get(season_number, set()))
                unaired_missing = sorted(set(raw_missing) - set(aired_missing))
                season["rawTotal"] = int(season.get("total") or 0)
                season["rawMissing"] = int(season.get("missing") or 0)
                season["rawMissingEpisodes"] = raw_missing
                season["unairedMissingEpisodes"] = unaired_missing
                season["missingEpisodes"] = aired_missing
                season["missing"] = len(aired_missing)
                season["airedMissingEpisodes"] = aired_missing
                season["airedMissing"] = len(aired_missing)
                aired_missing_episodes += len(aired_missing)
        else:
            for season in seasons:
                raw_missing = sorted({int(ep) for ep in season.get("missingEpisodes") or []})
                season["rawTotal"] = int(season.get("total") or 0)
                season["rawMissing"] = int(season.get("missing") or 0)
                season["rawMissingEpisodes"] = raw_missing
                season["unairedMissingEpisodes"] = []
                season["airedMissingEpisodes"] = []
                season["airedMissing"] = 0
        missing_episodes = sum(season["missing"] for season in seasons)
        total_episodes = present_episodes + missing_episodes
        missing_category, category_label = _classify_missing_episode_item(tmdb_status, seasons, missing_episodes)
        if extra_local_episodes > 0:
            status = "error"
            label = f"异常入库 +{extra_local_episodes} 集"
            missing_category = "error"
            category_label = "异常入库"
        elif present_episodes >= total_episodes:
            status = "exists"
            label = f"已入库 {present_episodes}/{total_episodes}"
        elif present_episodes > 0:
            status = "partial"
            label = f"已入库缺集 {present_episodes}/{total_episodes}"
        else:
            status = "error"
            label = "刮削异常"
            missing_category = "error"
            category_label = "统计异常"
        poster_url = normalized.get("poster_url") or ""
        backdrop_url = normalized.get("backdrop_url") or ""
        item = {
            **base_item,
            "title": normalized.get("title") or title,
            "original_title": normalized.get("original_title") or base_item["original_title"],
            "year": normalized.get("year") or year,
            "poster_url": poster_url,
            "backdrop_url": backdrop_url,
            "rating": normalized.get("vote_average") or normalized.get("rating") or 0,
            "overview": normalized.get("overview") or "",
        }
        return {
            "tmdbId": tmdb_id,
            "item": item,
            "localItem": local_item,
            "title": item["title"],
            "year": item["year"],
            "poster_url": poster_url,
            "backdrop_url": backdrop_url,
            "status": status,
            "label": label,
            "missingCategory": missing_category,
            "categoryLabel": category_label,
            "tmdbStatus": tmdb_status,
            "libraryId": library_id,
            "libraryName": library_name,
            "presentEpisodes": present_episodes,
            "totalEpisodes": total_episodes,
            "missingEpisodes": missing_episodes,
            "rawTotalEpisodes": raw_total_episodes,
            "rawMissingEpisodes": raw_missing_episodes,
            "airedMissingEpisodes": aired_missing_episodes,
            "extraLocalEpisodes": extra_local_episodes,
            "extraLocalSeasons": extra_local_seasons,
            "seasons": seasons,
            "seasonBrief": _build_missing_episode_season_brief(seasons),
        }
    except Exception as e:
        return {
            "tmdbId": tmdb_id,
            "item": base_item,
            "localItem": local_item,
            "title": title,
            "year": year,
            "poster_url": "",
            "backdrop_url": "",
            "status": "error",
            "label": "统计失败",
            "missingCategory": "error",
            "categoryLabel": "统计异常",
            "tmdbStatus": "",
            "libraryId": library_id,
            "libraryName": library_name,
            "presentEpisodes": 0,
            "totalEpisodes": 0,
            "missingEpisodes": 0,
            "airedMissingEpisodes": 0,
            "extraLocalEpisodes": 0,
            "extraLocalSeasons": [],
            "seasons": [],
            "seasonBrief": str(e) or "请求失败",
        }


def _accumulate_missing_episode_summary(summary: dict, item: dict) -> None:
    summary["tvCount"] += 1
    status = item.get("status")
    if status == "exists":
        summary["completeCount"] += 1
    elif status == "partial":
        summary["partialCount"] += 1
    elif status == "missing":
        summary["errorCount"] += 1
    elif status == "error":
        summary["errorCount"] += 1
    missing_episodes = int(item.get("missingEpisodes") or 0)
    if missing_episodes > 0:
        summary["missingCount"] += 1
    category = item.get("missingCategory")
    normalized_tmdb_status = str(item.get("tmdbStatus") or "").strip().lower()
    aired_missing_episodes = int(item.get("airedMissingEpisodes") or 0)
    is_active_series = _is_missing_episode_active_status(normalized_tmdb_status)
    if is_active_series and aired_missing_episodes > 0:
        summary["airingAiredMissingCount"] += 1
    if category == "airing_recent_missing" and is_active_series and aired_missing_episodes <= 0:
        summary["airingRecentMissingCount"] += 1
    elif category == "ended_missing":
        summary["endedMissingCount"] += 1
    elif category == "partial_missing":
        summary["otherMissingCount"] += 1
    summary["actionableMissingEpisodes"] += missing_episodes
    summary["presentEpisodes"] += int(item.get("presentEpisodes") or 0)
    summary["totalEpisodes"] += int(item.get("totalEpisodes") or 0)
    summary["missingEpisodes"] += missing_episodes
    summary["extraLocalEpisodes"] += int(item.get("extraLocalEpisodes") or 0)


def _build_missing_episode_libraries(results: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for item in results:
        library_id = str(item.get("libraryId") or "")
        library_name = item.get("libraryName") or "未分类媒体库"
        key = library_id or library_name
        library = grouped.setdefault(key, {
            "libraryId": library_id,
            "libraryName": library_name,
            "summary": _empty_missing_episode_summary(),
            "items": [],
        })
        library["items"].append(item)
        _accumulate_missing_episode_summary(library["summary"], item)
    libraries = list(grouped.values())
    libraries.sort(key=lambda lib: (
        -int(lib["summary"].get("missingEpisodes") or 0),
        lib.get("libraryName") or "",
    ))
    return libraries


def _build_missing_episode_libraries_from_entries(entries: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for entry in entries:
        library_id = str(entry.get("library_id") or "")
        library_name = entry.get("library_name") or "未分类媒体库"
        key = library_id or library_name
        library = grouped.setdefault(key, {
            "libraryId": library_id,
            "libraryName": library_name,
            "summary": _empty_missing_episode_summary(),
            "items": [],
        })
        library["summary"]["tvCount"] += 1
    libraries = list(grouped.values())
    libraries.sort(key=lambda lib: lib.get("libraryName") or "")
    return libraries


def _get_rss_real_library_names(server_idx: int | str | None = None) -> set[str]:
    try:
        if not os.path.exists(RSS_TASKS_FILE):
            return set()
        with open(RSS_TASKS_FILE, "r", encoding="utf-8") as f:
            tasks = json.load(f)
        if not isinstance(tasks, list):
            return set()
        target_server_idx = int(server_idx or 0)
        names = set()
        for task in tasks:
            if not isinstance(task, dict):
                continue
            try:
                task_server_idx = int(task.get("target_server_idx", 0) or 0)
            except Exception:
                task_server_idx = 0
            if task_server_idx != target_server_idx:
                continue
            name = str(task.get("name") or "").strip()
            if name:
                names.add(name)
        return names
    except Exception as e:
        logger.debug(f"[Discover] 读取 RSS 真实库列表失败: {e}")
        return set()


def _filter_rss_real_library_entries(entries: list[dict], meta: dict | None = None) -> list[dict]:
    rss_library_names = _get_rss_real_library_names((meta or {}).get("server_idx", 0))
    if not rss_library_names:
        return entries
    filtered = [
        entry for entry in entries
        if str(entry.get("library_name") or "").strip() not in rss_library_names
    ]
    skipped = len(entries) - len(filtered)
    if skipped:
        logger.info(f"[Discover] 缺集统计已跳过 RSS 真实库 {skipped} 条")
    return filtered


_missing_episode_stats_lock = threading.RLock()
_missing_episode_cache_db_lock = threading.RLock()
_missing_episode_cache_db_ready = False
MISSING_EPISODE_TMDB_MAX_WORKERS = 12
MISSING_EPISODE_STATS_CACHE_VERSION = 14
MISSING_EPISODE_SUMMARY_PREVIEW_VERSION = 6
MISSING_EPISODE_SUMMARY_PREVIEW_LIMIT = 48
_missing_episode_stats_state: dict = {
    "cache_key": "",
    "running": False,
    "payload": None,
    "progress": {"current": 0, "total": 0},
    "error": "",
}


def _create_missing_episode_stats_cache_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS missing_episode_stats_cache (
            cache_key TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            saved_at INTEGER NOT NULL DEFAULT 0,
            entry_count INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            summary_payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_missing_episode_stats_saved_at
            ON missing_episode_stats_cache(version, saved_at);
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(missing_episode_stats_cache)").fetchall()}
    if "summary_payload_json" not in columns:
        conn.execute("ALTER TABLE missing_episode_stats_cache ADD COLUMN summary_payload_json TEXT NOT NULL DEFAULT '{}'")


def _slim_missing_episode_preview_item(item: dict) -> dict:
    media_item = item.get("item") if isinstance(item.get("item"), dict) else {}
    preview_media = {
        "id": media_item.get("id") or item.get("tmdbId") or "",
        "_tmdb_id": media_item.get("_tmdb_id") or item.get("tmdbId") or "",
        "tmdb_id": media_item.get("tmdb_id") or item.get("tmdbId") or "",
        "title": media_item.get("title") or item.get("title") or "",
        "original_title": media_item.get("original_title") or "",
        "year": media_item.get("year") or item.get("year") or "",
        "poster_url": media_item.get("poster_url") or item.get("poster_url") or "",
        "backdrop_url": media_item.get("backdrop_url") or item.get("backdrop_url") or "",
        "rating": media_item.get("rating") or 0,
        "overview": media_item.get("overview") or "",
        "media_type": "tv",
        "source": media_item.get("source") or "tmdb",
        "subscribed": bool(media_item.get("subscribed", False)),
        "exists_in_library": True,
    }
    return {
        "tmdbId": item.get("tmdbId") or "",
        "item": preview_media,
        "title": item.get("title") or preview_media["title"],
        "year": item.get("year") or preview_media["year"],
        "poster_url": item.get("poster_url") or preview_media["poster_url"],
        "backdrop_url": item.get("backdrop_url") or preview_media["backdrop_url"],
        "status": item.get("status") or "",
        "label": item.get("label") or "",
        "missingCategory": item.get("missingCategory") or "",
        "categoryLabel": item.get("categoryLabel") or "",
        "tmdbStatus": item.get("tmdbStatus") or "",
        "localItem": item.get("localItem") or {},
        "libraryId": item.get("libraryId") or "",
        "libraryName": item.get("libraryName") or "",
        "presentEpisodes": item.get("presentEpisodes") or 0,
        "totalEpisodes": item.get("totalEpisodes") or 0,
        "missingEpisodes": item.get("missingEpisodes") or 0,
        "airedMissingEpisodes": item.get("airedMissingEpisodes") or 0,
        "extraLocalEpisodes": item.get("extraLocalEpisodes") or 0,
        "extraLocalSeasons": item.get("extraLocalSeasons") or [],
        "seasons": item.get("seasons") or [],
        "seasonBrief": item.get("seasonBrief") or "",
    }


def _missing_episode_preview_sort_items(items: list[dict]) -> list[dict]:
    def num(value) -> int:
        try:
            return int(float(str(value or 0).strip() or 0))
        except Exception:
            return 0

    def year(item: dict) -> int:
        return num(str(item.get("year") or "")[:4])

    problem_items = [item for item in items if isinstance(item, dict) and item.get("status") == "partial"]
    if not problem_items:
        problem_items = [item for item in items if isinstance(item, dict)]
    return sorted(
        problem_items,
        key=lambda item: (
            -year(item),
            -num(item.get("missingEpisodes")),
            str(item.get("title") or ""),
        ),
    )


def _build_missing_episode_summary_payload(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    summary_payload = dict(payload)
    full_libraries = [lib for lib in (payload.get("libraries") or []) if isinstance(lib, dict)]
    first_problem_lib = next((lib for lib in full_libraries if (lib.get("summary") or {}).get("missingEpisodes")), None)
    preview_library_key = ""
    preview_items: list[dict] = []
    if first_problem_lib:
        preview_library_key = str(first_problem_lib.get("libraryId") or first_problem_lib.get("libraryName") or "")
        preview_items = [
            _slim_missing_episode_preview_item(item)
            for item in _missing_episode_preview_sort_items(first_problem_lib.get("items") or [])[:MISSING_EPISODE_SUMMARY_PREVIEW_LIMIT]
        ]
    summary_payload["items"] = preview_items
    summary_payload["libraries"] = []
    for lib in full_libraries:
        lib_key = str(lib.get("libraryId") or lib.get("libraryName") or "")
        summary_lib = {
            "libraryId": lib.get("libraryId", ""),
            "libraryName": lib.get("libraryName", ""),
            "summary": lib.get("summary") or _empty_missing_episode_summary(),
        }
        if lib_key == preview_library_key:
            summary_lib["items"] = preview_items
        summary_payload["libraries"].append(summary_lib)
    summary_payload["summaryOnly"] = True
    summary_payload["previewVersion"] = MISSING_EPISODE_SUMMARY_PREVIEW_VERSION
    return summary_payload


def _legacy_missing_episode_stats_cache_file() -> dict:
    try:
        if not os.path.exists(MISSING_EPISODE_STATS_CACHE_FILE):
            return {}
        with open(MISSING_EPISODE_STATS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug(f"[Discover] 读取缺集统计缓存失败: {e}")
        return {}


def _write_missing_episode_stats_cache_to_db(conn, data: dict) -> None:
    meta = data.get("_meta") if isinstance(data, dict) else {}
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(meta, dict) or not isinstance(payload, dict):
        return
    cache_key = str(meta.get("cache_key") or "")
    if not cache_key:
        return
    version = int(meta.get("version", MISSING_EPISODE_STATS_CACHE_VERSION) or MISSING_EPISODE_STATS_CACHE_VERSION)
    saved_at = int(meta.get("saved_at", time.time()) or time.time())
    entry_count = int(meta.get("entry_count", 0) or 0)
    conn.execute(
        """
        INSERT INTO missing_episode_stats_cache(cache_key, version, saved_at, entry_count, payload_json, summary_payload_json)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            version = excluded.version,
            saved_at = excluded.saved_at,
            entry_count = excluded.entry_count,
            payload_json = excluded.payload_json,
            summary_payload_json = excluded.summary_payload_json
        """,
        (
            cache_key,
            version,
            saved_at,
            entry_count,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            json.dumps(_build_missing_episode_summary_payload(payload), ensure_ascii=False, separators=(",", ":")),
        ),
    )


def _read_missing_episode_stats_cache_from_db(conn, cache_key: str | None = None, summary_only: bool = False) -> dict:
    payload_column = "summary_payload_json" if summary_only else "payload_json"
    if cache_key:
        row = conn.execute(
            f"""
            SELECT cache_key, version, saved_at, entry_count, {payload_column} AS payload_json
            FROM missing_episode_stats_cache
            WHERE cache_key = ? AND version = ?
            LIMIT 1
            """,
            (str(cache_key), MISSING_EPISODE_STATS_CACHE_VERSION),
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            SELECT cache_key, version, saved_at, entry_count, {payload_column} AS payload_json
            FROM missing_episode_stats_cache
            WHERE version = ?
            ORDER BY saved_at DESC LIMIT 1
            """,
            (MISSING_EPISODE_STATS_CACHE_VERSION,),
        ).fetchone()
    if not row:
        if not cache_key:
            return {}
        row = conn.execute(
            f"""
            SELECT cache_key, version, saved_at, entry_count, {payload_column} AS payload_json
            FROM missing_episode_stats_cache
            WHERE version = ?
            ORDER BY saved_at DESC LIMIT 1
            """,
            (MISSING_EPISODE_STATS_CACHE_VERSION,),
        ).fetchone()
        if not row:
            return {}
    try:
        payload = json.loads(row["payload_json"] or "{}")
        if not isinstance(payload, dict):
            return {}
        if summary_only and not payload.get("summary") and not payload.get("libraries"):
            return {}
    except Exception:
        return {}
    return {
        "_meta": {
            "version": int(row["version"] or MISSING_EPISODE_STATS_CACHE_VERSION),
            "cache_key": str(row["cache_key"] or ""),
            "saved_at": int(row["saved_at"] or 0),
            "entry_count": int(row["entry_count"] or 0),
        },
        "payload": payload,
    }


def _migrate_missing_episode_stats_json_if_needed(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS count FROM missing_episode_stats_cache").fetchone()
    if row and int(row["count"] or 0) > 0:
        return
    data = _legacy_missing_episode_stats_cache_file()
    meta = data.get("_meta") if isinstance(data, dict) else {}
    if not isinstance(meta, dict) or int(meta.get("version", 0) or 0) != MISSING_EPISODE_STATS_CACHE_VERSION:
        return
    _write_missing_episode_stats_cache_to_db(conn, data)
    logger.info("[Discover] 已迁移缺集统计缓存 JSON 到 SQLite")


def _ensure_missing_episode_stats_cache_schema() -> None:
    global _missing_episode_cache_db_ready
    if _missing_episode_cache_db_ready:
        return
    with _missing_episode_cache_db_lock:
        if _missing_episode_cache_db_ready:
            return
        with cache_db(write=True) as conn:
            _create_missing_episode_stats_cache_schema(conn)
            _migrate_missing_episode_stats_json_if_needed(conn)
        _missing_episode_cache_db_ready = True


def _read_missing_episode_stats_cache_file(cache_key: str | None = None, summary_only: bool = False) -> dict:
    try:
        _ensure_missing_episode_stats_cache_schema()
        with cache_db() as conn:
            return _read_missing_episode_stats_cache_from_db(conn, cache_key, summary_only=summary_only)
    except Exception as e:
        logger.debug(f"[Discover] 读取缺集统计 SQLite 缓存失败: {e}")
        return {}


def _write_missing_episode_stats_cache_file(data: dict) -> None:
    _ensure_missing_episode_stats_cache_schema()
    with cache_db(write=True) as conn:
        _write_missing_episode_stats_cache_to_db(conn, data)


def _normalize_missing_episode_entry_for_cache(entry: dict) -> dict:
    seasons = {}
    for season, episodes in (entry.get("seasons") or {}).items():
        try:
            season_key = str(int(season))
        except Exception:
            continue
        episode_nums = set()
        for ep in episodes or []:
            try:
                ep_num = int(ep)
            except Exception:
                continue
            if ep_num > 0:
                episode_nums.add(ep_num)
        seasons[season_key] = sorted(episode_nums)
    return {
        "tmdb_id": str(entry.get("tmdb_id") or ""),
        "library_id": str(entry.get("library_id") or ""),
        "seasons": seasons,
    }


def _build_missing_episode_entries_digest(entries: list[dict]) -> str:
    normalized = [_normalize_missing_episode_entry_for_cache(entry) for entry in entries or []]
    normalized.sort(key=lambda item: (item.get("tmdb_id") or "", item.get("library_id") or ""))
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_missing_episode_cache_key(entries: list[dict], meta: dict | None = None) -> str:
    try:
        server_idx = int((meta or {}).get("server_idx", 0) or 0)
    except Exception:
        server_idx = 0
    return f"library_missing_episode_stats_v{MISSING_EPISODE_STATS_CACHE_VERSION}_server_{server_idx}"


def _missing_episode_result_key(item: dict) -> tuple[str, str]:
    return str(item.get("tmdbId") or item.get("tmdb_id") or ""), str(item.get("libraryId") or "")


def _missing_episode_entry_key(entry: dict) -> tuple[str, str]:
    return str(entry.get("tmdb_id") or ""), str(entry.get("library_id") or "")


def _decorate_missing_episode_payload(payload: dict, cache_key: str, entries: list[dict], meta: dict, message: str = "") -> dict:
    payload = dict(payload or {})
    payload_meta = dict(meta or {})
    payload_meta.update({
        "missing_stats_cache_version": MISSING_EPISODE_STATS_CACHE_VERSION,
        "missing_stats_cache_key": cache_key,
        "missing_stats_entry_count": len(entries or []),
        "missing_stats_index_digest": _build_missing_episode_entries_digest(entries),
        "missing_stats_updated_at": int(time.time()),
    })
    payload["meta"] = payload_meta
    if message:
        payload["message"] = message
    payload.setdefault("ready", True)
    payload.setdefault("running", False)
    return payload


def _save_missing_episode_stats_cache(payload: dict, cache_key: str, entries: list[dict]) -> None:
    if not isinstance(payload, dict) or payload.get("running"):
        return
    data = {
        "_meta": {
            "version": MISSING_EPISODE_STATS_CACHE_VERSION,
            "cache_key": cache_key,
            "saved_at": int(time.time()),
            "entry_count": len(entries or []),
        },
        "payload": payload,
    }
    try:
        _write_missing_episode_stats_cache_file(data)
    except Exception as e:
        logger.warning(f"[Discover] 缺集统计缓存落盘失败: {e}")


def _load_missing_episode_stats_cache(cache_key: str | None = None, summary_only: bool = False) -> dict | None:
    data = _read_missing_episode_stats_cache_file(cache_key, summary_only=summary_only)
    meta = data.get("_meta") if isinstance(data, dict) else {}
    if not isinstance(meta, dict) or meta.get("version") != MISSING_EPISODE_STATS_CACHE_VERSION:
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict) or payload.get("running"):
        return None
    payload = dict(payload)
    if cache_key and meta.get("cache_key") != cache_key:
        payload_meta = dict(payload.get("meta") or {})
        payload_meta.setdefault("missing_stats_cache_migrated_from", meta.get("cache_key") or "")
        payload_meta["missing_stats_cache_key"] = cache_key
        payload["meta"] = payload_meta
    payload["running"] = False
    payload.setdefault("message", "统计完成")
    return payload


def _load_latest_missing_episode_stats_payload_any_version() -> tuple[dict | None, int]:
    """Read the latest full stats payload only as an incremental-update base."""
    try:
        _ensure_missing_episode_stats_cache_schema()
        with cache_db() as conn:
            row = conn.execute(
                """
                SELECT version, payload_json
                FROM missing_episode_stats_cache
                ORDER BY saved_at DESC LIMIT 1
                """
            ).fetchone()
        if not row:
            return None, 0
        payload = json.loads(row["payload_json"] or "{}")
        if not isinstance(payload, dict) or payload.get("running"):
            return None, int(row["version"] or 0)
        payload = dict(payload)
        payload["running"] = False
        payload.setdefault("message", "统计完成")
        return payload, int(row["version"] or 0)
    except Exception as e:
        logger.debug(f"[Discover] 读取旧版缺集统计缓存失败: {e}")
        return None, 0


def _load_or_backfill_missing_episode_stats_summary(cache_key: str | None, entries: list[dict]) -> dict | None:
    cached = _load_missing_episode_stats_cache(cache_key, summary_only=True)
    if cached and cached.get("previewVersion") == MISSING_EPISODE_SUMMARY_PREVIEW_VERSION:
        return cached
    full = _load_missing_episode_stats_cache(cache_key)
    if not full:
        return None
    summary = _build_missing_episode_summary_payload(full)
    try:
        saved_key = (full.get("meta") or {}).get("missing_stats_cache_key") or cache_key
        if saved_key:
            _save_missing_episode_stats_cache(full, saved_key, entries)
    except Exception as e:
        logger.debug(f"[Discover] 缺集统计摘要缓存回填失败: {e}")
    return summary


def _sort_missing_episode_results(results: list[dict]) -> list[dict]:
    priority = {"airing_recent_missing": 0, "ended_missing": 1, "partial_missing": 2, "error": 3, "complete": 4}
    return sorted(results, key=lambda item: (
        priority.get(item.get("missingCategory"), 5),
        -int(item.get("missingEpisodes") or 0),
        item.get("title") or "",
    ))


def _build_missing_episode_progress_payload(entries: list[dict], meta: dict, running: bool = False, message: str = "") -> dict:
    summary = _empty_missing_episode_summary()
    summary["tvCount"] = len(entries)
    return {
        "ready": True,
        "running": running,
        "meta": meta,
        "summary": summary,
        "items": [],
        "libraries": _build_missing_episode_libraries_from_entries(entries),
        "progress": {"current": 0, "total": len(entries)},
        "message": message,
    }


def _store_missing_episode_progress(payload: dict, cache_key: str) -> None:
    with _missing_episode_stats_lock:
        _missing_episode_stats_state["cache_key"] = cache_key
        _missing_episode_stats_state["payload"] = payload
        _missing_episode_stats_state["progress"] = payload.get("progress") or {"current": 0, "total": 0}


def _run_missing_episode_stats_job(
    entries: list[dict],
    api_key: str,
    meta: dict,
    cache_key: str,
    full_calibration: bool = False,
) -> None:
    results: list[dict] = []
    total = len(entries)
    payload = _build_missing_episode_progress_payload(entries, meta, running=True, message="正在统计剧集缺集...")
    _store_missing_episode_progress(payload, cache_key)
    try:
        if full_calibration:
            calibration_payload = _build_missing_episode_progress_payload(
                entries,
                meta,
                running=True,
                message="正在校准 Emby 剧集索引...",
            )
            _store_missing_episode_progress(calibration_payload, cache_key)
            build_discover_index(
                server_idx=int((meta or {}).get("server_idx", 0) or 0),
                reason="missing_episode_stats:manual_calibration",
                force=True,
            )
            meta = get_discover_index_meta()
            entries = _filter_rss_real_library_entries(get_discover_series_entries(), meta)
            cache_key = _build_missing_episode_cache_key(entries, meta)
            total = len(entries)
            payload = _build_missing_episode_progress_payload(entries, meta, running=True, message="正在统计剧集缺集...")
            _store_missing_episode_progress(payload, cache_key)

        max_workers = min(MISSING_EPISODE_TMDB_MAX_WORKERS, max(1, total))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_build_missing_episode_stat, entry, api_key) for entry in entries]
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.debug(f"[Discover] 缺集统计条目计算失败: {e}")
                sorted_results = _sort_missing_episode_results(results)
                summary = _empty_missing_episode_summary()
                for item in sorted_results:
                    _accumulate_missing_episode_summary(summary, item)
                summary["tvCount"] = total
                progress = {"current": len(results), "total": total}
                progress_payload = {
                    "ready": True,
                    "running": True,
                    "meta": meta,
                    "summary": summary,
                    "items": sorted_results,
                    "libraries": _merge_missing_episode_libraries(entries, sorted_results),
                    "progress": progress,
                    "message": "正在统计剧集缺集...",
                }
                _store_missing_episode_progress(progress_payload, cache_key)

        final_results = _sort_missing_episode_results(results)
        final_summary = _empty_missing_episode_summary()
        for item in final_results:
            _accumulate_missing_episode_summary(final_summary, item)
        final_summary["tvCount"] = total
        final_payload = {
            "ready": True,
            "running": False,
            "meta": meta,
            "summary": final_summary,
            "items": final_results,
            "libraries": _merge_missing_episode_libraries(entries, final_results),
            "progress": {"current": len(results), "total": total},
            "message": "统计完成",
        }
        final_payload = _decorate_missing_episode_payload(final_payload, cache_key, entries, meta, message="统计完成")
        _cache_set(cache_key, final_payload, ttl=24 * 60 * 60)
        _save_missing_episode_stats_cache(final_payload, cache_key, entries)
        _store_missing_episode_progress(final_payload, cache_key)
    except Exception as e:
        logger.warning(f"[Discover] 缺集统计后台任务失败: {e}")
        with _missing_episode_stats_lock:
            _missing_episode_stats_state["error"] = str(e)
    finally:
        with _missing_episode_stats_lock:
            _missing_episode_stats_state["running"] = False


def _merge_missing_episode_libraries(entries: list[dict], results: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    entry_counts: dict[str, int] = {}
    for entry in entries:
        library_id = str(entry.get("library_id") or "")
        library_name = entry.get("library_name") or "未分类媒体库"
        key = library_id or library_name
        entry_counts[key] = entry_counts.get(key, 0) + 1
        library = grouped.setdefault(key, {
            "libraryId": library_id,
            "libraryName": library_name,
            "summary": _empty_missing_episode_summary(),
            "items": [],
        })

    for item in results:
        library_id = str(item.get("libraryId") or "")
        library_name = item.get("libraryName") or "未分类媒体库"
        key = library_id or library_name
        library = grouped.setdefault(key, {
            "libraryId": library_id,
            "libraryName": library_name,
            "summary": _empty_missing_episode_summary(),
            "items": [],
        })
        library["items"].append(item)
        _accumulate_missing_episode_summary(library["summary"], item)

    for key, library in grouped.items():
        library["summary"]["tvCount"] = entry_counts.get(key, int(library["summary"].get("tvCount") or 0))

    libraries = list(grouped.values())
    libraries.sort(key=lambda lib: (
        -int(lib["summary"].get("missingEpisodes") or 0),
        lib.get("libraryName") or "",
    ))
    return libraries


def _build_missing_episode_stats_payload(entries: list[dict], api_key: str, meta: dict) -> dict:
    summary = _empty_missing_episode_summary()
    if not entries:
        return {"ready": True, "meta": meta, "summary": summary, "items": [], "libraries": []}
    max_workers = min(MISSING_EPISODE_TMDB_MAX_WORKERS, max(1, len(entries)))
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_build_missing_episode_stat, entry, api_key) for entry in entries]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                logger.debug(f"[Discover] 缺集统计条目计算失败: {e}")
    results = _sort_missing_episode_results(results)
    for item in results:
        _accumulate_missing_episode_summary(summary, item)
    return {
        "ready": True,
        "meta": meta,
        "summary": summary,
        "items": results,
        "libraries": _build_missing_episode_libraries(results),
    }


def sync_missing_episode_stats_entry(entry: dict | None = None, remove: bool = False, tmdb_id: str = "", library_id: str = "") -> bool:
    """Patch the persisted missing-episode statistics for one Series."""
    cached_payload = _load_missing_episode_stats_cache()
    if not cached_payload:
        cached_payload, legacy_version = _load_latest_missing_episode_stats_payload_any_version()
        if cached_payload:
            logger.info(f"[Discover] 缺集统计单剧缓存使用旧版完整缓存作为底稿: v{legacy_version}")
    if not cached_payload:
        with _missing_episode_stats_lock:
            cached_payload = _missing_episode_stats_state.get("payload")
    if not isinstance(cached_payload, dict) or cached_payload.get("running"):
        logger.info("[Discover] 缺集统计单剧缓存未更新: 当前没有已完成的缺集统计缓存")
        return False

    meta = get_discover_index_meta()
    entries = _filter_rss_real_library_entries(get_discover_series_entries(), meta)
    entry_keys = {_missing_episode_entry_key(item) for item in entries}
    target_key = _missing_episode_entry_key(entry or {"tmdb_id": tmdb_id, "library_id": library_id})
    if target_key == ("", ""):
        logger.info("[Discover] 缺集统计单剧缓存未更新: 缺少 TMDB ID 和媒体库 ID")
        return False
    target_tmdb_id, target_library_id = target_key

    results = []
    for item in cached_payload.get("items") or []:
        item_key = _missing_episode_result_key(item)
        is_target = item_key[0] == target_tmdb_id and (not target_library_id or item_key[1] == target_library_id)
        if is_target:
            continue
        if item_key in entry_keys:
            results.append(item)

    if not remove:
        if not entry:
            entry = next((item for item in entries if _missing_episode_entry_key(item) == target_key), None)
        if not entry or _missing_episode_entry_key(entry) not in entry_keys:
            logger.info(
                f"[Discover] 缺集统计单剧缓存未更新: 目标剧集不在当前统计索引中 "
                f"TMDB={target_tmdb_id} Library={target_library_id}"
            )
            return False
        api_key = _get_tmdb_key()
        if not api_key:
            logger.info("[Discover] 缺集统计单剧缓存未更新: 未配置 TMDB API Key")
            return False
        results.append(_build_missing_episode_stat(entry, api_key))

    results = _sort_missing_episode_results(results)
    summary = _empty_missing_episode_summary()
    for item in results:
        _accumulate_missing_episode_summary(summary, item)
    summary["tvCount"] = len(entries)
    cache_key = _build_missing_episode_cache_key(entries, meta)
    final_payload = {
        "ready": True,
        "running": False,
        "meta": meta,
        "summary": summary,
        "items": results,
        "libraries": _merge_missing_episode_libraries(entries, results),
        "progress": {"current": len(entries), "total": len(entries)},
        "message": "统计完成",
    }
    final_payload = _decorate_missing_episode_payload(final_payload, cache_key, entries, meta, message="统计完成")
    _cache_set(cache_key, final_payload, ttl=24 * 60 * 60)
    _save_missing_episode_stats_cache(final_payload, cache_key, entries)
    _store_missing_episode_progress(final_payload, cache_key)
    publish_realtime_event("missing_episode_stats_updated", {
        "action": "removed" if remove else "updated",
        "media_type": "tv",
        "tmdb_id": target_tmdb_id,
        "library_id": target_library_id,
        "cache_key": cache_key,
    })
    logger.info(
        f"[Discover] 缺集统计单剧缓存已写入: TMDB={target_tmdb_id} "
        f"Library={target_library_id or 'all'} remove={remove}"
    )
    return True


@router.get("/library/missing-episode-stats")
def library_missing_episode_stats(
    refresh: int = Query(0, ge=0, le=1),
    start: int = Query(0, ge=0, le=1),
    summary_only: int = Query(0, ge=0, le=1),
):
    wants_summary = bool(summary_only)
    meta = get_discover_index_meta()
    if not get_discover_index_ready():
        cached = _load_missing_episode_stats_cache(summary_only=wants_summary)
        if cached:
            return cached
        return {
            "ready": False,
            "meta": meta,
            "summary": _empty_missing_episode_summary(),
            "items": [],
            "libraries": [],
            "message": "Emby 媒体库索引正在构建，请稍后刷新。",
        }
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    entries = _filter_rss_real_library_entries(get_discover_series_entries(), meta)
    cache_key = _build_missing_episode_cache_key(entries, meta)
    if wants_summary and not start and not refresh:
        with _missing_episode_stats_lock:
            if _missing_episode_stats_state.get("payload") and _missing_episode_stats_state.get("running"):
                return _build_missing_episode_summary_payload(_missing_episode_stats_state["payload"])
        cached_summary = _load_or_backfill_missing_episode_stats_summary(cache_key, entries)
        if cached_summary and cached_summary.get("previewVersion") == MISSING_EPISODE_SUMMARY_PREVIEW_VERSION:
            return cached_summary
    with _missing_episode_stats_lock:
        if _missing_episode_stats_state.get("payload") and _missing_episode_stats_state.get("running"):
            payload = _missing_episode_stats_state["payload"]
            return _build_missing_episode_summary_payload(payload) if wants_summary else payload

    if start or refresh:
        message = "正在校准 Emby 剧集索引..." if refresh else "正在统计剧集缺集..."
        initial_payload = _build_missing_episode_progress_payload(entries, meta, running=True, message=message)
        with _missing_episode_stats_lock:
            _missing_episode_stats_state.update({
                "cache_key": cache_key,
                "running": True,
                "payload": initial_payload,
                "progress": initial_payload["progress"],
                "error": "",
            })
        worker = threading.Thread(
            target=_run_missing_episode_stats_job,
            args=(entries, api_key, meta, cache_key, bool(refresh)),
            daemon=True,
        )
        worker.start()
        return _build_missing_episode_summary_payload(initial_payload) if wants_summary else initial_payload

    if not refresh:
        cached = None if wants_summary else _cache_get(cache_key)
        if cached:
            return cached
        if wants_summary:
            cached = _load_or_backfill_missing_episode_stats_summary(cache_key, entries)
        else:
            cached = _load_missing_episode_stats_cache(cache_key)
        if cached:
            cached_key = (cached.get("meta") or {}).get("missing_stats_cache_key") or cache_key
            if cached_key == cache_key and not wants_summary:
                _cache_set(cache_key, cached, ttl=24 * 60 * 60)
                if (cached.get("meta") or {}).get("missing_stats_cache_migrated_from"):
                    _save_missing_episode_stats_cache(cached, cache_key, entries)
            with _missing_episode_stats_lock:
                _missing_episode_stats_state.update({
                    "cache_key": cached_key,
                    "running": False,
                    "payload": _missing_episode_stats_state.get("payload") if wants_summary else cached,
                    "progress": cached.get("progress") or {"current": 0, "total": 0},
                    "error": "",
                })
            return cached
    with _missing_episode_stats_lock:
        if _missing_episode_stats_state.get("cache_key") == cache_key and _missing_episode_stats_state.get("payload"):
            payload = _missing_episode_stats_state["payload"]
            return _build_missing_episode_summary_payload(payload) if wants_summary else payload
    payload = _build_missing_episode_progress_payload(entries, meta, running=False, message="点击开始统计")
    return _build_missing_episode_summary_payload(payload) if wants_summary else payload


@router.get("/tv/{tmdb_id}/season/{season_num}")
async def season_detail(tmdb_id: int, season_num: int):
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    cache_key = f"season_{tmdb_id}_{season_num}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = tmdb.get_season_details_tmdb(tmdb_id, season_num, api_key)
    if not data:
        raise HTTPException(404, "未找到季信息")
    _cache_set(cache_key, data, ttl=3600)
    return data

# ========== 搜索 ==========

@router.get("/search")
def search_media(query: str = Query(..., min_length=1), type: str = Query("movie"), page: int = Query(1, ge=1)):
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    normalized_type = "tv" if str(type or "").lower() in {"tv", "series"} else "movie"
    normalized_query = str(query or "").strip()
    if page == 1 and normalized_query.isdigit():
        tmdb_id = int(normalized_query)
        if normalized_type == "tv":
            data = tmdb.get_tv_details(tmdb_id, api_key)
        else:
            data = tmdb.get_movie_details(tmdb_id, api_key)
        if not data:
            return {"items": [], "total_pages": 1, "page": page}
        item = _normalize_tmdb_item(data)
        item["media_type"] = normalized_type
        _mark_library_exists_on_items([item])
        return {"items": [item], "total_pages": 1, "page": page}
    data = tmdb.search_media_for_discover(query, api_key, item_type=type, page=page)
    if not data:
        return {"items": [], "total_pages": 0, "page": page}
    items = [_normalize_tmdb_item(i) for i in data.get("results", [])]
    for item in items:
        item["media_type"] = type
    _mark_library_exists_on_items(items)
    return {"items": items, "total_pages": data.get("total_pages", 1), "page": page}


@router.post("/library/exists")
def check_library_exists(items: list[dict]):
    results = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        media_type = _normalize_discover_media_type(item.get("media_type"))
        key = item.get("_existence_key")
        tmdb_id = str(item.get("tmdb_id") or item.get("_tmdb_id") or "").strip()
        if not tmdb_id and item.get("source") in {"tmdb", "themoviedb"}:
            tmdb_id = str(item.get("id") or "").strip()
        if not key:
            key = f"{tmdb_id}:{media_type}" if tmdb_id else ""
        if key:
            results[key] = _item_exists_in_discover_index(item)
    return {"results": results}

# ========== 外部来源→TMDB 批量解析 ==========

@router.post("/resolve_tmdb")
async def resolve_douban_to_tmdb(items: list[dict]):
    """
    批量将外部来源条目解析为 TMDB ID。
    输入: [{"title": "xxx", "year": "2024", "media_type": "movie"}, ...]
    返回: {"results": {"xxx_2024": 12345, ...}}
    """
    import asyncio
    api_key = _get_tmdb_key()
    if not api_key:
        return {"results": {}}

    async def _resolve_one(item):
        raw_title = item.get("title", "") or item.get("name", "")
        title, season_num = _extract_season_from_title(raw_title)
        title = title or raw_title
        for prefix in ("电视剧", "电影", "纪录片", "综艺节目", "综艺"):
            if title.startswith(prefix):
                title = title[len(prefix):].strip()
                break
        normalized_search_title = _normalize_library_title(title)
        if normalized_search_title:
            title = normalized_search_title
        year = str(item.get("year", "") or "")[:4]
        media_type = _normalize_discover_media_type(item.get("media_type", "movie"))
        if not title:
            return None, None, None
        emby_tmdb_id = lookup_discover_tmdb_id(title, year, media_type)
        if emby_tmdb_id:
            return item.get("_key"), int(emby_tmdb_id), None
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            lambda: tmdb.search_media_for_discover(title, api_key, item_type=media_type, page=1)
        )
        if not data or not data.get("results"):
            return item.get("_key"), None, None
        results = data["results"]
        if year and year.isdigit():
            year_val = int(year)
            if media_type == "movie":
                for cand in results:
                    cand_date = cand.get("release_date", "")
                    if cand_date and len(cand_date) >= 4:
                        try:
                            if int(cand_date[:4]) == year_val:
                                return item.get("_key"), cand.get("id"), cand.get("vote_average")
                        except ValueError:
                            pass
                return item.get("_key"), None, None
            else:
                if season_num:
                    for cand in results[:3]:
                        cand_id = cand.get("id")
                        if not cand_id:
                            continue
                        season_data = await loop.run_in_executor(
                            None,
                            lambda sid=cand_id: tmdb.get_season_details_tmdb(sid, season_num, api_key, append_to_response=None)
                        )
                        if season_data:
                            season_date = season_data.get("air_date", "")
                            if season_date and len(season_date) >= 4:
                                try:
                                    if int(season_date[:4]) == year_val:
                                        return item.get("_key"), cand_id, cand.get("vote_average")
                                except ValueError:
                                    pass
                    return item.get("_key"), None, None
                for cand in results:
                    cand_date = cand.get("first_air_date", "")
                    if cand_date and len(cand_date) >= 4:
                        try:
                            if int(cand_date[:4]) == year_val:
                                return item.get("_key"), cand.get("id"), cand.get("vote_average")
                        except ValueError:
                            pass
                return item.get("_key"), None, None
        return item.get("_key"), results[0].get("id"), results[0].get("vote_average")

    tasks = [_resolve_one(item) for item in items[:30]]
    results_list = await asyncio.gather(*tasks, return_exceptions=True)
    results = {}
    ratings = {}
    for r in results_list:
        if isinstance(r, Exception) or r is None:
            continue
        key, tmdb_id, rating = r
        if key and tmdb_id:
            results[str(key)] = tmdb_id
            ratings[str(key)] = rating
    return {"results": results, "ratings": ratings}

# ========== 类型筛选 + 今日推荐 ==========

@router.get("/genres")
def get_genres():
    """合并电影+剧集类型列表，缓存 24h"""
    cache_key = "genres_all"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    movie_genres = tmdb.get_movie_genres_tmdb(api_key) or []
    tv_genres = tmdb.get_tv_genres_tmdb(api_key) or []
    all_genres = {}
    for g in movie_genres:
        all_genres[g["id"]] = {**g, "media_type": "movie"}
    for g in tv_genres:
        if g["id"] not in all_genres:
            all_genres[g["id"]] = {**g, "media_type": "tv"}
        else:
            all_genres[g["id"]]["media_type"] = "both"
    result = {"genres": list(all_genres.values())}
    _cache_set(cache_key, result, ttl=86400)
    return result


@router.get("/discover_by_genre")
def discover_by_genre(genre_id: int = Query(...), media_type: str = Query("movie"), page: int = Query(1, ge=1)):
    """按类型筛选，缓存 30min"""
    cache_key = f"genre_{genre_id}_{media_type}_{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    params = {"with_genres": genre_id, "page": page}
    if media_type == "tv":
        data = tmdb.discover_tv_tmdb(api_key, params)
    else:
        data = tmdb.discover_movie_tmdb(api_key, params)
    if not data:
        raise HTTPException(500, "TMDB 请求失败")
    items = [_normalize_tmdb_item(i) for i in data.get("results", [])]
    for i in items:
        i["media_type"] = media_type
    _mark_library_exists_on_items(items)
    result = {"items": items, "total_pages": data.get("total_pages", 1), "page": page}
    _cache_set(cache_key, result)
    return result


@router.get("/today_picks")
def today_picks():
    """今日推荐：随机选 20 条热门电影，每天内容不同，缓存到当天结束"""
    import datetime
    today = datetime.date.today().isoformat()
    cache_key = f"today_picks_{today}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    api_key = _get_tmdb_key()
    if not api_key:
        raise HTTPException(400, "未配置 TMDB API Key")
    # 用当天日期作为随机种子，保证每天固定
    rng = random.Random(today)
    page = rng.randint(1, 5)
    data = tmdb.get_popular_movies_tmdb(api_key, {"page": page})
    if not data:
        raise HTTPException(500, "TMDB 请求失败")
    items = [_normalize_tmdb_item(i) for i in data.get("results", [])]
    for i in items:
        i["media_type"] = "movie"
    rng.shuffle(items)
    items = items[:20]
    _mark_library_exists_on_items(items)
    # 缓存到当天结束
    now = time.time()
    tomorrow = now + 86400 - (now % 86400)
    ttl = max(int(tomorrow - now), 3600)
    result = {"items": items}
    _cache_set(cache_key, result, ttl=ttl)
    return result


# ========== TMDB 图片代理 ==========

_tmdb_img_cache: OrderedDict[str, tuple] = OrderedDict()  # path -> (bytes, content_type, expiry)
_task_cover_cache: OrderedDict[str, tuple] = OrderedDict()  # key -> (bytes, content_type, expiry)
_img_client: Optional[httpx.AsyncClient] = None
_douban_img_client: Optional[httpx.AsyncClient] = None
_bangumi_img_client: Optional[httpx.AsyncClient] = None
_bangumi_direct_img_client: Optional[httpx.AsyncClient] = None

def _get_img_client() -> httpx.AsyncClient:
    """复用全局 httpx.AsyncClient，避免每次新建连接"""
    global _img_client
    if _img_client is None or _img_client.is_closed:
        proxy = _get_proxy_url()
        _img_client = httpx.AsyncClient(verify=False, timeout=20, proxy=proxy or None)
    return _img_client

def _get_douban_img_client() -> httpx.AsyncClient:
    """豆瓣图片专用客户端，不走代理"""
    global _douban_img_client
    if _douban_img_client is None or _douban_img_client.is_closed:
        limits = httpx.Limits(max_connections=80, max_keepalive_connections=40)
        _douban_img_client = httpx.AsyncClient(verify=False, timeout=20, limits=limits)
    return _douban_img_client


def _get_bangumi_img_client(use_proxy: bool = True) -> httpx.AsyncClient:
    global _bangumi_img_client, _bangumi_direct_img_client
    limits = httpx.Limits(max_connections=80, max_keepalive_connections=40)
    if use_proxy:
        proxy = _get_proxy_url()
        if _bangumi_img_client is None or _bangumi_img_client.is_closed:
            _bangumi_img_client = httpx.AsyncClient(verify=False, timeout=20, proxy=proxy or None, limits=limits)
        return _bangumi_img_client
    if _bangumi_direct_img_client is None or _bangumi_direct_img_client.is_closed:
        _bangumi_direct_img_client = httpx.AsyncClient(verify=False, timeout=20, limits=limits)
    return _bangumi_direct_img_client

async def close_img_clients():
    """关闭全局图片代理客户端，在应用关闭时调用"""
    global _img_client, _douban_img_client, _bangumi_img_client, _bangumi_direct_img_client
    if _img_client and not _img_client.is_closed:
        await _img_client.aclose()
    if _douban_img_client and not _douban_img_client.is_closed:
        await _douban_img_client.aclose()
    if _bangumi_img_client and not _bangumi_img_client.is_closed:
        await _bangumi_img_client.aclose()
    if _bangumi_direct_img_client and not _bangumi_direct_img_client.is_closed:
        await _bangumi_direct_img_client.aclose()


def put_task_cover_preview(key: str, img_bytes: bytes, content_type: str = "image/jpeg", ttl_seconds: int = 86400):
    if not key or not img_bytes:
        return
    if len(_task_cover_cache) >= 200:
        _task_cover_cache.popitem(last=False)
    _task_cover_cache[key] = (img_bytes, content_type, time.time() + ttl_seconds)


@router.get("/task_cover")
async def task_cover_preview(key: str = Query(...)):
    entry = _task_cover_cache.get(key)
    if not entry:
        raise HTTPException(404, "封面预览不存在或已过期")
    img_bytes, content_type, expires = entry
    if time.time() >= expires:
        _task_cover_cache.pop(key, None)
        raise HTTPException(404, "封面预览已过期")
    _task_cover_cache.move_to_end(key)
    return Response(content=img_bytes, media_type=content_type, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/tmdb_img")
async def tmdb_image_proxy(path: str = Query(..., description="TMDB 图片路径，如 /pB8BM7pdSp6B6IhKQgzaRbpPpVs.jpg")):
    """代理转发 TMDB 图片，解决国内无法访问 image.tmdb.org 的问题"""
    if not path:
        raise HTTPException(400, "缺少图片路径")

    # 检查内存缓存
    cache_key = path
    if cache_key in _tmdb_img_cache:
        img_bytes, content_type, expires = _tmdb_img_cache[cache_key]
        if time.time() < expires:
            _tmdb_img_cache.move_to_end(cache_key)
            return Response(content=img_bytes, media_type=content_type,
                          headers={"Cache-Control": "public, max-age=86400"})
        del _tmdb_img_cache[cache_key]

    url = f"https://image.tmdb.org/t/p/w500{path}"
    client = _get_img_client()

    try:
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code == 200:
            content_type = resp.headers.get("content-type", "image/jpeg")
            img_bytes = resp.content
            # LRU 淘汰：超过 500 张时移除最旧的
            if len(_tmdb_img_cache) >= 500:
                _tmdb_img_cache.popitem(last=False)
            _tmdb_img_cache[cache_key] = (img_bytes, content_type, time.time() + 86400)
            return Response(content=img_bytes, media_type=content_type,
                          headers={"Cache-Control": "public, max-age=86400"})
        else:
            raise HTTPException(502, f"TMDB 图片获取失败: {resp.status_code}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"图片代理请求失败: {e}")


# ========== 豆瓣 / 哔哩哔哩图片代理 ==========

@router.get("/douban_img")
async def douban_image_proxy(url: str = Query(..., description="豆瓣图片完整 URL")):
    """代理转发豆瓣图片"""
    if not url or ("doubanio.com" not in url and "douban.com" not in url):
        raise HTTPException(400, "无效的豆瓣图片 URL")

    cache_key = url
    if cache_key in _tmdb_img_cache:
        img_bytes, content_type, expires = _tmdb_img_cache[cache_key]
        if time.time() < expires:
            _tmdb_img_cache.move_to_end(cache_key)
            return Response(content=img_bytes, media_type=content_type,
                          headers={"Cache-Control": "public, max-age=86400"})
        del _tmdb_img_cache[cache_key]

    client = _get_douban_img_client()

    try:
        req = client.build_request("GET", url, headers={"Referer": "https://m.douban.com/"})
        resp = await client.send(req, follow_redirects=True, stream=True)
        if resp.status_code != 200:
            await resp.aclose()
            raise HTTPException(502, f"豆瓣图片获取失败: {resp.status_code}")

        content_type = resp.headers.get("content-type", "image/jpeg")

        async def stream_and_cache():
            chunks = []
            completed = False
            try:
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    yield chunk
                completed = True
            finally:
                await resp.aclose()
                if completed and chunks:
                    if len(_tmdb_img_cache) >= 500:
                        _tmdb_img_cache.popitem(last=False)
                    _tmdb_img_cache[cache_key] = (b"".join(chunks), content_type, time.time() + 86400)

        return StreamingResponse(stream_and_cache(), media_type=content_type,
                                 headers={"Cache-Control": "public, max-age=86400"})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"图片代理请求失败: {e}")


@router.get("/bangumi_img")
async def bangumi_image_proxy(url: str = Query(..., description="Bangumi 图片完整 URL")):
    """代理转发 Bangumi 图片，代理异常时回退直连。"""
    if not url or "bgm.tv" not in url:
        raise HTTPException(400, "无效的 Bangumi 图片 URL")

    cache_key = url
    if cache_key in _tmdb_img_cache:
        img_bytes, content_type, expires = _tmdb_img_cache[cache_key]
        if time.time() < expires:
            _tmdb_img_cache.move_to_end(cache_key)
            return Response(content=img_bytes, media_type=content_type,
                          headers={"Cache-Control": "public, max-age=86400"})
        del _tmdb_img_cache[cache_key]

    headers = {"Referer": "https://bgm.tv/", "User-Agent": "Mozilla/5.0"}
    proxy_url = _get_proxy_url()
    attempts = [True, False] if proxy_url else [False]
    last_error = None
    for use_proxy in attempts:
        client = _get_bangumi_img_client(use_proxy=use_proxy)
        try:
            resp = await client.get(url, follow_redirects=True, headers=headers)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                if use_proxy:
                    logger.warning(f"[bangumi] 图片代理请求失败，回退直连: {last_error}")
                    continue
                raise HTTPException(502, f"Bangumi 图片获取失败: {resp.status_code}")
            content_type = resp.headers.get("content-type", "image/jpeg")
            img_bytes = resp.content
            if len(_tmdb_img_cache) >= 500:
                _tmdb_img_cache.popitem(last=False)
            _tmdb_img_cache[cache_key] = (img_bytes, content_type, time.time() + 86400)
            return Response(content=img_bytes, media_type=content_type,
                          headers={"Cache-Control": "public, max-age=86400"})
        except httpx.HTTPError as e:
            last_error = e
            if use_proxy:
                logger.warning(f"[bangumi] 图片代理请求失败，回退直连: {e}")
                continue
            raise HTTPException(502, f"图片代理请求失败: {e}")

    raise HTTPException(502, f"图片代理请求失败: {last_error}")


@router.get("/bili_img")
async def bili_image_proxy(url: str = Query(..., description="哔哩哔哩图片完整 URL")):
    """代理转发哔哩哔哩图片"""
    if not url or "hdslb.com" not in url:
        raise HTTPException(400, "无效的哔哩哔哩图片 URL")

    cache_key = url
    if cache_key in _tmdb_img_cache:
        img_bytes, content_type, expires = _tmdb_img_cache[cache_key]
        if time.time() < expires:
            _tmdb_img_cache.move_to_end(cache_key)
            return Response(content=img_bytes, media_type=content_type,
                          headers={"Cache-Control": "public, max-age=86400"})
        del _tmdb_img_cache[cache_key]

    client = _get_douban_img_client()

    try:
        resp = await client.get(url, follow_redirects=True, headers={"Referer": "https://www.bilibili.com/"})
        if resp.status_code == 200:
            content_type = resp.headers.get("content-type", "image/jpeg")
            img_bytes = resp.content
            if len(_tmdb_img_cache) >= 500:
                _tmdb_img_cache.popitem(last=False)
            _tmdb_img_cache[cache_key] = (img_bytes, content_type, time.time() + 86400)
            return Response(content=img_bytes, media_type=content_type,
                          headers={"Cache-Control": "public, max-age=86400"})
        else:
            raise HTTPException(502, f"哔哩哔哩图片获取失败: {resp.status_code}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"图片代理请求失败: {e}")


# ========== Emby 封面代理（供企业微信抓取）==========

import hmac
import hashlib
import json as _json

from app.routers.config_302 import get_emby_config_by_index_sync

def _emby_cover_sign(server_idx: int, item_id: str, ts: int, secret: str) -> str:
    msg = f"emby_cover:v1:{server_idx}:{item_id}:{ts}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

def build_emby_cover_url(base_url: str, server_idx: int, item_id: str, secret: str) -> str:
    ts = int(time.time())
    sig = _emby_cover_sign(server_idx, item_id, ts, secret)
    return f"{base_url}/api/discover/emby_cover?server_idx={server_idx}&item_id={item_id}&ts={ts}&sig={sig}"

@router.get("/emby_cover")
async def emby_cover_proxy(
    server_idx: int = Query(...),
    item_id: str = Query(...),
    ts: int = Query(...),
    sig: str = Query(...),
):
    """代理 Emby 封面图，供企业微信等外部服务抓取（带 HMAC 签名保护）"""
    import json as _json2
    from core.configs import AUTH_FILE

    # 读取签名密钥
    secret = ""
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            secret = _json2.load(f).get("secret", "")
    except Exception:
        pass
    if not secret:
        raise HTTPException(403, "签名密钥未配置")

    # 验证签名
    expected = _emby_cover_sign(server_idx, item_id, ts, secret)
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(403, "签名无效")

    # 验证有效期（24小时）
    if abs(time.time() - ts) > 86400:
        raise HTTPException(403, "链接已过期")

    # 读取 Emby 服务器配置
    svr = get_emby_config_by_index_sync(server_idx)
    if not svr or not svr.get("enabled", True):
        raise HTTPException(404, "服务器配置不存在")

    # 检查缓存
    cache_key = f"emby_cover:{server_idx}:{item_id}"
    if cache_key in _tmdb_img_cache:
        img_bytes, content_type, expires = _tmdb_img_cache[cache_key]
        if time.time() < expires:
            _tmdb_img_cache.move_to_end(cache_key)
            return Response(content=img_bytes, media_type=content_type,
                            headers={"Cache-Control": "public, max-age=86400"})
        del _tmdb_img_cache[cache_key]

    # 用 EmbyClient 拉取封面字节
    from core.emby_client import EmbyClient
    client = EmbyClient(svr["url"], svr["key"], svr.get("public_host"))
    img_bytes = client.download_cover(item_id)
    client.close()

    if not img_bytes:
        raise HTTPException(404, "封面图片不存在")

    content_type = "image/png" if img_bytes.startswith(b'\x89PNG') else "image/jpeg"
    if len(_tmdb_img_cache) >= 500:
        _tmdb_img_cache.popitem(last=False)
    _tmdb_img_cache[cache_key] = (img_bytes, content_type, time.time() + 86400)
    return Response(content=img_bytes, media_type=content_type,
                    headers={"Cache-Control": "public, max-age=86400"})
