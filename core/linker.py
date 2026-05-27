# core/linker.py
import os
import logging
import shutil
import re
from typing import List, Dict, Set
from pathlib import Path

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Linker")

class HardLinkManager:
    """
    硬链接同步管理器 (V3.4 - 详细缺失报告版)
    修改记录:
    1. 移除分季逻辑，强制同步整部剧。
    2. 移除文件后缀过滤，全量同步。
    3. 文件夹命名改为直接使用源目录名。
    4. 增加空榜单熔断保护，防止误删。
    5. [修复] 电影模式下自动识别父目录，确保同步 NFO/封面等元数据。
    6. [增强] 缺失名单格式升级为: 标题 (年份) {tmdb-ID}
    """

    def __init__(self, link_root: str):
        self.link_root = link_root
        self.last_synced_dirs: List[str] = []
        self.last_removed_dirs: List[str] = []
        if not os.path.exists(self.link_root):
            try:
                os.makedirs(self.link_root)
            except Exception as e:
                logger.error(f"无法创建根目录 {self.link_root}: {e}")

    def sync_items(self, items: List[Dict], emby_client, cleanup_stale: bool = True) -> int:
        """
        执行同步的主入口
        """
        logger.info(f"开始同步 {len(items)} 个项目到: {self.link_root}")
        self.last_synced_dirs = []
        self.last_removed_dirs = []
        
        # --- [熔断保护预检查] ---
        skip_cleanup = False
        if len(items) == 0:
            logger.warning("  [熔断警告] 本次同步列表为空！为防止误删，将跳过清理(Cleanup)步骤。")
            skip_cleanup = True
        if not cleanup_stale:
            skip_cleanup = True

        active_folders = set()
        success_count = 0
        missing_list = [] # 记录缺失项目

        for item in items:
            title = item.get('title')
            tmdb_id = item.get('tmdb_id')
            item_type = item.get('type', 'Movie')
            year = item.get('year') # 获取年份

            if not tmdb_id:
                logger.warning(f"  [跳过] 缺失 TMDb ID: {title}")
                continue

            # 1. 物理寻址
            # (确保你已经应用了之前的 exclude_path 修复)
            source_path = emby_client.find_path_by_id(tmdb_id, item_type, exclude_path=self.link_root)
            
            # --- [修改点] 格式化信息字符串 ---
            year_str = f" ({year})" if year else ""
            info_str = f"{title}{year_str} {{tmdb-{tmdb_id}}}"

            if not source_path or not os.path.exists(source_path):
                # 打印详细警告
                logger.warning(f"  [未命中] Emby 库中无此资源: {info_str}")
                # 加入详细名单
                missing_list.append(info_str)
                continue

            # 2. 目标路径规划
            if os.path.isdir(source_path):
                folder_name = os.path.basename(source_path.rstrip(os.sep))
            else:
                folder_name = os.path.basename(os.path.dirname(source_path))

            if not folder_name: 
                folder_name = self._sanitize_filename(f"{title}")

            target_dir = os.path.join(self.link_root, folder_name)
            active_folders.add(folder_name)

            # 3. 执行硬链接
            try:
                if self._process_link(source_path, target_dir):
                    self.last_synced_dirs.append(os.path.normpath(target_dir))
                    success_count += 1
            except Exception as e:
                logger.error(f"  [错误] 链接失败 {title}: {e}")

        # --- [修改点] 打印详细汇总 ---
        if missing_list:
            logger.warning(f"========== 缺失项目汇总 (共{len(missing_list)}个) ==========")
            for item_str in missing_list:
                logger.warning(f"  - {item_str}")
        else:
            logger.info("========== 所有项目全部命中 ==========")

        # 4. 过期清理
        if not skip_cleanup:
            self.last_removed_dirs = self._cleanup_stale_folders(active_folders)
        elif not cleanup_stale:
            logger.info("  [增量同步] 跳过榜单过期目录清理。")
        else:
            logger.info("  [熔断生效] 跳过清理步骤，现有硬链接已保留。")

        return success_count

    def _process_link(self, src_path: str, dst_dir: str) -> bool:
        if not os.path.exists(dst_dir):
            os.makedirs(dst_dir)

        if not self._check_same_filesystem(src_path, dst_dir):
            logger.error(f"  [跨盘错误] 无法硬链接，源和目标不在同一分区: {src_path}")
            return False

        real_src_root = src_path
        if os.path.isfile(src_path):
            real_src_root = os.path.dirname(src_path)

        if os.path.isdir(real_src_root):
            return self._recursive_link(real_src_root, dst_dir)

        return False

    def _recursive_link(self, src_root, dst_root):
        linked_any = False
        # 收集源目录中所有文件的相对路径，用于后续清理
        src_files_rel = set()

        for root, dirs, files in os.walk(src_root):
            rel_path = os.path.relpath(root, src_root)
            target_path = os.path.join(dst_root, rel_path)

            if not os.path.exists(target_path):
                os.makedirs(target_path)

            for file in files:
                src_files_rel.add(os.path.join(rel_path, file))
                s_file = os.path.join(root, file)
                d_file = os.path.join(target_path, file)
                if self._link_file_smart(s_file, d_file):
                    linked_any = True

        # 清理目标目录中源目录不存在的文件（洗版后旧文件名残留）
        if os.path.exists(dst_root):
            for root, dirs, files in os.walk(dst_root):
                rel_path = os.path.relpath(root, dst_root)
                for file in files:
                    file_rel = os.path.join(rel_path, file)
                    if file_rel not in src_files_rel:
                        stale_path = os.path.join(root, file)
                        logger.info(f"  [清理] 删除洗版残留: {os.path.basename(stale_path)}")
                        try:
                            os.remove(stale_path)
                        except OSError as e:
                            logger.error(f"  [清理失败] {stale_path}: {e}")

        return linked_any

    def _link_file_smart(self, src: str, dst: str) -> bool:
        try:
            if not os.path.exists(dst):
                os.link(src, dst)
                return True
            
            src_stat = os.stat(src)
            dst_stat = os.stat(dst)
            
            if src_stat.st_ino == dst_stat.st_ino:
                return True
            else:
                logger.info(f"  [Update] 检测到文件变动(洗版)，更新链接: {os.path.basename(dst)}")
                os.remove(dst)
                os.link(src, dst)
                return True
        except OSError as e:
            logger.error(f"  [Link Error] {e}")
            return False

    def _cleanup_stale_folders(self, active_folders: Set[str]):
        removed_dirs = []
        if not os.path.exists(self.link_root):
            return removed_dirs

        current_dirs = [d for d in os.listdir(self.link_root) if os.path.isdir(os.path.join(self.link_root, d))]
        
        for d in current_dirs:
            if d not in active_folders:
                full_path = os.path.join(self.link_root, d)
                logger.info(f"  [清理] 榜单已移除，删除本地硬链: {d}")
                try:
                    shutil.rmtree(full_path)
                    removed_dirs.append(os.path.normpath(full_path))
                except Exception as e:
                    logger.error(f"  [清理失败] {d}: {e}")
        return removed_dirs

    def _check_same_filesystem(self, path1, path2):
        try:
            p1 = path1
            while not os.path.exists(p1):
                p1 = os.path.dirname(p1)
            p2 = path2
            while not os.path.exists(p2):
                p2 = os.path.dirname(p2)
            return os.stat(p1).st_dev == os.stat(p2).st_dev
        except:
            return False

    def _sanitize_filename(self, name):
        return re.sub(r'[\\/:*?"<>|]', '', name).strip()
