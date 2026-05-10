import os
import json
import uuid
import tempfile
import asyncio
import threading
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, PrivateAttr
from typing import Optional, List
from core.logger import logger
from app.dependencies import update_task_progress

# Re-export from service modules for backward compatibility (main.py imports these)
from app.services.media_organize_state import register_main_event_loop, CONFIG_FILE, VIDEO_EXTS  # noqa: F401
from app.services.media_organize_core import (
    create_life_event_callback,  # noqa: F401
    _run_organize_async,
    _schedule_or_refresh_source_poll,
)
from app.services import media_organize_state as _state
from app.services.media_organize_state import _organize_trigger_lock
from app.services.media_organize_115_ops import (
    _get_115_client,
)
from app.services.media_organize_tmdb import _load_config_data, _fetch_tmdb_data, _build_scraping_config
from app.services.media_organize_scrape import _noop_transfer, _write_nfo, _download_image

router = APIRouter(prefix="/api/media_organize", tags=["media_organize"])


# ==========================================
# 数据模型
# ==========================================

class MediaOrganizeConfig(BaseModel):
    drive_index: int = 0
    source_cid: str = '0'
    source_name: str = '根目录'
    target_cid: str = '0'
    target_name: str = '根目录'
    failed_cid: str = '0'
    failed_name: str = '根目录'
    scrape_enabled: bool = True
    emby_local_scrape: bool = False
    scrape_nfo: bool = True
    scrape_poster: bool = True
    scrape_fanart: bool = True
    scrape_logo: bool = True
    scrape_banner: bool = False
    scrape_thumb: bool = True
    scrape_season_poster: bool = True
    scrape_episode_thumb: bool = True
    policy_nfo: str = 'missing_only'
    policy_poster: str = 'missing_only'
    policy_fanart: str = 'missing_only'
    policy_logo: str = 'missing_only'
    policy_banner: str = 'missing_only'
    policy_thumb: str = 'missing_only'
    policy_season_poster: str = 'missing_only'
    policy_episode_thumb: str = 'missing_only'
    life_monitor_enabled: bool = False
    auto_sync_strm: bool = False
    wash_enabled: bool = False
    wash_by_equivalent_size: bool = False
    wash_tolerance_ratio: float = 0.0
    wash_reserved_1: bool = False
    wash_reserved_2: bool = False
    organize_parse_mode: str = 'ffprobe'
    movie_folder_format: str = '{title} ({year}) {tmdb-{tmdb_id}}'
    movie_rename_format: str = '{en_title}.{year}.{resource_pix}.{web_source}.{resource_type}.{resource_effect}.{video_encode}.{color_depth}.{video_effect}.{fps}.{audio_encode}-{resource_team}'
    tv_folder_format: str = '{title} ({year}) {tmdb-{tmdb_id}}'
    tv_episode_format: str = '{en_title}.{season_episode}.{year}.{resource_pix}.{web_source}.{resource_type}.{video_encode}.{color_depth}.{video_effect}.{fps}.{audio_encode}-{resource_team}'

    class Config:
        extra = "ignore"


class ScrapeRequest(BaseModel):
    cid: str = '0'
    media_type: str = 'movie'
    tmdb_id: int = 0
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    overwrite: bool = False
    drive_index: int = 0


class OrganizeRequest(BaseModel):
    media_type: str = ''
    is_bluray: bool = False
    drive_index: int = 0
    overwrite: bool = False
    _prefetched_source_tree_entries: Optional[list[dict]] = PrivateAttr(default=None)


class Browse115Payload(BaseModel):
    cid: str = '0'
    drive_index: int = 0


class CategoryRulesPayload(BaseModel):
    movie: List[dict] = []
    tv: List[dict] = []


# ==========================================
# 配置端点
# ==========================================

@router.get("/defaults")
async def get_default_config():
    return MediaOrganizeConfig().dict()


@router.get("/get")
async def get_config():
    """读取媒体整理配置"""
    if not os.path.exists(CONFIG_FILE):
        return MediaOrganizeConfig().dict()
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            data["drive_index"] = 0
            return data
        return MediaOrganizeConfig().dict()
    except Exception as e:
        logger.error(f"[MediaOrganize] 读取配置失败: {e}")
        return MediaOrganizeConfig().dict()


