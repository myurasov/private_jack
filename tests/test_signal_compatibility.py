"""Offline upgrade tests for the Bluetooth signal sensor."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
import pytest_asyncio

from custom_components.private_jack import sensor
from custom_components.private_jack.const import CONF_DEVICE_ADDRESS, DOMAIN

ADDRESS = "fixture-device"
LEGACY = f"{ADDRESS}_sa_bt_signal"
UPSTREAM = f"{ADDRESS}_bluetooth_signal"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def hass(tmp_path):
    """Load real HA registries without starting integrations or Bluetooth."""
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    yield hass
    await hass.async_stop(force=True)


async def setup_signal(hass):
    entry = SimpleNamespace(entry_id="test", data={CONF_DEVICE_ADDRESS: ADDRESS})
    coordinator = SimpleNamespace(
        device_type="portable", model_code=None, data={}, last_update_success=True
    )
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    entities = []
    await sensor.async_setup_entry(hass, entry, entities.extend)
    # Unpacking also detects a second signal sensor in the setup callback.
    (signal,) = (
        e for e in entities if e.device_class == sensor.SensorDeviceClass.SIGNAL_STRENGTH
    )
    signal.hass = hass
    return signal


@pytest.mark.parametrize(
    ("existing", "disabled_by"),
    [
        (LEGACY, None), (LEGACY, er.RegistryEntryDisabler.USER),
        (UPSTREAM, None), (None, None),
    ],
    ids=["legacy-enabled", "legacy-user-disabled", "upstream-enabled", "fresh"],
)
async def test_setup_reuses_existing_registry_entry(hass, existing, disabled_by):
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", DOMAIN, "other-device_sa_bt_signal")
    if existing:
        old = registry.async_get_or_create("sensor", DOMAIN, existing, disabled_by=disabled_by)
        old = registry.async_update_entity(old.entity_id, new_entity_id="sensor.renamed_signal")

    signal = await setup_signal(hass)
    assert signal.unique_id == (existing or UPSTREAM)
    assert signal.entity_registry_enabled_default is (existing == LEGACY)
    # Register the entity emitted by setup with HA, using its advertised default.
    default_disabler = (
        None if signal.entity_registry_enabled_default else er.RegistryEntryDisabler.INTEGRATION
    )
    registered = registry.async_get_or_create(
        "sensor", DOMAIN, signal.unique_id, disabled_by=default_disabler
    )
    if existing:
        assert registered.id == old.id
        assert registered.entity_id == old.entity_id
        assert registered.disabled_by == disabled_by
    else:
        assert registered.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    alternate_id = UPSTREAM if signal.unique_id == LEGACY else LEGACY
    assert registry.async_get_entity_id("sensor", DOMAIN, alternate_id) is None


@pytest.fixture
def radios(monkeypatch):
    def reading(name, rssi):
        return SimpleNamespace(
            scanner=SimpleNamespace(name=name), advertisement=SimpleNamespace(rssi=rssi)
        )

    readings = [reading("first", -50), reading("second", -72)]
    monkeypatch.setattr(
        sensor.bluetooth, "async_scanner_devices_by_address", Mock(return_value=readings)
    )
    monkeypatch.setattr(sensor, "_connected_via", Mock(return_value="source-id"))
    monkeypatch.setattr(
        sensor.bluetooth, "async_scanner_by_source", Mock(return_value=readings[1].scanner)
    )
    return readings


async def test_fresh_install_reports_upstream_attributes(hass, radios):
    signal = await setup_signal(hass)
    assert signal.extra_state_attributes == {
        "best_source": "first",
        "connected_via": "second",
        "rssi_by_source": {"first": -50, "second": -72},
    }


async def test_legacy_reading_survives_expired_advertisements(hass, radios, monkeypatch):
    er.async_get(hass).async_get_or_create("sensor", DOMAIN, LEGACY)
    signal = await setup_signal(hass)
    info = Mock(return_value=SimpleNamespace(time=80.25))
    now = datetime(2020, 1, 1, 12, 0, 30, 900000, tzinfo=UTC)
    monkeypatch.setattr(sensor.bluetooth, "async_last_service_info", info)
    monkeypatch.setattr(sensor, "monotonic", lambda: 100.75)
    monkeypatch.setattr(sensor.dt_util, "utcnow", lambda: now)

    expected = {
        "best_source": "first",
        "radio": "first",
        "connected_via": "second",
        "rssi_by_source": {"first": -50, "second": -72},
        "per_radio": {"first": -50, "second": -72},
        "last_advert_at": "2020-01-01T12:00:10+00:00",
    }
    assert signal.extra_state_attributes == expected
    info.assert_called_once_with(hass, ADDRESS, connectable=True)

    # A clock adjustment and then a vanished advertisement must not re-date the snapshot.
    now = now.replace(minute=5)
    radios.pop(0)
    assert signal.extra_state_attributes == expected
    radios.clear()
    info.return_value = None
    assert signal.extra_state_attributes == expected
    assert signal.available and signal.native_value == -50
