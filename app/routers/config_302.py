import os
import json
import base64
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, List, Any
from p115client import P115Client

# [新增] 引入日志模块，用于打印配置状态
from core.logger import logger
from app.services.drive115_auth_probe import format_115_login_app_label, probe_115_cookie
from app.services.media_organize_115_ops import _get_115_client, run_115_write_request_sync

# ❌ [重要] 绝对不要在这里导入 task_service，否则会报错 ImportError (循环引用)

router = APIRouter(prefix="/api/config_302", tags=["config_302"])

# 配置文件保存路径
CONFIG_FILE = "config/config_302.json"

# ==========================================
# 1. 定义数据模型 (顺序很重要)
# ==========================================

# [第一步] 先定义基础配置类
class Drive115Config(BaseModel):
    name: str = '115'
    cookie: str = ''  # 大号 cookie（存储资源）
    show_cookie: bool = False # 前端辅助字段
    enable_sync: bool = False # 同播复制开关
    enable_rapid: bool = False # 秒传开关
    auto_delete: bool = True
    delete_cron: str = '30 3 * * *'
    recycle_code: str = ''
    upload_dir: str = '/ChillPoster'
    enable_standard_topology: bool = True  # 115 一条龙自动化配置
    local_media_root: str = ''
    remote_root_name: str = '影视库'

    # 资源转存配置
    transfer_dir: str = ''         # 转存目标目录：填路径如 "/转存" 或数字 ID
    transfer_drive_index: int = 0  # 使用哪个 115 账号转存

    # 秒传小号池配置（适配前端的多账号池设计）
    rapid_mode: str = 'auto'  # 调度策略: auto 自动轮询, 或指定账号索引
    rapid_accounts: list = []  # 小号池: [{"name": "小号1", "cookie": "xxx", "recycle_code": "", "upload_dir": "/ChillPoster"}]

    # 允许前端发送额外的字段，防止 422 错误
    class Config:
        extra = "ignore"

class Emby302Modes(BaseModel):
    pickcode: bool = True

class Emby302Config(BaseModel):
    name: str = 'Emby'
    url: str = ''
    key: str = ''
    public_host: str = ''
    proxy_port: str = '8098'
    modes: Emby302Modes = Emby302Modes()
    preload: bool = False
    rapid_play: bool = False
    enabled: bool = True
    drive_index: int = -1  # 兼容旧字段，固定为 0 处理

    # 允许前端发送额外的字段
    class Config:
        extra = "ignore"

class Config302Payload(BaseModel):
    drives: List[Drive115Config] = []
    embys: List[Emby302Config] = []

class SaveEmbyPayload(BaseModel):
    embys: List[Emby302Config] = []

class Test115Payload(BaseModel):
    cookie: str


class Start115QrPayload(BaseModel):
    app: str = "115android"


class Status115QrPayload(BaseModel):
    uid: str
    time: int
    sign: str


class Result115QrPayload(BaseModel):
    uid: str
    app: str = "115android"


SUPPORTED_115_QR_APPS = {
    "web": "115生活(网页版)",
    "android": "115生活(Android端)",
    "ios": "115生活(iOS端)",
    "ipad": "115生活(iPad端)",
    "115android": "115网盘(Android端)",
    "115ios": "115网盘(iOS端)",
    "115ipad": "115网盘(iPad端)",
    "tv": "115生活(Android电视端)",
    "apple_tv": "115生活(Apple TV端)",
    "wechatmini": "115生活(微信小程序)",
    "alipaymini": "115生活(支付宝小程序)",
    "windows": "115生活(Windows端)",
    "mac": "115生活(macOS端)",
    "linux": "115生活(Linux端)",
    "qandroid": "115管理(Android端)",
    "qios": "115管理(iOS端)",
    "qipad": "115管理(iPad端)",
    "harmony": "115网盘(鸿蒙端)",
}


STATUS_MESSAGES = {
    "waiting": "等待扫码",
    "scanned": "已扫码，请在手机上确认登录",
    "confirmed": "扫码确认成功，正在获取 Cookie",
    "expired": "二维码已过期，请重新生成",
    "cancelled": "扫码已取消",
    "error": "扫码状态异常",
}


def _format_115_login_app_label(app: str) -> str:
    return format_115_login_app_label(app)


def _load_json_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"读取配置失败 {path}: {e}")
        return {}


def _normalize_single_drive_config(drive: Any) -> dict:
    normalized = Drive115Config().dict()
    if isinstance(drive, dict):
        normalized.update(drive)
    normalized["enable_standard_topology"] = True
    normalized["transfer_drive_index"] = 0
    if not isinstance(normalized.get("rapid_accounts"), list):
        normalized["rapid_accounts"] = []
    return normalized


def _normalize_single_emby_config(emby: Any) -> dict:
    normalized = Emby302Config().dict()
    if isinstance(emby, dict):
        for key in ("name", "url", "key", "public_host", "proxy_port", "modes", "preload", "rapid_play", "enabled", "drive_index"):
            if key in emby:
                normalized[key] = emby[key]
    normalized["drive_index"] = 0
    modes = normalized.get("modes")
    if not isinstance(modes, dict):
        modes = Emby302Modes().dict()
    normalized["modes"] = {
        "pickcode": bool(modes.get("pickcode", True)),
    }
    return normalized


