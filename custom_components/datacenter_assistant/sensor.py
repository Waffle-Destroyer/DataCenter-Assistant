import logging
from datetime import timedelta
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import STATE_UNKNOWN
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import aiohttp
from aiohttp import ClientError
import asyncio
from .coordinator import get_coordinator, get_resource_coordinator
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .utils import truncate_description, get_resource_icon, safe_name_conversion
from .base_sensors import VCFBaseSensor
from .entity_factory import VCFEntityFactory, VCFDomainUpdateStatusSensor, VCFDomainCapacitySensor, VCFClusterHostCountSensor, VCFHostResourceSensor

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=60)
_DOMAIN = "datacenter_assistant"


class VCFSensorSetupManager:
    """Manager class for setting up VCF sensors."""
    
    def __init__(self, hass, entry, async_add_entities):
        self.hass = hass
        self.entry = entry
        self.async_add_entities = async_add_entities
        self.existing_domain_entities = set()
        self.existing_resource_entities = set()
        
    async def setup_sensors(self):
        """Setup all VCF sensors."""
        entities = []

        try:
            # Initialize coordinators
            coordinator = get_coordinator(self.hass, self.entry)
            resource_coordinator = self.hass.data.get(_DOMAIN, {}).get("resource_coordinator")
            
            # Refresh coordinators
            await self._refresh_coordinators(coordinator, resource_coordinator)

            # Create overall status sensors
            entities.extend([
                VCFOverallStatusSensor(coordinator),
                VCFDomainCountSensor(coordinator)
            ])

            # Store coordinator and add_entities for dynamic entity creation
            self._store_coordinator_data(coordinator)
            
            # Set up dynamic entity creation
            await self._setup_dynamic_entities(coordinator, resource_coordinator)

            # Add initial entities
            self.async_add_entities(entities, True)

        except Exception as e:
            _LOGGER.error("VCF sensors could not be initialized: %s", e)
            self.async_add_entities([], True)
    
    async def _refresh_coordinators(self, coordinator, resource_coordinator):
        """Refresh both coordinators."""
        try:
            await coordinator.async_config_entry_first_refresh()
        except Exception as e:
            _LOGGER.warning("VCF coordinator first refresh failed: %s", e)

        if resource_coordinator:
            try:
                await resource_coordinator.async_config_entry_first_refresh()
                _LOGGER.info("VCF resource coordinator started successfully")
            except Exception as e:
                _LOGGER.warning("VCF resource coordinator first refresh failed: %s", e)
    
    def _store_coordinator_data(self, coordinator):
        """Store coordinator data for other components."""
        self.hass.data.setdefault(_DOMAIN, {})["coordinator"] = coordinator
        self.hass.data.setdefault(_DOMAIN, {})["async_add_entities"] = self.async_add_entities
    
    async def _setup_dynamic_entities(self, coordinator, resource_coordinator):
        """Setup dynamic entity creation."""
        # Set up listeners for dynamic entity creation
        def coordinator_update_callback():
            # Schedule task on event loop (thread-safe)
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self._create_domain_entities(coordinator))
            )
        
        def resource_coordinator_update_callback():
            # Schedule task on event loop (thread-safe)
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self._create_resource_entities(resource_coordinator))
            )

        # Add listeners
        coordinator.async_add_listener(coordinator_update_callback)
        if resource_coordinator:
            resource_coordinator.async_add_listener(resource_coordinator_update_callback)

        # Schedule initial entity creation
        self.hass.loop.call_later(2.0, lambda: self.hass.async_create_task(self._create_domain_entities(coordinator)))
        self.hass.loop.call_later(3.0, lambda: self.hass.async_create_task(self._create_resource_entities(resource_coordinator)))
    
    async def _create_domain_entities(self, coordinator):
        """Create domain-specific entities using factory."""
        if coordinator.data and "domain_updates" in coordinator.data:
            new_entities = []
            for domain_id, domain_data in coordinator.data["domain_updates"].items():
                if domain_id not in self.existing_domain_entities:
                    domain_name = domain_data.get("domain_name", "Unknown")
                    domain_prefix = domain_data.get("domain_prefix", f"domain{len(self.existing_domain_entities) + 1}")
                    
                    domain_entities = VCFEntityFactory.create_domain_sensors(coordinator, domain_id, domain_name, domain_prefix)
                    new_entities.extend(domain_entities)
                    self.existing_domain_entities.add(domain_id)
            
            if new_entities:
                _LOGGER.info(f"Adding {len(new_entities)} domain entities")
                self.async_add_entities(new_entities, True)

    async def _create_resource_entities(self, resource_coordinator):
        """Create resource-specific entities using factory."""
        if resource_coordinator and resource_coordinator.data and "domain_resources" in resource_coordinator.data:
            new_entities = []
            domain_resources = resource_coordinator.data["domain_resources"]
            
            for domain_id, domain_data in domain_resources.items():
                domain_key = f"{domain_id}_resources"
                if domain_key not in self.existing_resource_entities:
                    domain_name = domain_data.get("domain_name", "Unknown")
                    domain_prefix = domain_data.get("domain_prefix", f"domain{len(self.existing_resource_entities) + 1}")
                    
                    resource_entities = VCFEntityFactory.create_resource_sensors(
                        resource_coordinator, domain_id, domain_name, domain_prefix, domain_data
                    )
                    new_entities.extend(resource_entities)
                    self.existing_resource_entities.add(domain_key)
            
            if new_entities:
                _LOGGER.info(f"Adding {len(new_entities)} resource entities")
                self.async_add_entities(new_entities, True)

