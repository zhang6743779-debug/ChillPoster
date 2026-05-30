"""
TMDb/media-organize helpers extracted from app.routers.media_organize.

Pure helper functions for filename parsing, TMDb search, title normalization,
and scraping-config construction.  No FastAPI dependencies.
"""

import os
import re
import json
import hashlib
import asyncio
import time as _time
import threading
from pathlib import Path
from typing import Optional

import requests

from core.logger import logger
from core.meta.string import StringUtils

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WEAK_MATCH_STOPWORDS = {
    "alternative", "ending", "extended", "cut", "edition", "ultimate",
    "director", "version", "uncut", "remastered",
}

_ROMAN_MAP = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
}

_DIRECT_TMDB_ID_CACHE_TTL_SECONDS = 30
_CORRECTED_TMDB_ID_CACHE_TTL_SECONDS = 6 * 60 * 60
_DIRECT_TMDB_ID_CACHE_MAX_SIZE = 5000
_DIRECT_TMDB_ID_CACHE_LOCK = threading.Lock()
_DIRECT_TMDB_ID_CACHE: dict[tuple, tuple[float, tuple[Optional[int], str]]] = {}
_TMDB_SQLITE_CACHE_LOCK = threading.Lock()
_TMDB_SQLITE_CACHE_READY = False
_TMDB_DETAIL_CACHE_MOVIE_TTL_SECONDS = 30 * 24 * 60 * 60
_TMDB_DETAIL_CACHE_ENDED_TV_TTL_SECONDS = 30 * 24 * 60 * 60
_TMDB_DETAIL_CACHE_ACTIVE_TV_TTL_SECONDS = 24 * 60 * 60
_TMDB_SEARCH_CACHE_HIT_TTL_SECONDS = 30 * 24 * 60 * 60
_TMDB_SEARCH_CACHE_MISS_TTL_SECONDS = 6 * 60 * 60
_EXPLICIT_SEASON_OR_EPISODE_MARKER_RE = re.compile(
    r'(?i)(\bS\d{1,3}E[P]?\d{1,4}\b|\bS\d{1,3}\b|\bE[P]?\d{1,4}\b|Episode\s+\d{1,4}|Season\s+\d{1,3}|[第\s]*\d{1,4}\s*[集话話期幕]|第\s*[0-9一二三四五六七八九十百零]+\s*季)'
)
_TOTAL_EPISODE_COUNT_RE = re.compile(
    r'(?i)([全共]\s*[0-9一二三四五六七八九十百零]+\s*[集话話期幕]|(?<![第\dA-Za-z])[0-9一二三四五六七八九十百零]+\s*[集话話期幕]\s*全)'
)


def _build_direct_tmdb_cache_key(title_key: tuple, media_type: str, file_path: str) -> tuple:
    parent_dir = os.path.basename(os.path.dirname(file_path)) if file_path else ""
    grandparent_dir = os.path.basename(os.path.dirname(os.path.dirname(file_path))) if file_path else ""
    return title_key + (media_type, parent_dir, grandparent_dir)


def _prune_direct_tmdb_id_cache(now_ts: float):
    stale_keys = [
        key for key, (ts, value) in _DIRECT_TMDB_ID_CACHE.items()
        if now_ts - ts > (value[2] if len(value) > 2 else _DIRECT_TMDB_ID_CACHE_TTL_SECONDS)
    ]
    for key in stale_keys:
        _DIRECT_TMDB_ID_CACHE.pop(key, None)

    if len(_DIRECT_TMDB_ID_CACHE) <= _DIRECT_TMDB_ID_CACHE_MAX_SIZE:
        return

    sorted_items = sorted(_DIRECT_TMDB_ID_CACHE.items(), key=lambda item: item[1][0], reverse=True)
    _DIRECT_TMDB_ID_CACHE.clear()
    for key, value in sorted_items[:_DIRECT_TMDB_ID_CACHE_MAX_SIZE]:
        _DIRECT_TMDB_ID_CACHE[key] = value


def _get_cached_direct_tmdb_id(cache_key: tuple) -> tuple[Optional[int], str]:
    if not cache_key:
        return None, ""
    now_ts = _time.time()
    with _DIRECT_TMDB_ID_CACHE_LOCK:
        _prune_direct_tmdb_id_cache(now_ts)
        cached = _DIRECT_TMDB_ID_CACHE.get(cache_key)
        if not cached:
            return None, ""
        ts, value = cached
        ttl = value[2] if len(value) > 2 else _DIRECT_TMDB_ID_CACHE_TTL_SECONDS
        if now_ts - ts > ttl:
            _DIRECT_TMDB_ID_CACHE.pop(cache_key, None)
            return None, ""
        _DIRECT_TMDB_ID_CACHE[cache_key] = (now_ts, value)
        return value[0], value[1]


def _set_cached_direct_tmdb_id(cache_key: tuple, tmdb_id_direct: Optional[int], tmdb_id_source: str, ttl_seconds: int = _DIRECT_TMDB_ID_CACHE_TTL_SECONDS):
    if not cache_key or not tmdb_id_direct:
        return
    now_ts = _time.time()
    with _DIRECT_TMDB_ID_CACHE_LOCK:
        _prune_direct_tmdb_id_cache(now_ts)
        _DIRECT_TMDB_ID_CACHE[cache_key] = (now_ts, (tmdb_id_direct, tmdb_id_source, ttl_seconds))


def _cache_corrected_tmdb_id(parsed: Optional[dict], corrected_tmdb_id: Optional[int]):
    if not parsed or not corrected_tmdb_id:
        return
    cache_key = _build_direct_tmdb_cache_key(
        parsed.get("title_key"),
        parsed.get("media_type", ""),
        parsed.get("file_path", ""),
    )
    _set_cached_direct_tmdb_id(
        cache_key,
        int(corrected_tmdb_id),
        "404_corrected",
        ttl_seconds=_CORRECTED_TMDB_ID_CACHE_TTL_SECONDS,
    )


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


async def _load_config_data() -> dict:
    CONFIG_FILE = "config/media_organize.json"
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                try:
                    from app.routers.media_organize import _apply_default_scrape_fields
                    return _apply_default_scrape_fields(data)
                except Exception:
                    return data
        except Exception:
            pass
    return {}


def _build_scraping_config(config_data: dict) -> "ScrapingConfig":
    from core.organizer import ScrapingConfig, ScrapingPolicy

    policy_map = {
        'missing_only': ScrapingPolicy.MISSING_ONLY,
        'overwrite': ScrapingPolicy.OVERWRITE,
        'skip': ScrapingPolicy.SKIP,
    }

    config = ScrapingConfig()

    for media_type in ['movie', 'tv', 'season', 'episode']:
        for meta_type in ['nfo', 'poster', 'backdrop', 'logo', 'banner', 'thumb']:
            key = f"policy_{meta_type}"
            # 兼容前端 fanart -> 后端 backdrop 的命名差异
            if meta_type == 'backdrop':
                raw = config_data.get(key, config_data.get('policy_fanart', 'missing_only'))
            else:
                raw = config_data.get(key, 'missing_only')
            policy = policy_map.get(raw, ScrapingPolicy.MISSING_ONLY)

            enabled_key = f"scrape_{meta_type}"
            if meta_type == 'backdrop':
                enabled = config_data.get(enabled_key, config_data.get('scrape_fanart', True))
            else:
                enabled = config_data.get(enabled_key, True)
            if not enabled:
                policy = ScrapingPolicy.SKIP

            # set_policy 的 key 必须和 MetadataType.value 一致
            # MetadataType.BACKDROP.value = "fanart"，所以需要映射
            policy_key = "fanart" if meta_type == "backdrop" else meta_type
            config.set_policy(media_type, policy_key, policy)

    def apply_specific_policy(
        media_type: str,
        metadata_type: str,
        policy_key: str,
        enabled_key: str,
        fallback_policy_key: str,
        fallback_enabled_key: str,
    ):
        raw = config_data.get(policy_key, config_data.get(fallback_policy_key, 'missing_only'))
        policy = policy_map.get(raw, ScrapingPolicy.MISSING_ONLY)
        enabled = config_data.get(enabled_key, config_data.get(fallback_enabled_key, True))
        if not enabled:
            policy = ScrapingPolicy.SKIP
        config.set_policy(media_type, metadata_type, policy)

    # 专用配置项覆盖通用配置，主要用于 STRM 全量同步的“补齐缺失 TMDb 元数据”。
    apply_specific_policy('season', 'poster', 'policy_season_poster', 'scrape_season_poster', 'policy_poster', 'scrape_poster')
    apply_specific_policy('episode', 'thumb', 'policy_episode_thumb', 'scrape_episode_thumb', 'policy_thumb', 'scrape_thumb')

    return config


# ---------------------------------------------------------------------------
# TMDb data fetching
# ---------------------------------------------------------------------------


