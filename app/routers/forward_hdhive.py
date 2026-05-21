import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field

from app.services.forward_hdhive_service import forward_hdhive_service
from app.services.telegram_service import telegram_notify_service
from core.logger import logger


router = APIRouter(prefix="/api/forward", tags=["ForwardHDHive"])


class ForwardConfigRequest(BaseModel):
    enabled: bool = True
    account_id: str = ""
    public_base_url: str = ""
    hdhive_enabled: bool = True
    max_unlock_points: int = 4
    library_enabled: bool = True
    transfer_mode: str = "series"
    aiying_enabled: bool = False
    aiying_tg_id: str = ""
    aiying_chill_token: str = ""


class ResourceTestRequest(BaseModel):
    type: str = "movie"
    tmdb_id: str = ""


class ResourceSearchRequest(BaseModel):
    type: str = "movie"
    tmdb_id: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None
    sources: list[str] = Field(default_factory=list)


class ResourceTransferRequest(BaseModel):
    source: str = "hdhive"
    slug: str = ""
    resource_id: str = ""
    type: str = "movie"
    tmdb_id: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None


@router.get("/config")
async def get_config(request: Request):
    return forward_hdhive_service.get_config(request, telegram_user_id=await _telegram_user_id())


@router.get("/search_sources")
async def get_search_sources():
    return {"sources": forward_hdhive_service.get_search_source_options()}


@router.post("/config")
async def save_config(req: ForwardConfigRequest, request: Request):
    forward_hdhive_service.update_config(req.model_dump())
    return forward_hdhive_service.get_config(request, telegram_user_id=await _telegram_user_id())


@router.post("/token/refresh")
async def refresh_widget_token(request: Request):
    forward_hdhive_service.refresh_widget_token()
    return forward_hdhive_service.get_config(request, telegram_user_id=await _telegram_user_id())


async def _telegram_user_id() -> str:
    cached = getattr(telegram_notify_service, "_account_user_cache", None)
    if isinstance(cached, dict) and cached.get("id"):
        return str(cached.get("id") or "")
    return ""


@router.post("/test_resources")
async def test_resources(req: ResourceTestRequest):
    tmdb_id = str(req.tmdb_id or "").strip()
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="TMDB ID 不能为空")
    errors: dict[str, str] = {}
    resources: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    if forward_hdhive_service.config.get("hdhive_enabled", True):
        try:
            resources = forward_hdhive_service.fetch_resources(req.type, tmdb_id, use_cache=False)
            filtered = forward_hdhive_service.filter_resources(resources)
        except HTTPException as e:
            errors["hdhive"] = str(e.detail)
    aiying_resources: list[dict[str, Any]] = []
    aiying_filtered: list[dict[str, Any]] = []
    try:
        aiying_resources = await forward_hdhive_service.fetch_aiying_resources(req.type, tmdb_id, use_cache=False)
        aiying_filtered = forward_hdhive_service.filter_aiying_resources(aiying_resources, media_type=req.type)
    except HTTPException as e:
        errors["aiying"] = str(e.detail)
    return {
        "total": len(resources),
        "filtered": len(filtered),
        "items": filtered,
        "aiying_total": len(aiying_resources),
        "aiying_filtered": len(aiying_filtered),
        "aiying_items": aiying_filtered,
        "errors": errors,
        "aiying_stats": {
            "success_count": int(forward_hdhive_service.config.get("aiying_success_count") or 0),
            "today_used": int(forward_hdhive_service.config.get("aiying_today_used") or 0),
            "last_times": forward_hdhive_service.config.get("aiying_last_times"),
            "last_message": str(forward_hdhive_service.config.get("aiying_last_message") or ""),
            "last_result_count": int(forward_hdhive_service.config.get("aiying_last_result_count") or 0),
            "last_checked_at": str(forward_hdhive_service.config.get("aiying_last_checked_at") or ""),
        },
    }


@router.post("/resources")
async def load_forward_resources(
    request: Request,
    token: Optional[str] = Query(None),
):
    forward_hdhive_service.verify_token(token or request.headers.get("x-forward-token"))
    if not forward_hdhive_service.config.get("enabled", True):
        return []

    params: dict[str, Any] = await request.json()
    return await _load_forward_resources_from_params(request, params)


@router.post("/search_resources")
async def search_forward_resources(req: ResourceSearchRequest, request: Request):
    tmdb_id = str(req.tmdb_id or "").strip()
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="TMDB ID 不能为空")
    params: dict[str, Any] = {
        "tmdbId": tmdb_id,
        "type": req.type,
        "sources": req.sources or [],
    }
    if req.season is not None:
        params["season"] = req.season
    if req.episode is not None:
        params["episode"] = req.episode
    params["ignoreEnabled"] = True
    return await _load_forward_resources_from_params(request, params, respect_enabled=False)


@router.post("/transfer_resource")
async def transfer_forward_resource(req: ResourceTransferRequest, request: Request):
    tmdb_id = str(req.tmdb_id or "").strip()
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="TMDB ID 不能为空")
    source = str(req.source or "hdhive").strip().lower()
    if source == "aiying":
        resource_id = str(req.resource_id or "").strip()
        if not resource_id:
            raise HTTPException(status_code=400, detail="缺少爱影资源 ID")
        return await forward_hdhive_service.transfer_aiying_resource_to_organize(
            request,
            resource_id=resource_id,
            media_type=req.type,
            tmdb_id=tmdb_id,
            season=req.season,
            episode=req.episode,
            require_enabled=False,
        )
    slug = str(req.slug or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="缺少影巢资源 slug")
    return await forward_hdhive_service.transfer_resource_to_organize(
        request,
        slug=slug,
        media_type=req.type,
        tmdb_id=tmdb_id,
        season=req.season,
        episode=req.episode,
        require_enabled=False,
    )


