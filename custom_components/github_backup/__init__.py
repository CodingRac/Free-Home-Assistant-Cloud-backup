"""Free Home Assistant Cloud backup integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .backup import GitHubBackupAgent
from .const import CONF_REPOSITORY, DATA_BACKUP_LISTENERS, NAME

type GitHubBackupConfigEntry = ConfigEntry[GitHubBackupAgent]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Rename default entry titles from the previous release."""
    if entry.version != 1:
        return False
    if entry.minor_version < 2:
        repository = entry.data[CONF_REPOSITORY]
        title = entry.title
        if title == f"GitHub ({repository})":
            title = f"{NAME} ({repository})"
        hass.config_entries.async_update_entry(entry, title=title, minor_version=2)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: GitHubBackupConfigEntry
) -> bool:
    """Make the configured repository available to Home Assistant Backups."""
    entry.runtime_data = GitHubBackupAgent(hass, entry)

    @callback
    def notify_backup_manager() -> None:
        for listener in hass.data.get(DATA_BACKUP_LISTENERS, ()):
            listener()

    entry.async_on_unload(entry.async_on_state_change(notify_backup_manager))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: GitHubBackupConfigEntry
) -> bool:
    """Allow Home Assistant to unload this destination."""
    return True
