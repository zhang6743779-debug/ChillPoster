import requests
import random
import os
import urllib3
import base64
import json
import time
import logging
import threading
from copy import deepcopy
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from threading import BoundedSemaphore

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("EmbyClient")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except Exception:
        value = default
    return min(max(value, minimum), maximum)


DISCOVER_SCAN_ITEM_PAGE_LIMIT = _env_int("CHILLPOSTER_DISCOVER_SCAN_ITEM_PAGE_LIMIT", 1000, 100, 5000)
DISCOVER_EPISODE_PAGE_LIMIT = _env_int("CHILLPOSTER_DISCOVER_EPISODE_PAGE_LIMIT", 5000, 500, 10000)

DEFAULT_LIBRARY_LOCALE_OPTIONS = {
    "PreferredMetadataLanguage": "zh",
    "MetadataCountryCode": "CN",
    "PreferredImageLanguage": "zh",
}

class EmbyClient:
    """
    Emby API 客户端 (V6.3 - 优化版)
    修改记录:
    1. get_libraries 增加返回物理路径 (Locations)，用于 Webhook 路径匹配。
    """
    def __init__(self, host, key, public_host=None):
        self.host = host.rstrip('/')
        self.public_host = public_host.rstrip('/') if public_host else self.host
        self.key = key
        self.user_id = None
        
        # --- 连接池 (仅用于查询元数据，不限制上传速度) ---
        self.session = requests.Session()
        
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS"]
        )
        
        adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        self.session.headers.update({
            "X-Emby-Token": key, 
            "User-Agent": "PosterMaker/Web/6.0"
        })
        
        # 强制直连配置
        self.proxies = { "http": None, "https": None }
        self.timeout = 30

        # 并发控制 (仅限制查询，不限制上传)
        self.semaphore = BoundedSemaphore(20)

    def close(self):
        """关闭连接池"""
        try:
            self.session.close()
        except:
            pass

    def _request(self, method, endpoint, **kwargs):
        """统一请求封装 (仅用于查询)"""
        if endpoint.startswith("http"):
            url = endpoint
        else:
            url = f"{self.host}/{endpoint.lstrip('/')}"

        kwargs.setdefault('timeout', self.timeout)
        kwargs.setdefault('verify', False)
        kwargs.setdefault('proxies', self.proxies)

        with self.semaphore:
            try:
                resp = self.session.request(method, url, **kwargs)
                resp.raise_for_status()
                try:
                    return resp.json()
                except:
                    return resp
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    # 404不打印error，保持日志干净
                    pass
                else:
                    try:
                        resp_body = e.response.text
                    except:
                        resp_body = ""
                    logger.error(f"HTTP 请求失败 ({e.response.status_code}): {url} | {e} | body={resp_body[:500]}")
                raise
            except Exception as e:
                logger.error(f"请求异常: {e} | URL: {url}")
                raise

    def _get_user_id(self):
        if self.user_id:
            return self.user_id
        try:
            res = self._request("GET", "emby/Users")
            for user in res:
                if user.get('Policy', {}).get('IsAdministrator', False):
                    self.user_id = user['Id']
                    return self.user_id
        except Exception:
            pass
        return None

    # =========================================================================
    # ★★★ 核心加速区：上传逻辑 ★★★
    # =========================================================================

    def _get_boxset_map(self):
        """[复刻] 获取合集映射"""
        uid = self._get_user_id()
        mapping = {}
        try:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": "BoxSet,Folder",
                "Fields": "ParentId"
            }
            # 使用 session 查询以减轻服务器握手压力
            endpoint = f"emby/Users/{uid}/Items" if uid else "emby/Items"
            res = self._request("GET", endpoint, params=params)
            
            items = res.get('Items', []) if isinstance(res, dict) else []
            for item in items:
                mapping[item['Name']] = item['Id']
        except: 
            pass
        return mapping

    def _resolve_real_id(self, item_id):
        """[复刻] 反查真实ID"""
        uid = self._get_user_id()
        try:
            endpoint = f"emby/Users/{uid}/Items/{item_id}" if uid else f"emby/Items/{item_id}"
            info = self._request("GET", endpoint)
            
            name = info.get('Name')
            if not name: return None
            
            boxset_map = self._get_boxset_map()
            return boxset_map.get(name)
        except: 
            return None

    def upload_cover(self, item_id, image_data):
        """
        [极速模式] 上传封面
        ★ 关键修改：为了速度，这里不使用 self.session 连接池。
        ★ 而是使用 requests 原生请求，这意味着没有并发上限（取决于你的线程数）。
        """
        mime_type = "image/jpeg"
        if image_data.startswith(b'\x89PNG'): mime_type = "image/png"
        
        # 构造独立的 Headers，不依赖 session
        base_headers = {
            "X-Emby-Token": self.key, 
            "User-Agent": "PosterMaker/Web/6.0",
            "Content-Type": mime_type
        }

        # 定义内部函数 1：尝试二进制上传 (使用 requests 直接发)
        def _try_binary_upload(target_id):
            url = f"{self.host}/emby/Items/{target_id}/Images/Primary"
            try:
                # [核心差异] 使用 requests.post 而不是 self.session.post
                # 这避免了连接池锁等待，实现最大化并发
                res = requests.post(
                    url, 
                    headers=base_headers, 
                    data=image_data, 
                    verify=False, 
                    proxies=self.proxies, # 确保走直连
                    timeout=self.timeout
                )
                if res.status_code in [200, 204]: return True
                return False
            except: return False

        # 定义内部函数 2：尝试 Base64 上传
        def _try_base64_upload(target_id):
            url = f"{self.host}/emby/Items/{target_id}/Images/Primary"
            try:
                b64_data = base64.b64encode(image_data)
                res = requests.post(
                    url, 
                    headers=base_headers, 
                    data=b64_data, 
                    verify=False, 
                    proxies=self.proxies,
                    timeout=self.timeout
                )
                if res.status_code in [200, 204]: return True
                return False
            except: return False

        # Emby 4.10 beta expects a base64 image body for this endpoint. Sending
        # raw bytes first works on some servers but causes noisy 500 errors here.
        if _try_base64_upload(item_id): return True
        if _try_binary_upload(item_id): return True
        
        # 2. 如果失败，尝试反查真实 ID
        real_id = self._resolve_real_id(item_id)
        if real_id and str(real_id) != str(item_id):
            if _try_base64_upload(real_id): return True
            if _try_binary_upload(real_id): return True
            
        return False

    # =========================================================================
    # ★★★ 其他功能 (保持不变) ★★★
    # =========================================================================

    def download_cover(self, item_id):
        try:
            # 下载也建议用 requests 直连，避免抢占查询通道
            url = f"{self.host}/emby/Items/{item_id}/Images/Primary"
            res = requests.get(url, headers={"X-Emby-Token": self.key}, verify=False, proxies=self.proxies, timeout=self.timeout)
            if res.status_code == 200: return res.content
        except: pass

        try:
            info = self._request("GET", f"emby/Items/{item_id}")
            if "Primary" in info.get("ImageTags", {}):
                tag = info["ImageTags"]["Primary"]
                url = f"{self.host}/emby/Items/{item_id}/Images/Primary?tag={tag}"
                res = requests.get(url, headers={"X-Emby-Token": self.key}, verify=False, proxies=self.proxies, timeout=self.timeout)
                if res.status_code == 200: return res.content
        except: pass

        real_id = self._resolve_real_id(item_id)
        if real_id and str(real_id) != str(item_id):
            return self.download_cover(real_id)
        return None

    def get_primary_image_url(self, item_id, max_height=500, quality=90):
        try:
            info = self._request("GET", f"emby/Items/{item_id}")
            image_tags = info.get("ImageTags", {}) if isinstance(info, dict) else {}
            primary_tag = image_tags.get("Primary")
            if primary_tag:
                return (
                    f"{self.public_host}/emby/Items/{item_id}"
                    f"/Images/Primary?tag={primary_tag}&quality={quality}&maxHeight={max_height}"
                )
        except:
            pass

        real_id = self._resolve_real_id(item_id)
        if real_id and str(real_id) != str(item_id):
            return self.get_primary_image_url(real_id, max_height=max_height, quality=quality)
        return ""

    def test_connection(self, require_library_access=False):
        try:
            self._request("GET", "emby/System/Info", timeout=5)
            if require_library_access:
                self.get_libraries(strict=True)
            elif not self._get_user_id():
                return False
            return True
        except:
            return False

    def get_server_id(self):
        try:
            info = self._request("GET", "emby/System/Info", timeout=10)
            if isinstance(info, dict):
                return info.get("Id") or info.get("ServerId") or info.get("SystemId")
        except Exception:
            pass
        return None

    def get_scheduled_tasks(self):
        """获取 Emby 计划任务列表，兼容部分版本对隐藏/禁用任务的过滤。"""
        tasks = []
        seen = set()
        query_variants = (
            None,
            {"IsHidden": "true"},
            {"IsHidden": "false"},
            {"IsEnabled": "false"},
        )
        for params in query_variants:
            try:
                res = self._request("GET", "emby/ScheduledTasks", timeout=15, params=params or {})
            except Exception:
                if not tasks:
                    raise
                continue
            if not isinstance(res, list):
                continue
            for item in res:
                if not isinstance(item, dict):
                    continue
                task_id = item.get("Id") or item.get("Key") or item.get("Name")
                if not task_id or task_id in seen:
                    continue
                seen.add(task_id)
                tasks.append(item)
        return tasks

    def get_scheduled_task(self, task_id):
        """获取单个 Emby 计划任务详情。"""
        if not task_id:
            raise ValueError("task_id is required")
        return self._request("GET", f"emby/ScheduledTasks/{task_id}", timeout=15)

    def update_scheduled_task_triggers(self, task_id, triggers):
        """更新指定 Emby 计划任务的触发器。"""
        if not task_id:
            raise ValueError("task_id is required")
        if not isinstance(triggers, list):
            raise ValueError("triggers must be a list")
        return self._request("POST", f"emby/ScheduledTasks/{task_id}/Triggers", json=triggers, timeout=15)

    def run_scheduled_task(self, task_id):
        """立即运行指定 Emby 计划任务。"""
        if not task_id:
            raise ValueError("task_id is required")
        return self._request("POST", f"emby/ScheduledTasks/Running/{task_id}", timeout=15)

    def stop_scheduled_task(self, task_id):
        """停止正在运行的 Emby 计划任务。"""
        if not task_id:
            raise ValueError("task_id is required")
        return self._request("DELETE", f"emby/ScheduledTasks/Running/{task_id}", timeout=15)

    def get_libraries(self, strict=False):
            """
            [优化版] 优先使用 Users/Views 获取用户设置的正确排序，
            同时合并 VirtualFolders 的物理路径信息 (Locations)。
            strict=True 时，任一关键接口鉴权失败都会直接抛错，避免把鉴权失败误判为空库。
            """
            paths_map = {}
            vf_items = []
            try:
                res = self._request("GET", "emby/Library/VirtualFolders")
                if isinstance(res, list):
                    vf_items = res
                    for item in res:
                        paths_map[item.get('ItemId')] = item.get('Locations', [])
            except Exception as e:
                if strict:
                    raise
                logger.warning(f"获取 VirtualFolders 失败 (不影响显示，仅影响路径匹配): {e}")

            uid = self._get_user_id()
            if uid:
                try:
                    res = self._request("GET", f"emby/Users/{uid}/Views")
                    items = res.get('Items', []) if isinstance(res, dict) else res

                    final_libs = []
                    for item in items:
                        lib_id = item.get('Id')
                        locations = paths_map.get(lib_id, [])
                        final_libs.append({
                            "name": item.get('Name'),
                            "id": lib_id,
                            "type": item.get('CollectionType', 'unknown'),
                            "paths": locations
                        })

                    return final_libs
                except Exception as e:
                    if strict:
                        raise
                    logger.error(f"获取用户视图排序失败: {e}")

            if strict:
                raise requests.exceptions.HTTPError("Emby library access requires a valid user context")

            if vf_items:
                return [{
                    "name": item['Name'],
                    "id": item['ItemId'],
                    "type": item.get('CollectionType', 'unknown'),
                    "paths": item.get('Locations', [])
                } for item in vf_items]

            return []

    def get_libraries_with_covers(self):
        try:
            libs = self.get_libraries()
            result = []
            for lib in libs:
                i_id = lib['id']
                img_url = f"{self.public_host}/emby/Items/{i_id}/Images/Primary?maxHeight=300&quality=90"
                result.append({"name": lib['name'], "id": i_id, "cover_url": img_url, "type": lib['type']})
            return result
        except: return []

    def get_assets(self, library_id, mode="random", poster_limit=6, backdrop_limit=1):
        bg_url = None
        backdrops = [] 
        posters = []
        count = self.get_library_count(library_id)
        
        uid = self._get_user_id()
        endpoint = f"emby/Users/{uid}/Items" if uid else "emby/Items"

        if backdrop_limit > 0:
            params = {
                "ParentId": library_id, 
                "Recursive": "true", 
                "IncludeItemTypes": "Movie,Series", 
                "ImageTypes": "Backdrop", 
                "EnableImageTypes": "Backdrop,Logo",
                "Fields": "ImageTags,BackdropImageTags", 
                "Limit": max(20, backdrop_limit + 5) 
            }
            if mode == "random": params["SortBy"] = "Random"
            else: 
                params["SortBy"] = "DateCreated,SortName"
                params["SortOrder"] = "Descending"

            try:
                res = self._request("GET", endpoint, params=params)
                items = res.get('Items', [])
                valid_items = []
                for item in items:
                    url = None
                    if 'BackdropImageTags' in item and len(item['BackdropImageTags']) > 0:
                        tag = item['BackdropImageTags'][0]
                        url = f"{self.host}/emby/Items/{item['Id']}/Images/Backdrop/0?MaxHeight=2160&tag={tag}&quality=90"
                    elif 'Backdrop' in item.get('ImageTags', {}):
                        tag = item['ImageTags']['Backdrop']
                        url = f"{self.host}/emby/Items/{item['Id']}/Images/Backdrop/0?MaxHeight=2160&tag={tag}&quality=90"
                    if url: valid_items.append(url)

                if len(valid_items) > 0:
                    if mode == "random" and len(valid_items) > backdrop_limit:
                        backdrops = random.sample(valid_items, backdrop_limit)
                    else:
                        backdrops = valid_items[:backdrop_limit]
                    bg_url = backdrops[0] 
            except Exception as e: 
                logger.error(f"Error fetching backdrops: {e}")

        if poster_limit > 0:
            p_params = {
                "ParentId": library_id, 
                "Recursive": "true", 
                "IncludeItemTypes": "Movie,Series", 
                "ImageTypes": "Primary", 
                "Fields": "ImageTags", 
                "Limit": max(30, poster_limit + 10) 
            }
            if mode == "random": p_params["SortBy"] = "Random"
            else: 
                p_params["SortBy"] = "DateCreated"
                p_params["SortOrder"] = "Descending"
            
            try:
                res = self._request("GET", endpoint, params=p_params)
                p_items = res.get('Items', [])
                valid_posters = []
                for item in p_items:
                    if 'Primary' in item.get('ImageTags', {}):
                        p_tag = item['ImageTags'].get('Primary')
                        valid_posters.append(f"{self.host}/emby/Items/{item['Id']}/Images/Primary?MaxHeight=600&tag={p_tag}")
                if len(valid_posters) > 0:
                    if mode == "random" and len(valid_posters) > poster_limit:
                        posters = random.sample(valid_posters, poster_limit)
                    else:
                        posters = valid_posters[:poster_limit]
            except Exception as e: 
                logger.error(f"Error fetching posters: {e}")

        return { "bg_url": bg_url, "backdrops": backdrops, "posters": posters, "count": count }

    def get_item_images(self, item_id, img_type='Backdrop'):
        images = []
        try:
            uid = self._get_user_id()
            endpoint = f"emby/Users/{uid}/Items/{item_id}" if uid else f"emby/Items/{item_id}"
            
            info = self._request("GET", endpoint)
            
            if img_type == 'Backdrop':
                tags = info.get('BackdropImageTags', [])
                for i, tag in enumerate(tags):
                    url = f"{self.public_host}/emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&quality=90&maxHeight=1080"
                    images.append(url)
            elif img_type == 'Primary':
                if 'Primary' in info.get('ImageTags', {}):
                    tag = info['ImageTags']['Primary']
                    url = f"{self.public_host}/emby/Items/{item_id}/Images/Primary?tag={tag}&quality=90&maxHeight=600"
                    images.append(url)
        except: 
            pass
        return images

    def get_random_pool(self, library_id, img_type='Backdrop', limit=50):
        uid = self._get_user_id()
        params = {
            "ParentId": library_id, 
            "Recursive": "true", 
            "IncludeItemTypes": "Movie,Series",
            "SortBy": "Random", 
            "Limit": limit, 
            "Fields": "ImageTags,BackdropImageTags", 
            "ImageTypes": img_type
        }
        urls = []
        try:
            endpoint = f"emby/Users/{uid}/Items" if uid else "emby/Items"
            res = self._request("GET", endpoint, params=params)
            items = res.get('Items', [])
            
            for item in items:
                i_id = item['Id']
                if img_type == 'Backdrop':
                    if item.get('BackdropImageTags'):
                        tag = item['BackdropImageTags'][0]
                        urls.append(f"{self.public_host}/emby/Items/{i_id}/Images/Backdrop/0?tag={tag}&maxHeight=1080&quality=90")
                else: 
                    if 'Primary' in item.get('ImageTags', {}):
                        tag = item['ImageTags']['Primary']
                        urls.append(f"{self.public_host}/emby/Items/{i_id}/Images/Primary?tag={tag}&maxHeight=600&quality=90")
        except: 
            pass
        return urls
    
    def search_items(self, query, library_id=None, img_type="Primary"):
        uid = self._get_user_id()
        params = {
            "SearchTerm": query, 
            "Recursive": "true", 
            "IncludeItemTypes": "Movie,Series,BoxSet", 
            "Limit": 20,
            "Fields": "ImageTags,BackdropImageTags,PrimaryImageAspectRatio"
        }
        if library_id: 
            params["ParentId"] = library_id
            
        results = []
        try:
            endpoint = f"emby/Users/{uid}/Items" if uid else "emby/Items"
            res = self._request("GET", endpoint, params=params)
            items = res.get('Items', [])
            
            for i in items:
                img_url = None
                if img_type == 'Backdrop' and i.get('BackdropImageTags'):
                    tag = i['BackdropImageTags'][0]
                    img_url = f"{self.public_host}/emby/Items/{i['Id']}/Images/Backdrop/0?tag={tag}&maxHeight=400&maxWidth=711&quality=90"
                elif 'Primary' in i.get('ImageTags', {}):
                    tag = i['ImageTags']['Primary']
                    img_url = f"{self.public_host}/emby/Items/{i['Id']}/Images/Primary?tag={tag}&maxHeight=400&quality=90"
                
                results.append({
                    "name": i.get('Name'), 
                    "id": i.get('Id'), 
                    "type": i.get('Type'), 
                    "year": i.get('ProductionYear'), 
                    "image": img_url 
                })
        except: 
            pass
        return results

    def find_path_by_id(self, tmdb_id, item_type='Movie', exclude_path=None):
        """
        根据 TMDb ID 查找物理路径 (支持版本合并/MediaSources挖掘)
        """
        if not tmdb_id: return None
        
        search_type = 'Series' if item_type == 'Series' else 'Movie'

        params = {
            "Recursive": "true",
            "AnyProviderIdEquals": f"Tmdb.{tmdb_id}",
            "IncludeItemTypes": search_type,
            # 【关键修改 1】增加 MediaSources 字段，防止 Emby 合并版本导致找不到源路径
            "Fields": "Path,MediaSources",
        }
        
        try:
            endpoint = "emby/Items"
            user_id = self._get_user_id()
            if user_id: params['UserId'] = user_id

            res = self._request("GET", endpoint, params=params)
            
            if res.get("Items"):
                for item in res["Items"]:
                    found_type = item.get("Type")
                    if found_type != search_type: continue

                    # --- 内部函数：检查路径是否有效 ---
                    def is_valid_source(p):
                        if not p: return False
                        if not exclude_path: return True
                        # 路径标准化对比
                        p_check = p.replace('\\', '/').rstrip('/')
                        p_exclude = exclude_path.replace('\\', '/').rstrip('/')
                        return not p_check.startswith(p_exclude)

                    # 1. 先检查顶层 Path
                    main_path = item.get("Path")
                    if is_valid_source(main_path):
                        return main_path

                    # 2. 【核心修复】如果顶层 Path 被排除，检查 MediaSources (应对版本合并)
                    if item.get("MediaSources"):
                        for source in item["MediaSources"]:
                            sub_path = source.get("Path")
                            if is_valid_source(sub_path):
                                logger.debug(f"[DeepSearch] 在合并版本中找到源路径: {sub_path}")
                                return sub_path
                
        except Exception as e: 
            logger.error(f"ID寻址失败 ({tmdb_id}): {e}")
        return None

    def get_series_episode_counts(self, tmdb_id: int) -> dict:
        """
        返回 {season_number: {episode_numbers}} 表示 Emby 中该系列拥有的所有集。
        系列不在 Emby 则返回空 dict。
        """
        if not tmdb_id:
            return {}
        try:
            params = {
                "Recursive": "true",
                "AnyProviderIdEquals": f"Tmdb.{tmdb_id}",
                "IncludeItemTypes": "Series",
                "Fields": "Id",
            }
            user_id = self._get_user_id()
            if user_id:
                params["UserId"] = user_id
            res = self._request("GET", "emby/Items", params=params)
            items = res.get("Items", [])
            if not items:
                return {}
            series_id = items[0].get("Id")
            if not series_id:
                return {}
            result = self.get_series_episode_counts_by_id(series_id)
            logger.debug(f"[剧集集数] TMDB:{tmdb_id} Series:{series_id} 结果: { {k: len(v) for k, v in result.items()} }")
            return result
        except Exception as e:
            logger.warning(f"获取剧集集数失败 ({tmdb_id}): {e}")
            return {}

    def get_series_episode_counts_by_id(self, series_id: str) -> dict:
        if not series_id:
            return {}
        user_id = self._get_user_id()
        endpoint = f"emby/Users/{user_id}/Items" if user_id else "emby/Items"
        try:
            result = {}
            start = 0
            limit = DISCOVER_EPISODE_PAGE_LIMIT
            while True:
                params = {
                    "ParentId": series_id,
                    "Recursive": "true",
                    "IncludeItemTypes": "Episode",
                    "Fields": "ParentIndexNumber,IndexNumber",
                    "StartIndex": start,
                    "Limit": limit,
                }
                data = self._request("GET", endpoint, params=params)
                items = data.get("Items", []) if isinstance(data, dict) else []
                if not items:
                    break
                for item in items:
                    season_num = item.get("ParentIndexNumber")
                    ep_num = item.get("IndexNumber")
                    if season_num is None or ep_num is None:
                        continue
                    try:
                        result.setdefault(int(season_num), set()).add(int(ep_num))
                    except Exception:
                        continue
                total = data.get("TotalRecordCount", 0) if isinstance(data, dict) else 0
                start += len(items)
                if not total or start >= total:
                    break
            return result
        except Exception as e:
            logger.warning(f"获取剧集集数失败 (Series:{series_id}): {e}")
            return {}

    def get_series_episode_counts_by_library(self, library_id: str) -> dict:
        """
        批量返回指定媒体库内所有剧集的集数索引：
        {series_id: {season_number: {episode_numbers}}}
        """
        if not library_id:
            return {}
        user_id = self._get_user_id()
        endpoint = f"emby/Users/{user_id}/Items" if user_id else "emby/Items"
        try:
            result = {}
            start = 0
            limit = DISCOVER_EPISODE_PAGE_LIMIT
            while True:
                params = {
                    "ParentId": library_id,
                    "Recursive": "true",
                    "IncludeItemTypes": "Episode",
                    "Fields": "SeriesId,ParentIndexNumber,IndexNumber",
                    "StartIndex": start,
                    "Limit": limit,
                }
                data = self._request("GET", endpoint, params=params)
                items = data.get("Items", []) if isinstance(data, dict) else []
                if not items:
                    break
                for item in items:
                    series_id = str(item.get("SeriesId") or "").strip()
                    season_num = item.get("ParentIndexNumber")
                    ep_num = item.get("IndexNumber")
                    if not series_id or season_num is None or ep_num is None:
                        continue
                    try:
                        result.setdefault(series_id, {}).setdefault(int(season_num), set()).add(int(ep_num))
                    except Exception:
                        continue
                total = data.get("TotalRecordCount", 0) if isinstance(data, dict) else 0
                start += len(items)
                if not total or start >= total:
                    break
            return result
        except Exception as e:
            logger.warning(f"批量获取媒体库剧集集数失败 (Library:{library_id}): {e}")
            return {}

    def ensure_library_exists(self, name, path, collection_type="movies", enable_scrapers=False, refresh_on_path_add=True):
        """
        自动建库逻辑 - 返回 (lib_id, is_new)。is_new=True 表示新建的库。
        """
        existing_libs = self.get_libraries()
        target_lib = next((lib for lib in existing_libs if lib['name'] == name), None)

        if target_lib:
            try:
                res = self._request("GET", "emby/Library/VirtualFolders")
                remote_lib = next((l for l in res if l['Name'] == name), None)
                if remote_lib:
                    locations = remote_lib.get('Locations', [])
                    norm_path = path.rstrip('/\\')
                    norm_locations = [l.rstrip('/\\') for l in locations]
                    if norm_path in norm_locations:
                        logger.info(f"媒体库 '{name}' 已包含路径 '{path}'，跳过添加")
                        return target_lib['id'], False
            except Exception as e:
                logger.warning(f"检查库路径失败，将尝试直接添加: {e}")

            try:
                params = { "Name": name, "Path": path, "Refresh": "true" if refresh_on_path_add else "false" }
                self._request("POST", "emby/Library/VirtualFolders/Paths", params=params)
            except: pass
            return target_lib['id'], False

        logger.info(f"正在创建新媒体库: {name} -> {path}")
        
        if enable_scrapers:
            lib_options = {
                **DEFAULT_LIBRARY_LOCALE_OPTIONS,
                "EnableArchiveMediaFiles": False,
                "EnablePhotos": False,
                "EnableRealtimeMonitor": True,
                "EnableMarkerDetection": True,
                "ExtractChapterImagesDuringLibraryScan": False,
                "DownloadImagesInAdvance": False,
                "SaveLocalMetadata": True,
                "MetadataSavers": ["Nfo"],
                "LocalMetadataReaderOrder": ["Nfo"],
                "EnableInternetProviders": True,
                "DisabledSubtitleFetchers": ["Open Subtitles"],
                "TypeOptions": [
                    {"Type": "Movie", "MetadataFetchers": ["TheMovieDb"], "ImageFetchers": ["TheMovieDb"], "ImageOptions": []},
                    {"Type": "Series", "MetadataFetchers": ["TheMovieDb"], "ImageFetchers": ["TheMovieDb"], "ImageOptions": []},
                    {"Type": "Season", "MetadataFetchers": ["TheMovieDb"], "ImageFetchers": ["TheMovieDb"]},
                    {"Type": "Episode", "MetadataFetchers": ["TheMovieDb"], "ImageFetchers": ["TheMovieDb", "Image Capture"], "ImageOptions": []},
                ]
            }
        else:
            lib_options = {
                **DEFAULT_LIBRARY_LOCALE_OPTIONS,
                "EnableArchiveMediaFiles": False,
                "EnablePhotos": False,
                "EnableRealtimeMonitor": True,
                "ExtractChapterImagesDuringLibraryScan": False,
                "DownloadImagesInAdvance": False,
                "SaveLocalMetadata": False,
                "EnableInternetProviders": False,
                "DisabledSubtitleFetchers": ["Open Subtitles"],
                "TypeOptions": [
                    {"Type": "Movie", "MetadataFetchers": [], "ImageFetchers": [], "ImageOptions": []},
                    {"Type": "Series", "MetadataFetchers": [], "ImageFetchers": [], "ImageOptions": []},
                    {"Type": "Season", "MetadataFetchers": [], "ImageFetchers": []},
                    {"Type": "Episode", "MetadataFetchers": [], "ImageFetchers": []}
                ]
            }

        try:
            params = {
                "Name": name,
                "CollectionType": collection_type,
                "RefreshKey": f"AutoCreated_{int(time.time())}"
            }
            
            body = {
                "LibraryOptions": lib_options
            }
            body["LibraryOptions"]["PathInfos"] = [{"Path": path}]

            self._request("POST", "emby/Library/VirtualFolders", params=params, json=body)
            logger.info(f"媒体库创建成功: {name}")

            time.sleep(2)
            new_libs = self.get_libraries()
            new_target = next((lib for lib in new_libs if lib['name'] == name), None)
            if new_target: return new_target['id'], True
        except Exception as e:
            logger.error(f"创建媒体库失败: {e}")
        return None, False

    def fix_library_locale_defaults(self, overwrite=False):
        """
        一次性修复已有媒体库的语言/地区设置。
        overwrite=False 时只填充空白值，避免覆盖用户手动设置。
        """
        results = []
        updated_count = 0
        skipped_count = 0
        failed_count = 0

        try:
            data = self._request("GET", "emby/Library/VirtualFolders/Query")
        except Exception:
            data = self._request("GET", "emby/Library/VirtualFolders")

        if isinstance(data, dict):
            folders = data.get("Items") or []
        else:
            folders = data
        if not isinstance(folders, list):
            return {
                "updated": 0,
                "skipped": 0,
                "failed": 0,
                "items": results,
            }

        for folder in folders:
            if not isinstance(folder, dict):
                continue

            lib_id = str(folder.get("ItemId") or folder.get("Id") or "").strip()
            lib_name = str(folder.get("Name") or "").strip() or lib_id
            options = folder.get("LibraryOptions")

            if not lib_id or not isinstance(options, dict):
                skipped_count += 1
                results.append({
                    "name": lib_name,
                    "id": lib_id,
                    "status": "skipped",
                    "reason": "missing_library_options",
                })
                continue

            next_options = deepcopy(options)
            changed_fields = []
            for key, value in DEFAULT_LIBRARY_LOCALE_OPTIONS.items():
                current = next_options.get(key)
                if overwrite or current is None or str(current).strip() == "":
                    if current != value:
                        next_options[key] = value
                        changed_fields.append(key)

            if not changed_fields:
                skipped_count += 1
                results.append({
                    "name": lib_name,
                    "id": lib_id,
                    "status": "skipped",
                    "reason": "already_set",
                })
                continue

            try:
                self._request(
                    "POST",
                    "emby/Library/VirtualFolders/LibraryOptions",
                    json={
                        "Id": lib_id,
                        "LibraryOptions": next_options,
                    },
                )
                updated_count += 1
                results.append({
                    "name": lib_name,
                    "id": lib_id,
                    "status": "updated",
                    "changed_fields": changed_fields,
                })
                logger.info(f"已修复媒体库语言/地区设置: {lib_name} ({lib_id})")
            except Exception as e:
                failed_count += 1
                results.append({
                    "name": lib_name,
                    "id": lib_id,
                    "status": "failed",
                    "message": str(e),
                    "changed_fields": changed_fields,
                })
                logger.warning(f"修复媒体库语言/地区设置失败: {lib_name} ({lib_id}) -> {e}")

        return {
            "updated": updated_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "items": results,
        }
    
    def delete_library(self, library_id):
        """
        [强制修改] 按照指令：直接发送库 ID 来删除
        请求 URL 将变为：DELETE /emby/Library/VirtualFolders?id=xxxx&Refresh=false
        """
        if not library_id: return False
        
        try:
            logger.info(f"正在通过 ID 删除媒体库: {library_id}")
            
            # ★★★ 严格按照你的要求修改 ★★★
            # 1. 参数改为 id (而不是 Name)
            # 2. Refresh=false 用于解决 500 超时报错
            params = { 
                "id": library_id, 
                "Refresh": "false" 
            }
            
            # 直接向 VirtualFolders 接口发送带 id 的 DELETE 请求
            self._request("DELETE", "emby/Library/VirtualFolders", params=params)
            
            logger.info(f"媒体库删除指令已发送 (ID: {library_id})")
            return True

        except Exception as e:
            logger.error(f"通过 ID 删除请求遇到错误: {e}")
            
            # 为了确保一定能删掉，如果上面的报错，尝试通用的 Item 删除接口 (这也是一种通过 ID 删库的方式)
            try:
                logger.info("尝试使用 Item 接口强制删除...")
                self._request("DELETE", f"emby/Items/{library_id}")
                return True
            except:
                return False

    def delete_library_by_path(self, path):
            """
            [纯路径模式] 仅根据物理路径查找并删除媒体库
            :param path: 库在硬盘上的绝对路径
            """
            if not path:
                logger.warning("未提供路径，无法执行删除操作")
                return False

            # 1. 路径标准化函数 (处理斜杠和大小写，确保匹配准确)
            def normalize(p):
                return p.replace('\\', '/').rstrip('/').lower()

            target_path = normalize(path)
            all_libs = self.get_libraries()
            target_lib = None

            # 2. 遍历查找匹配路径的库
            for lib in all_libs:
                # lib['paths'] 来自 get_libraries 中的 Locations 字段
                if 'paths' in lib and lib['paths']:
                    for loc in lib['paths']:
                        if normalize(loc) == target_path:
                            target_lib = lib
                            logger.info(f"路径匹配成功: '{path}' -> 媒体库 '{lib['name']}'")
                            break
                if target_lib:
                    break
            
            if not target_lib:
                logger.warning(f"Emby 中未找到路径为 '{path}' 的媒体库，跳过删除。")
                return True # 找不到视为已清理，返回 True

            # 3. 使用找到的真实名称执行删除
            real_name = target_lib['name']
            try:
                logger.info(f"正在请求删除 Emby 媒体库: {real_name}")
                # Emby 删除接口主要依赖 Name 参数
                params = {
                    "Name": real_name, 
                    "Refresh": "true"
                }
                self._request("DELETE", "emby/Library/VirtualFolders", params=params)
                return True
            except Exception as e:
                logger.error(f"删除媒体库失败: {real_name} | {e}")
                return False

    def get_library_count(self, library_id):
        uid = self._get_user_id()
        try:
            params = { "ParentId": library_id, "Recursive": "true", "IncludeItemTypes": "Movie,Series", "Limit": 0 }
            endpoint = f"emby/Users/{uid}/Items" if uid else "emby/Items"
            res = self._request("GET", endpoint, params=params)
            return res.get('TotalRecordCount', 0)
        except: return 0

    def get_all_library_items(self, item_types="Movie,Series", library_id=None, library_name=""):
        """全量扫描媒体库，返回 { "tmdb_id:type": True } dict"""
        uid = self._get_user_id()
        endpoint = f"emby/Users/{uid}/Items" if uid else "emby/Items"
        results = {}
        start = 0
        limit = DISCOVER_SCAN_ITEM_PAGE_LIMIT
        while True:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": item_types,
                "Fields": "ProviderIds,Name,OriginalTitle,ProductionYear",
                "StartIndex": start,
                "Limit": limit,
            }
            if library_id:
                params["ParentId"] = library_id
            try:
                data = self._request("GET", endpoint, params=params)
            except Exception as e:
                logger.error(f"扫描媒体库失败 (StartIndex={start}): {e}")
                break
            items = data.get("Items", [])
            if not items:
                break
            for item in items:
                provider_ids = item.get("ProviderIds", {})
                tmdb_id = provider_ids.get("Tmdb")
                if not tmdb_id:
                    continue
                emby_type = item.get("Type", "")
                media_type = "tv" if emby_type == "Series" else "movie"
                result_key = f"{tmdb_id}:{media_type}"
                if library_id:
                    result_key = f"{result_key}:{library_id}"
                results[result_key] = {
                    "emby_id": item.get("Id", "") or "",
                    "tmdb_id": str(tmdb_id),
                    "media_type": media_type,
                    "title": item.get("Name", "") or "",
                    "original_title": item.get("OriginalTitle", "") or "",
                    "year": str(item.get("ProductionYear", "") or ""),
                    "library_id": str(library_id or ""),
                    "library_name": str(library_name or ""),
                }
            total = data.get("TotalRecordCount", 0)
            start += len(items)
            if start >= total:
                break
        return results

    def get_item_parent_library(self, item_id):
        """
        [优化版] 使用 Ancestors 接口一次性获取层级链，查找父级库
        API: /Items/{Id}/Ancestors
        """
        uid = self._get_user_id()
        # 构造请求 URL
        endpoint = f"emby/Users/{uid}/Items/{item_id}/Ancestors" if uid else f"emby/Items/{item_id}/Ancestors"
        
        try:
            # 发送一次请求，获取所有祖先节点 (列表通常是从下往上，或者从上往下，Emby通常返回完整列表)
            ancestors = self._request("GET", endpoint)
            
            # 遍历祖先节点，寻找类型为媒体库的节点
            # 常见的库类型：CollectionFolder, UserView, BoxSet (合集模式下)
            target_types = ['CollectionFolder', 'UserView', 'BoxSet']
            
            for item in ancestors:
                if item.get('Type') in target_types:
                    # 找到了，直接返回
                    return {"name": item.get('Name'), "id": item.get('Id')}
            
            return None
            
        except Exception as e:
            logger.error(f"反查父级库失败 (ID: {item_id}): {e}")
            return None

    def refresh_library(self, library_id=None):
        try:
            if library_id:
                lib_name = ""
                try:
                    libs = self.get_libraries()
                    hit = next((l for l in libs if str(l.get('id')) == str(library_id)), None)
                    if hit:
                        lib_name = hit.get('name', '')
                except Exception:
                    pass

                display_name = lib_name or library_id
                logger.info(f"刷新媒体库: {display_name}")
                endpoint = f"emby/Items/{library_id}/Refresh?Recursive=true&ImageRefreshMode=Default&MetadataRefreshMode=Default&ReplaceAllImages=false&ReplaceAllMetadata=false"
            else:
                logger.info("刷新所有媒体库")
                endpoint = "emby/Library/Refresh"
            self._request("POST", endpoint)
            return True
        except: return False

    def notify_media_updated(self, path, update_type="Created"):
        path = str(path or "").strip()
        if not path:
            return False
        return self.notify_media_updates([path], update_type=update_type)

    def notify_media_updates(self, paths, update_type="Created"):
        updates = []
        seen = set()
        for path in paths or []:
            path = str(path or "").strip()
            if not path:
                continue
            key = path.replace("\\", "/").rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            updates.append({"Path": path, "UpdateType": update_type or "Created"})

        if not updates:
            return False
        try:
            self._request(
                "POST",
                "emby/Library/Media/Updated",
                json={"Updates": updates},
            )
            return True
        except Exception as e:
            logger.warning(f"通知 Emby 路径更新失败: {len(updates)} 个路径 | {e}")
            return False

    def _parse_emby_datetime(self, value):
        if not value:
            return 0.0
        text = str(value).strip()
        if not text:
            return 0.0
        normalized = text.replace('Z', '+00:00')
        try:
            return datetime.fromisoformat(normalized).timestamp()
        except Exception:
            try:
                return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
            except Exception:
                return 0.0

    def _normalize_year_value(self, value):
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        try:
            num = int(float(text))
            return "" if num <= 1 else str(num)
        except Exception:
            return text

    def _build_episode_label(self, season_map):
        from app.dependencies import format_episode_range

        parts = []
        episode_groups = []
        for season in sorted(season_map.keys()):
            episodes = sorted(set(season_map.get(season) or []))
            if not episodes:
                continue
            range_text = format_episode_range(episodes)
            if not range_text:
                continue
            parts.append(f"S{int(season):02d}{range_text}")
            episode_groups.append({"season": int(season), "episodes": episodes})
        return ",".join(parts), episode_groups

    def get_item_info(self, item_id):
        """
        获取媒体详情（用于通知）

        Returns:
            dict: {
                "name": 媒体名称,
                "type": 类型 (Movie/Series/Episode),
                "year": 年份,
                "series_name": 剧集名称 (仅Episode),
                "season": 季数 (仅Episode),
                "episode": 集数 (仅Episode),
                "poster_url": 海报图片URL,
                "overview": 简介
            }
        """
        uid = self._get_user_id()
        endpoint = f"emby/Users/{uid}/Items/{item_id}" if uid else f"emby/Items/{item_id}"

        try:
            params = {
                "fields": "Overview,CommunityRating,Genres,Tagline,ProductionYear,SeriesName,ParentIndexNumber,IndexNumber,SeriesId,ImageTags,ProviderIds,OriginalTitle"
            }
            info = self._request("GET", endpoint, params=params)

            result = {
                "name": info.get("Name", "未知媒体"),
                "type": info.get("Type", ""),
                "year": info.get("ProductionYear", ""),
                "overview": info.get("Overview", ""),
                "poster_url": None,
                "backdrop_url": None,
                "genres": ", ".join(info.get("Genres", [])) if info.get("Genres") else "",
                "community_rating": info.get("CommunityRating"),
                "tagline": info.get("Tagline", ""),
                "original_title": info.get("OriginalTitle", ""),
                "tmdb_id": str(info.get("ProviderIds", {}).get("Tmdb", "")) if info.get("ProviderIds", {}).get("Tmdb") else "",
                "status": info.get("Status", ""),
            }

            # 获取海报图片
            if "Primary" in info.get("ImageTags", {}):
                tag = info["ImageTags"]["Primary"]
                result["poster_url"] = f"{self.public_host}/emby/Items/{item_id}/Images/Primary?tag={tag}&quality=90&maxHeight=500"
            backdrop_tags = info.get("BackdropImageTags") or []
            if backdrop_tags:
                result["backdrop_url"] = f"{self.public_host}/emby/Items/{item_id}/Images/Backdrop/0?tag={backdrop_tags[0]}&quality=90&maxWidth=1280"
            elif "Backdrop" in info.get("ImageTags", {}):
                tag = info["ImageTags"]["Backdrop"]
                result["backdrop_url"] = f"{self.public_host}/emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&quality=90&maxWidth=1280"

            # 剧集特殊处理
            if info.get("Type") == "Episode":
                result["series_name"] = info.get("SeriesName", "")
                result["season"] = info.get("ParentIndexNumber", "?")
                result["episode"] = info.get("IndexNumber", "?")
                # 获取剧集海报
                series_id = info.get("SeriesId")
                if series_id and not result["poster_url"]:
                    result["poster_url"] = f"{self.public_host}/emby/Items/{series_id}/Images/Primary?quality=90&maxHeight=500"

                # ★ 获取剧集(Series)级别的评分和类型，而非单集的
                series_id_for_meta = info.get("SeriesId")
                if series_id_for_meta:
                    try:
                        series_endpoint = f"emby/Users/{uid}/Items/{series_id_for_meta}" if uid else f"emby/Items/{series_id_for_meta}"
                        series_info = self._request("GET", series_endpoint, params=params)
                        if series_info:
                            series_rating = series_info.get("CommunityRating")
                            if series_rating:
                                result["community_rating"] = series_rating
                            series_genres = series_info.get("Genres", [])
                            if series_genres:
                                result["genres"] = ", ".join(series_genres)
                            series_overview = series_info.get("Overview", "")
                            if series_overview:
                                result["overview"] = series_overview
                            result["series_year"] = series_info.get("ProductionYear", "")
                    except Exception as e:
                        logger.debug(f"获取剧集元数据失败: {e}")

            return result

        except Exception as e:
            logger.error(f"获取媒体详情失败 (ID: {item_id}): {e}")
            return None

    def get_recently_added_items(self, limit: int | None = None) -> list:
        uid = self._get_user_id()
        endpoint = f"emby/Users/{uid}/Items" if uid else "emby/Items"
        try:
            requested_limit = max(1, int(limit)) if limit is not None else None
        except Exception:
            requested_limit = 20
        base_limit = requested_limit or 20
        page_limit = min(max(base_limit * 20, 200), 1000)
        max_scan = page_limit if requested_limit is None else max(page_limit, min(base_limit * 200, 10000))
        base_params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Fields": "Overview,CommunityRating,Genres,ProductionYear,ImageTags,BackdropImageTags,DateCreated,SeriesId,SeriesName,ParentIndexNumber,IndexNumber"
        }
        try:
            result = []
            series_cache = {}
            current_series_entry = None
            start_index = 0

            def flush_current_series():
                nonlocal current_series_entry
                if not current_series_entry:
                    return
                entry = current_series_entry
                episode_label, episode_groups = self._build_episode_label(entry.pop("_season_map", {}))
                entry["episode_label"] = episode_label
                entry["episode_groups"] = episode_groups
                result.append(entry)
                current_series_entry = None

            while start_index < max_scan:
                batch_limit = min(page_limit, max_scan - start_index)
                params = dict(base_params)
                params["Limit"] = batch_limit
                params["StartIndex"] = start_index
                data = self._request("GET", endpoint, params=params)
                items = data.get("Items", []) if isinstance(data, dict) else []
                if not items:
                    break

                for item in items:
                    item_type = item.get("Type", "")
                    date_created = item.get("DateCreated", "")

                    if item_type == "Movie":
                        flush_current_series()
                        image_tags = item.get("ImageTags", {})
                        backdrop_tags = item.get("BackdropImageTags") or []
                        poster_url = None
                        backdrop_url = None
                        if "Primary" in image_tags:
                            poster_url = f"{self.public_host}/emby/Items/{item['Id']}/Images/Primary?tag={image_tags['Primary']}&quality=90&maxHeight=500"
                        if backdrop_tags:
                            backdrop_url = f"{self.public_host}/emby/Items/{item['Id']}/Images/Backdrop/0?tag={backdrop_tags[0]}&quality=90&maxWidth=1280"
                        elif "Backdrop" in image_tags:
                            backdrop_url = f"{self.public_host}/emby/Items/{item['Id']}/Images/Backdrop/0?tag={image_tags['Backdrop']}&quality=90&maxWidth=1280"
                        result.append({
                            "id": item.get("Id"),
                            "card_id": f"movie:{item.get('Id')}",
                            "title": item.get("Name", "未知媒体"),
                            "year": self._normalize_year_value(item.get("ProductionYear", "")),
                            "type": "Movie",
                            "media_type": "movie",
                            "poster_url": poster_url,
                            "backdrop_url": backdrop_url,
                            "overview": item.get("Overview", "") or "",
                            "rating": item.get("CommunityRating"),
                            "genres": ", ".join(item.get("Genres", [])) if item.get("Genres") else "",
                            "date_created": date_created
                        })
                    elif item_type == "Episode":
                        series_id = item.get("SeriesId")
                        season = item.get("ParentIndexNumber")
                        episode = item.get("IndexNumber")
                        if not series_id:
                            continue

                        if current_series_entry and current_series_entry.get("id") != series_id:
                            flush_current_series()

                        if current_series_entry is None:
                            if series_id not in series_cache:
                                series_cache[series_id] = self.get_item_info(series_id) or {}
                            series_info = series_cache.get(series_id) or {}
                            current_series_entry = {
                                "id": series_id,
                                "card_id": f"series:{series_id}:{date_created}:{start_index + len(result)}",
                                "title": series_info.get("name") or item.get("SeriesName") or item.get("Name", "未知媒体"),
                                "year": self._normalize_year_value(series_info.get("year", "")),
                                "type": "Series",
                                "media_type": "tv",
                                "poster_url": series_info.get("poster_url"),
                                "backdrop_url": series_info.get("backdrop_url"),
                                "overview": series_info.get("overview", "") or "",
                                "rating": series_info.get("community_rating"),
                                "genres": series_info.get("genres", ""),
                                "date_created": date_created,
                                "_season_map": {}
                            }

                        if season is not None and episode is not None:
                            try:
                                season_num = int(season)
                                episode_num = int(episode)
                                current_series_entry["_season_map"].setdefault(season_num, []).append(episode_num)
                            except Exception:
                                pass

                    if requested_limit:
                        projected_count = len(result) + (1 if current_series_entry else 0)
                        if projected_count >= requested_limit:
                            break

                start_index += len(items)
                if requested_limit:
                    projected_count = len(result) + (1 if current_series_entry else 0)
                    if projected_count >= requested_limit:
                        break
                try:
                    total_record_count = int(data.get("TotalRecordCount", 0)) if isinstance(data, dict) else 0
                except Exception:
                    total_record_count = 0
                if total_record_count and start_index >= total_record_count:
                    break
                if len(items) < batch_limit:
                    break

            flush_current_series()
            result.sort(key=lambda x: self._parse_emby_datetime(x.get("date_created")), reverse=True)
            return result[:requested_limit] if requested_limit else result
        except Exception as e:
            logger.debug(f"[EmbyClient] 查询最近入库失败: {e}")
            return []

    def get_recent_playbacks(self, limit: int | None = None) -> list:
        uid = self._get_user_id()
        if uid:
            try:
                resume_params = {
                    "IncludeItemTypes": "Movie,Episode",
                }
                if limit:
                    resume_params["Limit"] = limit
                data = self._request(
                    "GET",
                    f"emby/Users/{uid}/Items/Resume",
                    params=resume_params,
                )
                items = data.get("Items", []) if isinstance(data, dict) else []
                result = []
                for item in items:
                    item_id = item.get("Id")
                    if not item_id:
                        continue
                    item_info = self.get_item_info(item_id) or {}
                    item_type = item_info.get("type") or item.get("Type", "")
                    media_type = "tv" if item_type in ("Series", "Episode") else "movie"
                    season = item_info.get("season") if item_type == "Episode" else None
                    episode = item_info.get("episode") if item_type == "Episode" else None
                    display_title = item_info.get("name") or item.get("Name", "未知媒体")
                    if item_type == "Episode":
                        series_name = item_info.get("series_name") or item.get("SeriesName") or display_title
                        display_title = series_name
                    backdrop_url = item_info.get("backdrop_url")
                    poster_url = item_info.get("poster_url")
                    if not backdrop_url:
                        backdrop_tags = item.get("BackdropImageTags") or []
                        image_tags = item.get("ImageTags", {})
                        if backdrop_tags:
                            backdrop_url = f"{self.public_host}/emby/Items/{item_id}/Images/Backdrop/0?tag={backdrop_tags[0]}&quality=90&maxWidth=1280"
                        elif "Backdrop" in image_tags:
                            backdrop_url = f"{self.public_host}/emby/Items/{item_id}/Images/Backdrop/0?tag={image_tags['Backdrop']}&quality=90&maxWidth=1280"
                    result.append({
                        "id": item_id,
                        "title": display_title,
                        "year": item_info.get("series_year") if item_type == "Episode" else item_info.get("year", item.get("ProductionYear", "")),
                        "type": item_type,
                        "media_type": media_type,
                        "backdrop_url": backdrop_url or poster_url,
                        "poster_url": poster_url,
                        "overview": item_info.get("overview", item.get("Overview", "")) or "",
                        "rating": item_info.get("community_rating", item.get("CommunityRating")),
                        "genres": item_info.get("genres", ", ".join(item.get("Genres", [])) if item.get("Genres") else ""),
                        "played_at": item.get("DatePlayed") or item.get("UserData", {}).get("LastPlayedDate", ""),
                        "series_name": item_info.get("series_name") or item.get("SeriesName", ""),
                        "season": season,
                        "episode": episode,
                        "progress_percent": round(float(item.get("UserData", {}).get("PlayedPercentage") or 0), 1),
                    })
                if result:
                    return result[:limit] if limit else result
            except Exception as e:
                logger.debug(f"[EmbyClient] 通过继续观看查询最近播放失败: {e}")

        endpoint = f"emby/Users/{uid}/Items" if uid else "emby/Items"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "Filters": "IsPlayed",
            "Fields": "UserData,ProductionYear,Overview,CommunityRating,Genres,ImageTags,BackdropImageTags,SeriesName,ParentIndexNumber,IndexNumber,SeriesId,DatePlayed,ProviderIds,OriginalTitle"
        }
        if limit:
            params["Limit"] = max(limit * 3, 12)
        try:
            data = self._request("GET", endpoint, params=params)
            items = data.get("Items", []) if isinstance(data, dict) else []
            result = []
            for item in items:
                item_info = self.get_item_info(item.get("Id")) or {}
                item_type = item_info.get("type") or item.get("Type", "")
                media_type = "tv" if item_type in ("Series", "Episode") else "movie"
                season = item_info.get("season") if item_type == "Episode" else None
                episode = item_info.get("episode") if item_type == "Episode" else None
                display_title = item_info.get("name") or item.get("Name", "未知媒体")
                if item_type == "Episode":
                    series_name = item_info.get("series_name") or item.get("SeriesName") or display_title
                    display_title = series_name
                backdrop_url = item_info.get("backdrop_url")
                poster_url = item_info.get("poster_url")
                if not backdrop_url:
                    backdrop_tags = item.get("BackdropImageTags") or []
                    image_tags = item.get("ImageTags", {})
                    if backdrop_tags:
                        backdrop_url = f"{self.public_host}/emby/Items/{item['Id']}/Images/Backdrop/0?tag={backdrop_tags[0]}&quality=90&maxWidth=1280"
                    elif "Backdrop" in image_tags:
                        backdrop_url = f"{self.public_host}/emby/Items/{item['Id']}/Images/Backdrop/0?tag={image_tags['Backdrop']}&quality=90&maxWidth=1280"
                result.append({
                    "id": item.get("Id"),
                    "title": display_title,
                    "year": item_info.get("series_year") if item_type == "Episode" else item_info.get("year", item.get("ProductionYear", "")),
                    "type": item_type,
                    "media_type": media_type,
                    "backdrop_url": backdrop_url or poster_url,
                    "poster_url": poster_url,
                    "overview": item_info.get("overview", item.get("Overview", "")) or "",
                    "rating": item_info.get("community_rating", item.get("CommunityRating")),
                    "genres": item_info.get("genres", ", ".join(item.get("Genres", [])) if item.get("Genres") else ""),
                    "played_at": item.get("DatePlayed") or item.get("UserData", {}).get("LastPlayedDate", ""),
                    "series_name": item_info.get("series_name") or item.get("SeriesName", ""),
                    "season": season,
                    "episode": episode,
                    "progress_percent": round(float(item.get("UserData", {}).get("PlayedPercentage") or 0), 1),
                })
            result.sort(key=lambda x: self._parse_emby_datetime(x.get("played_at")), reverse=True)
            return result[:limit] if limit else result
        except Exception as e:
            logger.debug(f"[EmbyClient] 查询最近播放失败: {e}")
            return []

    def _count_items_by_types(self, item_types: str) -> int:
        try:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": item_types,
                "Limit": 0,
            }
            user_id = self._get_user_id()
            if user_id:
                params["UserId"] = user_id
            data = self._request("GET", "emby/Items", params=params)
            return int(data.get("TotalRecordCount", 0)) if isinstance(data, dict) else 0
        except Exception as e:
            logger.debug(f"[EmbyClient] 统计媒体数量失败 ({item_types}): {e}")
            return 0

    def _get_user_count(self) -> int:
        try:
            data = self._request("GET", "emby/Users")
            return len(data) if isinstance(data, list) else 0
        except Exception as e:
            logger.debug(f"[EmbyClient] 查询用户数量失败: {e}")
            return 0

    def get_dashboard_media_stats(self) -> dict:
        try:
            libraries = self.get_libraries()
            result = {
                "total": 0,
                "movie_count": self._count_items_by_types("Movie"),
                "series_count": self._count_items_by_types("Series"),
                "episode_count": self._count_items_by_types("Episode"),
                "user_count": self._get_user_count(),
                "movie_libraries": 0,
                "series_libraries": 0,
                "other_libraries": 0,
                "libraries": []
            }
            for lib in libraries:
                lib_type = (lib.get("type") or "unknown").lower()
                count = self.get_library_count(lib.get("id"))
                result["total"] += count
                if lib_type in ("movies", "movie"):
                    result["movie_libraries"] += 1
                elif lib_type in ("tvshows", "tvshow", "series", "tv"):
                    result["series_libraries"] += 1
                else:
                    result["other_libraries"] += 1
                result["libraries"].append({
                    "id": lib.get("id"),
                    "name": lib.get("name"),
                    "type": lib.get("type", "unknown"),
                    "count": count
                })
            result["libraries"].sort(key=lambda item: item.get("count", 0), reverse=True)
            return result
        except Exception as e:
            logger.debug(f"[EmbyClient] 查询首页媒体统计失败: {e}")
            return {
                "total": 0,
                "movie_count": 0,
                "series_count": 0,
                "episode_count": 0,
                "user_count": 0,
                "movie_libraries": 0,
                "series_libraries": 0,
                "other_libraries": 0,
                "libraries": []
            }

    def get_recently_added_episodes(self, series_id: str, within_seconds: int = 120) -> list:
        """
        查询某剧集最近入库的集数列表，用于"按剧集分组"通知场景。

        Args:
            series_id: 剧集(Series)的 Emby Item ID
            within_seconds: 只返回最近 N 秒内入库的集（默认 120 秒）

        Returns:
            list of dict: [{"season": int, "episode": int}, ...]，按 season/episode 排序
        """
        uid = self._get_user_id()
        endpoint = f"emby/Users/{uid}/Items" if uid else "emby/Items"
        params = {
            "ParentId": series_id,
            "Recursive": "true",
            "IncludeItemTypes": "Episode",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Limit": 100,
            "fields": "ParentIndexNumber,IndexNumber,DateCreated",
        }
        try:
            import datetime
            data = self._request("GET", endpoint, params=params)
            items = data.get("Items", []) if isinstance(data, dict) else []
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=within_seconds)
            result = []
            for item in items:
                created_str = item.get("DateCreated", "")
                try:
                    # Emby 返回格式：2024-01-15T10:30:00.0000000Z
                    created_dt = datetime.datetime.strptime(created_str[:19], "%Y-%m-%dT%H:%M:%S")
                    if created_dt < cutoff:
                        break  # 已按时间降序，可提前退出
                except Exception:
                    pass
                season_num = item.get("ParentIndexNumber")
                ep_num = item.get("IndexNumber")
                if season_num is not None and ep_num is not None:
                    result.append({"season": int(season_num), "episode": int(ep_num)})
            return result
        except Exception as e:
            logger.debug(f"[EmbyClient] 查询最近入库集数失败: {e}")
            return []
