import os
import re
import json
from collections import Counter
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from core.configs import APP_LOG_FILE
from core.logger import sanitize_log_text, should_hide_console_log_line
from app.services.organize_history_service import list_organize_history

router = APIRouter(prefix="/api/organize-history", tags=["OrganizeHistory"])

MAX_SOURCE_LINES = 20000
MAX_RESPONSE_LIMIT = 500
DEFAULT_PAGE_SIZE = 50

LOG_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})\s+-\s+"
    r"(?P<level>[A-Z]+)\s+-\s+(?P<message>.*)$"
)

CATEGORY_DEFS = [
    {"key": "organize_success", "label": "整理成功", "icon": "fa-circle-check", "tone": "success"},
    {"key": "organize_failed", "label": "整理失败", "icon": "fa-circle-xmark", "tone": "danger"},
    {"key": "wash_success", "label": "洗版成功", "icon": "fa-arrows-rotate", "tone": "success"},
    {"key": "wash_failed", "label": "洗版失败", "icon": "fa-triangle-exclamation", "tone": "warning"},
    {"key": "sha1_duplicate", "label": "SHA1重复", "icon": "fa-copy", "tone": "warning"},
    {"key": "strm_generated", "label": "STRM生成", "icon": "fa-file-code", "tone": "info"},
    {"key": "skipped", "label": "跳过记录", "icon": "fa-forward", "tone": "muted"},
]

CATEGORY_MAP = {item["key"]: item for item in CATEGORY_DEFS}


def _existing_log_paths() -> list[str]:
    paths: list[str] = []
    for idx in range(3, 0, -1):
        path = f"{APP_LOG_FILE}.{idx}"
        if os.path.exists(path):
            paths.append(path)
    if os.path.exists(APP_LOG_FILE):
        paths.append(APP_LOG_FILE)
    return paths


def _read_tail_lines(max_lines: int = MAX_SOURCE_LINES) -> list[str]:
    lines: list[str] = []
    for path in _existing_log_paths():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fp:
                lines.extend(fp.readlines())
        except OSError:
            continue
    return lines[-max_lines:]


def _strip_prefix(message: str) -> str:
    message = re.sub(r"^\[[^\]]+\]\s*", "", message).strip()
    return message


def _short_text(value: str, max_len: int = 120) -> str:
    value = (value or "").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def _looks_like_media_record(message: str) -> bool:
    return bool(
        re.search(r"\.(mkv|mp4|mov|avi|ts|strm)\b", message, re.I)
        or re.search(r"S\d{1,2}E\d{1,3}", message, re.I)
        or re.search(r"\{tmdb-\d+\}", message, re.I)
        or re.search(r"《[^》]+》", message)
    )


