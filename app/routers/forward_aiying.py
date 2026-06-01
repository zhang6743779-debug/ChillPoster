import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from app.services.forward_aiying_service import forward_aiying_service
from app.services.moviepilot_resource_service import moviepilot_resource_service
from app.services.telegram_service import telegram_notify_service
from core.logger import logger


router = APIRouter(prefix="/api/forward", tags=["ForwardAiying"])


class ForwardConfigRequest(BaseModel):
    enabled: bool = True
    public_base_url: str = ""
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
    title: str = ""
    year: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None
    sources: list[str] = Field(default_factory=list)


class ResourceTransferRequest(BaseModel):
    source: str = "aiying"
    resource_id: str = ""
    type: str = "movie"
    tmdb_id: str = ""
    title: str = ""
    year: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None


class ResourcePreviewRequest(ResourceTransferRequest):
    pass


class ResourceDownloadRequest(ResourceTransferRequest):
    fill_mode: str = "full"
    existing_episodes_by_season: dict[str, list[int]] = Field(default_factory=dict)


@router.get("/config")
async def get_config(request: Request):
    return forward_aiying_service.get_config(request, telegram_user_id=await _telegram_user_id())


@router.get("/search_sources")
async def get_search_sources():
    return {"sources": forward_aiying_service.get_search_source_options()}


@router.post("/config")
async def save_config(req: ForwardConfigRequest, request: Request):
    forward_aiying_service.update_config(req.model_dump())
    return forward_aiying_service.get_config(request, telegram_user_id=await _telegram_user_id())


@router.post("/token/refresh")
async def refresh_widget_token(request: Request):
    forward_aiying_service.refresh_widget_token()
    return forward_aiying_service.get_config(request, telegram_user_id=await _telegram_user_id())


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
    aiying_resources: list[dict[str, Any]] = []
    aiying_filtered: list[dict[str, Any]] = []
    try:
        aiying_resources = await forward_aiying_service.fetch_aiying_resources(req.type, tmdb_id, use_cache=False)
        aiying_filtered = forward_aiying_service.filter_aiying_resources(aiying_resources, media_type=req.type)
    except HTTPException as e:
        errors["aiying"] = str(e.detail)
    return {
        "aiying_total": len(aiying_resources),
        "aiying_filtered": len(aiying_filtered),
        "aiying_items": aiying_filtered,
        "errors": errors,
        "aiying_stats": {
            "success_count": int(forward_aiying_service.config.get("aiying_success_count") or 0),
            "today_used": int(forward_aiying_service.config.get("aiying_today_used") or 0),
            "last_times": forward_aiying_service.config.get("aiying_last_times"),
            "last_message": str(forward_aiying_service.config.get("aiying_last_message") or ""),
            "last_result_count": int(forward_aiying_service.config.get("aiying_last_result_count") or 0),
            "last_checked_at": str(forward_aiying_service.config.get("aiying_last_checked_at") or ""),
        },
    }


@router.post("/resources")
async def load_forward_resources(
    request: Request,
    token: Optional[str] = Query(None),
):
    forward_aiying_service.verify_token(token or request.headers.get("x-forward-token"))
    if not forward_aiying_service.config.get("enabled", True):
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
        "title": req.title,
        "year": req.year,
        "sources": req.sources or [],
    }
    if req.season is not None:
        params["season"] = req.season
    if req.episode is not None:
        params["episode"] = req.episode
    params["ignoreEnabled"] = True
    return await _load_forward_resources_from_params(request, params, respect_enabled=False)


@router.post("/search_resources/stream")
async def stream_forward_resources(req: ResourceSearchRequest):
    tmdb_id = str(req.tmdb_id or "").strip()
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="TMDB ID 不能为空")
    media_type = "tv" if str(req.type or "").lower() in {"tv", "series"} else "movie"
    source_set = _parse_source_set(req.sources)

    async def event_source():
        if "moviepilot" not in source_set:
            yield _sse_event({
                "type": "done",
                "sourceKey": "moviepilot",
                "sourceName": "MoviePilot",
                "text": "未选择 MoviePilot 搜索源",
                "items": [],
                "total_items": 0,
            })
            return
        try:
            async for event in moviepilot_resource_service.stream_resources(
                media_type=media_type,
                tmdb_id=tmdb_id,
                title=req.title,
                year=req.year,
                season=req.season,
                episode=req.episode,
            ):
                yield _sse_event(event)
        except HTTPException as e:
            yield _sse_event({
                "type": "error",
                "sourceKey": "moviepilot",
                "sourceName": "MoviePilot",
                "message": str(e.detail),
                "text": str(e.detail),
                "items": [],
            })
        except Exception as e:
            logger.warning(f"[Forward] MoviePilot 流式查询失败: {e}")
            yield _sse_event({
                "type": "error",
                "sourceKey": "moviepilot",
                "sourceName": "MoviePilot",
                "message": f"MoviePilot 查询失败: {e}",
                "text": f"MoviePilot 查询失败: {e}",
                "items": [],
            })

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.post("/transfer_resource")
async def transfer_forward_resource(req: ResourceTransferRequest, request: Request):
    tmdb_id = str(req.tmdb_id or "").strip()
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="TMDB ID 不能为空")
    resource_id = str(req.resource_id or "").strip()
    if not resource_id:
        raise HTTPException(status_code=400, detail="缺少爱影资源 ID")
    return await forward_aiying_service.transfer_aiying_resource_to_organize(
        request,
        resource_id=resource_id,
        media_type=req.type,
        tmdb_id=tmdb_id,
        season=req.season,
        episode=req.episode,
        require_enabled=False,
    )


