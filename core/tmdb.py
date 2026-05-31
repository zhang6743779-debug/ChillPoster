# handler/tmdb.py

import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import time
import concurrent.futures
import unicodedata
from typing import Optional, List, Dict, Any, Callable
import logging
import config_manager
import constants
import threading
logger = logging.getLogger(__name__)


def contains_chinese(text):
    if not text:
        return False
    return any('\u4e00' <= char <= '\u9fff' for char in str(text))


def normalize_name_for_matching(name):
    if not name:
        return ""
    nfkd_form = unicodedata.normalize('NFKD', str(name))
    ascii_name = "".join(char for char in nfkd_form if not unicodedata.combining(char))
    return ''.join(filter(str.isalnum, ascii_name.lower()))

# ★★★ 自定义的重试类，用于输出更友好的日志 ★★★
class LoggedRetry(Retry):
    """
    一个继承自 urllib3.Retry 的自定义类，
    用于在每次重试时记录一条更清晰、更友好的日志消息。
    """
    def increment(self, method, url, response=None, error=None, _pool=None, _stacktrace=None):
        # 首先，调用父类的 increment 方法。
        # 如果不应该重试了（例如，达到最大次数），它会抛出异常，
        # 这样我们的日志代码就不会执行。
        new_retry = super().increment(method, url, response, error, _pool, _stacktrace)

        # 如果代码能执行到这里，说明即将进行一次重试。
        
        # 确定失败原因
        if response:
            reason = f"不成功的状态码: {response.status}"
        elif error:
            reason = f"连接错误: {error.__class__.__name__}"
        else:
            reason = "未知错误"

        # 获取下一次重试的等待时间
        backoff_time = self.get_backoff_time()
        # 计算当前是第几次重试
        attempt_number = len(self.history) + 1
        
        # 记录一条警告级别的日志，这样既能引起注意又不会像错误一样吓人
        logger.warning(
            f"TMDb API 请求失败 ({reason})。将在 {backoff_time:.2f} 秒后重试... (第 {attempt_number}/{self.total} 次)"
        )

        return new_retry


# ★★★ 创建带连接池的 Session，由上层统一控制 TMDb 重试策略 ★★★
def requests_retry_session(
    retries=3,
    backoff_factor=0.5,
    status_forcelist=(500, 502, 503, 504),
    session=None,
):
    """创建一个带连接池的 requests.Session 对象。"""
    session = session or requests.Session()
    adapter = HTTPAdapter(max_retries=0, pool_connections=50, pool_maxsize=50)

    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

# 创建一个全局的、可复用的、带连接池的 session 实例
# 整个程序将通过这个实例来请求 TMDB API
tmdb_session = requests_retry_session()

_tmdb_request_state = threading.local()


def _set_last_tmdb_error(status_code: Optional[int], error_message: str, url: str):
    _tmdb_request_state.last_error = {
        "status_code": status_code,
        "message": error_message,
        "url": url,
    }


def clear_last_tmdb_error():
    _tmdb_request_state.last_error = None


def get_last_tmdb_error() -> Optional[Dict[str, Any]]:
    return getattr(_tmdb_request_state, "last_error", None)


# TMDb API 频率限制：按 1 秒窗口控制，官方约 50 req/s，留余量设为 40
_tmdb_rate_lock = threading.Lock()
_tmdb_request_times: list = []
TMDB_MAX_REQUESTS_PER_1S = 40


def _tmdb_rate_limit():
    """TMDb API 频率控制：1 秒内最多 40 次请求"""
    with _tmdb_rate_lock:
        now = time.monotonic()
        # 清理 1 秒前的记录
        _tmdb_request_times[:] = [t for t in _tmdb_request_times if now - t < 1]
        if len(_tmdb_request_times) >= TMDB_MAX_REQUESTS_PER_1S:
            wait = 1 - (now - _tmdb_request_times[0]) + 0.02
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
                _tmdb_request_times[:] = [t for t in _tmdb_request_times if now - t < 1]
        _tmdb_request_times.append(now)

def get_tmdb_api_base_url() -> str:
    """
    从配置管理器获取TMDb API基础URL，如果未配置则使用默认值
    """
    return config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_TMDB_API_BASE_URL, "https://api.themoviedb.org/3")

# 默认语言设置
DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_REGION = "CN"
DEFAULT_IMAGE_LANGUAGE = "zh,en,null,ja,ko"
CHINESE_TITLE_FALLBACK_LANGUAGES = ("zh-HK", "zh-TW", "zh-SG")
CHINESE_COLLECTION_TRANSLATION_REGIONS = ("CN", "SG", "MY", "HK", "TW", "MO")


def _normalize_tmdb_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _pick_chinese_collection_translation(translations_data: Optional[Dict[str, Any]]) -> tuple[str, str]:
    if not isinstance(translations_data, dict):
        return "", ""

    translations = [item for item in translations_data.get("translations", []) or [] if isinstance(item, dict)]
    for region in CHINESE_COLLECTION_TRANSLATION_REGIONS:
        for item in translations:
            if str(item.get("iso_639_1") or "").lower() != "zh":
                continue
            if str(item.get("iso_3166_1") or "").upper() != region:
                continue
            data = item.get("data") or {}
            title = _normalize_tmdb_text(data.get("title") or data.get("name"))
            overview = _normalize_tmdb_text(data.get("overview"))
            if title and contains_chinese(title):
                return title, overview

    for item in translations:
        if str(item.get("iso_639_1") or "").lower() != "zh":
            continue
        data = item.get("data") or {}
        title = _normalize_tmdb_text(data.get("title") or data.get("name"))
        overview = _normalize_tmdb_text(data.get("overview"))
        if title and contains_chinese(title):
            return title, overview
    return "", ""


def _infer_chinese_collection_name_from_parts(details: Dict[str, Any]) -> str:
    if not isinstance(details, dict):
        return ""

    original_name = _normalize_tmdb_text(details.get("original_name") or details.get("name"))
    suffix = "三部曲 (系列)" if "trilogy" in original_name.lower() else " (系列)"
    parts = details.get("parts") or []
    sorted_parts = sorted(
        [part for part in parts if isinstance(part, dict)],
        key=lambda part: str(part.get("release_date") or "9999-99-99"),
    )
    for part in sorted_parts:
        title = _normalize_tmdb_text(part.get("title") or part.get("name"))
        if title and contains_chinese(title):
            return f"{title}{suffix}"
    return ""