@router.post("/save")
async def save_config(config: MediaOrganizeConfig):
    """保存媒体整理配置"""
    try:
        old_enabled = False
        old_data = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    loaded_data = json.load(f)
                    if isinstance(loaded_data, dict):
                        old_data = loaded_data
                        old_enabled = loaded_data.get("life_monitor_enabled", False)
            except Exception:
                pass

        merged_data = dict(old_data) if isinstance(old_data, dict) else {}
        merged_data.update(config.dict())

        from app.routers.config_302 import get_config_302_sync
        cfg302 = get_config_302_sync()
        topology = cfg302.get("standard_topology") if isinstance(cfg302, dict) else None
        if topology and isinstance(topology, dict):
            merged_data["source_name"] = topology.get("transfer_dir", merged_data.get("source_name", config.source_name))
            merged_data["target_name"] = topology.get("media_dir", merged_data.get("target_name", config.target_name))
            merged_data["failed_name"] = topology.get("failed_dir", merged_data.get("failed_name", config.failed_name))
            merged_data["source_cid"] = str(topology.get("transfer_dir_cid", merged_data.get("source_cid", config.source_cid)) or "0")
            merged_data["target_cid"] = str(topology.get("media_dir_cid", merged_data.get("target_cid", config.target_cid)) or "0")
            merged_data["failed_cid"] = str(topology.get("failed_dir_cid", merged_data.get("failed_cid", config.failed_cid)) or "0")

        merged_data["drive_index"] = 0
        normalized_config = MediaOrganizeConfig(**merged_data)
        merged_data.update(normalized_config.dict())

        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(merged_data, f, ensure_ascii=False, indent=4)
        logger.info(f"[MediaOrganize] 配置已保存")

        new_enabled = normalized_config.life_monitor_enabled
        if new_enabled != old_enabled:
            await _toggle_life_monitor(new_enabled, normalized_config)

        return {"status": "success", "message": "配置已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")


async def _toggle_life_monitor(enabled: bool, config: MediaOrganizeConfig, force_restart: bool = False):
    """热更新 115 生活事件监控"""
    try:
        from core.monitor115.monitor import life_event_monitor, create_monitor
        from app.services.drive115_service import drive115_service

        if enabled:
            if life_event_monitor and life_event_monitor.is_running:
                if not force_restart:
                    logger.info("[MediaOrganize] 监控已在运行")
                    return
                life_event_monitor.stop()
                logger.info("[MediaOrganize] 已停止旧的 Life 监控，准备按最新配置重启")

            source_dir = config.source_name if config.source_name != "根目录" else ""
            target_dir = config.target_name if config.target_name != "根目录" else ""
            if not source_dir or not target_dir:
                logger.warning("[MediaOrganize] 源目录或目标目录未配置，无法启动监控")
                return

            client, _ = await drive115_service.get_client(0)
            if not client:
                logger.warning("[MediaOrganize] 115 客户端未就绪，无法启动监控")
                return

            callback = create_life_event_callback(
                source_dir,
                config.drive_index,
                target_dir,
                str(config.source_cid),
                str(config.target_cid),
            )
            monitor = create_monitor(
                client=client,
                source_dir=source_dir,
                target_dir=target_dir,
                callback=callback,
                start_mode="latest",
            )
            if monitor.start():
                logger.info("[MediaOrganize] 115 Life 事件监控已启动")
            else:
                logger.warning("[MediaOrganize] 115 Life 事件监控启动失败")
        else:
            if life_event_monitor and life_event_monitor.is_running:
                life_event_monitor.stop()
                logger.info("[MediaOrganize] 115 Life 事件监控已停止")
    except Exception as e:
        logger.error(f"[MediaOrganize] 切换 Life 监控失败: {e}")


# ==========================================
# 二级分类规则 API
# ==========================================

