# app/schemas.py
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

class LoginRequest(BaseModel):
    username: str
    password: str

class ChangeAuthRequest(BaseModel):
    old_password: str
    new_username: str
    new_password: str

class EmbyConnectionPayload(BaseModel):
    url: str
    key: str
    public_host: str = None

class ConnectionRequest(EmbyConnectionPayload):
    pass

class PreviewRequest(EmbyConnectionPayload):
    library_id: str
    config: dict
    image_data: str = None
    custom_assets: dict = None
    mode: str = "random"

class SuiteBackupRequest(EmbyConnectionPayload):
    suite_name: str

class SuiteRestoreRequest(EmbyConnectionPayload):
    suite_name: str
    target_ids: list = []

class SuiteContentRequest(BaseModel):
    suite_name: str

class EmbySearchRequest(EmbyConnectionPayload):
    query: str
    library_id: str = None
    type: str = "Primary"

class EmbyItemImagesRequest(EmbyConnectionPayload):
    item_id: str
    type: str = "Backdrop"

class EmbyRandomPoolRequest(EmbyConnectionPayload):
    library_id: str
    type: str = "Backdrop"
    limit: int = 50

class TaskTarget(BaseModel):
    server_idx: int = 0
    library_id: str
    library_name: str = "Unknown"
    url: Optional[str] = None
    key: Optional[str] = None
    public_host: Optional[str] = None

class RunTaskRequest(BaseModel):
    preset_filename: str
    targets: list[TaskTarget]
    mode: str = "random"

class CreateTaskRequest(BaseModel):
    name: str
    cron: str
    preset_filename: str
    targets: list[TaskTarget]
    mode: str = "random"
    enabled: bool = True 

class UpdateTaskRequest(BaseModel):
    id: str
    name: str
    cron: str
    preset_filename: str
    targets: list[TaskTarget]
    mode: str = "random"
    enabled: bool = True 

class RunSavedTaskRequest(BaseModel):
    id: str

class RssGlobalConfig(BaseModel):
    source_root: str 
    link_root: str

class RssTaskModel(BaseModel):
    id: str = None
    name: str
    rss_url: str
    cron: str
    target_server_idx: int
    content_type: str = "movies"
    enabled: bool = True
    last_entries: List[str] = Field(default_factory=list)
    entry_tmdb_map: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    last_sync_at: Optional[float] = None

class UpdateRssTaskRequest(BaseModel):
    id: str
    name: str
    rss_url: str
    cron: str
    target_server_idx: int
    content_type: str = "movies"
    enabled: bool = True

class WebhookConfigModel(BaseModel):
    enabled: bool = False
    engine: str = "classic"
    preset: str = ""
    mode: str = "random"

class WechatNotifyConfigModel(BaseModel):
    enabled: bool = False
    name: str = "微信"
    channel_name: str = ""
    corp_id: str = ""
    app_secret: str = ""
    token: str = ""
    agent_id: str = ""
    proxy_url: str = ""
    encoding_aes_key: str = ""
    admin_whitelist: str = ""
    notify_types: dict = {
        "playback": True,
        "media_added": True,
        "checkin": True,
        "task_complete": True
    }
    templates: dict = {}

class TelegramNotifyConfigModel(BaseModel):
    enabled: bool = False
    name: str = "Telegram"
    bot_token: str = ""
    chat_id: str = ""
    account_monitor_enabled: bool = False
    api_id: str = ""
    api_hash: str = ""
    phone: str = ""
    selected_dialogs: List[Dict[str, Any]] = Field(default_factory=list)
    monitor_reply_enabled: bool = False
    transfer_dir_mode: str = "system"
    transfer_dir: str = ""
    notify_types: dict = {
        "playback": True,
        "media_added": True,
        "organize_complete": True,
        "resource_transfer": True,
        "checkin": True,
        "task_complete": True
    }
    templates: dict = {}

class TelegramSendCodeRequest(BaseModel):
    api_id: str = ""
    api_hash: str = ""
    phone: str = ""

class TelegramSignInRequest(BaseModel):
    code: str = ""
    password: str = ""

class TelegramDialogsRequest(BaseModel):
    selected_dialogs: List[Dict[str, Any]] = Field(default_factory=list)

class ToggleTaskRequest(BaseModel):
    id: str
    enabled: bool
