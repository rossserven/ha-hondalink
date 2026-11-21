from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HondaLinkAPI, HondaLinkAuthError, HondaLinkCommandError, HondaLinkError
from .const import CONF_SCAN_INTERVAL, CONF_VIN, DEFAULT_SCAN_INTERVAL, DOMAIN
from .util import merge_dashboard, parse_iso_datetime, status_body

_LOGGER = logging.getLogger(__name__)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class HondaLinkDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: HondaLinkAPI) -> None:
        self.api = api
        self._entry = entry
        self.vin = entry.data[CONF_VIN]
        self._push_body: dict[str, Any] | None = None
        # Last Update location result, and the cached position when it arrived. It is kept until
        # the cached dashboard reports a different position (the vehicle uploaded a newer one).
        self._location: tuple[dict[str, Any], Any] | None = None
        self._cached_gps: Any = None
        interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            logging.getLogger(__name__),
            name=f"{DOMAIN}-{self.vin}",
            update_interval=timedelta(minutes=interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            try:
                data = await self.api.async_get_dashboard_latest(self.vin)
            except HondaLinkAuthError:
                # The token may be revoked while the password still works: log in once and retry.
                self.api.access_token = None
                try:
                    await self.api.async_login()
                except HondaLinkAuthError as err:
                    raise ConfigEntryAuthFailed(str(err)) from err
                data = await self.api.async_get_dashboard_latest(self.vin)
        except HondaLinkError as err:
            # Includes an auth error after a successful login: credentials are good, so not a reauth case.
            raise UpdateFailed(str(err)) from err
        self._cached_gps = status_body(data).get("gpsData")
        return self._with_push_data(data)

    def _with_push_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """Overlay the last live (push) refresh on the cached dashboard data, newest value wins."""
        if not self._push_body and not self._location:
            return data
        cached = status_body(data)
        cached_time = _as_utc(parse_iso_datetime(cached.get("timestamp")))
        merged = cached
        if self._push_body:
            push_time = _as_utc(parse_iso_datetime(self._push_body.get("timestamp")))
            if cached_time and push_time and cached_time > push_time:
                merged = merge_dashboard(self._push_body, cached)
            else:
                merged = merge_dashboard(cached, self._push_body)
        if self._location:
            gps, cached_gps_then = self._location
            if self._cached_gps and self._cached_gps != cached_gps_then:
                # The vehicle has uploaded a new position since Update location ran.
                self._location = None
            else:
                merged = merge_dashboard(merged, {"gpsData": gps})
        return {**data, "responseBody": merged}

    async def async_force_refresh(self) -> None:
        try:
            result = await self.api.async_request_dashboard_update(self.vin)
            if self.api.push_active:
                body = (result.response or {}).get("responseBody")
                if isinstance(body, dict) and body.get("timestamp"):
                    # Merge, so values the dashboard result leaves empty are kept.
                    self._push_body = merge_dashboard(self._push_body or {}, body)
                    if body.get("gpsData"):
                        self._location = None
        except HondaLinkError as err:
            _LOGGER.warning("HondaLink live dashboard refresh request failed: %s", err)
        await self.async_refresh()

    async def async_update_location(self) -> None:
        await self._run_command(self.api.async_request_location, location=True)

    async def async_initial_refresh(self) -> None:
        await self.async_force_refresh()

    async def async_start_engine(self) -> None:
        await self._run_command(self.api.async_start_engine, refresh_state=True)

    async def async_stop_engine(self) -> None:
        await self._run_command(self.api.async_stop_engine, refresh_state=True)

    async def async_lock(self) -> None:
        await self._run_command(self.api.async_lock, refresh_state=True)

    async def async_unlock(self) -> None:
        await self._run_command(self.api.async_unlock, refresh_state=True)

    async def async_horn(self) -> None:
        await self._run_command(self.api.async_horn)

    async def async_lights(self) -> None:
        await self._run_command(self.api.async_lights)

    async def async_stop_horn_lights(self) -> None:
        await self._run_command(self.api.async_stop_horn_lights)

    async def _run_command(self, command, *, refresh_state: bool = False, location: bool = False) -> None:
        try:
            result = await command()
        except HondaLinkAuthError as err:
            # Never resend the command; refresh the session so the next attempt can work.
            self.api.access_token = None
            try:
                await self.api.async_login()
            except HondaLinkAuthError as login_err:
                self._entry.async_start_reauth(self.hass)
                raise HomeAssistantError("HondaLink sign-in failed; reauthentication required") from login_err
            except (HondaLinkError, aiohttp.ClientError, asyncio.TimeoutError) as login_err:
                raise HomeAssistantError(f"HondaLink could not refresh the session: {login_err}") from login_err
            raise HomeAssistantError("HondaLink session expired; check the vehicle's status before sending the command again") from err
        except HondaLinkCommandError as err:
            raise HomeAssistantError(str(err)) from err
        if location:
            body = (result.response or {}).get("responseBody")
            if isinstance(body, dict) and body.get("gpsData"):
                self._location = (body["gpsData"], self._cached_gps)
            self.async_set_updated_data(self._with_push_data(self.data or {}))
            return
        if self.api.push_active and refresh_state:
            # The cached dashboard does not reflect the new state; ask the vehicle for a live one.
            self._entry.async_create_background_task(
                self.hass, self.async_force_refresh(), f"{DOMAIN}-refresh-after-command"
            )
        else:
            await self.async_request_refresh()