def _normalize_config_302_data(data: Any) -> dict:
    data = data if isinstance(data, dict) else {}
    normalized = {
        key: value for key, value in data.items()
        if key not in ("drives", "drive", "embys", "emby")
    }

    raw_drives = data.get("drives") if isinstance(data.get("drives"), list) else []
    raw_embys = data.get("embys") if isinstance(data.get("embys"), list) else []
    raw_drive = raw_drives[0] if raw_drives else data.get("drive")
    raw_emby = raw_embys[0] if raw_embys else data.get("emby")

    if len(raw_drives) > 1:
        logger.warning(f"[302] 检测到 {len(raw_drives)} 个顶层 115 配置，仅保留第一个")
    if len(raw_embys) > 1:
        logger.warning(f"[302] 检测到 {len(raw_embys)} 个顶层 Emby 配置，仅保留第一个")

    normalized["drives"] = [_normalize_single_drive_config(raw_drive)]
    normalized["embys"] = [_normalize_single_emby_config(raw_emby)]
    return normalized


def _normalize_115_qr_app(app: str) -> str:
    app = (app or "").strip()
    if app not in SUPPORTED_115_QR_APPS:
        raise HTTPException(status_code=400, detail=f"不支持的扫码客户端: {app}")
    return app


def _build_cookie_string(cookie_data: Any) -> str:
    if isinstance(cookie_data, str):
        return cookie_data.strip().strip(";")
    if isinstance(cookie_data, dict):
        pairs = []
        for key, value in cookie_data.items():
            if value is None:
                continue
            key = str(key).strip()
            value = str(value).strip()
            if key and value:
                pairs.append(f"{key}={value}")
        return "; ".join(pairs)
    if isinstance(cookie_data, list):
        pairs = []
        for item in cookie_data:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("key") or "").strip()
                value = str(item.get("value") or "").strip()
                if name and value:
                    pairs.append(f"{name}={value}")
            elif isinstance(item, str):
                text = item.strip().strip(";")
                if text:
                    pairs.append(text)
        return "; ".join(pairs)
    return ""


def _extract_cookie_from_scan_result(resp: dict) -> str:
    data = (resp or {}).get("data") or {}
    for key in ("cookie", "cookies"):
        cookie = _build_cookie_string(data.get(key))
        if cookie:
            return cookie
        cookie = _build_cookie_string(resp.get(key))
        if cookie:
            return cookie
    headers = resp.get("headers") or {}
    if isinstance(headers, dict):
        set_cookie = headers.get("set-cookie") or headers.get("Set-Cookie")
        if isinstance(set_cookie, str):
            parts = []
            for chunk in set_cookie.split(","):
                segment = chunk.strip()
                kv = segment.split(";", 1)[0].strip()
                if "=" in kv and not kv.lower().startswith(("path=", "expires=", "domain=", "max-age=", "httponly", "secure", "samesite")):
                    parts.append(kv)
            if parts:
                return "; ".join(parts)
    return ""


def _save_config_302_sync(data: dict):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def _find_child_dir_in_115(client: P115Client, parent_cid: int, name: str) -> Optional[str]:
    try:
        resp = client.fs_files({"cid": int(parent_cid), "limit": 1000, "fc_mix": 0})
        if not resp or not resp.get("state"):
            return None
        raw_items = resp.get("data", [])
        if isinstance(raw_items, dict):
            raw_items = (
                raw_items.get("list")
                or raw_items.get("files")
                or raw_items.get("data")
                or raw_items.get("items")
                or []
            )
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("fid") and str(item.get("fc", "") or "") != "0":
                continue
            item_name = str(item.get("n") or item.get("fn") or item.get("name") or "").strip()
            if item_name == name:
                return str(item.get("cid") or item.get("id") or item.get("category_id") or item.get("fid") or "")
    except Exception as e:
        logger.warning(f"查找 115 子目录失败: parent={parent_cid}, name={name}, error={e}")
    return None


def _normalize_115_remote_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    while "//" in text:
        text = text.replace("//", "/")
    return text.rstrip("/") or "/"


def _get_115_dir_id_by_path(client: P115Client, path: str) -> Optional[str]:
    normalized_path = _normalize_115_remote_path(path)
    if not normalized_path or normalized_path == "/":
        return "0"
    try:
        resp = client.fs_dir_getid(normalized_path)
        if resp and resp.get("state"):
            cid = str(resp.get("id") or resp.get("cid") or "").strip()
            if cid:
                return cid
    except Exception as e:
        logger.warning(f"按路径查询 115 目录失败: path={normalized_path}, error={e}")
    return None


def _ensure_115_dir(client: P115Client, parent_cid: int, name: str, parent_path: str = "") -> str:
    expected_path = ""
    if parent_path:
        expected_path = f"{_normalize_115_remote_path(parent_path).rstrip('/')}/{str(name or '').strip()}"
        existing_cid = _get_115_dir_id_by_path(client, expected_path)
        if existing_cid:
            return existing_cid
    elif int(parent_cid) == 0:
        expected_path = f"/{str(name or '').strip()}"
        existing_cid = _get_115_dir_id_by_path(client, expected_path)
        if existing_cid:
            return existing_cid

    existing_cid = _find_child_dir_in_115(client, parent_cid, name)
    if existing_cid:
        return existing_cid

    resp = run_115_write_request_sync(
        client,
        "创建配置目录",
        lambda write_client: write_client.fs_mkdir_app(
            name,
            pid=int(parent_cid),
            app="android",
            async_=False,
        ),
        raise_on_state_false=False,
    )
    if resp and resp.get("state"):
        created_cid = str(resp.get("cid") or resp.get("id") or "")
        if created_cid:
            return created_cid
    if expected_path:
        existing_cid = _get_115_dir_id_by_path(client, expected_path)
        if existing_cid:
            return existing_cid
    existing_cid = _find_child_dir_in_115(client, parent_cid, name)
    if existing_cid:
        return existing_cid
    raise RuntimeError(f"创建 115 目录失败: parent={parent_cid}, name={name}, resp={resp}")


