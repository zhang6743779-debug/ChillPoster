# core/importer.py
import logging
import requests
import re
import os
import sys
import json
import subprocess
import numpy as np
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

# 引用本地核心模块
import core.tmdb as tmdb
from core.douban import DoubanApi

try:
    from zhconv import convert as zh_convert
except Exception:
    zh_convert = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Importer")

class UniversalImporter:
    """
    全能榜单解析器 (V5.1 - 修复豆瓣搜索补全逻辑)
    已同步 custom_collection.py 的 ListImporter 核心逻辑
    """
    _last_logged_proxy_url = None
    
    # 增强版季号匹配正则，支持 (第二季) [第2季] 等格式
    SEASON_PATTERN = re.compile(r'(.*?)\s*[（(]?\s*(第?[一二三四五六七八九十百]+)\s*季\s*[)）]?')
    
    CHINESE_NUM_MAP = {
        '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
        '十一': 11, '十二': 12, '十三': 13, '十四': 14, '十五': 15, '十六': 16, '十七': 17, '十八': 18, '十九': 19, '二十': 20,
        '第一': 1, '第二': 2, '第三': 3, '第四': 4, '第五': 5, '第六': 6, '第七': 7, '第八': 8, '第九': 9, '第十': 10,
        '第十一': 11, '第十二': 12, '第十三': 13, '第十四': 14, '第十五': 15, '第十六': 16, '第十七': 17, '第十八': 18, '第十九': 19, '第二十': 20
    }

    DIGIT_TO_CHINESE_MAP = {
        1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '七', 8: '八', 9: '九',
        10: '十', 11: '十一', 12: '十二', 13: '十三', 14: '十四', 15: '十五',
        16: '十六', 17: '十七', 18: '十八', 19: '十九', 20: '二十'
    }

    def __init__(self, tmdb_api_key: str, proxy_url: str = None):
        self.tmdb_api_key = tmdb_api_key
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})

        # RSS 抓取专用直连会话：显式禁用环境代理与会话代理
        self.rss_session = requests.Session()
        self.rss_session.headers.update({'User-Agent': 'Mozilla/5.0'})
        self.rss_session.trust_env = False
        self.rss_session.proxies = {}

        self.douban_api = DoubanApi(cooldown_seconds=1.5)

        if proxy_url:
            self.proxies = {"http": proxy_url, "https": proxy_url}
            self.session.proxies.update(self.proxies)
            if proxy_url != UniversalImporter._last_logged_proxy_url:
                logger.info(f"Importer 已启用代理: {proxy_url}")
                UniversalImporter._last_logged_proxy_url = proxy_url
        else:
            self.proxies = None

    def normalize_string(self, s: str) -> str:
        """
        字符串归一化 (逻辑同步 custom_collection.py)
        去除标点、符号、空格，转小写
        """
        if not s: return ""
        return re.sub(r'[\s:：·\-*\'!,?.。]+', '', s).lower()

    def _is_all_chinese(self, text):
        if not text: return False
        for char in text:
            if '\u4e00' <= char <= '\u9fff':
                return True
        return False

    def fetch_items(self, url: str, default_type: str = 'Movie'):
        """统一入口"""
        items = []
        source_type = 'unknown'

        if not url: return []
        logger.info(f"开始解析 URL: {url} (默认类型: {default_type})")

        if url.startswith('maoyan://'):
            source_type = 'maoyan'
            items = self._get_from_maoyan(url)
        elif 'themoviedb.org/discover/' in url:
            source_type = 'tmdb_discover'
            items = self._get_from_tmdb_discover(url)
        elif 'themoviedb.org/list/' in url:
            source_type = 'tmdb_list'
            items = self._get_from_tmdb_list(url)
        elif 'douban.com/doulist' in url:
            source_type = 'douban'
            items = self._get_from_douban_doulist(url)
        else:
            source_type = 'rss'
            items = self._get_from_rss(url, default_type)

        verified_items = self._process_items_with_precision(items)
        
        logger.info(f"解析完成 [{source_type}]: 原始 {len(items)} -> 有效 {len(verified_items)}")
        return verified_items

    def _match_by_ids(self, imdb_id: str, tmdb_id: str, item_type: str):
        """
        通过 ID 匹配 (逻辑同步 custom_collection.py)
        """
        if tmdb_id:
            logger.debug(f"[匹配] 通过 TMDb ID 直接命中: {tmdb_id}")
            return tmdb_id
        if imdb_id:
            logger.debug(f"[匹配] 通过 IMDb ID 查找 TMDb: {imdb_id}")
            try:
                tmdb_id_from_imdb = tmdb.get_tmdb_id_by_imdb_id(imdb_id, self.tmdb_api_key, item_type)
                if tmdb_id_from_imdb:
                    logger.debug(f"[匹配] IMDb -> TMDb 命中: {imdb_id} -> {tmdb_id_from_imdb}")
                    return str(tmdb_id_from_imdb)
                else:
                    logger.debug(f"[匹配] IMDb 未找到对应 TMDb: {imdb_id}")
            except Exception as e:
                logger.debug(f"[匹配] IMDb 查找 TMDb 失败: {e}")
        return None

    def _process_items_with_precision(self, items):
        """
        [核心逻辑] 并发执行精准匹配
        逻辑完全同步 custom_collection.py -> find_first_match
        """
        results = []
        
        def process_single_item(item):
            # 获取原始信息
            original_source_title = item.get('title', '').strip()
            year = item.get('year')
            rss_imdb_id = item.get('imdb_id')
            douban_link = item.get('douban_link')
            
            # 这里的 type 可能是默认值，后续匹配可能会修正
            # custom_collection 中通过 definition.get('item_type') 传入列表，这里模拟该行为
            default_type = item.get('type', 'Movie')
            types_to_check = [default_type]
            if default_type == 'Movie':
                types_to_check.append('Series')
            else:
                types_to_check.append('Movie')

            # 辅助函数：构建返回结果
            def create_result(tmdb_id, item_type, confirmed_season=None):
                result = {
                    'tmdb_id': str(tmdb_id), 
                    'type': item_type, 
                    'title': original_source_title,
                    'year': year
                }
                if item_type == 'Series' and confirmed_season is not None:
                    result['season'] = confirmed_season
                return result

            # 1. RSS 自带 IMDb ID 匹配 (优先级最高)
            if rss_imdb_id:
                for item_type in types_to_check:
                    tmdb_id = self._match_by_ids(rss_imdb_id, None, item_type)
                    if tmdb_id:
                        # 尝试解析季号用于补充信息
                        _, s_num = self._parse_series_title_and_season(original_source_title)
                        return create_result(tmdb_id, item_type, s_num)

            # 2. 标题 + 年份 搜索 (常规匹配)
            # 清理标题
            cleaned_title = re.sub(r'^\s*\d+\.\s*', '', original_source_title)
            cleaned_title = re.sub(r'\s*\(\d{4}\)$', '', cleaned_title).strip()
            
            for item_type in types_to_check:
                match_result = self._match_title_to_tmdb(cleaned_title, item_type, year=year)
                
                if match_result:
                    tmdb_id, matched_type, matched_season = match_result
                    return create_result(tmdb_id, matched_type, matched_season)
            
            # --- [新增] 2.5 主动豆瓣搜索 (针对豆瓣RSS但没解析出链接的情况) ---
            # 这就是你之前没找到的代码，现在加上了
            if item.get('force_douban_search') and not douban_link:
                logger.debug(f"[豆瓣补全] 缺少链接，尝试搜索: '{original_source_title}'")
                try:
                    search_res = self.douban_api.search(original_source_title, count=1)
                    
                    # 适配 douban.py 的返回结构 {'items': [...]}
                    items_list = search_res.get('items', []) if isinstance(search_res, dict) else []
                    
                    if items_list:
                        first_hit = items_list[0]
                        # douban.py search 返回的结构通常在 'target' 中
                        target = first_hit.get('target') or first_hit
                        d_id = target.get('id')
                        
                        if d_id:
                            # 构造链接，让第3步逻辑去处理详情获取
                            douban_link = f"https://movie.douban.com/subject/{d_id}/"
                            logger.debug(f"[豆瓣补全] 搜索命中，生成链接: {douban_link}")
                except Exception as e:
                    logger.debug(f"[豆瓣补全] 主动搜索失败: {e}")

            # 3. 豆瓣辅助回退 (Douban Fallback)
            if douban_link:
                logger.debug(f"[匹配] 标题匹配失败，尝试豆瓣回退: '{original_source_title}'")
                try:
                    # 使用当前正在尝试的类型去抓取豆瓣
                    douban_details = self.douban_api.get_details_from_douban_link(
                        douban_link, 
                        mtype='movie' if types_to_check[0] == 'Movie' else 'tv'
                    )
                    
                    if douban_details:
                        # 3a. 豆瓣转 IMDb
                        imdb_id_from_douban = douban_details.get("imdb_id")
                        if not imdb_id_from_douban and douban_details.get("attrs", {}).get("imdb"):
                            imdb_ids = douban_details["attrs"]["imdb"]
                            if isinstance(imdb_ids, list) and len(imdb_ids) > 0:
                                imdb_id_from_douban = imdb_ids[0]

                        if imdb_id_from_douban:
                            logger.debug(f"[豆瓣回退] 获取 IMDb 成功: {imdb_id_from_douban}")
                            for item_type in types_to_check:
                                tmdb_id = self._match_by_ids(imdb_id_from_douban, None, item_type)
                                if tmdb_id:
                                    return create_result(tmdb_id, item_type)
                        
                        # 3b. 豆瓣转 Original Title
                        logger.debug("[豆瓣回退] IMDb 路径失败，尝试 original_title")
                        original_title = douban_details.get("original_title")
                        if original_title:
                            for item_type in types_to_check:
                                match_result = self._match_title_to_tmdb(original_title, item_type, year=year)
                                if match_result:
                                    tmdb_id, matched_type, matched_season = match_result
                                    logger.debug(f"[豆瓣回退] original_title 匹配成功: '{original_title}'")
                                    return create_result(tmdb_id, matched_type, matched_season)
                except Exception as e:
                    logger.debug(f"[豆瓣回退] 处理异常: {e}")

            # 4. 无年份回退搜索 (Last Resort)
            logger.debug(f"[匹配] 前置方案失败，尝试无年份回退: '{original_source_title}'")
            for item_type in types_to_check:
                match_result = self._match_title_to_tmdb(cleaned_title, item_type, year=None)
                if match_result:
                    tmdb_id, matched_type, matched_season = match_result
                    logger.info(f"[匹配] 无年份回退命中: '{original_source_title}'，年份可能不准确")
                    result = create_result(tmdb_id, matched_type, matched_season)
                    logger.info(f"[匹配结果] 命中: '{original_source_title}' -> TMDb:{tmdb_id} ({matched_type})")
                    return result

            logger.warning(f"[匹配结果] 未命中: '{original_source_title}'")
            return None

        # 限制并发数
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_item = {executor.submit(process_single_item, item): item for item in items}
            for future in as_completed(future_to_item):
                res = future.result()
                if res and res.get('tmdb_id'):
                    results.append(res)
        
        # 去重
        unique_items = []
        seen_keys = set()
        for item in results:
            key = f"{item['type']}-{item['tmdb_id']}-{item.get('season')}"
            if key not in seen_keys:
                seen_keys.add(key)
                unique_items.append(item)

        return unique_items

    def _build_clean_title_candidates(self, title: str):
        candidates = set()
        base = (title or '').strip()
        if not base:
            return []

        candidates.add(base)

        normalized = base.replace('【', ' ').replace('】', ' ').replace('·', ' ')
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        if normalized:
            candidates.add(normalized)

        cleaned = normalized
        cleaned = re.sub(r'\s*Part\s*\d+\s*$', '', cleaned, flags=re.I).strip()
        cleaned = re.sub(r'\s*(最终季|第[一二三四五六七八九十百\d]+季)\s*$', '', cleaned).strip()
        cleaned = re.sub(r'\s*年番\s*$', '', cleaned).strip()
        cleaned = re.sub(r'^[\d]+[\.、\s-]+', '', cleaned).strip()
        cleaned = re.sub(r'[·\.]\s*[^\s·\.]+季\s*$', '', cleaned).strip()
        if cleaned:
            candidates.add(cleaned)

        token_suffix_trimmed = re.sub(r'\s+[^\s]+季\s*$', '', cleaned).strip()
        if token_suffix_trimmed:
            candidates.add(token_suffix_trimmed)

        dot_suffix_trimmed = re.sub(r'[·\.]\s*[^·\.]+$', '', base).strip()
        if dot_suffix_trimmed:
            candidates.add(dot_suffix_trimmed)

        if zh_convert:
            for v in [zh_convert(base, 'zh-cn'), zh_convert(base, 'zh-tw'), zh_convert(cleaned, 'zh-cn'), zh_convert(cleaned, 'zh-tw')]:
                v = re.sub(r'\s+', ' ', (v or '')).strip()
                if v:
                    candidates.add(v)

        no_year = re.sub(r'(?:19\d{2}|20\d{2})\s*$', '', cleaned).strip()
        if no_year:
            candidates.add(no_year)

        tail_num_match = re.match(r'^(.*?)(\d{1,2})\s*$', no_year)
        if tail_num_match:
            base_name = tail_num_match.group(1).strip()
            season_num = int(tail_num_match.group(2))
            if base_name:
                candidates.add(base_name)
                if season_num in self.DIGIT_TO_CHINESE_MAP:
                    candidates.add(f"{base_name} 第{self.DIGIT_TO_CHINESE_MAP[season_num]}季")
                candidates.add(f"{base_name} 第{season_num}季")

        out = []
        seen_norm = set()
        for c in candidates:
            c = re.sub(r'\s+', ' ', c).strip()
            if not c:
                continue
            key = self.normalize_string(c)
            if not key or key in seen_norm:
                continue
            seen_norm.add(key)
            out.append(c)
        return out

    def _match_title_to_tmdb(self, title: str, item_type: str, year: str = None):
        """
        [智能匹配引擎]
        逻辑同步 custom_collection.py -> _match_title_to_tmdb
        包含：标题变体生成、精确/包含匹配、剧集季数验证
        """
        if not title: return None

        # --- A. 生成搜索候选项 ---
        titles_to_try = set(self._build_clean_title_candidates(title))

        # 1. 分割中文和英文标题
        match = re.match(r'([\u4e00-\u9fa5\s·0-9]+)[\s:：*]*(.*)', title.strip())
        if match:
            part1 = match.group(1).strip()
            part2 = match.group(2).strip()
            if part1: titles_to_try.add(part1)
            if part2: titles_to_try.add(part2)

        # 2. 中文数字映射 (一 -> 1)
        num_map = {'1': '一', '2': '二', '3': '三', '4': '四', '5': '五', '6': '六', '7': '七', '8': '八', '9': '九'}
        current_titles = list(titles_to_try)
        for t in current_titles:
            if any(num in t for num in num_map.keys()):
                new_title = t
                for num, char in num_map.items():
                    new_title = new_title.replace(num, char)
                titles_to_try.add(new_title)

        final_titles = list(titles_to_try)
        logger.debug(f"[匹配] 候选标题: '{title}' -> {final_titles}")

        first_search_results = None
        year_info = f" (年份: {year})" if year else ""

        # --- B. 遍历搜索 ---
        for title_variation in final_titles:
            if not title_variation: continue
            
            # TMDb 搜索：先带年份，失败后仅做一次无年份重试（取消年份窗口）
            results = tmdb.search_media(title_variation, self.tmdb_api_key, item_type, year=year)
            if (not results) and year:
                results = tmdb.search_media(title_variation, self.tmdb_api_key, item_type, year=None)

            if first_search_results is None:
                first_search_results = results

            if not results:
                continue

            norm_variation = self.normalize_string(title_variation)

            # C1. 精确匹配 (Exact Match)
            for result in results:
                res_title = result.get('title') if item_type == 'Movie' else result.get('name')
                res_original_title = result.get('original_title') if item_type == 'Movie' else result.get('original_name')

                norm_title = self.normalize_string(res_title)
                norm_original_title = self.normalize_string(res_original_title)

                if norm_variation == norm_title or norm_variation == norm_original_title:
                    tmdb_id = str(result.get('id'))
                    logger.info(f"[匹配] 精确匹配成功: '{title}'{year_info} -> {res_title} (ID: {tmdb_id})")
                    
                    # 剧集季数特殊处理
                    if item_type == 'Series':
                        # 解析原标题中的季数
                        _, s_num = self._parse_series_title_and_season(title)
                        if s_num is not None:
                            # 验证季数
                            if self._verify_season_in_results([result], title, s_num):
                                return tmdb_id, 'Series', s_num
                            else:
                                continue # 精确匹配但不包含季数，继续找

                    return tmdb_id, item_type, None
            
            # C2. 包含匹配 (Contains Match)
            for result in results:
                res_title = result.get('title') if item_type == 'Movie' else result.get('name')
                res_original_title = result.get('original_title') if item_type == 'Movie' else result.get('original_name')

                norm_title = self.normalize_string(res_title)
                norm_original_title = self.normalize_string(res_original_title)

                if norm_variation in norm_title or norm_variation in norm_original_title:
                    tmdb_id = str(result.get('id'))
                    logger.info(f"[匹配] 包含匹配成功: '{title}'{year_info} -> {res_title} (ID: {tmdb_id})")
                    
                    if item_type == 'Series':
                        _, s_num = self._parse_series_title_and_season(title)
                        if s_num is not None:
                             if self._verify_season_in_results([result], title, s_num):
                                return tmdb_id, 'Series', s_num
                             else:
                                continue

                    return tmdb_id, item_type, None

        # --- Series Logic: 如果上面没返回，针对剧集进行更深入的季数验证 ---
        if item_type == 'Series':
            show_name_parsed, season_number_to_validate = self._parse_series_title_and_season(title)
            show_name = show_name_parsed if show_name_parsed else title
            
            # 如果之前没搜到，或者需要验证季数
            if season_number_to_validate is not None:
                # 重新搜索 (使用解析后的剧名)
                results = tmdb.search_media(show_name, self.tmdb_api_key, 'Series', year=year)
                if not results and year:
                    results = tmdb.search_media(show_name, self.tmdb_api_key, 'Series', year=None)
                
                matched_id = self._verify_season_in_results(results[:5], show_name, season_number_to_validate)
                if matched_id:
                     return matched_id, 'Series', season_number_to_validate
                
                # 兜底：尝试原始标题
                if show_name != title:
                    logger.debug(f"[匹配] 兜底搜索，使用原始标题: '{title}'")
                    fallback_results = tmdb.search_media(title, self.tmdb_api_key, 'Series', year=None)
                    if fallback_results:
                        best_match = fallback_results[0]
                        return str(best_match.get('id')), 'Series', None

        # D. 回退机制 (Fallback)
        if first_search_results:
            first_result = first_search_results[0]
            tmdb_id = str(first_result.get('id'))
            logger.debug(f"[匹配] 标题 '{title}'{year_info} 精确与包含匹配失败，回退首个结果: {first_result.get('title') if item_type == 'Movie' else first_result.get('name')} (ID: {tmdb_id})")
            return tmdb_id, item_type, None

        return None

    def _verify_season_in_results(self, candidates_list, show_name, season_number_to_validate):
        """
        [Series Helper] 验证候选列表中是否有包含特定季数的剧集
        """
        if not candidates_list: return None
        
        norm_show_name = self.normalize_string(show_name)
        # 简单排序：名字越像越靠前
        candidates_list.sort(key=lambda x: 0 if self.normalize_string(x.get('name', '')) == norm_show_name else 1)
        
        logger.debug(f"[剧集验证] '{show_name}' 校验第 {season_number_to_validate} 季，候选数: {len(candidates_list)}")

        for candidate in candidates_list:
            candidate_id = str(candidate.get('id'))
            candidate_name = candidate.get('name')
            
            # 获取详情
            series_details = tmdb.get_tv_details(int(candidate_id), self.tmdb_api_key, append_to_response="seasons")
            
            if series_details and 'seasons' in series_details:
                has_season = False
                for season in series_details['seasons']:
                    if season.get('season_number') == season_number_to_validate:
                        has_season = True
                        break
                
                if has_season:
                    logger.debug(f"[剧集验证] 命中: '{candidate_name}' (ID: {candidate_id}) 包含第 {season_number_to_validate} 季")
                    return candidate_id
                else:
                    logger.debug(f"    - 候选 '{candidate_name}' (ID: {candidate_id}) 没有第 {season_number_to_validate} 季，跳过。")
        return None

    def _parse_series_title_and_season(self, text):
        """
        解析标题和季号 (逻辑同步)
        """
        match = self.SEASON_PATTERN.search(text)
        if match:
            title_part = match.group(1).strip()
            season_str = match.group(2)
            
            if season_str in self.CHINESE_NUM_MAP:
                s_num = self.CHINESE_NUM_MAP[season_str]
            elif season_str.isdigit():
                s_num = int(season_str)
            else:
                s_num = None
                
            if s_num:
                return title_part, s_num
        
        match_s = re.search(r'(.*?)\s*S(\d+)', text, re.I)
        if match_s:
            return match_s.group(1).strip(), int(match_s.group(2))
            
        return text, None

    def _execute_maoyan_fetch(self, maoyan_url, limit=20):
        """
        调用 maoyan_fetcher.py 子进程 (增强健壮性)
        """
        temp_output_file = f"maoyan_temp_{int(datetime.now().timestamp())}.json"
        content_key = maoyan_url.replace('maoyan://', '')
        parts = content_key.split('-')
        
        platform = 'all'
        valid_platforms = {'tencent', 'iqiyi', 'youku', 'mango'}
        if len(parts) > 1 and parts[-1] in valid_platforms:
            platform = parts[-1]
            type_part = '-'.join(parts[:-1])
        else:
            type_part = content_key
        
        types_to_fetch = [t.strip() for t in type_part.split(',') if t.strip()]
        
        base_dir = os.path.dirname(os.path.abspath(__file__))
        fetcher_script = os.path.join(base_dir, 'maoyan_fetcher.py')
        if not os.path.exists(fetcher_script):
            fetcher_script = fetcher_script + 'c'
        if not os.path.exists(fetcher_script):
            logger.error(f"找不到 maoyan_fetcher.py: {fetcher_script}")
            return []

        cmd = [
            sys.executable,
            fetcher_script,
            '--api-key', self.tmdb_api_key,
            '--output-file', temp_output_file,
            '--num', str(limit),
            '--platform', platform,
            '--types', *types_to_fetch
        ]

        try:
            logger.debug("[猫眼] 启动抓取子进程")
            subprocess.check_call(cmd, timeout=300)

            if os.path.exists(temp_output_file):
                with open(temp_output_file, 'r', encoding='utf-8') as f:
                    results = json.load(f)
                os.remove(temp_output_file)
                
                formatted = []
                for r in results:
                    formatted.append({
                        'title': r.get('title'),
                        'tmdb_id': r.get('tmdb_id') or r.get('id'),
                        'type': r.get('type'),
                        'year': r.get('year'),
                        'season': r.get('season')
                    })
                return formatted
        except subprocess.TimeoutExpired:
            logger.error("猫眼抓取子进程超时")
        except Exception as e:
            logger.error(f"猫眼抓取失败: {e}")
        finally:
            if os.path.exists(temp_output_file):
                try: os.remove(temp_output_file)
                except: pass

        return []

    def _get_from_maoyan(self, url):
        return self._execute_maoyan_fetch(url)

    def _get_from_rss(self, url, default_type='Movie'):
        """RSS 解析 (保持原有健壮性逻辑)"""
        items = []
        
        # 预判是否为豆瓣源
        is_douban_source = '/douban/' in url

        try:
            resp = self.rss_session.get(url, timeout=20)
            content = resp.text
            if content and 'encoding="gb2312"' in content.lower():
                 content = resp.content.decode('gb2312', errors='ignore')
            
            root = ET.fromstring(content)
            channel = root.find('channel')
            if channel is None: return []

            for item in channel.findall('item'):
                def _safe_get_text(xml_item, tag_name):
                    node = xml_item.find(tag_name)
                    if node is not None and node.text is not None:
                        return node.text.strip()
                    return ""

                title = _safe_get_text(item, 'title')
                desc = _safe_get_text(item, 'description')
                link = _safe_get_text(item, 'link')
                
                full_text = f"{title} {desc} {link}"
                
                year = None
                year_match = re.search(r'\b(19\d{2}|20\d{2})\b', full_text)
                if year_match: year = year_match.group(1)
                
                imdb_id = None
                imdb_match = re.search(r'tt\d{7,8}', full_text)
                if imdb_match: imdb_id = imdb_match.group(0)
                
                douban_link = None
                if 'douban.com' in link:
                    douban_link = link
                elif 'douban.com' in desc:
                    d_match = re.search(r'https?://(?:movie\.)?douban\.com/subject/\d+/?', desc)
                    if d_match: douban_link = d_match.group(0)

                if title:
                    clean_title = re.sub(r'(.*?)\s*(\(|\[).*', r'\1', title).strip()
                    clean_title = clean_title.replace('.', ' ')
                    
                    item_data = {
                        'title': clean_title, 
                        'year': year, 
                        'imdb_id': imdb_id, 
                        'douban_link': douban_link,
                        'type': default_type 
                    }

                    # [新增 2] 如果链接是空的，但源头是豆瓣 RSS，打上强制搜索标记
                    if is_douban_source and not douban_link and not imdb_id:
                        item_data['force_douban_search'] = True
                    
                    items.append(item_data)
        except Exception as e:
            logger.error(f"RSS解析失败: {e}")
        return items

    def _get_from_douban_doulist(self, url):
        """解析豆瓣豆列"""
        items = []
        base_url = url.split('?')[0]
        for page in range(3): 
            start = page * 25
            u = f"{base_url}?start={start}&sort=seq&playable=0&sub_type="
            try:
                headers = {'User-Agent': 'Mozilla/5.0'}
                resp = self.session.get(u, headers=headers, timeout=10)
                if resp.status_code != 200: break
                
                soup = BeautifulSoup(resp.text, 'html.parser')
                rows = soup.find_all('div', class_='doulist-item')
                if not rows: break
                
                for row in rows:
                    title_div = row.find('div', class_='title')
                    if not title_div: continue
                    link_tag = title_div.find('a')
                    if not link_tag: continue
                    
                    title = link_tag.get_text(strip=True)
                    link = link_tag.get('href')
                    
                    year = None
                    abstract = row.find('div', class_='abstract')
                    if abstract:
                        ym = re.search(r'\b(19\d{2}|20\d{2})\b', abstract.get_text())
                        if ym: year = ym.group(1)
                    
                    mtype = 'Series' if '/tv/' in link else 'Movie'
                    items.append({
                        'title': title, 
                        'year': year, 
                        'douban_link': link, 
                        'type': mtype
                    })
            except Exception as e:
                logger.error(f"豆瓣豆列解析错误: {e}")
                break
        return items

    def _get_from_tmdb_list(self, url):
        """解析 TMDb 片单"""
        match = re.search(r'themoviedb\.org/list/(\d+)', url)
        if not match: return []
        list_id = int(match.group(1))
        try:
            data = tmdb.get_list_details_tmdb(list_id, self.tmdb_api_key)
            if not data or 'items' not in data: return []
            res = []
            for i in data['items']:
                mtype = 'Movie' if i.get('media_type') == 'movie' else 'Series'
                res.append({
                    'title': i.get('title') if mtype == 'Movie' else i.get('name'),
                    'year': (i.get('release_date') or i.get('first_air_date') or '')[:4],
                    'tmdb_id': str(i.get('id')),
                    'type': mtype
                })
            return res
        except Exception as e:
            logger.error(f"TMDb 片单解析失败: {e}")
            return []

    def _get_from_tmdb_discover(self, url):
        """解析 TMDb 发现页 (支持动态日期)"""
        url = unquote(url)
        today = datetime.now().date()
        def repl(m):
            base = today
            if m.group(1) == 'tomorrow': base += timedelta(days=1)
            days = int(m.group(2)) if m.group(2) else 0
            return (base + timedelta(days=days)).isoformat()
        
        url = re.sub(r'\{(today|tomorrow)(\+\d+)?\}', repl, url)
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        
        try:
            is_movie = '/discover/movie' in url
            if is_movie:
                res = tmdb.discover_movie_tmdb(self.tmdb_api_key, params)
                raw_list = res.get('results', [])
                return [{'title': i.get('title'), 'year': (i.get('release_date') or '')[:4], 'tmdb_id': str(i.get('id')), 'type': 'Movie'} for i in raw_list]
            else:
                res = tmdb.discover_tv_tmdb(self.tmdb_api_key, params)
                raw_list = res.get('results', [])
                return [{'title': i.get('name'), 'year': (i.get('first_air_date') or '')[:4], 'tmdb_id': str(i.get('id')), 'type': 'Series'} for i in raw_list]
        except Exception as e:
             logger.error(f"TMDb 发现页解析失败: {e}")
             return []
