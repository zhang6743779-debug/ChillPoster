# app/routers/discover.py
# 发现推荐页 API — 对接 TMDB / 豆瓣 / 扩展数据源

import os
import re
import sys
import time
import types
import logging
import random
import importlib.util
from collections import OrderedDict
from datetime import datetime
from typing import Optional, Dict, List, Any

import httpx
import requests
from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import Response, RedirectResponse, StreamingResponse

from core.configs import global_config
from core.media_library_cache import load_cache
from core import tmdb
from core.douban import DoubanApi

logger = logging.getLogger("Discover")
router = APIRouter(prefix="/api/discover", tags=["Discover"])

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

_LIBRARY_TMDB_MARKER_RE = re.compile(r"(?:tmdb(?:id)?[-=: ]*|tmdb-)(\d+)", re.IGNORECASE)
_LIBRARY_TMDB_INDEX_TTL = 60
_library_tmdb_index_cache: tuple[float, dict] | None = None

# ========== 工具函数 ==========

def _normalize_discover_media_type(value: Any) -> str:
    value = str(value or "").strip().lower()
    if value in {"tv", "series", "show", "电视剧", "剧集", "番剧", "动漫", "动画", "综艺", "纪录片", "少儿"}:
        return "tv"
    return "movie"


def _extract_library_tmdb_ids(text: str) -> set[str]:
    return {match.group(1) for match in _LIBRARY_TMDB_MARKER_RE.finditer(str(text or "")) if match.group(1)}


def _infer_library_item_media_type(item: dict, task_key: str, items: dict) -> str:
    path = str(item.get("path", "") or "")
    name = str(item.get("name", "") or "")
    if re.search(r"(?:^|/)(电影|影片|Movie|Movies)(?:/|$)", path, re.IGNORECASE):
        return "movie"
    if re.search(r"(?:^|/)(Season\s*\d+|S\d{1,2})(?:/|$)", path, re.IGNORECASE):
        return "tv"
    if re.search(r"(?:^|/)(剧集|电视剧|番剧|动漫|动画|综艺|纪录片|电视)(?:/|$)", path):
        return "tv"

    try:
        parent_id = int(item.get("parent_id", 0) or 0)
    except (TypeError, ValueError):
        parent_id = 0
    if parent_id:
        parent = items.get(str(parent_id)) or items.get(parent_id)
        if isinstance(parent, dict):
            parent_text = f"{parent.get('path', '')}/{parent.get('name', '')}"
            if re.search(r"Season\s*\d+|S\d{1,2}", parent_text, re.IGNORECASE):
                return "tv"

    task_path = str(task_key or "").split(":", 1)[-1]
    combined = f"{task_path}/{path}/{name}"
    if re.search(r"(?:^|/)(剧集|电视剧|番剧|动漫|动画|综艺|纪录片|电视)(?:/|$)", combined):
        return "tv"
    return "movie"


def _get_library_tmdb_index() -> dict:
    global _library_tmdb_index_cache
    now = time.time()
    if _library_tmdb_index_cache and now - _library_tmdb_index_cache[0] < _LIBRARY_TMDB_INDEX_TTL:
        return _library_tmdb_index_cache[1]

    index = {"movie": set(), "tv": set()}
    cache = load_cache()
    for task_key, task in (cache.get("tasks") or {}).items():
        items = task.get("items") or {}
        for item in items.values():
            if not isinstance(item, dict):
                continue
            tmdb_ids = _extract_library_tmdb_ids(f"{item.get('path', '')} {item.get('name', '')}")
            if not tmdb_ids:
                continue
            media_type = _infer_library_item_media_type(item, str(task_key), items)
            index.setdefault(media_type, set()).update(tmdb_ids)

    _library_tmdb_index_cache = (now, index)
    return index


def _mark_library_exists_on_items(items: list[dict]):
    if not items:
        return
    index = _get_library_tmdb_index()
    for item in items:
        if not isinstance(item, dict):
            continue
        tmdb_id = item.get("_tmdb_id") or item.get("tmdb_id")
        if not tmdb_id and item.get("source") in {"tmdb", "themoviedb"}:
            tmdb_id = item.get("id")
        if not tmdb_id:
            item["exists_in_library"] = False
            continue
        tmdb_id = str(tmdb_id)
        media_type = _normalize_discover_media_type(item.get("media_type"))
        item["exists_in_library"] = tmdb_id in index.get(media_type, set())


# 内置 TMDB API Key（MoviePilot 同款默认 key，用户可在设置中覆盖）
_BUILTIN_TMDB_KEY = "db55323b8d3e4154498498a75642b381"

def _get_proxy_url() -> str:
    """获取用户配置的代理地址"""
    return global_config.proxy_url or ""

def _get_tmdb_key() -> str:
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
    index = _get_library_tmdb_index()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        tmdb_id = str(item.get("tmdb_id") or item.get("_tmdb_id") or item.get("id") or "").strip()
        if not tmdb_id:
            continue
        media_type = _normalize_discover_media_type(item.get("media_type"))
        exists = tmdb_id in index.get(media_type, set())
        results[f"{tmdb_id}:{media_type}"] = exists
    return {"results": results}

# ========== 豆瓣→TMDB 批量解析 ==========

@router.post("/resolve_tmdb")
async def resolve_douban_to_tmdb(items: list[dict]):
    """
    批量将豆瓣条目解析为 TMDB ID。
    输入: [{"title": "xxx", "year": "2024", "media_type": "movie"}, ...]
    返回: {"results": {"xxx_2024": 12345, ...}}
    """
    import asyncio
    api_key = _get_tmdb_key()
    if not api_key:
        return {"results": {}}

    async def _resolve_one(item):
        title = item.get("title", "")
        year = item.get("year", "")
        media_type = item.get("media_type", "movie")
        if not title:
            return None, None, None
        loop = asyncio.get_event_loop()
        params = {"query": title}
        if year and year.isdigit():
            params["year"] = year if media_type == "movie" else year
        data = await loop.run_in_executor(
            None,
            lambda: tmdb.search_media_for_discover(title, api_key, item_type=media_type, page=1)
        )
        if data and data.get("results"):
            results = data["results"]
            # 年份校验：优先匹配年份差 ≤1 的候选
            year_str = item.get("year", "")
            if year_str and year_str.isdigit():
                year_val = int(year_str)
                date_key = "release_date" if media_type == "movie" else "first_air_date"
                for cand in results:
                    cand_date = cand.get(date_key, "")
                    if cand_date and len(cand_date) >= 4:
                        try:
                            cand_year = int(cand_date[:4])
                            if abs(cand_year - year_val) <= 1:
                                return item.get("_key"), cand.get("id"), cand.get("vote_average")
                        except ValueError:
                            pass
                # 年份都不匹配，跳过该条目
                return item.get("_key"), None, None
            # 无年份信息，降级接受第一个
            first = results[0]
            return item.get("_key"), first.get("id"), first.get("vote_average")
        return item.get("_key"), None, None

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