def _ensure_tmdb_sqlite_cache_schema() -> bool:
    global _TMDB_SQLITE_CACHE_READY
    if _TMDB_SQLITE_CACHE_READY:
        return True
    with _TMDB_SQLITE_CACHE_LOCK:
        if _TMDB_SQLITE_CACHE_READY:
            return True
        try:
            from core.cache_db import tmdb_cache_db as cache_db
            with cache_db(write=True) as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tmdb_detail_cache (
                        media_type TEXT NOT NULL,
                        tmdb_id INTEGER NOT NULL,
                        fetched_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT '',
                        episode_keys_json TEXT NOT NULL DEFAULT '[]',
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (media_type, tmdb_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_tmdb_detail_cache_expires
                        ON tmdb_detail_cache(expires_at);

                    CREATE TABLE IF NOT EXISTS tmdb_search_cache (
                        cache_key TEXT PRIMARY KEY,
                        fetched_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        hit INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_tmdb_search_cache_expires
                        ON tmdb_search_cache(expires_at);
                    """
                )
            _TMDB_SQLITE_CACHE_READY = True
            return True
        except Exception as e:
            logger.debug(f"[TMDbCache] 初始化 SQLite 缓存失败，跳过缓存: {e}")
            return False


def _tmdb_episode_key(season: Optional[int], episode: Optional[int]) -> str:
    try:
        if season is None or episode is None:
            return ""
        season_num = int(season)
        episode_num = int(episode)
        if season_num <= 0 or episode_num <= 0:
            return ""
        return f"S{season_num}E{episode_num}"
    except Exception:
        return ""


def _normalize_required_episodes(required_episodes: Optional[list[tuple]], parsed: Optional[dict], season_number: Optional[int]) -> list[str]:
    keys: list[str] = []
    seen = set()

    def _add(season, episode):
        key = _tmdb_episode_key(season, episode)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)

    for item in required_episodes or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        _add(item[0], item[1])

    if parsed:
        _add(season_number if season_number is not None else parsed.get("season"), parsed.get("episode"))
    return keys


def _tmdb_data_episode_keys(data: Optional[dict]) -> list[str]:
    if not isinstance(data, dict):
        return []
    episodes = data.get("episodes_details")
    if not isinstance(episodes, dict):
        return []
    return sorted(str(key) for key in episodes.keys() if key)


def _tmdb_data_has_required_episodes(data: Optional[dict], required_episode_keys: list[str]) -> bool:
    if not required_episode_keys:
        return True
    episode_keys = set(_tmdb_data_episode_keys(data))
    return all(key in episode_keys for key in required_episode_keys)


def _tmdb_series_is_ended(data: Optional[dict]) -> bool:
    if not isinstance(data, dict):
        return False
    source = data.get("series_details") if isinstance(data.get("series_details"), dict) else data
    status = str((source or {}).get("status") or "").strip().lower()
    return status in {"ended", "canceled", "cancelled"}


def _validate_tmdb_tv_episode(tmdb_data: Optional[dict], season_number, episode_number) -> dict:
    """Validate that a parsed TV season/episode exists in the fetched TMDb aggregate."""
    result = {
        "ok": True,
        "message": "",
        "season": season_number,
        "episode": episode_number,
    }
    if not isinstance(tmdb_data, dict):
        return result
    if "series_details" not in tmdb_data:
        return result
    if season_number is None or episode_number is None:
        return result

    try:
        season_num = int(season_number)
        episode_num = int(episode_number)
    except (TypeError, ValueError):
        return {
            **result,
            "ok": False,
            "message": "季集号不是有效数字",
        }

    result["season"] = season_num
    result["episode"] = episode_num
    if season_num < 0 or episode_num <= 0:
        return {
            **result,
            "ok": False,
            "message": f"季集号无效：S{season_num:02d}E{episode_num:02d}",
        }

    series = tmdb_data.get("series_details") or {}
    season_summaries = series.get("seasons") if isinstance(series, dict) else []
    season_summary = None
    if isinstance(season_summaries, list):
        for item in season_summaries:
            if not isinstance(item, dict):
                continue
            try:
                if int(item.get("season_number")) == season_num:
                    season_summary = item
                    break
            except (TypeError, ValueError):
                continue
        known_season_numbers = {
            int(item.get("season_number"))
            for item in season_summaries
            if isinstance(item, dict) and str(item.get("season_number", "")).lstrip("-").isdigit()
        }
        if known_season_numbers and season_num not in known_season_numbers:
            return {
                **result,
                "ok": False,
                "message": f"TMDb 中不存在第 {season_num} 季",
            }

    episode_key = f"S{season_num}E{episode_num}"
    episodes = tmdb_data.get("episodes_details")
    if isinstance(episodes, dict):
        if episode_key in episodes:
            return result
        season_episode_numbers = []
        for key in episodes.keys():
            match = re.match(r"^S(-?\d+)E(\d+)$", str(key or ""))
            if not match:
                continue
            try:
                if int(match.group(1)) == season_num:
                    season_episode_numbers.append(int(match.group(2)))
            except (TypeError, ValueError):
                continue
        if season_episode_numbers:
            max_episode = max(season_episode_numbers)
            return {
                **result,
                "ok": False,
                "message": f"TMDb 中不存在 S{season_num:02d}E{episode_num:02d}，本季当前最多到第 {max_episode} 集",
            }

    season_details = tmdb_data.get("seasons_details")
    if isinstance(season_details, list):
        for season_data in season_details:
            if not isinstance(season_data, dict):
                continue
            try:
                if int(season_data.get("season_number")) != season_num:
                    continue
            except (TypeError, ValueError):
                continue
            season_episodes = season_data.get("episodes")
            if not isinstance(season_episodes, list):
                break
            episode_numbers = []
            for episode in season_episodes:
                if not isinstance(episode, dict):
                    continue
                try:
                    episode_numbers.append(int(episode.get("episode_number")))
                except (TypeError, ValueError):
                    continue
            if episode_num in episode_numbers:
                return result
            if episode_numbers:
                return {
                    **result,
                    "ok": False,
                    "message": f"TMDb 中不存在 S{season_num:02d}E{episode_num:02d}，本季当前最多到第 {max(episode_numbers)} 集",
                }

    if season_summary and season_summary.get("episode_count") is not None:
        try:
            episode_count = int(season_summary.get("episode_count"))
            if episode_count >= 0 and episode_num > episode_count:
                return {
                    **result,
                    "ok": False,
                    "message": f"TMDb 中不存在 S{season_num:02d}E{episode_num:02d}，本季当前共 {episode_count} 集",
                }
        except (TypeError, ValueError):
            pass

    return result


def _contains_chinese_text(text: str) -> bool:
    return bool(re.search(r"[一-鿿]", str(text or "")))


def _is_legacy_inferred_collection_name(name: str) -> bool:
    value = str(name or "").strip()
    if not value or not _contains_chinese_text(value):
        return False
    if value.endswith((" (系列)", "（系列）")):
        return False
    return value.endswith(("系列", "三部曲"))


def _tmdb_cached_movie_collection_refresh_reason(data: Optional[dict]) -> str:
    if not isinstance(data, dict):
        return ""

    belongs = data.get("belongs_to_collection")
    if not isinstance(belongs, dict) or not belongs.get("id"):
        return ""

    collection_details = data.get("collection_details")
    if not isinstance(collection_details, dict):
        return "缺少新版合集详情"

    collection_name = (
        collection_details.get("localized_name")
        or collection_details.get("name")
        or belongs.get("localized_name")
        or belongs.get("name")
    )
    if collection_details.get("_collection_language_fallback_checked"):
        collection_source = str(collection_details.get("_collection_name_source") or "")
        if not collection_source:
            return "缺少合集命名来源标记"
        if collection_source == "parts" and _is_legacy_inferred_collection_name(collection_name):
            return "旧版推断合集格式"
        return ""

    if collection_name and not _contains_chinese_text(collection_name):
        return "缺少中文合集名检查"
    return ""


def _tmdb_cached_detail_refresh_reason(media_type: str, data: Optional[dict]) -> str:
    if not isinstance(data, dict):
        return ""
    if media_type == "movie":
        collection_reason = _tmdb_cached_movie_collection_refresh_reason(data)
        if collection_reason:
            return collection_reason

    source = data.get("series_details") if media_type != "movie" and isinstance(data.get("series_details"), dict) else data
    if source.get("_title_language_fallback_checked"):
        return ""
    title_field = "title" if media_type == "movie" else "name"
    if not _contains_chinese_text(source.get(title_field, "")):
        return "缺少中文备选语言检查"
    return ""


def _tmdb_detail_status(media_type: str, data: Optional[dict]) -> str:
    if not isinstance(data, dict):
        return ""
    if media_type == "movie":
        return "movie"
    series_details = data.get("series_details") if isinstance(data.get("series_details"), dict) else data
    return str(series_details.get("status", "") or "")


def _human_tmdb_media_type(media_type: str) -> str:
    value = str(media_type or "").strip().lower()
    if value == "movie":
        return "电影"
    if value in {"tv", "series"}:
        return "剧集"
    return value or "未知"


def _human_tmdb_status(status: str) -> str:
    value = str(status or "").strip()
    lower = value.lower()
    status_map = {
        "movie": "电影",
        "ended": "已完结",
        "returning series": "连载中",
        "in production": "制作中",
        "planned": "计划中",
        "canceled": "已取消",
        "cancelled": "已取消",
        "pilot": "试播",
    }
    return status_map.get(lower, value or "未知")


def _format_ttl_days(ttl_seconds: int | float) -> str:
    try:
        seconds = float(ttl_seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{int(seconds // 86400)}天"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)}小时"
    return f"{int(seconds)}秒"


def _tmdb_detail_ttl(media_type: str, data: Optional[dict]) -> int:
    if media_type == "movie":
        return _TMDB_DETAIL_CACHE_MOVIE_TTL_SECONDS
    status = _tmdb_detail_status(media_type, data).strip().lower()
    if status in {"ended", "canceled", "cancelled"}:
        return _TMDB_DETAIL_CACHE_ENDED_TV_TTL_SECONDS
    return _TMDB_DETAIL_CACHE_ACTIVE_TV_TTL_SECONDS


def _get_cached_tmdb_detail(tmdb_id: int, media_type: str, required_episode_keys: Optional[list[str]] = None) -> Optional[dict]:
    if not tmdb_id or not media_type or not _ensure_tmdb_sqlite_cache_schema():
        return None
    try:
        from core.cache_db import tmdb_cache_db as cache_db
        now_ts = _time.time()
        with cache_db() as conn:
            row = conn.execute(
                "SELECT payload_json, expires_at FROM tmdb_detail_cache WHERE media_type = ? AND tmdb_id = ?",
                (str(media_type), int(tmdb_id)),
            ).fetchone()
        if not row:
            return None
        if float(row["expires_at"] or 0) <= now_ts:
            return None
        data = json.loads(row["payload_json"])
        refresh_reason = _tmdb_cached_detail_refresh_reason(media_type, data)
        if refresh_reason:
            logger.debug(f"[TMDbCache] 详情缓存需要刷新: 类型：{_human_tmdb_media_type(media_type)} | TMDb编号：{tmdb_id} | 原因：{refresh_reason}")
            return None
        if media_type != "movie" and not _tmdb_data_has_required_episodes(data, list(required_episode_keys or [])):
            if _tmdb_series_is_ended(data):
                logger.debug(f"[TMDbCache] 剧集详情缓存命中但目标集不存在: TMDb编号：{tmdb_id} | 目标集：{required_episode_keys}")
                return data
            logger.debug(f"[TMDbCache] 剧集详情缓存需要刷新: TMDb编号：{tmdb_id} | 原因：缺少目标集 | 目标集：{required_episode_keys}")
            return None
        logger.trace(f"[TMDbCache] TMDb详情缓存命中: 类型：{_human_tmdb_media_type(media_type)} | TMDb编号：{tmdb_id}")
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug(f"[TMDbCache] 读取详情缓存失败: 类型：{_human_tmdb_media_type(media_type)} | TMDb编号：{tmdb_id} | 错误：{e}")
        return None


def _set_cached_tmdb_detail(tmdb_id: int, media_type: str, data: Optional[dict]) -> None:
    if not tmdb_id or not media_type or not isinstance(data, dict) or not _ensure_tmdb_sqlite_cache_schema():
        return
    try:
        from core.cache_db import tmdb_cache_db as cache_db
        now_ts = _time.time()
        ttl_seconds = _tmdb_detail_ttl(media_type, data)
        status = _tmdb_detail_status(media_type, data)
        episode_keys = _tmdb_data_episode_keys(data)
        payload_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with cache_db(write=True) as conn:
            conn.execute(
                """
                INSERT INTO tmdb_detail_cache(
                    media_type, tmdb_id, fetched_at, expires_at, status, episode_keys_json, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_type, tmdb_id) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at,
                    status = excluded.status,
                    episode_keys_json = excluded.episode_keys_json,
                    payload_json = excluded.payload_json
                """,
                (
                    str(media_type),
                    int(tmdb_id),
                    now_ts,
                    now_ts + ttl_seconds,
                    status,
                    json.dumps(episode_keys, ensure_ascii=False, separators=(",", ":")),
                    payload_json,
                ),
            )
        logger.debug(f"[TMDbCache] TMDb详情已缓存: 类型：{_human_tmdb_media_type(media_type)} | TMDb编号：{tmdb_id} | 有效期：{_format_ttl_days(ttl_seconds)} | 状态：{_human_tmdb_status(status)}")
    except Exception as e:
        logger.debug(f"[TMDbCache] 写入详情缓存失败: 类型：{_human_tmdb_media_type(media_type)} | TMDb编号：{tmdb_id} | 错误：{e}")


def _tmdb_data_actual_id(media_type: str, data: Optional[dict]) -> Optional[int]:
    if not isinstance(data, dict):
        return None
    source = data.get("series_details") if media_type != "movie" else data
    if not isinstance(source, dict):
        source = data
    try:
        actual_id = int(source.get("id") or 0)
        return actual_id or None
    except (TypeError, ValueError):
        return None


def _strip_tmdb_id_from_text(text: str) -> str:
    cleaned = str(text or "")
    if not cleaned:
        return ""
    cleaned = re.sub(r'\s*[\{\[]tmdb(?:id)?[-=: ]*\d+[\}\]]\s*', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*(?<!\d)tmdb(?:id)?[-=: ]*\d+(?!\d)\s*', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned.strip('.-_ ')


def _title_dirs_for_invalid_tmdb_id(parsed: dict, failed_tmdb_id: Optional[int]) -> list[str]:
    file_path = str((parsed or {}).get("file_path") or "")
    if not file_path:
        return []

    path = Path(file_path)
    parent_dirs = [parent.name for parent in path.parents if parent.name and parent.name not in {"/", "."}]
    matched_dirs: list[str] = []
    fallback_dirs: list[str] = []

    for index, dir_name in enumerate(parent_dirs[:4]):
        if not dir_name:
            continue
        extracted_id = _extract_tmdb_id_from_text(dir_name)
        if extracted_id and (not failed_tmdb_id or int(extracted_id) == int(failed_tmdb_id)):
            matched_dirs.append(dir_name)
        if index == 0 and SEASON_DIR_PATTERN.match(dir_name):
            continue
        fallback_dirs.append(dir_name)

    return matched_dirs or fallback_dirs[:2]


def _build_parsed_without_invalid_tmdb_id(parsed: Optional[dict], media_type: str, failed_tmdb_id: Optional[int]) -> Optional[dict]:
    if not parsed:
        return None

    from core.meta import MetaInfo

    fallback = dict(parsed)
    fallback["media_type"] = media_type or fallback.get("media_type") or ""
    fallback["tmdb_id_direct"] = None
    fallback["tmdb_id_source"] = ""

    dir_titles: list[str] = []
    dir_cn_name = ""
    dir_en_name = ""
    dir_year = None

    for dir_name in _title_dirs_for_invalid_tmdb_id(parsed, failed_tmdb_id):
        cleaned_dir_name = _strip_tmdb_id_from_text(dir_name)
        if not cleaned_dir_name:
            continue
        dir_titles.append(cleaned_dir_name)
        try:
            dir_meta = MetaInfo(_preprocess_dir_name(cleaned_dir_name))
        except Exception:
            continue
        cn_name, en_name, title = _select_titles_from_meta(dir_meta)
        if title:
            dir_titles.append(title)
        if not dir_cn_name and cn_name:
            dir_cn_name = cn_name
        if not dir_en_name and en_name:
            dir_en_name = en_name
        if not dir_year and dir_meta.year:
            dir_year = dir_meta.year

    if dir_cn_name or dir_en_name:
        fallback["cn_name"] = dir_cn_name or ""
        fallback["en_name"] = dir_en_name or ""
        fallback["title"] = dir_cn_name or dir_en_name
    if dir_year:
        fallback["year"] = dir_year

    title_candidates = []
    title_candidates.extend(dir_titles)
    title_candidates.extend(fallback.get("titles_to_try") or [])
    title_candidates.extend([
        fallback.get("title") or "",
        fallback.get("cn_name") or "",
        fallback.get("en_name") or "",
    ])
    fallback["titles_to_try"] = _build_titles_to_try(*title_candidates)

    season = fallback.get("season")
    year = fallback.get("year")
    title_key = (
        _normalize_title_for_match(fallback.get("cn_name") or ""),
        _normalize_title_for_match(fallback.get("en_name") or ""),
        fallback["media_type"],
        season or "",
        year or "",
    )
    fallback["title_key"] = title_key
    fallback["group_key"] = title_key[:2] + (fallback["media_type"], year or "")
    return fallback


def _search_after_invalid_direct_tmdb_id(parsed: Optional[dict], media_type: str, api_key: str, failed_tmdb_id: Optional[int]) -> Optional[dict]:
    fallback_parsed = _build_parsed_without_invalid_tmdb_id(parsed, media_type, failed_tmdb_id)
    if not fallback_parsed:
        return None

    logger.warning(
        f"[MediaOrganize] 直写TMDb编号无效，改用标题重新识别: 原TMDb编号：{failed_tmdb_id or ''} | 标题候选：{' | '.join(fallback_parsed.get('titles_to_try') or [])}"
    )
    matched = _search_tmdb_for_title_sync(fallback_parsed, api_key, set())
    if not matched:
        logger.warning(
            f"[MediaOrganize] 直写TMDb编号无效，标题重新识别未命中: 原TMDb编号：{failed_tmdb_id or ''}"
        )
        return None

    corrected_tmdb_id = matched.get("tmdb_id")
    if failed_tmdb_id and corrected_tmdb_id and int(corrected_tmdb_id) == int(failed_tmdb_id):
        logger.warning(
            f"[MediaOrganize] 直写TMDb编号无效，标题重新识别仍命中原编号，已忽略: TMDb编号：{failed_tmdb_id}"
        )
        return None
    return matched


def _fetch_tmdb_data_uncached_sync(tmdb_id: int, media_type: str, api_key: str, season_number: Optional[int] = None, parsed: Optional[dict] = None) -> Optional[dict]:
    """同步版本：直接调用 tmdb 模块（在线程池中安全调用）"""
    try:
        from core import tmdb
        if media_type == 'movie':
            data = tmdb.get_movie_details(tmdb_id, api_key)
            if data:
                return data
            last_error = tmdb.get_last_tmdb_error()
            if last_error and last_error.get("status_code") == 404 and parsed:
                corrected = _search_after_invalid_direct_tmdb_id(parsed, "movie", api_key, tmdb_id)
                if corrected and corrected.get("tmdb_id"):
                    corrected_tmdb_id = int(corrected["tmdb_id"])
                    fallback = tmdb.get_movie_details(corrected_tmdb_id, api_key)
                    if fallback:
                        _cache_corrected_tmdb_id(parsed, corrected_tmdb_id)
                        logger.warning(f"[MediaOrganize] 无效TMDb编号已修正: 原TMDb编号：{tmdb_id} | 新TMDb编号：{corrected_tmdb_id} | 类型：电影")
                        return fallback
            return None
        else:
            data = tmdb.aggregate_full_series_data_from_tmdb(tmdb_id, api_key)
            if data:
                return data
            last_error = tmdb.get_last_tmdb_error()
            if last_error and last_error.get("status_code") == 404 and parsed:
                corrected = _search_after_invalid_direct_tmdb_id(parsed, "tv", api_key, tmdb_id)
                if corrected and corrected.get("tmdb_id"):
                    corrected_tmdb_id = int(corrected["tmdb_id"])
                    fallback = tmdb.aggregate_full_series_data_from_tmdb(corrected_tmdb_id, api_key)
                    if fallback:
                        _cache_corrected_tmdb_id(parsed, corrected_tmdb_id)
                        logger.warning(f"[MediaOrganize] 无效TMDb编号已修正: 原TMDb编号：{tmdb_id} | 新TMDb编号：{corrected_tmdb_id} | 类型：剧集")
                        return fallback
            return None
    except Exception as e:
        logger.error(f"[MediaOrganize] TMDb 同步请求失败: {e}")
        return None


def _fetch_tmdb_data_sync(
    tmdb_id: int,
    media_type: str,
    api_key: str,
    season_number: Optional[int] = None,
    parsed: Optional[dict] = None,
    required_episodes: Optional[list[tuple]] = None,
) -> Optional[dict]:
    required_episode_keys = _normalize_required_episodes(required_episodes, parsed, season_number)
    cached = _get_cached_tmdb_detail(tmdb_id, media_type, required_episode_keys)
    if cached:
        return cached
    data = _fetch_tmdb_data_uncached_sync(tmdb_id, media_type, api_key, season_number, parsed)
    if data:
        cache_tmdb_id = _tmdb_data_actual_id(media_type, data) or tmdb_id
        _set_cached_tmdb_detail(cache_tmdb_id, media_type, data)
    return data


async def _fetch_tmdb_data(
    tmdb_id: int,
    media_type: str,
    season_number: Optional[int] = None,
    parsed: Optional[dict] = None,
    required_episodes: Optional[list[tuple]] = None,
) -> Optional[dict]:
    try:
        from core.configs import global_config

        api_key = global_config.tmdb_key
        if not api_key:
            logger.error("[MediaOrganize] TMDb API Key 未配置")
            return None

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            _fetch_tmdb_data_sync,
            tmdb_id,
            media_type,
            api_key,
            season_number,
            parsed,
            required_episodes,
        )
    except Exception as e:
        logger.error(f"[MediaOrganize] TMDb 请求失败: {e}")
        return None


def _normalize_direct_tmdb_media_type(value: str) -> str:
    value = str(value or "").strip().lower()
    if value in {"tv", "series"}:
        return "tv"
    if value == "movie":
        return "movie"
    return ""


def _resolve_direct_tmdb_id_media_type_sync(tmdb_id: int, api_key: str, preferred_media_type: str = "") -> Optional[str]:
    """
    Probe the lightweight TMDb detail endpoints to determine whether a direct
    {tmdb-...} ID belongs to a movie or a TV show before title correction runs.
    """
    if not tmdb_id or not api_key:
        return None

    preferred = _normalize_direct_tmdb_media_type(preferred_media_type)
    candidates = []
    if preferred:
        candidates.append(preferred)
    candidates.extend(mt for mt in ("movie", "tv") if mt not in candidates)

    try:
        from core import tmdb
    except Exception as e:
        logger.debug(f"[MediaIdentify] 无法加载 TMDb 模块，跳过直写ID类型探测: {e}")
        return None

    for media_type in candidates:
        endpoint = f"/{'movie' if media_type == 'movie' else 'tv'}/{int(tmdb_id)}"
        try:
            data = tmdb._tmdb_request(  # noqa: SLF001 - lightweight probe avoids full detail fetch/correction.
                endpoint,
                api_key,
                {"language": getattr(tmdb, "DEFAULT_LANGUAGE", "zh-CN")},
                use_default_language=False,
            )
        except Exception as e:
            logger.debug(f"[MediaIdentify] 直写TMDb ID类型探测失败: TMDb:{tmdb_id} 类型:{media_type} 错误:{e}")
            continue
        if not isinstance(data, dict):
            continue
        try:
            actual_id = int(data.get("id") or 0)
        except (TypeError, ValueError):
            actual_id = 0
        if actual_id == int(tmdb_id):
            return media_type

    return None


async def _resolve_direct_tmdb_id_media_type(tmdb_id: int, api_key: str, preferred_media_type: str = "") -> Optional[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _resolve_direct_tmdb_id_media_type_sync,
        tmdb_id,
        api_key,
        preferred_media_type,
    )


# ---------------------------------------------------------------------------
# Title normalization & variant generation
# ---------------------------------------------------------------------------


def _normalize_title_for_match(name: str) -> str:
    """标准化标题用于匹配：去标点、空格，繁转简，转小写"""
    if not name:
        return ""
    import zhconv
    # 先转简体，再去标点，确保繁简一致
    simplified = zhconv.convert(name, 'zh-hans')
    return re.sub(r'[\s:：·\-*\'!,?.。、\-—―\+\|\\_/&#～~\(\)（）【】「」]', '', simplified).lower()


def _generate_title_variants(en_title: str, cn_title: str = "") -> list:
    """生成标题变体用于搜索"""
    import zhconv

    variants = []
    if en_title:
        variants.append(en_title)
    if cn_title:
        variants.append(cn_title)

    # 繁简互转
    current = list(variants)
    for t in current:
        try:
            simplified = zhconv.convert(t, "zh-hans")
            if simplified != t and simplified not in variants:
                variants.append(simplified)
            traditional = zhconv.convert(t, "zh-hant")
            if traditional != t and traditional not in variants:
                variants.append(traditional)
        except Exception:
            pass

    # 数字↔中文转换
    num_to_cn = {'0': '零', '1': '一', '2': '二', '3': '三', '4': '四', '5': '五',
                 '6': '六', '7': '七', '8': '八', '9': '九'}
    cn_to_num = {v: k for k, v in num_to_cn.items()}

    current = list(variants)
    for t in current:
        if any(c in t for c in num_to_cn):
            new_t = t
            for num, cn in num_to_cn.items():
                new_t = new_t.replace(num, cn)
            if new_t not in variants:
                variants.append(new_t)
        if any(c in t for c in cn_to_num):
            new_t = t
            for cn, num in cn_to_num.items():
                new_t = new_t.replace(cn, num)
            if new_t not in variants:
                variants.append(new_t)

    return variants



def _strip_trailing_year_from_title(title: str) -> tuple[str, Optional[int]]:
    text = str(title or "").strip()
    if not text:
        return "", None
    match = re.match(r'^(.*?)[\s._-]*[（(]\s*((?:19|20)\d{2})\s*[）)]\s*$', text)
    if not match:
        return text, None
    stripped = match.group(1).strip().rstrip("-_. ")
    return stripped, int(match.group(2))



def _collapse_repeated_year_tokens(title: str, year: Optional[int] = None) -> str:
    text = re.sub(r'\s+', ' ', str(title or '')).strip()
    if not text:
        return ""
    if year:
        text = re.sub(rf'(?<!\d)({year})(?:\s+\1)+(?!\d)', str(year), text)
    text = re.sub(r'(?<!\d)((?:19|20)\d{2})(?:\s+\1)+(?!\d)', r'\1', text)
    return text.strip()



def _build_search_seed_titles(title: str) -> list[str]:
    seeds: list[str] = []

    def _add(value: str):
        cleaned = re.sub(r'\s+', ' ', str(value or '')).strip().strip('.-_')
        if cleaned and cleaned not in seeds:
            seeds.append(cleaned)

    base = str(title or '').strip()
    if not base:
        return seeds
    _add(base)

    stripped, extracted_year = _strip_trailing_year_from_title(base)
    if stripped and stripped != base:
        _add(stripped)

    collapsed = _collapse_repeated_year_tokens(stripped or base, extracted_year)
    if collapsed and collapsed != (stripped or base):
        _add(collapsed)

    colon_base = collapsed or stripped or base
    if re.search(r'[：:]', colon_base):
        prefix = re.split(r'[：:]', colon_base, 1)[0].strip()
        if prefix and len(prefix) >= 2:
            _add(prefix)

    if re.search(r'[一-鿿]', colon_base) and re.search(r'(?<=[一-鿿])\s+(?=[一-鿿])', colon_base):
        _add(re.sub(r'(?<=[一-鿿])\s+(?=[一-鿿])', '', colon_base))

    return seeds



def _build_titles_to_try(*titles: str) -> list[str]:
    seen = set()
    variants: list[str] = []
    for title in titles:
        if not title:
            continue
        for seed in _build_search_seed_titles(title):
            for variant in _generate_title_variants(seed):
                normalized = _normalize_title_for_match(variant)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                variants.append(variant)
    return variants


def _find_corrected_tmdb_id_by_title_year(parsed: Optional[dict], media_type: str, api_key: str, failed_tmdb_id: Optional[int] = None) -> Optional[int]:
    if not parsed or not api_key:
        return None

    from core import tmdb

    titles_to_try = parsed.get("titles_to_try") or _build_titles_to_try(
        str(parsed.get("title") or ""),
        str(parsed.get("cn_name") or ""),
        str(parsed.get("en_name") or ""),
    )
    year = str(parsed.get("year") or "") or None
    finder = tmdb.find_movie_tmdb_id_by_title_year if media_type == "movie" else tmdb.find_tv_tmdb_id_by_title_year

    for candidate_title in titles_to_try:
        corrected_tmdb_id = finder(str(candidate_title or ""), api_key, year)
        if not corrected_tmdb_id:
            continue
        if failed_tmdb_id and int(corrected_tmdb_id) == int(failed_tmdb_id):
            logger.debug(
                f"[MediaOrganize] 404 后按标题年份重识别仍命中原 TMDb ID，继续尝试其他标题: {candidate_title} ({year or '未知年份'}) -> TMDb:{corrected_tmdb_id}"
            )
            continue
        logger.warning(
            f"[MediaOrganize] 404 后按标题年份重识别命中: {candidate_title} ({year or '未知年份'}) -> TMDb:{corrected_tmdb_id}"
        )
        return int(corrected_tmdb_id)

    if titles_to_try:
        logger.warning(
            f"[MediaOrganize] 404 后按标题年份重识别未命中: 原TMDb:{failed_tmdb_id or ''} | 年份:{year or '未知年份'} | 标题候选:{' | '.join(titles_to_try)}"
        )
    return None


def _preprocess_dir_name(dir_name: str) -> str:
    """
    预处理目录名，把中文名+年份没有空格分隔的情况拆开。
    如 "小宝与康熙2000 张卫健 1080P" → "小宝与康熙 2000 张卫健 1080P"
    """
    # 在中文和年份之间加空格：中文尾部 + 19xx/20xx
    dir_name = re.sub(r'([一-鿿])((?:19|20)\d{2})', r'\1 \2', dir_name)
    # 在年份和中文之间加空格：19xx/20xx + 中文头部
    dir_name = re.sub(r'((?:19|20)\d{2})([一-鿿])', r'\1 \2', dir_name)
    return dir_name


def _build_movie_weak_title_variants(title: str) -> list[str]:
    """电影弱匹配标题变体：罗马数字归一化 + 去噪词"""
    if not title:
        return []

    variants: list[str] = []
    base = re.sub(r'\s+', ' ', title).strip()
    if base:
        variants.append(base)

    words = base.split()
    normalized_words = [_ROMAN_MAP.get(w.lower(), w) for w in words]
    roman_norm = " ".join(normalized_words).strip()
    if roman_norm and roman_norm not in variants:
        variants.append(roman_norm)

    no_stopwords = " ".join([w for w in normalized_words if w.lower() not in _WEAK_MATCH_STOPWORDS]).strip()
    if no_stopwords and no_stopwords not in variants:
        variants.append(no_stopwords)

    return [v for v in variants if v]


def _movie_weak_result_is_plausible(weak_title: str, result: dict, year: Optional[int]) -> bool:
    norm_weak = _normalize_title_for_match(weak_title)
    if not norm_weak or not isinstance(result, dict):
        return False

    result_titles = [
        result.get('title') or '',
        result.get('original_title') or '',
    ]
    normalized_titles = [
        _normalize_title_for_match(title)
        for title in result_titles
        if title
    ]
    normalized_titles = [title for title in normalized_titles if title]
    if not normalized_titles:
        return False

    for norm_title in normalized_titles:
        if norm_title == norm_weak or norm_weak in norm_title:
            return True

    if year:
        result_year = str(result.get('release_date') or '')[:4]
        if result_year == str(year):
            for norm_title in normalized_titles:
                if len(norm_title) >= 4 and norm_title in norm_weak:
                    return True

    return False


# ---------------------------------------------------------------------------
# TMDb season verification
# ---------------------------------------------------------------------------


def _get_tv_season_year(tmdb_id: int, season_number: int, api_key: str) -> str:
    try:
        from core import tmdb as tmdb_mod
        details = tmdb_mod.get_tv_details(int(tmdb_id), api_key, append_to_response="seasons")
        if details and 'seasons' in details:
            for s in details['seasons']:
                if s.get('season_number') == season_number:
                    air_date = str(s.get('air_date') or "")
                    return air_date[:4] if air_date else ""
    except Exception as e:
        logger.warning(f"[MediaIdentify] 获取季年份失败: {e}")
    return ""


def _verify_season_exists(tmdb_id: int, season_number: int, api_key: str) -> bool:
    """验证剧集是否包含指定季"""
    return bool(_get_tv_season_year(tmdb_id, season_number, api_key))


# ---------------------------------------------------------------------------
# Filename parsing & TMDb search
# ---------------------------------------------------------------------------


def _extract_tmdb_id_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    tmdbid_match = re.search(r'\{tmdb(?:id)?[-=: ]*(\d+)\}', text, re.IGNORECASE)
    if not tmdbid_match:
        tmdbid_match = re.search(r'\[tmdb(?:id)?[-=: ]*(\d+)\]', text, re.IGNORECASE)
    if not tmdbid_match:
        tmdbid_match = re.search(r'(?<!\d)tmdb(?:id)?[-=: ]*(\d+)(?!\d)', text, re.IGNORECASE)
    if tmdbid_match:
        return int(tmdbid_match.group(1))
    return None


AUXILIARY_CN_STEM_FULLMATCH_RE = re.compile(
    r"^(双语|字幕|特效|内封|外挂|官译|简体|繁体|繁中|简中|中英|简英|多语|"
    r"国英|台粤|音轨|评论|国配|台配|粤语|韩语|日语|杜比|全景声|无损|中字|"
    r"国语|原声)+$"
)


NOISY_SHORT_WORDS = {'disc', 'cd', 'dvd', 'part', 'episode', 'ep', 'vol'}
SEASON_DIR_PATTERN = re.compile(r'^(season\s*\d+|s\d+|第.{0,3}季|\d{1,2})$', re.IGNORECASE)
CN_TRAILING_EPISODE_STEM_RE = re.compile(r'^(.{2,80}?)(\d{1,4})$')
EPISODE_RANGE_DIR_RE = re.compile(r'^\s*(\d{1,4})\s*[-_~～—–至到]\s*(\d{1,4})\s*$')
TV_CONTEXT_TEXT_RE = re.compile(r'(连续剧|連續劇|电视剧|電視劇|剧集|劇集|短剧|短劇|剧版|劇版|迷你剧|迷你劇)')


def _should_use_parent_title_for_file_stem(stem: str, parent_dir_name: str, file_tmdbid: Optional[int], file_doubanid: Optional[str]) -> bool:
    if not stem or not parent_dir_name:
        return False
    if file_tmdbid or file_doubanid:
        return False
    if not re.search(r"[A-Za-z]{2,}", parent_dir_name):
        return False
    if not StringUtils.is_all_chinese(stem):
        return False
    if len(stem) > 16:
        return False
    if not AUXILIARY_CN_STEM_FULLMATCH_RE.match(stem):
        return False
    if re.search(r"[第共]\s*[0-9一二三四五六七八九十百零]+\s*[季集话話]", stem):
        return False
    return True


def _build_meta_from_path(filename: str, file_path: str):
    from core.meta import MetaInfo, MetaInfoPath

    if file_path:
        return MetaInfoPath(Path(file_path))
    return MetaInfo(filename)


def _parse_cn_trailing_episode_stem(stem: str) -> tuple[str, Optional[int]]:
    text = re.sub(r'\s+', ' ', str(stem or '')).strip()
    if not text:
        return "", None
    match = CN_TRAILING_EPISODE_STEM_RE.match(text)
    if not match:
        return "", None
    title = str(match.group(1) or "").strip().rstrip('.-_ ')
    if not title or not re.search(r'[一-鿿]', title):
        return "", None
    if re.search(r'[集话話期幕季]\s*$', title):
        return "", None
    try:
        episode = int(match.group(2))
    except (TypeError, ValueError):
        return "", None
    if not 0 < episode < 10000:
        return "", None
    if len(_normalize_title_for_match(title)) < 2:
        return "", None
    return title, episode


def _is_episode_range_dir(dir_name: str, episode: Optional[int] = None) -> bool:
    match = EPISODE_RANGE_DIR_RE.match(str(dir_name or ""))
    if not match:
        return False
    try:
        start = int(match.group(1))
        end = int(match.group(2))
    except (TypeError, ValueError):
        return False
    if start <= 0 or end <= 0 or start > end:
        return False
    if episode is not None and not (start <= int(episode) <= end):
        return False
    return True


def _has_tv_context_text(text: str) -> bool:
    value = str(text or "")
    if not value:
        return False
    return bool(TV_CONTEXT_TEXT_RE.search(value) or _has_total_episode_count_marker(value))


def _path_has_tv_context_for_trailing_episode(file_path: str, title: str, episode: int) -> bool:
    if not file_path or not title:
        return False
    try:
        dir_names = [
            parent.name
            for parent in list(Path(file_path).parents)[:4]
            if parent.name and parent.name not in {"/", "."}
        ]
    except Exception:
        return False
    if not dir_names:
        return False

    title_norm = _normalize_title_for_match(title)
    context_dirs = [name for name in dir_names if _has_tv_context_text(name)]
    has_episode_range = any(_is_episode_range_dir(name, episode) for name in dir_names[:2])

    if not context_dirs:
        return False
    if has_episode_range:
        return True

    for dir_name in context_dirs:
        if title_norm and title_norm in _normalize_title_for_match(dir_name):
            return True
    return False


def _normalize_display_meta_info(meta_info: dict) -> dict:
    normalized = dict(meta_info or {})

    resource_type = str(normalized.get("resource_type", "") or "")
    if resource_type == "Blu-ray":
        normalized["resource_type"] = "BluRay"

    resource_effect = str(normalized.get("resource_effect", "") or "")
    resource_effect_tags = [tag for tag in re.split(r"[.,\s]+", resource_effect) if tag]

    video_effect = str(normalized.get("video_effect", "") or "")
    if video_effect:
        tags = []
        for raw_tag in re.split(r"[.,\s]+", video_effect):
            tag = str(raw_tag or "").strip()
            if not tag:
                continue
            upper_tag = tag.upper()
            if upper_tag in {"REPACK", "PROPER"}:
                resource_effect_tags.append(upper_tag)
                continue
            if upper_tag in {"DOVI", "DV", "DOLBY", "DOLBYVISION"}:
                tags.append("DV")
            elif upper_tag == "VISION":
                continue
            else:
                tags.append(tag)
        deduped = []
        seen = set()
        for tag in tags:
            key = tag.upper()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(tag)
        normalized["video_effect"] = ".".join(deduped)

    if resource_effect_tags:
        deduped_resource_effects = []
        seen_resource_effects = set()
        for tag in resource_effect_tags:
            upper_tag = str(tag).upper()
            if upper_tag in seen_resource_effects:
                continue
            seen_resource_effects.add(upper_tag)
            deduped_resource_effects.append(upper_tag)
        normalized["resource_effect"] = ".".join(deduped_resource_effects)

    audio_encode = str(normalized.get("audio_encode", "") or "")
    if audio_encode:
        audio_encode = audio_encode.replace("DTSHD MA", "DTSHD-MA")
        audio_encode = re.sub(r"\bTrueHD\b", "TrueHD", audio_encode)
        normalized["audio_encode"] = audio_encode

    return normalized


def _enrich_meta_info_from_title(meta_info: dict, filename: str, path_meta) -> dict:
    enriched = dict(meta_info or {})
    title = str(filename or "")
    path_resource_type = str(getattr(path_meta, "resource_type", "") or "")
    path_web_source = str(getattr(path_meta, "web_source", "") or "")
    path_resource_effect = str(getattr(path_meta, "resource_effect", "") or "")
    path_video_effect = str(getattr(path_meta, "video_effect", "") or "")
    path_resource_team = str(getattr(path_meta, "resource_team", "") or "")

    if not enriched.get("web_source"):
        if path_web_source:
            enriched["web_source"] = path_web_source
        elif re.search(r"(?i)\bUHD[.\s-]*Blu[.\s-]*ray\b", title):
            enriched["web_source"] = "UHD"

    if not enriched.get("resource_type"):
        if path_resource_type:
            enriched["resource_type"] = path_resource_type
        elif re.search(r"(?i)\bBlu[.\s-]*ray\b", title):
            enriched["resource_type"] = "BluRay"
    elif enriched.get("resource_type") == "UHD" and re.search(r"(?i)\bUHD[.\s-]*Blu[.\s-]*ray\b", title):
        enriched["web_source"] = enriched.get("web_source") or "UHD"
        enriched["resource_type"] = "BluRay"

    if not enriched.get("resource_effect"):
        resource_effect_tags = []
        if path_resource_effect:
            for raw_tag in re.split(r"[.,\s]+", path_resource_effect):
                tag = str(raw_tag or "").strip().upper()
                if tag:
                    resource_effect_tags.append(tag)
        for candidate in ("REMUX", "REPACK", "PROPER"):
            if re.search(rf"(?i)\b{candidate}\b", title):
                resource_effect_tags.append(candidate)
        if resource_effect_tags:
            deduped_resource_effects = []
            seen_resource_effects = set()
            for tag in resource_effect_tags:
                if tag in seen_resource_effects:
                    continue
                seen_resource_effects.add(tag)
                deduped_resource_effects.append(tag)
            enriched["resource_effect"] = ".".join(deduped_resource_effects)

    if not enriched.get("video_effect") and path_video_effect:
        enriched["video_effect"] = path_video_effect

    if not enriched.get("color_depth"):
        bit_match = re.search(r"(?i)\b(12bit|10bit|8bit)\b", title)
        if bit_match:
            enriched["color_depth"] = bit_match.group(1).lower()
        elif str(enriched.get("video_effect", "") or "").upper().startswith("HDR"):
            enriched["color_depth"] = "10bit"

    if not enriched.get("release_group"):
        release_group = str(enriched.get("resource_team", "") or path_resource_team)
        if not release_group:
            m = re.search(r"(?:-|@)([A-Za-z0-9_]+)$", os.path.splitext(title)[0])
            if m:
                release_group = m.group(1).strip()
        if release_group:
            enriched["release_group"] = release_group
            if not enriched.get("resource_team"):
                enriched["resource_team"] = release_group

    return _normalize_display_meta_info(enriched)


def _clone_meta_with_cleared_title(meta):
    from copy import deepcopy

    cloned = deepcopy(meta)
    cloned.cn_name = None
    cloned.en_name = None
    return cloned


def _select_titles_from_meta(meta) -> tuple[str, str, str]:
    cn_name = meta.cn_name or ""
    en_name = meta.en_name or ""
    title = cn_name if cn_name else en_name
    return cn_name, en_name, title


def _is_clear_file_title(title: str) -> bool:
    cleaned = str(title or "").strip()
    normalized = _normalize_title_for_match(cleaned)
    if not normalized:
        return False
    if normalized in NOISY_SHORT_WORDS:
        return False
    if re.fullmatch(r'\d+', normalized):
        return False
    if re.search(r'[一-鿿]', cleaned):
        return len(normalized) >= 2
    return len(normalized) >= 4


def _titles_are_clear_conflict(dir_title: str, file_title: str) -> bool:
    dir_clean = str(dir_title or "").strip()
    file_clean = str(file_title or "").strip()
    dir_norm = _normalize_title_for_match(dir_clean)
    file_norm = _normalize_title_for_match(file_clean)
    if not dir_norm or not file_norm or dir_norm == file_norm:
        return False

    dir_has_cn = bool(re.search(r'[一-鿿]', dir_clean))
    file_has_cn = bool(re.search(r'[一-鿿]', file_clean))
    dir_has_latin = bool(re.search(r'[A-Za-z]', dir_clean))
    file_has_latin = bool(re.search(r'[A-Za-z]', file_clean))

    if dir_has_cn != file_has_cn:
        # 中文目录 + 英文文件名常见于正常资源命名，不能仅凭文本不同判为冲突。
        return False
    if dir_has_latin != file_has_latin and not dir_has_cn and not file_has_cn:
        return False
    return True


def _dir_title_tmdb_conflicts_with_file_title(file_meta, path_title: str, file_path: str, media_type: str) -> tuple[bool, str]:
    if not file_path:
        return False, ""
    file_cn, file_en, file_title = _select_titles_from_meta(file_meta)
    if not _is_clear_file_title(file_title):
        return False, ""
    file_norm = _normalize_title_for_match(file_title)

    parent_dir = os.path.basename(os.path.dirname(file_path))
    grandparent_dir = os.path.basename(os.path.dirname(os.path.dirname(file_path)))
    candidates = [(parent_dir, "parent")]
    if media_type != "movie":
        candidates.append((grandparent_dir, "grandparent"))

    from core.meta import MetaInfo

    for dir_name, source in candidates:
        if not dir_name or not _extract_tmdb_id_from_text(dir_name):
            continue
        cleaned_dir_name = _strip_tmdb_id_from_text(dir_name)
        dir_meta = MetaInfo(_preprocess_dir_name(cleaned_dir_name))
        _, _, dir_title = _select_titles_from_meta(dir_meta)
        if _titles_are_clear_conflict(dir_title or cleaned_dir_name, file_title):
            return True, source

    return False, ""


def _normalize_media_type(meta_type, season: Optional[int], episode: Optional[int], force_movie: bool, media_type_hint: str = None) -> str:
    from core.meta.types import MediaType

    if media_type_hint:
        return media_type_hint
    if force_movie:
        return "movie"
    if meta_type == MediaType.TV or season is not None or episode is not None:
        return "tv"
    return "movie"


def _extract_tmdb_id_from_dirs(file_path: str, media_type: str) -> tuple[Optional[int], str]:
    if not file_path:
        return None, ""

    parent_dir = os.path.basename(os.path.dirname(file_path))
    grandparent_dir = os.path.basename(os.path.dirname(os.path.dirname(file_path)))

    if media_type == "movie":
        tmdb_id = _extract_tmdb_id_from_text(parent_dir)
        return tmdb_id, "parent" if tmdb_id else ""

    tmdb_id = _extract_tmdb_id_from_text(parent_dir)
    if tmdb_id:
        return tmdb_id, "parent"
    tmdb_id = _extract_tmdb_id_from_text(grandparent_dir)
    if tmdb_id:
        return tmdb_id, "grandparent"
    return None, ""



def _strip_total_episode_count_markers(text: str) -> str:
    return _TOTAL_EPISODE_COUNT_RE.sub(" ", str(text or ""))


def _has_total_episode_count_marker(*texts: str) -> bool:
    return any(_TOTAL_EPISODE_COUNT_RE.search(str(text or "")) for text in texts)


def _parse_numeric_video_stem_episode(text: str) -> Optional[int]:
    raw = str(text or "").strip()
    if not raw:
        return None
    basename = os.path.basename(raw.replace("\\", "/"))
    stem, ext = os.path.splitext(basename)
    if not ext or not stem.isdigit() or len(stem) >= 5:
        return None
    episode = int(stem)
    return episode if 0 < episode < 10000 else None


def _has_explicit_season_or_episode_marker(*texts: str) -> bool:
    for text in texts:
        if _parse_numeric_video_stem_episode(text) is not None:
            return True
        cleaned = _strip_total_episode_count_markers(text)
        if cleaned and _EXPLICIT_SEASON_OR_EPISODE_MARKER_RE.search(cleaned):
            return True
    return False



def _parse_explicit_season_episode_marker(*texts: str) -> tuple[Optional[int], Optional[int]]:
    for text in texts:
        raw = str(text or "")
        if not raw:
            continue
        numeric_episode = _parse_numeric_video_stem_episode(raw)
        if numeric_episode is not None:
            return None, numeric_episode
        raw = _strip_total_episode_count_markers(raw)

        match = re.search(r'(?i)\bS(\d{1,3})E[P]?(\d{1,4})\b', raw)
        if match:
            return int(match.group(1)), int(match.group(2))

        match = re.search(r'(?i)\bSeason\s+(\d{1,3})\b', raw)
        if match:
            return int(match.group(1)), None

        match = re.search(r'(?i)\bS(\d{1,3})\b', raw)
        if match:
            return int(match.group(1)), None

        match = re.search(r'(?i)\bEpisode\s+(\d{1,4})\b', raw)
        if match:
            return None, int(match.group(1))

        match = re.search(r'(?i)\bE[P]?(\d{1,4})\b', raw)
        if match:
            return None, int(match.group(1))

        match = re.search(r'第\s*([0-9一二三四五六七八九十百零]+)\s*季', raw)
        if match:
            try:
                import cn2an
                return int(cn2an.cn2an(match.group(1), mode='smart')), None
            except Exception:
                pass

        match = re.search(r'第\s*([0-9一二三四五六七八九十百零]+)\s*[集话話期幕]', raw)
        if match:
            try:
                import cn2an
                return None, int(cn2an.cn2an(match.group(1), mode='smart'))
            except Exception:
                pass

        match = re.search(r'(?<!\d)(\d{1,4})\s*[集话話期幕]', raw)
        if match:
            return None, int(match.group(1))

    return None, None


def _collect_same_title_year_candidates(file_path: str, title: str, file_meta, parsed_year) -> list[dict]:
    if not title:
        return []

    title_norm = _normalize_title_for_match(title)
    if not title_norm:
        return []

    from core.meta import MetaInfo

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(candidate_title: str, candidate_year, source: str):
        candidate_title = str(candidate_title or "").strip()
        candidate_year = str(candidate_year or "").strip()
        candidate_norm = _normalize_title_for_match(candidate_title)
        if not candidate_norm or candidate_norm != title_norm or not candidate_year:
            return
        key = (candidate_norm, candidate_year)
        if key in seen:
            return
        seen.add(key)
        candidates.append({
            "title": candidate_title,
            "title_norm": candidate_norm,
            "year": candidate_year,
            "source": source,
        })

    file_cn, file_en, file_title = _select_titles_from_meta(file_meta)
    _add(file_title, getattr(file_meta, "year", None), "filename")

    for parent in list(Path(file_path).parents)[:4]:
        dir_name = parent.name
        if not dir_name or dir_name in {"/", "."}:
            continue
        if SEASON_DIR_PATTERN.match(dir_name):
            continue

        cleaned_dir_name = _strip_tmdb_id_from_text(dir_name)
        cleaned_dir_name = _strip_total_episode_count_markers(cleaned_dir_name)
        cleaned_dir_name = _preprocess_dir_name(cleaned_dir_name)
        if not cleaned_dir_name:
            continue

        try:
            dir_meta = MetaInfo(cleaned_dir_name)
        except Exception:
            continue

        _, _, dir_title = _select_titles_from_meta(dir_meta)
        _add(dir_title, dir_meta.year, "folder")

    if not candidates:
        _add(title, parsed_year, "parsed")
    return candidates



def _should_treat_leading_number_as_title(cn_name: str, filename: str, season: Optional[int], episode: Optional[int]) -> bool:
    if season is None or episode is None or season != 1 or episode <= 1:
        return False
    name = str(cn_name or "").strip()
    if not name or not re.match(r'^\d{1,3}[一-鿿A-Za-z]', name):
        return False
    if _has_explicit_season_or_episode_marker(filename):
        return False
    return True



def _parse_filename(filename: str, media_type_hint: str = None, file_path: str = "", quiet: bool = False) -> Optional[dict]:
    """
    纯本地解析：优先复用 MetaInfo / MetaInfoPath，不发起任何网络请求。
    返回解析结果 dict，包含 cn_name, en_name, year, season, episode, media_type, meta_info, title_key 等。
    如果无法提取标题，返回 None。
    """
    from core.meta import MetaInfo

    sp_special = bool(re.search(r'(?i)(\bS\d{1,2}SP\b|\bSP\b|\bSPECIAL\b|特别篇|特別篇|特典)', filename))
    normalized_filename = re.sub(r'(?i)\bS(\d{1,2})SP\b', r'S\1', filename)

    file_meta = MetaInfo(normalized_filename)
    path_meta = _build_meta_from_path(normalized_filename, file_path)

    stem = Path(normalized_filename).stem if normalized_filename else ""
    parent_dir_name = os.path.basename(os.path.dirname(file_path)) if file_path else ""
    if _should_use_parent_title_for_file_stem(stem, parent_dir_name, file_meta.tmdbid, file_meta.doubanid):
        path_meta = _clone_meta_with_cleared_title(path_meta)
        if parent_dir_name:
            parent_meta = MetaInfo(_preprocess_dir_name(parent_dir_name))
            path_meta.merge(parent_meta)
        grandparent_dir_name = os.path.basename(os.path.dirname(os.path.dirname(file_path))) if file_path else ""
        if grandparent_dir_name and grandparent_dir_name not in {"/", "."}:
            grandparent_meta = MetaInfo(_preprocess_dir_name(grandparent_dir_name))
            path_meta.merge(grandparent_meta)
        logger.debug(f"[MediaIdentify] 文件名仅为辅助标签，改用父目录标题识别: {filename} -> {parent_dir_name}")

    cn_name, en_name, title = _select_titles_from_meta(path_meta)
    year = path_meta.year
    season = path_meta.begin_season
    episode = path_meta.begin_episode
    force_movie = False
    title_source = "file_or_path"

    decimal_sequel_match = re.match(r'^([一-鿿]{2,}\d+(?:\.\d+)+)(?=(?:\s*[（(](?:19|20)\d{2}[）)])|[.\s_-](?:19|20)\d{2}\b)', filename)
    if decimal_sequel_match and season is None:
        decimal_title = decimal_sequel_match.group(1)
        normalized_cn_name = re.sub(r'\s+', '', str(cn_name or ''))
        normalized_decimal_title = re.sub(r'\s+', '', decimal_title)
        if not normalized_cn_name or normalized_cn_name in {re.sub(r'\s+', '', decimal_title.split('.', 1)[0]), normalized_decimal_title}:
            cn_name = decimal_title
            en_name = ""
            episode = None
            force_movie = True
            title_source = "decimal_sequel_fix"

    sequel_match = re.match(r'^([一-鿿]{2,})(\d{1,2})(?=(?:\s*[（(](?:19|20)\d{2}[）)])|[.\s_-](?:19|20)\d{2}\b)', filename)
    if sequel_match and season is None and not decimal_sequel_match:
        sequel_title = f"{sequel_match.group(1)}{sequel_match.group(2)}"
        sequel_num = int(sequel_match.group(2))
        if cn_name in (sequel_match.group(1), sequel_title):
            if episode is None or sequel_num == int(episode):
                cn_name = sequel_title
                episode = None
                force_movie = True
                title_source = "sequel_fix"

    has_explicit_episode_marker = _has_explicit_season_or_episode_marker(filename, file_path)
    has_total_episode_count_marker = _has_total_episode_count_marker(filename, file_path)
    explicit_season, explicit_episode = _parse_explicit_season_episode_marker(filename, file_path)
    trailing_episode_context_match = False

    if has_explicit_episode_marker:
        if explicit_season is not None:
            season = explicit_season
        if explicit_episode is not None:
            episode = explicit_episode
        if episode is not None and season is None:
            season = 1

    if sp_special and has_explicit_episode_marker and explicit_episode is None:
        season = 0
        episode = None

    if not has_explicit_episode_marker and season is None and episode is None:
        trailing_title, trailing_episode = _parse_cn_trailing_episode_stem(stem)
        if (
            trailing_title
            and trailing_episode is not None
            and _path_has_tv_context_for_trailing_episode(file_path, trailing_title, trailing_episode)
        ):
            cn_name = trailing_title
            en_name = ""
            season = 1
            episode = trailing_episode
            force_movie = False
            trailing_episode_context_match = True
            title_source = "trailing_episode_context"
            if not quiet:
                logger.debug(
                    f"[MediaIdentify] 中文尾号按剧集识别: {filename} -> "
                    f"{trailing_title} S01E{trailing_episode:02d}"
                )

    if not has_explicit_episode_marker and not trailing_episode_context_match and (season is not None or episode is not None) and not force_movie:
        season = None
        episode = None
        if has_total_episode_count_marker:
            title_source = "total_episode_count_fix"
        else:
            force_movie = True
            title_source = "implicit_episode_movie_fix"

    if _should_treat_leading_number_as_title(cn_name, filename, season, episode):
        season = None
        episode = None
        force_movie = True
        title_source = "leading_number_title_fix"

    sequel_year_hint = bool(re.match(r'^([一-鿿]{2,})(\d{1,2})(?:\s*[（(](?:19|20)\d{2}[）)])', filename))
    if has_explicit_episode_marker and cn_name and not en_name and episode is None and not force_movie and not sequel_year_hint:
        _cleaned = cn_name
        _cleaned = re.sub(r'[（(][^）)]*[）)]$', '', _cleaned)
        _cleaned = re.sub(r'(end|END)$', '', _cleaned)
        m = re.match(r'^(.+?)(\d{1,3})$', _cleaned)
        if m:
            candidate = m.group(1)
            ep_str = m.group(2)
            if len(candidate) >= 2 and not re.search(r'\d', candidate):
                cn_name = candidate
                episode = int(ep_str)
                title_source = "sticky_episode_fix"

    if (cn_name or en_name) and not year and file_path:
        if parent_dir_name and parent_dir_name != "/":
            year_match = re.search(r'(?<!\d)((?:19|20)\d{2})(?!\d)', parent_dir_name)
            if year_match:
                year = int(year_match.group(1))
                if not quiet:
                    logger.debug(f"[MediaIdentify] 从父目录补充年份: {parent_dir_name} -> {year}")

    title = cn_name if cn_name else en_name
    need_parent = (not title or (not cn_name and (len(title) <= 3 or title.lower() in NOISY_SHORT_WORDS))) if title else True
    if need_parent and file_path:
        parent_name = os.path.basename(os.path.dirname(file_path))
        if parent_name and parent_name != "/":
            if not quiet:
                logger.debug(f"[MediaIdentify] 文件名无标题，尝试父目录识别: {filename} -> {parent_name}")
            parent_meta = MetaInfo(_preprocess_dir_name(parent_name))
            cn_name = parent_meta.cn_name or cn_name
            en_name = parent_meta.en_name or en_name
            if not year:
                year = parent_meta.year
            if season is None:
                season = parent_meta.begin_season
            if episode is None:
                episode = parent_meta.begin_episode
            title = cn_name if cn_name else en_name
            title_source = "parent"
            if not title:
                grandparent = os.path.basename(os.path.dirname(os.path.dirname(file_path)))
                if grandparent and grandparent not in {"/", "."}:
                    if not quiet:
                        logger.debug(f"[MediaIdentify] 父目录仍无标题，尝试上级目录: {grandparent}")
                    gp_meta = MetaInfo(_preprocess_dir_name(grandparent))
                    cn_name = gp_meta.cn_name or cn_name
                    en_name = gp_meta.en_name or en_name
                    if not year:
                        year = gp_meta.year
                    if season is None:
                        season = gp_meta.begin_season
                    if episode is None:
                        episode = gp_meta.begin_episode
                    title = cn_name if cn_name else en_name
                    title_source = "grandparent"

    if en_name:
        en_name = re.sub(r'(?i)\b([a-z]+)sp\b', r'\1', en_name).strip()

    title = cn_name if cn_name else en_name
    if not title:
        logger.warning(f"[MediaIdentify] 无法从文件名 '{filename}' 及其目录路径中提取标题")
        return None

    media_type = _normalize_media_type(path_meta.type, season, episode, force_movie, media_type_hint)
    prefer_file_title_on_dir_conflict, conflict_dir_tmdb_source = _dir_title_tmdb_conflicts_with_file_title(
        file_meta,
        title,
        file_path,
        media_type,
    )
    if prefer_file_title_on_dir_conflict:
        file_cn_name, file_en_name, file_title = _select_titles_from_meta(file_meta)
        cn_name = file_cn_name or ""
        en_name = file_en_name or ""
        title = file_title
        if file_meta.year:
            year = file_meta.year
        title_source = "filename_conflict_priority"
        if not quiet:
            logger.info(
                f"[MediaIdentify] 文件名标题与目录TMDb线索不一致，优先使用文件名: 文件名标题={title} | 目录ID来源={conflict_dir_tmdb_source}"
            )

    title_year_candidates = (
        _collect_same_title_year_candidates(file_path, title, file_meta, year)
        if media_type == "tv"
        else []
    )

    title_key = (
        _normalize_title_for_match(cn_name),
        _normalize_title_for_match(en_name),
        media_type,
        season or "",
        year or "",
    )
    direct_tmdb_cache_key = _build_direct_tmdb_cache_key(title_key, media_type, file_path)

    cached_tmdb_id_direct, cached_tmdb_id_source = _get_cached_direct_tmdb_id(direct_tmdb_cache_key)
    if cached_tmdb_id_direct:
        tmdb_id_direct = cached_tmdb_id_direct
        tmdb_id_source = cached_tmdb_id_source
    else:
        path_tmdb_id = None if prefer_file_title_on_dir_conflict else path_meta.tmdbid
        tmdb_id_direct = file_meta.tmdbid or _extract_tmdb_id_from_text(filename) or path_tmdb_id
        tmdb_id_source = "file"
        if not tmdb_id_direct:
            if prefer_file_title_on_dir_conflict:
                tmdb_id_direct, tmdb_id_source = None, ""
            else:
                tmdb_id_direct, tmdb_id_source = _extract_tmdb_id_from_dirs(file_path, media_type)
            if tmdb_id_direct:
                _set_cached_direct_tmdb_id(direct_tmdb_cache_key, tmdb_id_direct, tmdb_id_source)

    meta_info = _enrich_meta_info_from_title({
        "resource_pix": path_meta.resource_pix or "",
        "resource_type": path_meta.resource_type or "",
        "resource_effect": path_meta.resource_effect or "",
        "video_effect": path_meta.video_effect or "",
        "color_depth": getattr(path_meta, "color_depth", "") or "",
        "video_encode": path_meta.video_encode or "",
        "audio_encode": path_meta.audio_encode or "",
        "web_source": path_meta.web_source or "",
        "resource_team": path_meta.resource_team or "",
        "release_group": getattr(path_meta, "release_group", "") or "",
        "fps": f"{path_meta.fps}FPS" if path_meta.fps else "",
        "part": path_meta.part or "",
    }, filename, path_meta)

    folder_fallback_titles: list[str] = []
    folder_fallback_title = ""
    folder_fallback_cn_name = ""
    folder_fallback_en_name = ""
    folder_fallback_year = None
    parent_title = ""
    if file_path and parent_dir_name and parent_dir_name not in {"/", "."}:
        parent_meta = MetaInfo(_preprocess_dir_name(parent_dir_name))
        parent_title = parent_meta.cn_name or ""
    if prefer_file_title_on_dir_conflict:
        titles_to_try = _build_titles_to_try(title, cn_name, en_name, file_meta.en_name)
        fallback_dir_name = ""
        if conflict_dir_tmdb_source == "parent":
            fallback_dir_name = parent_dir_name
        elif conflict_dir_tmdb_source == "grandparent" and file_path:
            fallback_dir_name = os.path.basename(os.path.dirname(os.path.dirname(file_path)))
        if fallback_dir_name:
            cleaned_fallback_dir = _strip_tmdb_id_from_text(fallback_dir_name)
            fallback_meta = MetaInfo(_preprocess_dir_name(cleaned_fallback_dir))
            fallback_cn, fallback_en, fallback_title = _select_titles_from_meta(fallback_meta)
            folder_fallback_title = fallback_title or cleaned_fallback_dir
            folder_fallback_cn_name = fallback_cn or ""
            folder_fallback_en_name = fallback_en or ""
            folder_fallback_year = fallback_meta.year
            folder_fallback_titles = _build_titles_to_try(cleaned_fallback_dir, fallback_title, fallback_cn, fallback_en)
    else:
        titles_to_try = _build_titles_to_try(parent_title, title, cn_name, en_name, file_meta.en_name)

    return {
        "filename": filename,
        "file_path": file_path or "",
        "title": title,
        "cn_name": cn_name,
        "en_name": en_name,
        "year": year,
        "season": season,
        "episode": episode,
        "media_type": media_type,
        "meta_info": meta_info,
        "titles_to_try": titles_to_try,
        "title_year_candidates": title_year_candidates,
        "folder_fallback_titles": folder_fallback_titles,
        "folder_fallback_title": folder_fallback_title,
        "folder_fallback_cn_name": folder_fallback_cn_name,
        "folder_fallback_en_name": folder_fallback_en_name,
        "folder_fallback_year": folder_fallback_year,
        "tmdb_id_direct": tmdb_id_direct,
        "tmdb_id_source": tmdb_id_source,
        "title_source": title_source,
        "title_key": title_key,
        "group_key": title_key[:2] + (media_type, year or ""),  # 不含 season，用于整理分组
    }


async def _search_tmdb_for_title(parsed: dict, api_key: str, failed_cache: set) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _search_tmdb_for_title_sync, parsed, api_key, failed_cache)


def _is_valid_tv_match(media_type: str, season: Optional[int], tmdb_id: int, api_key: str) -> bool:
    if media_type == 'tv' and season is not None and season > 0:
        return _verify_season_exists(tmdb_id, season, api_key)
    return True


def _tv_season_year_matches(media_type: str, season: Optional[int], year: Optional[int], tmdb_id: int, api_key: str) -> bool:
    if media_type != 'tv' or not year or season is None or season <= 0:
        return False
    return _get_tv_season_year(tmdb_id, season, api_key) == str(year)



def _search_tmdb_candidates(titles_to_try: list[str], filename: str, media_type: str, year: Optional[int], season: Optional[int], api_key: str, log_prefix: str = "[MediaIdentify]") -> Optional[dict]:
    from core import tmdb

    item_type = "movie" if media_type == "movie" else "tv"

    for search_title in titles_to_try:
        if not search_title:
            continue

        year_results = tmdb.search_media(search_title, api_key, item_type, year=year) if year else None
        all_results = tmdb.search_media(search_title, api_key, item_type, year=None)
        results = list(year_results or [])
        seen_ids = {result.get('id') for result in results}
        for result in all_results or []:
            if result.get('id') not in seen_ids:
                results.append(result)
                seen_ids.add(result.get('id'))
        if not results:
            continue

        norm_search = _normalize_title_for_match(search_title)

        if media_type == "tv" and year and season is not None and len(year_results or []) == 1:
            result = (year_results or [])[0]
            tmdb_id = result.get('id')
            res_title = result.get('name') or ''
            res_orig = result.get('original_name') or ''
            if (
                (_normalize_title_for_match(res_title) == norm_search or _normalize_title_for_match(res_orig) == norm_search)
                and _is_valid_tv_match(media_type, season, tmdb_id, api_key)
            ):
                logger.debug(f"{log_prefix} 剧集年份唯一候选匹配: '{filename}' -> {res_title} S{season} ({year}) (ID: {tmdb_id})")
                return {"tmdb_id": tmdb_id, "media_type": media_type, "title": res_title}

        exact_matches = []
        contains_matches = []
        for result in results:
            res_title = result.get('title') if item_type == 'movie' else result.get('name')
            res_orig = result.get('original_title') if item_type == 'movie' else result.get('original_name')
            norm_title = _normalize_title_for_match(res_title)
            norm_orig = _normalize_title_for_match(res_orig)
            if norm_title == norm_search or norm_orig == norm_search:
                exact_matches.append(result)
            elif norm_search and (norm_search in norm_title or norm_search in norm_orig):
                contains_matches.append(result)

        if media_type == "tv" and year and season is not None:
            season_year_matches = []
            season_year_seen = set()
            for result in results:
                tmdb_id = result.get('id')
                if not tmdb_id or tmdb_id in season_year_seen:
                    continue
                res_title = result.get('name') or ''
                res_orig = result.get('original_name') or ''
                if _normalize_title_for_match(res_title) != norm_search and _normalize_title_for_match(res_orig) != norm_search:
                    continue
                if _tv_season_year_matches(media_type, season, year, tmdb_id, api_key):
                    season_year_matches.append(result)
                    season_year_seen.add(tmdb_id)
            if len(season_year_matches) == 1:
                result = season_year_matches[0]
                tmdb_id = result.get('id')
                res_title = result.get('name') or result.get('original_name') or ''
                logger.debug(f"{log_prefix} 剧集季年份唯一候选匹配: '{filename}' -> {res_title} S{season} ({year}) (ID: {tmdb_id})")
                return {"tmdb_id": tmdb_id, "media_type": media_type, "title": res_title}

        if year:
            for result in exact_matches:
                tmdb_id = result.get('id')
                date_field = result.get('release_date') if item_type == 'movie' else result.get('first_air_date')
                res_year = str(date_field)[:4] if date_field else None
                if res_year == str(year) and _is_valid_tv_match(media_type, season, tmdb_id, api_key):
                    res_title = result.get('title') if item_type == 'movie' else result.get('name')
                    logger.debug(f"{log_prefix} 标题年份匹配: '{filename}' -> {res_title} ({res_year}) (ID: {tmdb_id})")
                    return {"tmdb_id": tmdb_id, "media_type": media_type, "title": res_title}

            for result in exact_matches:
                tmdb_id = result.get('id')
                if _tv_season_year_matches(media_type, season, year, tmdb_id, api_key):
                    res_title = result.get('title') if item_type == 'movie' else result.get('name')
                    logger.debug(f"{log_prefix} 标题季年份匹配: '{filename}' -> {res_title} S{season} ({year}) (ID: {tmdb_id})")
                    return {"tmdb_id": tmdb_id, "media_type": media_type, "title": res_title}

        for result in exact_matches:
            tmdb_id = result.get('id')
            res_title = result.get('title') if item_type == 'movie' else result.get('name')
            if year:
                date_field = result.get('release_date') if item_type == 'movie' else result.get('first_air_date')
                res_year = str(date_field)[:4] if date_field else None
                if res_year and res_year != str(year) and not _tv_season_year_matches(media_type, season, year, tmdb_id, api_key):
                    logger.debug(f"{log_prefix} 跳过年份不符的精确匹配: '{filename}' -> {res_title} ({res_year}) (ID: {tmdb_id})")
                    continue
            logger.debug(f"{log_prefix} 精确匹配: '{filename}' -> {res_title} (ID: {tmdb_id})")
            if _is_valid_tv_match(media_type, season, tmdb_id, api_key):
                return {"tmdb_id": tmdb_id, "media_type": media_type, "title": res_title}

        if year and media_type != "tv":
            for result in contains_matches:
                tmdb_id = result.get('id')
                date_field = result.get('release_date') if item_type == 'movie' else result.get('first_air_date')
                res_year = str(date_field)[:4] if date_field else None
                if res_year == str(year) and _is_valid_tv_match(media_type, season, tmdb_id, api_key):
                    res_title = result.get('title') if item_type == 'movie' else result.get('name')
                    logger.debug(f"{log_prefix} 包含年份匹配: '{filename}' -> {res_title} ({res_year}) (ID: {tmdb_id})")
                    return {"tmdb_id": tmdb_id, "media_type": media_type, "title": res_title}

        if not year:
            for result in contains_matches:
                tmdb_id = result.get('id')
                logger.debug(f"{log_prefix} 包含匹配: '{filename}' -> {result.get('title') if item_type == 'movie' else result.get('name')} (ID: {tmdb_id})")
                if _is_valid_tv_match(media_type, season, tmdb_id, api_key):
                    return {"tmdb_id": tmdb_id, "media_type": media_type, "title": result.get('title') if item_type == 'movie' else result.get('name')}
        elif contains_matches:
            logger.debug(f"{log_prefix} 带年份标题未命中，跳过无年份包含兜底: '{filename}' -> {search_title} ({year})")

        if year and len(results) == 1:
            result = results[0]
            date_field = result.get('release_date') if item_type == 'movie' else result.get('first_air_date')
            res_year = str(date_field)[:4] if date_field else None
            if res_year and res_year == str(year):
                res_title = result.get('title') if item_type == 'movie' else result.get('name')
                res_orig = result.get('original_title') if item_type == 'movie' else result.get('original_name')
                tmdb_id = result.get('id')
                if media_type == "tv" and _normalize_title_for_match(res_title) != norm_search and _normalize_title_for_match(res_orig) != norm_search:
                    continue
                logger.debug(f"{log_prefix} 年份匹配: '{filename}' -> {res_title} ({res_year}) (ID: {tmdb_id})")
                if _is_valid_tv_match(media_type, season, tmdb_id, api_key):
                    return {"tmdb_id": tmdb_id, "media_type": media_type, "title": res_title}

    if media_type == "movie":
        weak_variants: list[str] = []
        for t in titles_to_try:
            weak_variants.extend(_build_movie_weak_title_variants(t))
        weak_variants = list(dict.fromkeys(weak_variants))

        for weak_title in weak_variants:
            if not weak_title:
                continue
            weak_results = tmdb.search_media(weak_title, api_key, "movie", year=year)
            if not weak_results and year:
                weak_results = tmdb.search_media(weak_title, api_key, "movie", year=None)
            if not weak_results:
                continue

            def _sort_key(r):
                date = r.get('release_date') or ''
                y = str(date)[:4] if date else ''
                year_penalty = 0 if (year and y == str(year)) else 1
                return (year_penalty, -float(r.get('popularity') or 0))

            weak_results = sorted(weak_results, key=_sort_key)
            picked = None
            for weak_result in weak_results:
                if _movie_weak_result_is_plausible(weak_title, weak_result, year):
                    picked = weak_result
                    break
            if not picked:
                logger.debug(f"{log_prefix} 弱匹配候选相似度不足，跳过: '{filename}' -> '{weak_title}'")
                continue
            tmdb_id = picked.get('id')
            res_title = picked.get('title') or picked.get('original_title') or ''
            logger.debug(f"{log_prefix} 弱匹配命中: '{filename}' -> '{weak_title}' -> {res_title} (ID: {tmdb_id})")
            return {"tmdb_id": tmdb_id, "media_type": media_type, "title": res_title}

    return None



def _should_try_douban_fallback(parsed: dict) -> bool:
    media_type = str(parsed.get("media_type") or "")
    if media_type != "tv":
        return False
    title = str(parsed.get("cn_name") or parsed.get("title") or "")
    return bool(title and StringUtils.is_chinese(title))



def _extract_parent_series_aliases(*titles: str) -> list[str]:
    variants: list[str] = []
    seen = set()

    def _add(value: str):
        cleaned = re.sub(r'\s+', ' ', str(value or '')).strip().strip('.-_：:')
        normalized = _normalize_title_for_match(cleaned)
        if not cleaned or not normalized or normalized in seen:
            return
        seen.add(normalized)
        variants.append(cleaned)

    for title in titles:
        raw = str(title or '').strip()
        if not raw:
            continue

        patterns = [
            r'^(.*?)\s*第\s*[0-9一二三四五六七八九十百零两]+\s*季\s*$',
            r'^(.*?)\s*Season\s*\d+\s*$',
            r'^(.*?)(?<!\d)(\d+)\s*$',
            r'^(.*?)(?<![A-Za-z])(I|II|III|IV|V|VI|VII|VIII|IX|X)\s*$',
        ]
        for pattern in patterns:
            match = re.match(pattern, raw, flags=re.IGNORECASE)
            if not match:
                continue
            base = str(match.group(1) or '').strip().rstrip('：:.-_ ')
            if len(base) >= 2:
                _add(base)

        if '：' in raw or ':' in raw:
            prefix = re.split(r'[：:]', raw, 1)[0].strip()
            if len(prefix) >= 2:
                _add(prefix)

    return variants



def _extract_parent_series_season_hint(*titles: str) -> Optional[int]:
    for title in titles:
        raw = str(title or '').strip()
        if not raw:
            continue

        match = re.match(r'^.*?\s*第\s*([0-9一二三四五六七八九十百零两]+)\s*季\s*$', raw)
        if match:
            try:
                import cn2an
                season_num = int(cn2an.cn2an(match.group(1), mode='smart'))
                if season_num > 1:
                    return season_num
            except Exception:
                pass

        match = re.match(r'^.*?\s*Season\s*(\d+)\s*$', raw, flags=re.IGNORECASE)
        if match:
            season_num = int(match.group(1))
            if season_num > 1:
                return season_num

        match = re.match(r'^(.*?)(?<!\d)(\d{1,2})\s*$', raw)
        if match:
            base = str(match.group(1) or '').strip().rstrip('：:.-_ ')
            season_num = int(match.group(2))
            if len(base) >= 2 and 1 < season_num <= 30:
                return season_num

        match = re.match(r'^(.*?)(?<![A-Za-z])(I|II|III|IV|V|VI|VII|VIII|IX|X)\s*$', raw, flags=re.IGNORECASE)
        if match:
            base = str(match.group(1) or '').strip().rstrip('：:.-_ ')
            season_num = int(_ROMAN_MAP.get(str(match.group(2) or '').lower(), '0') or 0)
            if len(base) >= 2 and season_num > 1:
                return season_num

    return None



def _search_tmdb_via_douban_fallback(parsed: dict, api_key: str) -> Optional[dict]:
    from core import tmdb
    from core.douban import DoubanApi

    if not _should_try_douban_fallback(parsed):
        return None

    filename = parsed["filename"]
    media_type = parsed["media_type"]
    season = parsed.get("season")
    fallback_year = parsed.get("year")
    douban_queries = _build_titles_to_try(
        str(parsed.get("cn_name") or ""),
        str(parsed.get("title") or ""),
    )[:3]
    if not douban_queries:
        return None

    douban_api = DoubanApi()
    for query in douban_queries:
        try:
            logger.debug(f"[MediaIdentify][Douban] TMDb 未命中，尝试豆瓣搜索: '{query}'")
            search_items = douban_api.search(query, count=3) or []
        except Exception as e:
            logger.debug(f"[MediaIdentify][Douban] 搜索失败: '{query}' err={e}")
            continue

        for item in search_items:
            target = item.get("target") if isinstance(item, dict) else None
            target = target if isinstance(target, dict) else (item if isinstance(item, dict) else {})
            douban_id = str(target.get("id") or "").strip()
            if not douban_id:
                continue

            douban_link = f"https://movie.douban.com/subject/{douban_id}/"
            details = douban_api.get_details_from_douban_link(douban_link, mtype=media_type)
            if not details:
                continue

            imdb_id = str(details.get("imdb_id") or "").strip()
            if imdb_id:
                tmdb_id = tmdb.get_tmdb_id_by_imdb_id(imdb_id, api_key, media_type)
                if tmdb_id and _is_valid_tv_match(media_type, season, tmdb_id, api_key):
                    matched_title = details.get("title") or parsed.get("title") or filename
                    logger.info(f"[MediaIdentify][Douban] IMDb 回退命中: '{filename}' -> {matched_title} (TMDb:{tmdb_id})")
                    return {"tmdb_id": tmdb_id, "media_type": media_type, "title": matched_title}

            aliases = [str(alias or "") for alias in (details.get("aliases") or [])]
            parent_aliases = _extract_parent_series_aliases(
                str(details.get("title") or ""),
                str(details.get("original_title") or ""),
                *aliases,
            )
            season_hint = _extract_parent_series_season_hint(
                str(details.get("title") or ""),
                str(details.get("original_title") or ""),
                *aliases,
            )
            detail_titles = _build_titles_to_try(
                str(details.get("original_title") or ""),
                str(details.get("title") or ""),
                *aliases,
                *parent_aliases,
            )
            if not detail_titles:
                continue

            matched = _search_tmdb_candidates(
                detail_titles,
                filename,
                media_type,
                fallback_year or details.get("year"),
                season_hint or season,
                api_key,
                log_prefix="[MediaIdentify][Douban]",
            )
            if matched:
                if season_hint and media_type == "tv":
                    matched["season"] = season_hint
                logger.info(f"[MediaIdentify][Douban] 标题回退命中: '{filename}' -> {matched.get('title', '')} (TMDb:{matched.get('tmdb_id')})")
                return matched

    return None



def _tmdb_search_cache_key(parsed: dict) -> str:
    payload = {
        "v": 5,
        "media_type": parsed.get("media_type", ""),
        "title_key": parsed.get("title_key") or (),
        "titles_to_try": parsed.get("titles_to_try") or [],
        "title_year_candidates": parsed.get("title_year_candidates") or [],
        "folder_fallback_titles": parsed.get("folder_fallback_titles") or [],
        "year": parsed.get("year"),
        "season": parsed.get("season"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_cached_tmdb_search_result(parsed: dict) -> tuple[bool, Optional[dict]]:
    if not parsed or not _ensure_tmdb_sqlite_cache_schema():
        return False, None
    cache_key = _tmdb_search_cache_key(parsed)
    try:
        from core.cache_db import tmdb_cache_db as cache_db
        now_ts = _time.time()
        with cache_db() as conn:
            row = conn.execute(
                "SELECT hit, payload_json, expires_at FROM tmdb_search_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if not row:
            return False, None
        if float(row["expires_at"] or 0) <= now_ts:
            return False, None
        if not int(row["hit"] or 0):
            logger.trace(f"[TMDbCache] 搜索失败缓存命中: {parsed.get('filename', '')}")
            return True, None
        data = json.loads(row["payload_json"] or "{}")
        if not isinstance(data, dict) or not data.get("tmdb_id"):
            return False, None
        logger.trace(f"[TMDbCache] 搜索缓存命中: {parsed.get('filename', '')} -> TMDb:{data.get('tmdb_id')}")
        return True, data
    except Exception as e:
        logger.debug(f"[TMDbCache] 读取搜索缓存失败: 文件：{parsed.get('filename', '') if parsed else ''} | 错误：{e}")
        return False, None


def _set_cached_tmdb_search_result(parsed: dict, result: Optional[dict]) -> None:
    if not parsed or not _ensure_tmdb_sqlite_cache_schema():
        return
    cache_key = _tmdb_search_cache_key(parsed)
    hit = 1 if result else 0
    ttl_seconds = _TMDB_SEARCH_CACHE_HIT_TTL_SECONDS if hit else _TMDB_SEARCH_CACHE_MISS_TTL_SECONDS
    payload_json = json.dumps(result or {}, ensure_ascii=False, separators=(",", ":"))
    try:
        from core.cache_db import tmdb_cache_db as cache_db
        now_ts = _time.time()
        with cache_db(write=True) as conn:
            conn.execute(
                """
                INSERT INTO tmdb_search_cache(cache_key, fetched_at, expires_at, hit, payload_json)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at,
                    hit = excluded.hit,
                    payload_json = excluded.payload_json
                """,
                (cache_key, now_ts, now_ts + ttl_seconds, hit, payload_json),
            )
        if hit:
            logger.debug(f"[TMDbCache] 搜索缓存写入: 文件：{parsed.get('filename', '')} | TMDb编号：{(result or {}).get('tmdb_id')}")
    except Exception as e:
        logger.debug(f"[TMDbCache] 写入搜索缓存失败: 文件：{parsed.get('filename', '') if parsed else ''} | 错误：{e}")


def _search_tmdb_for_title_sync(parsed: dict, api_key: str, failed_cache: set) -> Optional[dict]:
    """
    根据 _parse_filename 的解析结果搜索 TMDb。
    返回 {"tmdb_id": int, "media_type": str, "title": str} 或 None。
    failed_cache: 本次任务内的搜索失败缓存，key 为不含 season 的 series_key。
    """
    title_key = parsed["title_key"]
    # 用不含 season 的 key 做失败缓存，同一部剧任意一季查不到就跳过其他季
    series_key = title_key[:2] + title_key[3:]  # 去掉 season（index 2）
    if series_key in failed_cache:
        return None

    filename = parsed["filename"]
    titles_to_try = parsed["titles_to_try"]
    media_type = parsed["media_type"]
    year = parsed["year"]
    season = parsed["season"]
    tmdb_id_direct = parsed["tmdb_id_direct"]

    if tmdb_id_direct:
        return {
            "tmdb_id": tmdb_id_direct,
            "media_type": media_type,
            "title": parsed["title"],
            "season": parsed.get("season"),
            "episode": parsed.get("episode"),
        }

    search_cache_found, cached_search = _get_cached_tmdb_search_result(parsed)
    if search_cache_found:
        if cached_search:
            return cached_search
        failed_cache.add(series_key)
        return None

    candidate_years = []
    seen_candidate_years = set()
    for item in parsed.get("title_year_candidates") or []:
        candidate_year = str((item or {}).get("year") or "").strip()
        if candidate_year and candidate_year not in seen_candidate_years:
            candidate_years.append(candidate_year)
            seen_candidate_years.add(candidate_year)
    if year and str(year) not in seen_candidate_years:
        candidate_years.append(str(year))
        seen_candidate_years.add(str(year))

    matched = None
    if candidate_years:
        for candidate_year in candidate_years:
            matched = _search_tmdb_candidates(titles_to_try, filename, media_type, candidate_year, season, api_key)
            if matched:
                break
    else:
        matched = _search_tmdb_candidates(titles_to_try, filename, media_type, year, season, api_key)
    if not matched and parsed.get("title_source") == "filename_conflict_priority":
        folder_fallback_titles = parsed.get("folder_fallback_titles") or []
        if folder_fallback_titles:
            fallback_year = parsed.get("folder_fallback_year") or year
            logger.info(
                f"[MediaIdentify] 文件名识别未命中，回退文件夹标题识别: {filename} -> {' | '.join(folder_fallback_titles)}"
            )
            matched = _search_tmdb_candidates(
                folder_fallback_titles,
                filename,
                media_type,
                fallback_year,
                season,
                api_key,
                log_prefix="[MediaIdentify][FolderFallback]",
            )
            if not matched:
                folder_parsed = dict(parsed)
                folder_parsed["title_source"] = "folder_fallback"
                folder_parsed["title"] = parsed.get("folder_fallback_title") or folder_fallback_titles[0]
                folder_parsed["cn_name"] = parsed.get("folder_fallback_cn_name") or (
                    folder_parsed["title"] if StringUtils.is_chinese(str(folder_parsed["title"] or "")) else ""
                )
                folder_parsed["en_name"] = parsed.get("folder_fallback_en_name") or ""
                folder_parsed["year"] = fallback_year
                folder_parsed["titles_to_try"] = folder_fallback_titles
                folder_parsed["folder_fallback_titles"] = []
                matched = _search_tmdb_via_douban_fallback(folder_parsed, api_key)
    if matched:
        _set_cached_tmdb_search_result(parsed, matched)
        return matched

    douban_matched = _search_tmdb_via_douban_fallback(parsed, api_key)
    if douban_matched:
        _set_cached_tmdb_search_result(parsed, douban_matched)
        return douban_matched

    logger.info(f"[MediaIdentify] 完全未找到匹配: '{filename}' (标题: {parsed['title']})")
    failed_cache.add(series_key)
    _set_cached_tmdb_search_result(parsed, None)
    return None


def _identify_media_from_filename(filename: str, media_type_hint: str = None, file_path: str = "") -> Optional[dict]:
    """
    兼容旧接口：解析 + 搜索合一。
    """
    from core.configs import global_config

    api_key = global_config.tmdb_key
    if not api_key:
        logger.error("[MediaIdentify] TMDb API Key 未配置，无法自动识别")
        return None

    parsed = _parse_filename(filename, media_type_hint, file_path)
    if not parsed:
        return None

    failed_cache = set()
    import asyncio
    search_result = asyncio.get_event_loop().run_until_complete(
        _search_tmdb_for_title(parsed, api_key, failed_cache)
    )
    if not search_result:
        return None

    return {
        "tmdb_id": search_result["tmdb_id"],
        "media_type": parsed["media_type"],
        "season": search_result.get("season", parsed["season"]),
        "episode": search_result.get("episode", parsed["episode"]),
        "title": search_result["title"],
        "meta_info": parsed["meta_info"],
    }
