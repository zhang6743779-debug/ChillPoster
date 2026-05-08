import argparse
import sys
import time

from app.services.docker_api import DockerAPI, build_replacement_container_payload
from core.logger import logger


def replace_container(container_id: str, image: str, stop_timeout: int = 20, skip_pull: bool = False):
    api = DockerAPI(timeout=300)
    logger.info(f"[UpgradeHelper] 检查当前容器: {container_id[:12]}")
    info = api.inspect_container(container_id)
    old_name = str(info.get("Name") or "").strip("/")
    if not old_name:
        raise RuntimeError("无法识别旧容器名称")

    if not skip_pull:
        logger.info(f"[UpgradeHelper] 拉取镜像: {image}")
        api.pull_image(image)

    payload = build_replacement_container_payload(info, image)
    logger.info(f"[UpgradeHelper] 停止旧容器: {old_name}")
    try:
        api.stop_container(container_id, timeout=stop_timeout)
    except Exception as e:
        logger.warning(f"[UpgradeHelper] 停止旧容器异常，继续尝试删除: {e}")

    logger.info(f"[UpgradeHelper] 删除旧容器: {old_name}")
    api.delete_container(container_id, force=True)

    logger.info(f"[UpgradeHelper] 创建新容器: {old_name}")
    created = api.create_container(old_name, payload)
    new_id = str((created or {}).get("Id") or "").strip()
    if not new_id:
        raise RuntimeError(f"创建新容器失败: {created}")

    logger.info(f"[UpgradeHelper] 启动新容器: {new_id[:12]}")
    api.start_container(new_id)
    logger.info("[UpgradeHelper] 容器替换完成")


def main() -> int:
    parser = argparse.ArgumentParser(description="ChillPoster self-upgrade helper")
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--image", default="chillne/chillposter:latest")
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--stop-timeout", type=int, default=20)
    parser.add_argument("--skip-pull", action="store_true")
    args = parser.parse_args()

    if args.delay > 0:
        time.sleep(args.delay)
    try:
        replace_container(args.container_id, args.image, stop_timeout=args.stop_timeout, skip_pull=args.skip_pull)
        return 0
    except Exception as e:
        logger.error(f"[UpgradeHelper] 升级失败: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
