# app/services/wechat_service.py
import os
import json
import hashlib
import requests
from datetime import datetime
from typing import Optional, List, Dict, Any
from core.configs import WECHAT_NOTIFY_CONFIG_FILE
from core.logger import logger
from app.services.notification_formatter import render_template, merge_templates


# TMDB 图片基础 URL


# 通知类型定义
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
        "description": "签到完成时发送通知",
        "icon": "✅"
    },
    "task_complete": {
        "name": "任务通知",
        "description": "海报生成等任务完成时发送通知",
        "icon": "🎨"
    }
}

DEFAULT_NOTIFY_TYPES = {
    "playback": True,
    "media_added": True,
    "organize_complete": True,
    "wash_result": True,
    "resource_transfer": True,
    "checkin": True,
    "task_complete": True,
}


def _compact_log_text(value: str, limit: int = 80) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


class WechatNotifyService:
    """企业微信通知服务 - 支持图文消息"""

    def __init__(self):
        self.config = self._load_config()
        self._access_token = None
        self._token_expires = 0
        self._token_acquired_time = 0  # 记录token获取时间

    def _load_config(self) -> dict:
        """加载配置"""
        config = None
        if os.path.exists(WECHAT_NOTIFY_CONFIG_FILE):
            try:
                with open(WECHAT_NOTIFY_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except Exception as e:
                logger.error(f"[微信通知] 加载配置失败: {e}")
        if config is None:
            config = {
                "enabled": False,
                "name": "微信",
                "channel_name": "",
                "corp_id": "",
                "app_secret": "",
                "token": "",
                "agent_id": "",
                "proxy_url": "",
                "encoding_aes_key": "",
                "admin_whitelist": "",
                "notify_types": DEFAULT_NOTIFY_TYPES.copy()
            }
        config["notify_types"] = {
            **DEFAULT_NOTIFY_TYPES,
            **(config.get("notify_types") or {}),
        }
        # 合并通知模板
        config["templates"] = merge_templates(config.get("templates"))
        return config

    def _save_config(self):
        """保存配置"""
        try:
            with open(WECHAT_NOTIFY_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[微信通知] 保存配置失败: {e}")

    def update_config(self, new_config: dict):
        """更新配置"""
        new_config["notify_types"] = {
            **DEFAULT_NOTIFY_TYPES,
            **(new_config.get("notify_types") or {}),
        }
        # 合并通知模板，防止前端未传 templates 时丢失默认模板
        new_config["templates"] = merge_templates(new_config.get("templates"))
        self.config = new_config
        self._save_config()
        # 清除缓存的token
        self._access_token = None
        self._token_expires = 0

    def get_config(self) -> dict:
        """获取配置"""
        return self.config

    def _get_api_base_url(self) -> str:
        """获取 API 基础 URL（支持代理）"""
        proxy_url = self.config.get("proxy_url", "").strip()
        if proxy_url:
            # 2022年6月后创建的应用需要使用代理URL作为基础URL
            return proxy_url.rstrip("/")
        return "https://qyapi.weixin.qq.com"

    def _get_access_token(self, force: bool = False) -> Optional[str]:
        """获取企业微信 access_token"""
        import time

        if not self.config.get("corp_id") or not self.config.get("app_secret"):
            logger.error("[微信通知] 缺少 corp_id 或 app_secret")
            return None

        # 检查是否需要刷新token（提前5分钟刷新）
        current_time = time.time()
        if not force and self._access_token and current_time < self._token_acquired_time + self._token_expires - 300:
            return self._access_token

        base_url = self._get_api_base_url()
        url = f"{base_url}/cgi-bin/gettoken"
        params = {
            "corpid": self.config["corp_id"],
            "corpsecret": self.config["app_secret"]
        }

        try:
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            if data.get("errcode") == 0:
                self._access_token = data["access_token"]
                self._token_expires = data.get("expires_in", 7200)
                self._token_acquired_time = current_time
                logger.debug("[微信通知] access_token 获取成功")
                return self._access_token
            else:
                logger.error(f"[微信通知] 获取 access_token 失败: {data}")
                return None
        except Exception as e:
            logger.error(f"[微信通知] 获取 access_token 异常: {e}")
            return None

    def _send_request(self, url: str, payload: dict, _retried: bool = False) -> bool:
        """发送请求到企业微信API"""
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            data = resp.json()
            if data.get("errcode") == 0:
                return True
            elif data.get("errcode") == 42001 and not _retried:
                # Token过期，强制刷新后重试（仅重试一次）
                logger.warning("[微信通知] access_token 已过期，正在刷新...")
                if self._get_access_token(force=True):
                    base_url = self._get_api_base_url()
                    new_url = f"{base_url}/cgi-bin/message/send?access_token={self._access_token}"
                    return self._send_request(new_url, payload, _retried=True)
                return False
            else:
                logger.error(f"[微信通知] 消息发送失败: {data}")
                return False
        except Exception as e:
            logger.error(f"[微信通知] 消息发送异常: {e}")
            return False

    def send_message(self, content: str, to_user: str = "@all") -> bool:
        """
        发送文本消息

        Args:
            content: 消息内容
            to_user: 接收用户，默认 @all 发送给所有人

        Returns:
            bool: 是否发送成功
        """
        if not self.config.get("enabled"):
            logger.warning("[微信通知] 通知未启用")
            return False

        access_token = self._get_access_token()
        if not access_token:
            return False

        base_url = self._get_api_base_url()
        url = f"{base_url}/cgi-bin/message/send?access_token={access_token}"

        payload = {
            "touser": to_user,
            "msgtype": "text",
            "agentid": self.config.get("agent_id", ""),
            "text": {
                "content": content
            },
            "safe": 0,
            "enable_id_trans": 0,
            "enable_duplicate_check": 0
        }

        if self._send_request(url, payload):
            logger.debug("[微信通知] 文本消息发送成功")
            return True
        return False

    def send_markdown(self, content: str, to_user: str = "@all") -> bool:
        """
        发送 Markdown 消息

        Args:
            content: Markdown 内容
            to_user: 接收用户

        Returns:
            bool: 是否发送成功
        """
        if not self.config.get("enabled"):
            logger.warning("[微信通知] 通知未启用")
            return False

        access_token = self._get_access_token()
        if not access_token:
            return False

        base_url = self._get_api_base_url()
        url = f"{base_url}/cgi-bin/message/send?access_token={access_token}"

        payload = {
            "touser": to_user,
            "msgtype": "markdown",
            "agentid": self.config.get("agent_id", ""),
            "markdown": {
                "content": content
            }
        }

        if self._send_request(url, payload):
            logger.debug("[微信通知] Markdown 发送成功")
            return True
        return False

    def send_news_message(self, title: str, description: str, image_url: str = "",
                          link_url: str = "", to_user: str = "@all") -> bool:
        """
        发送图文消息（卡片样式）

        Args:
            title: 卡片标题
            description: 卡片描述
            image_url: 图片地址（海报/背景图）
            link_url: 点击跳转链接
            to_user: 接收用户

        Returns:
            bool: 是否发送成功
        """
        if not self.config.get("enabled"):
            logger.warning("[微信通知] 通知未启用")
            return False

        access_token = self._get_access_token()
        if not access_token:
            return False

        base_url = self._get_api_base_url()
        url = f"{base_url}/cgi-bin/message/send?access_token={access_token}"

        article = {
            "title": title,
            "description": description,
            "picurl": image_url,
            "url": link_url
        }

        payload = {
            "touser": to_user,
            "msgtype": "news",
            "agentid": self.config.get("agent_id", ""),
            "news": {
                "articles": [article]
            }
        }

        if self._send_request(url, payload):
            logger.debug(
                f"[微信通知] 图文发送成功: 接收={to_user or '@all'} | 标题={_compact_log_text(title, 60)} | "
                f"描述长度={len(str(description or ''))} | 图片={'有' if image_url else '无'} | 链接={'有' if link_url else '无'}"
            )
            return True
        return False

    def upload_temp_image(self, image_path: str) -> Optional[str]:
        """上传企业微信临时图片素材，返回 media_id。"""
        if not self.config.get("enabled"):
            logger.warning("[微信通知] 通知未启用")
            return None
        if not image_path or not os.path.exists(image_path):
            logger.warning("[微信通知] 图片素材不存在")
            return None

        access_token = self._get_access_token()
        if not access_token:
            return None

        base_url = self._get_api_base_url()
        url = f"{base_url}/cgi-bin/media/upload?access_token={access_token}&type=image"
        try:
            with open(image_path, "rb") as fh:
                files = {"media": (os.path.basename(image_path), fh, "image/jpeg")}
                resp = requests.post(url, files=files, timeout=20)
            data = resp.json()
            if data.get("errcode") == 0 and data.get("media_id"):
                logger.debug("[微信通知] 临时图片素材上传成功")
                return str(data.get("media_id"))
            if data.get("errcode") == 42001:
                access_token = self._get_access_token(force=True)
                if access_token:
                    url = f"{base_url}/cgi-bin/media/upload?access_token={access_token}&type=image"
                    with open(image_path, "rb") as fh:
                        files = {"media": (os.path.basename(image_path), fh, "image/jpeg")}
                        resp = requests.post(url, files=files, timeout=20)
                    data = resp.json()
                    if data.get("errcode") == 0 and data.get("media_id"):
                        logger.debug("[微信通知] 临时图片素材刷新 token 后上传成功")
                        return str(data.get("media_id"))
            logger.error(f"[微信通知] 临时图片素材上传失败: {data}")
        except Exception as e:
            logger.error(f"[微信通知] 临时图片素材上传异常: {e}")
        return None

    def send_image_file(self, image_path: str, to_user: str = "@all") -> bool:
        """发送本地图片文件。"""
        media_id = self.upload_temp_image(image_path)
        if not media_id:
            return False

        access_token = self._get_access_token()
        if not access_token:
            return False

        base_url = self._get_api_base_url()
        url = f"{base_url}/cgi-bin/message/send?access_token={access_token}"
        payload = {
            "touser": to_user,
            "msgtype": "image",
            "agentid": self.config.get("agent_id", ""),
            "image": {"media_id": media_id},
            "safe": 0,
        }
        if self._send_request(url, payload):
            logger.debug("[微信通知] 图片消息发送成功")
            return True
        return False

    def send_news_messages(self, articles: List[Dict[str, str]], to_user: str = "@all") -> bool:
        """
        发送多条图文消息

        Args:
            articles: 文章列表，每篇文章包含 title, description, picurl, url
            to_user: 接收用户

        Returns:
            bool: 是否发送成功
        """
        if not self.config.get("enabled"):
            logger.warning("[微信通知] 通知未启用")
            return False

        if not articles:
            logger.warning("[微信通知] 文章列表为空")
            return False

        access_token = self._get_access_token()
        if not access_token:
            return False

        base_url = self._get_api_base_url()
        url = f"{base_url}/cgi-bin/message/send?access_token={access_token}"

        # 最多支持8条图文
        articles = articles[:8]

        payload = {
            "touser": to_user,
            "msgtype": "news",
            "agentid": self.config.get("agent_id", ""),
            "news": {
                "articles": articles
            }
        }

        if self._send_request(url, payload):
            logger.debug(f"[微信通知] 多图文发送成功: {len(articles)}条")
            return True
        return False

    def send_task_notification(self, task_name: str, status: str, message: str = "") -> bool:
        """
        发送任务完成通知

        Args:
            task_name: 任务名称
            status: 任务状态 (success/failed)
            message: 附加消息

        Returns:
            bool: 是否发送成功
        """
        status_emoji = "✅" if status == "success" else "❌"
        status_text = "成功" if status == "success" else "失败"

        content = f"""## {status_emoji} 任务通知

**任务名称**: {task_name}
**状态**: {status_text}
"""
        if message:
            content += f"\n**详情**: {message}\n"

        content += f"\n> 来自 ChillPoster"

        return self.send_markdown(content)

    def test_connection(self) -> dict:
        """测试连接"""
        result = {"success": False, "message": ""}

        if not self.config.get("corp_id"):
            result["message"] = "缺少企业ID"
            return result

        if not self.config.get("app_secret"):
            result["message"] = "缺少应用Secret"
            return result

        if not self.config.get("agent_id"):
            result["message"] = "缺少应用AgentId"
            return result

        access_token = self._get_access_token(force=True)
        if access_token:
            result["success"] = True
            base_url = self._get_api_base_url()
            proxy_info = f" (代理: {base_url})" if base_url != "https://qyapi.weixin.qq.com" else ""
            result["message"] = f"连接成功，access_token 获取正常{proxy_info}"
        else:
            result["message"] = "获取 access_token 失败，请检查配置"

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

    def _to_wechat_image_url(self, raw_url: str = "", server_idx: int = 0, item_id: str = "") -> str:
        """将图片 URL 转换为企业微信可访问的 ChillPoster 代理地址"""
        from core.configs import global_config, AUTH_FILE
        import time, hmac as _hmac, hashlib as _hashlib, json as _json

        base = global_config.app_public_base_url

        # 未配置公网地址：TMDb 图片直接返回（原有行为），Emby 内网地址返回空
        if not base:
            if raw_url and "image.tmdb.org" in raw_url:
                return raw_url
            return ""

        # TMDb 图片 → 内部代理
        if raw_url and "image.tmdb.org/t/p/" in raw_url:
            import re
            m = re.search(r"/t/p/\w+(/[^?]+)", raw_url)
            if m:
                return f"{base}/api/discover/tmdb_img?path={m.group(1)}"

        # 有 item_id → 生成 emby_cover 签名代理 URL
        if item_id:
            secret = ""
            try:
                with open(AUTH_FILE, "r", encoding="utf-8") as f:
                    secret = _json.load(f).get("secret", "")
            except Exception:
                pass
            if secret:
                ts = int(time.time())
                msg = f"emby_cover:v1:{server_idx}:{item_id}:{ts}"
                sig = _hmac.new(secret.encode(), msg.encode(), _hashlib.sha256).hexdigest()
                return f"{base}/api/discover/emby_cover?server_idx={server_idx}&item_id={item_id}&ts={ts}&sig={sig}"

        return raw_url or ""

    def _get_notification_image_url(self, *, search_name: str, media_type: str = "movie",
                                    year: str = "", tmdb_id: str = "", fallback_url: str = "",
                                    server_idx: int = 0, item_id: str = "") -> str:
        tmdb_url = self._get_media_backdrop_url(search_name, media_type, year=year, tmdb_id=tmdb_id)
        image_url = self._to_wechat_image_url(tmdb_url)
        if image_url:
            return image_url
        return self._to_wechat_image_url(fallback_url, server_idx, item_id)

    def notify_playback(self, item_name: str, emby_name: str = "Emby",
                        user_agent: str = "", poster_url: str = "",
                        original_name: str = "", media_type: str = "movie",
                        overview: str = "", rating: str = "", genres: str = "",
                        tagline: str = "", user_name: str = "",
                        client_info: str = "", year: str = "", tmdb_id: str = "", **kwargs) -> bool:
        """
        发送播放通知（图文卡片样式）
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

        server_idx = kwargs.get("server_idx", 0)
        item_id = kwargs.get("item_id", "")
        import re
        search_name = original_name or item_name.replace("🎬 ", "").replace("📺 ", "").split(" S")[0]
        search_name = re.sub(r'\s*\(\d{4}\)\s*$', '', search_name).strip()
        image_url = self._get_notification_image_url(
            search_name=search_name,
            media_type=media_type,
            year=year,
            tmdb_id=tmdb_id,
            fallback_url=poster_url,
            server_idx=server_idx,
            item_id=item_id,
        )

        return self.send_news_message(title=title, description=description, image_url=image_url)

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
        发送新媒体入库通知（图文卡片样式）
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

        server_idx = kwargs.get("server_idx", 0)
        item_id = kwargs.get("item_id", "")
        search_name = original_name or media_name.split(" S")[0]
        image_url = self._get_notification_image_url(
            search_name=search_name,
            media_type=media_type,
            year=year,
            tmdb_id=tmdb_id,
            fallback_url=poster_url,
            server_idx=server_idx,
            item_id=item_id,
        )

        return self.send_news_message(title=title, description=description, image_url=image_url)

    def notify_organize_complete(self, media_name: str, media_type: str = "tv",
                                 year: str = "", season_episode: str = "",
                                 rating: str = "", genres: str = "",
                                 overview: str = "", tmdb_id: str = "",
                                 quality: str = "", video: str = "", audio: str = "",
                                 episode_count: str = "", episode_ranges: str = "", file_size: str = "",
                                 release_group: str = "", elapsed: str = "", library_location: str = "",
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
            "video": video or kwargs.get("video") or "",
            "audio": audio or "",
            "library_location": library_location or kwargs.get("library_location") or "",
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
        image_url = self._get_notification_image_url(
            search_name=search_name,
            media_type=media_type,
            year=year,
            tmdb_id=tmdb_id,
        )

        return self.send_news_message(title=title, description=description, image_url=image_url)

    def notify_wash_result(self, media_name: str, media_type: str = "tv",
                           year: str = "", season_episode: str = "",
                           rating: str = "", genres: str = "", overview: str = "",
                           tmdb_id: str = "", library_location: str = "",
                           status: str = "success", status_text: str = "",
                           status_emoji: str = "", decision_text: str = "",
                           reason_text: str = "", old_resource: dict | None = None,
                           new_resource: dict | None = None, old_summary: str = "",
                           new_summary: str = "", old_file_name: str = "",
                           new_file_name: str = "", original_name: str = "",
                           **kwargs) -> bool:
        """发送洗版结果通知。优先发送渲染图片，失败时回退普通图文消息。"""
        if not self.is_notify_type_enabled("wash_result"):
            return False

        templates = self.config.get("templates", {}).get("wash_result", {})
        title_tpl = templates.get("title", "洗版{{ status_text }} {{ status_emoji }} 《{{ title }}》{% if year %}({{ year }}){% endif %}{% if season_episode %} {{ season_episode }}{% endif %}")
        text_tpl = templates.get("text", "")
        now = kwargs.get("now") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        media_type_label = "电影" if media_type == "movie" else "剧集"
        status_text = status_text or ("成功" if status == "success" else "失败")
        status_emoji = status_emoji or ("✅" if status == "success" else "⚠️")
        context = {
            "title": media_name,
            "year": str(year) if year else "",
            "media_type": media_type_label,
            "season_episode": season_episode or "",
            "rating": rating or "",
            "genres": genres or "",
            "overview": overview or "",
            "tmdb_id": tmdb_id or "",
            "library_location": library_location or "",
            "library_location_short": kwargs.get("library_location_short") or "",
            "status": status or "",
            "status_text": status_text,
            "status_emoji": status_emoji,
            "decision_text": decision_text or "",
            "reason_text": reason_text or "",
            "old_summary": old_summary or "",
            "new_summary": new_summary or "",
            "old_file_name": old_file_name or "",
            "new_file_name": new_file_name or "",
            "old_file_short": kwargs.get("old_file_short") or "",
            "new_file_short": kwargs.get("new_file_short") or "",
            "now": now,
        }
        title = render_template(title_tpl, context)
        description = render_template(text_tpl, context)

        search_name = original_name or media_name.split(" S")[0]
        poster_url = self._get_notification_image_url(
            search_name=search_name,
            media_type=media_type,
            year=year,
            tmdb_id=tmdb_id,
        )

        # 企业微信图片消息在聊天内会被压成小缩略图，点开又按原图展示；
        # 洗版通知默认走图文卡片，让资源对比信息直接显示在消息内容里。
        return self.send_news_message(title=title, description=description, image_url=poster_url)

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

        # 构建卡片标题和描述
        title = f"{status_emoji} 签到通知"
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

        return self.send_news_message(
            title=title,
            description=description
        )

    def notify_task_complete(self, task_name: str, status: str = "success",
                             detail: str = "", posters_count: int = 0,
                             poster_url: str = "", elapsed: str = "",
                             scanned: int = 0, scanned_dirs: int = 0,
                             generated: int = 0, downloaded: int = 0,
                             download_failed: int = 0, skipped: int = 0,
                             strm_generated: int = 0,
                             subtitle_downloaded: int = 0, aux_downloaded: int = 0,
                             subtitle_download_failed: int = 0, aux_download_failed: int = 0,
                             strm_skipped: int = 0, subtitle_skipped: int = 0,
                             aux_skipped: int = 0, video_min_size_skipped: int = 0,
                             out_of_scope_skipped: int = 0, other_skipped: int = 0,
                             tmdb_generated: int = 0, tmdb_skipped: int = 0,
                             tmdb_failed: int = 0,
                             deleted: int = 0, failed: int = 0,
                             retry_success: int = 0, retry_failed: int = 0,
                             task_category: str = "", total_count: int = 0,
                             success_count: int = 0, already_count: int = 0,
                             trigger: str = "", summary: str = "",
                             accounts_text: str = "", organize_size: str = "") -> bool:
        """发送任务完成通知（图文卡片样式）"""
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
            "is_strm_task": str(task_name or "").startswith("STRM任务") or task_category == "strm",
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
            "strm_generated": int(strm_generated or generated or 0),
            "subtitle_downloaded": int(subtitle_downloaded or 0),
            "aux_downloaded": int(aux_downloaded or 0),
            "subtitle_download_failed": int(subtitle_download_failed or 0),
            "aux_download_failed": int(aux_download_failed or 0),
            "strm_skipped": int(strm_skipped or 0),
            "subtitle_skipped": int(subtitle_skipped or 0),
            "aux_skipped": int(aux_skipped or 0),
            "video_min_size_skipped": int(video_min_size_skipped or 0),
            "out_of_scope_skipped": int(out_of_scope_skipped or 0),
            "other_skipped": int(other_skipped or 0),
            "tmdb_generated": int(tmdb_generated or 0),
            "tmdb_skipped": int(tmdb_skipped or 0),
            "tmdb_failed": int(tmdb_failed or 0),
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
            "organize_size": organize_size or "",
            "poster_url": poster_url or "",
            "now": now,
        }

        title = render_template(title_tpl, context)
        description = render_template(text_tpl, context)

        return self.send_news_message(
            title=title,
            description=description,
            image_url=poster_url
        )


# 全局实例
wechat_notify_service = WechatNotifyService()