def _apply_chinese_collection_name_fallback(details: Optional[Dict[str, Any]], api_key: str) -> Optional[Dict[str, Any]]:
    if not isinstance(details, dict) or not DEFAULT_LANGUAGE.startswith("zh"):
        return details

    details["_collection_language_fallback_checked"] = True
    current_name = _normalize_tmdb_text(details.get("name"))
    if current_name:
        details.setdefault("original_name", current_name)
    if contains_chinese(current_name):
        details["_collection_name_source"] = "tmdb"
        return details

    collection_id = details.get("id")
    translation_name = ""
    translation_overview = ""
    try:
        translations_data = _tmdb_request(
            f"/collection/{collection_id}/translations",
            api_key,
            {},
            use_default_language=False,
        )
        translation_name, translation_overview = _pick_chinese_collection_translation(translations_data)
    except Exception as e:
        logger.debug(f"TMDb: 获取合集中文翻译失败 (ID: {collection_id}): {e}")

    fallback_source = "translation" if translation_name else "parts"
    fallback_name = translation_name or _infer_chinese_collection_name_from_parts(details)
    if fallback_name:
        details["name"] = fallback_name
        details["localized_name"] = fallback_name
        details["_collection_name_source"] = fallback_source
        if translation_overview and not contains_chinese(_normalize_tmdb_text(details.get("overview"))):
            details["overview"] = translation_overview
        logger.debug(f"TMDb: 合集名称中文化: {current_name or collection_id} -> {fallback_name}")
    else:
        details["_collection_name_source"] = "original"
    return details


