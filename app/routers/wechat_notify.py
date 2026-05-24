# app/routers/wechat_notify.py
import hashlib
import asyncio
import xml.etree.ElementTree as ET
from functools import partial
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from app.schemas import (
    WechatNotifyConfigModel,
    TelegramNotifyConfigModel,
    TelegramSendCodeRequest,
    TelegramSignInRequest,
    TelegramDialogsRequest,
)
from app.services.wechat_service import wechat_notify_service, NOTIFICATION_TYPES as WECHAT_NOTIFICATION_TYPES
from app.services.telegram_service import telegram_notify_service
from app.services.transfer_service import transfer_service
from core.logger import logger

router = APIRouter(tags=["Notify"])


# ==========================================
# 通知类型（通用）
# ==========================================
NOTIFICATION_TYPES = {
    "playback": {
        "name": "播放通知",
        "description": "有人通过302播放媒体时发送通知",
        "icon": "🎬"
    },
    "media_added": {
        "name": "入库通知",
        "description": "新媒体添加到媒体库时发送通知",
        "icon": "📚"
    },
    "organize_complete": {
        "name": "整理通知",
        "description": "媒体整理完成时发送通知",
        "icon": "💿"
    },
    "wash_result": {
        "name": "洗版通知",
        "description": "整理过程中触发洗版成功或失败时发送通知",
        "icon": "💎"
    },
    "resource_transfer": {
        "name": "转存通知",
        "description": "115网盘转存完成时发送通知",
        "icon": "📥"
    },
    "checkin": {
        "name": "签到通知",
        "description": "影巢签到完成时发送通知",
        "icon": "✅"
    },
    "task_complete": {
        "name": "任务通知",
        "description": "海报生成等任务完成时发送通知",
        "icon": "🎨"
    }
}


# ==========================================
# 通用接口
# ==========================================

@router.get("/api/notify/types")
def get_notification_types():
    """获取可用的通知类型列表"""
    return NOTIFICATION_TYPES


@router.get("/api/notify/channels")
def get_notification_channels():
    """获取所有通知渠道及其状态"""
    return {
        "wechat": {
            "name": "企业微信",
            "enabled": wechat_notify_service.get_config().get("enabled", False),
            "configured": bool(wechat_notify_service.get_config().get("corp_id") and
                              wechat_notify_service.get_config().get("app_secret"))
        },
        "telegram": {
            "name": "Telegram",
            "enabled": telegram_notify_service.get_config().get("enabled", False),
            "configured": bool(telegram_notify_service.get_config().get("bot_token") and
                              telegram_notify_service.get_config().get("chat_id"))
        }
    }


# ==========================================
# 企业微信接口
# ==========================================

@router.get("/api/wechat-notify/types")
def get_wechat_notification_types():
    """获取可用的通知类型列表（兼容旧接口）"""
    return NOTIFICATION_TYPES


@router.get("/api/wechat-notify/config")
def get_wechat_notify_config():
    """获取微信通知配置"""
    return wechat_notify_service.get_config()


@router.post("/api/wechat-notify/config")
def save_wechat_notify_config(cfg: WechatNotifyConfigModel):
    """保存微信通知配置"""
    try:
        wechat_notify_service.update_config(cfg.model_dump())
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/wechat-notify/test")
def test_wechat_notify():
    """测试微信通知连接"""
    result = wechat_notify_service.test_connection()
    if result["success"]:
        return {"status": "ok", "message": result["message"]}
    else:
        return {"status": "error", "message": result["message"]}


@router.post("/api/wechat-notify/send")
def send_wechat_test_message(message: str = "这是一条测试消息"):
    """发送测试消息"""
    success = wechat_notify_service.send_message(message)
    if success:
        return {"status": "ok", "message": "消息发送成功"}
    else:
        return {"status": "error", "message": "消息发送失败"}


@router.post("/api/wechat-notify/test-template")
def test_wechat_template():
    """用当前模板发送测试通知"""
    # 先保存配置（确保模板已持久化）
    # 然后调用 notify_playback 发送一条模板渲染后的测试通知
    success = wechat_notify_service.notify_playback(
        item_name="🎬 测试电影 (2026)",
        emby_name="ChillPoster",
        user_agent="Mozilla/5.0 Test",
        poster_url="",
        original_name="Test Movie",
        media_type="movie",
        overview="这是一条测试通知，用于预览模板效果。如果你看到这条消息，说明模板渲染和通知发送都正常工作。",
        rating="8.5",
        genres="科幻, 动作, 冒险",
        tagline="测试标语 - 模板效果预览",
    )
    if success:
        return {"status": "ok", "message": "模板测试通知发送成功"}
    else:
        return {"status": "error", "message": "模板测试通知发送失败，请检查通知是否启用"}


