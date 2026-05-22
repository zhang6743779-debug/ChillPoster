import sys
import os
import json
import logging
import re
from .configs import APP_LOG_FILE, CONFIG_DIR, CONFIG_FILE

_log_line_publisher = None

_URL_RE = re.compile(r"https?://[^\s'\"<>，。]+", re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&;](?:t|u|k|token|access_token|api_key|apikey|key|sign|sig|auth|authorization|cookie|session)=)"
    r"[^&;\s'\"<>，。)}\]]+"
)
_HEADER_SECRET_RE = re.compile(
    r"(?im)\b(authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+"
)


def _is_115_direct_url(url: str) -> bool:
    text = str(url or "").lower()
    return (
        "115cdn.net" in text
        or "cdnfhnfile" in text
        or "cdnfile" in text and "115" in text
    )


def _redact_url(url: str) -> str:
    if _is_115_direct_url(url):
        match = re.match(r"(?i)^(https?://[^/?#]+)", url)
        if match:
            return f"{match.group(1)}/[115-direct-url-redacted]"
        return "[115-direct-url-redacted]"
    return _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}***", url)


def sanitize_log_text(message: str) -> str:
    """Redact credentials and short-lived direct-link parameters before logs leave the process."""
    text = str(message or "")
    if not text:
        return text
    text = _URL_RE.sub(lambda m: _redact_url(m.group(0)), text)
    text = _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}***", text)
    text = _HEADER_SECRET_RE.sub(lambda m: f"{m.group(1)}: ***", text)
    return text

HIDDEN_CONSOLE_LOG_FRAGMENTS = (
    "libssl detected, it will be used for encryption",
    "Handling update UpdateShort",
    "Handling update Updates",
    "Handling container",
    "Handling acknowledge for",
    "Handling RPC result for message",
    "Handling bad salt for message",
    "Handling new session created",
    "Receiving items from the network",
    "Waiting for messages to send",
    "Assigned msg_id =",
    "Encrypting ",
    "Encrypted messages put in a queue to be sent",
    "Getting difference for channel",
    "Got difference for channel",
    "Timeout waiting for updates expired",
    "Starting direct file download",
    "Borrowing sender for dc_id",
    "Returning borrowed sender for dc_id",
    "Connecting to ",
    "Connection to ",
    "Connection attempt ",
    "Connection success",
    "Starting send loop",
    "Starting receive loop",
    "Disconnecting from ",
    "Disconnection from ",
    "Closing current connection",
    "Cancelling ",
)


def should_hide_console_log_line(message: str) -> bool:
    return any(fragment in message for fragment in HIDDEN_CONSOLE_LOG_FRAGMENTS)

def register_log_line_publisher(publisher):
    global _log_line_publisher
    _log_line_publisher = publisher

# 日志轮转配置
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5MB
MAX_LOG_BACKUPS = 3             # 保留 3 个备份文件

def _rotate_log():
    """检查日志大小，超过 5MB 时轮转，保留 app.log.1 ~ app.log.3"""
    if not os.path.exists(APP_LOG_FILE):
        return
    try:
        if os.path.getsize(APP_LOG_FILE) < MAX_LOG_SIZE:
            return
    except OSError:
        return

    # 删除最久的备份
    oldest = f"{APP_LOG_FILE}.{MAX_LOG_BACKUPS}"
    if os.path.exists(oldest):
        try:
            os.remove(oldest)
        except OSError:
            pass

    # 依次轮转 app.log.2 → app.log.3, app.log.1 → app.log.2
    for i in range(MAX_LOG_BACKUPS - 1, 0, -1):
        src = f"{APP_LOG_FILE}.{i}"
        dst = f"{APP_LOG_FILE}.{i + 1}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass

    # 当前日志 → app.log.1
    try:
        os.replace(APP_LOG_FILE, f"{APP_LOG_FILE}.1")
    except OSError:
        pass