def _ensure_standard_topology_dirs(drive_index: int, local_media_root: str, remote_root_name: str = "影视库") -> dict:
    from pathlib import Path

    normalized_local_root = str(local_media_root or "").strip()
    if not normalized_local_root:
        raise ValueError("本地媒体根目录不能为空")

    local_root_dir = Path(normalized_local_root)
    if local_root_dir.name != remote_root_name:
        local_root_dir = local_root_dir / remote_root_name

    client = _get_115_client(drive_index)
    remote_root_path = f"/{remote_root_name}"
    remote_root_cid = _ensure_115_dir(client, 0, remote_root_name)
    media_cid = _ensure_115_dir(client, int(remote_root_cid), "媒体目录", remote_root_path)
    instant_cid = _ensure_115_dir(client, int(remote_root_cid), "秒传目录", remote_root_path)
    failed_cid = _ensure_115_dir(client, int(remote_root_cid), "失败目录", remote_root_path)
    transfer_cid = _ensure_115_dir(client, int(remote_root_cid), "转存目录", remote_root_path)
    dedup_cid = _ensure_115_dir(client, int(remote_root_cid), "重复目录", remote_root_path)
    wash_cid = _ensure_115_dir(client, int(remote_root_cid), "洗版目录", remote_root_path)

    local_media_dir = local_root_dir / "媒体库"
    local_real_library_dir = local_root_dir / "真实库"
    local_root_dir.mkdir(parents=True, exist_ok=True)
    local_media_dir.mkdir(parents=True, exist_ok=True)
    local_real_library_dir.mkdir(parents=True, exist_ok=True)

    return {
        "remote": {
            "root": {"name": f"/{remote_root_name}", "cid": str(remote_root_cid)},
            "media": {"name": f"/{remote_root_name}/媒体目录", "cid": str(media_cid)},
            "instant": {"name": f"/{remote_root_name}/秒传目录", "cid": str(instant_cid)},
            "failed": {"name": f"/{remote_root_name}/失败目录", "cid": str(failed_cid)},
            "transfer": {"name": f"/{remote_root_name}/转存目录", "cid": str(transfer_cid)},
            "dedup": {"name": f"/{remote_root_name}/重复目录", "cid": str(dedup_cid)},
            "wash": {"name": f"/{remote_root_name}/洗版目录", "cid": str(wash_cid)},
        },
        "local": {
            "root": str(local_root_dir),
            "media": str(local_media_dir),
            "real_library": str(local_real_library_dir),
        },
    }


def _resolve_existing_standard_topology_dirs(drive_index: int, local_media_root: str, remote_root_name: str = "影视库") -> dict | None:
    from pathlib import Path

    normalized_local_root = str(local_media_root or "").strip() or _default_standard_local_root()
    remote_root_name = str(remote_root_name or "影视库").strip() or "影视库"

    client = _get_115_client(drive_index)
    remote_root_path = f"/{remote_root_name}"
    remote_root_cid = _get_115_dir_id_by_path(client, remote_root_path) or _find_child_dir_in_115(client, 0, remote_root_name)
    if not remote_root_cid:
        return None

    required_dirs = {
        "media": "媒体目录",
        "failed": "失败目录",
        "transfer": "转存目录",
        "dedup": "重复目录",
        "wash": "洗版目录",
    }
    resolved: dict[str, dict[str, str]] = {}
    for key, dirname in required_dirs.items():
        cid = _get_115_dir_id_by_path(client, f"{remote_root_path}/{dirname}") or _find_child_dir_in_115(client, int(remote_root_cid), dirname)
        if not cid:
            return None
        resolved[key] = {"name": f"/{remote_root_name}/{dirname}", "cid": str(cid)}
    instant_cid = _get_115_dir_id_by_path(client, f"{remote_root_path}/秒传目录") or _find_child_dir_in_115(client, int(remote_root_cid), "秒传目录")
    resolved["instant"] = {
        "name": f"/{remote_root_name}/秒传目录",
        "cid": str(instant_cid or ""),
    }

    local_root_dir = Path(normalized_local_root)
    if local_root_dir.name != remote_root_name:
        local_root_dir = local_root_dir / remote_root_name

    return {
        "remote": {
            "root": {"name": f"/{remote_root_name}", "cid": str(remote_root_cid)},
            **resolved,
        },
        "local": {
            "root": str(local_root_dir),
            "media": str(local_root_dir / "媒体库"),
            "real_library": str(local_root_dir / "真实库"),
        },
    }


def _default_standard_local_root() -> str:
    return os.environ.get("CHILLPOSTER_LOCAL_MEDIA_ROOT") or os.path.join(os.path.expanduser("~"), "Desktop")


