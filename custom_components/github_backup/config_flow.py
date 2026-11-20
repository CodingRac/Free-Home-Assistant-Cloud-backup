"""Guide the user through setting up and renewing the GitHub credential."""

import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    FlowType,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AGENT_ID,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    CONF_TOKEN,
    DOMAIN,
    NAME,
)
from .github import DestinationError, GitHubRepository

TOKEN_FIELD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
TOKEN_PATTERN = re.compile(r"github_pat_[A-Za-z0-9_]+")
NEW_REPOSITORY_URL = "https://github.com/new"
NEW_TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"
BACKUP_LOCATIONS_PATH = "/config/backup/settings#locations"
REPOSITORY_PATTERN = re.compile(
    r"(?:https://github\.com/)?"
    r"(?P<repository>[a-zA-Z0-9-]+/(?!\.{1,2}(?:[/?#]|$))[a-zA-Z0-9_.-]+)"
    r"/?(?:[?#][^\s]*)?",
    re.IGNORECASE | re.ASCII,
)


class GitHubBackupConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure the backup repository and its GitHub credential."""

    VERSION = 1
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Expose the Configure gear on the repository row."""
        return GitHubBackupOptionsFlow()

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Show the same connection form for setup and reconfiguration."""
        entry = (
            self._get_reconfigure_entry() if self.source == SOURCE_RECONFIGURE else None
        )
        errors = {}
        previous = entry.data if entry else None
        current_repository = previous[CONF_REPOSITORY] if previous else ""
        repository_input = (
            f"https://github.com/{current_repository}" if previous else ""
        )
        if user_input is not None:
            repository_input = user_input[CONF_REPOSITORY].strip()
            token = user_input[CONF_TOKEN].strip()
            if (match := REPOSITORY_PATTERN.fullmatch(repository_input)) is None:
                errors[CONF_REPOSITORY] = "invalid_repository"
            else:
                repository = match.group("repository")
                expected_id = (
                    previous[CONF_REPOSITORY_ID]
                    if previous
                    and repository.casefold() == current_repository.casefold()
                    else None
                )
                try:
                    repository_id = await self._validate(repository, token, expected_id)
                    existing = await self.async_set_unique_id(str(repository_id))
                    if existing is not None and existing is not entry:
                        return self.async_abort(reason="already_configured")
                    if entry is not None and previous is not None:
                        return self._update_connection(
                            entry, previous, repository, repository_id, token
                        )
                    data = {
                        CONF_REPOSITORY: repository,
                        CONF_REPOSITORY_ID: repository_id,
                        CONF_AGENT_ID: self.flow_id,
                        CONF_TOKEN: token,
                    }
                except DestinationError as err:
                    errors["base"] = err.reason
                else:
                    return self.async_create_entry(
                        title=f"{NAME} ({repository})",
                        description="turn_on_backups",
                        description_placeholders={
                            "locations_path": BACKUP_LOCATIONS_PATH,
                        },
                        data=data,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_REPOSITORY): str,
                        vol.Required(CONF_TOKEN): TOKEN_FIELD,
                    }
                ),
                {CONF_REPOSITORY: repository_input} if repository_input else None,
            ),
            description_placeholders={
                "new_repository_url": NEW_REPOSITORY_URL,
                "new_token_url": f"{NEW_TOKEN_URL}?name=Home%20Assistant%20Backup&contents=write",
            },
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        """Open the shared connection form with the saved repository pre-filled."""
        return await self.async_step_user(user_input)

    async def async_step_reauth(self, entry_data) -> ConfigFlowResult:
        """Ask for a replacement credential after an authentication failure."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> ConfigFlowResult:
        """Validate a replacement token against the original repository."""
        entry = self._get_reauth_entry()
        previous = entry.data
        errors = {}
        repository = previous[CONF_REPOSITORY]
        repository_id = previous[CONF_REPOSITORY_ID]
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            try:
                await self._validate(repository, token, repository_id)
                return self._update_connection(
                    entry, previous, repository, repository_id, token
                )
            except DestinationError as err:
                errors["base"] = err.reason
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): TOKEN_FIELD}),
            description_placeholders={
                "repository": repository,
                "new_token_url": NEW_TOKEN_URL,
            },
            errors=errors,
        )

    @callback
    def _update_connection(
        self,
        entry: ConfigEntry,
        previous: Mapping[str, Any],
        repository: str,
        repository_id: int,
        token: str,
    ) -> ConfigFlowResult:
        """Save the new connection while retaining the backup agent's identity."""
        if entry.data is not previous:
            raise DestinationError(
                "connection_changed",
                "The GitHub connection changed while saving. Try again.",
            )
        return self.async_update_reload_and_abort(
            entry,
            unique_id=str(repository_id),
            title=(
                entry.title
                if repository_id == previous[CONF_REPOSITORY_ID]
                else f"{NAME} ({repository})"
            ),
            data={
                CONF_REPOSITORY: repository,
                CONF_REPOSITORY_ID: repository_id,
                CONF_AGENT_ID: previous.get(
                    CONF_AGENT_ID, str(previous[CONF_REPOSITORY_ID])
                ),
                CONF_TOKEN: token,
            },
        )

    async def _validate(
        self, repository: str, token: str, repository_id: int | None = None
    ) -> int:
        if not TOKEN_PATTERN.fullmatch(token):
            raise DestinationError(
                "invalid_token", "Provide a fine-grained GitHub token."
            )
        client = GitHubRepository(async_get_clientsession(self.hass), repository, token)
        return await client.async_validate(repository_id)


class GitHubBackupOptionsFlow(OptionsFlow):
    """Route the Configure gear to the existing connection editor."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        """Open the same config flow used by Reconfigure."""
        step = await self.hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_RECONFIGURE,
                "entry_id": self.config_entry.entry_id,
            },
        )
        if step["type"] is FlowResultType.ABORT:
            return self.async_abort(reason=step["reason"])

        result = self.async_abort(reason="continue_reconfigure")
        result["next_flow"] = (FlowType.CONFIG_FLOW, step["flow_id"])
        return result
