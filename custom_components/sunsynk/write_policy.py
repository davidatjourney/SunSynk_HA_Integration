"""Shared permission to change cloud settings; authentication remains separate."""

from homeassistant.exceptions import HomeAssistantError

from .const import ACCESS_READ_ONLY, ACCESS_READ_WRITE


class SunsynkReadOnlyError(HomeAssistantError):
    """Raised before a cloud write when write access has not been enabled."""


class WritePolicy:
    """One revocable policy shared by a config entry and all its API clients."""

    def __init__(self, mode: str = ACCESS_READ_ONLY) -> None:
        self._writable = mode == ACCESS_READ_WRITE

    @property
    def mode(self) -> str:
        return ACCESS_READ_WRITE if self._writable else ACCESS_READ_ONLY

    def ensure_writable(self) -> None:
        """Reject a write without pretending that it was applied."""
        if not self._writable:
            raise SunsynkReadOnlyError(
                "Integration is read-only. Select Read/write in integration "
                "options to allow changes."
            )

    def revoke(self) -> None:
        """Block queued clients before the config entry reloads."""
        self._writable = False
