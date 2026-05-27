import re
import traceback
from typing import Optional

from Pinyin2Hanzi import is_pinyin

from core.logger import logger
from core.meta.customization import CustomizationMatcher
from core.meta.metabase import MetaBase
from core.meta.releasegroup import ReleaseGroupsMatcher
from core.meta.string import StringUtils
from core.meta.tokens import Tokens
from core.meta.types import MediaType
from core.meta.streamingplatform import StreamingPlatforms


# 媒体文件扩展名和字幕/音频扩展名（从 settings 搬过来）
RMT_MEDIAEXT = ['.mp4', '.mkv', '.ts', '.avi', '.wmv', '.flv', '.mov',
                '.rmvb', '.iso', '.m2ts', '.mpg', '.mpeg', '.m4v', '.3gp',
                '.asf', '.dat', '.vob', '.f4v']
RMT_SUBEXT = ['.srt', '.ass', '.ssa', '.sub']
RMT_AUDIOEXT = ['.flac', '.ape', '.wav', '.mp3', '.aac', '.ogg']


class MetaVideo(MetaBase):
    """
    识别电影、电视剧
    """
    # 控制标位区
    _stop_name_flag = False
    _stop_cnname_flag = False
    _last_token = ""
    _last_token_type = ""
    _continue_flag = True
    _unknown_name_str = ""
    _source = ""
    _effect = []
    # 正则式区
    _season_re = r"S(\d{3})|^S(\d{1,3})$|S(\d{1,3})E"
    _episode_re = r"EP?(\d{2,4})$|^EP?(\d{1,4})$|^S\d{1,2}EP?(\d{1,4})$|S\d{2}EP?(\d{2,4})"
    _part_re = r"(^PART[0-9ABI]{0,2}$|^CD[0-9]{0,2}$|^DVD[0-9]{0,2}$|^DISK[0-9]{0,2}$|^DISC[0-9]{0,2}$)"
    _roman_numerals = r"^(?=[MDCLXVI])M*(C[MD]|D?C{0,3})(X[CL]|L?X{0,3})(I[XV]|V?I{0,3})$"
    _source_re = r"^BLURAY$|^HDTV$|^UHDTV$|^HDDVD$|^WEBRIP$|^DVDRIP$|^BDRIP$|^BLU$|^WEB$|^BD$|^HDRip$|^REMUX$|^UHD$"
    _effect_re = r"^SDR$|^HDR\d*$|^DOLBY$|^DOVI$|^DV$|^3D$|^REPACK$|^HLG$|^HDR10(\+|Plus)$|^EDR$|^HQ$"
    _resources_type_re = r"%s|%s" % (_source_re, _effect_re)
    _name_no_begin_re = r"^[\[【].+?[\]】]"
    _name_no_chinese_re = r".*版|.*字幕"
    _name_se_words = ['共', '第', '季', '集', '话', '話', '期']
    _name_movie_words = ['剧场版', '劇場版', '电影版', '電影版']
    _name_nostring_re = (r"^PTS|^JADE|^AOD|^CHC|^[A-Z]{1,4}TV[\-0-9UVHDK]*"
                         r"|HBO$|\s+HBO|\d{1,2}th|\d{1,2}bit|NETFLIX|AMAZON|IMAX|^3D|\s+3D|^BBC\s+|\s+BBC|BBC$|DISNEY\+?|XXX|\s+DC$"
                         r"|[第\s共]+[0-9一二三四五六七八九十\-\s]+季"
                         r"|[第\s共]+[0-9一二三四五六七八九十百零\-\s]+[集话話]"
                         r"|连载|日剧|美剧|电视剧|动画片|动漫|欧美|西德|日韩|超高清|高清|无水印|下载|蓝光|翡翠台|梦幻天堂·龙网|★?\d*月?新番"
                         r"|最终季|合集|[多中国英葡法俄日韩德意西印泰台港粤双文语简繁体特效内封官译外挂]+字幕|版本|出品|台版|港版|\w+字幕组|\w+字幕社"
                         r"|未删减版|UNCUT$|UNRATE$|WITH EXTRAS$|RERIP$|SUBBED$|PROPER$|REPACK$|SEASON$|EPISODE$|Complete$|Extended$|Extended Version$"
                         r"|S\d{2}\s*-\s*S\d{2}|S\d{2}|\s+S\d{1,2}|EP?\d{2,4}\s*-\s*EP?\d{2,4}|EP?\d{2,4}|\s+EP?\d{1,4}"
                         r"|CD[\s.]*[1-9]|DVD[\s.]*[1-9]|DISK[\s.]*[1-9]|DISC[\s.]*[1-9]"
                         r"|[248]K|\d{3,4}[PIX]+"
                         r"|CD[\s.]*[1-9]|DVD[\s.]*[1-9]|DISK[\s.]*[1-9]|DISC[\s.]*[1-9]|\s+GB")
    _resources_pix_re = r"^[SBUHD]*(\d{3,4}[PI]+)|\d{3,4}X(\d{3,4})"
    _resources_pix_re2 = r"(^[248]+K)"
    _video_encode_re = r"^(H26[45])$|^(x26[45])$|^AVC$|^HEVC$|^VC\d?$|^MPEG\d?$|^MPEG[12]VIDEO$|^MPG[12]$|^Xvid$|^DivX$|^AV1$|^HDR\d*$|^AVS(\+|[23])$"
    _audio_encode_re = r"^DTS\d?$|^DTSHD$|^DTSHDMA$|^Atmos$|^TrueHD\d?$|^AC3$|^\dAudios?$|^DDP\d?$|^DD\+\d?$|^DD\d?$|^LPCM\d?$|^AAC\d?$|^FLAC\d?$|^HD\d?$|^MA\d?$|^HR\d?$|^Opus\d?$|^Vorbis\d?$|^AV[3S]A$"
    _fps_re = r"(\d{2,3})(?=FPS)"

    def __init__(self, title: str, subtitle: str = None, isfile: bool = False):
        super().__init__(title, subtitle, isfile)
        if not title:
            return
        original_title = title
        self._source = ""
        self._effect = []
        self._index = 0
        # 判断是否纯数字命名
        if isfile \
                and title.isdigit() \
                and len(title) < 5:
            self.begin_episode = int(title)
            self.type = MediaType.TV
            return
        # 全名为Season xx 及 Sxx 直接返回
        season_full_res = re.search(r"^(?:Season\s+|S)(\d{1,3})$", title, re.IGNORECASE)
        if season_full_res:
            self.type = MediaType.TV
            season = season_full_res.group(1)
            if season:
                self.begin_season = int(season)
                self.total_season = 1
            return
        # 去掉名称中第1个[]的内容
        _first_bracket = re.match(r'^[\[【](.+?)[\]】]', title)
        if _first_bracket:
            _bracket_content = _first_bracket.group(1)
            # 如果第一个括号内为点分隔的英文发布名格式（含年份+资源类型），保留内容去掉括号
            if re.search(r'[A-Za-z]+\..+(?:19|20)\d{2}', _bracket_content) \
                    and re.search(r'(?:2160|1080|720|480)[PIpi]|4K|UHD|Blu[\-.]?ray|REMUX|WEB[\-.]?DL|HDTV',
                                  _bracket_content, re.IGNORECASE):
                title = _bracket_content + title[_first_bracket.end():]
            else:
                title = title[_first_bracket.end():]
        # 把xxxx-xxxx年份换成前一个年份，常出现在季集上
        title = re.sub(r'([\s.]+)(\d{4})-(\d{4})', r'\1\2', title)
        # 把大小去掉
        title = re.sub(r'[0-9.]+\s*[MGT]i?B(?![A-Z]+)', "", title, flags=re.IGNORECASE)
        # 把年月日去掉
        title = re.sub(r'\d{4}[\s._-]\d{1,2}[\s._-]\d{1,2}', "", title)
        # 拆分tokens
        tokens = Tokens(title)
        # 实例化StreamingPlatforms对象
        streaming_platforms = StreamingPlatforms()
        # 解析名称、年份、季、集、资源类型、分辨率等
        token = tokens.get_next()
        while token:
            self._index += 1
            # Part
            self.__init_part(token, tokens)
            # 标题
            if self._continue_flag:
                self.__init_name(token)
            # 年份
            if self._continue_flag:
                self.__init_year(token)
            # 分辨率
            if self._continue_flag:
                self.__init_resource_pix(token)
            # 季
            if self._continue_flag:
                self.__init_season(token)
            # 集
            if self._continue_flag:
                self.__init_episode(token)
            # 资源类型
            if self._continue_flag:
                self.__init_resource_type(token)
            # 流媒体平台
            if self._continue_flag:
                self.__init_web_source(token, tokens, streaming_platforms)
            # 视频编码
            if self._continue_flag:
                self.__init_video_encode(token)
            # 音频编码
            if self._continue_flag:
                self.__init_audio_encode(token)
            # 帧率
            if self._continue_flag:
                self.__init_fps(token)
            # 取下一个
            token = tokens.get_next()
            self._continue_flag = True
        # 合成质量：拆分 REMUX（来源处理）和 HDR/DV（视频特效）
        if self._effect:
            self._effect.reverse()
            remux_tags = [e for e in self._effect if e.upper() == "REMUX"]
            video_tags = [e for e in self._effect if e.upper() != "REMUX"]
            if remux_tags:
                self.resource_effect = ".".join(remux_tags)
            if video_tags:
                self.video_effect = ".".join(video_tags)
        if self._source:
            self.resource_type = self._source.strip()
            # UHD BluRay 场景：UHD 拆到 web_source，BluRay 留在 resource_type
            if "UHD" in self.resource_type and "BluRay" in self.resource_type:
                if not self.web_source:
                    self.web_source = "UHD"
                self.resource_type = self.resource_type.replace("UHD", "").strip()
        # 提取原盘DIY
        if self.resource_type and "BluRay" in self.resource_type:
            if (self.subtitle and re.findall(r'D[Ii]Y', self.subtitle)) \
                    or re.findall(r'-D[Ii]Y@', original_title):
                self.resource_type = f"{self.resource_type} DIY"
        # 解析副标题，只要季和集
        self.init_subtitle(self.org_string)
        if not self._subtitle_flag and self.subtitle:
            self.init_subtitle(self.subtitle)
        # 去掉名字中不需要的干扰字符，过短的纯数字不要
        self.cn_name = self.__fix_name(self.cn_name)
        self.en_name = StringUtils.str_title(self.__fix_name(self.en_name))
        # 处理part
        if self.part and self.part.upper() == "PART":
            self.part = None
        # 没有中文标题时，尝试从描述中获取中文名
        if not self.cn_name and self.en_name and self.subtitle:
            if self.__is_pinyin(self.en_name):
                cn_name = self.__get_title_from_description(self.subtitle)
                if cn_name and len(cn_name) == len(self.en_name.split()):
                    self.cn_name = cn_name
        # 制作组/字幕组
        self.resource_team = ReleaseGroupsMatcher().match(title=original_title) or None
        # 自定义占位符
        self.customization = CustomizationMatcher().match(title=original_title) or None

    @staticmethod
    def __get_title_from_description(description: str) -> Optional[str]:
        if not description:
            return None
        titles = re.split(r'[\s/|]+', description)
        if StringUtils.is_chinese(titles[0]):
            return titles[0]
        return None

    @staticmethod
    def __is_pinyin(name_str: Optional[str]) -> bool:
        if not name_str:
            return False
        for n in name_str.lower().split():
            if not is_pinyin(n):
                return False
        return True

    def __fix_name(self, name: Optional[str]):
        if not name:
            return name
        name = re.sub(r'%s' % self._name_nostring_re, '', name,
                      flags=re.IGNORECASE).strip()
        name = re.sub(r'\s+', ' ', name)
        if name.isdecimal() \
                and int(name) < 1800 \
                and not self.year \
                and not self.begin_season \
                and not self.resource_pix \
                and not self.resource_type \
                and not self.audio_encode \
                and not self.video_encode:
            if self.begin_episode is None:
                self.begin_episode = int(name)
                name = None
            elif self.is_in_episode(int(name)) and not self.begin_season:
                name = None
        return name

    def __init_name(self, token: Optional[str]):
        if not token:
            return
        if self._unknown_name_str:
            if not self.cn_name:
                if not self.en_name:
                    self.en_name = self._unknown_name_str
                elif self._unknown_name_str != self.year:
                    self.en_name = "%s %s" % (self.en_name, self._unknown_name_str)
                self._last_token_type = "enname"
            self._unknown_name_str = ""
        if self._stop_name_flag:
            return
        if token.upper() == "AKA":
            self._continue_flag = False
            self._stop_name_flag = True
            return
        if token in self._name_se_words:
            self._last_token_type = 'name_se_words'
            return
        if StringUtils.is_chinese(token):
            self._last_token_type = "cnname"
            if not self.cn_name:
                self.cn_name = token
            elif not self._stop_cnname_flag:
                if re.search("|".join(self._name_movie_words), token, flags=re.IGNORECASE) \
                        or (not re.search("%s" % self._name_no_chinese_re, token, flags=re.IGNORECASE)
                            and not any(w in token for w in self._name_se_words)):
                    self.cn_name = "%s %s" % (self.cn_name, token)
                self._stop_cnname_flag = True
        else:
            is_roman_digit = re.search(self._roman_numerals, token)
            if token.isdigit() or is_roman_digit:
                if self._last_token_type == 'name_se_words':
                    return
                if self.name:
                    if token.startswith('0'):
                        return
                    if token.isdigit():
                        try:
                            int(token)
                        except ValueError:
                            return
                    if not is_roman_digit \
                            and self._last_token_type == "cnname" \
                            and int(token) < 1900:
                        return
                    if (token.isdigit() and len(token) < 4) or is_roman_digit:
                        if self._last_token_type == "cnname":
                            self.cn_name = "%s %s" % (self.cn_name, token)
                        elif self._last_token_type == "enname":
                            self.en_name = "%s %s" % (self.en_name, token)
                        self._continue_flag = False
                    elif token.isdigit() and len(token) == 4:
                        if not self._unknown_name_str:
                            self._unknown_name_str = token
                else:
                    if not self._unknown_name_str:
                        self._unknown_name_str = token
            elif re.search(r"%s" % self._season_re, token, re.IGNORECASE):
                if self.en_name and re.search(r"SEASON$", self.en_name, re.IGNORECASE):
                    self.en_name += ' '
                self._stop_name_flag = True
                return
            elif re.search(r"%s" % self._episode_re, token, re.IGNORECASE) \
                    or re.search(r"(%s)" % self._resources_type_re, token, re.IGNORECASE) \
                    or re.search(r"%s" % self._resources_pix_re, token, re.IGNORECASE):
                self._stop_name_flag = True
                return
            else:
                media_exts = RMT_MEDIAEXT + RMT_SUBEXT + RMT_AUDIOEXT
                if ".%s".lower() % token in media_exts:
                    return
                if self.en_name:
                    self.en_name = "%s %s" % (self.en_name, token)
                else:
                    self.en_name = token
                self._last_token_type = "enname"

    def __init_part(self, token: str, tokens: Tokens):
        if not self.name:
            return
        if not self.year \
                and not self.begin_season \
                and not self.begin_episode \
                and not self.resource_pix \
                and not self.resource_type:
            return
        re_res = re.search(r"%s" % self._part_re, token, re.IGNORECASE)
        if re_res:
            if not self.part:
                self.part = re_res.group(1)
            nextv = tokens.cur()
            if nextv \
                    and ((nextv.isdigit() and (len(nextv) == 1 or len(nextv) == 2 and nextv.startswith('0')))
                         or nextv.upper() in ['A', 'B', 'C', 'I', 'II', 'III']):
                self.part = "%s%s" % (self.part, nextv)
                tokens.get_next()
            self._last_token_type = "part"
            self._continue_flag = False

    def __init_year(self, token: str):
        if not self.name:
            return
        if not token.isdigit():
            return
        if len(token) != 4:
            return
        if not 1900 < int(token) < 2050:
            return
        if self.year:
            if self.en_name:
                self.en_name = "%s %s" % (self.en_name.strip(), self.year)
            elif self.cn_name:
                self.cn_name = "%s %s" % (self.cn_name, self.year)
        elif self.en_name and re.search(r"SEASON$", self.en_name, re.IGNORECASE):
            self.en_name += ' '
        self.year = token
        self._last_token_type = "year"
        self._continue_flag = False
        self._stop_name_flag = True

    def __init_resource_pix(self, token: str):
        if not self.name:
            return
        re_res = re.findall(r"%s" % self._resources_pix_re, token, re.IGNORECASE)
        if re_res:
            self._last_token_type = "pix"
            self._continue_flag = False
            self._stop_name_flag = True
            resource_pix = None
            for pixs in re_res:
                if isinstance(pixs, tuple):
                    pix_t = None
                    for pix_i in pixs:
                        if pix_i:
                            pix_t = pix_i
                            break
                    if pix_t:
                        resource_pix = pix_t
                else:
                    resource_pix = pixs
                if resource_pix and not self.resource_pix:
                    self.resource_pix = resource_pix.lower()
                    break
            if self.resource_pix \
                    and self.resource_pix.isdigit() \
                    and self.resource_pix[-1] not in 'kpi':
                self.resource_pix = "%sp" % self.resource_pix
        else:
            re_res = re.search(r"%s" % self._resources_pix_re2, token, re.IGNORECASE)
            if re_res:
                self._last_token_type = "pix"
                self._continue_flag = False
                self._stop_name_flag = True
                if not self.resource_pix:
                    self.resource_pix = re_res.group(1).lower()

    def __init_season(self, token: str):
        re_res = re.findall(r"%s" % self._season_re, token, re.IGNORECASE)
        if re_res:
            self._last_token_type = "season"
            self.type = MediaType.TV
            self._stop_name_flag = True
            self._continue_flag = True
            for se in re_res:
                if isinstance(se, tuple):
                    se_t = None
                    for se_i in se:
                        if se_i and str(se_i).isdigit():
                            se_t = se_i
                            break
                    if se_t:
                        se = int(se_t)
                    else:
                        break
                else:
                    se = int(se)
                if self.begin_season is None:
                    self.begin_season = se
                    self.total_season = 1
                else:
                    if se > self.begin_season:
                        self.end_season = se
                        self.total_season = (self.end_season - self.begin_season) + 1
                        if self.isfile and self.total_season > 1:
                            self.end_season = None
                            self.total_season = 1
        elif token.isdigit():
            try:
                int(token)
            except ValueError:
                return
            if self._last_token_type == "SEASON" \
                    and self.begin_season is None \
                    and len(token) < 3:
                self.begin_season = int(token)
                self.total_season = 1
                self._last_token_type = "season"
                self._stop_name_flag = True
                self._continue_flag = False
                self.type = MediaType.TV
        elif token.upper() == "SEASON" and self.begin_season is None:
            self._last_token_type = "SEASON"
        elif self.type == MediaType.TV and self.begin_season is None:
            self.begin_season = 1

    def __init_episode(self, token: str):
        re_res = re.findall(r"%s" % self._episode_re, token, re.IGNORECASE)
        if re_res:
            self._last_token_type = "episode"
            self._continue_flag = False
            self._stop_name_flag = True
            self.type = MediaType.TV
            for se in re_res:
                if isinstance(se, tuple):
                    se_t = None
                    for se_i in se:
                        if se_i and str(se_i).isdigit():
                            se_t = se_i
                            break
                    if se_t:
                        se = int(se_t)
                    else:
                        break
                else:
                    se = int(se)
                if self.begin_episode is None:
                    self.begin_episode = se
                    self.total_episode = 1
                else:
                    if se > self.begin_episode:
                        self.end_episode = se
                        self.total_episode = (self.end_episode - self.begin_episode) + 1
                        if self.isfile and self.total_episode > 2:
                            self.end_episode = None
                            self.total_episode = 1
        elif token.isdigit():
            try:
                int(token)
            except ValueError:
                return
            if self.begin_episode is not None \
                    and self.end_episode is None \
                    and len(token) < 5 \
                    and int(token) > self.begin_episode \
                    and self._last_token_type == "episode":
                self.end_episode = int(token)
                self.total_episode = (self.end_episode - self.begin_episode) + 1
                if self.isfile and self.total_episode > 2:
                    self.end_episode = None
                    self.total_episode = 1
                self._continue_flag = False
                self.type = MediaType.TV
            elif self.begin_episode is None \
                    and 1 < len(token) < 4 \
                    and self._last_token_type != "year" \
                    and self._last_token_type != "videoencode" \
                    and token != self._unknown_name_str:
                self.begin_episode = int(token)
                self.total_episode = 1
                self._last_token_type = "episode"
                self._continue_flag = False
                self._stop_name_flag = True
                self.type = MediaType.TV
            elif self._last_token_type == "EPISODE" \
                    and self.begin_episode is None \
                    and len(token) < 5:
                self.begin_episode = int(token)
                self.total_episode = 1
                self._last_token_type = "episode"
                self._continue_flag = False
                self._stop_name_flag = True
                self.type = MediaType.TV
        elif token.upper() == "EPISODE":
            self._last_token_type = "EPISODE"

    def __init_resource_type(self, token):
        if not self.name:
            return
        if token.upper() == "DL" \
                and self._last_token_type == "source" \
                and self._last_token == "WEB":
            self._source = "WEB-DL"
            self._continue_flag = False
            return
        elif token.upper() == "RAY" \
                and self._last_token_type == "source" \
                and self._last_token == "BLU":
            if self._source == "UHD":
                self._source = "UHD BluRay"
            else:
                self._source = "BluRay"
            self._continue_flag = False
            return
        elif token.upper() == "WEBDL":
            self._source = "WEB-DL"
            self._continue_flag = False
            return
        if token.upper() == "REMUX" \
                and self._source in ("BluRay", "UHD BluRay"):
            self._effect.append("REMUX")
            self._continue_flag = False
            return
        elif token.upper() == "BLURAY" \
                and self._source == "UHD":
            self._source = "UHD BluRay"
            self._continue_flag = False
            return
        source_res = re.search(r"(%s)" % self._source_re, token, re.IGNORECASE)
        if source_res:
            self._last_token_type = "source"
            self._continue_flag = False
            self._stop_name_flag = True
            if not self._source:
                self._source = source_res.group(1)
                self._last_token = self._source.upper()
            return
        effect_res = re.search(r"(%s)" % self._effect_re, token, re.IGNORECASE)
        if effect_res:
            self._last_token_type = "effect"
            self._continue_flag = False
            self._stop_name_flag = True
            effect = effect_res.group(1)
            if effect not in self._effect:
                self._effect.append(effect)
            self._last_token = effect.upper()

    def __init_web_source(self, token: str, tokens: Tokens, streaming_platforms: StreamingPlatforms):
        if not self.name:
            return

        platform_name = None
        query_range = 1
        consume_next = 0

        current_idx = self._index - 1
        token_list = tokens.tokens
        candidates = []
        for length in (3, 2, 1):
            for start_idx in range(current_idx - length + 1, current_idx + 1):
                end_idx = start_idx + length
                if start_idx < 0 or end_idx > len(token_list) or not (start_idx <= current_idx < end_idx):
                    continue
                parts = token_list[start_idx:end_idx]
                for separator in [" ", "-", ""]:
                    candidates.append((separator.join(parts), start_idx, end_idx))

        for candidate, start_idx, end_idx in candidates:
            if streaming_platforms.is_streaming_platform(candidate):
                platform_name = streaming_platforms.get_streaming_platform_name(candidate)
                query_range = end_idx - start_idx
                consume_next = max(0, end_idx - current_idx - 1)
                break

        if not platform_name:
            return

        for _ in range(consume_next):
            tokens.get_next()

        web_tokens = ["WEB", "DL", "WEBDL", "WEBRIP"]
        match_start_idx = current_idx - (query_range - consume_next - 1)
        match_end_idx = current_idx + consume_next
        start_index = max(0, match_start_idx - query_range)
        end_index = min(len(token_list), match_end_idx + 1 + query_range)
        tokens_to_check = token_list[start_index:end_index]

        if any(tok and tok.upper() in web_tokens for tok in tokens_to_check):
            self.web_source = platform_name
            self._continue_flag = False

    def __init_video_encode(self, token: str):
        if not self.name:
            return
        if not self.year \
                and not self.resource_pix \
                and not self.resource_type \
                and not self.begin_season \
                and not self.begin_episode:
            return
        re_res = re.search(r"(%s)" % self._video_encode_re, token, re.IGNORECASE)
        if re_res:
            self._continue_flag = False
            self._stop_name_flag = True
            self._last_token_type = "videoencode"
            if not self.video_encode:
                if re_res.group(2):
                    self.video_encode = re_res.group(2).upper()
                elif re_res.group(3):
                    self.video_encode = re_res.group(3).lower()
                else:
                    self.video_encode = re_res.group(1).upper()
                self._last_token = self.video_encode
            elif self.video_encode == "10bit":
                self.video_encode = f"{re_res.group(1).upper()} 10bit"
                self._last_token = re_res.group(1).upper()
        elif token.upper() in ['H', 'X']:
            self._continue_flag = False
            self._stop_name_flag = True
            self._last_token_type = "videoencode"
            self._last_token = token.upper() if token.upper() == "H" else token.lower()
        elif token in ["264", "265"] \
                and self._last_token_type == "videoencode" \
                and self._last_token in ['H', 'X']:
            self.video_encode = "%s%s" % (self._last_token, token)
        elif token.isdigit() \
                and self._last_token_type == "videoencode" \
                and self._last_token in ['VC', 'MPEG']:
            self.video_encode = "%s%s" % (self._last_token, token)
        elif token.upper() == "10BIT":
            self._last_token_type = "videoencode"
            if not self.video_encode:
                self.video_encode = "10bit"
            else:
                self.video_encode = f"{self.video_encode} 10bit"

    def __init_audio_encode(self, token: str):
        if not self.name:
            return
        if not self.year \
                and not self.resource_pix \
                and not self.resource_type \
                and not self.begin_season \
                and not self.begin_episode:
            return
        re_res = re.search(r"(%s)" % self._audio_encode_re, token, re.IGNORECASE)
        if re_res:
            self._continue_flag = False
            self._stop_name_flag = True
            self._last_token_type = "audioencode"
            self._last_token = re_res.group(1).upper()
            if not self.audio_encode:
                self.audio_encode = re_res.group(1)
            else:
                if self.audio_encode.upper() == "DTS":
                    self.audio_encode = "%s-%s" % (self.audio_encode, re_res.group(1))
                else:
                    self.audio_encode = "%s %s" % (self.audio_encode, re_res.group(1))
        elif token.isdigit() \
                and self._last_token_type == "audioencode":
            if self.audio_encode:
                if self._last_token.isdigit():
                    self.audio_encode = "%s.%s" % (self.audio_encode, token)
                elif self.audio_encode[-1].isdigit():
                    self.audio_encode = "%s %s.%s" % (self.audio_encode[:-1], self.audio_encode[-1], token)
                else:
                    self.audio_encode = "%s %s" % (self.audio_encode, token)
            self._last_token = token

    def __init_fps(self, token: str):
        if not self.name:
            return
        re_res = re.search(rf"({self._fps_re})", token, re.IGNORECASE)
        if re_res:
            self._continue_flag = False
            self._stop_name_flag = True
            self._last_token_type = "fps"
            fps_value = None
            if re_res.group(1):
                fps_value = re_res.group(1)
            if fps_value and fps_value.isdigit():
                self.fps = int(fps_value)
                self._last_token = f"{self.fps}FPS"
