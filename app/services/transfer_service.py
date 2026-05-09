# app/services/transfer_service.py
import re
import json
import os
from datetime import datetime
from p115client.util import share_extract_payload
from app.services.drive115_service import drive115_service
from app.routers.config_302 import get_config_302
from core.logger import logger
from app.services.media_organize_115_ops import run_115_write_request_sync

# 115 链接正则
RE_115_LINK = re.compile(
    r'https?://(?:115\.com/s/|115cdn\.com/s/|share\.115\.com/|anxia\.com/\S*?)'
    r'[a-zA-Z0-9]+(?:\?password=[a-zA-Z0-9]+)?'
)

SOURCE_NAMES = {
    "wechat": "企业微信",
    "telegram": "Telegram",
    "manual": "手动",
}

HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config", "transfer_history.json"
)


class TransferService:
    def __init__(self):
        self._max_history = 200
        self._history = self._load_history()

    def _load_history(self) -> list:
        """从文件加载转存记录"""
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data[:self._max_history]
            except Exception as e:
                logger.error(f"[转存] 加载历史记录失败: {e}")
        return []

    def _save_history(self):
        """持久化转存记录到文件"""
        try:
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[转存] 保存历史记录失败: {e}")

    def extract_links(self, text: str) -> list[str]:
        """从消息文本中提取所有 115 分享链接"""
        if not text:
            return []
        links = RE_115_LINK.findall(text)
        # 也尝试提取纯 share_code-receive_code 格式
        stripped = text.strip()
        if not links and re.fullmatch(r'[a-zA-Z0-9]{6,20}-[a-zA-Z0-9]{4,20}', stripped):
            links = [stripped]
        return links

    async def process_link(self, link: str, source: str = "manual") -> dict:
        """
        处理单条 115 分享链接，执行转存

        Returns:
            {success: bool, status: str, name: str, link: str, share_code: str, message: str}
        """
        # 1. 解析链接
        try:
            payload = share_extract_payload(link)
        except ValueError as e:
            result = {
                "success": False,
                "status": "解析失败",
                "name": "",
                "link": link,
                "share_code": "",
                "message": f"链接解析失败: {e}",
            }
            self._add_history(result, source, link)
            return result

        share_code = payload.get("share_code", "")
        receive_code = payload.get("receive_code") or ""

        # 2. 获取 115 客户端
        cfg = await get_config_302()
        drives = cfg.get("drives", [])
        drive_index = 0
        if isinstance(drives, list) and len(drives) > 0:
            drive_index = drives[0].get("transfer_drive_index", 0)

        client, drive_cfg = await drive115_service.get_client(drive_index)
        if not client:
            result = {
                "success": False,
                "status": "客户端错误",
                "name": "",
                "link": link,
                "share_code": share_code,
                "message": "115 客户端未配置",
            }
            self._add_history(result, source, link)
            return result

        # 3. 获取目标目录 CID：仅使用 302 转存配置 transfer_dir
        cid = 0
        transfer_dir = ""
        if isinstance(drives, list) and len(drives) > 0:
            d = drives[0]
            transfer_dir = str(d.get("transfer_dir", "")).strip()

        if transfer_dir:
            if transfer_dir.isdigit():
                cid = int(transfer_dir)
            else:
                # 逐级查询/创建目录
                parts = [p for p in transfer_dir.strip("/").split("/") if p]
                if not parts:
                    cid = 0
                else:
                    try:
                        for i, part in enumerate(parts):
                            current_path = "/" + "/".join(parts[:i + 1])
                            dir_info = client.fs_dir_getid_app(current_path)
                            if dir_info and dir_info.get("id"):
                                cid = dir_info["id"]
                            else:
                                # 目录不存在，创建
                                mkdir_resp = run_115_write_request_sync(
                                    client,
                                    "创建转存目录",
                                    lambda write_client, part=part: write_client.fs_mkdir_app(part, app="android", async_=False),
                                    raise_on_state_false=False,
                                )
                                if mkdir_resp and mkdir_resp.get("state"):
                                    dir_info = client.fs_dir_getid_app(current_path)
                                    cid = dir_info.get("id", 0) if dir_info else 0
                                else:
                                    err = mkdir_resp.get("error", "未知错误") if mkdir_resp else "无响应"
                                    logger.warning(f"[转存] 创建目录失败 {current_path}: {err}")
                                    cid = 0
                                    break
                        logger.info(f"[转存] 目录就绪: {transfer_dir} (CID={cid})")
                    except Exception as e:
                        logger.warning(f"[转存] 目录操作异常 ({transfer_dir}): {e}，将转存到根目录")
                        cid = 0

        # 4. 调用 share_receive
        try:
            resp = client.share_receive({
                "share_code": share_code,
                "receive_code": receive_code,
                "file_id": "0",
                "cid": cid,
            })
            logger.info(f"[转存] share_receive 返回: {json.dumps(resp, ensure_ascii=False)}")
        except Exception as e:
            logger.error(f"[转存] share_receive 调用失败: {e}")
            result = {
                "success": False,
                "status": "转存失败",
                "name": "",
                "link": link,
                "share_code": share_code,
                "message": f"115 接口调用失败: {e}",
            }
            self._add_history(result, source, link)
            return result

        # 5. 解析结果
        state = resp.get("state", False)
        error_msg = resp.get("error_msg", "") or resp.get("error", "") or resp.get("message", "") or resp.get("msg", "")
        data = resp.get("data", {}) or resp.get("result", {}) or {}

        # 尝试从响应中提取文件/文件夹名称
        name = ""
        if isinstance(data, dict):
            # 优先取文件夹/文件名称
            name = (data.get("receive_title", "")
                    or data.get("file_name", "")
                    or data.get("name", "")
                    or "")
        if not name and isinstance(data, list) and data:
            # 如果 data 是列表，取第一个元素的名称
            first = data[0] if isinstance(data[0], dict) else {}
            name = first.get("file_name", "") or first.get("name", "") or ""
        if not name:
            # 从链接中提取一个可读的名字
            name = share_code

        if state:
            # 成功
            result = {
                "success": True,
                "status": "转存成功",
                "name": name,
                "link": link,
                "share_code": share_code,
                "message": f"转存成功 (115)\n名称: {name}\n链接: {link}",
            }
        else:
            # 失败（包括已存在的情况）
            result = {
                "success": False,
                "status": "转存失败",
                "name": "",
                "link": link,
                "share_code": share_code,
                "message": f"转存失败 (115)\n链接: {link}\n原因: {error_msg or '未知错误'}",
            }

        self._add_history(result, source, link)
        return result

    def _add_history(self, result: dict, source: str, link: str):
        """添加转存记录"""
        self._history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": SOURCE_NAMES.get(source, source),
            "link": link,
            "status": result.get("status", ""),
            "name": result.get("name", ""),
            "success": result.get("success", False),
            "share_code": result.get("share_code", ""),
        })
        if len(self._history) > self._max_history:
            self._history = self._history[:self._max_history]
        self._save_history()

    def get_history(self) -> list:
        """获取转存历史记录"""
        return self._history

    def clear_history(self):
        """清空转存记录"""
        self._history = []
        self._save_history()


# 单例
transfer_service = TransferService()