@router.post("/preview_resource")
async def preview_forward_resource(req: ResourcePreviewRequest):
    tmdb_id = str(req.tmdb_id or "").strip()
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="TMDB ID 不能为空")
    resource_id = str(req.resource_id or "").strip()
    if not resource_id:
        raise HTTPException(status_code=400, detail="缺少资源 ID")
    source = str(req.source or "aiying").strip().lower()
    if source == "moviepilot":
        return await moviepilot_resource_service.preview_torrent_resource(
            resource_id=resource_id,
            media_type=req.type,
            tmdb_id=tmdb_id,
            season=req.season,
            episode=req.episode,
        )
    return await forward_aiying_service.preview_aiying_resource(
        resource_id=resource_id,
        media_type=req.type,
        tmdb_id=tmdb_id,
        season=req.season,
        episode=req.episode,
        require_enabled=False,
    )


@router.post("/download_resource")
async def download_forward_resource(req: ResourceDownloadRequest):
    tmdb_id = str(req.tmdb_id or "").strip()
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="TMDB ID 不能为空")
    resource_id = str(req.resource_id or "").strip()
    if not resource_id:
        raise HTTPException(status_code=400, detail="缺少资源 ID")
    source = str(req.source or "").strip().lower()
    if source != "moviepilot":
        raise HTTPException(status_code=400, detail="当前仅支持 MoviePilot 资源添加到下载器")
    return await moviepilot_resource_service.add_torrent_to_downloader(
        resource_id=resource_id,
        media_type=req.type,
        tmdb_id=tmdb_id,
        title=req.title,
        year=req.year,
        season=req.season,
        episode=req.episode,
        fill_mode=req.fill_mode,
        existing_episodes_by_season=req.existing_episodes_by_season,
    )


def _sse_event(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _parse_source_set(raw_sources: Any) -> set[str]:
    if isinstance(raw_sources, str):
        source_set = {part.strip().lower() for part in raw_sources.split(",") if part.strip()}
    elif isinstance(raw_sources, list):
        source_set = {str(part or "").strip().lower() for part in raw_sources if str(part or "").strip()}
    else:
        source_set = set()
    if not source_set or "all" in source_set:
        source_set = {"aiying"}
    return source_set


async def _load_forward_resources_from_params(request: Request, params: dict[str, Any], *, respect_enabled: bool = True):
    tmdb_id = str(params.get("tmdbId") or params.get("tmdb_id") or "").strip()
    if not tmdb_id:
        return []
    media_type = "tv" if str(params.get("type") or "").lower() in {"tv", "series"} else "movie"
    raw_sources = params.get("sources") or params.get("source") or []
    source_set = _parse_source_set(raw_sources)
    result: list[dict[str, Any]] = []
    if "aiying" in source_set:
        try:
            aiying_resources = await forward_aiying_service.fetch_aiying_resources(media_type, tmdb_id, require_enabled=respect_enabled)
            result.extend(forward_aiying_service.build_aiying_forward_resources(request, params, aiying_resources))
        except HTTPException as e:
            logger.warning(f"[Forward] 爱影查询失败: {getattr(e, 'detail', str(e))}")
    if "moviepilot" in source_set:
        try:
            result.extend(await moviepilot_resource_service.search_resources(
                media_type=media_type,
                tmdb_id=tmdb_id,
                title=params.get("title"),
                year=params.get("year"),
                season=forward_aiying_service._to_optional_int(params.get("season")),
                episode=forward_aiying_service._to_optional_int(params.get("episode")),
            ))
        except HTTPException as e:
            logger.warning(f"[Forward] MoviePilot 查询失败: {getattr(e, 'detail', str(e))}")
    return result


@router.get("/play")
async def play_forward_resource(
    request: Request,
    token: str = Query(""),
    source: str = Query("aiying"),
    resource_id: Optional[str] = Query(None),
    type: str = Query("movie"),
    tmdb_id: str = Query(""),
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
    ignore_enabled: bool = Query(False),
):
    forward_aiying_service.verify_token(token)
    if not resource_id:
        raise HTTPException(status_code=400, detail="缺少爱影资源 ID")
    direct_url = await forward_aiying_service.play_aiying_resource(
        request,
        resource_id=resource_id,
        media_type=type,
        tmdb_id=tmdb_id,
        season=season,
        episode=episode,
        require_enabled=not ignore_enabled,
    )
    return RedirectResponse(direct_url, status_code=302)


@router.get("/widget.js")
async def widget_js(request: Request, token: str = Query("")):
    forward_aiying_service.verify_token(token)
    base_url = forward_aiying_service.get_public_base_url(request)
    payload = {
        "baseUrl": base_url,
        "token": token,
    }
    js = f"""
var ChillPosterForward = {json.dumps(payload, ensure_ascii=False)};
var WidgetMetadata = {{
    id: "chillposter.forward.aiying",
    title: "ChillPoster",
    icon: "https://raw.githubusercontent.com/Chill-lucky/ChillPoster/main/static/favicon.ico",
    version: "1.0.0",
    requiredVersion: "0.0.1",
    description: "通过 ChillPoster 查询爱影资源，并使用 115 Cookie 获取直链播放",
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
