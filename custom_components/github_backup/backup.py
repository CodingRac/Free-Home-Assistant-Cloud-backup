"""Expose GitHub as a Home Assistant backup destination."""

import json
import logging
import re
from asyncio import Lock
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import datetime
from typing import Any

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupNotFound,
    OnProgressCallback,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.dt import as_utc, parse_datetime

from .const import (
    CONF_AGENT_ID,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    CONF_TOKEN,
    DATA_BACKUP_LISTENERS,
    DOMAIN,
)
from .github import (
    ASSET_NAME,
    MAX_ASSET_SIZE,
    BackupPayload,
    DestinationError,
    GitHubRepository,
)

TAG_PREFIX = "ha-backup-"
DATED_TAG_PREFIX = "ha-dated-backup-"
METADATA_MARKER = "<!-- github_backup:v1 -->\n"
BACKUP_ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,128}")
RETENTION_FIELDS = ("instance_id", "with_automatic_settings")
_LOGGER = logging.getLogger(__name__)


def _tag(backup_id: str) -> str:
    if not BACKUP_ID_PATTERN.fullmatch(backup_id):
        raise BackupNotFound("Invalid Home Assistant backup ID.")
    return TAG_PREFIX + backup_id


def _dated_tag(backup_id: str, backup_time: datetime) -> str:
    """Use a fixed-width UTC timestamp so GitHub sorts newest backups first."""
    return f"{DATED_TAG_PREFIX}{as_utc(backup_time):%Y%m%dT%H%M%S%fZ}-{backup_id}"


def _metadata(release: dict) -> AgentBackup | None:
    """Read metadata only from releases created by this integration."""
    body = release.get("body") or ""
    if not body.startswith(METADATA_MARKER):
        return None
    try:
        backup = AgentBackup.from_dict(json.loads(body.removeprefix(METADATA_MARKER)))
        if (
            (backup_time := parse_datetime(backup.date)) is not None
            and release["tag_name"]
            in (_tag(backup.backup_id), _dated_tag(backup.backup_id, backup_time))
            and backup.protected is True
            and type(backup.size) is int
            and 0 < backup.size < MAX_ASSET_SIZE
            and isinstance(backup.name, str)
            and isinstance(backup.extra_metadata, dict)
        ):
            return backup
    except (KeyError, TypeError, ValueError, BackupNotFound):
        pass
    raise DestinationError(
        "invalid_metadata", "A GitHub backup release has invalid metadata."
    )


def _archive(release: dict) -> dict | None:
    return next(
        (asset for asset in release["assets"] if asset["name"] == ASSET_NAME),
        None,
    )


def _complete(asset: dict | None, backup: AgentBackup) -> bool:
    return (
        asset is not None
        and asset["state"] == "uploaded"
        and asset["size"] == backup.size
    )


