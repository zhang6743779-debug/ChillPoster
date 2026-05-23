from typing import Any

from p115client import P115Client
from p115client.const import SSOENT_TO_APP


def format_115_login_app_label(app: str) -> str:
    app = (app or "").strip()
    if not app:
        return ""
    mapping = {
        "web": "115生活(网页版)",
        "desktop": "115浏览器",
        "android": "115生活(Android端)",
        "ios": "115生活(iOS端)",
        "ipad": "115生活(iPad端)",
        "115android": "115网盘(Android端)",
        "115ios": "115网盘(iOS端)",
        "115ipad": "115网盘(iPad端)",
        "tv": "115生活(Android电视端)",
        "apple_tv": "115生活(Apple TV端)",
        "qandroid": "115管理(Android端)",
        "qios": "115管理(iOS端)",
        "qipad": "115管理(iPad端)",
        "windows": "115生活(Windows端)",
        "os_windows": "115生活(Windows端)",
        "mac": "115生活(macOS端)",
        "os_mac": "115生活(macOS端)",
        "linux": "115生活(Linux端)",
        "os_linux": "115生活(Linux端)",
        "wechatmini": "115生活(微信小程序)",
        "alipaymini": "115生活(支付宝小程序)",
        "harmony": "115网盘(鸿蒙端)",
    }
    return mapping.get(app, app)


def _safe_call(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def probe_115_cookie(cookie: str, configured_name: str = "115 网盘") -> dict:
    cookie = str(cookie or "").strip()
    if not cookie:
        return {"status": "error", "message": "Cookie 为空"}

    client = P115Client(cookie)
    user_info = _safe_call(client.user_info, {}) or {}
    user_data = user_info.get("data") if isinstance(user_info, dict) else {}
    if not isinstance(user_data, dict):
        user_data = {}

    login_info = _safe_call(client.login_info, {}) or {}
    login_data = login_info.get("data") if isinstance(login_info, dict) else {}
    if not isinstance(login_data, dict):
        login_data = {}

    if not user_info.get("state") and not user_data and not login_data:
        message = (
            user_info.get("error")
            or user_info.get("message")
            or login_info.get("error")
            or login_info.get("message")
            or "Cookie 无效或已过期"
        )
        return {"status": "error", "message": str(message)}

    user_my = _safe_call(client.user_my, {}) or {}
    user_my_data = user_my.get("data") if isinstance(user_my, dict) else {}
    if not isinstance(user_my_data, dict):
        user_my_data = {}

    login_app = _safe_str(_safe_call(client.login_app, "") or SSOENT_TO_APP.get(client.login_ssoent) or "")
    vip_forever = bool(user_my_data.get("forever") or login_data.get("is_forever"))
    vip_active = bool(
        vip_forever
        or _safe_int(user_my_data.get("vip")) > 0
        or _safe_int(user_data.get("is_vip")) > 0
        or _safe_int(login_data.get("is_vip")) > 0
    )

    return {
        "status": "ok",
        "message": "连接成功! Cookie 有效",
        "client": client,
        "login_data": login_data,
        "user_data": user_data,
        "user_my_data": user_my_data,
        "account_name": _safe_str(
            user_data.get("user_name")
            or user_data.get("user_name_prepub")
            or user_my_data.get("user_name")
            or login_data.get("user_name"),
            configured_name,
        ),
        "uid": _safe_str(
            user_data.get("display_uid")
            or user_data.get("user_id")
            or user_my_data.get("display_uid")
            or user_my_data.get("user_id")
            or login_data.get("user_id"),
            "--",
        ),
        "login_app": login_app,
        "login_app_label": format_115_login_app_label(login_app),
        "vip_forever": vip_forever,
        "vip_active": vip_active,
    }
