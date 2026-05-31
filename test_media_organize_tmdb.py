import unittest
from unittest.mock import patch

from app.services.media_organize_tmdb import _parse_filename, _search_tmdb_for_title_sync


class MediaOrganizeTmdbParsingTest(unittest.TestCase):
    def test_auxiliary_storyboard_under_bonus_dir_is_not_identified(self):
        parsed = _parse_filename(
            "电影分镜.mkv",
            file_path="/最近接收/200X/秒速5厘米 (2007)/特典/电影分镜.mkv",
            quiet=True,
        )

        self.assertIsNone(parsed)

    def test_auxiliary_parent_dir_is_not_a_search_candidate(self):
        parsed = _parse_filename(
            "Some.Movie.2007.mkv",
            file_path="/最近接收/Some Movie (2007)/特典/Some.Movie.2007.mkv",
            quiet=True,
        )

        self.assertIsNotNone(parsed)
        self.assertNotIn("特典", parsed["titles_to_try"])

    def test_tv_chinese_exact_match_can_ignore_wrong_release_year(self):
        parsed = _parse_filename(
            "駁命老公追老婆.2004.双语.EP20.end.TVRip.x264.mkv",
            file_path="/最近接收/[TVB2004][驳命老公追老婆][双语字幕20集][TV-MKV][新势力]/駁命老公追老婆.2004.双语.EP20.end.TVRip.x264.mkv",
            quiet=True,
        )
        self.assertIsNotNone(parsed)

        def fake_search_media(title, api_key, item_type, year=None):
            self.assertEqual(item_type, "tv")
            if title == "驳命老公追老婆" and year is None:
                return [{
                    "id": 109135,
                    "name": "驳命老公追老婆",
                    "original_name": "駁命老公追老婆",
                    "first_air_date": "2002-11-11",
                }]
            return []

        with patch("core.tmdb.search_media", side_effect=fake_search_media), \
                patch("app.services.media_organize_tmdb._get_cached_tmdb_search_result", return_value=(False, None)), \
                patch("app.services.media_organize_tmdb._set_cached_tmdb_search_result"), \
                patch("app.services.media_organize_tmdb._get_tv_season_year", return_value="2002"):
            matched = _search_tmdb_for_title_sync(parsed, "fake-api-key", set())

        self.assertEqual(matched["tmdb_id"], 109135)
        self.assertEqual(matched["media_type"], "tv")


if __name__ == "__main__":
    unittest.main()
