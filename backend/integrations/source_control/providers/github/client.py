from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

import httpx

from ...domain.enums import SourceControlErrorCode
from ...errors import NetworkError, SourceControlError
from ...security.redaction import redact_data, redact_url
from .errors import map_github_http_error
from .manifest import DEFAULT_GITHUB_API_URL, GitHubManifest


@dataclass(frozen=True)
class GitHubResponse:
    """Immutable response payload returned by GitHub REST API calls."""

    status_code: int
    headers: dict[str, str]
    data: Any
    url: str

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def next_page_url(self) -> Optional[str]:
        """Extracts the next page URL from RFC 5988 Link header if present."""
        link_header = self.headers.get("link")
        if not link_header:
            return None

        # Format: <https://api.github.com/resource?page=2>; rel="next"
        matches = re.findall(r'<([^>]+)>;\s*rel="([^"]+)"', link_header)
        for target_url, rel in matches:
            if rel.lower() == "next":
                return target_url
        return None


class GitHubClient:
    """Hardened asynchronous HTTP client coordinating communication with GitHub REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_GITHUB_API_URL,
        token: Optional[str] = None,
        *,
        timeout_seconds: float = 30.0,
        manifest: Optional[GitHubManifest] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.manifest = manifest or GitHubManifest()
        self._session: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> GitHubClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def _ensure_session(self) -> httpx.AsyncClient:
        """Initializes the underlying httpx.AsyncClient instance lazily."""
        if self._session is None or self._session.is_closed:
            self._session = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=True,
            )
        return self._session

    async def close(self) -> None:
        """Closes the underlying HTTP transport session."""
        if self._session is not None and not self._session.is_closed:
            await self._session.aclose()
            self._session = None

    def _resolve_url(self, path_or_url: str) -> str:
        """Resolves relative endpoint path or verifies absolute URL against API host."""
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        normalized = "/" + path_or_url.lstrip("/")
        return f"{self.base_url}{normalized}"

    async def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_data: Optional[Any] = None,
        token: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> GitHubResponse:
        """Executes a sanitized HTTP request against GitHub REST API with error translation.

        Raises:
            NetworkError: On transport timeouts, DNS, or socket failures.
            SourceControlError: On normalized GitHub REST API error responses.
        """
        session = await self._ensure_session()
        full_url = self._resolve_url(path_or_url)
        effective_token = token or self.token

        request_headers = self.manifest.build_default_headers(effective_token)
        if headers:
            request_headers.update(headers)

        safe_url = redact_url(full_url)
        try:
            res = await session.request(
                method=method.upper(),
                url=full_url,
                params=params,
                json=json_data,
                headers=request_headers,
            )
        except httpx.TimeoutException as exc:
            raise NetworkError(
                f"GitHub API request timed out ({self.timeout_seconds}s): [{method.upper()}] {safe_url}",
                details={"url": safe_url, "timeout_seconds": str(self.timeout_seconds)},
            ) from exc
        except (httpx.NetworkError, httpx.ConnectError) as exc:
            raise NetworkError(
                f"GitHub API network connection failed: [{method.upper()}] {safe_url}. Details: {exc}",
                details={"url": safe_url},
            ) from exc

        # Parse JSON or fallback to raw text
        raw_text = res.text
        data: Any = None
        if raw_text and raw_text.strip():
            try:
                data = res.json()
            except Exception:
                data = raw_text

        # Translate non-2xx responses into domain exceptions
        if not (200 <= res.status_code < 300):
            response_headers_dict = dict(res.headers)
            raise map_github_http_error(
                status_code=res.status_code,
                body=raw_text,
                headers=response_headers_dict,
                url=safe_url,
                method=method,
            )

        return GitHubResponse(
            status_code=res.status_code,
            headers=dict(res.headers),
            data=data,
            url=safe_url,
        )

    async def get(
        self,
        path_or_url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        token: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> GitHubResponse:
        """Convenience GET request."""
        return await self.request("GET", path_or_url, params=params, token=token, headers=headers)

    async def post(
        self,
        path_or_url: str,
        *,
        json_data: Optional[Any] = None,
        token: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> GitHubResponse:
        """Convenience POST request."""
        return await self.request("POST", path_or_url, json_data=json_data, token=token, headers=headers)

    async def patch(
        self,
        path_or_url: str,
        *,
        json_data: Optional[Any] = None,
        token: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> GitHubResponse:
        """Convenience PATCH request."""
        return await self.request("PATCH", path_or_url, json_data=json_data, token=token, headers=headers)

    async def put(
        self,
        path_or_url: str,
        *,
        json_data: Optional[Any] = None,
        token: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> GitHubResponse:
        """Convenience PUT request."""
        return await self.request("PUT", path_or_url, json_data=json_data, token=token, headers=headers)

    async def delete(
        self,
        path_or_url: str,
        *,
        token: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> GitHubResponse:
        """Convenience DELETE request."""
        return await self.request("DELETE", path_or_url, token=token, headers=headers)

    async def paginate(
        self,
        path_or_url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        token: Optional[str] = None,
        max_pages: int = 20,
    ) -> Sequence[Any]:
        """Automatically traverses GitHub paginated endpoints and collects array items."""
        aggregated: list[Any] = []
        next_target: Optional[str] = path_or_url
        current_params: Optional[Mapping[str, Any]] = params
        pages_retrieved = 0

        while next_target and pages_retrieved < max_pages:
            res = await self.get(next_target, params=current_params, token=token)
            pages_retrieved += 1

            if isinstance(res.data, list):
                aggregated.extend(res.data)
            elif isinstance(res.data, dict) and "items" in res.data and isinstance(res.data["items"], list):
                # Search API envelopes
                aggregated.extend(res.data["items"])
            else:
                break

            next_target = res.next_page_url
            # Subsequent pagination URLs already contain their own query parameters
            current_params = None

        return tuple(aggregated)
