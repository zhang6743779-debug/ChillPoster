import uvicorn
import os
import shutil  # [新增] 用于文件复制
import asyncio
import json
import logging

# httpcore_request 在被导入时给 AsyncConnectionPool.__del__ 注入了动态 import，
# Python 退出时 import 系统已销毁会报 ImportError。
# 提前无条件占位：httpcore_request 看到 __del__ 已存在就会跳过注入。
try:
    from httpcore import AsyncConnectionPool
    setattr(AsyncConnectionPool, "__del__", lambda _: None)
except Exception:
    pass

# 过滤 uvicorn 优雅关闭时强制取消 SSE 连接产生的预期噪音
class _ShutdownNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "timeout graceful shutdown exceeded" in msg:
            return False
        if "Invalid HTTP request received" in msg:
            return False
        if record.exc_info and record.exc_info[0] is not None:
            if issubclass(record.exc_info[0], asyncio.CancelledError):
                return False
        return True

logging.getLogger("uvicorn.error").addFilter(_ShutdownNoiseFilter())
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager

# 导入所有路由模块
from app.routers import auth
from app.routers import server
from app.routers import tasks
from app.routers import webhook
from app.routers import resources
from app.routers import rss
from app.routers import config_302
from app.routers import gateway
from app.routers import hdhive
from app.routers import wechat_notify
from app.routers import discover
from app.routers import moviepilot
from app.routers import transfer
from app.routers import strm
from app.routers import media_organize
from app.routers import upgrade
from app.routers import docker_manager
from app.routers import drive115_cleanup
from app.routers import drive115_upload
from app.routers import system_health

# === [新增] 导入网关全局客户端，用于优雅关闭 ===
from app.routers.gateway import proxy_client 

# === [新增] 导入 115 服务实例，用于优雅关闭 ===
from app.services.drive115_service import drive115_service

# 导入核心组件
from core.logger import logger, register_log_line_publisher

# === 【核心修复点】路径修正：增加 app. 前缀 ===
from app.services.task_service import task_service_instance
from app.services.rss_service import rss_service_instance
from app.services.hdhive_service import hdhive_service
from app.services.telegram_service import telegram_notify_service
from app.services.drive115_upload_service import drive115_upload_service
# ============================================

# ==========================================
# [新增] 日志屏蔽配置
# ==========================================
tasks.load_log_buffer_from_file()
register_log_line_publisher(tasks.publish_log_line)

# 1. 屏蔽 httpx/httpcore 的 INFO 日志 (GET/POST 请求不打印)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.client").setLevel(logging.WARNING)
logging.getLogger("websockets.server").setLevel(logging.WARNING)
logging.getLogger("websockets.protocol").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("telethon.client").setLevel(logging.WARNING)
logging.getLogger("telethon.crypto").setLevel(logging.WARNING)
logging.getLogger("telethon.extensions").setLevel(logging.WARNING)
logging.getLogger("telethon.network").setLevel(logging.WARNING)
logging.getLogger("telethon.network.connection").setLevel(logging.WARNING)
logging.getLogger("telethon.network.mtprotosender").setLevel(logging.WARNING)

# 2. [新增] 屏蔽 apscheduler 的英文启动日志
logging.getLogger("apscheduler").setLevel(logging.WARNING)
# ==========================================


