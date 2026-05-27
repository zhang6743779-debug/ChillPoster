"""
影巢 (HDHive) 服务模块
"""

import os
import json
import asyncio
import httpx
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Optional, List, Dict
from urllib.parse import urlencode

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel

from core.logger import logger
from core.configs import global_config
from app.services.hdhive_openapi_client import (
    HDHiveOpenClient,
    HDHiveAPIError,
    HDHiveForbiddenError,
)
from app.services.hdhive_playwright_client import (
    HDHivePlaywrightClient,
    HDHiveLoginError,
)

CONFIG_DIR = "config"
HDHIVE_CONFIG_PATH = os.path.join(CONFIG_DIR, "hdhive.json")
DEFAULT_OPENAPI_CLIENT_ID = "app_9f2C01307386e5b289ee9d28"
DEFAULT_OPENAPI_APP_SECRET = "451984025101c539dfb5d14256bcae1e"


class HDHiveUserInfo(BaseModel):
    id: int = 0
    nickname: str = ""
    username: str = ""
    email: str = ""
    is_admin: bool = False
    is_vip: bool = False
    vip_expiration_date: str = ""
    last_active_at: str = ""
    warnings_nums: int = 0
    points: int = 0
    signin_days_total: int = 0
    share_num: int = 0
    is_activate: bool = False
    notification_method: str = ""
    avatar_url: str = ""
    created_at: str = ""
    telegram_user: Optional[dict] = None


class HDHiveUsage(BaseModel):
    quota: int = 0
    used: int = 0
    today_used: int = 0


class HDHiveAccount(BaseModel):
    id: str = ""
    name: Optional[str] = ""
    password: Optional[str] = ""
    token: str = ""
    api_key: Optional[str] = ""
    openapi_client_id: Optional[str] = ""
    openapi_app_secret: Optional[str] = ""
    openapi_redirect_uri: Optional[str] = ""
    openapi_access_token: Optional[str] = ""
    openapi_refresh_token: Optional[str] = ""
    openapi_token_expires_at: Optional[int] = 0
    openapi_refresh_expires_at: Optional[int] = 0
    openapi_scope: Optional[str] = ""
    openapi_oauth_state: Optional[str] = ""
    openapi_authorized_at: Optional[str] = ""
    status: str = "unknown"
    last_checkin: str = ""
    checkin_count: int = 0
    checkin_points: int = 0
    enabled: Optional[bool] = True
    checkin_type: Optional[str] = "none"
    checkin_cron: Optional[str] = ""
    user_info: Optional[HDHiveUserInfo] = None
    usage: Optional[HDHiveUsage] = None


class HDHiveConfig(BaseModel):
    accounts: List[HDHiveAccount] = []


