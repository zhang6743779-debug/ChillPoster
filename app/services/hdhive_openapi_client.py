"""
HDHive Open API 客户端
"""

from collections import deque
from threading import Lock
from time import monotonic, sleep
from typing import Any, Literal
import httpx

from core.configs import global_config


class HDHiveAPIError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        description: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.description = description
        self.http_status = http_status
        super().__init__(f"[{code}] {message}" + (f" — {description}" if description else ""))


class HDHiveAuthError(HDHiveAPIError):
    pass


class HDHiveForbiddenError(HDHiveAPIError):
    pass


class HDHiveNotFoundError(HDHiveAPIError):
    pass


class HDHiveRateLimitError(HDHiveAPIError):
    pass


class HDHiveInsufficientPointsError(HDHiveAPIError):
    pass


_ERROR_MAP: dict[int, type[HDHiveAPIError]] = {
    401: HDHiveAuthError,
    403: HDHiveForbiddenError,
    404: HDHiveNotFoundError,
    402: HDHiveInsufficientPointsError,
    429: HDHiveRateLimitError,
}

_CODE_MAP: dict[str, type[HDHiveAPIError]] = {
    "MISSING_API_KEY": HDHiveAuthError,
    "INVALID_API_KEY": HDHiveAuthError,
    "DISABLED_API_KEY": HDHiveAuthError,
    "EXPIRED_API_KEY": HDHiveAuthError,
    "VIP_REQUIRED": HDHiveForbiddenError,
    "ENDPOINT_DISABLED": HDHiveForbiddenError,
    "ENDPOINT_QUOTA_EXCEEDED": HDHiveRateLimitError,
    "RATE_LIMIT_EXCEEDED": HDHiveRateLimitError,
    "INSUFFICIENT_POINTS": HDHiveInsufficientPointsError,
}


MediaType = Literal["movie", "tv"]


_RATE_LIMIT_QPS = 5
_RATE_LIMIT_WINDOW_SECONDS = 1.0
_RATE_LIMIT_LOCK = Lock()
_RATE_LIMIT_REQUESTS: deque[float] = deque()


def _throttle_openapi_request() -> None:
    while True:
        with _RATE_LIMIT_LOCK:
            now = monotonic()
            while _RATE_LIMIT_REQUESTS and now - _RATE_LIMIT_REQUESTS[0] >= _RATE_LIMIT_WINDOW_SECONDS:
                _RATE_LIMIT_REQUESTS.popleft()
            if len(_RATE_LIMIT_REQUESTS) < _RATE_LIMIT_QPS:
                _RATE_LIMIT_REQUESTS.append(now)
                return
            wait_seconds = _RATE_LIMIT_WINDOW_SECONDS - (now - _RATE_LIMIT_REQUESTS[0])
        sleep(max(wait_seconds, 0.01))


class HDHiveOpenClient:
    BASE_URL = "https://hdhive.com/api/open"

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        proxy = global_config.proxy_url or None
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={"X-API-Key": api_key},
            timeout=timeout,
            verify=False,
            proxy=proxy,
        )

    def __enter__(self) -> "HDHiveOpenClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _raise_for_response(resp: httpx.Response) -> None:
        try:
            body: dict[str, Any] = resp.json()
        except Exception:
            resp.raise_for_status()
            return

        if body.get("success"):
            return

        code: str = str(body.get("code", resp.status_code))
        message: str = body.get("message", "Unknown error")
        description: str | None = body.get("description")
        http_status: int = resp.status_code

        exc_cls = _CODE_MAP.get(code) or _ERROR_MAP.get(http_status, HDHiveAPIError)
        raise exc_cls(code=code, message=message, description=description, http_status=http_status)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        _throttle_openapi_request()
        resp = self._client.request(method, path, params=params, json=json)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                sleep_seconds = float(retry_after) if retry_after else 1.0
            except Exception:
                sleep_seconds = 1.0
            sleep(max(sleep_seconds, 1.0))
            _throttle_openapi_request()
            resp = self._client.request(method, path, params=params, json=json)
        self._raise_for_response(resp)
        body: dict[str, Any] = resp.json()
        return body.get("data"), body.get("meta")

    def ping(self) -> dict[str, Any]:
        data, _ = self._request("GET", "/ping")
        return data

    def get_quota(self) -> dict[str, Any]:
        data, _ = self._request("GET", "/quota")
        return data

    def get_usage(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        data, _ = self._request("GET", "/usage", params=params or None)
        return data

    def get_usage_today(self) -> dict[str, Any]:
        data, _ = self._request("GET", "/usage/today")
        return data

    def get_me(self) -> dict[str, Any]:
        data, _ = self._request("GET", "/me")
        return data

    def get_resources(self, media_type: MediaType, tmdb_id: str | int) -> list[dict[str, Any]]:
        data, _ = self._request("GET", f"/resources/{media_type}/{tmdb_id}")
        return data if isinstance(data, list) else []

    def unlock_resources(
        self,
        *,
        slug: str | None = None,
        slugs: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if slugs:
            body["slugs"] = slugs
        elif slug:
            body["slug"] = slug
        else:
            raise ValueError("slug or slugs is required")
        data, _ = self._request("POST", "/resources/unlock", json=body)
        return data if isinstance(data, dict) else {}

    def check_resource(self, url: str) -> dict[str, Any]:
        data, _ = self._request("POST", "/check/resource", json={"url": url})
        return data if isinstance(data, dict) else {}

    def checkin(self, is_gambler: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if is_gambler:
            body["is_gambler"] = True
        data, _ = self._request("POST", "/checkin", json=body or None)
        return data
