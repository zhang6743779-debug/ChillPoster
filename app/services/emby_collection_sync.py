import json
import os
import re
import threading
import time
from html import escape as html_escape
from pathlib import Path
from typing import Optional

import requests

from app.routers.config_302 import get_emby_configs_sync
from core.configs import CONFIG_DIR
from core.emby_client import DISCOVER_SCAN_ITEM_PAGE_LIMIT, EmbyClient
from core.logger import logger
from core.organizer import VIDEO_EXTENSIONS


TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/original"
PENDING_COLLECTION_SYNC_FILE = f"{CONFIG_DIR}/emby_collection_sync_pending.json"
PENDING_COLLECTION_SYNC_TTL_SECONDS = 7 * 86400
_PENDING_LOCK = threading.Lock()
_BACKFILL_PAGE_LIMIT = max(100, min(DISCOVER_SCAN_ITEM_PAGE_LIMIT, 1000))


def _clean_text(value) -> str:
    return str(value or "").strip()


def _normalize_path(path: str) -> str:
    return str(path or "").replace("\\", "/").rstrip("/").lower()


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _image_url(path: str) -> str:
    value = _clean_text(path)
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"{TMDB_IMAGE_BASE_URL}{value}"


def build_movie_collection_sync_payload(tmdb_data: dict, variables: dict, target_path: str) -> Optional[dict]:
    if not isinstance(tmdb_data, dict):
        return None

    collection = tmdb_data.get("collection_details") if isinstance(tmdb_data.get("collection_details"), dict) else {}
    belongs = tmdb_data.get("belongs_to_collection") if isinstance(tmdb_data.get("belongs_to_collection"), dict) else {}
    if not collection and not belongs:
        return None

    collection_id = collection.get("id") or belongs.get("id")
    collection_name = _clean_text(collection.get("name") or belongs.get("name"))
    movie_tmdb_id = _clean_text((variables or {}).get("tmdb_id") or tmdb_data.get("id"))
    if not collection_id or not collection_name or not movie_tmdb_id:
        return None

    return {
        "movie_tmdb_id": movie_tmdb_id,
        "movie_title": _clean_text((variables or {}).get("title") or tmdb_data.get("title")),
        "movie_year": _clean_text((variables or {}).get("year") or (tmdb_data.get("release_date") or "")[:4]),
        "target_path": _clean_text(target_path),
        "collection_tmdb_id": _clean_text(collection_id),
        "collection_name": collection_name,
        "collection_overview": _clean_text(collection.get("overview") or belongs.get("overview")),
        "poster_path": _clean_text(collection.get("poster_path") or belongs.get("poster_path")),
        "backdrop_path": _clean_text(collection.get("backdrop_path") or belongs.get("backdrop_path")),
    }


def schedule_emby_collection_sync(payloads: list[dict]):
    now = time.time()
    incoming = []
    for payload in _dedupe_payloads(payloads):
        item = dict(payload)
        item.setdefault("created_at", now)
        item["updated_at"] = now
        incoming.append(item)

    if not incoming:
        return 0

    with _PENDING_LOCK:
        pending = _prune_pending_payloads(_load_pending_payloads_unlocked(), now=now)
        pending_by_key = {_payload_key(item): item for item in pending}
        added = 0
        updated = 0

        for item in incoming:
            key = _payload_key(item)
            if key in pending_by_key:
                created_at = pending_by_key[key].get("created_at") or item.get("created_at") or now
                pending_by_key[key].update(item)
                pending_by_key[key]["created_at"] = created_at
                pending_by_key[key]["updated_at"] = now
                updated += 1
            else:
                pending_by_key[key] = item
                added += 1

        pending = _dedupe_payloads(list(pending_by_key.values()))
        _save_pending_payloads_unlocked(pending)

    logger.info(
        f"[EmbyCollection] 已记录待同步合集: 新增 {added} 条, 更新 {updated} 条, "
        f"待处理 {len(pending)} 条；等待 Emby/神医通知触发"
    )
    return len(incoming)


