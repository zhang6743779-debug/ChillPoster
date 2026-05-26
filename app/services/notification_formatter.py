# app/services/notification_formatter.py
import copy
from jinja2 import Template

# 模板版本号：每次修改 DEFAULT_TEMPLATES 内容时递增，触发强制覆盖旧配置
TEMPLATE_VERSION = 22

# 通知模板默认值
DEFAULT_TEMPLATES = {
    "media_added": {
        "title": "{{ media_type }} 入库 ✅ 《{{ title }}》{% if year %}({{ year }}){% endif %}",
        "text": "⭐️评分：{{ rating or '暂无' }}\n🎬类型：{{ media_type }}{% if genres %} · {{ genres }}{% endif %}"
                "{% if status %}\n📡连载: {{ status }}{% endif %}"
                "{% if premiere_date %}\n📅首播：{{ premiere_date }}{% endif %}"
                "{% if tagline %}\n💬标语：{{ tagline }}{% endif %}"
                "{% if item_count %}\n📦集数:共 {{ item_count }} 集{% endif %}"
                "\n📁媒体库：{{ library_name }}{% if server_name %} · {{ server_name }}{% endif %}"
                "\n🕐入库时间：{{ now }}"
                "{% if tmdb_url %}\n🔗[TMDB]({{ tmdb_url }}){% endif %}"
                "\n\n📝简介：{{ overview or '暂无简介' }}"
    },
    "organize_complete": {
        "title": "整理完成 ✅ 《{{ title }}》{% if year %}({{ year }}){% endif %}{% if season_episode %} {{ season_episode }}{% endif %}",
        "text": "⭐️评分：{{ rating or '暂无' }}\n🎬类型：{{ media_type }}{% if genres %} · {{ genres }}{% endif %}"
                "{% if quality %}\n💎画质：{{ quality }}{% endif %}"
                "{% if video %}\n🎞️视频：{{ video }}{% endif %}"
                "{% if audio %}\n🎵音频：{{ audio }}{% endif %}"
                "{% if library_location %}\n📁库位：{{ library_location }}{% endif %}"
                "{% if episode_count %}\n📖数量：{{ episode_count }} 集{% endif %}"
                "{% if episode_ranges %}\n📚集数：{{ episode_ranges }}{% endif %}"
                "{% if file_size %}\n⚖️大小：{{ file_size }}{% endif %}"
                "{% if tmdb_id %}\n🎬tmdbid：{{ tmdb_id }}{% endif %}"
                "{% if release_group %}\n👨‍🎨制作组：{{ release_group }}{% endif %}"
                "{% if elapsed %}\n⏱️整理耗时：{{ elapsed }}{% endif %}"
                "\n🕐完成时间：{{ now }}"
                "{% if overview %}\n\n📝简介：{{ overview }}{% endif %}"
    },
    "wash_result": {
        "title": "洗版{{ status_text }} {{ status_emoji }} 《{{ title }}》{% if year %}({{ year }}){% endif %}{% if season_episode %} {{ season_episode }}{% endif %}",
        "text": "🎬类型：{{ media_type }}{% if genres %} · {{ genres }}{% endif %}"
                "{% if library_location %}\n📁库位：{{ library_location }}{% endif %}"
                "\n📌结果：{{ decision_text }}"
                "\n📝原因：{{ reason_text }}"
                "{% if old_summary %}\n\n📦已入库旧资源：{{ old_summary }}{% endif %}"
                "{% if new_summary %}\n✨本次整理资源：{{ new_summary }}{% endif %}"
                "\n\n🕐完成时间：{{ now }}"
    },
    "playback": {
        "title": "🎬 正在播放《{{ title }}》{% if year %}({{ year }}){% endif %}",
        "text": "⭐️评分：{{ rating or '暂无' }}\n🎬类型：{{ media_type }}{% if genres %} · {{ genres }}{% endif %}"
                "{% if tagline %}\n💬标语：{{ tagline }}{% endif %}"
                "\n\n👤用户：{{ user_name or '未知' }}\n🖥️服务器：{{ emby_name }}\n📱客户端：{{ client_info or '未知' }}"
                "\n🕐时间：{{ now }}"
                "\n\n📝简介：{{ overview or '暂无简介' }}"
    },
    "task_complete": {
        "title": "{{ status_emoji }} {{ task_name }}",
        "text": "{% if task_category == 'media_organize' %}"
                "{{ status_emoji }} 状态：{{ status_text }}"
                "\n🧩 类型：媒体整理任务"
                "{% if elapsed %}\n⏱️ 总耗时：{{ elapsed }}{% endif %}"
                "{% if total_count %}\n📦 扫描视频：{{ total_count }}{% endif %}"
                "{% if organize_size %}\n⚖️ 整理体积：{{ organize_size }}{% endif %}"
                "{% if success_count %}\n✅ 整理成功：{{ success_count }}{% endif %}"
                "{% if failed %}\n❌ 整理失败：{{ failed }}{% endif %}"
                "{% if skipped %}\n⏭️ 跳过处理：{{ skipped }}{% endif %}"
                "{% if generated %}\n🎞️ 新生成STRM：{{ generated }}{% endif %}"
                "{% if detail %}\n\n📝 {{ detail }}{% endif %}"
                "\n🕒 完成时间：{{ now }}"
                "\n\n— ChillPoster"
                "{% else %}"
                "{% if status_text %}{{ status_emoji }} 状态：{{ status_text }}{% endif %}"
                "{% if task_category == 'signin' %}\n🔔 类型：115 自动签到{% elif task_category == 'poster' %}\n🎨 类型：海报生成任务{% elif task_category %}\n🧩 类型：{{ task_category }}{% endif %}"
                "{% if trigger %}\n🚀 触发：{{ trigger }}{% endif %}"
                "{% if elapsed %}\n⏱️ 耗时：{{ elapsed }}{% endif %}"
                "{% if summary %}\n📌 概览：{{ summary }}{% endif %}"
                "{% if task_category != 'signin' %}"
                "{% if total_count %}\n👥 总数：{{ total_count }}{% endif %}"
                "{% if success_count %}\n✅ 成功：{{ success_count }}{% endif %}"
                "{% if already_count %}\n🔄 已签：{{ already_count }}{% endif %}"
                "{% if failed %}\n❌ 失败：{{ failed }}{% endif %}"
                "{% endif %}"
                "{% if is_strm_task %}"
                "{% if scanned %}\n📦 扫描：{{ scanned }}{% endif %}"
                "{% if scanned_dirs %}\n📁 文件夹：{{ scanned_dirs }}{% endif %}"
                "\n🎞️ STRM：生成 {{ strm_generated }} / 已存在 {{ strm_skipped }}"
                "\n💬 字幕：下载 {{ subtitle_downloaded }} / 已存在 {{ subtitle_skipped }} / 失败 {{ subtitle_download_failed }}"
                "\n🧾 附属：下载 {{ aux_downloaded }} / 已存在 {{ aux_skipped }} / 失败 {{ aux_download_failed }}"
                "{% if out_of_scope_skipped %}\n🚫 不在同步范围：{{ out_of_scope_skipped }}{% endif %}"
                "{% if other_skipped %}\n⏭️ 其他跳过：{{ other_skipped }}{% endif %}"
                "\n🧩 TMDb补齐：{{ tmdb_generated }}"
                "\n⏭️ TMDb跳过：{{ tmdb_skipped }}"
                "\n❌ TMDb失败：{{ tmdb_failed }}"
                "\n⏭️ 同步文件跳过合计：{{ skipped }}"
                "{% if deleted %}\n🧹 删除：{{ deleted }}{% endif %}"
                "\n🔁 重试：成功 {{ retry_success }} / 失败 {{ retry_failed }}"
                "{% else %}"
                "{% if scanned %}\n📦 扫描：{{ scanned }}{% endif %}"
                "{% if scanned_dirs %}\n📁 文件夹：{{ scanned_dirs }}{% endif %}"
                "{% if generated %}\n🎞️ 生成：{{ generated }}{% endif %}"
                "{% if downloaded %}\n⬇️ 下载：{{ downloaded }}{% endif %}"
                "{% if download_failed %}\n❌ 下载失败：{{ download_failed }}{% endif %}"
                "{% if skipped %}\n⏭️ 跳过：{{ skipped }}{% endif %}"
                "{% if deleted %}\n🧹 删除：{{ deleted }}{% endif %}"
                "{% endif %}"
                "{% if posters_count %}\n🖼️ 处理数量：{{ posters_count }}{% endif %}"
                "{% if detail %}\n📝 详情：{{ detail }}{% endif %}"
                "{% if accounts_text and task_category != 'signin' %}\n\n{{ accounts_text }}{% endif %}"
                "\n🕒 时间：{{ now }}"
                "\n\n— ChillPoster"
                "{% endif %}"
    }
}