# ==========================================
# [新增] 启动时恢复默认文件逻辑
# ==========================================
def restore_defaults():
    """
    检查 config, fonts, templates 等目录。
    如果用户挂载了空卷导致文件缺失，从 defaults/ 目录恢复它们。
    """
    base_dir = os.getcwd()
    # 定义映射关系: 备份源 -> 目标目录
    # 注意：这些 defaults 目录是在 Dockerfile 构建阶段通过 cp 命令生成的
    folder_map = {
        "defaults/config": "config",
        "defaults/fonts": "fonts",
        "defaults/templates": "templates",
        "defaults/layouts": "layouts"
    }

    logger.info("[启动] 检查默认资源完整性")

    for src_rel, dst_rel in folder_map.items():
        src_path = os.path.join(base_dir, src_rel)
        dst_path = os.path.join(base_dir, dst_rel)

        # 1. 如果镜像里本身就没有备份源，跳过
        if not os.path.exists(src_path):
            continue

        # 2. 确保目标根目录存在 (例如创建一个空的 /app/templates)
        if not os.path.exists(dst_path):
            try:
                os.makedirs(dst_path)
            except Exception:
                pass # 忽略已存在报错

        # 3. 递归遍历备份目录下的所有文件
        restore_count = 0
        for root, dirs, files in os.walk(src_path):
            # 计算相对路径，例如 fonts/myfont.ttf -> /myfont.ttf
            rel_path = os.path.relpath(root, src_path)
            target_root = os.path.join(dst_path, rel_path)

            if not os.path.exists(target_root):
                try:
                    os.makedirs(target_root)
                except Exception:
                    pass

            for file in files:
                src_file = os.path.join(root, file)
                dst_file = os.path.join(target_root, file)

                # 【核心逻辑】只有当目标文件不存在时才复制 
                # (既修复了空卷问题，又保护了用户修改过的文件不被覆盖)
                if not os.path.exists(dst_file):
                    try:
                        shutil.copy2(src_file, dst_file)
                        restore_count += 1
                    except Exception as e:
                        logger.error(f"[启动] 恢复文件失败 {dst_file}: {e}")
        
        if restore_count > 0:
            logger.info(f"[启动] 已恢复 {restore_count} 个文件: {dst_rel}")


# ==========================================
# 1. 定义 UI 管理端 App (监听 5256)
# ==========================================

DEFAULT_PROJECT_VERSION = "v1.0.0.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIST_DIR = os.path.join(BASE_DIR, "frontend", "dist")
STATIC_DIR = FRONTEND_DIST_DIR if os.path.exists(FRONTEND_DIST_DIR) else os.path.join(BASE_DIR, "static")
VERSION_FILE = os.path.join(BASE_DIR, "VERSION")


def get_project_version() -> str:
    version = ""
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                version = f.read().strip()
        except Exception:
            version = ""
    if not version:
        version = os.getenv("CHILLPOSTER_VERSION", "").strip()
    if not version:
        version = DEFAULT_PROJECT_VERSION
    return version if version.startswith("v") else f"v{version}"


PROJECT_VERSION = get_project_version()


