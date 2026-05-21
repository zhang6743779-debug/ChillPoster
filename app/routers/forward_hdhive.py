import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from app.services.forward_hdhive_service import forward_hdhive_service


router = APIRouter(prefix="/api/forward", tags=["ForwardHDHive"])


class ForwardConfigRequest(BaseModel):
    enabled: bool = True
    account_id: str = ""
    public_base_url: str = ""
    max_unlock_points: int = 4


class ResourceTestRequest(BaseModel):
    type: str = "movie"
    tmdb_id: str = ""


@router.get("/config")
async def get_config(request: Request):
    return forward_hdhive_service.get_config(request)


@router.post("/config")
async def save_config(req: ForwardConfigRequest, request: Request):
    forward_hdhive_service.update_config(req.model_dump())
    return forward_hdhive_service.get_config(request)


@router.post("/test_resources")
async def test_resources(req: ResourceTestRequest):
    tmdb_id = str(req.tmdb_id or "").strip()
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="TMDB ID 不能为空")
    resources = forward_hdhive_service.fetch_resources(req.type, tmdb_id, use_cache=False)
    filtered = forward_hdhive_service.filter_resources(resources)
    return {
        "total": len(resources),
        "filtered": len(filtered),
        "items": filtered,
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
    tmdb_id = str(params.get("tmdbId") or params.get("tmdb_id") or "").strip()
    if not tmdb_id:
        return []
    media_type = "tv" if str(params.get("type") or "").lower() in {"tv", "series"} else "movie"
    resources = forward_hdhive_service.fetch_resources(media_type, tmdb_id)
    return forward_hdhive_service.build_forward_resources(request, params, resources)


@router.get("/play")
async def play_forward_resource(
    request: Request,
    token: str = Query(""),
    slug: str = Query(...),
    type: str = Query("movie"),
    tmdb_id: str = Query(""),
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
):
    forward_hdhive_service.verify_token(token)
    direct_url = await forward_hdhive_service.play_resource(
        request,
        slug=slug,
        media_type=type,
        tmdb_id=tmdb_id,
        season=season,
        episode=episode,
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
(function () {{
  const ChillPosterForward = {json.dumps(payload, ensure_ascii=False)};

  WidgetMetadata = {{
    id: "chillposter.forward.hdhive",
    title: "ChillPoster 影巢",
    icon: "https://hdhive.com/favicon.ico",
    version: "1.0.0",
    requiredVersion: "0.0.1",
    description: "通过 ChillPoster 查询影巢资源，并使用 115 Cookie 获取直链播放",
    author: "ChillPoster",
    site: "https://github.com/Chill-lucky/ChillPoster",
    modules: [
      {{
        id: "loadResource",
        title: "影巢资源",
        functionName: "loadResource",
        type: "stream",
        params: []
      }}
    ]
  }};

  async function chillposterPost(path, body) {{
    const url = `${{ChillPosterForward.baseUrl}}${{path}}?token=${{encodeURIComponent(ChillPosterForward.token)}}`;
    const res = await fetch(url, {{
      method: "POST",
      headers: {{
        "Content-Type": "application/json",
        "X-Forward-Token": ChillPosterForward.token
      }},
      body: JSON.stringify(body || {{}})
    }});
    if (!res.ok) {{
      const text = await res.text();
      throw new Error(text || `ChillPoster HTTP ${{res.status}}`);
    }}
    return await res.json();
  }}

  async function loadResource(params) {{
    return await chillposterPost("/api/forward/resources", params || {{}});
  }}
}})();
"""
    return Response(js, media_type="application/javascript; charset=utf-8")