class LoggerWriter:
    def __init__(self, writer):
        self.writer = writer
        self.log_file = None
        self._bytes_written = 0
        self._line_buffer = ""
        self._open_file()

    def _open_file(self):
        try:
            if not os.path.exists(CONFIG_DIR):
                os.makedirs(CONFIG_DIR)
            _rotate_log()
            # buffering=1 使用行缓冲，确保写入及时
            self.log_file = open(APP_LOG_FILE, "a", encoding="utf-8", buffering=1)
            self._bytes_written = 0
        except:
            pass

    def write(self, message):
        safe_message = sanitize_log_text(message)
        # 过滤掉频繁接口日志，防止 Web 端日志刷屏
        if (
            "GET /api/progress" in safe_message or
            "GET /api/system_logs" in safe_message or
            "GET /api/system_logs/stream" in safe_message or
            "/api/save HTTP/1.1" in safe_message or
            "/api/clear_task_progress HTTP/1.1" in safe_message or
            "INFO:" in safe_message and "HTTP/1.1\"" in safe_message or
            should_hide_console_log_line(safe_message)
        ):
            return

        # 1. 写入原始控制台 (Docker/后台可见)
        if self.writer:
            try:
                self.writer.write(safe_message)
                self.writer.flush()
            except:
                pass

        # 2. 写入日志文件 (Web 端可见)
        if self.log_file:
            try:
                self.log_file.write(safe_message)
                self.log_file.flush()
                self._bytes_written += len(safe_message.encode('utf-8'))
                # 每写入 64KB 检查一次是否需要轮转
                if self._bytes_written >= 65536:
                    self._bytes_written = 0
                    if os.path.exists(APP_LOG_FILE) and os.path.getsize(APP_LOG_FILE) >= MAX_LOG_SIZE:
                        self.log_file.close()
                        _rotate_log()
                        self.log_file = open(APP_LOG_FILE, "a", encoding="utf-8", buffering=1)
            except:
                pass

        if _log_line_publisher:
            try:
                self._line_buffer += safe_message
                while "\n" in self._line_buffer:
                    line, self._line_buffer = self._line_buffer.split("\n", 1)
                    if line.strip():
                        _log_line_publisher(line + "\n")
            except:
                pass

    def flush(self):
        if self.writer:
            try: self.writer.flush()
            except: pass
        if self.log_file:
            try: self.log_file.flush()
            except: pass

    def isatty(self):
        return getattr(self.writer, 'isatty', lambda: False)()

def _normalize_log_level(level_value):
    level_str = str(level_value or "").strip().upper()
    if level_str in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        return level_str
    return "INFO"



def _read_log_level_from_settings():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                return _normalize_log_level(settings.get("log_level", "INFO"))
    except Exception:
        pass
    return "INFO"


def _quiet_noisy_dependency_loggers():
    noisy_loggers = (
        "httpx",
        "httpcore",
        "apscheduler",
        "urllib3",
        "requests",
        "asyncio",
        "tzlocal",
        "tzdata",
        "websockets",
        "websockets.client",
        "websockets.server",
        "websockets.protocol",
        "telethon",
        "telethon.client",
        "telethon.crypto",
        "telethon.extensions",
        "telethon.network",
        "telethon.network.connection",
        "telethon.network.mtprotosender",
    )
    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)



def set_log_level(level_value, announce=True):
    level_name = _normalize_log_level(level_value)
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    previous_level = _normalize_log_level(logging.getLevelName(root_logger.level))
    root_logger.setLevel(level)

    app_logger = logging.getLogger("ChillPoster")
    app_logger.setLevel(level)

    logging.getLogger("uvicorn").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(level)

    # 噪音依旧保持压制。DEBUG 模式只放大本项目日志，不展开第三方网络库细节。
    _quiet_noisy_dependency_loggers()

    if announce and previous_level != level_name:
        if level_name == "DEBUG":
            app_logger.info("-------DEBUG模式已开启-------")
        elif previous_level == "DEBUG" and level_name == "INFO":
            app_logger.info("-------DEBUG模式已关闭-------")
        else:
            app_logger.info(f"-------日志级别已切换为{level_name}-------")

    return level_name



def setup_logging():
    # ★★★ 核心修复：立即劫持标准输出 ★★★
    # 只有当 stdout 不是 LoggerWriter 时才劫持，防止重复劫持
    if not isinstance(sys.stdout, LoggerWriter):
        sys.stdout = LoggerWriter(sys.stdout)
        sys.stderr = LoggerWriter(sys.stderr)

    # 修复 'Logger' object has no attribute 'trace'
    TRACE_LEVEL_NUM = 5 
    logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")
    def trace(self, message, *args, **kws):
        if self.isEnabledFor(TRACE_LEVEL_NUM):
            self._log(TRACE_LEVEL_NUM, message, args, **kws)
    
    # 防止重复添加方法
    if not hasattr(logging.Logger, "trace"):
        logging.Logger.trace = trace

    # 配置 logger，强制使用 sys.stdout (已被劫持)
    # 这会配置 root logger，使得所有 logger.info() 都输出到 LoggerWriter
    initial_level_name = _read_log_level_from_settings()
    initial_level = getattr(logging, initial_level_name, logging.INFO)

    logging.basicConfig(
        level=initial_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        stream=sys.stdout, # 明确指定流，此时 sys.stdout 已经是 LoggerWriter 了
        force=True         # 强制覆盖之前的配置
    )

    set_log_level(initial_level_name, announce=False)
    logging.info(f"[启动] 日志系统初始化完成，级别: {initial_level_name}")

# =========================================================
#  新增部分：执行初始化 并 导出 main.py 需要的 logger 对象
# =========================================================

# 1. 在模块导入时立即执行配置，确保 stdout 被劫持
setup_logging()

# 2. 定义 logger 对象 (main.py 中 from core.logger import logger 就是找它)
logger = logging.getLogger("ChillPoster")
