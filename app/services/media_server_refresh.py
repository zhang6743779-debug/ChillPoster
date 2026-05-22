import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Dict, Optional

from core.emby_client import EmbyClient
from core.logger import logger
from app.routers.config_302 import get_emby_configs_sync
from app.services.emby_library_cache import find_libraries_for_path, refresh_server_libraries


class RefreshMediaItem:
    def __init__(self, title: str, year: Any, type: str, category: str, target_path: Path):
        self.title = title
        self.year = year
        self.type = type
        self.category = category
        self.target_path = target_path


class _ServiceAdapter:
    def __init__(self, server_idx: int, server: dict):
        self.server_idx = server_idx
        self.server = server
        self.client = EmbyClient(server.get("url", ""), server.get("key", ""), server.get("public_host"))

    def is_inactive(self) -> bool:
        try:
            return not self.client.test_connection()
        except Exception:
            return True

    @staticmethod
    def _norm_path(path: str) -> str:
        try:
            path = str(Path(path).resolve())
        except Exception:
            path = str(path)
        return path.replace("\\", "/").rstrip("/").lower()

    def refresh_library_by_items(self, items: List[RefreshMediaItem]):
        library_ids = set()

        for item in items:
            target_path = self._norm_path(str(item.target_path))
            if not target_path:
                continue

            matched_libs = find_libraries_for_path(self.server_idx, target_path)
            if not matched_libs:
                refresh_server_libraries(self.server_idx)
                matched_libs = find_libraries_for_path(self.server_idx, target_path)

            for lib in matched_libs:
                lib_id = lib.get("id")
                if lib_id:
                    library_ids.add(str(lib_id))

        if library_ids:
            for library_id in sorted(library_ids):
                self.client.refresh_library(library_id)
            refresh_server_libraries(self.server_idx)
        else:
            self.refresh_root_library()

    def refresh_root_library(self):
        self.client.refresh_library()
        refresh_server_libraries(self.server_idx)

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


class MediaServerRefresh:
    _enabled = True
    _delay = 20
    _mediaservers = []

    _in_delay = False
    _pending_items = []
    _end_time = 0.0
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", self._enabled)
            self._delay = config.get("delay") or self._delay
            self._mediaservers = config.get("mediaservers") or self._mediaservers

    def _build_refresh_item(self, event_info: dict) -> Optional[RefreshMediaItem]:
        if not event_info:
            return None
        target_path = event_info.get("target_path", "")
        if not target_path:
            return None
        return RefreshMediaItem(
            title=event_info.get("title", ""),
            year=event_info.get("year", ""),
            type=event_info.get("type", ""),
            category=event_info.get("category", ""),
            target_path=Path(target_path),
        )

    def _refresh_items(self, items: List[RefreshMediaItem]):
        if not self._enabled or not items:
            return

        service_infos = self.service_infos
        if not service_infos:
            return

        for name, service in service_infos.items():
            try:
                if hasattr(service.instance, 'refresh_library_by_items'):
                    service.instance.refresh_library_by_items(items)
                elif hasattr(service.instance, 'refresh_root_library'):
                    service.instance.refresh_root_library()
                else:
                    logger.warning(f"{name} 不支持刷新")
            finally:
                if hasattr(service.instance, 'close'):
                    service.instance.close()

    @property
    def service_infos(self) -> Optional[Dict[str, Any]]:
        if not self._enabled:
            return None

        embys = get_emby_configs_sync()
        if not embys:
            logger.warning("尚未配置媒体服务器，请检查配置")
            return None

        active_services = {}
        for idx, server in enumerate(embys):
            if self._mediaservers and server.get("name") not in self._mediaservers:
                continue
            if not server.get("enabled", True):
                continue
            service_name = server.get("name") or server.get("url", "")
            if not server.get("url") or not server.get("key"):
                logger.warning(f"媒体服务器 {service_name} 配置不完整，请检查配置")
                continue

            adapter = _ServiceAdapter(idx, server)
            if adapter.is_inactive():
                logger.warning(f"媒体服务器 {service_name} 未连接，请检查配置")
                adapter.close()
            else:
                active_services[service_name] = SimpleNamespace(instance=adapter)

        if not active_services:
            logger.warning("没有已连接的媒体服务器，请检查配置")
            return None

        return active_services

    def get_state(self) -> bool:
        return self._enabled

    def refresh(self, event_info: dict):
        if not self._enabled:
            return

        item = self._build_refresh_item(event_info)
        if not item:
            return

        def debounce_delay(duration: int):
            with self._lock:
                self._end_time = time.time() + float(duration)
                if self._in_delay:
                    return False
                self._in_delay = True

            def end_time():
                with self._lock:
                    return self._end_time

            while time.time() < end_time():
                time.sleep(1)
            with self._lock:
                self._in_delay = False
            return True

        if self._delay:
            with self._lock:
                self._pending_items.append(item)
            if not debounce_delay(self._delay):
                return
            with self._lock:
                items = self._pending_items
                self._pending_items = []
        else:
            items = [item]

        self._refresh_items(items)

    def refresh_immediately(self, event_infos: List[dict]):
        if not self._enabled or not event_infos:
            return

        items = []
        for event_info in event_infos:
            item = self._build_refresh_item(event_info)
            if item:
                items.append(item)

        if not items:
            return

        logger.trace(f"[MediaServerRefresh] 立即刷新媒体库: {len(items)} 个路径")
        self._refresh_items(items)

    def stop_service(self):
        with self._lock:
            self._end_time = 0.0


media_server_refresh = MediaServerRefresh()
