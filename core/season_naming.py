import os


_FALSE_VALUES = {"0", "false", "no", "off", "n"}


def is_season_padded_enabled() -> bool:
    raw = os.getenv("SEASON_PADDED")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSE_VALUES


def format_season_dir_name(season_number: int) -> str:
    number = int(season_number)
    if is_season_padded_enabled():
        return f"Season {number:02d}"
    return f"Season {number}"
