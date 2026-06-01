from __future__ import annotations
import json
import os
import shutil
from datetime import datetime
from core.logger import logger

RULES_FILE = "config/media_organize_category_rules.json"

DEFAULT_SUB_CLASSIFY = {
    "movie": {"enabled": True, "levels": ["year_decade"]},
    "tv": {"enabled": True, "levels": ["year_decade"]},
    "sync_emby_library": True,
    "emby_server_idx": 0,
    "emby_library_level": "level3",
}


def _apply_default_sub_classify(rules: dict) -> dict:
    if not isinstance(rules, dict):
        rules = {}
    sub_classify = dict(DEFAULT_SUB_CLASSIFY)
    existing = rules.get("sub_classify")
    if isinstance(existing, dict):
        sub_classify.update(existing)
        for media_type in ("movie", "tv"):
            media_defaults = DEFAULT_SUB_CLASSIFY[media_type]
            media_existing = existing.get(media_type)
            if isinstance(media_existing, dict):
                media_data = dict(media_defaults)
                media_data.update(media_existing)
                sub_classify[media_type] = media_data
    rules["sub_classify"] = sub_classify
    rules.setdefault("movie", [])
    rules.setdefault("tv", [])
    return rules


def _load_default_rules() -> dict:
    base = os.path.dirname(__file__)
    for path in [
        os.path.join(base, "../../defaults/config/media_organize_category_rules.json"),
        os.path.join(base, "../../config/media_organize_category_rules.json"),
    ]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return _apply_default_sub_classify(json.load(f))
        except Exception:
            continue
    logger.warning("[CategoryMatcher] 默认规则文件未找到，使用空规则")
    return _apply_default_sub_classify({"movie": [], "tv": []})

DEFAULT_RULES: dict = _load_default_rules()


def _migrate_production_countries_to_origin_country(value) -> tuple[object, int]:
    """Migrate legacy category conditions without changing user rule shape."""
    changed = 0
    if isinstance(value, dict):
        migrated = {}
        for key, item in value.items():
            if key == "field" and item == "production_countries":
                migrated[key] = "origin_country"
                changed += 1
                continue
            migrated_item, item_changed = _migrate_production_countries_to_origin_country(item)
            migrated[key] = migrated_item
            changed += item_changed
        return migrated, changed
    if isinstance(value, list):
        migrated_list = []
        for item in value:
            migrated_item, item_changed = _migrate_production_countries_to_origin_country(item)
            migrated_list.append(migrated_item)
            changed += item_changed
        return migrated_list, changed
    return value, 0


def _backup_and_save_migrated_rules(path: str, rules: dict, changed_count: int) -> None:
    if changed_count <= 0:
        return
    try:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = f"{path}.bak-production-countries-{timestamp}"
        shutil.copy2(path, backup_path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
        logger.info(
            f"[CategoryMatcher] 已迁移分类规则字段 production_countries -> origin_country: "
            f"{changed_count} 处，备份: {backup_path}"
        )
    except Exception as e:
        logger.warning(f"[CategoryMatcher] 分类规则字段迁移失败，继续使用内存迁移结果: {e}")


def load_rules() -> dict:
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            migrated, changed_count = _migrate_production_countries_to_origin_country(loaded)
            if changed_count:
                _backup_and_save_migrated_rules(RULES_FILE, migrated, changed_count)
            return _apply_default_sub_classify(migrated)
        except Exception as e:
            logger.warning(f"[CategoryMatcher] 读取规则文件失败，使用默认: {e}")
    return _apply_default_sub_classify(DEFAULT_RULES)


def save_rules(rules: dict):
    os.makedirs(os.path.dirname(RULES_FILE), exist_ok=True)
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)