@router.get("/api/wechat-notify/callback")
async def wechat_callback_verify(
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = ""
):
    """
    企业微信回调 URL 验证
    企业微信会发送 GET 请求来验证 URL 有效性
    """
    config = wechat_notify_service.get_config()
    token = config.get("token", "")

    if not token:
        return PlainTextResponse("token not configured", status_code=400)

    # 验证签名
    sort_list = [token, timestamp, nonce, echostr]
    sort_list.sort()
    sha = hashlib.sha1()
    sha.update("".join(sort_list).encode())
    signature = sha.hexdigest()

    if signature == msg_signature:
        # 解密 echostr（如果配置了 EncodingAESKey）
        encoding_aes_key = config.get("encoding_aes_key", "")
        if encoding_aes_key:
            # 使用企业微信的加密消息解密
            from app.services.wechat_crypto import WeChatCrypto
            try:
                crypto = WeChatCrypto(token, encoding_aes_key, config.get("corp_id", ""))
                echo_str = crypto.decrypt(echostr, msg_signature, timestamp, nonce)
                return PlainTextResponse(echo_str)
            except Exception:
                # 解密失败，直接返回 echostr
                return PlainTextResponse(echostr)
        else:
            return PlainTextResponse(echostr)
    else:
        return PlainTextResponse("invalid signature", status_code=403)


@router.post("/api/wechat-notify/callback")
async def wechat_callback_message(request: Request):
    """
    接收企业微信推送的消息
    流程：读取 body → 解密(如需) → 解析 XML → 提取 115 链接 → 转存 → 通知
    """
    try:
        body = await request.body()
        if not body:
            return PlainTextResponse("success")

        config = wechat_notify_service.get_config()
        encoding_aes_key = config.get("encoding_aes_key", "")
        token = config.get("token", "")
        corp_id = config.get("corp_id", "")

        # 解析 XML
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return PlainTextResponse("success")

        xml_content = body

        # 如果配置了加密，需要解密
        if encoding_aes_key and token:
            encrypt_elem = root.find("Encrypt")
            if encrypt_elem is not None and encrypt_elem.text:
                try:
                    from app.services.wechat_crypto import WeChatCrypto
                    # 获取签名参数（从 query string）
                    msg_signature = request.query_params.get("msg_signature", "")
                    timestamp = request.query_params.get("timestamp", "")
                    nonce = request.query_params.get("nonce", "")
                    crypto = WeChatCrypto(token, encoding_aes_key, corp_id)
                    decrypted = crypto.decrypt(encrypt_elem.text, msg_signature, timestamp, nonce)
                    xml_content = decrypted
                    root = ET.fromstring(xml_content)
                except Exception as e:
                    logger.error(f"[WeChat] 消息解密失败: {e}")
                    return PlainTextResponse("success")

        # 提取消息内容
        msg_type = root.findtext("MsgType", "")
        if msg_type != "text":
            return PlainTextResponse("success")

        content = root.findtext("Content", "")
        if not content:
            return PlainTextResponse("success")

        # 提取资源链接
        links = transfer_service.extract_links(content)
        if not links:
            return PlainTextResponse("success")

        logger.info(f"[WeChat] 收到 {len(links)} 条资源链接，开始处理...")

        # 异步处理转存（不阻塞微信回调响应）
        async def _process_and_notify():
            try:
                results = await transfer_service.process_links(links, source="wechat")
                should_trigger_organize = False
                for result in results:
                    if transfer_service.is_successful_115_transfer(result):
                        should_trigger_organize = True
                    await asyncio.to_thread(
                        partial(
                            send_to_all_channels,
                            title=result.get("status", "转存"),
                            description=result.get("message", ""),
                            notify_type="resource_transfer",
                        )
                    )
                if should_trigger_organize:
                    from app.services.media_organize_core import schedule_auto_organize_after_transfer
                    schedule_auto_organize_after_transfer(
                        drive_index=0,
                        source="wechat",
                        reason="企业微信转存成功",
                    )
            except Exception as e:
                logger.error(f"[WeChat] 转存后台任务异常: {e}", exc_info=True)

        asyncio.create_task(_process_and_notify())

    except Exception as e:
        logger.error(f"[WeChat] 处理回调消息异常: {e}")

    return PlainTextResponse("success")


# ==========================================
# Telegram 接口
# ==========================================

@router.get("/api/telegram-notify/config")
def get_telegram_notify_config():
    """获取 Telegram 通知配置"""
    return telegram_notify_service.get_config()


@router.post("/api/telegram-notify/config")
def save_telegram_notify_config(cfg: TelegramNotifyConfigModel):
    """保存 Telegram 账号监听配置"""
    try:
        telegram_notify_service.update_config(cfg.model_dump(exclude_unset=True))
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/telegram-notify/test")
def test_telegram_notify():
    """测试 Telegram 通知连接"""
    result = telegram_notify_service.test_connection()
    if result["success"]:
        return {"status": "ok", "message": result["message"]}
    return {"status": "error", "message": result["message"]}


