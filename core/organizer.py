# core/organizer.py
"""
MediaOrganizer - 媒体文件整理与刮削核心模块

参考实现: MoviePilot MediaChain (media.py)
职责：
  1. 识别视频文件的媒体类型 (电影 / 剧集)
  2. 按 Emby/Jellyfin 标准目录结构整理文件
  3. 生成 .nfo 元数据文件并下载海报图片
  4. 整理完成后触发回调 (对接 strm 生成等下游逻辑)

抽象接口 (由外部实现注入)：
  - transfer_media(src, dst)  -> 文件转移 (move / hardlink / 云盘API)
  - save_nfo(path, content)   -> NFO 文件写入
  - download_image(url, path) -> 图片下载
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from time import perf_counter

import logging

logger = logging.getLogger("ChillPoster.organizer")


# ==========================================
# 常量
# ==========================================
VIDEO_EXTENSIONS = {'.mp4', '.mpg', '.mkv', '.mpeg', '.ts', '.vob', '.iso', '.m4v', '.avi', '.3gp', '.wmv', '.webm', '.flv', '.mov', '.m2ts', '.rmvb', '.rm', '.asf', '.f4v', '.m2t', '.mts', '.mpe', '.tp', '.trp', '.divx', '.ogv', '.dv'}
BDMV_MARKERS = {'BDMV', 'STREAM', 'CLIPINF', 'PLAYLIST', 'BACKUP', 'CERTIFICATE', 'AACS'}
IMAGE_BASE_URL = "https://image.tmdb.org/t/p/original"

# 特殊季目录名 (对应原代码 RENAME_FORMAT_S0_NAMES)
SPECIAL_SEASON_NAMES = {"specials", "sp", "bonus", "extras", "幕后", "特辑"}


# ==========================================
# 枚举：四维目标分类
# ==========================================
class MediaType(Enum):
    MOVIE = "movie"        # 电影
    TV = "tv"              # 剧集根目录
    SEASON = "season"      # 剧集季目录
    EPISODE = "episode"    # 单集


# ==========================================
# 枚举：刮削元数据类型 (参考 ScrapingMetadata)
# ==========================================
class MetadataType(Enum):
    NFO = "nfo"
    POSTER = "poster"
    BACKDROP = "fanart"
    LOGO = "logo"
    DISC = "disc"
    BANNER = "banner"
    THUMB = "thumb"


# ==========================================
# 枚举：刮削策略 (参考 ScrapingPolicy)
# ==========================================
class ScrapingPolicy(Enum):
    SKIP = "skip"                    # 跳过
    MISSING_ONLY = "missing_only"    # 仅缺失时刮削 (默认)
    OVERWRITE = "overwrite"          # 覆盖已有文件


# ==========================================
# 图片类型关键词映射 (参考 IMAGE_METADATA_MAP)
# ==========================================
_IMAGE_KEYWORD_MAP: Dict[str, MetadataType] = {
    "poster": MetadataType.POSTER,
    "backdrop": MetadataType.BACKDROP,
    "fanart": MetadataType.BACKDROP,
    "background": MetadataType.BACKDROP,
    "logo": MetadataType.LOGO,
    "disc": MetadataType.DISC,
    "cdart": MetadataType.DISC,
    "banner": MetadataType.BANNER,
    "thumb": MetadataType.THUMB,
}


# ==========================================
# 数据类：刮削配置 (参考 ScrapingConfig)
# ==========================================
@dataclass
class ScrapingConfig:
    """
    媒体刮削策略配置，控制每种类型+元数据组合的刮削行为。

    用法:
        config = ScrapingConfig()  # 全部默认 MISSING_ONLY
        config = ScrapingConfig({"movie_nfo": "overwrite", "episode_thumb": "skip"})
    """
    _policies: Dict[Tuple[str, str], ScrapingPolicy] = field(default_factory=dict)

    # 默认配置: 所有类型都启用 MISSING_ONLY
    _DEFAULTS: Dict[str, List[str]] = field(default_factory=lambda: {
        "movie":   ["nfo", "poster", "backdrop", "logo", "disc", "banner", "thumb"],
        "tv":      ["nfo", "poster", "backdrop", "logo", "banner", "thumb"],
        "season":  ["nfo", "poster", "banner", "thumb"],
        "episode": ["nfo", "thumb"],
    }, repr=False)

    def __post_init__(self):
        # 先填充默认值
        for media_type, meta_types in self._DEFAULTS.items():
            for meta_type in meta_types:
                key = (media_type, meta_type)
                if key not in self._policies:
                    self._policies[key] = ScrapingPolicy.MISSING_ONLY

    def set_policy(self, media_type: Union[str, MediaType], metadata_type: Union[str, MetadataType], policy: ScrapingPolicy):
        """设置单条策略"""
        mt = media_type.value if isinstance(media_type, MediaType) else media_type
        md = metadata_type.value if isinstance(metadata_type, MetadataType) else metadata_type
        self._policies[(mt, md)] = policy

    def get_policy(self, media_type: Union[str, MediaType], metadata_type: Union[str, MetadataType]) -> ScrapingPolicy:
        """获取策略，未配置时返回 SKIP"""
        mt = media_type.value if isinstance(media_type, MediaType) else media_type
        md = metadata_type.value if isinstance(metadata_type, MetadataType) else metadata_type
        return self._policies.get((mt, md), ScrapingPolicy.SKIP)

    def should_scrape(self, media_type: Union[str, MediaType], metadata_type: Union[str, MetadataType], file_exists: bool) -> bool:
        """
        判断是否应该执行刮削。

        :param media_type: 媒体类型
        :param metadata_type: 元数据类型
        :param file_exists: 目标文件是否已存在
        :return: 是否应该刮削
        """
        policy = self.get_policy(media_type, metadata_type)

        if policy == ScrapingPolicy.SKIP:
            logger.trace(f"{media_type} {metadata_type} 刮削策略: skip")
            return False

        if not file_exists:
            return True

        if policy == ScrapingPolicy.OVERWRITE:
            logger.trace(f"{media_type} {metadata_type} 文件存在，覆盖模式")
            return True

        # MISSING_ONLY 且文件已存在
        return False

    @classmethod
    def from_dict(cls, config_dict: Dict[str, str]) -> "ScrapingConfig":
        """
        从扁平字典构建配置。
        格式: {"movie_nfo": "overwrite", "episode_thumb": "skip", ...}
        """
        policies = {}
        for key, value in config_dict.items():
            if "_" in key:
                media_type, meta_type = key.split("_", 1)
                try:
                    policies[(media_type, meta_type)] = ScrapingPolicy(value)
                except ValueError:
                    logger.warning(f"未知的刮削策略: {key}={value}")
        return cls(_policies=policies)


# ==========================================
# 数据类：整理结果
# ==========================================
@dataclass
class OrganizeResult:
    success: bool
    media_type: MediaType
    target_path: str           # 最终落盘的媒体文件/目录路径
    message: str = ""
    metadata_files: List[str] = field(default_factory=list)  # 生成的 nfo / 图片路径列表


# ==========================================
# 核心类：MediaOrganizer
# ==========================================
class MediaOrganizer:
    """
    媒体文件整理器。

    对标参考代码 MediaChain.scrape_metadata 的核心逻辑，
    剥离 StorageChain 和事件总线，保留纯整理规则。

    使用方式:
        organizer = MediaOrganizer(
            library_root="/media/EmbyLibrary",
            transfer_media=my_transfer_fn,
            save_nfo=my_nfo_fn,
            download_image=my_image_fn,
            on_complete=my_strm_callback,
        )

        # 整理电影
        result = organizer.organize_movie(
            src_video=Path("/downloads/Inception.2010.1080p.mkv"),
            tmdb_data={...},
        )

        # 整理目录 (刮削已有目录的元数据)
        result = organizer.scrape_directory(
            dir_path=Path("/media/Movie/Inception (2010) [tmdbid-27205]"),
            tmdb_data={...},
            media_type=MediaType.MOVIE,
        )
    """

    def __init__(
        self,
        library_root: str,
        transfer_media: Callable[[str, str], None],
        save_nfo: Callable[[str, str], None],
        download_image: Callable[[str, str], None],
        on_complete: Optional[Callable[[str], None]] = None,
        scraping_config: Optional[ScrapingConfig] = None,
    ):
        """
        Args:
            library_root:     媒体库根目录 (电影和剧集的父目录)
            transfer_media:   文件转移函数  (src_path: str, dest_path: str) -> None
            save_nfo:         NFO 写入函数   (path: str, content: str) -> None
            download_image:   图片下载函数   (url: str, path: str) -> None
            on_complete:      整理完成回调   (target_media_path: str) -> None
            scraping_config:  刮削策略配置 (None 则全部使用 MISSING_ONLY)
        """
        self.library_root = Path(library_root)
        self.transfer_media = transfer_media
        self.save_nfo = save_nfo
        self.download_image = download_image
        self.on_complete = on_complete
        self.config = scraping_config or ScrapingConfig()

    # ==========================================
    # 公开入口 - 电影整理
    # ==========================================

    def organize_movie(
        self,
        src_video: Path,
        tmdb_data: Dict[str, Any],
        *,
        is_bluray: bool = False,
        bluray_root: Optional[Path] = None,
        overwrite: bool = False,
    ) -> OrganizeResult:
        """
        整理单部电影 (从原始视频文件转移到媒体库并刮削)。

        Args:
            src_video:   原始视频文件路径
            tmdb_data:   TMDb 电影详情 (来自 get_movie_details)
            is_bluray:   是否为蓝光原盘 (BDMV)
            bluray_root: 蓝光原盘根目录 (is_bluray=True 时必填)
            overwrite:   全局覆盖标志 (无视策略强制覆盖已有文件)

        Returns:
            OrganizeResult
        """
        title = tmdb_data.get("title", "Unknown")
        year = (tmdb_data.get("release_date") or "0000")[:4]
        tmdb_id = tmdb_data.get("id", 0)

        folder_name = self._make_movie_folder_name(title, year, tmdb_id)
        movie_dir = self.library_root / "Movie" / folder_name

        try:
            if is_bluray and bluray_root:
                return self._organize_bluray_movie(bluray_root, movie_dir, tmdb_data, overwrite)
            else:
                return self._organize_single_movie(src_video, movie_dir, tmdb_data, overwrite)
        except Exception as e:
            logger.error(f"[Organizer] 电影整理失败 '{title}': {e}", exc_info=True)
            return OrganizeResult(False, MediaType.MOVIE, str(movie_dir), str(e))

    # ==========================================
    # 公开入口 - 剧集整理
    # ==========================================

    def organize_tv(
        self,
        src_video: Path,
        tmdb_data: Dict[str, Any],
        *,
        season_number: int,
        episode_number: int,
        episode_tmdb_data: Optional[Dict[str, Any]] = None,
        organize_scope: MediaType = MediaType.EPISODE,
        overwrite: bool = False,
    ) -> OrganizeResult:
        """
        整理电视剧。

        Args:
            src_video:           原始视频文件路径
            tmdb_data:           TMDb 剧集顶层详情 (来自 get_tv_details / aggregate)
            season_number:       季号
            episode_number:      集号
            episode_tmdb_data:   单集 TMDb 详情 (可选)
            organize_scope:      整理范围 (EPISODE / SEASON / TV)
            overwrite:           全局覆盖标志

        Returns:
            OrganizeResult
        """
        series_name = tmdb_data.get("name", "Unknown")
        year = (tmdb_data.get("first_air_date") or "0000")[:4]
        tmdb_id = tmdb_data.get("id", 0)

        folder_name = self._make_tv_folder_name(series_name, year, tmdb_id)
        series_dir = self.library_root / "TV" / folder_name

        try:
            if organize_scope == MediaType.EPISODE:
                return self._organize_episode(
                    src_video, series_dir, tmdb_data,
                    season_number, episode_number, episode_tmdb_data, overwrite,
                )
            elif organize_scope == MediaType.SEASON:
                return self._organize_season(series_dir, tmdb_data, season_number, overwrite)
            elif organize_scope == MediaType.TV:
                return self._organize_tv_root(series_dir, tmdb_data, overwrite)
            else:
                return OrganizeResult(False, organize_scope, str(series_dir), f"未知的整理范围: {organize_scope}")
        except Exception as e:
            logger.error(f"[Organizer] 剧集整理失败 '{series_name}': {e}", exc_info=True)
            return OrganizeResult(False, organize_scope, str(series_dir), str(e))

    # ==========================================
    # 公开入口 - 目录刮削 (已有目录补全元数据)
    # ==========================================

    def scrape_directory(
        self,
        dir_path: Path,
        tmdb_data: Dict[str, Any],
        media_type: MediaType,
        *,
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
        init_folder: bool = True,
        recursive: bool = True,
        overwrite: bool = False,
        video_stem: Optional[str] = None,
        season_image_dir: Optional[Path] = None,
    ) -> OrganizeResult:
        """
        对已有目录执行刮削 (生成/补全 NFO 和图片)。

        对标参考代码 scrape_metadata 的目录处理逻辑:
          - init_folder: 是否初始化当前目录级元数据
          - recursive:   是否递归处理子目录/子文件

        Args:
            dir_path:        目标目录路径
            tmdb_data:       TMDb 数据
            media_type:      刮削目标类型 (MOVIE / TV / SEASON / EPISODE)
            season_number:   季号 (SEASON/EPISODE 时必填)
            episode_number:  集号 (EPISODE 时必填)
            init_folder:     是否生成当前目录级元数据
            recursive:       是否递归处理子目录
            overwrite:       全局覆盖标志
            video_stem:      视频文件名主体 (用于 EPISODE 的 NFO/图片命名)

        Returns:
            OrganizeResult
        """
        meta_files: List[str] = []

        try:
            # 递归处理子目录 (对标 _handle_movie_directory / _handle_tv_directory)
            if recursive and dir_path.is_dir():
                for child in sorted(dir_path.iterdir()):
                    if child.is_dir():
                        # 跳过非季目录
                        if media_type == MediaType.TV:
                            child_season = self._parse_season_number(child.name)
                            if child_season is None:
                                continue
                            # 子目录按 SEASON 刮削
                            self.scrape_directory(
                                child, tmdb_data, MediaType.SEASON,
                                season_number=child_season,
                                init_folder=True, recursive=True, overwrite=overwrite,
                            )
                        elif media_type == MediaType.MOVIE:
                            # 电影目录递归 (非蓝光)
                            if not self.is_bluray_folder(child):
                                self.scrape_directory(
                                    child, tmdb_data, MediaType.MOVIE,
                                    init_folder=False, recursive=True, overwrite=overwrite,
                                )

            # 初始化当前目录元数据
            if init_folder:
                if media_type == MediaType.MOVIE:
                    # 蓝光原盘只生成 NFO
                    if self.is_bluray_folder(dir_path):
                        self._scrape_nfo(dir_path, tmdb_data, MediaType.MOVIE, overwrite)
                    else:
                        self._scrape_movie_dir(dir_path, tmdb_data, overwrite, video_stem)

                elif media_type == MediaType.TV:
                    self._scrape_tv_root(dir_path, tmdb_data, overwrite)

                elif media_type == MediaType.SEASON:
                    self._scrape_season_dir(dir_path, tmdb_data, season_number, overwrite, image_dir=season_image_dir)
                    if episode_number is not None and video_stem:
                        meta_files.extend(self._scrape_single_episode(
                            dir_path, tmdb_data, season_number, episode_number,
                            None, overwrite, video_stem,
                        ))

                elif media_type == MediaType.EPISODE:
                    if episode_number is not None and video_stem:
                        meta_files.extend(self._scrape_single_episode(
                            dir_path, tmdb_data, season_number, episode_number,
                            None, overwrite, video_stem,
                        ))
                    else:
                        self._scrape_episode_files(dir_path, tmdb_data, season_number, overwrite)

            return OrganizeResult(True, media_type, str(dir_path), metadata_files=meta_files)
        except Exception as e:
            logger.error(f"[Organizer] 目录刮削失败 '{dir_path}': {e}", exc_info=True)
            return OrganizeResult(False, media_type, str(dir_path), str(e))

    # ==========================================
    # 电影 - 单文件整理
    # ==========================================

    def _organize_single_movie(
        self,
        src_video: Path,
        movie_dir: Path,
        tmdb_data: Dict[str, Any],
        overwrite: bool,
    ) -> OrganizeResult:
        movie_dir.mkdir(parents=True, exist_ok=True)

        # 1. 转移视频文件 -> MovieDir/OriginalName.ext
        dest_video = movie_dir / src_video.name
        self.transfer_media(str(src_video), str(dest_video))
        logger.info(f"[Organizer] 电影视频已转移: {src_video.name} -> {movie_dir.name}/")

        # 2. 生成元数据 (NFO 命名与视频同名 stem.nfo，参考 _get_target_fileitem_and_path)
        meta_files = self._scrape_movie_dir(movie_dir, tmdb_data, overwrite, src_video.stem)

        # 3. 触发回调
        if self.on_complete:
            self.on_complete(str(dest_video))

        return OrganizeResult(True, MediaType.MOVIE, str(dest_video), metadata_files=meta_files)

    # ==========================================
    # 电影 - 蓝光原盘整理
    # ==========================================

    def _organize_bluray_movie(
        self,
        bluray_root: Path,
        movie_dir: Path,
        tmdb_data: Dict[str, Any],
        overwrite: bool,
    ) -> OrganizeResult:
        movie_dir.mkdir(parents=True, exist_ok=True)

        # 整体搬移，不破坏 BDMV 内部层级
        for item in bluray_root.iterdir():
            dest = movie_dir / item.name
            self.transfer_media(str(item), str(dest))

        logger.info(f"[Organizer] 蓝光原盘已转移: {bluray_root.name} -> {movie_dir.name}/")

        # 蓝光原盘 NFO 与目录同名 (参考: dir NFO = target_dir_path.name + .nfo)
        meta_files = self._scrape_movie_dir(movie_dir, tmdb_data, overwrite, movie_dir.stem, is_bluray=True)

        if self.on_complete:
            self.on_complete(str(movie_dir))

        return OrganizeResult(True, MediaType.MOVIE, str(movie_dir), metadata_files=meta_files)

    # ==========================================
    # 剧集 - 单集整理
    # ==========================================

    def _organize_episode(
        self,
        src_video: Path,
        series_dir: Path,
        tmdb_data: Dict[str, Any],
        season_number: int,
        episode_number: int,
        episode_tmdb_data: Optional[Dict[str, Any]],
        overwrite: bool,
    ) -> OrganizeResult:
        series_name = tmdb_data.get("name", "Unknown")

        # 目标季目录: SeriesDir/Season XX/
        season_dir = series_dir / f"Season {season_number:02d}"
        season_dir.mkdir(parents=True, exist_ok=True)

        # 规范化文件名: SeriesName SxxExx.ext
        ext = src_video.suffix
        safe_name = self._sanitize_filename(series_name)
        ep_filename = f"{safe_name} S{season_number:02d}E{episode_number:02d}{ext}"
        dest_video = season_dir / ep_filename

        # 1. 转移视频
        self.transfer_media(str(src_video), str(dest_video))
        logger.info(f"[Organizer] 单集已转移: {src_video.name} -> {series_name}/Season {season_number:02d}/{ep_filename}")

        # 2. 生成单集元数据 (NFO 与视频同名 stem.nfo，参考 _handle_tv_episode_file)
        ep_stem = dest_video.stem  # "SeriesName S01E01"
        meta_files = self._scrape_single_episode(
            season_dir, tmdb_data, season_number, episode_number,
            episode_tmdb_data, overwrite, ep_stem,
        )

        # 3. 触发回调
        if self.on_complete:
            self.on_complete(str(dest_video))

        return OrganizeResult(True, MediaType.EPISODE, str(dest_video), metadata_files=meta_files)

    # ==========================================
    # 剧集 - 季级元数据
    # ==========================================

    def _organize_season(
        self,
        series_dir: Path,
        tmdb_data: Dict[str, Any],
        season_number: int,
        overwrite: bool,
    ) -> OrganizeResult:
        season_dir = series_dir / f"Season {season_number:02d}"
        season_dir.mkdir(parents=True, exist_ok=True)

        meta_files = self._scrape_season_dir(season_dir, tmdb_data, season_number, overwrite)

        logger.info(f"[Organizer] 季元数据已生成: Season {season_number:02d}")
        return OrganizeResult(True, MediaType.SEASON, str(season_dir), metadata_files=meta_files)

    # ==========================================
    # 剧集 - 根目录级元数据
    # ==========================================

    def _organize_tv_root(
        self,
        series_dir: Path,
        tmdb_data: Dict[str, Any],
        overwrite: bool,
    ) -> OrganizeResult:
        series_dir.mkdir(parents=True, exist_ok=True)

        meta_files = self._scrape_tv_root(series_dir, tmdb_data, overwrite)

        logger.info(f"[Organizer] 剧集根目录元数据已生成: {series_dir.name}")
        return OrganizeResult(True, MediaType.TV, str(series_dir), metadata_files=meta_files)

    # ==========================================
    # 刮削执行层 (对标 _scrape_nfo_generic / _scrape_images_generic)
    # ==========================================

    def _scrape_movie_dir(
        self,
        movie_dir: Path,
        tmdb_data: Dict[str, Any],
        overwrite: bool,
        stem: Optional[str] = None,
        is_bluray: bool = False,
    ) -> List[str]:
        """刮削电影目录: NFO + 图片"""
        meta_files: List[str] = []

        # NFO: 与视频同名 (stem.nfo) 或与目录同名 (蓝光)
        nfo_stem = stem or movie_dir.stem
        nfo_path = movie_dir / f"{nfo_stem}.nfo"
        if self.config.should_scrape(MediaType.MOVIE, MetadataType.NFO, nfo_path.exists()) or overwrite:
            nfo_content = self._build_movie_nfo(tmdb_data)
            self.save_nfo(str(nfo_path), nfo_content)
            meta_files.append(str(nfo_path))

        # 图片
        meta_files.extend(self._scrape_images(
            movie_dir, tmdb_data, MediaType.MOVIE, overwrite,
        ))

        return meta_files

    def _scrape_tv_root(
        self,
        series_dir: Path,
        tmdb_data: Dict[str, Any],
        overwrite: bool,
    ) -> List[str]:
        """刮削剧集根目录: tvshow.nfo + poster/fanart/logo"""
        meta_files: List[str] = []

        # tvshow.nfo
        nfo_path = series_dir / "tvshow.nfo"
        if self.config.should_scrape(MediaType.TV, MetadataType.NFO, nfo_path.exists()) or overwrite:
            nfo_content = self._build_tvshow_nfo(tmdb_data)
            self.save_nfo(str(nfo_path), nfo_content)
            meta_files.append(str(nfo_path))

        # 图片 (TV 根目录不下载季图片，参考: _scrape_images_generic 跳过 season 前缀)
        meta_files.extend(self._scrape_images(
            series_dir, tmdb_data, MediaType.TV, overwrite,
            skip_season_images=True,
        ))

        return meta_files

    def _scrape_season_dir(
        self,
        season_dir: Path,
        tmdb_data: Dict[str, Any],
        season_number: Optional[int],
        overwrite: bool,
        image_dir: Optional[Path] = None,
    ) -> List[str]:
        """刮削季目录: season.nfo + 季海报"""
        meta_files: List[str] = []
        season_dir.mkdir(parents=True, exist_ok=True)

        # season.nfo
        nfo_path = season_dir / "season.nfo"
        if self.config.should_scrape(MediaType.SEASON, MetadataType.NFO, nfo_path.exists()) or overwrite:
            nfo_content = self._build_season_nfo(tmdb_data, season_number)
            self.save_nfo(str(nfo_path), nfo_content)
            meta_files.append(str(nfo_path))

        # 季图片写入 image_dir（若指定），否则写入 season_dir
        meta_files.extend(self._scrape_images(
            image_dir or season_dir, tmdb_data, MediaType.SEASON, overwrite,
            season_number=season_number,
        ))

        return meta_files

    def _scrape_single_episode(
        self,
        season_dir: Path,
        tmdb_data: Dict[str, Any],
        season_number: int,
        episode_number: int,
        episode_tmdb_data: Optional[Dict[str, Any]],
        overwrite: bool,
        ep_stem: str,
    ) -> List[str]:
        """刮削单集: NFO + thumb，文件命名与视频同名"""
        meta_files: List[str] = []

        # 获取集详情
        ep_data = episode_tmdb_data
        if not ep_data:
            episodes = tmdb_data.get("episodes_details", {})
            key = f"S{season_number}E{episode_number}"
            ep_data = episodes.get(key, {})

        still_path = ep_data.get("still_path") if ep_data else None
        thumb_filename = f"{ep_stem}-thumb.jpg" if still_path else ""

        # NFO: 与视频同名 stem.nfo (参考 _handle_tv_episode_file -> _get_target_fileitem_and_path)
        nfo_path = season_dir / f"{ep_stem}.nfo"
        if self.config.should_scrape(MediaType.EPISODE, MetadataType.NFO, nfo_path.exists()) or overwrite:
            nfo_content = self._build_episode_nfo(tmdb_data, ep_data, season_number, episode_number, thumb_filename)
            self.save_nfo(str(nfo_path), nfo_content)
            meta_files.append(str(nfo_path))

        # thumb: 与视频同名 stem-thumb.jpg (参考 EPISODE thumb 命名规则)
        if still_path:
            thumb_path = season_dir / f"{ep_stem}-thumb.jpg"
            if self.config.should_scrape(MediaType.EPISODE, MetadataType.THUMB, thumb_path.exists()) or overwrite:
                self.download_image(f"{IMAGE_BASE_URL}{still_path}", str(thumb_path))
                meta_files.append(str(thumb_path))

        return meta_files

    def _scrape_episode_files(
        self,
        season_dir: Path,
        tmdb_data: Dict[str, Any],
        season_number: Optional[int],
        overwrite: bool,
    ) -> List[str]:
        """刮削季目录下的所有集文件 (对标 _handle_tv_directory 递归处理)"""
        meta_files: List[str] = []
        if not season_dir.is_dir():
            return meta_files

        for child in sorted(season_dir.iterdir()):
            if child.is_file() and self.is_video_file(child):
                file_meta = self._parse_episode_from_filename(child.name)
                if file_meta:
                    _, ep_num = file_meta
                    meta_files.extend(self._scrape_single_episode(
                        season_dir, tmdb_data, season_number or 0, ep_num,
                        None, overwrite, child.stem,
                    ))

        return meta_files

    # ==========================================
    # 图片刮削 (对标 _scrape_images_generic)
    # ==========================================

    def _scrape_images(
        self,
        target_dir: Path,
        tmdb_data: Dict[str, Any],
        media_type: MediaType,
        overwrite: bool,
        season_number: Optional[int] = None,
        skip_season_images: bool = False,
    ) -> List[str]:
        """
        刮削图片，对标参考代码 _scrape_images_generic 的完整逻辑:
          - TV 根目录跳过季图片
          - SEASON 目录只下载当前季号的图片
          - 每种图片类型独立判断刮削策略
        """
        meta_files: List[str] = []

        # 收集图片 (从 TMDb 数据的 images 字段和顶层字段)
        image_items = self._collect_image_items(tmdb_data, media_type, season_number)

        download_tasks: List[Tuple[str, str]] = []
        for image_name, image_url in image_items:
            # 1. 匹配元数据类型
            metadata_type = self._match_image_metadata_type(image_name)
            if not metadata_type:
                continue

            # 2. TV 根目录跳过季图片 (参考: TV 模式下跳过 season 前缀的图片)
            if skip_season_images and image_name.lower().startswith("season"):
                continue

            # 3. SEASON 目录只下载当前季号的图片 (参考: 季号匹配过滤)
            if media_type == MediaType.SEASON and season_number is not None and image_name.lower().startswith("season"):
                image_season_str = "00" if "specials" in image_name.lower() else image_name[6:8]
                if image_season_str != str(season_number).rjust(2, "0"):
                    logger.trace(f"当前刮削季为: {season_number}，跳过非本季图片: {image_name}")
                    continue

            # 4. 策略判断
            if not self.config.should_scrape(media_type, metadata_type, False) and not overwrite:
                continue

            # 5. 确定文件名 (参考 _get_target_fileitem_and_path)
            ext = Path(image_name).suffix or ".jpg"
            # EPISODE thumb: 与视频同名
            if metadata_type == MetadataType.THUMB and media_type == MediaType.EPISODE:
                # 由 _scrape_single_episode 处理，这里跳过
                continue

            # 季图片命名: season-poster.jpg, season01-poster.jpg 等
            if image_name.lower().startswith("season"):
                filename = image_name
            else:
                filename = f"{metadata_type.value}{ext}"

            img_path = target_dir / filename
            if img_path.exists() and not overwrite:
                # 检查 MISSING_ONLY 策略
                if not self.config.should_scrape(media_type, metadata_type, True):
                    continue

            download_tasks.append((image_url, str(img_path)))

        def _download(task: Tuple[str, str]) -> str:
            image_url, img_path = task
            download_started = perf_counter()
            logger.trace(f"[Organizer] 图片刮削下载开始: {img_path}")
            self.download_image(image_url, img_path)
            logger.trace(f"[Organizer] 图片刮削下载结束: {img_path} | 耗时:{perf_counter() - download_started:.2f}s")
            return img_path

        if download_tasks:
            with ThreadPoolExecutor(max_workers=5) as img_executor:
                future_to_path = {
                    img_executor.submit(_download, task): task[1]
                    for task in download_tasks
                }
                wait(future_to_path)
                for future, img_path in future_to_path.items():
                    try:
                        meta_files.append(future.result())
                    except Exception as e:
                        logger.warning(f"[Organizer] 图片刮削下载失败: {img_path} | {e}")
                        raise

        return meta_files

    def _collect_image_items(
        self,
        tmdb_data: Dict[str, Any],
        media_type: MediaType,
        season_number: Optional[int] = None,
    ) -> List[Tuple[str, str]]:
        """
        收集图片项: (image_name, image_url)。
        从 TMDb 数据的顶层字段和 images 子字段中提取。
        兼容剧集聚合数据的嵌套结构 (series_details 包装)。
        """
        items: List[Tuple[str, str]] = []

        # 兼容聚合数据：TV 数据可能嵌套在 series_details 里
        source = tmdb_data
        if "series_details" in tmdb_data:
            source = tmdb_data["series_details"]

        # 顶层字段。thumb.jpg 没有独立 TMDb 字段，沿用 backdrop 作为横版缩略图。
        if media_type in (MediaType.MOVIE, MediaType.TV):
            for field_name, file_key, default_name in [
                ("poster_path", "poster_path", "poster.jpg"),
                ("backdrop_path", "backdrop_path", "fanart.jpg"),
                ("backdrop_path", "backdrop_path", "thumb.jpg"),
            ]:
                path = source.get(file_key)
                if path:
                    items.append((default_name, f"{IMAGE_BASE_URL}{path}"))

        # images 子数据 (logo, disc, banner 等)
        # 同名图片（如 logo）可能有多张不同语言版本，按语言优先级只取最优的一张：中文 > 无语言标记 > 英文 > 其他
        _lang_priority = {"zh": 0, "zh-cn": 0, "zh-tw": 1, "en": 2}
        images = source.get("images", {})
        for img_type, default_ext in [
            ("logos", ".png"),
            ("discs", ".png"),
            ("banners", ".jpg"),
        ]:
            imgs = images.get(img_type, [])
            if not imgs:
                continue
            best = sorted(imgs, key=lambda i: _lang_priority.get((i.get("iso_639_1") or "").lower(), 3))[0]
            file_path = best.get("file_path", "")
            if file_path:
                items.append((f"{img_type.rstrip('s')}{default_ext}", f"{IMAGE_BASE_URL}{file_path}"))

        # 季海报 (从 seasons 列表)
        if season_number is not None:
            for s in source.get("seasons", []):
                if s.get("season_number") == season_number:
                    poster = s.get("poster_path")
                    if poster:
                        items.append((f"season{season_number:02d}-poster.jpg", f"{IMAGE_BASE_URL}{poster}"))
                    break

        # 全量季海报 (TV 根目录刮削时用)
        if media_type == MediaType.TV:
            for s in source.get("seasons", []):
                s_num = s.get("season_number")
                poster = s.get("poster_path")
                if s_num is not None and poster:
                    items.append((f"season{s_num:02d}-poster.jpg", f"{IMAGE_BASE_URL}{poster}"))

        return items

    def _match_image_metadata_type(self, image_name: str) -> Optional[MetadataType]:
        """根据图片名称关键词匹配元数据类型 (参考 IMAGE_METADATA_MAP)"""
        name_lower = image_name.lower()
        for keyword, meta_type in _IMAGE_KEYWORD_MAP.items():
            if keyword in name_lower:
                return meta_type
        return None

    # ==========================================
    # NFO 通用刮削入口 (对标 _scrape_nfo_generic)
    # ==========================================

    def _scrape_nfo(
        self,
        target_dir: Path,
        tmdb_data: Dict[str, Any],
        media_type: MediaType,
        overwrite: bool,
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
        ep_data: Optional[Dict[str, Any]] = None,
        stem: Optional[str] = None,
    ) -> Optional[str]:
        """
        通用 NFO 刮削，对标 _scrape_nfo_generic。

        Returns:
            NFO 文件路径，跳过时返回 None
        """
        # 确定 NFO 文件名
        if media_type == MediaType.MOVIE:
            filename = f"{stem or target_dir.stem}.nfo"
        elif media_type == MediaType.TV:
            filename = "tvshow.nfo"
        elif media_type == MediaType.SEASON:
            filename = "season.nfo"
        elif media_type == MediaType.EPISODE:
            filename = f"{stem or target_dir.stem}.nfo"
        else:
            return None

        nfo_path = target_dir / filename
        file_exists = nfo_path.exists()

        if not self.config.should_scrape(media_type, MetadataType.NFO, file_exists) and not overwrite:
            return None

        # 生成内容
        if media_type == MediaType.MOVIE:
            content = self._build_movie_nfo(tmdb_data)
        elif media_type == MediaType.TV:
            content = self._build_tvshow_nfo(tmdb_data)
        elif media_type == MediaType.SEASON:
            content = self._build_season_nfo(tmdb_data, season_number)
        elif media_type == MediaType.EPISODE:
            if not ep_data:
                episodes = tmdb_data.get("episodes_details", {})
                key = f"S{season_number}E{episode_number}"
                ep_data = episodes.get(key, {})
            thumb_filename = f"{stem or target_dir.stem}-thumb.jpg" if (ep_data or {}).get("still_path") else ""
            content = self._build_episode_nfo(tmdb_data, ep_data or {}, season_number, episode_number, thumb_filename)
        else:
            return None

        self.save_nfo(str(nfo_path), content)
        return str(nfo_path)

    # ==========================================
    # NFO XML 构建
    # ==========================================

    def _build_movie_nfo(self, data: Dict[str, Any]) -> str:
        """构建电影 movie.nfo XML 内容"""
        title = self._xml_escape(data.get("title", ""))
        original_title = self._xml_escape(data.get("original_title", ""))
        sort_title = self._xml_escape(data.get("title", ""))
        year = (data.get("release_date") or "")[:4]
        plot = self._xml_escape(data.get("overview", ""))
        rating = data.get("vote_average", 0)
        votes = data.get("vote_count", 0)
        runtime = data.get("runtime", 0)
        tagline = self._xml_escape(data.get("tagline", ""))
        imdb_id = data.get("imdb_id", "") or ""
        tmdb_id = data.get("id", "")

        genres_xml = self._tags_xml(data.get("genres", []), "genre")
        studios_xml = self._tags_xml(data.get("production_companies", []), "studio")
        countries_xml = self._tags_xml(data.get("production_countries", []), "country")

        # 导演 / 编剧
        directors_xml = ""
        writers_xml = ""
        credits = data.get("credits", {})
        for crew in credits.get("crew", []):
            job = crew.get("job", "")
            name = self._xml_escape(crew.get("name", ""))
            if job == "Director":
                directors_xml += f"  <director>{name}</director>\n"
            elif job in ("Writer", "Screenplay", "Story"):
                writers_xml += f"  <credits>{name}</credits>\n"

        cast_xml = self._build_cast_xml(credits.get("cast", [])[:20])

        return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>{title}</title>
  <originaltitle>{original_title}</originaltitle>
  <sorttitle>{sort_title}</sorttitle>
  <year>{year}</year>
  <plot>{plot}</plot>
  <tagline>{tagline}</tagline>
  <rating>{rating}</rating>
  <votes>{votes}</votes>
  <runtime>{runtime}</runtime>
  <imdb>{imdb_id}</imdb>
  <tmdbid>{tmdb_id}</tmdbid>
{genres_xml}{studios_xml}{countries_xml}{directors_xml}{writers_xml}{cast_xml}</movie>"""

    def _build_tvshow_nfo(self, data: Dict[str, Any]) -> str:
        """构建剧集 tvshow.nfo XML 内容"""
        title = self._xml_escape(data.get("name", ""))
        original_title = self._xml_escape(data.get("original_name", ""))
        sort_title = self._xml_escape(data.get("name", ""))
        year = (data.get("first_air_date") or "")[:4]
        plot = self._xml_escape(data.get("overview", ""))
        rating = data.get("vote_average", 0)
        votes = data.get("vote_count", 0)
        status = self._xml_escape(data.get("status", ""))
        tmdb_id = data.get("id", "")

        ext_ids = data.get("external_ids", {})
        imdb_id = ext_ids.get("imdb_id", "") or "" if ext_ids else ""

        genres_xml = self._tags_xml(data.get("genres", []), "genre")
        studios_xml = self._tags_xml(data.get("production_companies", []), "studio")

        # 演员 (aggregate_credits 优先，参考原代码)
        cast_source = data.get("credits", {}).get("cast", [])
        if data.get("aggregate_credits", {}).get("cast"):
            agg_cast = data["aggregate_credits"]["cast"]
            mapped = []
            for actor in agg_cast:
                new_actor = actor.copy()
                roles = actor.get("roles", [])
                if roles and "character" in roles[0]:
                    new_actor["character"] = roles[0]["character"]
                mapped.append(new_actor)
            cast_source = mapped

        cast_xml = self._build_cast_xml(cast_source[:20])

        return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>{title}</title>
  <originaltitle>{original_title}</originaltitle>
  <sorttitle>{sort_title}</sorttitle>
  <year>{year}</year>
  <plot>{plot}</plot>
  <rating>{rating}</rating>
  <votes>{votes}</votes>
  <status>{status}</status>
  <imdb>{imdb_id}</imdb>
  <tmdbid>{tmdb_id}</tmdbid>
{genres_xml}{studios_xml}{cast_xml}</tvshow>"""

    def _build_season_nfo(self, data: Dict[str, Any], season_number: Optional[int]) -> str:
        """构建季 season.nfo XML 内容"""
        series_name = self._xml_escape(data.get("name", ""))
        tmdb_id = data.get("id", "")
        sn = season_number or 0

        season_data = {}
        for s in data.get("seasons", []):
            if s.get("season_number") == sn:
                season_data = s
                break

        season_name = self._xml_escape(season_data.get("name", f"Season {sn}"))
        overview = self._xml_escape(season_data.get("overview", ""))

        return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<season>
  <title>{season_name}</title>
  <showtitle>{series_name}</showtitle>
  <season>{sn}</season>
  <plot>{overview}</plot>
  <tmdbid>{tmdb_id}</tmdbid>
</season>"""

    def _build_episode_nfo(
        self,
        series_data: Dict[str, Any],
        ep_data: Dict[str, Any],
        season_number: int,
        episode_number: int,
        thumb_filename: str = "",
    ) -> str:
        """构建单集 episode.nfo XML 内容"""
        series_name = self._xml_escape(series_data.get("name", ""))
        ep_title = self._xml_escape(ep_data.get("name", f"Episode {episode_number}"))
        overview = self._xml_escape(ep_data.get("overview", ""))
        rating = ep_data.get("vote_average", 0)
        votes = ep_data.get("vote_count", 0)
        air_date = ep_data.get("air_date", "")
        runtime = ep_data.get("runtime", 0)
        tmdb_id = series_data.get("id", "")

        credits = ep_data.get("credits", {})
        cast_xml = self._build_cast_xml(credits.get("cast", [])[:10])
        thumb_xml = f"  <thumb>{self._xml_escape(thumb_filename)}</thumb>\n" if thumb_filename else ""

        return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <title>{ep_title}</title>
  <showtitle>{series_name}</showtitle>
  <season>{season_number}</season>
  <episode>{episode_number}</episode>
  <aired>{air_date}</aired>
  <plot>{overview}</plot>
  <rating>{rating}</rating>
  <votes>{votes}</votes>
  <runtime>{runtime}</runtime>
{thumb_xml}  <tmdbid>{tmdb_id}</tmdbid>
{cast_xml}</episodedetails>"""

    # ==========================================
    # 工具方法
    # ==========================================

    @staticmethod
    def _make_movie_folder_name(title: str, year: str, tmdb_id: int) -> str:
        safe_title = MediaOrganizer._sanitize_filename(title)
        return f"{safe_title} ({year}) [tmdbid-{tmdb_id}]"

    @staticmethod
    def _make_tv_folder_name(name: str, year: str, tmdb_id: int) -> str:
        safe_name = MediaOrganizer._sanitize_filename(name)
        return f"{safe_name} ({year}) [tmdbid-{tmdb_id}]"

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        return re.sub(r'[\\/:*?"<>|]', '', name).strip()

    @staticmethod
    def _xml_escape(text: str) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    @staticmethod
    def is_video_file(path: Path) -> bool:
        return path.suffix.lower() in VIDEO_EXTENSIONS

    @staticmethod
    def is_bluray_folder(path: Path) -> bool:
        """
        判断路径是否为蓝光原盘目录 (参考 StorageChain.is_bluray_folder)。
        检测逻辑: 目录下包含 BDMV 子目录，或包含 STREAM/CLIPINF 等蓝光特征目录。
        """
        check_path = path if path.is_dir() else path.parent

        if check_path.name.upper() == "BDMV":
            return True

        try:
            child_names = {p.name.upper() for p in check_path.iterdir() if p.is_dir()}
            if "BDMV" in child_names:
                return True
            if child_names & BDMV_MARKERS:
                return True
        except (PermissionError, OSError):
            pass

        return False

    @staticmethod
    def _parse_season_number(name: str) -> Optional[int]:
        """
        从目录名解析季号 (参考 MetaInfo 逻辑)。
        支持: "Season 01", "S01", "第1季", "Season1" 等。
        """
        name_lower = name.lower().strip()

        # 特殊季
        if name_lower in SPECIAL_SEASON_NAMES:
            return 0

        patterns = [
            r's(?:eason)?[\s_]*(\d{1,2})',
            r'第\s*(\d{1,2})\s*季',
            r'season[\s]*(\d{1,2})',
        ]
        for pat in patterns:
            m = re.search(pat, name_lower)
            if m:
                return int(m.group(1))

        return None

    @staticmethod
    def _parse_episode_from_filename(filename: str) -> Optional[Tuple[int, int]]:
        """
        从文件名解析季号和集号。
        支持: "S01E03", "E03", "第3集" 等。
        Returns: (season, episode) 或 None
        """
        # SxxExx
        m = re.search(r'[Ss](\d{1,2})[Ee](\d{1,3})', filename)
        if m:
            return (int(m.group(1)), int(m.group(2)))

        # Exx (无季号)
        m = re.search(r'[Ee](\d{1,3})', filename)
        if m:
            return (0, int(m.group(1)))

        return None

    def _extract_logo_path(self, data: Dict[str, Any]) -> Optional[str]:
        """从 TMDb images 数据中提取 logo 路径 (优先中文 -> 英文 -> 首个)"""
        images = data.get("images", {})
        logos = images.get("logos", [])
        if not logos:
            return None

        for logo in logos:
            if logo.get("iso_639_1") == "zh":
                return logo.get("file_path")
        for logo in logos:
            if logo.get("iso_639_1") == "en":
                return logo.get("file_path")
        return logos[0].get("file_path")

    def _tags_xml(self, items: List[Dict], tag_name: str) -> str:
        """通用标签列表转 XML"""
        xml = ""
        for item in items:
            name = self._xml_escape(item.get("name", ""))
            if name:
                xml += f"  <{tag_name}>{name}</{tag_name}>\n"
        return xml

    def _build_cast_xml(self, cast: List[Dict]) -> str:
        """构建演员 XML 片段"""
        xml = ""
        for actor in cast:
            a_name = self._xml_escape(actor.get("name", ""))
            role = self._xml_escape(actor.get("character", ""))
            order = actor.get("order", 0)
            thumb = actor.get("profile_path", "")
            thumb_url = f"{IMAGE_BASE_URL}{thumb}" if thumb else ""
            xml += f"""  <actor>
    <name>{a_name}</name>
    <role>{role}</role>
    <order>{order}</order>
    <thumb>{thumb_url}</thumb>
  </actor>
"""
        return xml
