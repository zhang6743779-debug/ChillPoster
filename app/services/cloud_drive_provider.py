from __future__ import annotations

import base64
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from core.logger import logger


CONFIG_302_FILE = Path("config/config_302.json")
CLOUDDRIVE_PROVIDER_KEYS = {"123pan", "guangya", "clouddrive2"}
PROVIDER_LABELS = {
    "115": "115云盘",
    "123pan": "123云盘",
    "guangya": "光鸭云盘",
    "clouddrive2": "CloudDrive2",
}


def normalize_provider(value: str | None) -> str:
    provider = str(value or "115").strip().lower()
    aliases = {
        "123": "123pan",
        "123cloud": "123pan",
        "123yun": "123pan",
        "123yunpan": "123pan",
        "guangyapan": "guangya",
        "guangya": "guangya",
        "光鸭": "guangya",
        "光鸭云盘": "guangya",
        "clouddrive": "clouddrive2",
        "cloud_drive2": "clouddrive2",
    }
    return aliases.get(provider, provider or "115")


def provider_label(provider: str | None) -> str:
    return PROVIDER_LABELS.get(normalize_provider(provider), str(provider or "云盘"))


def is_cloud_provider(provider: str | None) -> bool:
    return normalize_provider(provider) in CLOUDDRIVE_PROVIDER_KEYS


def load_config_302_sync() -> dict:
    if not CONFIG_302_FILE.exists():
        return {}
    try:
        with CONFIG_302_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"[CloudDrive2] 读取 302 配置失败: {e}")
        return {}


def get_drive_config(drive_index: int = 0, config_data: dict | None = None) -> dict:
    data = config_data if isinstance(config_data, dict) else load_config_302_sync()
    drives = data.get("drives") if isinstance(data.get("drives"), list) else []
    if drives:
        try:
            idx = int(drive_index or 0)
        except (TypeError, ValueError):
            idx = 0
        drive = drives[idx] if 0 <= idx < len(drives) else drives[0]
        return drive if isinstance(drive, dict) else {}
    drive = data.get("drive")
    return drive if isinstance(drive, dict) else {}


def is_drive_115(drive_index: int = 0, config_data: dict | None = None) -> bool:
    drive = get_drive_config(drive_index, config_data)
    return normalize_provider(drive.get("provider")) == "115"


def encode_cloud_path(path: str) -> str:
    raw = str(path or "").encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cloud_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii")).decode("utf-8")


def normalize_remote_path(path: str | None) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if not text or text == "0":
        return "/"
    if not text.startswith("/"):
        text = "/" + text
    while "//" in text:
        text = text.replace("//", "/")
    return text.rstrip("/") or "/"


def _bool_from_config(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "只读"}