async def async_setup_entry(hass, entry, async_add_entities):
    """Setup sensor platform using OOP approach."""
    setup_manager = VCFSensorSetupManager(hass, entry, async_add_entities)
    await setup_manager.setup_sensors()


class VCFOverallStatusSensor(VCFBaseSensor):
    """Overall VCF system status sensor."""
    
    def __init__(self, coordinator):
        super().__init__(coordinator, "VCF Overall Status", "vcf_overall_status")
        
        # State preservation during API outages
        self._last_known_state = None
        self._last_known_attributes = None
        self._api_outage_active = False
        
        # Listen for API outage events
        self._remove_listeners = []
    
    async def async_added_to_hass(self):
        """Run when sensor is added to Home Assistant."""
        await super().async_added_to_hass()
        
        # Listen for API outage events
        self._remove_listeners.append(
            self.hass.bus.async_listen("vcf_api_outage_expected", self._handle_api_outage_expected)
        )
        self._remove_listeners.append(
            self.hass.bus.async_listen("vcf_api_restored", self._handle_api_restored)
        )
    
    async def async_will_remove_from_hass(self):
        """Run when sensor is removed from Home Assistant."""
        for remove_listener in self._remove_listeners:
            remove_listener()
    
    def _handle_api_outage_expected(self, event):
        """Handle notification of expected API outage."""
        reason = event.data.get("reason", "unknown")
        if reason == "sddc_manager_upgrade":
            _LOGGER.info("VCF Overall Status sensor: Preserving state during SDDC Manager upgrade")
            self._last_known_state = self._get_overall_status()
            self._last_known_attributes = self._get_status_attributes()
            self._api_outage_active = True
            # Schedule state update safely on the event loop
            self.hass.loop.call_soon_threadsafe(self.async_schedule_update_ha_state)
    
    def _handle_api_restored(self, event):
        """Handle notification of API restoration."""
        reason = event.data.get("reason", "unknown")
        _LOGGER.info(f"VCF Overall Status sensor: API restored, reason: {reason}")
        self._api_outage_active = False
        self._last_known_state = None
        self._last_known_attributes = None
        # Schedule state update safely on the event loop
        self.hass.loop.call_soon_threadsafe(self.async_schedule_update_ha_state)
    
    def _get_overall_status(self):
        """Get the actual overall status from coordinator data."""
        try:
            if self.coordinator.data is None:
                return "not_connected"
                
            if self.coordinator.data.get("setup_failed"):
                return "setup_failed"
                
            domain_updates = self.coordinator.data.get("domain_updates", {})
            if not domain_updates:
                return "no_domains"
            
            # Check status across all domains
            has_updates = any(d.get("update_status") == "updates_available" for d in domain_updates.values())
            has_errors = any(d.get("update_status") == "error" for d in domain_updates.values())
            
            if has_updates:
                return "updates_available"
            elif has_errors:
                return "partial_error"
            else:
                return "up_to_date"
                
        except Exception as e:
            _LOGGER.error(f"Error checking VCF overall status: {e}")
            return "error"
    
    def _get_status_attributes(self):
        """Get the actual status attributes from coordinator data."""
        try:
            if not self.coordinator.data:
                return {"error": "No data available"}
                
            domain_updates = self.coordinator.data.get("domain_updates", {})
            domains = self.coordinator.data.get("domains", [])
            
            return {
                "total": len(domains),
                "with_updates": sum(1 for d in domain_updates.values() if d.get("update_status") == "updates_available"),
                "up_to_date": sum(1 for d in domain_updates.values() if d.get("update_status") == "up_to_date"),
                "errors": sum(1 for d in domain_updates.values() if d.get("update_status") == "error"),
                "setup_failed": self.coordinator.data.get("setup_failed", False),
                "domains": [
                    {
                        "domainName": domain.get("name"),
                        "domainID": domain.get("id"),
                        "status": domain_updates.get(domain.get("id"), {}).get("update_status", "unknown")
                    }
                    for domain in domains
                ]
            }
            
        except Exception as e:
            _LOGGER.error(f"Error getting overall status attributes: {e}")
            return {"error": str(e)}

    @property
    def icon(self):
        state = self.state
        icons = {
            "updates_available": "mdi:update",
            "up_to_date": "mdi:check-circle",
            "setup_failed": "mdi:alert-circle"
        }
        return icons.get(state, "mdi:sync-alert")

    @property
    def state(self):
        """Return the overall state of VCF system."""
        # If we're in an API outage and have preserved state, use it
        if self._api_outage_active and self._last_known_state is not None:
            return self._last_known_state
        
        return self._get_overall_status()

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        # If we're in an API outage and have preserved attributes, use them
        if self._api_outage_active and self._last_known_attributes is not None:
            preserved_attributes = self._last_known_attributes.copy()
            preserved_attributes["state_preserved_during_upgrade"] = True
            preserved_attributes["preservation_reason"] = "SDDC Manager upgrade in progress"
            return preserved_attributes
        
        # Otherwise get current attributes
        attributes = self._get_status_attributes()
        
        # Check if we're using preserved state during API outage (fallback check)
        try:
            coordinator_manager = getattr(self.coordinator, '_coordinator_manager', None)
            if (coordinator_manager and 
                hasattr(coordinator_manager, '_is_sddc_upgrade_in_progress') and
                coordinator_manager._is_sddc_upgrade_in_progress):
                attributes["state_preserved_during_upgrade"] = True
                attributes["preservation_reason"] = "SDDC Manager upgrade in progress"
            else:
                attributes["state_preserved_during_upgrade"] = False
        except:
            attributes["state_preserved_during_upgrade"] = False
        
        return attributes