class CategoryMatcher:
    def __init__(self, rules: dict | None = None):
        self._rules = rules if rules is not None else load_rules()

    def match(self, metadata: dict, media_type: str) -> str:
        try:
            rule_list = self._rules.get(media_type, [])
            if not isinstance(rule_list, list):
                return "其他"
            for rule in rule_list:
                try:
                    if self._match_rule(rule, metadata):
                        path = rule.get("path", "其他")
                        return self._apply_sub_classify(path, metadata, media_type)
                except Exception as e:
                    logger.debug(f"[CategoryMatcher] 规则匹配异常跳过: {e}")
        except Exception as e:
            logger.warning(f"[CategoryMatcher] match 异常: {e}")
        return "其他"

    # ISO 3166-1 → 中文国家名（常用）
    _COUNTRY_ZH = {
        "CN": "中国", "HK": "香港", "TW": "台湾", "MO": "澳门",
        "US": "美国", "GB": "英国", "FR": "法国", "DE": "德国",
        "JP": "日本", "KR": "韩国", "IT": "意大利", "ES": "西班牙",
        "RU": "俄罗斯", "CA": "加拿大", "AU": "澳大利亚", "IN": "印度",
        "TH": "泰国", "VN": "越南", "MY": "马来西亚", "PH": "菲律宾",
        "SG": "新加坡", "ID": "印度尼西亚", "MX": "墨西哥", "BR": "巴西",
        "PT": "葡萄牙", "NL": "荷兰", "SE": "瑞典", "PL": "波兰",
        "TR": "土耳其", "AR": "阿根廷", "ZA": "南非", "NZ": "新西兰",
    }

    def _apply_sub_classify(self, base_path: str, metadata: dict, media_type: str) -> str:
        try:
            sc = (self._rules.get("sub_classify") or {}).get(media_type, {})
            if not sc.get("enabled"):
                return base_path
            for var in (sc.get("levels") or []):
                val = self._resolve_var(var, metadata, media_type)
                if val:
                    base_path = base_path + "/" + val
        except Exception as e:
            logger.debug(f"[CategoryMatcher] sub_classify 异常: {e}")
        return base_path

    def _resolve_var(self, var: str, metadata: dict, media_type: str) -> str:
        try:
            src = metadata.get("series_details") or metadata
            if var == "rating_tier":
                v = src.get("vote_average")
                if v is None:
                    return ""
                v = float(v)
                if v >= 9:   return "9分以上"
                if v >= 8:   return "8-9分"
                if v >= 7:   return "7-8分"
                return "7分以下"
            if var == "year_decade":
                date = src.get("release_date") or src.get("first_air_date") or ""
                year = int(str(date)[:4]) if len(str(date)) >= 4 and str(date)[:4].isdigit() else None
                if year is None:
                    return ""
                return f"{(year // 10) * 10}s"
            if var == "year":
                date = src.get("release_date") or src.get("first_air_date") or ""
                year = str(date)[:4] if len(str(date)) >= 4 and str(date)[:4].isdigit() else ""
                return year
            if var == "origin_country":
                codes = src.get("origin_country") or []
                if not codes:
                    pc = src.get("production_countries") or []
                    codes = [c.get("iso_3166_1", "") if isinstance(c, dict) else c for c in pc]
                code = codes[0] if codes else ""
                return self._COUNTRY_ZH.get(str(code).upper(), str(code))
            if var == "genre_label":
                genres = src.get("genres") or []
                if genres:
                    g = genres[0]
                    return g.get("name", "") if isinstance(g, dict) else str(g)
        except Exception as e:
            logger.debug(f"[CategoryMatcher] _resolve_var({var}) 异常: {e}")
        return ""

    def _match_rule(self, rule: dict, metadata: dict) -> bool:
        conditions = rule.get("conditions", [])
        if not isinstance(conditions, list):
            return False
        and_conds = [c for c in conditions if isinstance(c, dict) and c.get("logic", "AND") == "AND"]
        or_conds  = [c for c in conditions if isinstance(c, dict) and c.get("logic") == "OR"]

        for cond in and_conds:
            if not self._match_condition(cond, metadata):
                return False

        if or_conds:
            if not any(self._match_condition(c, metadata) for c in or_conds):
                return False

        return True

    def _match_condition(self, cond: dict, metadata: dict) -> bool:
        field = cond.get("field", "")
        value = cond.get("value", "")
        if not field or value is None:
            return False
        tokens = [t.strip() for t in str(value).split(",") if t.strip()]
        if not tokens:
            return False

        if field == "genre_ids":
            return self._match_genre_ids(tokens, metadata)
        if field == "keywords":
            return self._match_keywords(tokens, metadata) or self._match_title_keywords(tokens, metadata)
        if field == "include_keywords":
            return self._match_keywords(tokens, metadata)
        if field == "series_keywords":
            return self._match_series_keywords(tokens, metadata)
        return self._match_generic(field, tokens, metadata)

    def _match_genre_ids(self, tokens: list[str], metadata: dict) -> bool:
        raw = metadata.get("genres") or metadata.get("genre_ids")
        if raw is None:
            raw = (metadata.get("series_details") or {}).get("genres") or (metadata.get("series_details") or {}).get("genre_ids")
        if not isinstance(raw, list):
            return False
        ids: set[int] = set()
        for item in raw:
            if isinstance(item, dict):
                v = item.get("id")
            else:
                v = item
            try:
                ids.add(int(v))
            except (TypeError, ValueError):
                pass

        must_include = [int(t[1:]) for t in tokens if t.startswith("+") and t[1:].isdigit()]
        must_exclude = [int(t[1:]) for t in tokens if t.startswith("-") and t[1:].isdigit()]
        plain        = [int(t) for t in tokens if not t.startswith(("+", "-")) and t.isdigit()]

        for mid in must_include:
            if mid not in ids:
                return False
        for mid in must_exclude:
            if mid in ids:
                return False
        if plain and not any(p in ids for p in plain):
            return False
        return True

    def _match_keywords(self, tokens: list[str], metadata: dict) -> bool:
        kw_data = metadata.get("keywords")
        if kw_data is None:
            kw_data = (metadata.get("series_details") or {}).get("keywords", {})
        kw_list: list[str] = []
        if isinstance(kw_data, dict):
            for key in ("keywords", "results"):
                for item in kw_data.get(key, []):
                    if isinstance(item, dict):
                        kw_list.append(str(item.get("name", "")).lower())
                    else:
                        kw_list.append(str(item).lower())
        elif isinstance(kw_data, list):
            for item in kw_data:
                kw_list.append(str(item.get("name", "") if isinstance(item, dict) else item).lower())

        for t in tokens:
            if t.lower() in kw_list:
                return True
        return False

    def _match_title_keywords(self, tokens: list[str], metadata: dict) -> bool:
        title = (
            metadata.get("title") or metadata.get("name")
            or (metadata.get("series_details") or {}).get("title")
            or (metadata.get("series_details") or {}).get("name", "")
            or ""
        ).lower()
        return any(t.lower() in title for t in tokens)

    def _match_series_keywords(self, tokens: list[str], metadata: dict) -> bool:
        return self._match_title_keywords(tokens, metadata)

    def _match_generic(self, field: str, tokens: list[str], metadata: dict) -> bool:
        raw = metadata.get(field)
        if raw is None:
            # 兼容 series_details 嵌套
            raw = (metadata.get("series_details") or {}).get(field)
        if raw is None:
            return False

        values: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    for key in ("iso_3166_1", "iso_639_1", "name", "value"):
                        if key in item:
                            values.append(str(item[key]).lower())
                            break
                else:
                    values.append(str(item).lower())
        else:
            values = [str(raw).lower()]

        return any(t.lower() in values for t in tokens)