class CloudDrive2WebDavDrive:
    """CloudDrive2 WebDAV backed cloud drive adapter.

    123云盘和光鸭云盘先在 CloudDrive2 中添加账号，再由 ChillPoster
    通过 CloudDrive2 WebDAV 服务执行目录浏览、上传、删除和直链访问。
    """

    def __init__(self, drive_config: dict):
        self.drive_config = drive_config if isinstance(drive_config, dict) else {}
        self.provider = normalize_provider(self.drive_config.get("provider"))
        self.name = str(self.drive_config.get("name") or provider_label(self.provider)).strip()
        self.base_url = self._normalize_base_url(self.drive_config.get("clouddrive_base_url"))
        self.direct_base_url = str(self.drive_config.get("clouddrive_direct_base_url") or "").strip().rstrip("/")
        self.username = str(self.drive_config.get("clouddrive_username") or "").strip()
        self.password = str(self.drive_config.get("clouddrive_password") or "").strip()
        self.root_path = normalize_remote_path(self.drive_config.get("clouddrive_root_path") or "/")
        self.read_only = self.provider == "guangya" or _bool_from_config(self.drive_config.get("clouddrive_read_only"))
        if not self.base_url:
            raise ValueError(f"{provider_label(self.provider)} 未配置 CloudDrive2 WebDAV 地址")

    @staticmethod
    def _normalize_base_url(value: Any) -> str:
        raw = str(value or "").strip().rstrip("/")
        if not raw:
            return ""
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("CloudDrive2 WebDAV 地址必须包含 http:// 或 https://")
        path = parsed.path.rstrip("/")
        if not path:
            path = "/dav"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def _auth(self):
        if self.username or self.password:
            return httpx.BasicAuth(self.username, self.password)
        return None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": "ChillPoster/CloudDrive2"}
        if extra:
            headers.update(extra)
        return headers

    def _url_for_full_path(self, full_path: str) -> str:
        path = quote(normalize_remote_path(full_path).lstrip("/"), safe="/")
        return f"{self.base_url.rstrip('/')}/{path}" if path else self.base_url

    def _url_for_public_path(self, full_path: str) -> str:
        base = self.direct_base_url or self.base_url
        if not self.direct_base_url and (self.username or self.password):
            parsed = urlsplit(base)
            if "@" not in parsed.netloc:
                user = quote(self.username, safe="")
                password = quote(self.password, safe="")
                auth_netloc = f"{user}:{password}@{parsed.netloc}"
                base = urlunsplit((parsed.scheme, auth_netloc, parsed.path, "", ""))
        path = quote(normalize_remote_path(full_path).lstrip("/"), safe="/")
        return f"{base.rstrip('/')}/{path}" if path else base.rstrip("/")

    def _request(self, method: str, full_path: str, *, ok_statuses: set[int], timeout: float | None = 60, **kwargs) -> httpx.Response:
        resp = httpx.request(
            method,
            self._url_for_full_path(full_path),
            auth=self._auth(),
            timeout=timeout,
            **kwargs,
        )
        if resp.status_code not in ok_statuses:
            body = (resp.text or "").strip()
            if len(body) > 300:
                body = body[:300] + "..."
            raise RuntimeError(f"CloudDrive2 WebDAV {method} 失败: HTTP {resp.status_code} {body}")
        return resp

    def _full_path(self, path: str | None) -> str:
        req = normalize_remote_path(path)
        if req == "/":
            return self.root_path
        if self.root_path == "/":
            return req
        if req == self.root_path or req.startswith(self.root_path.rstrip("/") + "/"):
            return req
        return normalize_remote_path(f"{self.root_path.rstrip('/')}/{req.lstrip('/')}")

    def _display_path(self, full_path: str) -> str:
        full = normalize_remote_path(full_path)
        root = self.root_path.rstrip("/")
        if root and root != "/" and full.startswith(root + "/"):
            return normalize_remote_path(full[len(root):])
        if root and full == root:
            return "/"
        return full

    def _href_to_full_path(self, href: str) -> str:
        parsed_href = urlsplit(str(href or ""))
        href_path = unquote(parsed_href.path if parsed_href.scheme or parsed_href.netloc else str(href or ""))
        base_path = unquote(urlsplit(self.base_url).path.rstrip("/"))
        if base_path and href_path == base_path:
            return "/"
        if base_path and href_path.startswith(base_path + "/"):
            href_path = href_path[len(base_path):]
        return normalize_remote_path(href_path)

    def _parse_propfind(self, response: httpx.Response, request_full_path: str, include_files: bool) -> dict[str, Any]:
        root = ET.fromstring(response.content)
        ns = {"d": "DAV:"}
        dirs: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        request_full_path = normalize_remote_path(request_full_path)

        for node in root.findall("d:response", ns):
            href_text = (node.findtext("d:href", default="", namespaces=ns) or "").strip()
            full_path = self._href_to_full_path(href_text)
            if full_path == request_full_path:
                continue

            prop = node.find("d:propstat/d:prop", ns)
            if prop is None:
                continue
            is_dir = prop.find("d:resourcetype/d:collection", ns) is not None
            name = (prop.findtext("d:displayname", default="", namespaces=ns) or "").strip()
            if not name:
                name = os.path.basename(full_path.rstrip("/"))
            if not name:
                continue
            size_text = prop.findtext("d:getcontentlength", default="0", namespaces=ns) or "0"
            try:
                size = int(size_text)
            except (TypeError, ValueError):
                size = 0
            item = {
                "name": name,
                "cid": self._display_path(full_path),
                "path": self._display_path(full_path),
                "provider": self.provider,
                "size": size,
                "modified": prop.findtext("d:getlastmodified", default="", namespaces=ns) or "",
                "read_only": self.read_only,
            }
            if is_dir:
                dirs.append(item)
            elif include_files:
                files.append(item)

        result = {
            "status": "ok",
            "provider": self.provider,
            "provider_label": provider_label(self.provider),
            "read_only": self.read_only,
            "current": self._display_path(request_full_path),
            "dirs": dirs,
        }
        if include_files:
            result["files"] = files
        return result

    def _ensure_writable(self, action: str) -> None:
        if self.read_only:
            raise PermissionError(f"{provider_label(self.provider)} 在当前 CloudDrive2 版本为只读，不能执行{action}")

    def test_connection(self) -> dict[str, Any]:
        listing = self.list("/")
        return {
            "status": "ok",
            "provider": self.provider,
            "provider_label": provider_label(self.provider),
            "name": self.name,
            "root_path": self.root_path,
            "read_only": self.read_only,
            "message": f"{provider_label(self.provider)} 已通过 CloudDrive2 连接，根目录可访问",
            "total": len(listing.get("dirs") or []),
        }

    def list(self, path: str = "0", include_files: bool = False) -> dict[str, Any]:
        full_path = self._full_path(path)
        resp = self._request(
            "PROPFIND",
            full_path,
            ok_statuses={207},
            headers=self._headers({"Depth": "1"}),
        )
        return self._parse_propfind(resp, full_path, include_files)

    def iter_files(self, root_path: str) -> list[dict[str, Any]]:
        root_full = self._full_path(root_path)
        items: list[dict[str, Any]] = []

        def walk(display_path: str, ancestors: list[dict[str, str]]):
            listing = self.list(display_path, include_files=True)
            dirs = listing.get("dirs") or []
            files = listing.get("files") or []
            for raw in dirs + files:
                name = str(raw.get("name") or "").strip()
                display = normalize_remote_path(raw.get("path") or raw.get("cid"))
                is_dir = raw in dirs
                item = {
                    "name": name,
                    "id": display,
                    "path": display,
                    "parent_id": normalize_remote_path(display_path),
                    "pickcode": encode_cloud_path(display),
                    "cloud_path": display,
                    "size": int(raw.get("size") or 0),
                    "sha1": "",
                    "is_dir": is_dir,
                    "ancestors": list(ancestors),
                    "provider": self.provider,
                    "read_only": self.read_only,
                }
                items.append(item)
                if is_dir:
                    walk(display, [*ancestors, {"id": display, "name": name, "path": display}])

        walk(self._display_path(root_full), [])
        return items

    def ensure_dir(self, path: str) -> str:
        self._ensure_writable("创建目录")
        full = self._full_path(path)
        if full == "/":
            return self._display_path(full)
        current = ""
        for part in [p for p in full.strip("/").split("/") if p]:
            current = f"{current}/{part}" if current else f"/{part}"
            resp = httpx.request(
                "MKCOL",
                self._url_for_full_path(current),
                auth=self._auth(),
                headers=self._headers(),
                timeout=60,
            )
            if resp.status_code not in (200, 201, 204, 405):
                body = (resp.text or "").strip()
                raise RuntimeError(f"CloudDrive2 创建目录失败: {current} HTTP {resp.status_code} {body[:200]}")
        return self._display_path(full)

    def remove(self, path: str) -> None:
        self._ensure_writable("删除")
        full = self._full_path(path)
        if full in {"", "/"} or full == self.root_path:
            raise ValueError("禁止删除根目录")
        self._request("DELETE", full, ok_statuses={200, 202, 204, 207, 404}, headers=self._headers())

    def move(self, source_path: str, target_dir: str, filename: str | None = None) -> dict[str, Any]:
        self._ensure_writable("移动/重命名")
        source_full = self._full_path(source_path)
        target_display_dir = self.ensure_dir(target_dir)
        target_full_dir = self._full_path(target_display_dir)
        target_name = filename or os.path.basename(source_full.rstrip("/"))
        target_full = normalize_remote_path(f"{target_full_dir.rstrip('/')}/{target_name}")
        self._request(
            "MOVE",
            source_full,
            ok_statuses={200, 201, 204},
            headers=self._headers({
                "Destination": self._url_for_full_path(target_full),
                "Overwrite": "F",
            }),
        )
        return {"status": "ok", "path": self._display_path(target_full), "name": target_name}

    def get_direct_url(self, path: str) -> str:
        full = self._full_path(path)
        return self._url_for_public_path(full)

    def upload_file(self, local_path: str, target_dir: str, filename: str | None = None) -> dict[str, Any]:
        self._ensure_writable("上传")
        source = Path(local_path)
        if not source.is_file():
            raise FileNotFoundError(str(local_path))
        target_display_dir = self.ensure_dir(target_dir)
        target_full_dir = self._full_path(target_display_dir)
        target_name = filename or source.name
        target_full_path = normalize_remote_path(f"{target_full_dir.rstrip('/')}/{target_name}")
        with source.open("rb") as f:
            resp = httpx.put(
                self._url_for_full_path(target_full_path),
                content=f,
                auth=self._auth(),
                headers=self._headers(),
                timeout=None,
            )
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"CloudDrive2 上传失败: HTTP {resp.status_code} {(resp.text or '')[:200]}")
        return {"status": "ok", "path": self._display_path(target_full_path), "name": target_name}