def _apply_chinese_title_language_fallback(
    details: Optional[Dict[str, Any]],
    endpoint: str,
    api_key: str,
    title_field: str,
    language: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Mirror TMDb web fallback language behavior for Chinese titles."""
    if not details:
        return details

    current_language = language or DEFAULT_LANGUAGE
    if not str(current_language or "").lower().startswith("zh"):
        return details

    current_title = str(details.get(title_field) or "")
    if contains_chinese(current_title):
        details["_title_language_fallback_checked"] = True
        details["_title_language"] = current_language
        return details

    fallback_languages = [
        lang
        for lang in CHINESE_TITLE_FALLBACK_LANGUAGES
        if lang.lower() != str(current_language or "").lower()
    ]
    for fallback_language in fallback_languages:
        fallback_details = _tmdb_request(
            endpoint,
            api_key,
            {"language": fallback_language},
            use_default_language=False,
        )
        fallback_title = str((fallback_details or {}).get(title_field) or "").strip()
        if contains_chinese(fallback_title):
            details[title_field] = fallback_title
            details["_title_language_fallback_checked"] = True
            details["_title_language"] = fallback_language
            logger.trace(
                f"  通过 TMDb 备选语言 {fallback_language} 补充中文标题: {fallback_title}"
            )
            return details

    details["_title_language_fallback_checked"] = True
    details["_title_language"] = current_language
    return details


def _tmdb_request(endpoint: str, api_key: str, params: Optional[Dict[str, Any]] = None, use_default_language: bool = True) -> Optional[Dict[str, Any]]:
    """【V2.3】按状态码区分 TMDb 重试策略，并记录最后一次错误。"""
    clear_last_tmdb_error()
    if not api_key:
        logger.error("TMDb API Key 未提供，无法发起请求。")
        _set_last_tmdb_error(None, "TMDb API Key 未提供", "")
        return None

    tmdb_base_url = get_tmdb_api_base_url()
    full_url = f"{tmdb_base_url}{endpoint}"
    base_params = {
        "api_key": api_key,
    }
    if use_default_language:
        base_params["language"] = DEFAULT_LANGUAGE
    if params:
        base_params.update(params)

    proxies = config_manager.get_proxies_for_requests()
    max_attempts = 5
    max_429_attempts = 5
    unknown_status_retried = False
    last_response = None
    _429_count = 0

    for attempt in range(1, max_attempts + 1):
        try:
            _tmdb_rate_limit()
            response = tmdb_session.get(full_url, params=base_params, timeout=(10, 15), proxies=proxies)
            last_response = response

            if response.status_code == 404:
                try:
                    error_data = response.json()
                    error_details = error_data.get("status_message", response.text)
                except Exception:
                    error_details = response.text
                logger.error(f"TMDb API 资源不存在 (404): {error_details}. URL: {full_url}", exc_info=False)
                _set_last_tmdb_error(404, error_details, full_url)
                return None

            if response.status_code in (401, 403):
                try:
                    error_data = response.json()
                    error_details = error_data.get("status_message", response.text)
                except Exception:
                    error_details = response.text
                logger.error(f"TMDb API 鉴权/权限错误 ({response.status_code}): {error_details}. URL: {full_url}", exc_info=False)
                _set_last_tmdb_error(response.status_code, error_details, full_url)
                return None

            if response.status_code == 429:
                _429_count += 1
                retry_after_header = response.headers.get("Retry-After")
                if retry_after_header:
                    wait = int(retry_after_header)
                else:
                    wait = min(2 ** _429_count, 60)
                if _429_count >= max_429_attempts:
                    logger.error(f"TMDb API 限流 (429) 且已达到最大重试次数: URL: {full_url}", exc_info=False)
                    _set_last_tmdb_error(429, "TMDb API 限流且达到最大重试次数", full_url)
                    return None
                logger.warning(f"TMDb API 限流 (429)，等待 {wait}s 后重试... (第 {_429_count}/{max_429_attempts} 次, {'Retry-After' if retry_after_header else '指数退避'})")
                time.sleep(wait)
                continue

            if 500 <= response.status_code <= 504:
                if attempt >= max_attempts:
                    try:
                        error_data = response.json()
                        error_details = error_data.get("status_message", response.text)
                    except Exception:
                        error_details = response.text
                    logger.error(f"所有重试后 TMDb API HTTP 出现错误: {response.status_code} - {error_details}. URL: {full_url}", exc_info=False)
                    _set_last_tmdb_error(response.status_code, error_details, full_url)
                    return None
                logger.warning(f"TMDb API 服务端错误 ({response.status_code})，准备重试... (第 {attempt}/{max_attempts} 次)")
                time.sleep(min(2 ** (attempt - 1), 8))
                continue

            if response.status_code >= 400:
                if attempt == 1 and not unknown_status_retried:
                    unknown_status_retried = True
                    logger.warning(f"TMDb API 返回未知错误状态码 ({response.status_code})，重试一次... URL: {full_url}")
                    time.sleep(1)
                    continue
                try:
                    error_data = response.json()
                    error_details = error_data.get("status_message", response.text)
                except Exception:
                    error_details = response.text
                logger.error(f"所有重试后 TMDb API HTTP 出现错误: {response.status_code} - {error_details}. URL: {full_url}", exc_info=False)
                _set_last_tmdb_error(response.status_code, error_details, full_url)
                return None

            response.raise_for_status()
            try:
                return response.json()
            except json.JSONDecodeError as e:
                logger.error(f"TMDb API JSON 解码错误: {e}. URL: {full_url}. Response: {response.text[:200] if response else 'N/A'}", exc_info=False)
                _set_last_tmdb_error(None, f"JSON decode error: {e}", full_url)
                return None

        except requests.exceptions.Timeout as e:
            if attempt >= max_attempts:
                logger.error(f"所有重试后 TMDb API 请求均出现错误: {e}. URL: {full_url}", exc_info=False)
                _set_last_tmdb_error(None, str(e), full_url)
                return None
            logger.warning(f"TMDb API 请求超时，准备重试... (第 {attempt}/{max_attempts} 次) URL: {full_url}")
            time.sleep(min(2 ** (attempt - 1), 8))
        except requests.exceptions.ConnectionError as e:
            if attempt >= max_attempts:
                logger.error(f"所有重试后 TMDb API 请求均出现错误: {e}. URL: {full_url}", exc_info=False)
                _set_last_tmdb_error(None, str(e), full_url)
                return None
            logger.warning(f"TMDb API 网络异常，准备重试... (第 {attempt}/{max_attempts} 次) URL: {full_url}")
            time.sleep(min(2 ** (attempt - 1), 8))
        except requests.exceptions.RequestException as e:
            if attempt >= max_attempts:
                logger.error(f"所有重试后 TMDb API 请求均出现错误: {e}. URL: {full_url}", exc_info=False)
                _set_last_tmdb_error(None, str(e), full_url)
                return None
            logger.warning(f"TMDb API 请求异常，准备重试... (第 {attempt}/{max_attempts} 次) URL: {full_url}")
            time.sleep(min(2 ** (attempt - 1), 8))
        except json.JSONDecodeError as e:
            logger.error(f"TMDb API JSON 解码错误: {e}. URL: {full_url}. Response: {last_response.text[:200] if last_response else 'N/A'}", exc_info=False)
            _set_last_tmdb_error(None, str(e), full_url)
            return None
# --- 获取电影的详细信息 ---
def _enrich_movie_collection_details(details: Optional[Dict[str, Any]], api_key: str) -> Optional[Dict[str, Any]]:
    if not isinstance(details, dict):
        return details

    collection = details.get("belongs_to_collection")
    if not isinstance(collection, dict):
        return details

    try:
        collection_id = int(collection.get("id") or 0)
    except (TypeError, ValueError):
        collection_id = 0
    if not collection_id:
        return details

    try:
        collection_details = get_collection_details(collection_id, api_key)
        if not isinstance(collection_details, dict):
            return details
        details["collection_details"] = collection_details
        for key in ("poster_path", "backdrop_path"):
            if not collection.get(key) and collection_details.get(key):
                collection[key] = collection_details.get(key)
        detail_name = _normalize_tmdb_text(collection_details.get("name"))
        current_name = _normalize_tmdb_text(collection.get("name"))
        if detail_name and (
            not current_name
            or (DEFAULT_LANGUAGE.startswith("zh") and contains_chinese(detail_name) and not contains_chinese(current_name))
        ):
            if current_name:
                collection.setdefault("original_name", current_name)
            collection["name"] = detail_name
    except Exception as e:
        logger.debug(f"TMDb: 获取合集详情失败 (ID: {collection_id}): {e}")

    return details


def get_movie_details(movie_id: int, api_key: str, append_to_response: Optional[str] = "credits,videos,images,keywords,external_ids,translations,release_dates,alternative_titles", language: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    【新增】获取电影的详细信息。
    """
    endpoint = f"/movie/{movie_id}"
    params = {
        "language": language or DEFAULT_LANGUAGE,
        "append_to_response": append_to_response or "",
        "include_image_language": DEFAULT_IMAGE_LANGUAGE
    }
    logger.trace(f"TMDb: 获取电影详情 (ID: {movie_id})")
    details = _tmdb_request(endpoint, api_key, params)
    details = _apply_chinese_title_language_fallback(details, endpoint, api_key, "title", language)

    if details and details.get("original_language") != "en" and DEFAULT_LANGUAGE.startswith("zh"):
        if "translations" in (append_to_response or "") and details.get("translations", {}).get("translations"):
            for trans in details["translations"]["translations"]:
                if trans.get("iso_639_1") == "en" and trans.get("data", {}).get("title"):
                    details["english_title"] = trans["data"]["title"]
                    logger.trace(f"  从translations补充电影英文名: {details['english_title']}")
                    break
        if not details.get("english_title"):
            logger.trace(f"  尝试获取电影 {movie_id} 的英文名...")
            en_params = {"language": "en-US"}
            en_details = _tmdb_request(f"/movie/{movie_id}", api_key, en_params)
            if en_details and en_details.get("title"):
                details["english_title"] = en_details.get("title")
                logger.trace(f"  通过请求英文版补充电影英文名: {details['english_title']}")
    elif details and details.get("original_language") == "en":
        details["english_title"] = details.get("original_title")

    details = _enrich_movie_collection_details(details, api_key)
    return details


def _pick_best_tmdb_search_result(search_results: list[dict[str, Any]], title: str, year: Optional[str], item_type: str) -> Optional[dict[str, Any]]:
    if not search_results or not title:
        return None

    expected_year = str(year) if year else ""
    norm_title = normalize_name_for_matching(title)
    scored_results = []

    for result in search_results:
        result_title = result.get("title") if item_type == "movie" else result.get("name")
        result_orig = result.get("original_title") if item_type == "movie" else result.get("original_name")
        result_title = result_title or ""
        result_orig = result_orig or ""
        result_year = str((result.get("release_date") if item_type == "movie" else result.get("first_air_date")) or "")[:4]
        score = 0
        if normalize_name_for_matching(result_title) == norm_title or normalize_name_for_matching(result_orig) == norm_title:
            score += 3
        elif norm_title and norm_title in normalize_name_for_matching(result_title):
            score += 2
        if expected_year and result_year == expected_year:
            score += 2
        score += int(result.get("popularity") or 0)
        scored_results.append((score, result))

    if not scored_results:
        return None

    scored_results.sort(key=lambda item: item[0], reverse=True)
    return scored_results[0][1]


def find_movie_tmdb_id_by_title_year(title: str, api_key: str, year: Optional[str]) -> Optional[int]:
    if not title or not api_key:
        return None

    search_results = search_media(title, api_key, item_type="movie", year=year)
    if not search_results and year:
        search_results = search_media(title, api_key, item_type="movie", year=None)
    picked = _pick_best_tmdb_search_result(search_results or [], title, year, "movie")
    return int(picked.get("id")) if picked and picked.get("id") else None


def find_tv_tmdb_id_by_title_year(title: str, api_key: str, year: Optional[str]) -> Optional[int]:
    if not title or not api_key:
        return None

    search_results = search_media(title, api_key, item_type="tv", year=year)
    if not search_results and year:
        search_results = search_media(title, api_key, item_type="tv", year=None)
    picked = _pick_best_tmdb_search_result(search_results or [], title, year, "tv")
    return int(picked.get("id")) if picked and picked.get("id") else None


def get_movie_details_by_title_year(title: str, api_key: str, year: Optional[str], append_to_response: Optional[str] = "credits,videos,images,keywords,external_ids,translations,release_dates,alternative_titles") -> Optional[Dict[str, Any]]:
    picked_id = find_movie_tmdb_id_by_title_year(title, api_key, year)
    if not picked_id:
        return None
    return get_movie_details(int(picked_id), api_key, append_to_response=append_to_response)

# --- 获取电视剧的详细信息 ---
def get_tv_details(tv_id: int, api_key: str, append_to_response: Optional[str] = "credits,videos,images,keywords,external_ids,translations,content_ratings,alternative_titles", language: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    【已升级】获取电视剧的详细信息。
    """
    endpoint = f"/tv/{tv_id}"
    params = {
        "language": language or DEFAULT_LANGUAGE,
        "append_to_response": append_to_response or "",
        "include_image_language": DEFAULT_IMAGE_LANGUAGE
    }
    logger.trace(f"TMDb: 获取电视剧详情 (ID: {tv_id})")
    details = _tmdb_request(endpoint, api_key, params)
    details = _apply_chinese_title_language_fallback(details, endpoint, api_key, "name", language)

    if details and details.get("original_language") != "en" and DEFAULT_LANGUAGE.startswith("zh"):
        if "translations" in (append_to_response or "") and details.get("translations", {}).get("translations"):
            for trans in details["translations"]["translations"]:
                if trans.get("iso_639_1") == "en" and trans.get("data", {}).get("name"):
                    details["english_name"] = trans["data"]["name"]
                    logger.trace(f"  从translations补充剧集英文名: {details['english_name']}")
                    break
        if not details.get("english_name"):
            logger.trace(f"  尝试获取剧集 {tv_id} 的英文名...")
            en_params = {"language": "en-US"}
            en_details = _tmdb_request(f"/tv/{tv_id}", api_key, en_params)
            if en_details and en_details.get("name"):
                details["english_name"] = en_details.get("name")
                logger.trace(f"  通过请求英文版补充剧集英文名: {details['english_name']}")
    elif details and details.get("original_language") == "en":
        details["english_name"] = details.get("original_name")

    return details


def get_tv_details_by_title_year(title: str, api_key: str, year: Optional[str], append_to_response: Optional[str] = "credits,videos,images,keywords,external_ids,translations,content_ratings,alternative_titles") -> Optional[Dict[str, Any]]:
    picked_id = find_tv_tmdb_id_by_title_year(title, api_key, year)
    if not picked_id:
        return None
    return get_tv_details(int(picked_id), api_key, append_to_response=append_to_response)


def aggregate_full_series_data_by_title_year(title: str, api_key: str, year: Optional[str], max_workers: int = 5) -> Optional[Dict[str, Any]]:
    picked_id = find_tv_tmdb_id_by_title_year(title, api_key, year)
    if not picked_id:
        return None
    return aggregate_full_series_data_from_tmdb(int(picked_id), api_key, max_workers=max_workers)

# --- 获取演员详情 ---
def get_person_details_tmdb(person_id: int, api_key: str, append_to_response: Optional[str] = "movie_credits,tv_credits,images,external_ids,translations") -> Optional[Dict[str, Any]]:
    endpoint = f"/person/{person_id}"
    params = {
        "language": DEFAULT_LANGUAGE,
        "append_to_response": append_to_response
    }
    details = _tmdb_request(endpoint, api_key, params)

    # 尝试补充英文名，如果主语言是中文且original_name不是英文 (TMDb人物的original_name通常是其母语名)
    if details and details.get("name") != details.get("original_name") and DEFAULT_LANGUAGE.startswith("zh"):
        # 检查 translations 是否包含英文名
        if "translations" in (append_to_response or "") and details.get("translations", {}).get("translations"):
            for trans in details["translations"]["translations"]:
                if trans.get("iso_639_1") == "en" and trans.get("data", {}).get("name"):
                    details["english_name_from_translations"] = trans["data"]["name"]
                    logger.trace(f"  从translations补充人物英文名: {details['english_name_from_translations']}")
                    break
        # 如果 original_name 本身是英文，也可以用 (需要判断 original_name 的语言，较复杂)
        # 简单处理：如果 original_name 和 name 不同，且 name 是中文，可以认为 original_name 可能是外文名
        if details.get("original_name") and not contains_chinese(details.get("original_name", "")): # 假设 contains_chinese 在这里可用
             details["foreign_name_from_original"] = details.get("original_name")


    return details
# --- 获取电视剧某一季的详细信息 ---
def get_season_details_tmdb(tv_id: int, season_number: int, api_key: str, append_to_response: Optional[str] = "credits,images,external_ids,translations", item_name: Optional[str] = None, language: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    【已升级】获取电视剧某一季的详细信息，并支持 item_name 用于日志。
    ★ 修复：支持自定义 language 参数，用于获取英文兜底数据。
    """
    endpoint = f"/tv/{tv_id}/season/{season_number}"
    params = {
        "language": language or DEFAULT_LANGUAGE,
        "append_to_response": append_to_response or "",
        "include_image_language": DEFAULT_IMAGE_LANGUAGE,
    }

    item_name_for_log = f"'{item_name}' " if item_name else ""
    if language and language != DEFAULT_LANGUAGE:
        logger.trace(f"TMDb API: 获取电视剧 {item_name_for_log}(ID: {tv_id}) 第 {season_number} 季的详情 (语言: {language})...")
    else:
        logger.trace(f"TMDb API: 获取电视剧 {item_name_for_log}(ID: {tv_id}) 第 {season_number} 季的详情...")

    return _tmdb_request(endpoint, api_key, params)


def get_season_details_by_title_year(tv_title: str, season_number: int, api_key: str, year: Optional[str], append_to_response: Optional[str] = "credits", language: Optional[str] = None) -> Optional[Dict[str, Any]]:
    picked_id = find_tv_tmdb_id_by_title_year(tv_title, api_key, year)
    if not picked_id:
        return None
    return get_season_details_tmdb(int(picked_id), season_number, api_key, append_to_response=append_to_response, language=language)
# --- 获取电视剧某一季的详细信息，简化调用版 ---
def get_tv_season_details(tv_id: int, season_number: int, api_key: str) -> Optional[Dict[str, Any]]:
    """
    获取电视剧某一季的详细信息。
    这是 get_season_details_tmdb 的一个更简洁的别名，用于简化调用并获取海报。
    """
    # 直接调用已有的、功能更全的函数。
    # 我们不需要 'credits' 等附加信息，所以 append_to_response 传 None，这样请求更轻量。
    return get_season_details_tmdb(
        tv_id=tv_id,
        season_number=season_number,
        api_key=api_key,
        append_to_response=None
    )
# --- 接收一个剧集 TMDB ID 列表，并发地获取所有这些剧集的完整子项（季、集）信息 ---
def batch_get_full_series_details_tmdb(
    series_tmdb_ids: List[str], 
    api_key: str, 
    max_workers: int = 5, # <-- 将默认并发数从10降低到5，更加安全
    progress_callback: Optional[Callable] = None
) -> Dict[str, List[Dict]]:
    """
    【V1.2 - 稳定防崩溃版】
    接收一个剧集 TMDB ID 列表，并发地获取所有这些剧集的完整子项（季、集）信息。
    通过降低默认并发数和增加请求间隔，防止触发TMDb的速率限制。
    """
    if not series_tmdb_ids or not api_key:
        return {}

    logger.debug(f"[TMDb] 开始并发聚合子项数据: 剧集数={len(series_tmdb_ids)} 并发={max_workers}")
    
    all_children_data = {}
    completed_count = 0
    lock = threading.Lock()
    total_tasks = len(series_tmdb_ids)

    def _fetch_one_series_all_children(tv_id: str) -> Optional[List[Dict]]:
        """线程工作函数：获取单部剧集的所有子项"""
        try:
            series_details = get_tv_details(tv_id, api_key, append_to_response="seasons")
            if not series_details or not series_details.get("seasons"):
                logger.warning(f"无法获取剧集 {tv_id} 的季列表，跳过。")
                return None

            children = []
            for season_summary in series_details.get("seasons", []):
                season_num = season_summary.get("season_number")
                if season_num is None or season_num == 0:
                    continue
                
                children.append(season_summary)
                season_details = get_season_details_tmdb(tv_id, season_num, api_key)
                if season_details and season_details.get("episodes"):
                    children.extend(season_details["episodes"])
                
                # ★★★ 核心修复 2/2：在这里加入一个微小的延时 ★★★
                # 这会极大地增加程序的稳定性，避免被服务器拒绝连接。
                time.sleep(0.05) # 暂停 50 毫秒

            return children
        except Exception as e:
            logger.error(f"获取剧集 {tv_id} 的子项时出错: {e}", exc_info=False)
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {executor.submit(_fetch_one_series_all_children, tv_id): tv_id for tv_id in series_tmdb_ids}
        
        for future in concurrent.futures.as_completed(future_to_id):
            tv_id = future_to_id[future]
            try:
                result = future.result()
                if result:
                    all_children_data[tv_id] = result
            except Exception as exc:
                logger.error(f"    剧集 {tv_id} 的并发任务执行时产生异常: {exc}")
            finally:
                if progress_callback:
                    with lock:
                        completed_count += 1
                    
                    progress_percent = 15 + (completed_count / total_tasks) * 25
                    message = f"正在从TMDb并发获取... ({completed_count}/{total_tasks})"
                    progress_callback(int(progress_percent), message)

    logger.debug(f"[TMDb] 子项元数据聚合完成: {len(all_children_data)} 部剧集")
    return all_children_data
# --- 并发获取剧集详情 ---
def aggregate_full_series_data_from_tmdb(
    tv_id: int,
    api_key: str,
    max_workers: int = 5
) -> Optional[Dict[str, Any]]:
    """
    【V4 - 智能补全版】
    通过并发请求获取每一季的详情。
    ★ 新增特性：如果检测到分集简介为空（TMDb未返回中文），会自动请求英文版数据进行补全，
    确保 core_processor 的 AI 翻译功能有源文本可译。
    """
    if not tv_id or not api_key:
        return None

    logger.debug(f"[TMDb] 开始聚合剧集数据: tv_id={tv_id} 并发={max_workers}")
    
    # --- 步骤 1: 获取顶层剧集详情 ---
    series_details = get_tv_details(tv_id, api_key, append_to_response="credits,aggregate_credits,keywords,external_ids,translations,content_ratings,alternative_titles")
    
    if not series_details:
        logger.error(f"聚合失败：无法获取顶层剧集 {tv_id} 的详情。")
        return None
    
    # (此处省略补全主演员表的代码，保持原样即可)
    if series_details.get('aggregate_credits'):
        agg_cast = series_details['aggregate_credits'].get('cast', [])
        mapped_cast = []
        for actor in agg_cast:
            new_actor = actor.copy()
            roles = actor.get('roles', [])
            if roles and 'character' in roles[0]:
                new_actor['character'] = roles[0]['character']
            mapped_cast.append(new_actor)
        if mapped_cast:
            if 'credits' not in series_details: series_details['credits'] = {}
            series_details['credits']['cast'] = mapped_cast

    logger.debug(f"[TMDb] 已获取剧集顶层信息: {series_details.get('name')}，共 {len(series_details.get('seasons', []))} 季")

    # --- 步骤 2: 定义智能获取函数 ---
    def _fetch_season_smart(tvid, s_num):
        """内部函数：先获取默认语言季数据，仅在分集简介缺失时请求英文补全。"""
        data_zh = get_season_details_tmdb(tvid, s_num, api_key)
        if not data_zh:
            return None

        episodes = data_zh.get("episodes", [])
        missing_overview = any(not ep.get("overview") or len(ep.get("overview")) < 2 for ep in episodes)
        if not DEFAULT_LANGUAGE.startswith("zh") or not missing_overview:
            return data_zh

        data_en = get_season_details_tmdb(tvid, s_num, api_key, language="en-US")
        if not data_en:
            return data_zh

        episodes_en = data_en.get("episodes", [])
        en_ep_map = {e.get("episode_number"): e for e in episodes_en}

        filled_count = 0
        for ep in episodes:
            if not ep.get("overview") or len(ep.get("overview")) < 2:
                ep_num = ep.get("episode_number")
                if ep_num in en_ep_map:
                    en_overview = en_ep_map[ep_num].get("overview")
                    if en_overview:
                        ep["overview"] = en_overview
                        filled_count += 1

        if filled_count > 0:
            logger.debug(f"第 {s_num} 季成功补全了 {filled_count} 条英文简介。")

        return data_zh

    # --- 步骤 3: 构建任务 ---
    tasks = []
    for season in series_details.get("seasons", []):
        season_number = season.get("season_number")
        if season_number is not None and season_number > 0:
            tasks.append(("season", tv_id, season_number))

    if not tasks:
        return {"series_details": series_details, "seasons_details": [], "episodes_details": {}}

    # --- 步骤 4: 并发执行 (使用 _fetch_season_smart) ---
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {}
        for task in tasks:
            _, tvid, s_num = task
            # ★★★ 这里提交的是 _fetch_season_smart ★★★
            future = executor.submit(_fetch_season_smart, tvid, s_num)
            future_to_task[future] = f"S{s_num}"

        for i, future in enumerate(concurrent.futures.as_completed(future_to_task)):
            task_key = future_to_task[future]
            try:
                result_data = future.result()
                if result_data:
                    results[task_key] = result_data
                logger.trace(f"    ({i+1}/{len(tasks)}) 季数据 {task_key} 获取完成。")
            except Exception as exc:
                logger.error(f"    任务 {task_key} 执行时产生错误: {exc}")

    # --- 步骤 5: 聚合数据与结构清洗 (保持不变) ---
    final_aggregated_data = {
        "series_details": series_details,
        "seasons_details": [], 
        "episodes_details": {} 
    }

    temp_seasons = []

    for key, season_data in results.items():
        if not season_data: continue
        
        temp_seasons.append(season_data)
        
        episodes_list = season_data.get("episodes", [])
        season_num = season_data.get("season_number")
        
        for ep in episodes_list:
            ep_num = ep.get("episode_number")
            if season_num is not None and ep_num is not None:
                if 'credits' not in ep:
                    ep['credits'] = {
                        'cast': ep.get('cast', []),
                        'guest_stars': ep.get('guest_stars', []),
                        'crew': ep.get('crew', [])
                    }
                
                ep_key = f"S{season_num}E{ep_num}"
                final_aggregated_data["episodes_details"][ep_key] = ep

    temp_seasons.sort(key=lambda x: x.get("season_number", 0))
    final_aggregated_data["seasons_details"] = temp_seasons
            
    logger.debug(f"[TMDb] 聚合完成: 季详情={len(temp_seasons)} 集详情={len(final_aggregated_data['episodes_details'])}")
    
    return final_aggregated_data
# +++ 获取集详情 +++
def get_episode_details_tmdb(tv_id: int, season_number: int, episode_number: int, api_key: str, append_to_response: Optional[str] = "credits,videos,images,external_ids") -> Optional[Dict[str, Any]]:
    """
    获取电视剧某一集的详细信息。
    """
    endpoint = f"/tv/{tv_id}/season/{season_number}/episode/{episode_number}"
    params = {
        "language": DEFAULT_LANGUAGE,
        "append_to_response": append_to_response or "",
        "include_image_language": DEFAULT_IMAGE_LANGUAGE,
    }
    logger.trace(f"TMDb API: 获取电视剧 (ID: {tv_id}) S{season_number}E{episode_number} 的详情...")
    return _tmdb_request(endpoint, api_key, params)
def get_tv_episode_details(tv_id: int, season_number: int, episode_number: int, api_key: str) -> Optional[Dict[str, Any]]:
    """
    获取电视剧某一集的详细信息。
    这是 get_episode_details_tmdb 的一个更简洁的别名，用于简化调用。
    """
    return get_episode_details_tmdb(
        tv_id=tv_id,
        season_number=season_number,
        episode_number=episode_number,
        api_key=api_key,
        append_to_response=None # 仅获取基础信息，不需要 credits 等额外数据，提高速度
    )
# --- 通过外部ID (如 IMDb ID) 在 TMDb 上查找人物 ---
def find_person_by_external_id(external_id: str, api_key: str, source: str = "imdb_id",
                               names_for_verification: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """
    【V5 - 精确匹配版】通过外部ID查找TMDb名人信息。
    只使用最可靠的外文名 (original_name) 进行精确匹配验证。
    """
    if not all([external_id, api_key, source]):
        return None
    tmdb_base_url = get_tmdb_api_base_url()
    api_url = f"{tmdb_base_url}/find/{external_id}"
    params = {"api_key": api_key, "external_source": source, "language": "en-US"}
    logger.debug(f"TMDb: 正在通过 {source} '{external_id}' 查找人物...")
    try:
        _tmdb_rate_limit()
        proxies = config_manager.get_proxies_for_requests()
        response = tmdb_session.get(api_url, params=params, timeout=(10, 15), proxies=proxies)
        response.raise_for_status()
        data = response.json()
        person_results = data.get("person_results", [])
        if not person_results:
            logger.debug(f"未能通过 {source} '{external_id}' 找到任何人物。")
            return None

        person_found = person_results[0]
        tmdb_name = person_found.get('name')
        logger.debug(f"查找成功: 找到了 '{tmdb_name}' (TMDb ID: {person_found.get('id')})")

        if names_for_verification:
            # 1. 标准化 TMDb 返回的英文名
            normalized_tmdb_name = normalize_name_for_matching(tmdb_name)
            
            # 2. 获取我们期望的外文名 (通常来自豆瓣的 OriginalName)
            expected_original_name = names_for_verification.get("original_name")
            
            # 3. 只有在期望的外文名存在时，才进行验证
            if expected_original_name:
                normalized_expected_name = normalize_name_for_matching(expected_original_name)
                
                # 4. 进行精确比较
                if normalized_tmdb_name == normalized_expected_name:
                    logger.debug(f"[验证成功 - 精确匹配] TMDb name '{tmdb_name}' 与期望的 original_name '{expected_original_name}' 匹配。")
                else:
                    # 如果不匹配，检查一下姓和名颠倒的情况
                    parts = expected_original_name.split()
                    if len(parts) > 1:
                        reversed_name = " ".join(reversed(parts))
                        if normalize_name_for_matching(reversed_name) == normalized_tmdb_name:
                            logger.debug(f"[验证成功 - 精确匹配] 名字为颠倒顺序匹配。")
                            return person_found # 颠倒匹配也算成功

                    # 如果精确匹配和颠倒匹配都失败，则拒绝
                    logger.error(f"[验证失败] TMDb返回的名字 '{tmdb_name}' 与期望的 '{expected_original_name}' 不符。拒绝此结果！")
                    return None
            else:
                # 如果豆瓣没有提供外文名，我们无法进行精确验证，可以选择信任或拒绝
                # 当前选择信任，但打印一条警告
                logger.warning(f"[验证跳过] 未提供用于精确匹配的 original_name，将直接接受TMDb结果。")
        
        return person_found

    except requests.exceptions.RequestException as e:
        logger.error(f"TMDb: 通过外部ID查找时发生网络错误: {e}")
        return None
# --- 获取合集的详细信息 ---
def get_collection_details(collection_id: int, api_key: str) -> Optional[Dict[str, Any]]:
    """
    【新】获取指定 TMDb 合集的详细信息，包含其所有影片部分。
    """
    if not collection_id or not api_key:
        return None
        
    endpoint = f"/collection/{collection_id}"
    params = {"language": DEFAULT_LANGUAGE}
    
    logger.debug(f"TMDb: 获取合集详情 (ID: {collection_id})")
    details = _tmdb_request(endpoint, api_key, params)
    return _apply_chinese_collection_name_fallback(details, api_key)
# --- 搜索媒体 ---
def _merge_search_results(*result_groups: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()
    for results in result_groups:
        for result in results or []:
            result_id = result.get("id") if isinstance(result, dict) else None
            key = result_id if result_id is not None else id(result)
            if key in seen:
                continue
            seen.add(key)
            merged.append(result)
    return merged


def search_media(query: str, api_key: str, item_type: str = 'movie', year: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """
    【V3 - 年份感知版】通过名字在 TMDb 上搜索媒体（电影、电视剧、演员），支持年份筛选。
    """
    if not query or not api_key:
        return None
    
    # 根据 item_type 决定 API 的端点
    endpoint_map = {
        'movie': '/search/movie',
        'tv': '/search/tv',
        'series': '/search/tv', # series 是 tv 的别名
        'person': '/search/person'
    }
    endpoint = endpoint_map.get(item_type.lower())
    
    if not endpoint:
        logger.error(f"不支持的搜索类型: '{item_type}'")
        return None

    params = {
        "query": query,
        "include_adult": "true", # 电影搜索通常需要包含成人内容
        "language": DEFAULT_LANGUAGE
    }
    
    # 新增：如果提供了年份，则添加到请求参数中
    if year:
        item_type_lower = item_type.lower()
        if item_type_lower == 'movie':
            params['year'] = year
        elif item_type_lower in ['tv', 'series']:
            params['first_air_date_year'] = year

    year_info = f" (年份: {year})" if year else ""
    logger.debug(f"TMDb: 正在搜索 {item_type}: '{query}'{year_info}")
    data = _tmdb_request(endpoint, api_key, params)
    results = data.get("results") if data else None

    if params['language'].startswith("zh"):
        en_params = dict(params)
        en_params['language'] = 'en-US'

        # 如果中文搜索不到，可以尝试用英文再搜一次
        if data and not results:
            logger.debug(f"中文搜索 '{query}'{year_info} 未找到结果，尝试使用英文再次搜索...")
            en_data = _tmdb_request(endpoint, api_key, en_params)
            return en_data.get("results") if en_data else None

        # 非中文标题在 zh-CN 搜索下可能返回中文本地化字段，导致后续精确匹配看不到英文名。
        # 合并 en-US 结果并放在前面，保留中文结果作为补充候选。
        if results and not contains_chinese(query):
            en_data = _tmdb_request(endpoint, api_key, en_params)
            en_results = en_data.get("results") if en_data else None
            if en_results:
                return _merge_search_results(en_results, results)

    return results


def get_null_language_backdrop(media_id: int, media_type: str, api_key: str) -> str:
    """
    获取 TMDB 媒体的"未定义语言"剧照/背景图 URL（w500 尺寸）。

    TMDB 的 images 接口按语言分类返回剧照，其中 iso_639_1 为 null 的图片
    是未附带任何语言标签的剧照（通常是无文字的纯视觉图），适合用作通知封面。

    Args:
        media_id: TMDB 媒体 ID
        media_type: 'movie' 或 'tv'
        api_key: TMDB API Key

    Returns:
        str: 图片 URL，获取失败返回空字符串
    """
    if not api_key or not media_id:
        return ""

    type_prefix = "movie" if media_type == "movie" else "tv"
    endpoint = f"/{type_prefix}/{media_id}/images"

    # 只请求 null 语言的图片，减少返回数据量
    data = _tmdb_request(endpoint, api_key, params={
        "include_image_language": "null",
    }, use_default_language=False)

    if not data:
        return ""

    backdrops = data.get("backdrops", [])
    if backdrops:
        # 按投票数降序，取最热门的一张
        backdrops.sort(key=lambda x: x.get("vote_count", 0), reverse=True)
        path = backdrops[0].get("file_path", "")
        if path:
            return f"https://image.tmdb.org/t/p/w500{path}"

    # 如果没有 null 语言的 backdrop，尝试 still（剧照）
    stills = data.get("stills", [])
    if stills:
        stills.sort(key=lambda x: x.get("vote_count", 0), reverse=True)
        path = stills[0].get("file_path", "")
        if path:
            return f"https://image.tmdb.org/t/p/w500{path}"

    return ""


# --- 搜索媒体 (为探索页面定制) ---
def search_media_for_discover(query: str, api_key: str, item_type: str = 'movie', year: Optional[str] = None, page: int = 1) -> Optional[Dict[str, Any]]:
    """
    【新】为探索页面的搜索功能定制，返回完整的TMDb响应对象。
    """
    if not query or not api_key:
        return None
    
    endpoint_map = {
        'movie': '/search/movie',
        'tv': '/search/tv',
        'series': '/search/tv',
        'person': '/search/person'
    }
    endpoint = endpoint_map.get(item_type.lower())
    
    if not endpoint:
        logger.error(f"不支持的搜索类型: '{item_type}'")
        return None

    params = {
        "query": query,
        "include_adult": "true",
        "language": DEFAULT_LANGUAGE,
        "page": page
    }
    
    if year:
        if item_type.lower() == 'movie':
            params['year'] = year
        elif item_type.lower() in ['tv', 'series']:
            params['first_air_date_year'] = year

    year_info = f" (年份: {year})" if year else ""
    logger.debug(f"TMDb: 正在搜索 {item_type}: '{query}'{year_info} at page {page}")
    data = _tmdb_request(endpoint, api_key, params)
    
    if data and not data.get("results") and params['language'].startswith("zh"):
        logger.debug(f"中文搜索 '{query}'{year_info} 未找到结果，尝试使用英文再次搜索...")
        params['language'] = 'en-US'
        data = _tmdb_request(endpoint, api_key, params)

    return data
# --- 搜索电视剧 ---
def search_tv_shows(query: str, api_key: str, year: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """
    【新增】通过名字在 TMDb 上搜索电视剧。
    这是 search_media 的一个便捷封装。
    """
    return search_media(query=query, api_key=api_key, item_type='tv', year=year)
# --- 搜索演员 ---
def search_person_tmdb(query: str, api_key: str) -> Optional[List[Dict[str, Any]]]:
    """
    【新】通过名字在 TMDb 上搜索演员。
    """
    if not query or not api_key:
        return None
    endpoint = "/search/person"
    # 我们可以添加一些参数来优化搜索，比如只搜索非成人内容，并优先中文结果
    params = {
        "query": query,
        "include_adult": "false",
        "language": DEFAULT_LANGUAGE # 使用模块内定义的默认语言
    }
    logger.debug(f"TMDb: 正在搜索演员: '{query}'")
    data = _tmdb_request(endpoint, api_key, params)
    return data.get("results") if data else None
# --- 获取演员的所有影视作品 ---
def get_person_credits_tmdb(person_id: int, api_key: str) -> Optional[Dict[str, Any]]:
    """
    【新】获取一个演员参与的所有电影和电视剧作品。
    使用 append_to_response 来一次性获取 movie_credits 和 tv_credits。
    """
    if not person_id or not api_key:
        return None
    
    endpoint = f"/person/{person_id}"
    # ★★★ 关键：一次请求同时获取电影和电视剧作品 ★★★
    params = {
        "append_to_response": "movie_credits,tv_credits"
    }
    logger.trace(f"TMDb: 正在获取演员 (ID: {person_id}) 的所有作品...")
    
    # 这里我们直接调用 get_person_details_tmdb，因为它内部已经包含了 _tmdb_request 的逻辑
    # 并且我们不需要它的其他附加信息，所以第三个参数传我们自己的 append_to_response
    details = get_person_details_tmdb(person_id, api_key, append_to_response="movie_credits,tv_credits")

    return details

# --- 通过 TMDb API v3 /find/{imdb_id} 方式获取TMDb ID ---
def get_tmdb_id_by_imdb_id(imdb_id: str, api_key: str, media_type: str) -> Optional[int]:
    """
    通过 TMDb API v3 /find/{imdb_id} 方式获取TMDb ID。
    media_type: 'movie' 或 'tv'
    """
    tmdb_base_url = get_tmdb_api_base_url()
    url = f"{tmdb_base_url}/find/{imdb_id}"
    params = {
        "api_key": api_key,
        "external_source": "imdb_id"
    }
    
    try:
        _tmdb_rate_limit()
        proxies = config_manager.get_proxies_for_requests()
        resp = tmdb_session.get(url, params=params, proxies=proxies, timeout=(10, 15))
        
        if resp.status_code == 200:
            data = resp.json()
            if media_type.lower() == 'movie' and data.get('movie_results'):
                return data['movie_results'][0].get('id')
            elif media_type.lower() in ['series', 'tv']:
                if data.get('tv_results'):
                    return data['tv_results'][0].get('id')
    except Exception as e:
        logger.error(f"通过 IMDb ID 获取 TMDb ID 失败: {e}")
        
    return None

def get_list_details_tmdb(list_id: int, api_key: str, page: int = 1) -> Optional[Dict[str, Any]]:
    """
    【新】获取指定 TMDb 片单的详细信息，支持分页。
    """
    if not list_id or not api_key:
        return None
        
    endpoint = f"/list/{list_id}"
    params = {
        "language": DEFAULT_LANGUAGE,
        "page": page
    }
    
    logger.debug(f"TMDb: 获取片单详情 (ID: {list_id}, Page: {page})")
    return _tmdb_request(endpoint, api_key, params)

# --- 探索电影 ---
def discover_movie_tmdb(api_key: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """ 通过筛选条件发现电影。"""
    if not api_key:
        return None
    endpoint = "/discover/movie"
    logger.trace(f"TMDb: 发现电影 (条件: {params})")
    return _tmdb_request(endpoint, api_key, params, use_default_language=True)

# --- 探索电视剧 ---
def discover_tv_tmdb(api_key: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """ 通过筛选条件发现电视剧。"""
    if not api_key:
        return None
    endpoint = "/discover/tv"
    logger.trace(f"TMDb: 发现电视剧 (条件: {params})")
    return _tmdb_request(endpoint, api_key, params, use_default_language=True)

def get_movie_genres_tmdb(api_key: str) -> Optional[List[Dict[str, Any]]]:
    """【新】获取TMDb所有电影类型的官方列表。"""
    endpoint = "/genre/movie/list"
    data = _tmdb_request(endpoint, api_key, {"language": DEFAULT_LANGUAGE})
    return data.get("genres") if data else None

# --- 获取电视剧类型列表 ---
def get_tv_genres_tmdb(api_key: str) -> Optional[List[Dict[str, Any]]]:
    """【新】获取TMDb所有电视剧类型的官方列表。"""
    endpoint = "/genre/tv/list"
    data = _tmdb_request(endpoint, api_key, {"language": DEFAULT_LANGUAGE})
    return data.get("genres") if data else None

# --- 搜索 TMDb 电影公司 ---
def search_companies_tmdb(api_key: str, query: str) -> Optional[List[Dict[str, Any]]]:
    """【新】根据文本搜索TMDb电影公司，返回ID和名称。"""
    endpoint = "/search/company"
    params = {"query": query}
    data = _tmdb_request(endpoint, api_key, params)
    return data.get("results") if data else None

# --- 探索 TMDb 热门电影 ---
def get_popular_movies_tmdb(api_key: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    获取 TMDb 上的热门电影列表，支持分页等参数。
    这是“每日推荐”功能的核心数据源。
    """
    if not api_key:
        return None
    endpoint = "/movie/popular"
    logger.debug(f"TMDb: 获取热门电影 (参数: {params})")
    return _tmdb_request(endpoint, api_key, params, use_default_language=True)

# --- 获取 TMDb 趋势（周榜/日榜） ---
def get_trending_tmdb(api_key: str, media_type: str = "all", time_window: str = "week", page: int = 1) -> Optional[Dict[str, Any]]:
    """获取 TMDb 趋势榜单（所有/电影/剧集 x 日/周）"""
    if not api_key:
        return None
    endpoint = f"/trending/{media_type}/{time_window}"
    return _tmdb_request(endpoint, api_key, {"page": page}, use_default_language=True)

# --- 获取正在上映电影 ---
def get_now_playing_tmdb(api_key: str, page: int = 1) -> Optional[Dict[str, Any]]:
    """获取 TMDb 正在热映电影列表"""
    if not api_key:
        return None
    endpoint = "/movie/now_playing"
    return _tmdb_request(endpoint, api_key, {"page": page}, use_default_language=True)

# --- 获取热门电视剧 ---
def get_popular_tv_tmdb(api_key: str, page: int = 1) -> Optional[Dict[str, Any]]:
    """获取 TMDb 热门电视剧列表"""
    if not api_key:
        return None
    endpoint = "/tv/popular"
    return _tmdb_request(endpoint, api_key, {"page": page}, use_default_language=True)

# --- 搜索电视剧，返回完整响应 ---
def search_tv_tmdb(api_key: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    搜索电视剧，返回完整响应（包含 results 列表）。
    用于映射管理中"搜代表剧集"功能。
    """
    query = params.get('query')
    if not query:
        return None
    # 复用现有的 search_media_for_discover，它返回完整的 dict
    return search_media_for_discover(query=query, api_key=api_key, item_type='tv')


# --- 代理设置接口 ---
def set_proxy(proxy_url: str):
    """
    [兼容性接口] main.py 和 dependencies.py 会调用此方法。
    应用运行时代理配置，不改写 settings.json，避免启动期读空配置时覆盖用户设置。
    """
    try:
        cache = getattr(config_manager, "_settings_cache", None)
        if isinstance(cache, dict):
            cache["proxy_url"] = proxy_url or ""
            config_manager._settings_cache_time = time.time()
        logger.info(f"[TMDb] 代理配置已更新: {proxy_url if proxy_url else '关闭'}")
    except Exception as e:
        logger.error(f"TMDb 代理配置更新失败: {e}")