async def _load_forward_resources_from_params(request: Request, params: dict[str, Any], *, respect_enabled: bool = True):
    tmdb_id = str(params.get("tmdbId") or params.get("tmdb_id") or "").strip()
    if not tmdb_id:
        return []
    media_type = "tv" if str(params.get("type") or "").lower() in {"tv", "series"} else "movie"
    raw_sources = params.get("sources") or params.get("source") or []
    if isinstance(raw_sources, str):
        source_set = {part.strip().lower() for part in raw_sources.split(",") if part.strip()}
    elif isinstance(raw_sources, list):
        source_set = {str(part or "").strip().lower() for part in raw_sources if str(part or "").strip()}
    else:
        source_set = set()
    if not source_set or "all" in source_set:
        source_set = {"hdhive", "aiying"}
    result: list[dict[str, Any]] = []
    if "hdhive" in source_set and (not respect_enabled or forward_hdhive_service.config.get("hdhive_enabled", True)):
        try:
            resources = forward_hdhive_service.fetch_resources(media_type, tmdb_id, require_enabled=respect_enabled)
            result.extend(forward_hdhive_service.build_forward_resources(request, params, resources))
        except HTTPException as e:
            logger.warning(f"[Forward] 影巢查询失败: {getattr(e, 'detail', str(e))}")
    if "aiying" in source_set:
        try:
            aiying_resources = await forward_hdhive_service.fetch_aiying_resources(media_type, tmdb_id, require_enabled=respect_enabled)
            result.extend(forward_hdhive_service.build_aiying_forward_resources(request, params, aiying_resources))
        except HTTPException as e:
            logger.warning(f"[Forward] 爱影查询失败: {getattr(e, 'detail', str(e))}")
    return result


@router.get("/play")
async def play_forward_resource(
    request: Request,
    token: str = Query(""),
    slug: Optional[str] = Query(None),
    source: str = Query("hdhive"),
    resource_id: Optional[str] = Query(None),
    type: str = Query("movie"),
    tmdb_id: str = Query(""),
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
    ignore_enabled: bool = Query(False),
):
    forward_hdhive_service.verify_token(token)
    if str(source or "").lower() == "aiying":
        if not resource_id:
            raise HTTPException(status_code=400, detail="缺少爱影资源 ID")
        direct_url = await forward_hdhive_service.play_aiying_resource(
            request,
            resource_id=resource_id,
            media_type=type,
            tmdb_id=tmdb_id,
            season=season,
            episode=episode,
            require_enabled=not ignore_enabled,
        )
    else:
        if not slug:
            raise HTTPException(status_code=400, detail="缺少影巢资源 slug")
        direct_url = await forward_hdhive_service.play_resource(
            request,
            slug=slug,
            media_type=type,
            tmdb_id=tmdb_id,
            season=season,
            episode=episode,
            require_enabled=not ignore_enabled,
        )
    return RedirectResponse(direct_url, status_code=302)


@router.get("/widget.js")
async def widget_js(request: Request, token: str = Query("")):
    forward_hdhive_service.verify_token(token)
    base_url = forward_hdhive_service.get_public_base_url(request)
    payload = {
        "baseUrl": base_url,
        "token": token,
    }
    js = f"""
var ChillPosterForward = {json.dumps(payload, ensure_ascii=False)};
var WidgetMetadata = {{
    id: "chillposter.forward.hdhive",
    title: "ChillPoster",
    icon: "https://hdhive.com/favicon.ico",
    version: "1.0.0",
    requiredVersion: "0.0.1",
    description: "通过 ChillPoster 查询影巢/爱影资源，并使用 115 Cookie 获取直链播放",
    author: "ChillPoster",
    site: "https://github.com/Chill-lucky/ChillPoster",
    modules: [
      {{
        id: "loadResource",
        title: "ChillPoster 资源",
        functionName: "loadResource",
        type: "stream",
        params: []
      }}
    ]
  }};

async function chillposterPost(path, body) {{
    const url = `${{ChillPosterForward.baseUrl}}${{path}}?token=${{encodeURIComponent(ChillPosterForward.token)}}`;
    if (typeof Widget === "undefined" || !Widget.http || !Widget.http.post) {{
      throw new Error("Forward Widget.http.post 不可用");
    }}
    const response = await Widget.http.post(url, body || {{}}, {{
      headers: {{
        "Content-Type": "application/json",
        "User-Agent": "ForwardWidgets/1.0.0",
        "X-Forward-Token": ChillPosterForward.token
      }}
    }});
    if (!response) {{
      throw new Error("ChillPoster 请求无响应");
    }}
    return response.data;
}}

async function loadResource(params) {{
    return await chillposterPost("/api/forward/resources", params || {{}});
}}

if (typeof globalThis !== "undefined") {{
  globalThis.WidgetMetadata = WidgetMetadata;
  globalThis.loadResource = loadResource;
}}
"""
    return Response(
        js,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
