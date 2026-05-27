"""
影巢 (HDHive) 路由模块
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
import asyncio
import json

from app.services.hdhive_service import hdhive_service
from app.services.wechat_service import wechat_notify_service
from app.services.telegram_service import telegram_notify_service
from core.logger import logger

router = APIRouter(prefix="/api/hdhive", tags=["HDHive"])


# ==========================================
# 请求模型
# ==========================================

class AddAccountRequest(BaseModel):
    name: str
    password: str = ""
    token: str = ""
    api_key: str = ""
    openapi_client_id: str = ""
    openapi_app_secret: str = ""
    openapi_redirect_uri: str = ""


class UpdateAccountRequest(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    api_key: Optional[str] = None
    openapi_client_id: Optional[str] = None
    openapi_app_secret: Optional[str] = None
    openapi_redirect_uri: Optional[str] = None
    openapi_access_token: Optional[str] = None
    openapi_refresh_token: Optional[str] = None
    enabled: Optional[bool] = None
    checkin_type: Optional[str] = None  # none, normal, gambler
    checkin_cron: Optional[str] = None


class LoginRequest(BaseModel):
    account_id: str


class OpenAPIAuthorizeRequest(BaseModel):
    account_id: str
    scope: str = "meta query unlock write"


class CheckinRequest(BaseModel):
    account_id: Optional[str] = None  # 为空则签到所有


class GamblerCheckinRequest(BaseModel):
    account_id: str
    is_gambler: bool = True  # True=赌狗模式，False=普通模式（使用API）


# ==========================================
# API 路由
# ==========================================

@router.get("/config")
async def get_config():
    """获取影巢配置"""
    return hdhive_service.get_config()


@router.get("/events")
async def hdhive_events():
    """SSE 端点，签到成功后推送到前端刷新"""
    async def event_generator():
        queue = hdhive_service._get_event_queue()
        while True:
            try:
                event_type = await asyncio.wait_for(queue.get(), timeout=60)
                yield f"data: {json.dumps({'type': event_type})}\n\n"
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/account/add")
async def add_account(req: AddAccountRequest):
    """添加账号"""
    try:
        account = hdhive_service.add_account(
            name=req.name,
            password=req.password,
            token=req.token,
            api_key=req.api_key,
            openapi_client_id=req.openapi_client_id,
            openapi_app_secret=req.openapi_app_secret,
            openapi_redirect_uri=req.openapi_redirect_uri,
        )
        logger.info(f"[HDHive] 添加账号: {req.name}")
        return {"status": "ok", "account": account.model_dump()}
    except Exception as e:
        logger.error(f"[HDHive] 添加账号失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/account/update")
async def update_account(account_id: str, req: UpdateAccountRequest):
    """更新账号"""
    try:
        account = hdhive_service.update_account(
            account_id,
            name=req.name,
            password=req.password,
            token=req.token,
            api_key=req.api_key,
            openapi_client_id=req.openapi_client_id,
            openapi_app_secret=req.openapi_app_secret,
            openapi_redirect_uri=req.openapi_redirect_uri,
            openapi_access_token=req.openapi_access_token,
            openapi_refresh_token=req.openapi_refresh_token,
            enabled=req.enabled,
            checkin_type=req.checkin_type,
            checkin_cron=req.checkin_cron
        )
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        # 刷新定时任务
        if hdhive_service.scheduler:
            hdhive_service._refresh_jobs()
        return {"status": "ok", "account": account.model_dump()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[HDHive] 更新账号失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/account/remove")
async def remove_account(account_id: str):
    """删除账号"""
    try:
        if hdhive_service.remove_account(account_id):
            logger.info(f"[HDHive] 删除账号: {account_id}")
            return {"status": "ok"}
        else:
            raise HTTPException(status_code=404, detail="账号不存在")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[HDHive] 删除账号失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/account/test")
async def test_account(req: LoginRequest):
    """测试账号连接"""
    try:
        result = await hdhive_service.test_account(req.account_id)
        return result
    except Exception as e:
        logger.error(f"[HDHive] 测试账号失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/login")
async def login(req: LoginRequest):
    """登录获取 Token"""
    try:
        account = next((a for a in hdhive_service.config.accounts if a.id == req.account_id), None)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        result = await hdhive_service.login(account.name, account.password)
        if result.get("success"):
            # 更新 token
            hdhive_service.update_account(req.account_id, token=result["token"], status="ok")
            return {"status": "ok", "token": result["token"]}
        else:
            return {"status": "error", "message": result.get("error", "登录失败"), "hint": result.get("hint")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[HDHive] 登录失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/openapi/authorize-url")
async def openapi_authorize_url(req: OpenAPIAuthorizeRequest, request: Request):
    """生成 HDHive OpenAPI 用户授权链接"""
    try:
        return hdhive_service.build_openapi_authorize_url(req.account_id, request, req.scope or "meta query unlock write")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[HDHive] 生成 OpenAPI 授权链接失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/openapi/refresh")
async def openapi_refresh(req: LoginRequest):
    """手动刷新 OpenAPI 用户 Access Token"""
    try:
        account = next((a for a in hdhive_service.config.accounts if a.id == req.account_id), None)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        if not hdhive_service.refresh_openapi_token(account):
            return {"status": "error", "message": "缺少 Refresh Token 或应用 Secret"}
        return {"status": "ok", "account": account.model_dump()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[HDHive] 刷新 OpenAPI Token 失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/openapi/callback")
async def openapi_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    """HDHive OAuth 回调入口"""
    if error:
        return HTMLResponse(f"<h3>HDHive OpenAPI 授权失败</h3><p>{error}</p>", status_code=400)
    if not code or not state:
        return HTMLResponse("<h3>HDHive OpenAPI 授权失败</h3><p>缺少 code 或 state。</p>", status_code=400)
    try:
        account = hdhive_service.exchange_openapi_code(code, state)
        account.status = "ok"
        hdhive_service._save_config()
        return HTMLResponse(
            "<h3>HDHive OpenAPI 授权成功</h3>"
            "<p>可以关闭此页面并返回 ChillPoster 影巢配置页。</p>"
            "<script>setTimeout(function(){ window.close(); }, 1200);</script>"
        )
    except Exception as e:
        logger.error(f"[HDHive] OpenAPI 授权回调失败: {e}")
        return HTMLResponse(f"<h3>HDHive OpenAPI 授权失败</h3><p>{e}</p>", status_code=400)


@router.post("/checkin")
async def checkin(req: CheckinRequest):
    """签到"""
    try:
        if req.account_id:
            # 单个账号签到
            result = await hdhive_service.do_checkin(req.account_id)

            # 发送签到通知
            try:
                account = next((a for a in hdhive_service.config.accounts if a.id == req.account_id), None)
                if account and result.get("success"):
                    account_name = account.name or (account.user_info.nickname if account.user_info else None) or req.account_id
                    total_points = account.user_info.points if account.user_info else 0

                    # 解析获得的积分（积分已由 service 内部累积）
                    points = result.get("points", 0)
                    message = result.get("message", "")
                    if "已签到" in message or result.get("already_checked_in"):
                        status = "already"
                    else:
                        status = "success"

                    wechat_result = wechat_notify_service.notify_checkin(
                        account_name=account_name,
                        points=points,
                        total_points=total_points,
                        status=status,
                        message=message,
                        checkin_count=account.checkin_count,
                        checkin_points=account.checkin_points or 0
                    )
                    telegram_result = telegram_notify_service.notify_checkin(
                        account_name=account_name,
                        points=points,
                        total_points=total_points,
                        status=status,
                        message=message,
                        checkin_count=account.checkin_count,
                        checkin_points=account.checkin_points or 0
                    )
                    logger.info(f"[HDHive] 签到通知: 微信={wechat_result}, Telegram={telegram_result}")

                    # 推送签到成功事件到前端
                    if status == "success":
                        await hdhive_service.push_checkin_event("checkin_success")
                elif account:
                    logger.info(f"[HDHive] 签到结果非成功，跳过通知: {result}")
            except Exception as notify_err:
                logger.error(f"[HDHive] 发送签到通知异常: {notify_err}")

            return result
        else:
            # 所有账号签到
            results = await hdhive_service.checkin_all()
            return {"status": "ok", **results}
    except Exception as e:
        logger.error(f"[HDHive] 签到失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gambler-checkin")
async def gambler_checkin(req: LoginRequest):
    """赌狗签到 (-3 ~ +30积分，高风险高回报)"""
    try:
        result = await hdhive_service.do_gambler_checkin(req.account_id)

        # 发送签到通知
        try:
            account = next((a for a in hdhive_service.config.accounts if a.id == req.account_id), None)
            if account and result.get("success"):
                account_name = account.name or (account.user_info.nickname if account.user_info else None) or req.account_id
                total_points = account.user_info.points if account.user_info else 0

                # 解析获得的积分（积分已由 service 内部累积）
                points = result.get("points", 0)
                message = result.get("message", "")
                if "已签到" in message or result.get("already_checked_in"):
                    status = "already"
                else:
                    status = "success"

                notify_msg = f"🎲 赌狗模式: {message}"

                wechat_result = wechat_notify_service.notify_checkin(
                    account_name=account_name,
                    points=points,
                    total_points=total_points,
                    status=status,
                    message=notify_msg,
                    checkin_count=account.checkin_count,
                    checkin_points=account.checkin_points or 0
                )
                telegram_result = telegram_notify_service.notify_checkin(
                    account_name=account_name,
                    points=points,
                    total_points=total_points,
                    status=status,
                    message=notify_msg,
                    checkin_count=account.checkin_count,
                    checkin_points=account.checkin_points or 0
                )
                logger.info(f"[HDHive] 赌狗签到通知: 微信={wechat_result}, Telegram={telegram_result}")

                # 推送签到成功事件到前端
                if status == "success":
                    await hdhive_service.push_checkin_event("checkin_success")
            elif account:
                logger.info(f"[HDHive] 赌狗签到结果非成功，跳过通知: {result}")
        except Exception as notify_err:
            logger.error(f"[HDHive] 发送赌狗签到通知异常: {notify_err}")

        return result
    except Exception as e:
        logger.error(f"[HDHive] 赌狗签到失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/user-info")
async def get_user_info(req: LoginRequest):
    """获取用户信息"""
    try:
        account = next((a for a in hdhive_service.config.accounts if a.id == req.account_id), None)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        if account.openapi_access_token:
            try:
                user_data = hdhive_service.run_openapi_call(account, lambda client: client.get_me() or {})
                user_meta = user_data.get("user_meta", {}) if isinstance(user_data, dict) else {}
                result = {
                    "success": True,
                    "user_info": {
                        "id": user_data.get("id", 0),
                        "nickname": user_data.get("nickname", ""),
                        "username": user_data.get("username", ""),
                        "email": user_data.get("email", ""),
                        "avatar_url": user_data.get("avatar_url", ""),
                        "is_vip": user_data.get("is_vip", False),
                        "vip_expiration_date": user_data.get("vip_expiration_date", ""),
                        "last_active_at": user_data.get("last_active_at", ""),
                        "created_at": user_data.get("created_at", ""),
                        "telegram_user": user_data.get("telegram_user"),
                        "points": user_meta.get("points", 0),
                        "signin_days_total": user_meta.get("signin_days_total", 0),
                        "share_num": user_meta.get("share_num", 0),
                        "is_activate": user_meta.get("is_activate", False),
                        "notification_method": user_meta.get("notification_method", ""),
                    },
                }
            except Exception as e:
                result = {"success": False, "error": str(e)}
        elif account.token:
            result = await hdhive_service.get_user_info(account.token)
        else:
            return {"status": "error", "message": "请先完成 OpenAPI 授权"}

        if result.get("success"):
            # 更新账号的用户信息
            from app.services.hdhive_service import HDHiveUserInfo
            user_info = HDHiveUserInfo(**result["user_info"])
            hdhive_service.update_account(req.account_id, user_info=user_info)
            return {"status": "ok", "user_info": result["user_info"]}
        else:
            return {"status": "error", "message": result.get("error", "获取用户信息失败")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[HDHive] 获取用户信息失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/usage")
async def get_usage(req: LoginRequest):
    """获取API用量信息和用户详细信息"""
    try:
        account = next((a for a in hdhive_service.config.accounts if a.id == req.account_id), None)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        if not hdhive_service.account_has_openapi_credentials(account):
            return {"status": "error", "message": "请先填写 OpenAPI 应用 Secret"}

        result = await hdhive_service.get_usage(account)
        if result.get("success"):
            # 更新账号的用量信息
            from app.services.hdhive_service import HDHiveUsage
            usage = HDHiveUsage(**result["usage"])
            hdhive_service.update_account(req.account_id, usage=usage)

            # 如果有用户详细信息，也更新
            response_data = {"status": "ok", "usage": result["usage"]}

            # 检查是否需要VIP
            if result.get("vip_required"):
                response_data["vip_required"] = True

            if result.get("user_detail"):
                # 更新 user_info 中的详细字段
                if account.user_info:
                    detail = result["user_detail"]
                    account.user_info.id = detail.get("id", account.user_info.id)
                    account.user_info.nickname = detail.get("nickname", account.user_info.nickname)
                    account.user_info.username = detail.get("username", account.user_info.username)
                    account.user_info.email = detail.get("email", account.user_info.email)
                    account.user_info.avatar_url = detail.get("avatar_url", "")
                    account.user_info.is_vip = detail.get("is_vip", False)
                    account.user_info.vip_expiration_date = detail.get("vip_expiration_date", "")
                    account.user_info.last_active_at = detail.get("last_active_at", "")
                    account.user_info.created_at = detail.get("created_at", "")
                    account.user_info.telegram_user = detail.get("telegram_user")
                    account.user_info.points = detail.get("points", account.user_info.points)
                    account.user_info.signin_days_total = detail.get("signin_days_total", account.user_info.signin_days_total)
                    account.user_info.share_num = detail.get("share_num", account.user_info.share_num)
                    account.user_info.is_activate = detail.get("is_activate", False)
                    account.user_info.notification_method = detail.get("notification_method", "")
                    hdhive_service._save_config()
                response_data["user_detail"] = result["user_detail"]

            return response_data
        else:
            return {"status": "error", "message": result.get("error", "获取用量信息失败")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[HDHive] 获取用量信息失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
