# app/services/telegram_service.py
import os
import json
import asyncio
import threading
import requests
from datetime import datetime
from urllib.parse import urlparse, unquote
from typing import Any
from core.logger import logger
from app.services.notification_formatter import render_template, merge_templates

# TMDB 图片基础 URL


# Telegram 配置文件路径
TELEGRAM_NOTIFY_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config", "telegram_notify.json"
)
TELEGRAM_SESSION_NAME = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config", "telegram_user"
)
TELEGRAM_AVATAR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config", "telegram_avatars"
)


class TelegramNotifyService:
    """Telegram 服务 - 账号登录监听资源消息，并保留旧通知发送入口。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._polling = False
        self._poll_thread = None
        self._offset = 0
        self._login_phone = ""
        self._login_phone_code_hash = ""
        self._monitor_thread = None
        self._monitor_loop = None
        self._monitor_client = None
        self._monitor_stop_event = threading.Event()
        self._monitor_running = False
        self._account_session_lock = asyncio.Lock()
        self._account_user_cache = None
        self.config = self._load_config()
        self._proxies = None
        self._load_proxies()

    def _default_config(self) -> dict:
        return {
            "enabled": False,
            "name": "Telegram",
            "bot_token": "",
            "chat_id": "",
            "account_monitor_enabled": False,
            "api_id": "",
            "api_hash": "",
            "phone": "",
            "selected_dialogs": [],
            "monitor_reply_enabled": False,
            "transfer_dir_mode": "system",
            "transfer_dir": "",
            "bot_update_offset": 0,
            "notify_types": {
                "playback": True,
                "media_added": True,
                "organize_complete": True,
                "resource_transfer": True,
                "checkin": True,
                "task_complete": True
            },
            "templates": {}
        }

    def _normalize_selected_dialogs(self, dialogs: Any) -> list[dict]:
        normalized: list[dict] = []
        if not isinstance(dialogs, list):
            return normalized
        seen = set()
        for item in dialogs:
            if not isinstance(item, dict):
                continue
            dialog_id = str(item.get("id", "") or "").strip()
            if not dialog_id or dialog_id in seen:
                continue
            seen.add(dialog_id)
            normalized.append({
                "id": dialog_id,
                "title": str(item.get("title", "") or "").strip(),
                "type": str(item.get("type", "") or "").strip(),
                "username": str(item.get("username", "") or "").strip().lstrip("@"),
                "avatar_url": str(item.get("avatar_url", "") or "").strip(),
            })
        return normalized

    def _normalize_config(self, config: dict | None) -> dict:
        data = self._default_config()
        if isinstance(config, dict):
            data.update(config)

        # Bot Token 用于通知发送和 bot 入站资源解析；账号监听走 MTProto session。
        data["enabled"] = bool(data.get("enabled"))
        if "account_monitor_enabled" not in data and "monitor_enabled" in data:
            data["account_monitor_enabled"] = bool(data.get("monitor_enabled"))
        data.pop("monitor_enabled", None)
        data.pop("monitor_chat_ids", None)

        data["bot_token"] = str(data.get("bot_token", "") or "").strip()
        data["chat_id"] = str(data.get("chat_id", "") or "").strip()
        data["account_monitor_enabled"] = bool(data.get("account_monitor_enabled"))
        data["api_id"] = str(data.get("api_id", "") or "").strip()
        data["api_hash"] = str(data.get("api_hash", "") or "").strip()
        data["phone"] = str(data.get("phone", "") or "").strip()
        data["selected_dialogs"] = self._normalize_selected_dialogs(data.get("selected_dialogs"))
        data["monitor_reply_enabled"] = False
        data["transfer_dir_mode"] = str(data.get("transfer_dir_mode", "system") or "system").strip()
        if data["transfer_dir_mode"] not in {"system", "custom"}:
            data["transfer_dir_mode"] = "system"
        data["transfer_dir"] = str(data.get("transfer_dir", "") or "").strip()
        try:
            data["bot_update_offset"] = max(0, int(data.get("bot_update_offset") or 0))
        except (TypeError, ValueError):
            data["bot_update_offset"] = 0

        notify_types = data.setdefault("notify_types", {})
        notify_types.setdefault("organize_complete", True)
        notify_types.setdefault("resource_transfer", True)
        data["templates"] = merge_templates(data.get("templates"))
        return data

    def _monitor_transfer_dir(self) -> str | None:
        if self.config.get("transfer_dir_mode") != "custom":
            return None
        transfer_dir = str(self.config.get("transfer_dir", "") or "").strip()
        return transfer_dir or None

    def _load_config(self) -> dict:
        """加载配置"""
        config = None
        if os.path.exists(TELEGRAM_NOTIFY_CONFIG_FILE):
            try:
                with open(TELEGRAM_NOTIFY_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except Exception as e:
                logger.error(f"[Telegram通知] 加载配置失败: {e}")
        return self._normalize_config(config)

    def _load_proxies(self):
        """加载全局代理配置"""
        try:
            import config_manager
            proxy_url = config_manager.APP_CONFIG.get("network_http_proxy")
            if proxy_url:
                self._proxies = {"http": proxy_url, "https": proxy_url}
                logger.info("[启动] Telegram代理配置已加载")
            else:
                self._proxies = None
        except Exception as e:
            logger.debug(f"[Telegram通知] 加载代理配置失败: {e}")
            self._proxies = None

    def _save_config(self):
        """保存配置"""
        try:
            with open(TELEGRAM_NOTIFY_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[Telegram通知] 保存配置失败: {e}")

    def update_config(self, new_config: dict):
        """更新配置"""
        merged_config = dict(self.config)
        if isinstance(new_config, dict):
            merged_config.update(new_config)
        self.config = self._normalize_config(merged_config)
        self._save_config()
        # 重新加载代理
        self._load_proxies()
        if self.should_bot_poll():
            self.start_polling()
        else:
            self.stop_polling()
        if self.should_account_monitor():
            self.start_monitor()
        else:
            self.stop_monitor()

    def get_config(self) -> dict:
        """获取配置"""
        data = dict(self.config)
        data["monitor_running"] = self.is_monitor_running()
        data["bot_polling"] = self.is_bot_polling()
        return data

    def should_bot_poll(self) -> bool:
        """是否需要启动 Telegram Bot 入站消息轮询。"""
        return bool(self.config.get("enabled") and self.config.get("bot_token"))

    def is_bot_polling(self) -> bool:
        return bool(self._polling and self._poll_thread and self._poll_thread.is_alive())

    def should_account_monitor(self) -> bool:
        """是否需要启动 Telegram 账号资源监听。"""
        return bool(
            self.config.get("account_monitor_enabled")
            and self.config.get("api_id")
            and self.config.get("api_hash")
            and self.config.get("phone")
            and self.config.get("selected_dialogs")
        )

    def should_poll(self) -> bool:
        """兼容旧调用：这里表示是否需要启动账号监听。"""
        return self.should_account_monitor()

    def is_monitor_running(self) -> bool:
        return bool(self._monitor_running and self._monitor_thread and self._monitor_thread.is_alive())

    def _require_telethon(self):
        try:
            from telethon import TelegramClient, events
            from telethon.errors import (
                SessionPasswordNeededError,
                PhoneCodeInvalidError,
                PhoneCodeExpiredError,
                PasswordHashInvalidError,
            )
            return {
                "TelegramClient": TelegramClient,
                "events": events,
                "SessionPasswordNeededError": SessionPasswordNeededError,
                "PhoneCodeInvalidError": PhoneCodeInvalidError,
                "PhoneCodeExpiredError": PhoneCodeExpiredError,
                "PasswordHashInvalidError": PasswordHashInvalidError,
            }
        except ImportError as e:
            raise RuntimeError("缺少 Telethon 依赖，请先执行 pip install -r requirements.txt") from e

    def _parse_api_id(self) -> int:
        api_id = str(self.config.get("api_id", "") or "").strip()
        if not api_id.isdigit():
            raise ValueError("api_id 必须是数字")
        return int(api_id)

    def _build_telethon_proxy(self):
        proxy_url = ""
        try:
            import config_manager
            proxy_url = str(config_manager.APP_CONFIG.get("network_http_proxy", "") or "").strip()
        except Exception:
            proxy_url = ""
        if not proxy_url:
            return None

        parsed = urlparse(proxy_url)
        if not parsed.scheme or not parsed.hostname:
            return None
        try:
            import socks
        except ImportError:
            logger.warning("[Telegram账号] 已配置代理，但缺少 PySocks 依赖")
            return None

        scheme = parsed.scheme.lower()
        if scheme in {"socks5", "socks5h"}:
            proxy_type = socks.SOCKS5
        elif scheme in {"socks4", "socks4a"}:
            proxy_type = socks.SOCKS4
        elif scheme in {"http", "https"}:
            proxy_type = socks.HTTP
        else:
            return None

        default_port = 1080 if scheme.startswith("socks") else 8080
        username = unquote(parsed.username) if parsed.username else None
        password = unquote(parsed.password) if parsed.password else None
        return (proxy_type, parsed.hostname, parsed.port or default_port, True, username, password)

    def _create_account_client(self):
        telethon = self._require_telethon()
        api_hash = str(self.config.get("api_hash", "") or "").strip()
        if not api_hash:
            raise ValueError("api_hash 不能为空")
        os.makedirs(os.path.dirname(TELEGRAM_SESSION_NAME), exist_ok=True)
        return telethon["TelegramClient"](
            TELEGRAM_SESSION_NAME,
            self._parse_api_id(),
            api_hash,
            proxy=self._build_telethon_proxy(),
        )

    async def _acquire_account_session_lock(self):
        await self._account_session_lock.acquire()

    async def _disconnect_account_client(self, client):
        try:
            await client.disconnect()
        except Exception as e:
            if "database is locked" in str(e).lower():
                return
            logger.warning(f"[Telegram账号] 断开客户端失败: {e}")

    async def _account_status_payload(self) -> dict:
        status = {
            "authorized": False,
            "monitor_running": self.is_monitor_running(),
            "bot_polling": self.is_bot_polling(),
            "user": None,
            "message": "未登录",
        }
        if not self.config.get("api_id") or not self.config.get("api_hash"):
            status["message"] = "未配置 api_id/api_hash"
            return status
        if self._monitor_thread and self._monitor_thread.is_alive():
            status["authorized"] = True
            status["monitor_running"] = self.is_monitor_running()
            status["bot_polling"] = self.is_bot_polling()
            status["user"] = self._account_user_cache
            status["message"] = "已登录"
            return status

        await self._acquire_account_session_lock()
        client = self._create_account_client()
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
            status["authorized"] = bool(authorized)
            if authorized:
                me = await client.get_me()
                self._account_user_cache = self._format_user(me)
                status["user"] = self._account_user_cache
                status["message"] = "已登录"
        finally:
            await self._disconnect_account_client(client)
            self._account_session_lock.release()
        return status

    def get_account_status(self) -> dict:
        try:
            return asyncio.run(self._account_status_payload())
        except Exception as e:
            logger.error(f"[Telegram账号] 获取登录状态失败: {e}")
            return {
                "authorized": False,
                "monitor_running": self.is_monitor_running(),
                "bot_polling": self.is_bot_polling(),
                "user": None,
                "message": str(e),
            }

    def _format_user(self, user) -> dict:
        if not user:
            return {}
        name_parts = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""]
        return {
            "id": str(getattr(user, "id", "") or ""),
            "name": " ".join(part for part in name_parts if part).strip() or str(getattr(user, "id", "") or ""),
            "username": getattr(user, "username", "") or "",
            "phone": getattr(user, "phone", "") or "",
        }

    async def send_login_code_async(self, api_id: str, api_hash: str, phone: str) -> dict:
        self.stop_monitor()
        merged = dict(self.config)
        merged.update({"api_id": str(api_id or "").strip(), "api_hash": str(api_hash or "").strip(), "phone": str(phone or "").strip()})
        self.config = self._normalize_config(merged)
        self._save_config()

        if not self.config.get("phone"):
            return {"status": "error", "message": "手机号不能为空"}

        await self._acquire_account_session_lock()
        client = self._create_account_client()
        try:
            await client.connect()
            sent = await client.send_code_request(self.config["phone"])
            self._login_phone = self.config["phone"]
            self._login_phone_code_hash = getattr(sent, "phone_code_hash", "") or ""
            return {"status": "ok", "message": "验证码已发送，请在 Telegram 中查看"}
        except Exception as e:
            logger.error(f"[Telegram账号] 发送验证码失败: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            await self._disconnect_account_client(client)
            self._account_session_lock.release()

    async def sign_in_async(self, code: str, password: str = "") -> dict:
        code = str(code or "").strip()
        password = str(password or "")
        if not code and not password:
            return {"status": "error", "message": "请输入验证码"}

        telethon = self._require_telethon()
        await self._acquire_account_session_lock()
        client = self._create_account_client()
        try:
            await client.connect()
            if not await client.is_user_authorized():
                try:
                    if password and not code:
                        await client.sign_in(password=password)
                    else:
                        await client.sign_in(
                            phone=self._login_phone or self.config.get("phone"),
                            code=code,
                            phone_code_hash=self._login_phone_code_hash or None,
                        )
                except telethon["SessionPasswordNeededError"]:
                    if not password:
                        return {"status": "need_password", "message": "账号已开启两步验证，请输入密码"}
                    await client.sign_in(password=password)

            me = await client.get_me()
            self._account_user_cache = self._format_user(me)
            if self.should_account_monitor():
                self.start_monitor()
            return {"status": "ok", "message": "Telegram 登录成功", "user": self._account_user_cache}
        except telethon["PhoneCodeInvalidError"]:
            return {"status": "error", "message": "验证码错误"}
        except telethon["PhoneCodeExpiredError"]:
            return {"status": "error", "message": "验证码已过期，请重新发送"}
        except telethon["PasswordHashInvalidError"]:
            return {"status": "error", "message": "两步验证密码错误"}
        except Exception as e:
            logger.error(f"[Telegram账号] 登录失败: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            await self._disconnect_account_client(client)
            self._account_session_lock.release()

    async def logout_async(self) -> dict:
        self.stop_monitor()
        if not self.config.get("api_id") or not self.config.get("api_hash"):
            self._remove_session_files()
            return {"status": "ok", "message": "已清除本地 session"}

        await self._acquire_account_session_lock()
        try:
            client = self._create_account_client()
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
        except Exception as e:
            logger.warning(f"[Telegram账号] 登出时清理远端 session 失败: {e}")
        finally:
            if "client" in locals():
                await self._disconnect_account_client(client)
            self._account_session_lock.release()
        self._remove_session_files()
        self._account_user_cache = None
        return {"status": "ok", "message": "已退出 Telegram 登录"}

    def _remove_session_files(self):
        for suffix in (".session", ".session-journal"):
            path = TELEGRAM_SESSION_NAME + suffix
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.warning(f"[Telegram账号] 删除 session 文件失败: {path}: {e}")

    def _avatar_filename(self, dialog_id: str) -> str:
        safe_id = "".join(ch for ch in str(dialog_id or "") if ch.isalnum() or ch in {"-", "_"})
        return f"{safe_id or 'unknown'}.jpg"

    def avatar_path(self, filename: str) -> str | None:
        safe_name = os.path.basename(str(filename or ""))
        if not safe_name.endswith(".jpg"):
            return None
        path = os.path.join(TELEGRAM_AVATAR_DIR, safe_name)
        if not os.path.exists(path):
            return None
        return path

    def _dialog_avatar_url(self, entity, dialog_id: str) -> str:
        filename = self._avatar_filename(dialog_id)
        path = os.path.join(TELEGRAM_AVATAR_DIR, filename)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return f"/api/telegram-notify/avatar/{filename}"
        return ""

    async def avatar_path_async(self, filename: str) -> str | None:
        # 头像请求来自浏览器图片加载，不能为每张图片打开 Telethon session。
        # 否则会和账号监听共用的 SQLite session 文件抢锁，导致消息监听延迟或漏处理。
        return self.avatar_path(filename)

    async def _download_dialog_avatar(self, client, entity, dialog_id: str) -> str | None:
        if not getattr(entity, "photo", None):
            return None
        os.makedirs(TELEGRAM_AVATAR_DIR, exist_ok=True)
        filename = self._avatar_filename(dialog_id)
        path = os.path.join(TELEGRAM_AVATAR_DIR, filename)
        try:
            downloaded = await client.download_profile_photo(entity, file=path)
            if downloaded and os.path.exists(path) and os.path.getsize(path) > 0:
                return path
            if os.path.exists(path) and os.path.getsize(path) == 0:
                os.remove(path)
        except Exception as e:
            logger.debug(f"[Telegram账号] 下载会话头像失败 dialog={dialog_id}: {e}")
        return None

    async def _list_dialogs_with_client(self, client) -> dict:
        dialogs = []
        selected_ids = self._selected_dialog_ids()
        if not await client.is_user_authorized():
            return {"status": "unauthorized", "message": "请先完成 Telegram 登录", "dialogs": []}

        async for dialog in client.iter_dialogs():
            if not (dialog.is_group or dialog.is_channel):
                continue
            entity = dialog.entity
            dialog_type = "群组" if dialog.is_group else "频道"
            dialog_id = str(dialog.id)
            avatar_url = self._dialog_avatar_url(entity, dialog_id)
            dialogs.append({
                "id": dialog_id,
                "title": dialog.name or "",
                "type": dialog_type,
                "username": getattr(entity, "username", "") or "",
                "avatar_url": avatar_url,
                "selected": dialog_id in selected_ids,
            })
        return {"status": "ok", "dialogs": dialogs}

    async def _list_dialogs_from_monitor_client(self) -> dict | None:
        loop = self._monitor_loop
        client = self._monitor_client
        if not (self.is_monitor_running() and loop and client and loop.is_running()):
            return None
        future = asyncio.run_coroutine_threadsafe(self._list_dialogs_with_client(client), loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=20)
        except Exception as e:
            future.cancel()
            logger.warning(f"[Telegram账号] 通过监听客户端读取列表失败: {e}")
            return {"status": "error", "message": "监听运行中，读取列表失败，请稍后重试", "dialogs": []}

    async def list_dialogs_async(self) -> dict:
        monitor_result = await self._list_dialogs_from_monitor_client()
        if monitor_result is not None:
            return monitor_result

        await self._acquire_account_session_lock()
        client = self._create_account_client()
        try:
            await client.connect()
            return await self._list_dialogs_with_client(client)
        finally:
            await self._disconnect_account_client(client)
            self._account_session_lock.release()

    def update_selected_dialogs(self, selected_dialogs: list[dict]) -> dict:
        self.config["selected_dialogs"] = self._normalize_selected_dialogs(selected_dialogs)
        self._save_config()
        if self.should_account_monitor():
            self.start_monitor()
        else:
            self.stop_monitor()
        return {"status": "ok", "selected_dialogs": self.config["selected_dialogs"]}

    def _selected_dialog_ids(self) -> set[str]:
        return {str(item.get("id", "") or "").strip() for item in self.config.get("selected_dialogs", []) if str(item.get("id", "") or "").strip()}

    def _selected_dialog_label(self, dialog_id: str) -> str:
        dialog_id = str(dialog_id or "").strip()
        for item in self.config.get("selected_dialogs", []):
            if str(item.get("id", "") or "").strip() != dialog_id:
                continue
            return str(item.get("title") or item.get("username") or dialog_id).strip()
        return dialog_id

    def _bot_chat_label(self, chat: dict) -> str:
        if not isinstance(chat, dict):
            return ""
        title = str(chat.get("title") or "").strip()
        username = str(chat.get("username") or "").strip()
        first_name = str(chat.get("first_name") or "").strip()
        last_name = str(chat.get("last_name") or "").strip()
        name = title or username or " ".join(part for part in [first_name, last_name] if part).strip()
        return name or str(chat.get("id", "") or "").strip()

    async def _event_chat_label(self, event, event_chat_id: str) -> str:
        label = self._selected_dialog_label(event_chat_id)
        if label and label != str(event_chat_id):
            return label
        try:
            chat = getattr(event, "chat", None) or await event.get_chat()
            return str(
                getattr(chat, "title", "")
                or getattr(chat, "username", "")
                or getattr(chat, "first_name", "")
                or event_chat_id
            ).strip()
        except Exception:
            return str(event_chat_id or "").strip()

    def _send_request(self, method: str, payload: dict) -> bool:
        """发送请求到 Telegram API"""
        bot_token = self.config.get("bot_token")
        if not bot_token:
            logger.error("[Telegram通知] 未配置 Bot Token")
            return False

        url = f"https://api.telegram.org/bot{bot_token}/{method}"

        try:
            resp = requests.post(
                url,
                json=payload,
                proxies=self._proxies,
                timeout=30
            )
            data = resp.json()
            if data.get("ok"):
                return True
            else:
                logger.error(f"[Telegram通知] API 调用失败: {data}")
                return False
        except Exception as e:
            logger.error(f"[Telegram通知] 请求异常: {e}")
            return False

    def send_message(self, text: str, chat_id: str = None,
                     parse_mode: str = "Markdown") -> bool:
        """
        发送文本消息

        Args:
            text: 消息内容（支持 Markdown）
            chat_id: 聊天 ID，不传则使用配置中的默认值

        Returns:
            bool: 是否发送成功
        """
        if not self.config.get("enabled"):
            logger.warning("[Telegram通知] 通知未启用")
            return False

        target_chat_id = chat_id or self.config.get("chat_id")
        if not target_chat_id:
            logger.error("[Telegram通知] 未配置 Chat ID")
            return False

        payload = {
            "chat_id": target_chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        if self._send_request("sendMessage", payload):
            logger.debug("[Telegram通知] 消息发送成功")
            return True
        if parse_mode:
            payload.pop("parse_mode", None)
            if self._send_request("sendMessage", payload):
                logger.debug("[Telegram通知] 消息发送成功（纯文本重试）")
                return True
        return False

    def send_photo(self, photo_url: str, caption: str = "",
                   chat_id: str = None, parse_mode: str = "Markdown") -> bool:
        """
        发送图片消息

        Args:
            photo_url: 图片 URL
            caption: 图片说明（支持 Markdown）
            chat_id: 聊天 ID
            parse_mode: 解析模式

        Returns:
            bool: 是否发送成功
        """
        if not self.config.get("enabled"):
            logger.warning("[Telegram通知] 通知未启用")
            return False

        target_chat_id = chat_id or self.config.get("chat_id")
        if not target_chat_id:
            logger.error("[Telegram通知] 未配置 Chat ID")
            return False

        payload = {
            "chat_id": target_chat_id,
            "photo": photo_url,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        if caption:
            payload["caption"] = caption

        if self._send_request("sendPhoto", payload):
            logger.debug("[Telegram通知] 图片发送成功")
            return True
        if parse_mode:
            payload.pop("parse_mode", None)
            if self._send_request("sendPhoto", payload):
                logger.debug("[Telegram通知] 图片发送成功（纯文本重试）")
                return True
        return False

    def send_message_with_image(self, title: str, description: str,
                                 image_url: str = "", chat_id: str = None) -> bool:
        """
        发送带图片的消息（图文卡片样式）

        Args:
            title: 标题
            description: 描述
            image_url: 图片 URL
            chat_id: 聊天 ID

        Returns:
            bool: 是否发送成功
        """
        # 组合消息内容
        if title and description:
            caption = f"*{title}*\n\n{description}"
        elif title:
            caption = f"*{title}*"
        else:
            caption = description

        # 如果有图片，发送图片消息
        if image_url:
            return self.send_photo(image_url, caption, chat_id)
        else:
            # 否则发送纯文本消息
            return self.send_message(caption, chat_id)

    def test_connection(self) -> dict:
        """测试连接"""
        result = {"success": False, "message": ""}

        if not self.config.get("bot_token"):
            result["message"] = "缺少 Bot Token"
            return result

        if not self.config.get("chat_id"):
            result["message"] = "缺少 Chat ID"
            return result

        # 尝试获取 bot 信息
        bot_token = self.config.get("bot_token")
        url = f"https://api.telegram.org/bot{bot_token}/getMe"

        try:
            resp = requests.get(url, proxies=self._proxies, timeout=10)
            data = resp.json()
            if data.get("ok"):
                bot_info = data.get("result", {})
                bot_name = bot_info.get("first_name", "Unknown")
                bot_username = bot_info.get("username", "")

                # 尝试发送测试消息
                test_result = self.send_message(
                    f"🔔 测试消息\n\n来自 ChillPoster 的连接测试\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

                if test_result:
                    result["success"] = True
                    proxy_info = " (使用代理)" if self._proxies else ""
                    result["message"] = f"连接成功！Bot: {bot_name} (@{bot_username}){proxy_info}"
                else:
                    result["message"] = "Bot 验证成功，但发送消息失败，请检查 Chat ID"
            else:
                result["message"] = f"Bot Token 无效: {data.get('description', '未知错误')}"
        except Exception as e:
            error_msg = str(e)
            if "Network is unreachable" in error_msg or "Failed to establish" in error_msg:
                result["message"] = "网络无法访问 Telegram，请在「基本配置」中设置代理"
            else:
                result["message"] = f"连接失败: {error_msg}"

        return result

    def is_notify_type_enabled(self, notify_type: str) -> bool:
        """检查特定类型的通知是否启用"""
        if not (self.config.get("enabled") and self.config.get("bot_token") and self.config.get("chat_id")):
            return False
        notify_types = self.config.get("notify_types", {})
        return notify_types.get(notify_type, False)

    def _get_media_backdrop_url(self, media_name: str, media_type: str = "movie",
                                 year: str = "", tmdb_id: str = "") -> str:
        """获取媒体背景图URL（委托共享模块，带缓存）"""
        from app.services.tmdb_poster import get_media_backdrop_url
        return get_media_backdrop_url(media_name, media_type, year, tmdb_id)

    def notify_playback(self, item_name: str, emby_name: str = "Emby",
                        user_agent: str = "", poster_url: str = "",
                        original_name: str = "", media_type: str = "movie",
                        overview: str = "", rating: str = "", genres: str = "",
                        tagline: str = "", user_name: str = "",
                        client_info: str = "", year: str = "", tmdb_id: str = "", **kwargs) -> bool:
        """
        发送播放通知
        """
        if not self.is_notify_type_enabled("playback"):
            return False

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 用模板渲染标题和正文
        templates = self.config.get("templates", {}).get("playback", {})
        title_tpl = templates.get("title", "🎬 正在播放《{{ title }}》")
        text_tpl = templates.get("text", "")

        context = {
            "title": item_name,
            "media_type": "电影" if media_type == "movie" else "剧集",
            "rating": rating or "",
            "genres": genres or "",
            "overview": overview or "",
            "tagline": tagline or "",
            "emby_name": emby_name,
            "client_info": client_info or "未知客户端",
            "user_name": user_name or "未知用户",
            "now": now,
            "poster_url": poster_url or "",
            "year": year or "",
            "original_name": original_name or "",
        }

        title = render_template(title_tpl, context)
        description = render_template(text_tpl, context)

        # 背景图优先用传入的，如果为空或是内网地址则从 TMDB 获取
        if not poster_url or poster_url.startswith("http://"):
            search_name = original_name or item_name.replace("🎬 ", "").replace("📺 ", "")
            import re
            search_name = re.sub(r'\s*\(\d{4}\)\s*$', '', search_name).strip()
            poster_url = self._get_media_backdrop_url(search_name, media_type, year=year, tmdb_id=tmdb_id)

        return self.send_message_with_image(title, description, poster_url)

    def notify_media_added(self, media_name: str, media_type: str = "movie",
                           library_name: str = "", year: str = "",
                           poster_url: str = "", tmdb_id: str = "",
                           original_name: str = "",
                           overview: str = "", rating: str = "", genres: str = "",
                           tagline: str = "", server_name: str = "",
                           original_title: str = "", tmdb_url: str = "",
                           premiere_date: str = "", status: str = "",
                           item_count: str = "", **kwargs) -> bool:
        """
        发送新媒体入库通知
        """
        if not self.is_notify_type_enabled("media_added"):
            return False

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 用模板渲染标题和正文
        templates = self.config.get("templates", {}).get("media_added", {})
        title_tpl = templates.get("title", "《{{ title }}》{% if year %}({{ year }}){% endif %} 已入库 ✅")
        text_tpl = templates.get("text", "")

        context = {
            "title": media_name,
            "year": str(year) if year else "",
            "media_type": "电影" if media_type == "movie" else "剧集",
            "library_name": library_name,
            "rating": rating or "",
            "genres": genres or "",
            "overview": overview or "",
            "tagline": tagline or "",
            "poster_url": poster_url or "",
            "now": now,
            "server_name": server_name or "",
            "original_title": original_title or "",
            "tmdb_url": tmdb_url or "",
            "premiere_date": premiere_date or "",
            "status": status or "",
            "item_count": item_count or "",
        }

        title = render_template(title_tpl, context)
        description = render_template(text_tpl, context)

        # Telegram 无法访问内网 Emby 地址，使用 TMDB 背景图
        search_name = original_name or media_name.split(" S")[0]
        tmdb_poster = self._get_media_backdrop_url(search_name, media_type, year, tmdb_id)

        return self.send_message_with_image(title, description, tmdb_poster)

    def notify_organize_complete(self, media_name: str, media_type: str = "tv",
                                 year: str = "", season_episode: str = "",
                                 rating: str = "", genres: str = "",
                                 overview: str = "", tmdb_id: str = "",
                                 quality: str = "", audio: str = "",
                                 episode_count: str = "", episode_ranges: str = "", file_size: str = "",
                                 release_group: str = "", elapsed: str = "",
                                 original_name: str = "", **kwargs) -> bool:
        if not self.is_notify_type_enabled("organize_complete"):
            return False

        from app.services.notification_formatter import render_template
        templates = self.config.get("templates", {}).get("organize_complete", {})
        title_tpl = templates.get("title", "整理完成 ✅ 《{{ title }}》{% if year %}({{ year }}){% endif %}{% if season_episode %} {{ season_episode }}{% endif %}")
        text_tpl = templates.get("text", "")

        context = {
            "title": media_name,
            "year": str(year) if year else "",
            "media_type": "电影" if media_type == "movie" else "剧集",
            "season_episode": season_episode or "",
            "rating": rating or "",
            "genres": genres or "",
            "overview": overview or "",
            "tmdb_id": tmdb_id or "",
            "quality": quality or "",
            "audio": audio or "",
            "episode_count": episode_count or "",
            "episode_ranges": episode_ranges or kwargs.get("episode_ranges") or "",
            "file_size": file_size or "",
            "release_group": release_group or "",
            "elapsed": elapsed or "",
            "now": kwargs.get("now") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        title = render_template(title_tpl, context)
        description = render_template(text_tpl, context)

        search_name = original_name or media_name.split(" S")[0]
        tmdb_poster = self._get_media_backdrop_url(search_name, media_type, year, tmdb_id)

        return self.send_message_with_image(title, description, tmdb_poster)

    def notify_checkin(self, account_name: str, points: int, total_points: int,
                       status: str = "success", message: str = "",
                       checkin_count: int = 0, checkin_points: int = 0) -> bool:
        """
        发送签到通知

        Args:
            account_name: 账号名称
            points: 本次获得积分
            total_points: 总积分
            status: 签到状态 (success/already/failed)
            message: 附加消息
            checkin_count: 本机累计签到次数
            checkin_points: 本机累计签到积分

        Returns:
            bool: 是否发送成功
        """
        if not self.is_notify_type_enabled("checkin"):
            return False

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if status == "success":
            status_emoji = "✅"
            status_text = "签到成功"
            points_text = f"+{points} 积分"
        elif status == "already":
            status_emoji = "🔄"
            status_text = "今日已签到"
            points_text = "-"
        else:
            status_emoji = "❌"
            status_text = "签到失败"
            points_text = "-"

        title = f"{status_emoji} 影巢签到"
        description = f"""账号：{account_name}
