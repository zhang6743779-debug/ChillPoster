from collections.abc import Iterator, Mapping, MutableMapping
from os import PathLike
from types import EllipsisType
from typing import Any

from p115client import P115Client
from p115client.tool.iterdir import iter_dirs_with_path, iter_files_with_path


def _ensure_dir_fields(client: P115Client, item: dict[str, Any]) -> dict[str, Any]:
    data = dict(item)
    data["is_dir"] = True
    data.setdefault("size", 0)
    data.setdefault("sha1", "")
    data.setdefault("pickcode", "")
    if not data.get("pickcode"):
        try:
            data["pickcode"] = client.to_pickcode(int(data.get("id", 0) or 0), "fa")
        except Exception:
            data["pickcode"] = ""
    return data


def _ensure_file_fields(item: dict[str, Any]) -> dict[str, Any]:
    data = dict(item)
    data["is_dir"] = False
    if not data.get("pickcode") and data.get("pick_code"):
        data["pickcode"] = data.get("pick_code")
    if not data.get("pick_code") and data.get("pickcode"):
        data["pick_code"] = data.get("pickcode")
    data.setdefault("size", 0)
    data.setdefault("sha1", "")
    return data


def iter_tree_with_path_by_lists(
    client: str | PathLike | P115Client,
    cid: int | str | Mapping = 0,
    *,
    with_ancestors: bool = False,
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None,
    app: str = "android",
    max_workers: None | int = 0,
    max_files: int | None = 0,
    max_dirs: int | None = 0,
    file_page_size: int = 5000,
    file_cooldown: None | float = None,
    file_max_workers: int = 4,
    **request_kwargs,
) -> Iterator[dict[str, Any]]:
    """List a 115 tree with paths without using /files/file batch name filling."""
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    if isinstance(cid, Mapping):
        cid = cid.get("id") or cid.get("pickcode") or 0

    if id_to_dirnode is None or id_to_dirnode is ...:
        dirnode_cache = {}
    else:
        dirnode_cache = id_to_dirnode

    dirs = iter_dirs_with_path(
        client,
        cid=cid,
        with_ancestors=with_ancestors,
        id_to_dirnode=dirnode_cache,
        app=app,
        max_workers=max_workers,
        max_dirs=max_dirs,
        **request_kwargs,
    )
    for item in dirs:
        yield _ensure_dir_fields(client, item)

    file_count = 0
    files = iter_files_with_path(
        client,
        cid=cid,
        with_ancestors=with_ancestors,
        id_to_dirnode=dirnode_cache,
        path_already=True,
        app=app,
        page_size=file_page_size,
        cooldown=file_cooldown,
        max_workers=file_max_workers,
        **request_kwargs,
    )
    for item in files:
        yield _ensure_file_fields(item)
        file_count += 1
        if max_files and max_files > 0 and file_count >= max_files:
            break