def get_cloud_drive(drive_index: int = 0, drive_config: dict | None = None) -> CloudDrive2WebDavDrive:
    cfg = drive_config if isinstance(drive_config, dict) else get_drive_config(drive_index)
    provider = normalize_provider(cfg.get("provider"))
    if not is_cloud_provider(provider):
        raise ValueError("当前账号不是 CloudDrive2 云盘适配 provider")
    return CloudDrive2WebDavDrive(cfg)


def build_cloud_play_url(url_base: str, drive_index: int, cloud_path: str, filename: str = "") -> str:
    _, ext = os.path.splitext(str(filename or cloud_path or ""))
    suffix = ext if ext else ".mkv"
    return f"{url_base.rstrip('/')}/cd/{int(drive_index or 0)}/{encode_cloud_path(cloud_path)}{suffix}"


def parse_cloud_play_path(path: str) -> tuple[int, str, str]:
    normalized = str(path or "").strip().lstrip("/")
    if not normalized.lower().startswith("cd/"):
        return -1, "", ""
    parts = normalized.split("/", 2)
    if len(parts) < 3:
        return -1, "", ""
    try:
        drive_index = int(parts[1])
    except (TypeError, ValueError):
        drive_index = 0
    encoded = parts[2].split("/", 1)[0]
    if "." in encoded:
        encoded = encoded.rsplit(".", 1)[0]
    try:
        cloud_path = decode_cloud_path(encoded)
    except Exception:
        cloud_path = unquote(encoded)
    display_name = normalized.split("/", 2)[-1]
    return drive_index, cloud_path, display_name