def _pick_value(message: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        patterns = (
            rf"{re.escape(label)}[:：]\s*([^|；\n]+)",
            rf"{re.escape(label)}=([^|；\n]+)",
        )
        for pattern in patterns:
            matched = re.search(pattern, message)
            if matched:
                return _short_text(matched.group(1), 180)
    return ""


def _extract_title(message: str) -> str:
    quoted = re.search(r"《([^》]+)》", message)
    if quoted:
        return _short_text(quoted.group(1), 80)

    tmdb_folder = re.search(r"([^/|；]+?\(\d{4}\)\s*\{tmdb-\d+\})", message)
    if tmdb_folder:
        return _short_text(tmdb_folder.group(1), 90)

    for labels in (
        ("标题", "片名", "剧名"),
        ("新文件", "源媒体文件", "新媒体文件", "文件"),
        ("路径", "库位", "目录"),
    ):
        value = _pick_value(message, labels)
        if value:
            basename = value.replace("\\", "/").split("/")[-1]
            basename = re.sub(r"\.(mkv|mp4|mov|avi|ts|strm|nfo|jpg|png|webp)$", "", basename, flags=re.I)
            return _short_text(basename, 90)

    cleaned = _strip_prefix(message)
    cleaned = cleaned.split("|", 1)[0].split("；", 1)[0]
    return _short_text(cleaned, 90) or "整理记录"


def _extract_subtitle(message: str) -> str:
    parts: list[str] = []
    tmdb = re.search(r"(?:TMDb编号|TMDb|tmdb)[：:\s-]*(\d+)", message, re.I)
    if tmdb:
        parts.append(f"TMDb {tmdb.group(1)}")
    season = re.search(r"(S\d{1,2}(?:E\d{1,3})?)", message, re.I)
    if season:
        parts.append(season.group(1).upper())
    reason = _pick_value(message, ("原因", "判断原因", "结果"))
    if reason:
        parts.append(reason)
    return " / ".join(parts[:3])


def _classify(message: str, level: str) -> str | None:
    text = message
    noisy_keywords = (
        "[微信通知]",
        "[Telegram",
        "图文发送成功",
        "源目录事件",
        "转存后媒体整理完成",
        "整理任务正在运行",
        "阶段1/4",
        "已加载媒体库缓存SHA1索引",
        "SQLite 缓存",
        "TMDb详情已缓存",
        "TMDb 详情已缓存",
        "元数据下载完成",
        "全量 TMDb 元数据补齐",
        "整理完成: 成功",
        "移动失败目录",
        "移动失败文件:",
        "开始统一移动失败文件/目录",
        "统一移动失败文件/目录",
        "洗版未通过文件批量移动",
        "开始批量移动洗版未通过文件",
        "批量移动洗版未通过文件",
        "已暂存洗版未通过文件",
        "已跳过整理自身产生的事件",
        "跳过整理自身产生的事件",
        "保留旧文件，跳过入库",
    )
    if any(keyword in text for keyword in noisy_keywords):
        return None

    if "洗版" in text:
        if any(word in text for word in ("失败", "保留旧", "未通过", "质量更高", "等效体积不足")):
            return "wash_failed"
        if any(word in text for word in ("成功", "替换旧", "通过洗版")):
            return "wash_success"
        if "命中洗版候选" in text:
            return "wash_success"

    if "[Wash]" in text or text.startswith("保留旧文件"):
        if any(word in text for word in ("保留旧", "未通过", "质量更高", "等效体积不足")):
            return "wash_failed"

    if "SHA1" in text and any(word in text for word in ("重复", "已存在", "跳过")):
        return "sha1_duplicate"

    if "STRM" in text and any(word in text for word in ("新生成STRM", "已生成STRM", "STRM生成", "全量同步完成")):
        return "strm_generated"

    if any(word in text for word in ("电影整理完成", "剧集整理完成", "入库成功")):
        if any(word in text for word in ("失败", "ERROR", "异常")):
            return "organize_failed"
        return "organize_success"

    if re.search(r"失败\s+\d+/\d+[:：]", text) and "原因" in text:
        return "organize_failed"

    if any(word in text for word in ("整理失败", "入库失败", "移动失败", "识别失败")) or level == "ERROR":
        if any(word in text for word in ("整理", "入库", "媒体", "MediaOrganize", "Organizer")):
            return "organize_failed"

    if any(word in text for word in ("跳过入库", "跳过整理", "跳过处理", "已跳过")):
        return "skipped"

    return None


def _normalize_structured_record(record: dict) -> dict | None:
    if not isinstance(record, dict):
        return None
    category = str(record.get("category") or "").strip()
    if category not in CATEGORY_MAP:
        return None
    category_def = CATEGORY_MAP[category]
    title = str(record.get("title") or record.get("source_file") or "整理记录").strip()
    season_episode = str(record.get("season_episode") or "").strip()
    year = str(record.get("year") or "").strip()
    size = str(record.get("size") or "").strip()
    if not size:
        size_match = re.search(r"大小[：:]\s*([^；|,\s]+)", str(record.get("quality") or ""))
        if size_match:
            size = size_match.group(1).strip()
    source_path = str(record.get("source_path") or "")
    target_path = str(record.get("target_path") or "")
    source_file = str(record.get("source_file") or "")
    target_file = str(record.get("target_file") or "")
    if (
        category in {"wash_success", "wash_failed"}
        and "转存目录" in source_path
        and "媒体目录" in target_path
    ):
        source_path, target_path = target_path, source_path
        source_file, target_file = target_file, source_file
    subtitle_parts = [part for part in (year, season_episode, target_path or source_path) if part]
    detail_lines = []
    source_path_label = "原文件" if category in {"wash_success", "wash_failed"} else "源路径"
    target_path_label = "新文件" if category in {"wash_success", "wash_failed"} else "目标路径"
    for label, key in (
        (target_path_label, "target_path"),
        (source_path_label, "source_path"),
        ("原因", "reason"),
        ("决策", "decision"),
        ("画质", "quality"),
        ("视频", "video"),
        ("音频", "audio"),
        ("大小", "size"),
    ):
        if key == "source_path":
            value = source_path
        elif key == "target_path":
            value = target_path
        else:
            value = str(record.get(key) or "").strip()
        if value:
            detail_lines.append({"label": label, "value": _short_text(value, 220)})
    return {
        "id": str(record.get("id") or ""),
        "time": str(record.get("created_at") or ""),
        "level": "INFO",
        "category": category,
        "category_label": category_def["label"],
        "icon": category_def["icon"],
        "tone": category_def["tone"],
        "title": _short_text(title, 120),
        "subtitle": _short_text(" · ".join(subtitle_parts), 220),
        "message": _short_text(str(record.get("summary") or record.get("decision") or record.get("reason") or ""), 260),
        "detail_lines": detail_lines[:5],
        "raw": _short_text(json.dumps(record, ensure_ascii=False), 420),
        "media_type": str(record.get("media_type") or ""),
        "target_path": target_path,
        "source_path": source_path,
        "target_file": target_file,
        "source_file": source_file,
        "size": size,
        "reason": str(record.get("reason") or ""),
        "status_label": category_def["label"],
        "transfer_method": "整理",
        "season_episode": season_episode,
        "year": year,
    }


def _detail_lines(message: str, category: str) -> list[dict[str, str]]:
    candidates = [
        ("结果", ("结果", "本次决策")),
        ("原因", ("原因", "判断原因")),
        ("库位", ("库位", "目录", "路径")),
        ("旧资源", ("旧文件", "源媒体文件")),
        ("本次资源", ("新文件", "新媒体文件")),
        ("旧资源参数", ("旧文件参数", "旧资源质量", "old_meta")),
        ("本次资源参数", ("新文件参数", "本次整理资源", "new_meta")),
        ("数量", ("数量", "生成", "成功", "失败", "跳过")),
    ]
    lines: list[dict[str, str]] = []
    for label, keys in candidates:
        value = _pick_value(message, keys)
        if value:
            lines.append({"label": label, "value": value})

    if category == "sha1_duplicate" and not any(item["label"] == "原因" for item in lines):
        lines.insert(0, {"label": "原因", "value": "SHA1重复，已跳过"})
    if category == "strm_generated" and not lines:
        lines.append({"label": "生成", "value": _short_text(_strip_prefix(message), 140)})

    deduped: list[dict[str, str]] = []
    seen = set()
    for item in lines:
        key = (item["label"], item["value"])
        if item["value"] and key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped[:6]


def _build_record(line: str, index: int) -> dict[str, Any] | None:
    line = sanitize_log_text(line).strip()
    if not line or should_hide_console_log_line(line):
        return None

    matched = LOG_RE.match(line)
    if not matched:
        return None

    level = matched.group("level")
    message = matched.group("message").strip()
    category = _classify(message, level)
    if not category:
        return None
    if category in {"organize_success", "organize_failed", "strm_generated"} and not _looks_like_media_record(message):
        return None

    category_def = CATEGORY_MAP[category]
    display_message = _strip_prefix(message)
    failure_summary = re.search(
        r"失败\s+\d+/\d+[:：]\s*(?P<file>.*?)\s*[|｜]\s*原因[:：]\s*(?P<reason>.*)$",
        display_message,
    )
    if category == "organize_failed" and failure_summary:
        failed_file = failure_summary.group("file").strip()
        reason = failure_summary.group("reason").strip()
        return {
            "id": f"{matched.group('time')}-{index}",
            "time": matched.group("time"),
            "level": level,
            "category": category,
            "category_label": category_def["label"],
            "icon": category_def["icon"],
            "tone": category_def["tone"],
            "title": _short_text(failed_file, 120),
            "subtitle": "",
            "message": _short_text(reason, 260),
            "detail_lines": [{"label": "原因", "value": _short_text(reason, 220)}] if reason else [],
            "raw": _short_text(message, 420),
            "source_path": failed_file,
            "target_path": "",
            "size": "",
            "reason": reason,
            "status_label": category_def["label"],
            "transfer_method": "整理",
        }
    source_path = _pick_value(message, ("源路径", "源媒体文件", "文件", "路径"))
    target_path = _pick_value(message, ("目标路径", "新媒体文件", "库位"))
    size = _pick_value(message, ("大小", "文件大小", "新文件大小"))
    return {
        "id": f"{matched.group('time')}-{index}",
        "time": matched.group("time"),
        "level": level,
        "category": category,
        "category_label": category_def["label"],
        "icon": category_def["icon"],
        "tone": category_def["tone"],
        "title": _extract_title(message),
        "subtitle": _extract_subtitle(message),
        "message": _short_text(display_message, 260),
        "detail_lines": _detail_lines(message, category),
        "raw": _short_text(message, 420),
        "source_path": source_path,
        "target_path": target_path,
        "size": size,
        "reason": _pick_value(message, ("原因", "失败原因", "判断原因")),
        "status_label": category_def["label"],
        "transfer_method": "整理" if category.startswith("organize") else category_def["label"],
    }


def _matches_keyword(record: dict[str, Any], keyword: str) -> bool:
    if not keyword:
        return True
    haystack = " ".join(
        str(record.get(key, ""))
        for key in ("title", "subtitle", "message", "raw", "category_label", "source_path", "target_path", "reason")
    ).lower()
    return keyword.lower() in haystack


@router.get("/records")
async def get_organize_history(
    category: str = Query("organize_success"),
    keyword: str = Query(""),
    limit: int = Query(200, ge=1, le=MAX_RESPONSE_LIMIT),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=10, le=MAX_RESPONSE_LIMIT),
):
    if not isinstance(category, str):
        category = "organize_success"
    if not isinstance(keyword, str):
        keyword = ""
    if not isinstance(limit, int):
        limit = 200
    if not isinstance(page, int):
        page = 1
    if not isinstance(page_size, int):
        page_size = DEFAULT_PAGE_SIZE

    records = [
        record
        for record in (_normalize_structured_record(item) for item in list_organize_history())
        if record is not None
    ]

    keyword = (keyword or "").strip()
    keyword_records = [record for record in records if _matches_keyword(record, keyword)]
    counts = Counter(record["category"] for record in keyword_records)

    category_items = []
    for item in CATEGORY_DEFS:
        count = counts.get(item["key"], 0)
        category_items.append({**item, "count": count})

    if category in CATEGORY_MAP:
        visible_records = [record for record in keyword_records if record["category"] == category]
    else:
        visible_records = [record for record in keyword_records if record["category"] == "organize_success"]

    total = len(visible_records)
    page_size = max(10, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_RESPONSE_LIMIT))
    page_count = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), page_count))
    start = (page - 1) * page_size
    end = start + page_size

    return {
        "categories": category_items,
        "records": visible_records[start:end],
        "total": total,
        "page": page,
        "page_size": page_size,
        "page_count": page_count,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
