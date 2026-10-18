"""Data update coordinators for REMA 1000."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Catalog, RemaAuthError, RemaClient, RemaError, ShoppingList, is_insert
from .const import DOMAIN, LIST_SCAN_INTERVAL, OFFERS_SCAN_INTERVAL, STORAGE_VERSION

if TYPE_CHECKING:
    from .favorites import FavoriteOffers
    from .mealplan import MealPlanner
    from .storage import RemaPlanStore

_LOGGER = logging.getLogger(__name__)


@dataclass
class RemaRuntimeData:
    """Objects kept for a config entry while it is loaded."""

    client: RemaClient
    shopping_list: RemaShoppingListCoordinator
    offers: RemaOffersCoordinator
    store: RemaPlanStore
    planner: MealPlanner
    favorite_offers: FavoriteOffers


type RemaConfigEntry = ConfigEntry[RemaRuntimeData]


class RemaShoppingListCoordinator(DataUpdateCoordinator[ShoppingList]):
    """Keep the primary shopping list in sync."""

    config_entry: RemaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: RemaConfigEntry, client: RemaClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} shopping list",
            update_interval=LIST_SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> ShoppingList:
        """Fetch the primary list. It is looked up every time so a change of
        primary list in the app is followed automatically."""
        try:
            return await self.client.async_get_primary_list()
        except RemaAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
                translation_placeholders={"email": self.config_entry.data[CONF_EMAIL]},
            ) from err
        except RemaError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_add_items(self, items: list[dict[str, Any]]) -> None:
        """Add items ({product_id|name, amount}) to the primary list."""
        shopping_list = await self.client.async_get_primary_list()
        # One line per product (or free-text name): a new line is not on
        # shopping_list yet, so a second one would be POSTed again, and REMA
        # rejects a product that is already on the list (HTTP 422).
        merged: dict[Any, dict[str, Any]] = {}
        for item in items:
            name = item.get("name")
            key = item.get("product_id") or (name.strip().lower() if name else None)
            if key in merged:
                merged[key]["amount"] += int(item.get("amount") or 1)
            else:
                merged[key] = {**item, "amount": int(item.get("amount") or 1)}
        for item in merged.values():
            await self.client.async_add_to_list(
                shopping_list,
                amount=int(item.get("amount") or 1),
                product_id=item.get("product_id"),
                name=item.get("name"),
            )
        # Refresh right away (not debounced) so HA shows the change at once.
        await self.async_refresh()

    async def async_clear(self) -> int:
        """Remove every item from the primary list. Returns how many."""
        shopping_list = await self.client.async_get_primary_list()
        ids = [item.id for item in shopping_list.items]
        await self.client.async_delete_items(shopping_list.id, ids)
        await self.async_refresh()
        return len(ids)


@dataclass
class OffersData:
    """Tilbudsaviser for REMA 1000."""

    current: Catalog | None = None
    upcoming: Catalog | None = None
    catalogs: list[Catalog] = field(default_factory=list)
    first_seen: dict[str, str] = field(default_factory=dict)


class RemaOffersCoordinator(DataUpdateCoordinator[OffersData]):
    """Track the current and next tilbudsavis.

    The first time a catalogue is seen is stored, so we learn when REMA
    publishes next week's avis.
    """

    config_entry: RemaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: RemaConfigEntry, client: RemaClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} offers",
            update_interval=OFFERS_SCAN_INTERVAL,
        )
        self.client = client
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.catalogs"
        )
        self._first_seen: dict[str, str] | None = None

    async def _async_update_data(self) -> OffersData:
        """Fetch catalogues from Tjek."""
        if self._first_seen is None:
            stored = await self._store.async_load() or {}
            self._first_seen = dict(stored.get("first_seen") or {})
        try:
            catalogs = await self.client.async_get_catalogs()
        except RemaError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        now = datetime.now(UTC)
        changed = False
        for catalog in catalogs:
            if catalog.id not in self._first_seen:
                self._first_seen[catalog.id] = now.isoformat()
                changed = True
                _LOGGER.info(
                    "Ny REMA 1000 tilbudsavis fundet: %s (gyldig %s - %s)",
                    catalog.label,
                    catalog.valid_from,
                    catalog.valid_to,
                )
        if changed:
            await self._store.async_save({"first_seen": self._first_seen})

        # An insert (indstik) is part of its week's avis, whose offers it
        # repeats; it is never the week's avis itself, nor next week's.
        weekly = [c for c in catalogs if not is_insert(c, catalogs)]
        running = [
            c for c in weekly if c.valid_from and c.valid_to and c.valid_from <= now <= c.valid_to
        ]
        future = sorted(
            (c for c in weekly if c.valid_from and c.valid_from > now),
            key=lambda c: c.valid_from or now,
        )
        current = max(running, key=lambda c: c.offer_count or 0) if running else None
        return OffersData(
            current=current,
            upcoming=future[0] if future else None,
            catalogs=catalogs,
            first_seen=dict(self._first_seen),
        )