def render_template(template_str: str, context: dict) -> str:
    """渲染 Jinja2 模板，失败时返回原始字符串"""
    if not template_str:
        return ""
    try:
        tpl = Template(template_str)
        return tpl.render(**context)
    except Exception as e:
        from core.logger import logger
        logger.error(f"[Formatter] 模板渲染失败: {e} | 模板: {template_str[:80]}")
        return template_str


def get_default_templates() -> dict:
    """返回默认模板的深拷贝"""
    return copy.deepcopy(DEFAULT_TEMPLATES)


def merge_templates(user_templates: dict) -> dict:
    """用用户自定义模板覆盖默认模板，缺失项保留默认。
    若 JSON 里存的版本号低于 TEMPLATE_VERSION，强制使用最新默认模板。"""
    defaults = get_default_templates()
    defaults["_version"] = TEMPLATE_VERSION
    if not user_templates:
        return defaults
    # 版本号过旧 → 强制覆盖，忽略 JSON 里的旧模板
    # 注意：前端提交的 templates 不带 _version，视为当前版本（用户主动编辑优先）
    saved_version = user_templates.get("_version", TEMPLATE_VERSION)
    if saved_version < TEMPLATE_VERSION:
        return defaults
    for notify_type, templates in user_templates.items():
        if notify_type.startswith("_"):
            continue
        if notify_type in defaults and isinstance(templates, dict):
            for key in ["title", "text"]:
                if key in templates and templates[key]:
                    defaults[notify_type][key] = templates[key]
    return defaults