class VCFDomainCountSensor(VCFBaseSensor):
    """Sensor showing count of active domains."""
    
    def __init__(self, coordinator):
        super().__init__(coordinator, "VCF Active Domains Count", "vcf_active_domains_count", "mdi:server-network")

    @property
    def state(self):
        """Return the count of active domains."""
        domains = self.safe_get_data("domains", default=[])
        return len(domains) if domains else 0

    @property
    def extra_state_attributes(self):
        """Return domain details."""
        try:
            domains = self.safe_get_data("domains", default=[])
            domain_updates = self.safe_get_data("domain_updates", default={})
            
            if not domains or not domain_updates:
                return {"domains": []}
            
            return {
                "domains": [
                    {
                        "domainName": domain.get("name"),
                        "domainID": domain.get("id"),
                        "upd_status": domain_updates.get(domain.get("id"), {}).get("update_status", "unknown"),
                        "curr_ver": domain_updates.get(domain.get("id"), {}).get("current_version"),
                        "sddc_fqdn": domain.get("sddc_manager_fqdn"),
                        "homeassistant_prefix": domain.get("prefix")
                    }
                    for domain in domains
                ]
            }
            
        except Exception as e:
            _LOGGER.error(f"Error getting domain count attributes: {e}")
            return {"error": str(e)}



# All sensor classes except the main status sensors are now defined in entity_factory.py and base_sensors.py
# This provides better organization and reduces code duplication through inheritance and factory patterns