def sync_emby_collections_for_payloads(payloads: list[dict]):
    payloads = _dedupe_payloads(payloads)
    if not payloads:
        return {"status": "empty", "total": 0, "synced": 0, "pending": 0}

    embys = get_emby_configs_sync()
    if not embys:
        logger.debug("[EmbyCollection] 未配置 Emby，跳过合集同步")
        return {"status": "disabled", "total": len(payloads), "synced": 0, "pending": len(payloads)}

    synced_keys = set()
    failed = 0
    server_count = 0
    for idx, server in enumerate(embys):
        if not isinstance(server, dict) or not server.get("enabled", True):
            continue
        if not server.get("url") or not server.get("key"):
            continue

        server_count += 1
        server_name = server.get("name") or server.get("url") or f"Emby[{idx}]"
        client = EmbyClient(server.get("url", ""), server.get("key", ""), server.get("public_host"))
        try:
            for payload in payloads:
                if _payload_identity(payload) in synced_keys:
                    continue
                movie_item = _find_movie_item(client, payload)
                if not movie_item:
                    continue
                try:
                    if _sync_one_payload(client, payload, movie_item, server_name):
                        synced_keys.add(_payload_identity(payload))
                    else:
                        failed += 1
                except Exception as e:
                    failed += 1
                    logger.error(
                        f"[EmbyCollection] 合集同步异常: "
                        f"{payload.get('collection_name') or payload.get('collection_tmdb_id')} | "
                        f"{server_name} | {e}",
                        exc_info=True,
                    )
        finally:
            client.close()

    pending_count = len(payloads) - len(synced_keys)
    if pending_count:
        logger.info(f"[EmbyCollection] 仍有 {pending_count} 条合集待 Emby 入库通知后再试")

    return {
        "status": "ok" if synced_keys else "pending",
        "total": len(payloads),
        "synced": len(synced_keys),
        "pending": pending_count,
        "failed": failed,
        "servers": server_count,
        "synced_keys": sorted(synced_keys),
    }


def sync_pending_emby_collections_for_webhook(data: dict | None = None, event_type: str = "") -> dict:
    now = time.time()
    with _PENDING_LOCK:
        pending = _prune_pending_payloads(_load_pending_payloads_unlocked(), now=now)
        _save_pending_payloads_unlocked(pending)

    if not pending:
        return {"status": "empty", "total": 0, "synced": 0, "pending": 0}

    logger.info(
        f"[EmbyCollection] 收到 Emby/神医通知，处理待同步合集: "
        f"event={event_type or 'unknown'} pending={len(pending)}"
    )
    result = sync_emby_collections_for_payloads(pending)
    synced_keys = set(result.get("synced_keys") or [])

    with _PENDING_LOCK:
        current = _prune_pending_payloads(_load_pending_payloads_unlocked(), now=time.time())
        if synced_keys:
            current = [item for item in current if _payload_identity(item) not in synced_keys]
        _save_pending_payloads_unlocked(current)

    result["remaining"] = len(current)
    return result