@router.get("/category_rules/get")
async def get_category_rules():
    from app.services.category_matcher import load_rules, save_rules, DEFAULT_RULES
    if not os.path.exists("config/media_organize_category_rules.json"):
        save_rules(DEFAULT_RULES)
        return DEFAULT_RULES
    return load_rules()


@router.get("/category_rules/defaults")
async def get_default_category_rules():
    from app.services.category_matcher import DEFAULT_RULES
    return {"movie": DEFAULT_RULES.get("movie", []), "tv": DEFAULT_RULES.get("tv", [])}


@router.post("/category_rules/save")
async def save_category_rules(payload: CategoryRulesPayload):
    from app.services.category_matcher import save_rules, load_rules
    from app.services.emby_library_cache import diff_rule_paths, sync_desired_state

    try:
        existing = load_rules()
        rules = {
            "sub_classify": existing.get("sub_classify", {}),
            "movie": payload.movie,
            "tv": payload.tv,
        }
        diff = diff_rule_paths(existing, rules)
        save_rules(rules)
        sync_info = sync_desired_state(rules)

        warnings = []
        if diff["removed_paths"]:
            warnings.append("这些旧分类路径已删除，但对应 Emby 媒体库不会自动删除，请自行到 Emby 手动清理")

        return {
            "status": "success",
            "message": "规则已保存",
            "diff": {
                "added_paths": diff["added_paths"],
                "removed_paths": diff["removed_paths"],
                "unchanged_paths": diff["unchanged_paths"],
            },
            "removed_paths": diff["removed_paths"],
            "added_paths": diff["added_paths"],
            "warnings": warnings,
            "desired_count": sync_info.get("desired_count", 0),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/category_rules/sub_classify/save")
async def save_sub_classify(payload: dict):
    """单独保存子分类设置（含 Emby 同步配置）"""
    from app.services.category_matcher import save_rules, load_rules
    from app.services.emby_library_cache import apply_settings, sync_desired_state
    try:
        existing = load_rules()
        existing["sub_classify"] = payload
        save_rules(existing)
        sync_info = sync_desired_state(existing)
        apply_settings(payload)
        return {
            "status": "success",
            "message": "子分类设置已保存",
            "desired_count": sync_info.get("desired_count", 0),
            "settings_snapshot": sync_info.get("settings_snapshot", {}),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/emby_lib_cache/refresh")
async def refresh_emby_lib_cache():
    """手动刷新 Emby 媒体库缓存"""
    from app.services.emby_library_cache import refresh_cache
    count = refresh_cache()
    return {"status": "success", "count": count, "message": "Emby 媒体库快照已刷新"}


# ==========================================
# 浏览与刮削端点
# ==========================================

@router.post("/browse115")
async def browse_115(payload: Browse115Payload):
    """浏览 115 网盘目录"""
    try:
        client = _get_115_client(0)
        cid = payload.cid or "0"

        resp = client.fs_files_app(
            {"cid": int(cid), "limit": 1150, "fc_mix": 0},
            app="android",
            base_url="https://proapi.115.com",
            headers={"user-agent": "Mozilla/5.0 (Linux; Android 13; 23013RK75C Build/TKQ1.221114.001) AppleWebKit/537.36 Chrome/123.0.0.0 Mobile Safari/537.36"},
        )
        if not resp or not resp.get("state"):
            return {"status": "error", "message": "读取目录失败", "dirs": []}

        dirs = []
        for item in resp.get("data", []):
            if item.get("fc") == "0":
                dirs.append({
                    "name": item.get("fn", ""),
                    "cid": str(item.get("fid", "")),
                })

        return {"status": "ok", "dirs": dirs}
    except Exception as e:
        return {"status": "error", "message": f"浏览失败: {str(e)}", "dirs": []}


@router.post("/list_files")
async def list_files(payload: Browse115Payload):
    """列出 115 网盘目录下的视频文件"""
    try:
        client = _get_115_client(0)
        cid = payload.cid or "0"

        resp = client.fs_files_app(
            {"cid": int(cid), "limit": 1150, "fc_mix": 0},
            app="android",
            base_url="https://proapi.115.com",
            headers={"user-agent": "Mozilla/5.0 (Linux; Android 13; 23013RK75C Build/TKQ1.221114.001) AppleWebKit/537.36 Chrome/123.0.0.0 Mobile Safari/537.36"},
        )
        if not resp or not resp.get("state"):
            return {"status": "error", "message": "读取目录失败", "files": []}

        files = []
        for item in resp.get("data", []):
            if item.get("fc") == "1":
                name = item.get("fn", "")
                ext = os.path.splitext(name)[1].lower()
                if ext in VIDEO_EXTS:
                    files.append({
                        "name": name,
                        "cid": str(item.get("fid", "")),
                        "size": item.get("fs", 0),
                    })

        return {"status": "ok", "files": files}
    except Exception as e:
        return {"status": "error", "message": f"读取失败: {str(e)}", "files": []}


@router.post("/scrape")
async def scrape_directory(req: ScrapeRequest):
    """对网盘目录执行刮削"""
    try:
        config_data = await _load_config_data()
        drive_index = 0

        files_result = await list_files(Browse115Payload(cid=req.cid, drive_index=drive_index))
        if files_result["status"] != "ok":
            return {"status": "error", "message": files_result.get("message", "读取网盘目录失败")}

        tmdb_data = await _fetch_tmdb_data(req.tmdb_id, req.media_type, req.season_number, None)
        if not tmdb_data:
            return {"status": "error", "message": f"无法获取 TMDb 数据 (ID: {req.tmdb_id})"}

        scraping_config = _build_scraping_config(config_data)
        generated_files: List[str] = []

        from core.organizer import MediaOrganizer, MediaType

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            organizer = MediaOrganizer(
                library_root=str(tmpdir),
                transfer_media=_noop_transfer,
                save_nfo=_write_nfo,
                download_image=_download_image,
                scraping_config=scraping_config,
            )

            media_type = MediaType(req.media_type)
            result = organizer.scrape_directory(
                dir_path=tmpdir,
                tmdb_data=tmdb_data,
                media_type=media_type,
                season_number=req.season_number,
                episode_number=req.episode_number,
                init_folder=True,
                recursive=True,
                overwrite=req.overwrite,
            )

            for root, dirs, files in os.walk(tmpdir):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, tmpdir)
                    generated_files.append(rel)

        return {
            "status": "success" if result.success else "error",
            "message": f"刮削完成，生成 {len(generated_files)} 个元数据文件",
            "generated_files": generated_files,
            "file_count": len(files_result.get("files", [])),
        }
    except Exception as e:
        logger.error(f"[MediaOrganize] 刮削失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


# ==========================================
# 整理端点
# ==========================================

def _start_organize_thread(run_id: str, req: OrganizeRequest):
    """在后台线程中执行整理任务"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with _organize_trigger_lock:
        _state._organize_running = True
    try:
        loop.run_until_complete(_run_organize_async(run_id, req))
    except Exception as e:
        logger.error(f"[MediaOrganize] 整理任务异常: {e}", exc_info=True)
        update_task_progress(run_id, f"整理失败: {e}", 0, "error")
    finally:
        with _organize_trigger_lock:
            _state._organize_running = False
        loop.close()


@router.post("/organize")
async def organize_media(req: OrganizeRequest):
    """启动后台整理任务，立即返回 run_id"""
    config_data = await _load_config_data()
    source_cid = config_data.get("source_cid", "0")
    target_cid = config_data.get("target_cid", "0")
    if target_cid in ("0", 0):
        return {"status": "error", "message": "请先配置目标目录"}
    if source_cid in ("0", 0):
        return {"status": "error", "message": "请先配置源目录"}

    drive_index = 0

    run_id = f"organize_{uuid.uuid4().hex[:8]}"
    update_task_progress(run_id, "整理: 准备中...", 0)
    t = threading.Thread(target=_start_organize_thread, args=(run_id, req), daemon=True)
    t.start()
    logger.info(f"[MediaOrganize] 后台整理任务已启动: run_id={run_id}")
    return {"status": "ok", "run_id": run_id}