@asynccontextmanager
async def lifespan_ui(app: FastAPI):
    # --- 启动逻辑 ---
    logger.info("[启动] 初始化系统组件")
    # 注册主事件循环，供 115 Life 监控回调线程投递整理任务
    try:
        from app.routers.media_organize import register_main_event_loop
        register_main_event_loop(asyncio.get_event_loop())
    except Exception:
        pass
    try:
        # 尝试设置代理 (如果有)
        from core import tmdb
        from core.configs import global_config
        if global_config.proxy_url:
            tmdb.set_proxy(global_config.proxy_url)
            logger.info(f"[启动] 已应用代理: {global_config.proxy_url}")
    except Exception as e:
        logger.warning(f"[启动] 应用代理失败: {e}")

    task_service_instance.scheduler.start()
    task_service_instance.refresh_cleanup_jobs()
    task_service_instance.refresh_selected_cleanup_jobs()
    task_service_instance.schedule_daily_signin_job()
    strm.schedule_daily_full_sync_job(task_service_instance.scheduler)
    task_service_instance.load_active_jobs()
    rss_service_instance.load_active_jobs()
    hdhive_service.setup_scheduler(task_service_instance.scheduler)
    drive115_upload_service.start()
    if telegram_notify_service.should_bot_poll():
        telegram_notify_service.start_polling()
    if telegram_notify_service.should_account_monitor():
        telegram_notify_service.start_monitor()
    logger.info("[启动] 基础任务与服务初始化完成")

    # 加载 Emby 媒体库缓存
    try:
        from app.services.emby_library_cache import init_cache
        init_cache()
    except Exception as e:
        logger.warning(f"[启动] Emby 媒体库缓存加载失败: {e}")

    # 后台初始化媒体整理缓存，避免 SQLite 迁移/建索引阻塞 UI 启动。
    try:
        from core.media_library_cache import warmup_cache_in_background
        warmup_cache_in_background()
    except Exception as e:
        logger.warning(f"[启动] 媒体整理缓存预热启动失败: {e}")

    # 预热发现页扩展源缓存，避免首次请求卡顿
    try:
        from app.routers.discover import _get_reference_sources
        _get_reference_sources()
        logger.info("[启动] 发现页扩展源缓存预热完成")
    except Exception as e:
        logger.warning(f"[启动] 发现页扩展源预热失败: {e}")

    # [新增] 启动 115 生活事件监控
    try:
        from core.monitor115.monitor import create_monitor
        # 从媒体整理配置中读取网盘转存源目录和目标目录
        organize_cfg_path = os.path.join(os.getcwd(), "config", "media_organize.json")
        if os.path.exists(organize_cfg_path):
            with open(organize_cfg_path, "r", encoding="utf-8") as f:
                organize_cfg = json.load(f)
            source_dir = organize_cfg.get("source_name", "")
            target_dir = organize_cfg.get("target_name", "")
            life_monitor_on = organize_cfg.get("life_monitor_enabled", False)
            if source_dir and target_dir and life_monitor_on:
                # 使用工厂函数创建回调（带防抖和自动整理）
                from app.routers.media_organize import create_life_event_callback
                drive_idx = organize_cfg.get("drive_index", 0)
                life_event_callback = create_life_event_callback(
                    source_dir,
                    drive_idx,
                    target_dir,
                    str(organize_cfg.get("source_cid", "")),
                    str(organize_cfg.get("target_cid", "")),
                )

                client, _ = await drive115_service.get_client(0)
                if client:
                    monitor = create_monitor(
                        client=client,
                        source_dir=source_dir,
                        target_dir=target_dir,
                        callback=life_event_callback,
                        start_mode="latest",
                    )
                    monitor.start()
                    logger.info("[启动] 115 Life 事件监控已启动")
                else:
                    logger.warning("[启动] 115 客户端未就绪，跳过 Life 事件监控")
    except Exception as e:
        logger.warning(f"[启动] 115 Life 事件监控启动失败: {e}")

    yield
    # --- 关闭逻辑 ---
    # 停止 115 Life 事件监控
    try:
        from core.monitor115.monitor import life_event_monitor
        if life_event_monitor:
            life_event_monitor.stop()
    except Exception:
        pass
    telegram_notify_service.stop_monitor()
    telegram_notify_service.stop_polling()
    try:
        drive115_upload_service.stop()
    except Exception:
        pass
    try:
        task_service_instance.scheduler.shutdown(wait=False)
    except Exception:
        pass
    logger.info("[启动] 系统已关闭")

# 创建 UI 主应用
app = FastAPI(title="ChillPoster UI", version=PROJECT_VERSION, lifespan=lifespan_ui)

app.add_middleware(GZipMiddleware, minimum_size=1024)