@router.get("/api/telegram-notify/status")
async def get_telegram_status():
    """获取 Telegram 账号登录与监听状态"""
    return await telegram_notify_service._account_status_payload()


@router.post("/api/telegram-notify/send-code")
async def send_telegram_login_code(req: TelegramSendCodeRequest):
    """发送 Telegram 登录验证码"""
    return await telegram_notify_service.send_login_code_async(req.api_id, req.api_hash, req.phone)


@router.post("/api/telegram-notify/sign-in")
async def sign_in_telegram(req: TelegramSignInRequest):
    """使用验证码/两步验证密码登录 Telegram"""
    return await telegram_notify_service.sign_in_async(req.code, req.password)


@router.post("/api/telegram-notify/logout")
async def logout_telegram():
    """退出 Telegram 账号登录并清理本地 session"""
    return await telegram_notify_service.logout_async()


@router.get("/api/telegram-notify/dialogs")
async def list_telegram_dialogs():
    """获取 Telegram 群组与频道列表"""
    return await telegram_notify_service.list_dialogs_async()


@router.get("/api/telegram-notify/avatar/{filename}")
async def get_telegram_avatar(filename: str):
    """读取已缓存的 Telegram 群组/频道头像"""
    path = await telegram_notify_service.avatar_path_async(filename)
    if not path:
        raise HTTPException(status_code=404, detail="头像不存在")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/api/telegram-notify/dialogs")
def save_telegram_dialogs(req: TelegramDialogsRequest):
    """保存 Telegram 监听目标"""
    return telegram_notify_service.update_selected_dialogs(req.selected_dialogs)


@router.post("/api/telegram-notify/send")
def send_telegram_test_message(message: str = "这是一条测试消息"):
    """发送 Telegram 测试消息"""
    success = telegram_notify_service.send_message(message)
    if success:
        return {"status": "ok", "message": "消息发送成功"}
    else:
        return {"status": "error", "message": "消息发送失败"}


@router.post("/api/telegram-notify/test-template")
def test_telegram_template():
    """用当前模板发送测试通知"""
    success = telegram_notify_service.notify_playback(
        item_name="🎬 测试电影 (2026)",
        emby_name="ChillPoster",
        user_agent="Mozilla/5.0 Test",
        poster_url="",
        original_name="Test Movie",
        media_type="movie",
        overview="这是一条测试通知，用于预览模板效果。如果你看到这条消息，说明模板渲染和通知发送都正常工作。",
        rating="8.5",
        genres="科幻, 动作, 冒险",
        tagline="测试标语 - 模板效果预览",
    )
    if success:
        return {"status": "ok", "message": "模板测试通知发送成功"}
    else:
        return {"status": "error", "message": "模板测试通知发送失败，请检查通知是否启用"}


# ==========================================
# 统一通知接口（同时发送到所有启用的渠道）
# ==========================================

def send_to_all_channels(title: str, description: str, image_url: str = "",
                         notify_type: str = None, exclude_channels=None):
    """
    发送通知到所有启用的渠道

    Args:
        title: 标题
        description: 描述
        image_url: 图片URL
        notify_type: 通知类型（用于检查是否启用）
        exclude_channels: 要跳过的渠道，例如 {"telegram"}
    """
    exclude_channels = {str(item).lower() for item in (exclude_channels or [])}
    results = {}
    logger.info(f"[通知] 准备发送多渠道通知: 类型={notify_type}, 标题={title}, 跳过渠道={sorted(exclude_channels)}")

    # 检查微信是否启用
    wechat_enabled = wechat_notify_service.is_notify_type_enabled(notify_type) if notify_type else wechat_notify_service.get_config().get("enabled")
    if "wechat" in exclude_channels:
        wechat_enabled = False
    logger.info(f"[通知] 微信通知{'已启用' if wechat_enabled else '未启用'}: 类型={notify_type}")
    if wechat_enabled:
        results["wechat"] = wechat_notify_service.send_news_message(
            title=title,
            description=description,
            image_url=image_url
        )
    else:
        results["wechat"] = None

    # 检查 Telegram 是否启用
    tg_enabled = telegram_notify_service.is_notify_type_enabled(notify_type) if notify_type else telegram_notify_service.get_config().get("enabled")
    if "telegram" in exclude_channels:
        tg_enabled = False
    logger.info(f"[通知] Telegram 通知{'已启用' if tg_enabled else '未启用'}: 类型={notify_type}")
    if tg_enabled:
        results["telegram"] = telegram_notify_service.send_message_with_image(
            title=title,
            description=description,
            image_url=image_url
        )
    else:
        results["telegram"] = None

    return results
