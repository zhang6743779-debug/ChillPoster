"""
core/meta/mediainfo.py
媒体文件信息提取 - 通过 ffprobe 获取实际媒体流信息
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ChillPoster.mediainfo")
_FFPROBE_SEMAPHORE = threading.BoundedSemaphore(4)


def _probe_target_label(filepath: str) -> str:
    if filepath.startswith("http://") or filepath.startswith("https://"):
        return "HTTP(S) URL"
    return filepath


def _summarize_probe(probe: dict) -> str:
    streams = list((probe or {}).get("streams") or [])
    fmt = dict((probe or {}).get("format") or {})

    video_streams = [
        s for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    video = video_streams[0] if video_streams else {}
    audio = max(audio_streams, key=lambda s: int(s.get("channels") or 0), default={})

    video_parts = []
    if video:
        video_parts.append(str(video.get("codec_name") or "video"))
        if video.get("width") and video.get("height"):
            video_parts.append(f"{video.get('width')}x{video.get('height')}")
        if video.get("pix_fmt"):
            video_parts.append(str(video.get("pix_fmt")))
        fps = _parse_fps(video.get("r_frame_rate", "")) or _parse_fps(video.get("avg_frame_rate", ""))
        if fps:
            video_parts.append(fps)

    audio_parts = []
    if audio:
        audio_parts.append(str(audio.get("codec_name") or "audio"))
        if audio.get("channels"):
            audio_parts.append(f"{audio.get('channels')}ch")
        if audio.get("channel_layout"):
            audio_parts.append(str(audio.get("channel_layout")))

    duration = str(fmt.get("duration") or "").strip()
    try:
        duration = f"{float(duration):.1f}s" if duration else ""
    except Exception:
        pass

    return (
        f"format={fmt.get('format_name') or '-'} "
        f"streams={len(streams)} "
        f"video={' '.join(video_parts) if video_parts else '-'} "
        f"audio={' '.join(audio_parts) if audio_parts else '-'} "
        f"duration={duration or '-'}"
    )


def probe_file(filepath: str) -> Optional[dict]:
    """
    调用 ffprobe 获取媒体流信息。支持本地路径和 HTTP(S) URL。

    Args:
        filepath: 本地文件路径或 HTTP(S) 直链 URL

    Returns:
        ffprobe JSON 输出 (含 streams / format)，失败返回 None
    """
    is_url = filepath.startswith("http://") or filepath.startswith("https://")
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format"]
    if is_url:
        # 只读头部，避免下载大量数据；115 直链需要带 Referer 头防盗链
        cmd += [
            "-probesize", "5000000",
            "-analyzeduration", "0",
            "-headers", "User-Agent: Mozilla/5.0\r\nReferer: https://115.com\r\n",
        ]
    cmd.append(filepath)

    try:
        started_at = time.perf_counter()
        with _FFPROBE_SEMAPHORE:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        if result.returncode != 0:
            logger.debug(f"ffprobe 返回非零状态码 {result.returncode}: {_probe_target_label(filepath)} | {result.stderr[:200]}")
            return None
        probe = json.loads(result.stdout)
        logger.debug(f"[MediaInfo] ffprobe摘要: {_summarize_probe(probe)} | 耗时:{time.perf_counter() - started_at:.2f}s")
        return probe
    except FileNotFoundError:
        logger.warning("ffprobe 未安装或不在 PATH 中，跳过 MediaInfo 获取")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(f"ffprobe 超时: {_probe_target_label(filepath)}")
        return None
    except (json.JSONDecodeError, Exception) as e:
        logger.debug(f"ffprobe 解析失败 '{_probe_target_label(filepath)}': {e}")
        return None


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _parse_fps(rate: str) -> Optional[str]:
    if not rate or "/" not in rate:
        return None
    try:
        num, den = rate.split("/")
        num_i = int(num)
        den_i = int(den)
        if den_i <= 0:
            return None
        fps = round(num_i / den_i)
        if fps > 0:
            return f"{fps}fps"
    except Exception:
        return None
    return None


def _pick_best_audio_stream(audio_streams: list[dict]) -> Optional[dict]:
    if not audio_streams:
        return None

    def score(stream: dict) -> int:
        codec = (stream.get("codec_name") or "").lower()
        profile = (stream.get("profile") or "").lower()
        channels = int(stream.get("channels") or 2)
        bit_rate = int(stream.get("bit_rate") or 0)

        codec_score = 0
        if codec == "truehd":
            codec_score = 100
        elif "dts" in codec:
            if "x" in profile:
                codec_score = 98
            elif "ma" in profile or "hd" in profile:
                codec_score = 95
            else:
                codec_score = 90
        elif codec == "eac3":
            codec_score = 70
        elif codec == "ac3":
            codec_score = 60
        elif codec == "aac":
            codec_score = 50
        elif codec == "flac":
            codec_score = 55

        return codec_score * 100000 + channels * 1000 + bit_rate

    return max(audio_streams, key=score)


def _format_audio_encode(stream: dict) -> Optional[str]:
    if not stream:
        return None

    codec = (stream.get("codec_name") or "").lower()
    profile = (stream.get("profile") or "").lower()
    channels = int(stream.get("channels") or 2)

    if channels >= 8:
        ch = "7.1"
    elif channels >= 6:
        ch = "5.1"
    elif channels == 2:
        ch = "2.0"
    elif channels == 1:
        ch = "1.0"
    else:
        ch = str(float(channels))

    if codec == "truehd":
        codec_display = "TrueHD"
    elif "dts" in codec:
        if "x" in profile:
            codec_display = "DTSX"
        elif "ma" in profile or "hd" in profile:
            codec_display = "DTSHD.MA"
        else:
            codec_display = "DTS"
    elif codec == "eac3":
        codec_display = "DDP"
    elif codec == "ac3":
        codec_display = "DD"
    elif codec == "aac":
        codec_display = "AAC"
    elif codec == "flac":
        codec_display = "FLAC"
    elif codec:
        codec_display = codec.upper()
    else:
        return None

    return f"{codec_display}{ch}"


def _extract_title_hint(ff_format: dict, video_streams: list[dict]) -> str:
    """优先使用视频流 tags.title，其次 format.tags.title。"""
    for stream in video_streams:
        tags = stream.get("tags", {}) if isinstance(stream, dict) else {}
        if isinstance(tags, dict):
            title = (tags.get("title") or "").strip()
            if title:
                return title

    tags = ff_format.get("tags", {}) if isinstance(ff_format, dict) else {}
    if isinstance(tags, dict):
        title = (tags.get("title") or "").strip()
        if title:
            return title
    return ""


def _apply_title_fallback(info: dict, title: str) -> dict:
    """阶段2：仅从 format.tags.title 兜底缺失字段"""
    if not title:
        return info

    if not info.get("resource_pix"):
        m = re.search(r"\b(2160p|1080p|720p|480p|4k)\b", title, re.IGNORECASE)
        if m:
            v = m.group(1).lower()
            info["resource_pix"] = "2160p" if v == "4k" else v

    if not info.get("video_encode"):
        if re.search(r"\b(x265|h265|hevc)\b", title, re.IGNORECASE):
            info["video_encode"] = "H265"
        elif re.search(r"\b(x264|h264|avc)\b", title, re.IGNORECASE):
            info["video_encode"] = "H264"
        elif re.search(r"\bav1\b", title, re.IGNORECASE):
            info["video_encode"] = "AV1"

    if not info.get("color_depth"):
        m = re.search(r"\b(12bit|10bit|8bit)\b", title, re.IGNORECASE)
        if m:
            info["color_depth"] = m.group(1).lower()

    if not info.get("fps"):
        m = re.search(r"\b(\d{2,3})\s*fps\b", title, re.IGNORECASE)
        if m:
            info["fps"] = f"{int(m.group(1))}fps"

    if not info.get("video_effect"):
        matches = re.findall(r"\b(DV|DOVI|HDR10\+|HDR10|HLG|HDR)\b", title, re.IGNORECASE)
        if matches:
            normalized = []
            for t in matches:
                u = t.upper()
                normalized.append("DV" if u == "DOVI" else u)
            info["video_effect"] = ".".join(_dedupe_keep_order(normalized))

    if not info.get("audio_encode"):
        m = re.search(
            r"\b(DDP|E-?AC-?3|AC-?3|TRUEHD|DTS[-.\s]?HD(?:[-.\s]?MA)?|DTSX|DTS|AAC|FLAC)\b[\.\s-]*(7\.1|5\.1|2\.0)?",
            title,
            re.IGNORECASE,
        )
        if m:
            raw_codec = m.group(1).upper().replace(" ", "")
            raw_channels = m.group(2) or ""

            if raw_codec in ("EAC3", "E-AC-3"):
                codec = "DDP"
            elif raw_codec in ("AC3", "AC-3"):
                codec = "DD"
            elif raw_codec.startswith("DTS") and "HD" in raw_codec and "MA" in raw_codec:
                codec = "DTSHD.MA"
            elif raw_codec == "TRUEHD":
                codec = "TrueHD"
            elif raw_codec == "DTSX":
                codec = "DTSX"
            elif raw_codec == "DTS":
                codec = "DTS"
            elif raw_codec == "AAC":
                codec = "AAC"
            elif raw_codec == "FLAC":
                codec = "FLAC"
            else:
                codec = raw_codec

            info["audio_encode"] = f"{codec}{raw_channels}" if raw_channels else codec

    if not info.get("source"):
        m = re.search(r"(UHD[\.\s]?BluRay|BluRay|WEB-DL|WEBRip|REMUX|HDTV|BDRip)", title, re.IGNORECASE)
        if m:
            source_raw = m.group(1)
            lower = source_raw.lower().replace(" ", "").replace(".", "")
            if lower == "uhdbluray":
                info["source"] = "UHD.BluRay"
            elif lower == "bluray":
                info["source"] = "BluRay"
            else:
                info["source"] = source_raw.upper().replace(" ", ".")

    if not info.get("release_group"):
        # 兼容常见组名结尾：-FRDS / @AY
        m = re.search(r"(?:-|@)([A-Za-z0-9_]+)$", title)
        if m:
            potential = m.group(1).strip()
            if len(potential) >= 2 and not potential.isdigit():
                info["release_group"] = potential
        elif "-" in title:
            potential = title.split("-")[-1].strip()
            if len(potential) >= 2 and not potential.isdigit():
                info["release_group"] = potential

    return info


def _extract_duration_seconds(ff_format: dict, streams: list[dict]) -> Optional[float]:
    candidates = []
    if isinstance(ff_format, dict):
        candidates.append(ff_format.get("duration"))
    for stream in streams or []:
        candidates.append(stream.get("duration"))
    for raw in candidates:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _extract_probe_media_fields(probe: dict) -> tuple[dict, Optional[float]]:
    streams = probe.get("streams", [])
    ff_format = probe.get("format", {})

    video_streams = [
        s for s in streams
        if s.get("codec_type") == "video" and (s.get("codec_name") or "").lower() != "mjpeg"
    ]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    info = {
        "resource_pix": None,
        "video_encode": None,
        "color_depth": None,
        "resource_effect": None,
        "video_effect": None,
        "audio_encode": None,
        "fps": None,
        "source": None,
        "release_group": None,
    }

    if video_streams:
        vs = video_streams[0]

        width = int(vs.get("width") or 0)
        height = int(vs.get("height") or 0)
        if width >= 3800:
            info["resource_pix"] = "2160p"
        elif width >= 1900:
            info["resource_pix"] = "1080p"
        elif width >= 1200:
            info["resource_pix"] = "720p"
        elif width >= 700:
            info["resource_pix"] = "480p"
        elif height >= 2160:
            info["resource_pix"] = "2160p"
        elif height >= 1080:
            info["resource_pix"] = "1080p"
        elif height >= 720:
            info["resource_pix"] = "720p"
        elif height >= 480:
            info["resource_pix"] = "480p"

        codec = (vs.get("codec_name") or "").lower()
        if codec in ("hevc", "h265", "x265"):
            info["video_encode"] = "H265"
        elif codec in ("h264", "avc", "x264"):
            info["video_encode"] = "H264"
        elif codec == "av1":
            info["video_encode"] = "AV1"
        elif codec:
            info["video_encode"] = codec.upper()

        pix_fmt = (vs.get("pix_fmt") or "").lower()
        bits_raw = str(vs.get("bits_per_raw_sample") or "")
        if "12" in pix_fmt or bits_raw == "12":
            info["color_depth"] = "12bit"
        elif "10" in pix_fmt or bits_raw == "10":
            info["color_depth"] = "10bit"
        elif pix_fmt:
            info["color_depth"] = "8bit"

        info["fps"] = _parse_fps(vs.get("r_frame_rate", "")) or _parse_fps(vs.get("avg_frame_rate", ""))

        hdr_tags = []
        side_data = vs.get("side_data_list", []) or []
        for sd in side_data:
            side_type = (sd.get("side_data_type") or "").lower()
            if "dovi" in side_type or "dolby vision" in side_type:
                hdr_tags.append("DV")

        color_transfer = (vs.get("color_transfer") or "").lower()
        if color_transfer == "smpte2084":
            hdr_tags.append("HDR10")
        elif color_transfer == "arib-std-b67":
            hdr_tags.append("HLG")

        if hdr_tags:
            info["video_effect"] = ".".join(_dedupe_keep_order(hdr_tags))

    best_audio = _pick_best_audio_stream(audio_streams)
    info["audio_encode"] = _format_audio_encode(best_audio)

    title_hint = _extract_title_hint(ff_format, video_streams)
    info = _apply_title_fallback(info, title_hint)

    return info, _extract_duration_seconds(ff_format, streams)


def extract_media_fields(filepath: str) -> dict:
    """
    从 ffprobe 结果提取标准模板变量字段。

    Returns:
        {
            "resource_pix": "2160p",
            "video_encode": "H265",
            "audio_encode": "DDP5.1",
            "fps": "24fps",
            "video_effect": "DV.HDR10",
            "color_depth": "10bit",
            "source": "WEB-DL",
            "release_group": "FRDS",
        }
    """
    is_url = filepath.startswith("http://") or filepath.startswith("https://")
    if not is_url and not Path(filepath).is_file():
        return {}

    probe = probe_file(filepath)
    if not probe:
        return {}

    info, _ = _extract_probe_media_fields(probe)
    return {k: v for k, v in info.items() if v is not None and v != ""}


def extract_wash_fields(filepath: str) -> dict:
    is_url = filepath.startswith("http://") or filepath.startswith("https://")
    if not is_url and not Path(filepath).is_file():
        return {}

    probe = probe_file(filepath)
    if not probe:
        return {}

    info, duration_seconds = _extract_probe_media_fields(probe)
    result = {
        "resource_pix": info.get("resource_pix") or "",
        "video_encode": info.get("video_encode") or "",
    }
    if duration_seconds:
        result["duration_seconds"] = duration_seconds
    return {k: v for k, v in result.items() if v not in (None, "")}