# 允许跨域 (UI端)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载 API 路由 (只给 UI 用)
app.include_router(auth.router)
app.include_router(server.router)
app.include_router(tasks.router)
app.include_router(webhook.router)
app.include_router(resources.router)
app.include_router(rss.router)
app.include_router(config_302.router)
app.include_router(hdhive.router)
app.include_router(wechat_notify.router)
app.include_router(discover.router)
app.include_router(moviepilot.router)
app.include_router(transfer.router)
app.include_router(strm.router)
app.include_router(media_organize.router)
app.include_router(upgrade.router)
app.include_router(docker_manager.router)
app.include_router(drive115_cleanup.router)
app.include_router(drive115_upload.router)
app.include_router(system_health.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/fonts", StaticFiles(directory="fonts"), name="fonts")
app.mount("/templates", StaticFiles(directory="templates"), name="templates")
app.mount("/backups", StaticFiles(directory="backups"), name="backups")

@app.get("/api/version")
async def api_version():
    return {"version": PROJECT_VERSION}

@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html", status_code=302)


# ==========================================
# 2. 定义 网关/反代 App (监听 8011 或配置端口)
# ==========================================

# [新增] 网关的生命周期，用于优雅关闭 httpx 客户端
@asynccontextmanager
async def lifespan_gateway(app: FastAPI):
    logger.info("[Gateway] 网关服务启动")
    yield
    logger.info("[Gateway] 正在关闭连接池...")

    try:
        await asyncio.wait_for(proxy_client.aclose(), timeout=3)
    except asyncio.TimeoutError:
        logger.warning("[Gateway] 代理客户端关闭超时，强制跳过")

    try:
        await asyncio.wait_for(drive115_service.close(), timeout=3)
    except asyncio.TimeoutError:
        logger.warning("[Gateway] 115 客户端关闭超时，强制跳过")

    try:
        from app.routers.discover import close_img_clients
        await asyncio.wait_for(close_img_clients(), timeout=3)
    except Exception:
        pass

    logger.info("[Gateway] 网关服务已关闭")

proxy_app = FastAPI(title="ChillPoster Gateway", lifespan=lifespan_gateway)

# 允许跨域 (网关端，确保 Emby Web 客户端正常)
proxy_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载网关路由 (核心反代逻辑)
# 注意：gateway.router 接管所有路径 /{path:path}
proxy_app.include_router(gateway.router)


# ==========================================
# 3. 启动器 (同时运行两个服务)
# ==========================================

async def serve_apps():
    """读取配置并并发启动多个网关服务器"""

    # 1. 读取 302 配置中的所有端口号，并建立端口到 Emby 索引的映射
    gateway_ports = []  # 改为列表，支持多个端口
    port_to_emby_map = {}  # 端口 -> Emby 索引映射
    config_path = "config/config_302.json"

    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 兼容新版配置 (embys 数组) 和旧版配置 (emby 对象)
                # 新版：从 embys 数组的所有 enabled 元素读取端口
                embys = data.get("embys", [])
                if embys:
                    for idx, emby in enumerate(embys):
                        if emby.get("enabled", True):
                            cfg_port = emby.get("proxy_port")
                            if cfg_port:
                                port = int(cfg_port)
                                gateway_ports.append(port)
                                port_to_emby_map[port] = idx  # 记录端口到索引的映射
                else:
                    # 旧版：从 emby 对象读取
                    cfg_port = data.get("emby", {}).get("proxy_port")
                    if cfg_port:
                        port = int(cfg_port)
                        gateway_ports.append(port)
                        port_to_emby_map[port] = 0
        except Exception as e:
            logger.error(f"[启动] 读取网关端口失败，将使用默认 8011: {e}")

    # 如果没有找到任何端口配置，使用默认端口
    if not gateway_ports:
        gateway_ports = [8011]

    logger.info(f"[启动] UI 端口: 5256 | 网关端口: {gateway_ports}")

    # ==========================================================
    # 启动多个网关服务器
    # ==========================================================
    config_ui = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=5256,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=3,
    )

    server_ui = uvicorn.Server(config_ui)

    # 注册端口到 Emby 索引的映射（用于网关识别请求）
    from app.routers.gateway import register_gateway_port
    for port, emby_idx in port_to_emby_map.items():
        register_gateway_port(port, emby_idx)
        logger.info(f"[启动] 端口映射: {port} -> Emby[{emby_idx}]")

    # 为每个端口创建一个网关服务器
    gateway_servers = []
    for port in gateway_ports:
        config_gw = uvicorn.Config(
            proxy_app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=3,
        )
        gateway_servers.append(uvicorn.Server(config_gw))

    # 并发运行 UI 和所有网关服务
    try:
        servers = [server_ui.serve()] + [gs.serve() for gs in gateway_servers]
        await asyncio.gather(*servers)
    except asyncio.CancelledError:
        # 捕获任务取消信号，防止控制台打印一大堆 Traceback
        pass

if __name__ == "__main__":
    # --- [Step 1] 优先执行默认文件恢复 ---
    # 这必须在任何其他操作之前，确保 fonts/templates 等文件就位
    restore_defaults()

    # --- [Step 2] 检查必要目录 (保留原逻辑作为双重保险) ---
    if not os.path.exists("fonts"):
        os.makedirs("fonts")
        logger.info("创建 fonts 目录")
    
    logger.info("[启动] 正在启动服务")
    logger.info("[启动] 管理后台: http://localhost:5256/static/index.html")

    # 运行异步主程序
    try:
        asyncio.run(serve_apps())
    except KeyboardInterrupt:
        # 捕获最外层的 Ctrl+C，打印一句简单的提示即可
        logger.warning("[启动] 服务已停止")
    except Exception as e:
        logger.error(f"[启动] 服务异常退出: {e}")