def _standard_topology_from_result(result: dict) -> dict:
    return {
        "mode": "standard_topology",
        "remote_root": result["remote"]["root"]["name"],
        "remote_root_cid": result["remote"]["root"]["cid"],
        "media_dir": result["remote"]["media"]["name"],
        "media_dir_cid": result["remote"]["media"]["cid"],
        "instant_dir": result["remote"]["instant"]["name"],
        "instant_dir_cid": result["remote"]["instant"]["cid"],
        "failed_dir": result["remote"]["failed"]["name"],
        "failed_dir_cid": result["remote"]["failed"]["cid"],
        "transfer_dir": result["remote"]["transfer"]["name"],
        "transfer_dir_cid": result["remote"]["transfer"]["cid"],
        "dedup_dir": result["remote"]["dedup"]["name"],
        "dedup_dir_cid": result["remote"]["dedup"]["cid"],
        "wash_dir": result["remote"]["wash"]["name"],
        "wash_dir_cid": result["remote"]["wash"]["cid"],
        "local_media_root": result["local"]["root"],
        "local_media_dir": result["local"]["media"],
        "local_real_library_dir": result["local"]["real_library"],
        "real_library_dir_name": "真实库",
    }


def _standard_topology_to_result(topology: dict) -> dict:
    return {
        "remote": {
            "root": {"name": str(topology.get("remote_root") or ""), "cid": str(topology.get("remote_root_cid") or "")},
            "media": {"name": str(topology.get("media_dir") or ""), "cid": str(topology.get("media_dir_cid") or "")},
            "instant": {"name": str(topology.get("instant_dir") or ""), "cid": str(topology.get("instant_dir_cid") or "")},
            "failed": {"name": str(topology.get("failed_dir") or ""), "cid": str(topology.get("failed_dir_cid") or "")},
            "transfer": {"name": str(topology.get("transfer_dir") or ""), "cid": str(topology.get("transfer_dir_cid") or "")},
            "dedup": {"name": str(topology.get("dedup_dir") or ""), "cid": str(topology.get("dedup_dir_cid") or "")},
            "wash": {"name": str(topology.get("wash_dir") or ""), "cid": str(topology.get("wash_dir_cid") or "")},
        },
        "local": {
            "root": str(topology.get("local_media_root") or ""),
            "media": str(topology.get("local_media_dir") or ""),
            "real_library": str(topology.get("local_real_library_dir") or ""),
        },
    }


def _is_complete_standard_topology(topology: Any) -> bool:
    if not isinstance(topology, dict):
        return False
    required = (
        "remote_root", "remote_root_cid",
        "media_dir", "media_dir_cid",
        "transfer_dir", "transfer_dir_cid",
        "failed_dir", "failed_dir_cid",
        "dedup_dir", "dedup_dir_cid",
        "wash_dir", "wash_dir_cid",
    )
    return all(str(topology.get(key) or "").strip() for key in required)