class HDHiveService:
    BASE_URL = "https://hdhive.com"

    def __init__(self):
        self.config = HDHiveConfig()
        self.scheduler = None
        self._checkin_event_queue: Optional[asyncio.Queue] = None
        self._account_cookies: Dict[str, str] = {}
        self._load_config()

    def _parse_and_accumulate_points(self, account: HDHiveAccount, message: str, is_gambler: bool) -> int:
        import re

        points = 0
        if is_gambler:
            match = re.search(r"[+＋]\s*(\d+)", message)
            if match:
                points = int(match.group(1))
            else:
                match = re.search(r"[-－]\s*(\d+)", message)
                if match:
                    points = -int(match.group(1))
                else:
                    match = re.search(r"获得\s*([+-]?\d+)", message)
                    if match:
                        points = int(match.group(1))
        else:
            match = re.search(r"[+＋]\s*(\d+)", message)
            if match:
                points = int(match.group(1))
            else:
                match = re.search(r"获得\s*(\d+)", message)
                if match:
                    points = int(match.group(1))

        if points != 0:
            account.checkin_points = (account.checkin_points or 0) + points
        return points

    def _get_event_queue(self) -> asyncio.Queue:
        if self._checkin_event_queue is None:
            self._checkin_event_queue = asyncio.Queue()
        return self._checkin_event_queue

    async def push_checkin_event(self, event_type: str):
        queue = self._get_event_queue()
        try:
            queue.put_nowait(event_type)
        except asyncio.QueueFull:
            pass

    def _get_proxy(self) -> Optional[str]:
        return global_config.proxy_url

    def _create_client(self, **kwargs) -> httpx.AsyncClient:
        proxy = self._get_proxy()
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    def _openapi_secret_for_account(self, account: HDHiveAccount) -> str:
        return str(account.openapi_app_secret or DEFAULT_OPENAPI_APP_SECRET).strip()

    def _openapi_client_id_for_account(self, account: HDHiveAccount) -> str:
        return str(account.openapi_client_id or DEFAULT_OPENAPI_CLIENT_ID).strip()

    def account_has_openapi_credentials(self, account: HDHiveAccount) -> bool:
        return bool(self._openapi_secret_for_account(account))

    def account_has_openapi_user_token(self, account: HDHiveAccount) -> bool:
        return bool(str(account.openapi_access_token or "").strip())

    def account_can_query_openapi(self, account: HDHiveAccount) -> bool:
        return bool(self._openapi_secret_for_account(account) and account.openapi_access_token)

    def infer_openapi_redirect_uri(self, request) -> str:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if host:
            base = f"{proto}://{host}".rstrip("/")
        else:
            base = str(request.base_url).rstrip("/")
        return f"{base}/api/hdhive/openapi/callback"

    def build_openapi_authorize_url(self, account_id: str, request, scope: str = "meta query unlock write") -> dict:
        account = next((a for a in self.config.accounts if a.id == account_id), None)
        if not account:
            raise ValueError("账号不存在")
        client_id = self._openapi_client_id_for_account(account)
        if not client_id:
            raise ValueError("缺少 OpenAPI Client ID")
        redirect_uri = str(account.openapi_redirect_uri or "").strip() or self.infer_openapi_redirect_uri(request)
        state = uuid.uuid4().hex
        account.openapi_redirect_uri = redirect_uri
        account.openapi_oauth_state = state
        self._save_config()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
        }
        return {
            "url": f"{self.BASE_URL}/openapi/authorize?{urlencode(params)}",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
        }

    def _oauth_request(self, path: str, app_secret: str, body: dict[str, Any]) -> dict[str, Any]:
        proxy = self._get_proxy() or None
        with httpx.Client(
            base_url=f"{self.BASE_URL}/api/public/openapi/oauth",
            headers={"X-API-Key": app_secret, "Content-Type": "application/json"},
            timeout=30.0,
            verify=False,
            proxy=proxy,
        ) as client:
            resp = client.post(path, json=body)
            try:
                payload = resp.json()
            except Exception:
                resp.raise_for_status()
                raise
            if not payload.get("success"):
                raise HDHiveAPIError(
                    code=str(payload.get("code", resp.status_code)),
                    message=str(payload.get("message", "OpenAPI OAuth failed")),
                    description=payload.get("description"),
                    http_status=resp.status_code,
                )
            data = payload.get("data")
            return data if isinstance(data, dict) else {}

    def exchange_openapi_code(self, code: str, state: str) -> HDHiveAccount:
        account = next((a for a in self.config.accounts if a.openapi_oauth_state and a.openapi_oauth_state == state), None)
        if not account:
            raise ValueError("授权 state 无效或已过期")
        app_secret = self._openapi_secret_for_account(account)
        redirect_uri = str(account.openapi_redirect_uri or "").strip()
        if not redirect_uri:
            raise ValueError("缺少 OpenAPI 回调地址，请先在配置中保存回调地址")
        data = self._oauth_request(
            "/token",
            app_secret,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        self._apply_openapi_token(account, data)
        account.openapi_oauth_state = ""
        self._save_config()
        return account

    def _apply_openapi_token(self, account: HDHiveAccount, data: dict[str, Any]) -> None:
        now = int(time.time())
        account.openapi_access_token = str(data.get("access_token") or "")
        account.openapi_refresh_token = str(data.get("refresh_token") or account.openapi_refresh_token or "")
        try:
            account.openapi_token_expires_at = now + max(0, int(data.get("expires_in") or 0))
        except Exception:
            account.openapi_token_expires_at = 0
        try:
            refresh_expires = int(data.get("refresh_expires_in") or 0)
            if refresh_expires > 0:
                account.openapi_refresh_expires_at = now + refresh_expires
        except Exception:
            pass
        scopes = data.get("scopes")
        if isinstance(scopes, list):
            account.openapi_scope = " ".join(str(item) for item in scopes if item)
        elif data.get("scope"):
            account.openapi_scope = str(data.get("scope") or "")
        account.openapi_authorized_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def refresh_openapi_token(self, account: HDHiveAccount) -> bool:
        app_secret = self._openapi_secret_for_account(account)
        refresh_token = str(account.openapi_refresh_token or "").strip()
        if not app_secret or not refresh_token:
            return False
        data = self._oauth_request("/refresh", app_secret, {"refresh_token": refresh_token})
        self._apply_openapi_token(account, data)
        self._save_config()
        return True

    def ensure_openapi_token_fresh(self, account: HDHiveAccount) -> bool:
        expires_at = int(account.openapi_token_expires_at or 0)
        if self._openapi_secret_for_account(account) and account.openapi_refresh_token and expires_at and expires_at <= int(time.time()) + 60:
            return self.refresh_openapi_token(account)
        return False

    def run_openapi_call(self, account: HDHiveAccount, callback: Callable[[HDHiveOpenClient], Any]) -> Any:
        secret = self._openapi_secret_for_account(account)
        if not secret:
            raise HDHiveAPIError("MISSING_API_KEY", "请先填写 OpenAPI 应用 Secret", http_status=401)
        self.ensure_openapi_token_fresh(account)
        access_token = str(account.openapi_access_token or "").strip()
        try:
            with HDHiveOpenClient(secret, access_token=access_token) as client:
                return callback(client)
        except HDHiveAPIError as e:
            if e.code == "OPENAPI_REFRESH_REQUIRED" and self.refresh_openapi_token(account):
                with HDHiveOpenClient(secret, access_token=str(account.openapi_access_token or "").strip()) as client:
                    return callback(client)
            raise

    def _has_openapi_scope(self, account: HDHiveAccount, scope: str) -> bool:
        scopes = {part.strip() for part in str(account.openapi_scope or "").split() if part.strip()}
        return scope in scopes

    def _can_use_openapi_checkin(self, account: HDHiveAccount) -> bool:
        return bool(self._openapi_secret_for_account(account) and account.openapi_access_token)

    async def _openapi_checkin(self, account: HDHiveAccount, is_gambler: bool = False) -> dict:
        if not self._can_use_openapi_checkin(account):
            return {"success": False, "error": "请先完成 OpenAPI 授权"}
        if account.openapi_scope and not self._has_openapi_scope(account, "write"):
            return {"success": False, "error": "OpenAPI 授权缺少 write scope，请重新授权"}
        try:
            data = await asyncio.to_thread(
                lambda: self.run_openapi_call(account, lambda client: client.checkin(is_gambler=is_gambler))
            )
            message = str(data.get("message") or data.get("msg") or "签到成功") if isinstance(data, dict) else "签到成功"
            already = any(k in message for k in ["已签到", "已经签到", "明天再来", "签到过"])
            return {
                "success": True,
                "message": message,
                "already_checked_in": already,
                "data": data if isinstance(data, dict) else {},
                "via": "openapi",
            }
        except HDHiveAPIError as e:
            if e.code in {"SCOPE_NOT_ALLOWED", "USER_SCOPE_NOT_ALLOWED"}:
                return {"success": False, "error": "OpenAPI 授权缺少 write scope，请重新授权", "code": e.code}
            if e.code in {"OPENAPI_USER_REQUIRED", "OPENAPI_REAUTH_REQUIRED", "INVALID_OPENAPI_USER_TOKEN"}:
                return {"success": False, "error": "OpenAPI 授权已失效，请重新授权", "code": e.code}
            return {"success": False, "error": str(e), "code": e.code}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _load_config(self):
        if os.path.exists(HDHIVE_CONFIG_PATH):
            try:
                with open(HDHIVE_CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.config = HDHiveConfig(**data)
                    logger.trace(f"[HDHive] 已加载 {len(self.config.accounts)} 个账号配置")
            except Exception as e:
                logger.error(f"[HDHive] 加载配置失败: {e}")

    def _save_config(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(HDHIVE_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config.model_dump(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[HDHive] 保存配置失败: {e}")

    @staticmethod
    def _decode_token_exp(token: str) -> Optional[int]:
        try:
            import base64

            parts = token.split(".")
            if len(parts) < 2:
                return None
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
            exp = payload.get("exp")
            if isinstance(exp, (int, float)):
                return int(exp)
            return None
        except Exception:
            return None

    @staticmethod
    def _is_auth_related_error(message: str) -> bool:
        msg = (message or "").lower()
        keywords = [
            "未配置cookie",
            "缺少'token'",
            "缺少 token",
            "unauthorized",
            "token",
            "csrf",
            "登录已过期",
            "token已过期",
            "token 过期",
            "过期",
            "expired",
            "jwt",
            "signature has expired",
            "cookie missing",
            "401",
            "forbidden",
        ]
        return any(k in msg for k in keywords)

    def _cache_cookie_for_account(self, account: HDHiveAccount, cookie_str: str):
        if not cookie_str:
            return
        self._account_cookies[account.id] = cookie_str
        if account.name:
            self._account_cookies[account.name] = cookie_str

    async def login(self, name: str, password: str) -> dict:
        try:
            def _run_login():
                client = HDHivePlaywrightClient(headless=True)
                return client.login(username=name, password=password)

            result = await asyncio.to_thread(_run_login)
            if not result:
                return {"error": "登录失败，未获取到 token"}

            cookie_str, token = result
            self._account_cookies[name] = cookie_str
            logger.info(f"[HDHive] 登录成功: {name}")
            return {"success": True, "token": token, "cookie": cookie_str}
        except HDHiveLoginError as e:
            logger.warning(f"[HDHive] 登录失败: {e}")
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"[HDHive] 登录异常: {e}")
            return {"error": str(e)}

    async def _auto_login_for_account(self, account: HDHiveAccount) -> Optional[str]:
        if not account.name or not account.password:
            logger.warning(f"[HDHive] 账号缺少用户名或密码，无法自动登录: {account.id}")
            return None

        result = await self.login(account.name, account.password)
        if result.get("success") and result.get("token"):
            new_token = result["token"]
            account.token = new_token
            self._cache_cookie_for_account(account, result.get("cookie", ""))
            logger.info(f"[HDHive] 自动登录成功，已刷新 token: {account.name or account.id}")
            return new_token

        logger.warning(f"[HDHive] 自动登录失败: {account.name or account.id} - {result.get('error', '未知错误')}")
        return None

    async def _ensure_valid_token(self, account: HDHiveAccount) -> Optional[str]:
        if not account.token:
            return None

        exp_ts = self._decode_token_exp(account.token)
        if exp_ts is None:
            return None

        import time

        if exp_ts <= int(time.time()):
            logger.info(f"[HDHive] token 已过期，尝试自动刷新: {account.name or account.id}")
            return await self._auto_login_for_account(account)
        return None

    async def _playwright_action_checkin(self, account: HDHiveAccount, is_gambler: bool = False) -> dict:
        cookie_str = self._account_cookies.get(account.id) or self._account_cookies.get(account.name or "")
        if not cookie_str:
            if not account.token:
                return {"success": False, "error": "缺少 token"}
            cookie_str = f"token={account.token}"

        try:
            def _run_checkin():
                client = HDHivePlaywrightClient(headless=True)
                return client.checkin(cookie_str=cookie_str, gamble=is_gambler)

            ok, msg = await asyncio.to_thread(_run_checkin)
            if ok:
                already = any(k in (msg or "") for k in ["已签到", "已经签到", "明天再来", "签到过"])
                return {"success": True, "message": msg or "签到成功", "already_checked_in": already}
            return {"success": False, "error": msg or "签到失败"}
        except Exception as e:
            logger.error(f"[HDHive] Playwright Action 签到异常: {e}")
            return {"success": False, "error": str(e)}

    async def _checkin_with_token_retry(self, account: HDHiveAccount, is_gambler: bool = False) -> dict:
        """执行一次签到；若因 token/鉴权问题失败，则自动修复后仅重签一次。"""
        result = await self._playwright_action_checkin(account, is_gambler=is_gambler)
        if result.get("success"):
            return result

        err_msg = result.get("error", "")
        if not self._is_auth_related_error(err_msg):
            return result

        logger.info(f"[HDHive] 检测到 token 失效，尝试自动修复并重签一次: {account.name or account.id}")
        if await self._auto_login_for_account(account):
            self._save_config()
            return await self._playwright_action_checkin(account, is_gambler=is_gambler)

        return result

    async def _finalize_checkin_success(self, account: HDHiveAccount, result: dict, is_gambler: bool) -> dict:
        if not result.get("already_checked_in"):
            account.last_checkin = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account.checkin_count += 1
            result["points"] = self._parse_and_accumulate_points(account, result.get("message", ""), is_gambler=is_gambler)
        else:
            result["points"] = 0

        account.status = "ok"
        user_result = await self.get_user_info(account.token)
        if user_result.get("success"):
            account.user_info = HDHiveUserInfo(**user_result["user_info"])
        self._save_config()
        return result

    async def do_checkin(self, account_id: str) -> dict:
        account = next((a for a in self.config.accounts if a.id == account_id), None)
        if not account:
            return {"success": False, "message": "账号不存在"}

        if self._can_use_openapi_checkin(account):
            result = await self._openapi_checkin(account, is_gambler=False)
            if result.get("success"):
                return await self._finalize_checkin_success(account, result, is_gambler=False)
            if account.openapi_scope or result.get("code"):
                account.status = "error"
                self._save_config()
                err = result.get("error", "OpenAPI 签到失败")
                return {"success": False, "message": err, "error": err, "code": result.get("code")}

        refreshed = await self._ensure_valid_token(account)
        if refreshed:
            self._save_config()

        cookie_str = self._account_cookies.get(account.id) or self._account_cookies.get(account.name or "")
        if not cookie_str and account.name and account.password:
            await self._auto_login_for_account(account)

        if not account.token and account.name and account.password:
            new_token = await self._auto_login_for_account(account)
            if new_token:
                self._save_config()

        if not account.token:
            return {"success": False, "message": "请先配置用户名密码或 Token"}

        result = await self._checkin_with_token_retry(account, is_gambler=False)
        if result.get("success"):
            return await self._finalize_checkin_success(account, result, is_gambler=False)

        account.status = "error"
        self._save_config()
        err = result.get("error", "签到失败")
        return {"success": False, "message": err, "error": err}

    async def do_gambler_checkin(self, account_id: str) -> dict:
        account = next((a for a in self.config.accounts if a.id == account_id), None)
        if not account:
            return {"success": False, "message": "账号不存在"}

        if self._can_use_openapi_checkin(account):
            result = await self._openapi_checkin(account, is_gambler=True)
            if result.get("success"):
                return await self._finalize_checkin_success(account, result, is_gambler=True)
            if account.openapi_scope or result.get("code"):
                account.status = "error"
                self._save_config()
                err = result.get("error", "OpenAPI 签到失败")
                return {"success": False, "message": err, "error": err, "code": result.get("code")}

        refreshed = await self._ensure_valid_token(account)
        if refreshed:
            self._save_config()

        cookie_str = self._account_cookies.get(account.id) or self._account_cookies.get(account.name or "")
        if not cookie_str and account.name and account.password:
            await self._auto_login_for_account(account)

        if not account.token and account.name and account.password:
            new_token = await self._auto_login_for_account(account)
            if new_token:
                self._save_config()

        if not account.token:
            return {"success": False, "message": "请先配置用户名密码或 Token"}

        result = await self._checkin_with_token_retry(account, is_gambler=True)
        if result.get("success"):
            return await self._finalize_checkin_success(account, result, is_gambler=True)

        account.status = "error"
        self._save_config()
        err = result.get("error", "签到失败")
        return {"success": False, "message": err, "error": err}

    async def checkin_all(self) -> dict:
        results = []
        for account in self.config.accounts:
            if account.enabled and (account.token or (account.name and account.password)):
                result = await self.do_checkin(account.id)
                results.append(
                    {
                        "name": account.name,
                        "success": result.get("success", False),
                        "message": result.get("message", ""),
                    }
                )
        return {"results": results}

    async def get_user_info(self, token: str) -> dict:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Cookie": f"token={token}",
            "RSC": "1",
        }

        async with self._create_client(timeout=30, verify=False) as client:
            try:
                resp = await client.get(
                    f"{self.BASE_URL}/user/dashboard",
                    headers=headers,
                )

                if resp.status_code != 200:
                    return {"success": False, "error": f"HTTP {resp.status_code}"}

                text = resp.text
                for line in text.split("\n"):
                    if '"currentUser":{' in line and '"user_meta":{' in line:
                        try:
                            start = line.find('"currentUser":{')
                            if start < 0:
                                continue

                            json_start = start + len('"currentUser":')
                            brace_count = 0
                            json_end = json_start

                            for i, c in enumerate(line[json_start:], json_start):
                                if c == "{":
                                    brace_count += 1
                                elif c == "}":
                                    brace_count -= 1
                                    if brace_count == 0:
                                        json_end = i + 1
                                        break

                            json_str = line[json_start:json_end]
                            user = json.loads(json_str)
                            user_meta = user.get("user_meta", {})

                            return {
                                "success": True,
                                "user_info": {
                                    "id": user.get("id", 0),
                                    "nickname": user.get("nickname", ""),
                                    "username": user.get("username", ""),
                                    "email": user.get("email", ""),
                                    "is_admin": user.get("is_admin", False),
                                    "is_vip": user.get("is_vip", False),
                                    "warnings_nums": user.get("warnings_nums", 0),
                                    "points": user_meta.get("points", 0),
                                    "signin_days_total": user_meta.get("signin_days_total", 0),
                                    "share_num": user_meta.get("share_num", 0),
                                    "avatar_url": user.get("avatar_url", ""),
                                },
                            }
                        except json.JSONDecodeError:
                            continue

                return {"success": False, "error": "无法解析用户信息"}
            except Exception as e:
                return {"success": False, "error": str(e)}

    async def get_usage(self, account: HDHiveAccount) -> dict:
        try:
            def _run_open_usage():
                quota = self.run_openapi_call(account, lambda client: client.get_quota() or {})
                usage = self.run_openapi_call(account, lambda client: client.get_usage() or {})
                today = self.run_openapi_call(account, lambda client: client.get_usage_today() or {})
                user = {}
                if account.openapi_access_token:
                    try:
                        user = self.run_openapi_call(account, lambda client: client.get_me() or {})
                    except HDHiveAPIError as e:
                        if e.code not in {"OPENAPI_USER_REQUIRED", "USER_SCOPE_NOT_ALLOWED", "SCOPE_NOT_ALLOWED"}:
                            raise
                return quota, usage, today, user

            quota_data, usage_data, today_data, user_data = await asyncio.to_thread(_run_open_usage)

            user_meta = user_data.get("user_meta", {}) if isinstance(user_data, dict) else {}
            return {
                "success": True,
                "usage": {
                    "quota": quota_data.get("quota") or quota_data.get("endpoint_limit") or 0,
                    "used": usage_data.get("total") or usage_data.get("total_calls") or 0,
                    "today_used": today_data.get("total") or today_data.get("total_calls") or 0,
                },
                "user_detail": {
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
        except HDHiveForbiddenError as e:
            if getattr(e, "code", "") == "VIP_REQUIRED":
                return {"success": False, "error": "此功能需要 Premium 会员", "vip_required": True}
            return {"success": False, "error": str(e)}
        except HDHiveAPIError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def test_account(self, account_id: str) -> dict:
        account = next((a for a in self.config.accounts if a.id == account_id), None)
        if not account:
            return {"success": False, "message": "账号不存在"}

        if self.account_has_openapi_credentials(account):
            usage_result = await self.get_usage(account)
            if usage_result.get("success"):
                account.status = "ok"
                if usage_result.get("user_detail"):
                    account.user_info = HDHiveUserInfo(**usage_result["user_detail"])
                account.usage = HDHiveUsage(**usage_result.get("usage", {}))
                self._save_config()
                return {"success": True, "message": "OpenAPI 凭证有效"}

        refreshed = await self._ensure_valid_token(account)
        if refreshed:
            self._save_config()

        if account.token:
            user_result = await self.get_user_info(account.token)
            if user_result.get("success"):
                account.status = "ok"
                account.user_info = HDHiveUserInfo(**user_result["user_info"])
                self._save_config()
                return {"success": True, "message": "Token 有效"}

        if await self._auto_login_for_account(account):
            account.status = "ok"
            user_result = await self.get_user_info(account.token)
            if user_result.get("success"):
                account.user_info = HDHiveUserInfo(**user_result["user_info"])
            self._save_config()
            return {"success": True, "message": "登录成功，Token 已获取"}

        account.status = "error"
        self._save_config()
        return {"success": False, "message": "请填写密码或手动输入 Token"}

    def add_account(
        self,
        name: str,
        password: str = "",
        token: str = "",
        api_key: str = "",
        openapi_client_id: str = "",
        openapi_app_secret: str = "",
        openapi_redirect_uri: str = "",
    ) -> HDHiveAccount:
        import uuid

        account = HDHiveAccount(
            id=str(uuid.uuid4())[:8],
            name=name,
            password=password,
            token=token,
            api_key=api_key,
            openapi_client_id=openapi_client_id,
            openapi_app_secret=openapi_app_secret,
            openapi_redirect_uri=openapi_redirect_uri,
        )
        self.config.accounts.append(account)
        self._save_config()
        logger.info(f"[HDHive] 添加账号: {name}")
        return account

    def update_account(self, account_id: str, **kwargs) -> Optional[HDHiveAccount]:
        account = next((a for a in self.config.accounts if a.id == account_id), None)
        if account:
            for key, value in kwargs.items():
                if hasattr(account, key) and value is not None:
                    setattr(account, key, value)
            self._save_config()
            if self.scheduler:
                self._refresh_jobs()
        return account

    def remove_account(self, account_id: str) -> bool:
        for i, a in enumerate(self.config.accounts):
            if a.id == account_id:
                self.config.accounts.pop(i)
                self._save_config()
                logger.info(f"[HDHive] 删除账号: {account_id}")
                return True
        return False

    def get_config(self) -> dict:
        return self.config.model_dump()

    def setup_scheduler(self, scheduler):
        self.scheduler = scheduler
        self._refresh_jobs()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.recover_accounts_on_startup())
        except RuntimeError:
            pass

    async def recover_accounts_on_startup(self):
        if not self.config.accounts:
            return

        # 仅针对“已启用 + 自动签到开启(checkin_type!=none)”的账号做 token 自修复
        candidates = [
            a for a in self.config.accounts
            if a.enabled and a.checkin_type and a.checkin_type != "none"
        ]
        if not candidates:
            return

        logger.trace("[HDHive] 启动后签到账号 token 检查开始")
        changed = False

        for account in candidates:
            account_name = account.name or account.id

            # 无 token：仅在可自动登录时修复
            if not account.token:
                if account.name and account.password:
                    logger.info(f"[HDHive] 检测到 token 缺失，尝试自动修复: {account_name}")
                    if await self._auto_login_for_account(account):
                        account.status = "ok"
                        changed = True
                        logger.info(f"[HDHive] 自动修复成功: {account_name}")
                    else:
                        logger.warning(f"[HDHive] 自动修复失败: {account_name}")
                continue

            # 有 token：仅在已过期时修复，未过期不打印“自动修复”日志
            refreshed = await self._ensure_valid_token(account)
            if refreshed:
                account.status = "ok"
                changed = True
                logger.info(f"[HDHive] 自动修复成功: {account_name}")

        if changed:
            self._save_config()
        logger.trace("[HDHive] 启动后签到账号 token 检查完成")

    def _refresh_jobs(self):
        if not self.scheduler:
            return

        for account in self.config.accounts:
            try:
                self.scheduler.remove_job(f"hdhive_checkin_{account.id}")
            except Exception:
                pass

        for account in self.config.accounts:
            if not account.enabled:
                continue
            if not account.checkin_type or account.checkin_type == "none":
                continue

            cron_expr = account.checkin_cron or "0 8 * * *"
            try:
                trigger = CronTrigger.from_crontab(cron_expr)
                self.scheduler.add_job(
                    self._scheduled_checkin_sync,
                    trigger,
                    id=f"hdhive_checkin_{account.id}",
                    args=[account.id],
                    replace_existing=True,
                )
                checkin_type_name = "赌狗签到" if account.checkin_type == "gambler" else "普通签到"
                logger.trace(f"[HDHive] 已添加签到任务: {account.name or account.id} ({checkin_type_name} {cron_expr})")
            except Exception as e:
                logger.error(f"[HDHive] 添加签到任务失败 {account.name or account.id}: {e}")

    async def _scheduled_checkin_single(self, account_id: str):
        account = next((a for a in self.config.accounts if a.id == account_id), None)
        if not account:
            return

        is_gambler = account.checkin_type == "gambler"
        checkin_type_name = "赌狗签到" if is_gambler else "普通签到"
        logger.info(f"[HDHive] 开始定时签到: {account.name} ({checkin_type_name})")

        if is_gambler:
            result = await self.do_gambler_checkin(account_id)
        else:
            result = await self.do_checkin(account_id)

        if result.get("success"):
            logger.info(f"[HDHive] 定时签到成功: {account.name}")
        else:
            logger.warning(f"[HDHive] 定时签到失败: {account.name} - {result.get('message')}")

        try:
            from app.services.wechat_service import wechat_notify_service
            from app.services.telegram_service import telegram_notify_service

            account_name = account.name or (account.user_info.nickname if account.user_info else None) or account_id
            total_points = account.user_info.points if account.user_info else 0
            message = result.get("message", "")

            if "已签到" in message or result.get("already_checked_in"):
                status = "already"
            elif result.get("success"):
                status = "success"
            else:
                status = "failed"

            if status == "success":
                await self.push_checkin_event("checkin_success")

            notify_msg = f"🎲 赌狗模式: {message}" if is_gambler else message
            points = result.get("points", 0)
            wechat_notify_service.notify_checkin(
                account_name=account_name,
                points=points,
                total_points=total_points,
                status=status,
                message=notify_msg,
                checkin_count=account.checkin_count,
                checkin_points=account.checkin_points or 0,
            )
            telegram_notify_service.notify_checkin(
                account_name=account_name,
                points=points,
                total_points=total_points,
                status=status,
                message=notify_msg,
                checkin_count=account.checkin_count,
                checkin_points=account.checkin_points or 0,
            )
        except Exception as notify_err:
            logger.error(f"[HDHive] 定时签到通知异常: {notify_err}")

    def _scheduled_checkin_sync(self, account_id: str):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._scheduled_checkin_single(account_id))
        except Exception as e:
            logger.error(f"[HDHive] 定时签到异常: {e}")
        finally:
            loop.close()


hdhive_service = HDHiveService()
