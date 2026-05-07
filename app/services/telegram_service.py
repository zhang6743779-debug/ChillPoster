# app/services/telegram_service.py
import os
import json
import threading
import requests
from datetime import datetime
from typing import Optional, List, Dict, Any
from core.configs import WECHAT_NOTIFY_CONFIG_FILE
from core.logger import logger
from app.services.notification_formatter import render_template, merge_templates

# TMDB 图片基础 URL


# Telegram 配置文件路径
TELEGRAM_NOTIFY_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config", "telegram_notify.json"
)


class TelegramNotifyService:
    """Telegram 通知服务 - 支持图文消息"""

    def __init__(self):
        self.config = self._load_config()
        self._proxies = None
        self._load_proxies()

    def _load_config(self) -> dict:
        """加载配置"""
        config = None
        if os.path.exists(TELEGRAM_NOTIFY_CONFIG_FILE):
            try:
                with open(TELEGRAM_NOTIFY_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except Exception as e:
                logger.error(f"[Telegram通知] 加载配置失败: {e}")
        if config is None:
            config = {
                "enabled": False,
                "name": "Telegram",
                "bot_token": "",
                "chat_id": "",
                "notify_types": {
                    "playback": True,
                    "media_added": True,
                    "checkin": True,
                    "task_complete": True
                }
            }
        # 合并通知模板
        config["templates"] = merge_templates(config.get("templates"))
        return config

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
        # 合并通知模板，防止前端未传 templates 时丢失默认模板
        new_config["templates"] = merge_templates(new_config.get("templates"))
        self.config = new_config
        self._save_config()
        # 重新加载代理
        self._load_proxies()

    def get_config(self) -> dict:
        """获取配置"""
        return self.config

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
            "parse_mode": parse_mode
        }

        if self._send_request("sendMessage", payload):
            logger.debug("[Telegram通知] 消息发送成功")
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
            "parse_mode": parse_mode
        }

        if caption:
            payload["caption"] = caption

        if self._send_request("sendPhoto", payload):
            logger.debug("[Telegram通知] 图片发送成功")
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
        if not self.config.get("enabled"):
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
                                 episode_count: str = "", file_size: str = "",
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
    # Long Polling 接收消息（用于资源转存）
    # ==========================================

    def start_polling(self):
        """启动 Telegram long polling 后台线程"""
        if not self.config.get("enabled") or not self.config.get("bot_token"):
            logger.debug("[Telegram通知] 未启用或未配置 token，跳过 polling 启动")
            return
        if getattr(self, '_polling', False):
            return
        self._polling = True
        self._offset = 0
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="tg-poll")
        self._poll_thread.start()
        logger.info("[Telegram通知] Long polling 已启动")

    def stop_polling(self):
        """停止 long polling"""
        self._polling = False
        # 等待 polling 线程退出（最多等3秒）
        if getattr(self, '_poll_thread', None) and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=3)
        logger.info("[Telegram通知] Long polling 已停止")

    def _api_request(self, method: str, payload: dict = None, use_get: bool = False) -> dict:
        """调用 Telegram Bot API"""
        bot_token = self.config.get("bot_token", "")
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        # getUpdates 使用 GET 请求 + 长超时（客户端 timeout 要大于服务端 long polling timeout）
        timeout = 60 if method == "getUpdates" else 30
        try:
            if use_get or method == "getUpdates":
                resp = requests.get(url, params=payload or {}, proxies=self._proxies, timeout=timeout)
            else:
                resp = requests.post(url, json=payload or {}, proxies=self._proxies, timeout=timeout)
            return resp.json()
        except Exception as e:
            logger.error(f"[Telegram通知] API 请求异常 ({method}): {e}")
            return {}

    def _poll_loop(self):
        """Long polling 循环"""
        logger.info("[Telegram通知] Polling 循环已开始")
        while getattr(self, '_polling', False):
            try:
                resp = self._api_request("getUpdates", {
                    "offset": self._offset,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                })
                if not resp.get("ok"):
                    logger.warning(f"[Telegram通知] getUpdates 失败: {resp}")
                    import time
                    time.sleep(5)
                    continue

                for update in resp.get("result", []):
                    self._offset = update["update_id"] + 1
                    self._handle_update(update)

            except Exception as e:
                if self._polling:
                    logger.error(f"[Telegram通知] Polling 异常: {e}")
                    import time
                    time.sleep(5)

        logger.info("[Telegram通知] Polling 循环已退出")

    def _handle_update(self, update: dict):
        """处理单条 Telegram update"""
        msg = update.get("message", {})
        text = msg.get("text", "")
        if not text:
            return

        # 提取 115 链接
        from app.services.transfer_service import transfer_service
        links = transfer_service.extract_links(text)
        if not links:
            return

        chat_id = str(msg.get("chat", {}).get("id", ""))
        logger.info(f"[Telegram通知] 收到 {len(links)} 条 115 链接 (chat={chat_id})")

        # 同步处理转存（polling 在后台线程中）
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            for link in links:
                result = loop.run_until_complete(
                    transfer_service.process_link(link, source="telegram")
                )
                # 回复发消息者
                reply = result.get("message", "转存完成")
                self.send_message(reply, chat_id=chat_id)

                # 发送转存通知到所有启用的渠道
                try:
                    from app.routers.wechat_notify import send_to_all_channels
                    send_to_all_channels(
                        title=result.get("status", "转存"),
                        description=result.get("message", ""),
                        notify_type="resource_transfer",
                    )
                except Exception as e:
                    logger.error(f"[Telegram通知] 发送转存通知失败: {e}")
        finally:
            loop.close()


# 全局实例
telegram_notify_service = TelegramNotifyService()