def _normalize_remote_path_for_compare(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    while "//" in text:
        text = text.replace("//", "/")
    return text.rstrip("/") or "/"


def _media_organize_needs_standard_binding(media_data: dict, topology: dict, *, topology_created: bool = False) -> bool:
    if not isinstance(media_data, dict) or not media_data:
        return True

    source_name = _normalize_remote_path_for_compare(media_data.get("source_name", ""))
    target_name = _normalize_remote_path_for_compare(media_data.get("target_name", ""))
    transfer_dir = _normalize_remote_path_for_compare(topology.get("transfer_dir", ""))
    media_dir = _normalize_remote_path_for_compare(topology.get("media_dir", ""))
    source_cid = str(media_data.get("source_cid") or "").strip()
    target_cid = str(media_data.get("target_cid") or "").strip()

    if not source_cid or source_cid == "0" or not target_cid or target_cid == "0":
        return True
    if source_name == transfer_dir and target_name == media_dir:
        return False
    if source_name and target_name and (source_name == target_name or source_name.startswith(target_name + "/")):
        return True
    if media_dir and source_name and (source_name == media_dir or source_name.startswith(media_dir + "/")):
        return True
    if source_name.startswith("/emby/"):
        return True
    if "/媒体目录/" in source_name or source_name.endswith("/媒体目录"):
        return True
    return False


def is_media_organize_source_suspicious(media_data: dict) -> bool:
    if not isinstance(media_data, dict):
        return False
    source_name = _normalize_remote_path_for_compare(media_data.get("source_name", ""))
    target_name = _normalize_remote_path_for_compare(media_data.get("target_name", ""))
    if not source_name:
        return False
    if source_name.startswith("/emby/"):
        return True
    if target_name and (source_name == target_name or source_name.startswith(target_name + "/")):
        return True
    if "/媒体目录/" in source_name or source_name.endswith("/媒体目录"):
        return True
    cfg302 = get_config_302_sync()
    topology = cfg302.get("standard_topology") if isinstance(cfg302, dict) else None
    media_dir = _normalize_remote_path_for_compare((topology or {}).get("media_dir", ""))
    return bool(media_dir and (source_name == media_dir or source_name.startswith(media_dir + "/")))


def _persist_standard_topology_dirs(result: dict):
    data = get_config_302_sync()
    data["standard_topology"] = _standard_topology_from_result(result)
    _save_config_302_sync(data)


def _apply_standard_media_organize_binding(topology_result: dict, drive_index: int) -> dict:
    from app.routers.media_organize import MediaOrganizeConfig
    from app.services.media_organize_state import CONFIG_FILE as MEDIA_ORGANIZE_CONFIG_FILE

    media_data = MediaOrganizeConfig().dict()
    if os.path.exists(MEDIA_ORGANIZE_CONFIG_FILE):
        existing = _load_json_file(MEDIA_ORGANIZE_CONFIG_FILE)
        if isinstance(existing, dict):
            media_data.update(existing)

    media_data.update({
        "drive_index": drive_index,
        "source_cid": topology_result["remote"]["transfer"]["cid"],
        "source_name": topology_result["remote"]["transfer"]["name"],
        "target_cid": topology_result["remote"]["media"]["cid"],
        "target_name": topology_result["remote"]["media"]["name"],
        "failed_cid": topology_result["remote"]["failed"]["cid"],
        "failed_name": topology_result["remote"]["failed"]["name"],
        "dedup_cid": topology_result["remote"]["dedup"]["cid"],
        "dedup_name": topology_result["remote"]["dedup"]["name"],
        "wash_cid": topology_result["remote"]["wash"]["cid"],
        "wash_name": topology_result["remote"]["wash"]["name"],
        "auto_sync_strm": True,
    })

    os.makedirs(os.path.dirname(MEDIA_ORGANIZE_CONFIG_FILE), exist_ok=True)
    with open(MEDIA_ORGANIZE_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(media_data, f, ensure_ascii=False, indent=4)
    return media_data


def ensure_standard_topology_binding(reason: str = "", *, sync_media_organize: bool = True, create_missing: bool = False) -> dict | None:
    """Bind to an existing complete one-stop topology; create only for explicit setup flows."""
    data = get_config_302_sync()
    drives = data.get("drives") if isinstance(data.get("drives"), list) else []
    drive = drives[0] if drives else {}
    if not bool(drive.get("enable_standard_topology")):
        return None

    topology = data.get("standard_topology") if isinstance(data, dict) else None
    topology_created = False
    if not _is_complete_standard_topology(topology):
        local_media_root = str(drive.get("local_media_root") or "").strip() or _default_standard_local_root()
        remote_root_name = str(drive.get("remote_root_name") or "影视库").strip() or "影视库"
        result = (
            _ensure_standard_topology_dirs(
                drive_index=0,
                local_media_root=local_media_root,
                remote_root_name=remote_root_name,
            )
            if create_missing
            else _resolve_existing_standard_topology_dirs(
                drive_index=0,
                local_media_root=local_media_root,
                remote_root_name=remote_root_name,
            )
        )
        if not result:
            logger.warning(
                f"[302] 未找到完整一条龙目录，跳过媒体整理自动绑定: "
                f"reason={reason or 'auto'} root=/{remote_root_name}"
            )
            return None
        topology = _standard_topology_from_result(result)
        data["standard_topology"] = topology
        drive["local_media_root"] = result["local"]["root"]
        drive["remote_root_name"] = remote_root_name
        if str((result["remote"].get("instant") or {}).get("cid") or "").strip():
            drive["upload_dir"] = result["remote"]["instant"]["name"]
        drive["transfer_dir"] = result["remote"]["transfer"]["name"]
        drive["transfer_drive_index"] = 0
        data["drives"] = [drive]
        _save_config_302_sync(data)
        topology_created = True
        logger.info(f"[302] 已识别一条龙标准目录拓扑: reason={reason or 'auto'} create_missing={create_missing}")
    else:
        result = _standard_topology_to_result(topology)

    if sync_media_organize:
        from app.services.media_organize_state import CONFIG_FILE as MEDIA_ORGANIZE_CONFIG_FILE
        media_data = _load_json_file(MEDIA_ORGANIZE_CONFIG_FILE)
        if _media_organize_needs_standard_binding(media_data, topology, topology_created=topology_created):
            source_before = str(media_data.get("source_name") or "") if isinstance(media_data, dict) else ""
            media_data = _apply_standard_media_organize_binding(result, 0)
            logger.warning(
                f"[302] 已修正媒体整理目录为一条龙标准目录: "
                f"reason={reason or 'auto'} source_before={source_before or '-'} "
                f"source_now={media_data.get('source_name')}"
            )
    return topology


def apply_standard_topology_binding_from_result(topology_result: dict, drive_index: int, reason: str = "") -> dict:
    data = get_config_302_sync()
    drives = data.get("drives") if isinstance(data.get("drives"), list) else []
    drive = drives[0] if drives else _normalize_single_drive_config({})
    topology = _standard_topology_from_result(topology_result)
    data["standard_topology"] = topology
    drive["local_media_root"] = topology_result["local"]["root"]
    drive["remote_root_name"] = str(topology_result["remote"]["root"]["name"] or "/影视库").strip("/").split("/", 1)[0] or "影视库"
    drive["upload_dir"] = topology_result["remote"]["instant"]["name"]
    drive["transfer_dir"] = topology_result["remote"]["transfer"]["name"]
    drive["transfer_drive_index"] = drive_index
    data["drives"] = [drive]
    _save_config_302_sync(data)
    media_data = _apply_standard_media_organize_binding(topology_result, drive_index)
    logger.info(
        f"[302] 已应用一条龙目录绑定: reason={reason or 'setup'} "
        f"source={media_data.get('source_name')} target={media_data.get('target_name')}"
    )
    return media_data


def _derive_standard_strm_url_base(config_302_data: dict, drive_index: int, existing_task: Optional[dict] = None) -> str:
    from app.routers.strm import derive_strm_url_base
    return derive_strm_url_base(config_302_data, drive_index)



def _apply_standard_strm_binding(topology_result: dict, drive_index: int, config_302_data: Optional[dict] = None) -> dict:
    from app.routers.strm import StrmSyncTask, CONFIG_FILE as STRM_CONFIG_FILE

    config_302_data = config_302_data if isinstance(config_302_data, dict) else get_config_302_sync()
    strm_data = {"sync_tasks": []}
    existing_tasks = []
    if os.path.exists(STRM_CONFIG_FILE):
        existing = _load_json_file(STRM_CONFIG_FILE)
        if isinstance(existing, dict):
            strm_data.update(existing)
            if isinstance(existing.get("sync_tasks"), list):
                existing_tasks = [task for task in existing.get("sync_tasks", []) if isinstance(task, dict)]

    canonical_remote_path = topology_result["remote"]["media"]["name"]
    canonical_local_path = topology_result["local"]["media"]
    task_name = "标准媒体库同步"

    base_task = None
    for task in existing_tasks:
        if str(task.get("remote_path") or "").rstrip("/") == canonical_remote_path.rstrip("/"):
            base_task = task
            break
    if base_task is None and existing_tasks:
        base_task = existing_tasks[0]

    canonical_task = StrmSyncTask().dict()
    if isinstance(base_task, dict):
        canonical_task.update(base_task)

    canonical_task.update({
        "name": task_name,
        "drive_index": drive_index,
        "remote_path": canonical_remote_path,
        "local_path": canonical_local_path,
    })
    canonical_task["strm_url_base"] = _derive_standard_strm_url_base(config_302_data, drive_index, existing_task=canonical_task)

    preserved_tasks = []
    for task in existing_tasks:
        if str(task.get("name") or "").strip() == task_name:
            continue
        if str(task.get("remote_path") or "").rstrip("/") == canonical_remote_path.rstrip("/"):
            continue
        preserved_tasks.append(task)

    strm_data["sync_tasks"] = [canonical_task, *preserved_tasks]

    os.makedirs(os.path.dirname(STRM_CONFIG_FILE), exist_ok=True)
    with open(STRM_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(strm_data, f, ensure_ascii=False, indent=4)
    return strm_data


def _apply_standard_rss_binding(topology_result: dict) -> dict:
    from core.configs import RSS_CONFIG_FILE

    rss_data = _load_json_file(RSS_CONFIG_FILE)
    if not isinstance(rss_data, dict):
        rss_data = {}
    rss_data.update({
        "source_root": topology_result["local"]["media"],
        "link_root": topology_result["local"]["real_library"],
    })

    os.makedirs(os.path.dirname(RSS_CONFIG_FILE), exist_ok=True)
    with open(RSS_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(rss_data, f, ensure_ascii=False, indent=4)
    return rss_data


def _normalize_115_qr_status(resp: dict) -> tuple[str, str]:
    data = (resp or {}).get("data") or {}
    status = data.get("status")
    if status == 0:
        return "waiting", STATUS_MESSAGES["waiting"]
    if status == 1:
        return "scanned", STATUS_MESSAGES["scanned"]
    if status == 2:
        return "confirmed", STATUS_MESSAGES["confirmed"]
    if status == -1:
        return "expired", STATUS_MESSAGES["expired"]
    if status == -2:
        return "cancelled", STATUS_MESSAGES["cancelled"]
    message = (resp or {}).get("error") or (resp or {}).get("message") or STATUS_MESSAGES["error"]
    return "error", message


def get_config_302_sync() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return _normalize_config_302_data({})
    data = _load_json_file(CONFIG_FILE)
    return _normalize_config_302_data(data)


def get_primary_drive_config_sync() -> dict:
    data = get_config_302_sync()
    drives = data.get('drives', []) if isinstance(data.get('drives'), list) else []
    return drives[0] if drives else _normalize_single_drive_config({})


def get_primary_emby_config_sync() -> dict:
    data = get_config_302_sync()
    embys = data.get('embys', []) if isinstance(data.get('embys'), list) else []
    return embys[0] if embys else _normalize_single_emby_config({})


def get_emby_configs_sync() -> List[dict]:
    emby = get_primary_emby_config_sync()
    return [{
        'name': emby.get('name', ''),
        'url': emby.get('url', ''),
        'key': emby.get('key', ''),
        'public_host': emby.get('public_host', ''),
        'enabled': bool(emby.get('enabled', True)),
    }]


def get_emby_config_by_index_sync(server_idx: int) -> Optional[dict]:
    if server_idx < 0:
        return None
    return get_primary_emby_config_sync()


# ==========================================
# 2. 路由逻辑
# ==========================================

@router.get("/get")
async def get_config_302():
    """读取 302 配置"""
    try:
        return get_config_302_sync()
    except Exception as e:
        logger.error(f"读取 302 配置失败: {e}")
        return {}

@router.post("/save")
async def save_config_302(config: Config302Payload):
    """保存 302 配置"""
    try:
        old_data = get_config_302_sync()
        save_data = _normalize_config_302_data(config.dict())
        old_drive = get_primary_drive_config_sync()
        new_drive = save_data["drives"][0]

        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        _save_config_302_sync(save_data)

        logger.info("============ 302 配置已更新 ============")
        sync_status = "✅ 开启" if new_drive.get("enable_sync") else "⭕ 关闭"
        rapid_status = "✅ 开启" if new_drive.get("enable_rapid") else "⭕ 关闭"
        logger.info(f"[账号: {new_drive.get('name', '115')}] 同播复制: {sync_status} | 秒传模式: {rapid_status}")
        if new_drive.get("enable_sync"):
            logger.info("    └─ ⚡ 同播复制策略已生效: 多人观看同一视频时自动生成副本")
        logger.info("=======================================")

        try:
            from app.services.task_service import task_service_instance
            task_service_instance.refresh_cleanup_jobs()
        except Exception as e:
            logger.error(f"刷新清理任务失败: {e}")

        topology_result = None
        monitor_should_refresh = False
        monitor_refresh_reason = []
        try:
            from app.services.drive115_service import drive115_service
            from app.routers.media_organize import MediaOrganizeConfig, _toggle_life_monitor, CONFIG_FILE as MEDIA_ORGANIZE_CONFIG_FILE

            old_cookie = str(old_drive.get('cookie') or '').strip()
            new_cookie = str(new_drive.get('cookie') or '').strip()
            changed_cookies = []
            if old_cookie != new_cookie:
                if old_cookie:
                    changed_cookies.append(old_cookie)
                if new_cookie:
                    changed_cookies.append(new_cookie)

            old_media_organize_data = {}
            if os.path.exists(MEDIA_ORGANIZE_CONFIG_FILE):
                existing_media_organize_data = _load_json_file(MEDIA_ORGANIZE_CONFIG_FILE)
                if isinstance(existing_media_organize_data, dict):
                    old_media_organize_data = existing_media_organize_data

            if changed_cookies:
                drive115_service.invalidate_clients(cookies=changed_cookies)
                monitor_should_refresh = True
                monitor_refresh_reason.append('cookie_changed')

            if new_drive.get('enable_standard_topology'):
                local_media_root = _default_standard_local_root()
                remote_root_name = str(new_drive.get('remote_root_name') or '影视库').strip() or '影视库'
                new_drive['local_media_root'] = local_media_root
                new_drive['remote_root_name'] = remote_root_name
                topology_result = _ensure_standard_topology_dirs(
                    drive_index=0,
                    local_media_root=local_media_root,
                    remote_root_name=remote_root_name,
                )
                new_drive['upload_dir'] = topology_result['remote']['instant']['name']
                new_drive['transfer_dir'] = topology_result['remote']['transfer']['name']
                new_drive['transfer_drive_index'] = 0
                save_data["drives"] = [new_drive]
                save_data["standard_topology"] = _standard_topology_from_result(topology_result)
                _save_config_302_sync(save_data)
                media_organize_data = apply_standard_topology_binding_from_result(topology_result, 0, reason="config_302_save")
                _apply_standard_strm_binding(topology_result, 0, config_302_data=save_data)
                _apply_standard_rss_binding(topology_result)

                monitor_fields = ('source_name', 'target_name', 'source_cid', 'target_cid')
                if any(str(old_media_organize_data.get(field, '') or '') != str(media_organize_data.get(field, '') or '') for field in monitor_fields):
                    monitor_should_refresh = True
                    monitor_refresh_reason.append('monitor_dirs_changed')

            if monitor_should_refresh and os.path.exists(MEDIA_ORGANIZE_CONFIG_FILE):
                refreshed_media_organize_data = _load_json_file(MEDIA_ORGANIZE_CONFIG_FILE)
                if isinstance(refreshed_media_organize_data, dict):
                    refreshed_media_organize_data['drive_index'] = 0
                    media_organize_config = MediaOrganizeConfig(**refreshed_media_organize_data)
                    if media_organize_config.life_monitor_enabled:
                        logger.info(f"[302] 标准拓扑保存后刷新 Life 监控: reason={'+'.join(monitor_refresh_reason) or 'unknown'}")
                        await _toggle_life_monitor(True, media_organize_config, force_restart=True)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"刷新 115 客户端缓存失败: {e}")
            raise HTTPException(status_code=500, detail=f"保存成功，但自动创建标准目录失败: {str(e)}")

        message = '配置已保存'
        if topology_result:
            message = '配置已保存，115 一条龙目录已就绪'
        return {"status": "success", "message": message, "standard_topology": topology_result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")

@router.post("/save_emby")
async def save_emby_config(payload: SaveEmbyPayload):
    """仅保存 Emby 配置，不触发一条龙目录创建"""
    try:
        data = get_config_302_sync()
        normalized = [e.dict() for e in payload.embys]
        data["embys"] = normalized
        _save_config_302_sync(data)
        logger.info("[302] Emby 配置已保存")
        return {"status": "success", "message": "Emby 配置已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")

@router.post("/test_115")
async def test_115_cookie(payload: Test115Payload):
    """测试 115 Cookie 有效性"""
    if not payload.cookie:
        return {"status": "error", "message": "Cookie 为空"}

    try:
        result = probe_115_cookie(payload.cookie)
        if result.get("status") == "ok":
            return {
                "status": "ok",
                "message": result.get("message") or "连接成功! Cookie 有效",
                "login_app": result.get("login_app") or "",
                "login_app_label": result.get("login_app_label") or "",
            }
        return {"status": "error", "message": result.get("message") or "Cookie 无效或已过期"}

    except Exception as e:
        return {"status": "error", "message": f"连接异常: {str(e)}"}


@router.post("/115_qrcode/start")
async def start_115_qrcode(payload: Start115QrPayload):
    """生成 115 扫码登录二维码"""
    app = _normalize_115_qr_app(payload.app)
    try:
        resp = P115Client.login_qrcode_token(app=app)
        data = (resp or {}).get("data") or {}
        uid = data.get("uid")
        if not uid:
            message = resp.get("error") or resp.get("message") or "获取二维码失败"
            return {"status": "error", "message": message}

        qrcode_url = data.get("qrcode") or f"https://115.com/scan/dg-{uid}"

        qrcode_bytes = P115Client.login_qrcode(uid, app="web")
        if not qrcode_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            qrcode_bytes = P115Client.login_qrcode(uid)
        if not qrcode_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return {
                "status": "error",
                "message": "二维码图片生成失败，请切换客户端后重试",
            }

        qrcode_base64 = base64.b64encode(qrcode_bytes).decode("ascii")

        return {
            "status": "ok",
            "message": "二维码已生成，请使用 115 App 扫码",
            "app": app,
            "app_name": SUPPORTED_115_QR_APPS[app],
            "token": {
                "uid": uid,
                "time": data.get("time"),
                "sign": data.get("sign"),
            },
            "qrcode_url": qrcode_url,
            "qrcode": f"data:image/png;base64,{qrcode_base64}",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"生成 115 扫码二维码失败: {e}")
        return {"status": "error", "message": f"生成二维码失败: {str(e)}"}


@router.post("/115_qrcode/status")
async def get_115_qrcode_status(payload: Status115QrPayload):
    """查询 115 扫码登录状态"""
    try:
        resp = P115Client.login_qrcode_scan_status({
            "uid": payload.uid,
            "time": payload.time,
            "sign": payload.sign,
        })
        scan_status, message = _normalize_115_qr_status(resp)
        return {
            "status": "ok",
            "scan_status": scan_status,
            "message": message,
            "raw": resp,
        }
    except Exception as e:
        logger.error(f"查询 115 扫码状态失败: {e}")
        return {"status": "error", "scan_status": "error", "message": f"查询扫码状态失败: {str(e)}"}


@router.post("/115_qrcode/result")
async def get_115_qrcode_result(payload: Result115QrPayload):
    """获取 115 扫码登录结果并提取 Cookie"""
    app = _normalize_115_qr_app(payload.app)
    try:
        resp = P115Client.login_qrcode_scan_result(payload.uid, app=app)
        cookie = _extract_cookie_from_scan_result(resp)
        if not cookie:
            message = resp.get("error") or resp.get("message") or "扫码成功，但未能提取 Cookie"
            return {
                "status": "error",
                "message": message,
                "raw": resp,
            }

        return {
            "status": "ok",
            "message": "扫码登录成功，Cookie 已获取",
            "cookie": cookie,
        }
    except Exception as e:
        logger.error(f"获取 115 扫码登录结果失败: {e}")
        return {"status": "error", "message": f"获取扫码结果失败: {str(e)}"}


class ManualCleanupPayload(BaseModel):
    drive_index: int = 0
    account_type: str = "main"  # "main" 或 "rapid"
    account_index: int = 0

class StandardTopologyDirsPayload(BaseModel):
    drive_index: int = 0
    local_media_root: str
    remote_root_name: str = "影视库"

@router.post("/ensure_standard_topology_dirs")
async def ensure_standard_topology_dirs(payload: StandardTopologyDirsPayload):
    """创建标准目录拓扑（第一步：仅目录创建）"""
    try:
        result = _ensure_standard_topology_dirs(
            drive_index=payload.drive_index,
            local_media_root=payload.local_media_root,
            remote_root_name=str(payload.remote_root_name or "影视库").strip() or "影视库",
        )
        _persist_standard_topology_dirs(result)
        return {
            "status": "ok",
            "message": "标准目录创建完成",
            **result,
        }
    except Exception as e:
        logger.error(f"创建标准目录拓扑失败: {e}")
        return {"status": "error", "message": f"创建标准目录失败: {str(e)}"}


@router.post("/manual_signin_all")
async def manual_signin_all():
    """手动触发一次 115 批量签到"""
    from app.services.drive115_service import drive115_service

    try:
        results = await drive115_service.execute_all_signin_tasks(trigger="manual")
        total = len(results)
        success = sum(1 for item in results if item.get("status") == "success")
        already = sum(1 for item in results if item.get("status") == "already")
        failed = sum(1 for item in results if item.get("status") == "failed")
        return {
            "status": "ok",
            "message": f"签到完成：成功 {success}，已签 {already}，失败 {failed}",
            "total": total,
            "success": success,
            "already": already,
            "failed": failed,
        }
    except Exception as e:
        return {"status": "error", "message": f"签到失败: {str(e)}"}


@router.post("/manual_cleanup")
async def manual_cleanup(payload: ManualCleanupPayload):
    """手动触发 115 清理任务（删除目录 + 清空回收站）"""
    from app.services.drive115_service import drive115_service

    # 读取配置
    config_path = "config/config_302.json"
    if not os.path.exists(config_path):
        return {"status": "error", "message": "配置文件不存在"}

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        drives = data.get("drives", [])
        if not drives:
            return {"status": "error", "message": "没有配置 115 账号"}

        if payload.drive_index >= len(drives):
            return {"status": "error", "message": "账号索引超出范围"}

        drive_config = drives[payload.drive_index]

        # 执行清理
        await drive115_service.execute_cleanup_task(
            drive_config,
            payload.account_type,
            payload.account_index
        )

        account_name = ""
        if payload.account_type == "main":
            account_name = drive_config.get("name", f"主号{payload.drive_index + 1}")
        else:
            rapid_accounts = drive_config.get("rapid_accounts", [])
            if payload.account_index < len(rapid_accounts):
                account_name = rapid_accounts[payload.account_index].get("name", f"小号{payload.account_index + 1}")

        return {"status": "ok", "message": f"清理完成: {account_name}"}

    except Exception as e:
        return {"status": "error", "message": f"清理失败: {str(e)}"}
