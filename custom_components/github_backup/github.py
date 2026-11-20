"""GitHub REST operations for a private backup repository."""

from base64 import b64encode
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout
from aiohttp.payload import AsyncIterablePayload
from homeassistant.components.backup import BackupAgentError
from yarl import URL

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
ASSET_NAME = "home-assistant-backup.tar"
MAX_ASSET_SIZE = 2 * 1024**3
TIMEOUT = ClientTimeout(total=3600, connect=15, sock_read=120)
INITIAL_README = b"# Home Assistant backups\n\nEncrypted backup archives are stored under Releases.\n"


class DestinationError(BackupAgentError):
    """A safe error suitable for the backup UI and config flow."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class BackupPayload(AsyncIterablePayload):
    """An async request body with the size required by the GitHub upload API."""

    def __init__(self, stream: AsyncIterator[bytes], size: int) -> None:
        super().__init__(stream, content_type="application/octet-stream")
        self._size = size


class GitHubRepository:
    """Use a single token snapshot for an operation."""

    def __init__(
        self,
        session: ClientSession,
        repository: str,
        token: str,
        *,
        on_auth_failure: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._path = f"/repos/{repository}"
        self._on_auth_failure = on_auth_failure
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @asynccontextmanager
    async def _request(
        self,
        method: str,
        url: str | URL,
        *,
        expected: tuple[int, ...],
        authenticated: bool = True,
        accept: str | None = None,
        **kwargs,
    ) -> AsyncIterator[ClientResponse]:
        headers = self._headers.copy() if authenticated else {}
        if accept:
            headers["Accept"] = accept
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                allow_redirects=False,
                timeout=TIMEOUT,
                **kwargs,
            ) as response:
                if response.status not in expected:
                    if response.status == 429 or (
                        response.status == 403
                        and (
                            response.headers.get("X-RateLimit-Remaining") == "0"
                            or "Retry-After" in response.headers
                        )
                    ):
                        raise DestinationError(
                            "rate_limited",
                            "GitHub rate limit reached. Try again later.",
                        )
                    if response.status == 401 and authenticated:
                        if self._on_auth_failure is not None:
                            self._on_auth_failure()
                        raise DestinationError(
                            "invalid_token",
                            "GitHub rejected the token. Reconnect the backup integration.",
                        )
                    if response.status in (403, 404):
                        raise DestinationError(
                            "access_denied",
                            "GitHub denied access. Check that the token can access the "
                            "backup repository and has Contents Read and write permission.",
                        )
                    raise DestinationError(
                        "github_error", f"GitHub returned HTTP {response.status}."
                    )
                yield response
        except (ClientError, TimeoutError, ValueError):
            # Client exceptions can include signed download URLs.
            raise DestinationError(
                "cannot_connect", "The GitHub backup request could not be completed."
            ) from None

    async def async_validate(
        self, repository_id: int | None = None, *, require_private: bool = False
    ) -> int:
        """Verify repository identity and require privacy for setup or uploads."""
        async with self._request("GET", API + self._path, expected=(200,)) as response:
            repository = await response.json()
        if (repository_id is None or require_private) and repository.get(
            "visibility"
        ) != "private":
            raise DestinationError(
                "not_private", "Set the backup repository to Private in GitHub."
            )
        if repository_id is not None and repository["id"] != repository_id:
            raise DestinationError(
                "wrong_repository",
                "The repository no longer matches the configured ID.",
            )
        if repository.get("permissions", {}).get("push") is False:
            raise DestinationError(
                "access_denied", "The token needs Contents write access."
            )
        return repository["id"]

    async def async_release(self, tag: str) -> dict | None:
        """Get the published release for a tag."""
        url = f"{API}{self._path}/releases/tags/{tag}"
        async with self._request("GET", url, expected=(200, 404)) as response:
            return None if response.status == 404 else await response.json()

    async def async_releases(self) -> AsyncIterator[dict]:
        """List every page of releases."""
        page = 1
        while True:
            async with self._request(
                "GET",
                f"{API}{self._path}/releases",
                expected=(200,),
                params={"per_page": 100, "page": page},
            ) as response:
                releases = await response.json()
                has_next = "next" in response.links
            for release in releases:
                yield release
            if not has_next:
                break
            page += 1

    async def async_create_release(self, tag: str, body: str, *, title: str) -> dict:
        """Create a release and refuse to claim an existing tag."""
        await self._async_ensure_initial_commit()
        async with self._request(
            "GET", f"{API}{self._path}/git/ref/tags/{tag}", expected=(200, 404)
        ) as response:
            if response.status == 200:
                raise DestinationError(
                    "tag_exists", "A Git tag already uses the backup ID."
                )
        async with self._request(
            "POST",
            f"{API}{self._path}/releases",
            expected=(201,),
            json={
                "tag_name": tag,
                "name": title,
                "body": body,
                "prerelease": True,
            },
        ) as response:
            return await response.json()

    async def _async_has_commits(self) -> bool:
        async with self._request(
            "GET",
            f"{API}{self._path}/commits",
            expected=(200, 409),
            params={"per_page": 1},
        ) as response:
            return response.status == 200 and bool(await response.json())

    async def _async_ensure_initial_commit(self) -> None:
        """Provide a release tag target when the backup repository is empty."""
        if await self._async_has_commits():
            return

        async with self._request(
            "PUT",
            f"{API}{self._path}/contents/README.md",
            expected=(201, 409, 422),
            json={
                "message": "Initialize Home Assistant backup repository",
                "content": b64encode(INITIAL_README).decode("ascii"),
            },
        ) as response:
            if response.status == 201:
                return

        # Another writer may have initialized the default branch since the check.
        if not await self._async_has_commits():
            raise DestinationError(
                "cannot_initialize_repository",
                "GitHub could not create the first commit. Check Contents write access "
                "and repository rules for the default branch.",
            )

    async def async_upload(self, release_id: int, body: BackupPayload) -> dict:
        """Upload the raw archive to GitHub's dedicated upload host."""
        async with self._request(
            "POST",
            f"{UPLOADS}{self._path}/releases/{release_id}/assets",
            expected=(201,),
            params={"name": ASSET_NAME},
            data=body,
        ) as response:
            return await response.json()

    async def async_download(self, asset_id: int) -> AsyncIterator[bytes]:
        """Stream an asset, omitting the token from the signed redirect request."""
        url = f"{API}{self._path}/releases/assets/{asset_id}"
        async with self._request(
            "GET", url, expected=(200, 302), accept="application/octet-stream"
        ) as response:
            if response.status == 200:
                async for chunk in response.content.iter_chunked(256 * 1024):
                    yield chunk
                return
            location = URL(response.headers.get("Location", ""), encoded=True)
            if location.scheme != "https" or not location.host or location.user:
                raise DestinationError(
                    "github_error", "GitHub returned an invalid download URL."
                )

        async with self._request(
            "GET", location, expected=(200,), authenticated=False
        ) as response:
            async for chunk in response.content.iter_chunked(256 * 1024):
                yield chunk

    async def async_delete_asset(self, asset_id: int) -> None:
        """Remove an incomplete upload before retrying."""
        async with self._request(
            "DELETE",
            f"{API}{self._path}/releases/assets/{asset_id}",
            expected=(204, 404),
        ):
            pass

    async def async_delete_release(self, release_id: int, tag: str) -> None:
        """Remove the backup release and its tag."""
        async with self._request(
            "DELETE", f"{API}{self._path}/releases/{release_id}", expected=(204,)
        ):
            pass
        async with self._request(
            "DELETE", f"{API}{self._path}/git/refs/tags/{tag}", expected=(204, 404)
        ):
            pass
