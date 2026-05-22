# core/configs.py
import os
import json

# 获取项目根目录 (假设 core/configs.py 在 project/core/ 下，往上两层就是根目录)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 定义关键目录
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DEFAULTS_DIR = os.path.join(BASE_DIR, "defaults")
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
LAYOUTS_DIR = os.path.join(BASE_DIR, "layouts")
BACKUPS_DIR = os.path.join(BASE_DIR, "backups")

# 定义关键文件路径
APP_LOG_FILE = os.path.join(CONFIG_DIR, "app.log")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
AUTH_FILE = os.path.join(CONFIG_DIR, "auth.json")
TRANSLATIONS_FILE = os.path.join(CONFIG_DIR, "translations.json")
TASKS_FILE = os.path.join(CONFIG_DIR, "tasks.json")
TASK_PROGRESS_FILE = os.path.join(CONFIG_DIR, "task_progress.json")
LICENSE_FILE = os.path.join(CONFIG_DIR, "license.json")
RSS_TASKS_FILE = os.path.join(CONFIG_DIR, "rss_tasks.json")
RSS_CONFIG_FILE = os.path.join(CONFIG_DIR, "rss_settings.json")
WEBHOOK_CONFIG_FILE = os.path.join(CONFIG_DIR, "webhook.json")
WECHAT_NOTIFY_CONFIG_FILE = os.path.join(CONFIG_DIR, "wechat_notify.json")
MEDIA_LIBRARY_CACHE_FILE = os.path.join(CONFIG_DIR, "media_library_cache.json")
EMBY_DISCOVER_INDEX_FILE = os.path.join(CONFIG_DIR, "emby_discover_index.json")
MISSING_EPISODE_STATS_CACHE_FILE = os.path.join(CONFIG_DIR, "missing_episode_stats_cache.json")
DEVICE_ID_FILE = os.path.join(CONFIG_DIR, "device_id.txt")

# 确保必要的目录存在
def ensure_directories():
    for d in [CONFIG_DIR, FONTS_DIR, TEMPLATES_DIR, LAYOUTS_DIR, BACKUPS_DIR]:
        if not os.path.exists(d):
            try:
                os.makedirs(d)
            except:
                pass

ensure_directories()

# === [新增] 全局配置类，修复 import 错误 ===
class GlobalConfig:
    def __init__(self):
        self.proxy_url = None
        self.tmdb_key = None
        self.douban_cookie = None
        self.app_public_base_url = ""
        self._data = {}
        self.load()

    def load(self):
        """尝试从 settings.json 加载配置（启动时调用一次）"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    self._data = json.load(f)
                    self.proxy_url = self._data.get("proxy_url")
                    self.tmdb_key = self._data.get("tmdb_key")
                    self.douban_cookie = self._data.get("douban_cookie")
                    self.app_public_base_url = self._data.get("app_public_base_url", "").rstrip("/")
            except Exception:
                pass

    def get(self, key, default=None):
        return self._data.get(key, default)

# 实例化对象，供其他模块 import
global_config = GlobalConfig()