def run_existing_movie_collection_backfill(run_id: str) -> dict:
    from app.dependencies import ACTIVE_TASKS, update_task_progress
    from app.services.media_organize_tmdb import _fetch_tmdb_data_sync
    from core.configs import global_config

    def is_cancelled() -> bool:
        return bool(ACTIVE_TASKS.get(run_id, {}).get("cancel_requested"))

    def progress(message: str, percent: float, status: str = "running", force: bool = False):
        update_task_progress(
            run_id,
            message,
            percent,
            status,
            detail={
                "task": "collection_backfill",
                "servers": stats["servers"],
                "total": stats["total"],
                "processed": stats["processed"],
                "success": stats["success"],
                "failed": stats["failed"],
                "skipped": stats["no_tmdb"] + stats["no_collection"],
                "library_options_updated": stats["library_options_updated"],
                "library_options_skipped": stats["library_options_skipped"],
                "library_options_failed": stats["library_options_failed"],
                "no_tmdb": stats["no_tmdb"],
                "no_collection": stats["no_collection"],
                "tmdb_failed": stats["tmdb_failed"],
                "nfo_updated": stats["nfo_updated"],
                "nfo_has_set": stats["nfo_has_set"],
                "nfo_missing": stats["nfo_missing"],
                "nfo_unavailable": stats["nfo_unavailable"],
                "nfo_failed": stats["nfo_failed"],
            },
        )

    stats = {
        "servers": 0,
        "total": 0,
        "processed": 0,
        "success": 0,
        "failed": 0,
        "library_options_updated": 0,
        "library_options_skipped": 0,
        "library_options_failed": 0,
        "no_tmdb": 0,
        "no_collection": 0,
        "tmdb_failed": 0,
        "nfo_updated": 0,
        "nfo_has_set": 0,
        "nfo_missing": 0,
        "nfo_unavailable": 0,
        "nfo_failed": 0,
    }

    global_config.load()
    api_key = _clean_text(global_config.tmdb_key)
    if not api_key:
        progress("电影合集补齐失败: 未配置 TMDB API Key", 100, "error", force=True)
        return {"status": "error", "message": "未配置 TMDB API Key", **stats}

    servers = [
        (idx, server)
        for idx, server in enumerate(get_emby_configs_sync() or [])
        if isinstance(server, dict)
        and server.get("enabled", True)
        and _clean_text(server.get("url"))
        and _clean_text(server.get("key"))
    ]
    stats["servers"] = len(servers)
    if not servers:
        progress("电影合集补齐失败: 未配置可用 Emby", 100, "error", force=True)
        return {"status": "error", "message": "未配置可用 Emby", **stats}

    progress("电影合集补齐: 正在调整 Emby 电影库合集阈值...", 1)
    server_movies = []
    for idx, server in servers:
        if is_cancelled():
            progress("电影合集补齐已取消", 100, "stopped", force=True)
            return {"status": "stopped", **stats}

        server_name = server.get("name") or server.get("url") or f"Emby[{idx}]"
        client = EmbyClient(server.get("url", ""), server.get("key", ""), server.get("public_host"))
        try:
            try:
                option_result = client.ensure_movie_library_min_collection_items(1)
                stats["library_options_updated"] += int(option_result.get("updated") or 0)
                stats["library_options_skipped"] += int(option_result.get("skipped") or 0)
                stats["library_options_failed"] += int(option_result.get("failed") or 0)
                logger.info(
                    f"[EmbyCollection] 电影库最小自动合集尺寸检查完成: {server_name} | "
                    f"更新 {option_result.get('updated', 0)} 个, 跳过 {option_result.get('skipped', 0)} 个, "
                    f"失败 {option_result.get('failed', 0)} 个"
                )
            except Exception as e:
                stats["library_options_failed"] += 1
                logger.warning(f"[EmbyCollection] 调整电影库最小自动合集尺寸失败: {server_name} | {e}")

            movies = _list_emby_movie_items(client)
            server_movies.append((server_name, server, movies))
            stats["total"] += len(movies)
            logger.info(f"[EmbyCollection] 已读取 Emby 电影列表: {server_name} | {len(movies)} 部")
        finally:
            client.close()

    if stats["total"] <= 0:
        progress("电影合集补齐完成: 没有找到 Emby 电影", 100, "finished", force=True)
        return {"status": "finished", **stats}

    tmdb_cache: dict[str, Optional[dict]] = {}
    progress("电影合集补齐: 开始补齐合集...", 3)

    for server_name, server, movies in server_movies:
        client = EmbyClient(server.get("url", ""), server.get("key", ""), server.get("public_host"))
        try:
            for movie_item in movies:
                if is_cancelled():
                    progress("电影合集补齐已取消", _backfill_percent(stats), "stopped", force=True)
                    return {"status": "stopped", **stats}

                stats["processed"] += 1
                movie_title = _clean_text(movie_item.get("Name"))
                tmdb_id = _extract_movie_tmdb_id(movie_item)
                if not tmdb_id:
                    stats["no_tmdb"] += 1
                    _update_backfill_progress_throttled(progress, stats, movie_title)
                    continue

                if tmdb_id not in tmdb_cache:
                    try:
                        tmdb_cache[tmdb_id] = _fetch_tmdb_data_sync(int(tmdb_id), "movie", api_key)
                    except Exception as e:
                        logger.warning(f"[EmbyCollection] 获取 TMDB 电影详情失败: {tmdb_id} | {e}")
                        tmdb_cache[tmdb_id] = None

                tmdb_data = tmdb_cache.get(tmdb_id)
                if not tmdb_data:
                    stats["tmdb_failed"] += 1
                    _update_backfill_progress_throttled(progress, stats, movie_title)
                    continue

                payload = build_movie_collection_sync_payload(
                    tmdb_data,
                    {
                        "tmdb_id": tmdb_id,
                        "title": movie_title,
                        "year": _clean_text(movie_item.get("ProductionYear")),
                    },
                    _clean_text(movie_item.get("Path")),
                )
                if not payload:
                    stats["no_collection"] += 1
                    _update_backfill_progress_throttled(progress, stats, movie_title)
                    continue

                if _sync_one_payload(client, payload, {"Id": movie_item.get("Id")}, server_name):
                    stats["success"] += 1
                else:
                    stats["failed"] += 1

                nfo_result = _patch_movie_nfo_collection(movie_item.get("Path"), payload)
                if nfo_result == "updated":
                    stats["nfo_updated"] += 1
                elif nfo_result == "has_set":
                    stats["nfo_has_set"] += 1
                elif nfo_result == "missing":
                    stats["nfo_missing"] += 1
                elif nfo_result == "unavailable":
                    stats["nfo_unavailable"] += 1
                elif nfo_result == "failed":
                    stats["nfo_failed"] += 1

                _update_backfill_progress_throttled(progress, stats, movie_title)
        finally:
            client.close()

    message = (
        f"电影合集补齐完成: 已同步 {stats['success']} 部, "
        f"失败 {stats['failed']} 部, 无合集 {stats['no_collection']} 部, 无TMDB {stats['no_tmdb']} 部"
    )
    progress(message, 100, "finished", force=True)
    return {"status": "finished", **stats}