状态：{status_text}
获得：{points_text}
总积分：{total_points}"""
        if checkin_count > 0 or checkin_points > 0:
            description += f"\n本机签到：{checkin_count} 次，累计积分 {checkin_points}"
        if message:
            description += f"\n备注：{message}"
        description += f"\n时间：{now}"
        description += "\n\n— ChillPoster"

        return self.send_message_with_image(title, description)

    def notify_task_complete(self, task_name: str, status: str = "success",
                             detail: str = "", posters_count: int = 0,
                             poster_url: str = "", elapsed: str = "",
                             scanned: int = 0, scanned_dirs: int = 0,
                             generated: int = 0, downloaded: int = 0,
                             download_failed: int = 0, skipped: int = 0,
                             deleted: int = 0, failed: int = 0,
                             retry_success: int = 0, retry_failed: int = 0,
                             task_category: str = "", total_count: int = 0,
                             success_count: int = 0, already_count: int = 0,
                             trigger: str = "", summary: str = "",
                             accounts_text: str = "") -> bool:
        """发送任务完成通知"""
        if not self.is_notify_type_enabled("task_complete"):
            return False

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if status == "success":
            status_emoji = "✅"
            status_text = "执行成功"
        elif status in ("stopped", "cancelled"):
            status_emoji = "⏹️"
            status_text = "已取消"
        else:
            status_emoji = "❌"
            status_text = "执行失败"

        templates = self.config.get("templates", {}).get("task_complete", {})
        title_tpl = templates.get("title", "{{ status_emoji }} {{ task_name }}")
        text_tpl = templates.get("text", "状态：{{ status_text }}\n时间：{{ now }}")
        context = {
            "task_name": task_name,
            "status": status,
            "status_emoji": status_emoji,
            "status_text": status_text,
            "detail": detail or "",
            "posters_count": posters_count or 0,
            "elapsed": elapsed or "",
            "scanned": int(scanned or 0),
            "scanned_dirs": int(scanned_dirs or 0),
            "generated": int(generated or 0),
            "downloaded": int(downloaded or 0),
            "download_failed": int(download_failed or 0),
            "skipped": int(skipped or 0),
            "deleted": int(deleted or 0),
            "failed": int(failed or 0),
            "retry_success": int(retry_success or 0),
            "retry_failed": int(retry_failed or 0),
            "task_category": task_category or "",
            "total_count": int(total_count or 0),
            "success_count": int(success_count or 0),
            "already_count": int(already_count or 0),
            "trigger": trigger or "",
            "summary": summary or "",
            "accounts_text": accounts_text or "",
            "now": now,
        }

        title = render_template(title_tpl, context)
        description = render_template(text_tpl, context)

        return self.send_message_with_image(title, description, poster_url)

    # ==========================================
    # Telegram 账号监听（MTProto / Telethon）
    # ==========================================

    def start_monitor(self):
        """启动 Telegram 用户账号消息监听。"""
        if not self.should_account_monitor():
            logger.debug("[Telegram账号] 未启用、未登录配置或未选择监听目标，跳过启动")
            return
        with self._lock:
            if self.is_monitor_running():
                return
            self._monitor_stop_event = threading.Event()
            self._monitor_thread = threading.Thread(
                target=self._monitor_thread_main,
                daemon=True,
                name="telegram-account-monitor",
            )
            self._monitor_thread.start()
        logger.info("[Telegram账号] 监听线程已启动")

    def stop_monitor(self):
        """停止 Telegram 用户账号消息监听。"""
        self._monitor_stop_event.set()
        loop = self._monitor_loop
        client = self._monitor_client
        if loop and client and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
            except Exception:
                pass
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5)
        self._monitor_running = False

    def start_polling(self):
        """启动 Telegram Bot long polling，用于接收转发给机器人的资源链接。"""
        if not self.should_bot_poll():
            logger.debug("[Telegram通知] 未启用或未配置 token，跳过 polling 启动")
            return
        offset = self._initial_bot_update_offset()
        with self._lock:
            if self.is_bot_polling():
                return
            self._polling = True
            self._offset = offset
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                daemon=True,
                name="telegram-bot-polling",
            )
            self._poll_thread.start()
        logger.info("[Telegram通知] Long polling 已启动")

    def stop_polling(self):
        """停止 Telegram Bot long polling。"""
        self._polling = False
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=3)
        if self._poll_thread:
            logger.info("[Telegram通知] Long polling 已停止")
        self._poll_thread = None

    def _api_request(self, method: str, payload: dict = None, use_get: bool = False) -> dict:
        """调用 Telegram Bot API。"""
        bot_token = self.config.get("bot_token", "")
        if not bot_token:
            return {}
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        timeout = 35 if method == "getUpdates" else 30
        try:
            if use_get or method == "getUpdates":
                resp = requests.get(url, params=payload or {}, proxies=self._proxies, timeout=timeout)
            else:
                resp = requests.post(url, json=payload or {}, proxies=self._proxies, timeout=timeout)
            return resp.json()
        except Exception as e:
            if self._polling or method != "getUpdates":
                logger.error(f"[Telegram通知] API 请求异常 ({method}): {e}")
            return {}

    def _persist_bot_update_offset(self, offset: int):
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            return
        if offset <= int(self.config.get("bot_update_offset") or 0):
            return
        self.config["bot_update_offset"] = offset
        self._save_config()

    def _initial_bot_update_offset(self) -> int:
        offset = int(self.config.get("bot_update_offset") or 0)
        if offset > 0:
            return offset

        # First start after upgrading old configs: skip stale updates Telegram kept
        # while the bot listener was offline, avoiding surprise duplicate transfers.
        resp = self._api_request("getUpdates", {
            "offset": -1,
            "limit": 1,
            "timeout": 0,
            "allowed_updates": ["message"],
        }, use_get=True)
        if not resp.get("ok"):
            return 0
        result = resp.get("result") or []
        if not result:
            return 0
        try:
            offset = int(result[-1].get("update_id", -1)) + 1
        except (TypeError, ValueError):
            return 0
        if offset > 0:
            self._persist_bot_update_offset(offset)
            logger.info(f"[Telegram通知] 已跳过历史 updates，从 offset={offset} 开始")
        return offset

    def _poll_loop(self):
        """Telegram Bot long polling 循环。"""
        logger.info("[Telegram通知] Polling 循环已开始")
        while self._polling:
            try:
                resp = self._api_request("getUpdates", {
                    "offset": self._offset,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                })
                if not resp.get("ok"):
                    if self._polling:
                        logger.warning(f"[Telegram通知] getUpdates 失败: {resp}")
                    import time
                    time.sleep(5)
                    continue

                for update in resp.get("result", []) or []:
                    self._offset = int(update.get("update_id", self._offset)) + 1
                    self._persist_bot_update_offset(self._offset)
                    self._handle_update(update)
            except Exception as e:
                if self._polling:
                    logger.error(f"[Telegram通知] Polling 异常: {e}", exc_info=True)
                    import time
                    time.sleep(5)
        logger.info("[Telegram通知] Polling 循环已退出")

    def _extract_message_links(self, msg: dict) -> list[str]:
        from app.services.transfer_service import transfer_service

        links: list[str] = []
        for field in ("text", "caption"):
            value = str(msg.get(field, "") or "")
            if value:
                links.extend(transfer_service.extract_links(value))

        for field in ("entities", "caption_entities"):
            for entity in msg.get(field, []) or []:
                url = str(entity.get("url", "") or "").strip()
                if url:
                    links.extend(transfer_service.extract_links(url))

        reply_markup = msg.get("reply_markup") or {}
        for row in reply_markup.get("inline_keyboard", []) or []:
            for button in row or []:
                if not isinstance(button, dict):
                    continue
                url = str(button.get("url", "") or "").strip()
                if url:
                    links.extend(transfer_service.extract_links(url))
                login_url = button.get("login_url") if isinstance(button.get("login_url"), dict) else {}
                url = str(login_url.get("url", "") or "").strip()
                if url:
                    links.extend(transfer_service.extract_links(url))

        return transfer_service._dedupe_links(links)

    def _handle_update(self, update: dict):
        """处理单条 Telegram Bot update。"""
        msg = update.get("message") or {}
        if not isinstance(msg, dict):
            return

        links = self._extract_message_links(msg)
        if not links:
            return

        chat_id = str((msg.get("chat") or {}).get("id", "") or "")
        chat_label = self._bot_chat_label(msg.get("chat") or {})
        logger.info(f"[Telegram通知] 收到 {len(links)} 条资源链接 (chat={chat_id})")

        from app.services.transfer_service import transfer_service

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(
                transfer_service.process_links(
                    links,
                    source="telegram_bot",
                    target_dir=self._monitor_transfer_dir(),
                    source_meta={
                        "source_key": "telegram_bot",
                        "source_kind": "telegram_bot",
                        "source_label": "转存机器人",
                        "source_detail": chat_label,
                        "source_id": chat_id,
                    },
                )
            )
            for result in results:
                reply = result.get("message", "转存完成")
                if chat_id:
                    self.send_message(reply, chat_id=chat_id, parse_mode="")
                try:
                    from app.routers.wechat_notify import send_to_all_channels
                    send_to_all_channels(
                        title=result.get("status", "转存"),
                        description=result.get("message", ""),
                        notify_type="resource_transfer",
                        exclude_channels={"telegram"},
                    )
                except Exception as e:
                    logger.error(f"[Telegram通知] 发送转存通知失败: {e}")
        except Exception as e:
            logger.error(f"[Telegram通知] 处理资源消息失败: {e}", exc_info=True)
        finally:
            loop.close()

    def _monitor_thread_main(self):
        loop = asyncio.new_event_loop()
        self._monitor_loop = loop
        try:
            loop.run_until_complete(self._monitor_runner())
        finally:
            self._monitor_running = False
            self._monitor_client = None
            self._monitor_loop = None
            loop.close()

    async def _monitor_runner(self):
        telethon = self._require_telethon()
        client = self._create_account_client()
        self._monitor_client = client
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning("[Telegram账号] session 未登录，监听未启动")
                return
            self._account_user_cache = self._format_user(await client.get_me())

            async def _handler(event):
                await self._handle_telethon_event(event)

            client.add_event_handler(_handler, telethon["events"].NewMessage)
            self._monitor_running = True
            selected_count = len(self._selected_dialog_ids())
            logger.info(f"[Telegram账号] 消息监听已运行，目标数: {selected_count}")

            while not self._monitor_stop_event.is_set() and client.is_connected():
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"[Telegram账号] 监听异常: {e}", exc_info=True)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            logger.info("[Telegram账号] 消息监听已停止")

    def _extract_telethon_links(self, message) -> list[str]:
        from app.services.transfer_service import transfer_service

        links: list[str] = []
        raw_text = str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")
        if raw_text:
            links.extend(transfer_service.extract_links(raw_text))

        for entity in getattr(message, "entities", None) or []:
            url = str(getattr(entity, "url", "") or "").strip()
            if url:
                links.extend(transfer_service.extract_links(url))

        for row in getattr(message, "buttons", None) or []:
            for button in row or []:
                url = str(getattr(button, "url", "") or "").strip()
                if url:
                    links.extend(transfer_service.extract_links(url))

        return transfer_service._dedupe_links(links)

    async def _handle_telethon_event(self, event):
        if getattr(event, "out", False):
            return

        selected_ids = self._selected_dialog_ids()
        event_chat_id = str(getattr(event, "chat_id", "") or "")
        if not selected_ids or event_chat_id not in selected_ids:
            return

        links = self._extract_telethon_links(event.message)
        if not links:
            return

        chat_label = await self._event_chat_label(event, event_chat_id)
        logger.info(f"[Telegram账号] 收到 {len(links)} 条资源链接 (chat={event_chat_id})")

        from app.services.transfer_service import transfer_service

        try:
            results = await transfer_service.process_links(
                links,
                source="telegram_monitor",
                target_dir=self._monitor_transfer_dir(),
                source_meta={
                    "source_key": "telegram_monitor",
                    "source_kind": "telegram_monitor",
                    "source_label": "Telegram 监听",
                    "source_detail": chat_label,
                    "source_id": event_chat_id,
                },
            )
            for result in results:
                try:
                    from app.routers.wechat_notify import send_to_all_channels
                    await asyncio.to_thread(
                        send_to_all_channels,
                        title=result.get("status", "转存"),
                        description=result.get("message", ""),
                        notify_type="resource_transfer",
                    )
                except Exception as e:
                    logger.error(f"[Telegram账号] 发送转存通知失败: {e}")
        except Exception as e:
            logger.error(f"[Telegram账号] 处理资源消息失败: {e}", exc_info=True)


# 全局实例
telegram_notify_service = TelegramNotifyService()