class GitHubBackupAgent(BackupAgent):
    """Upload encrypted backups and expose them for retrieval and retention."""

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__()
        self._hass = hass
        self._entry = entry
        self._entry_data = entry.data
        self._repository = self._entry_data[CONF_REPOSITORY]
        self._repository_id = self._entry_data[CONF_REPOSITORY_ID]
        self._mutation_lock = Lock()
        self.name = entry.title
        self.unique_id = entry.data.get(CONF_AGENT_ID, str(self._repository_id))

    async def _destination(self, *, require_private: bool = False) -> GitHubRepository:
        token = self._entry_data.get(CONF_TOKEN)
        if not token:
            self._start_reauth()
            raise DestinationError(
                "invalid_token", "Reconnect the backup integration to supply a token."
            )
        destination = GitHubRepository(
            async_get_clientsession(self._hass),
            self._repository,
            token,
            on_auth_failure=self._start_reauth,
        )
        try:
            await destination.async_validate(
                self._repository_id, require_private=require_private
            )
        except DestinationError as err:
            if err.reason == "access_denied":
                self._start_reauth()
            raise
        return destination

    def _start_reauth(self) -> None:
        """Avoid reopening repair for credentials replaced during a reload."""
        if self._entry.data is self._entry_data:
            self._entry.async_start_reauth(self._hass)

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: OnProgressCallback,
        **kwargs: Any,
    ) -> None:
        """Stream an encrypted backup, resuming safely after an incomplete attempt."""
        if not backup.protected:
            raise DestinationError(
                "encryption_required",
                "Turn on encryption for the GitHub backup location.",
            )
        if not 0 < backup.size < MAX_ASSET_SIZE:
            raise DestinationError(
                "size_limit",
                "GitHub requires a backup size greater than 0 and under 2 GiB.",
            )
        if (backup_time := parse_datetime(backup.date)) is None:
            raise DestinationError("invalid_metadata", "The backup date is invalid.")

        legacy_tag = _tag(backup.backup_id)
        tag = _dated_tag(backup.backup_id, backup_time)
        metadata = backup.as_dict()
        metadata["extra_metadata"] = {
            key: backup.extra_metadata[key]
            for key in RETENTION_FIELDS
            if key in backup.extra_metadata
        }

        async with self._mutation_lock:
            destination = await self._destination(require_private=True)
            release = await destination.async_release(tag)
            if release is None:
                release = await destination.async_release(legacy_tag)
            if release is None:
                release = await destination.async_create_release(
                    tag,
                    METADATA_MARKER + json.dumps(metadata, separators=(",", ":")),
                    title=f"Home Assistant backup · {backup_time:%Y-%m-%d %H:%M:%S %Z}".rstrip(),
                )
            elif (stored := _metadata(release)) is None or stored.as_dict() != metadata:
                raise DestinationError(
                    "release_conflict", "A GitHub release already uses this backup ID."
                )

            if (asset := _archive(release)) is not None:
                if _complete(asset, backup):
                    on_progress(bytes_uploaded=backup.size)
                    return
                if asset["state"] != "starter":
                    raise DestinationError(
                        "release_conflict",
                        "The existing GitHub archive has an unexpected size.",
                    )
                await destination.async_delete_asset(asset["id"])

            stream = await open_stream()

            async def chunks() -> AsyncIterator[bytes]:
                sent = 0
                async for chunk in stream:
                    sent += len(chunk)
                    if sent > backup.size:
                        raise ValueError("Backup stream exceeded its declared size.")
                    yield chunk
                    on_progress(bytes_uploaded=sent)
                if sent != backup.size:
                    raise ValueError("Backup stream ended before its declared size.")

            asset = await destination.async_upload(
                release["id"], BackupPayload(chunks(), backup.size)
            )
            if not _complete(asset, backup):
                raise DestinationError(
                    "incomplete_upload",
                    "GitHub did not confirm the complete backup upload.",
                )
            on_progress(bytes_uploaded=backup.size)

    async def _complete_release(
        self, destination: GitHubRepository, backup_id: str
    ) -> tuple[dict, AgentBackup, dict]:
        release = await self._release_for_backup(destination, backup_id)
        if release is not None and (backup := _metadata(release)) is not None:
            asset = _archive(release)
            if asset is not None and _complete(asset, backup):
                return release, backup, asset
        raise BackupNotFound(f"Backup {backup_id} is unavailable in GitHub.")

    async def _release_for_backup(
        self, destination: GitHubRepository, backup_id: str
    ) -> dict | None:
        """Find a release when Home Assistant supplies only the backup ID."""
        if (release := await destination.async_release(_tag(backup_id))) is not None:
            return release
        async for release in destination.async_releases():
            tag = release["tag_name"]
            if tag.startswith(DATED_TAG_PREFIX) and (
                tag.removeprefix(DATED_TAG_PREFIX).partition("-")[2] == backup_id
            ):
                return release
        return None

    async def async_download_backup(
        self, backup_id: str, **kwargs: Any
    ) -> AsyncIterator[bytes]:
        """Return a stream for the archive."""
        destination = await self._destination()
        _, _, asset = await self._complete_release(destination, backup_id)
        return destination.async_download(asset["id"])

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        """Delete the verified backup release and its Git tag."""
        async with self._mutation_lock:
            destination = await self._destination()
            release, _, _ = await self._complete_release(destination, backup_id)
            if len(release["assets"]) != 1:
                raise DestinationError(
                    "release_conflict", "The backup release contains additional assets."
                )
            await destination.async_delete_release(release["id"], release["tag_name"])

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        """Find metadata for a complete archive."""
        destination = await self._destination()
        _, backup, _ = await self._complete_release(destination, backup_id)
        return backup

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        """Return complete backups so Home Assistant can apply its retention policy."""
        destination = await self._destination()
        backups = []
        async for release in destination.async_releases():
            try:
                backup = _metadata(release)
            except DestinationError:
                _LOGGER.warning(
                    "Skipping GitHub release %s with invalid backup metadata",
                    release.get("id"),
                )
                continue
            if backup is not None and _complete(_archive(release), backup):
                backups.append(backup)
        return backups


async def async_get_backup_agents(hass: HomeAssistant) -> list[BackupAgent]:
    """Return a stable agent instance for each loaded config entry."""
    return [
        entry.runtime_data for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    ]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant, *, listener: Callable[[], None], **kwargs: Any
) -> Callable[[], None]:
    """Notify the backup manager as GitHub destinations load and unload."""
    listeners = hass.data.setdefault(DATA_BACKUP_LISTENERS, [])
    listeners.append(listener)

    @callback
    def remove_listener() -> None:
        listeners.remove(listener)

    return remove_listener