def _dedupe_payloads(payloads: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for payload in payloads or []:
        if not isinstance(payload, dict):
            continue
        key = _payload_key(payload)
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        deduped.append(payload)
    return deduped


def _payload_key(payload: dict) -> tuple[str, str, str]:
    return (
        _clean_text(payload.get("movie_tmdb_id")),
        _clean_text(payload.get("collection_tmdb_id")),
        _normalize_path(payload.get("target_path", "")),
    )


def _payload_identity(payload: dict) -> str:
    return "|".join(_payload_key(payload))


def _load_pending_payloads_unlocked() -> list[dict]:
    if not os.path.exists(PENDING_COLLECTION_SYNC_FILE):
        return []
    try:
        with open(PENDING_COLLECTION_SYNC_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"[EmbyCollection] 读取待同步合集失败，将重新创建队列: {e}")
        return []

    if isinstance(data, dict):
        payloads = data.get("items") or data.get("payloads") or []
    elif isinstance(data, list):
        payloads = data
    else:
        payloads = []
    return _dedupe_payloads(payloads)


def _save_pending_payloads_unlocked(payloads: list[dict]):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data = {
        "updated_at": time.time(),
        "items": _dedupe_payloads(payloads),
    }
    with open(PENDING_COLLECTION_SYNC_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _prune_pending_payloads(payloads: list[dict], now: float | None = None) -> list[dict]:
    now = now or time.time()
    pruned = []
    expired = 0
    for payload in _dedupe_payloads(payloads):
        item = dict(payload)
        try:
            created_at = float(item.get("created_at") or now)
        except Exception:
            created_at = now
        if now - created_at > PENDING_COLLECTION_SYNC_TTL_SECONDS:
            expired += 1
            continue
        item["created_at"] = created_at
        pruned.append(item)

    if expired:
        logger.info(f"[EmbyCollection] 已清理过期待同步合集: {expired} 条")
    return pruned


def _backfill_percent(stats: dict) -> float:
    total = max(1, int(stats.get("total") or 0))
    processed = max(0, int(stats.get("processed") or 0))
    return min(99.0, 3.0 + (processed / total) * 96.0)


def _update_backfill_progress_throttled(progress_func, stats: dict, movie_title: str = ""):
    processed = int(stats.get("processed") or 0)
    total = int(stats.get("total") or 0)
    if processed == total or processed <= 3 or processed % 10 == 0:
        suffix = f": {movie_title}" if movie_title else ""
        progress_func(f"电影合集补齐: {processed}/{total}{suffix}", _backfill_percent(stats))


def _list_emby_movie_items(client: EmbyClient) -> list[dict]:
    uid = client._get_user_id()
    endpoint = f"emby/Users/{uid}/Items" if uid else "emby/Items"
    items = []
    start = 0
    while True:
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "Fields": "ProviderIds,Path,Name,OriginalTitle,ProductionYear",
            "StartIndex": start,
            "Limit": _BACKFILL_PAGE_LIMIT,
        }
        data = client._request("GET", endpoint, params=params)
        page_items = data.get("Items", []) if isinstance(data, dict) else []
        if not page_items:
            break
        items.extend(page_items)
        total = int((data or {}).get("TotalRecordCount") or 0)
        start += len(page_items)
        if not total or start >= total:
            break
    return items


def _extract_movie_tmdb_id(item: dict) -> str:
    provider_ids = item.get("ProviderIds") or {}
    for key in ("Tmdb", "TMDB", "TMDb", "TheMovieDb", "themoviedb"):
        value = _clean_text(provider_ids.get(key))
        if value:
            return value

    path = _clean_text(item.get("Path"))
    match = re.search(r"(?:tmdbid-|tmdb-|\{tmdb-|\[tmdbid-)(\d+)", path, re.IGNORECASE)
    return match.group(1) if match else ""


def _movie_nfo_candidates(item_path: str) -> tuple[list[Path], bool]:
    value = _clean_text(item_path)
    if not value:
        return [], False

    path = Path(value)
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        parent = path.parent
        candidates = [path.with_suffix(".nfo")]
        if parent.exists():
            candidates.extend(sorted(parent.glob("*.nfo")))
        return _unique_paths(candidates), parent.exists()

    if path.exists() and path.is_dir():
        candidates = sorted(path.glob("*.nfo"))
        candidates.append(path / f"{path.name}.nfo")
        candidates.append(path / "movie.nfo")
        return _unique_paths(candidates), True

    parent = path.parent
    return [path / f"{path.name}.nfo", path / "movie.nfo"], parent.exists()


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen = set()
    result = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _collection_set_xml(payload: dict) -> str:
    name = html_escape(_clean_text(payload.get("collection_name")), quote=False)
    overview = html_escape(_clean_text(payload.get("collection_overview")), quote=False)
    if overview:
        return f"  <set>\n    <name>{name}</name>\n    <overview>{overview}</overview>\n  </set>\n"
    return f"  <set>\n    <name>{name}</name>\n  </set>\n"


def _patch_movie_nfo_collection(item_path: str, payload: dict) -> str:
    candidates, parent_available = _movie_nfo_candidates(item_path)
    if not candidates:
        return "unavailable"

    existing_candidates = [path for path in candidates if path.exists() and path.is_file()]
    if not existing_candidates:
        return "missing" if parent_available else "unavailable"

    for nfo_path in existing_candidates:
        try:
            text = nfo_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = nfo_path.read_text(encoding="utf-8-sig")
            except Exception:
                continue
        except Exception:
            continue

        if re.search(r"<set\b", text, flags=re.IGNORECASE):
            return "has_set"

        set_xml = _collection_set_xml(payload)
        if re.search(r"</originaltitle\s*>", text, flags=re.IGNORECASE):
            updated = re.sub(
                r"(\s*</originaltitle\s*>\s*)",
                lambda match: f"{match.group(1)}{set_xml}",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            updated = re.sub(
                r"(<movie\b[^>]*>\s*)",
                lambda match: f"{match.group(1)}{set_xml}",
                text,
                count=1,
                flags=re.IGNORECASE,
            )

        if updated == text:
            continue
        try:
            nfo_path.write_text(updated, encoding="utf-8")
            logger.info(f"[EmbyCollection] 旧 NFO 已补写合集: {nfo_path}")
            return "updated"
        except Exception as e:
            logger.debug(f"[EmbyCollection] 旧 NFO 补写失败: {nfo_path} | {e}")
            return "failed"

    return "failed"


def _sync_one_payload(client: EmbyClient, payload: dict, movie_item: dict, server_name: str):
    if not movie_item:
        return False

    movie_id = movie_item.get("Id")
    collection_name = _clean_text(payload.get("collection_name"))
    if not movie_id or not collection_name:
        return False

    collection = client.find_collection_by_name(collection_name)
    created = False
    if collection and collection.get("Id"):
        collection_id = collection.get("Id")
        if not client.add_items_to_collection(collection_id, [movie_id]):
            return False
    else:
        result = client.create_collection(collection_name, [movie_id], is_locked=False)
        collection_id = (result or {}).get("Id")
        created = bool(collection_id)
        if not collection_id:
            collection = client.find_collection_by_name(collection_name)
            collection_id = (collection or {}).get("Id")
            created = False

    if not collection_id:
        logger.warning(f"[EmbyCollection] 合集创建失败: {collection_name} | {server_name}")
        return False

    collection = client.get_raw_item(
        collection_id,
        fields="ImageTags,BackdropImageTags,ProviderIds,Overview",
    ) or collection or {}

    _upload_collection_images(client, collection_id, collection, payload, force=created)
    logger.info(
        f"[EmbyCollection] 合集同步完成: {collection_name} <- "
        f"{payload.get('movie_title') or payload.get('movie_tmdb_id')} | {server_name}"
    )
    return True


def _find_movie_item(client: EmbyClient, payload: dict) -> Optional[dict]:
    tmdb_id = _clean_text(payload.get("movie_tmdb_id"))
    target_path = _normalize_path(payload.get("target_path", ""))
    items = client.find_items_by_provider_id("Tmdb", tmdb_id, item_types="Movie", limit=50)
    if items:
        return _pick_best_item_by_path(items, target_path)

    title = _clean_text(payload.get("movie_title"))
    if not title:
        return None

    expected_year = _clean_text(payload.get("movie_year"))
    expected_title = _normalize_name(title)
    candidates = client.search_items(title)
    movie_candidates = []
    for item in candidates or []:
        if item.get("type") != "Movie":
            continue
        item_year = _clean_text(item.get("year"))
        if expected_year and item_year and item_year != expected_year:
            continue
        if expected_title and _normalize_name(item.get("name")) != expected_title:
            continue
        movie_candidates.append({"Id": item.get("id"), "Name": item.get("name"), "Type": "Movie"})

    return movie_candidates[0] if movie_candidates else None


def _pick_best_item_by_path(items: list[dict], target_path: str) -> Optional[dict]:
    if not items:
        return None
    if not target_path:
        return items[0]

    scored = []
    for item in items:
        item_path = _normalize_path(item.get("Path", ""))
        score = 0
        if item_path:
            if item_path == target_path:
                score = 4
            elif item_path.startswith(target_path + "/"):
                score = 3
            elif target_path.startswith(item_path + "/"):
                score = 2
        scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def _upload_collection_images(client: EmbyClient, collection_id: str, collection: dict, payload: dict, force: bool = False):
    image_tags = collection.get("ImageTags") or {}
    backdrop_tags = collection.get("BackdropImageTags") or []

    poster_url = _image_url(payload.get("poster_path", ""))
    if poster_url and (force or not image_tags.get("Primary")):
        image_data = _download_image(poster_url)
        if image_data and client.upload_item_image(collection_id, image_data, "Primary"):
            logger.debug(f"[EmbyCollection] 合集封面已上传: {payload.get('collection_name')}")

    backdrop_url = _image_url(payload.get("backdrop_path", ""))
    if backdrop_url and (force or not backdrop_tags):
        image_data = _download_image(backdrop_url)
        if image_data and client.upload_item_image(collection_id, image_data, "Backdrop"):
            logger.debug(f"[EmbyCollection] 合集背景图已上传: {payload.get('collection_name')}")


def _download_image(url: str) -> bytes:
    if not url:
        return b""
    proxies = None
    try:
        from core.configs import global_config
        global_config.load()
        if global_config.proxy_url:
            proxies = {"http": global_config.proxy_url, "https": global_config.proxy_url}
    except Exception:
        proxies = None

    try:
        response = requests.get(url, timeout=(8, 30), proxies=proxies)
        response.raise_for_status()
        return response.content
    except Exception as e:
        logger.warning(f"[EmbyCollection] 合集图片下载失败: {url} | {e}")
        return b""
