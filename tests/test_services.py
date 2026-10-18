"""Tests for the REMA 1000 actions."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.middagsro.api import RemaError
from custom_components.middagsro.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .conftest import make_plan


@pytest.fixture
def storage_contents() -> dict[str, Any]:
    """One stored plan."""
    return {"plans": [make_plan()]}


async def test_search_products(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Search returns products with prices and labels."""
    result = await hass.services.async_call(
        DOMAIN, "search_products", {"query": "letmælk", "limit": 2}, blocking=True,
        return_response=True,
    )  # fmt: skip
    mock_client.async_search_products.assert_awaited_with("letmælk", 2)
    assert [p["id"] for p in result["products"]] == [20494, 20495]
    assert result["products"][0]["price"] == 10.95

    mock_client.async_search_products.side_effect = RemaError("HTTP 500")
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "search_products", {"query": "x"}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "update_failed"


async def test_add_items(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Products and free text are added, a single item is accepted too."""
    await hass.services.async_call(
        DOMAIN,
        "add_items",
        {"items": [{"product_id": 20494, "amount": 2}, {"name": "Opvasketabs"}]},
        blocking=True,
    )
    calls = [c.kwargs for c in mock_client.async_add_to_list.call_args_list]
    assert calls == [
        {"amount": 2, "product_id": 20494, "name": None},
        {"amount": 1, "product_id": None, "name": "Opvasketabs"},
    ]

    # The same product twice is one line: REMA rejects a second POST with HTTP 422.
    mock_client.async_add_to_list.reset_mock()
    await hass.services.async_call(
        DOMAIN,
        "add_items",
        {"items": [{"product_id": 30001}, {"name": "Kage"}, {"product_id": 30001, "amount": 2},
                   {"name": "kage "}]},
        blocking=True,
    )  # fmt: skip
    calls = [c.kwargs for c in mock_client.async_add_to_list.call_args_list]
    assert calls == [
        {"amount": 3, "product_id": 30001, "name": None},
        {"amount": 2, "product_id": None, "name": "Kage"},
    ]

    await hass.services.async_call(DOMAIN, "add_items", {"items": {"name": "Kaffe"}}, blocking=True)
    assert mock_client.async_add_to_list.call_args.kwargs["name"] == "Kaffe"

    calls = mock_client.async_add_to_list.call_count
    with pytest.raises(Exception):  # noqa: B017 - the schema error type depends on HA's version
        await hass.services.async_call(DOMAIN, "add_items", {"items": [{}]}, blocking=True)
    assert mock_client.async_add_to_list.call_count == calls

    mock_client.async_add_to_list.side_effect = RemaError("HTTP 500")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "add_items", {"items": [{"name": "Kaffe"}]}, blocking=True
        )


async def test_get_offers(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Offers in the current and next avis."""
    result = await hass.services.async_call(
        DOMAIN, "get_offers", {}, blocking=True, return_response=True
    )
    mock_client.async_get_offers.assert_awaited_with("cat-42")
    assert result["catalog"]["label"] == "Uge 42"
    assert result["offers"][0]["heading"] == "Hakket oksekød 8-12%"

    await hass.services.async_call(
        DOMAIN, "get_offers", {"catalog": "upcoming"}, blocking=True, return_response=True
    )
    mock_client.async_get_offers.assert_awaited_with("cat-43")

    init_integration.runtime_data.offers.data.upcoming = None
    result = await hass.services.async_call(
        DOMAIN, "get_offers", {"catalog": "upcoming"}, blocking=True, return_response=True
    )
    assert result == {"catalog": None, "offers": []}

    mock_client.async_get_offers.side_effect = RemaError("Tjek is down")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "get_offers", {}, blocking=True, return_response=True
        )


async def test_generate_and_send_meal_plan(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The actions start a plan and send a stored plan."""
    planner = init_integration.runtime_data.planner
    with patch.object(planner, "async_start_generation") as start:
        await hass.services.async_call(
            DOMAIN, "generate_meal_plan", {"start_date": "2026-10-18", "send": True}, blocking=True
        )
    assert start.call_args.args[0].isoformat() == "2026-10-18"
    assert start.call_args.kwargs == {"send": True}

    await hass.services.async_call(DOMAIN, "send_meal_plan", {"plan_id": "plan-1"}, blocking=True)
    added = [c.kwargs for c in mock_client.async_add_to_list.call_args_list]
    assert added == [
        {"amount": 1, "product_id": 30001, "name": None},
        {"amount": 1, "product_id": None, "name": "Persille (1 bundt)"},
        {"amount": 1, "product_id": None, "name": "Spaghetti"},
    ]
    assert hass.states.get("sensor.middagsro_meal_plan").state == "sent"

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "send_meal_plan", {"plan_id": "nope"}, blocking=True)
    assert err.value.translation_key == "plan_not_found"

    await init_integration.runtime_data.planner.async_restore_sent("plan-1")
    mock_client.async_add_to_list.side_effect = RemaError("HTTP 500")
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "send_meal_plan", {}, blocking=True)
    assert err.value.translation_key == "update_failed"

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "generate_meal_plan", {"start_date": "18/10"}, blocking=True
        )
    assert err.value.translation_key == "invalid_date"


async def test_choosing_the_account(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """With two accounts the action must say which one."""
    second = MockConfigEntry(
        domain=DOMAIN, title="b@example.com", unique_id="2", data={"email": "b", "password": "c"}
    )
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "search_products", {"query": "mælk"}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "entry_required"

    await hass.services.async_call(
        DOMAIN,
        "search_products",
        {"query": "mælk", "config_entry_id": second.entry_id},
        blocking=True,
        return_response=True,
    )

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "search_products",
            {"query": "mælk", "config_entry_id": "missing"},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "entry_not_found"


async def test_no_account_loaded(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Actions explain that no account is loaded."""
    await hass.config_entries.async_unload(init_integration.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "send_meal_plan", {}, blocking=True)
    assert err.value.translation_key == "entry_not_found"
