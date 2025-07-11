import logging
import asyncio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import time
from .utils import build_vcf_api_url, normalize_vcf_url

_LOGGER = logging.getLogger(__name__)
_LOGGER.debug("Initialized with log handlers: %s", logging.getLogger().handlers)

DOMAIN = "datacenter_assistant"
PLATFORMS = ["sensor", "binary_sensor", "button"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DataCenter Assistant from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry

    # Migrate existing config entries to ensure proper URL format
    await _migrate_config_entry(hass, entry)

    # Configure logging
    logging.getLogger('custom_components.datacenter_assistant').setLevel(logging.CRITICAL)
    
    # Log integration loading
    _LOGGER.debug("DataCenter Assistant integration loaded")
    
    # Setup platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register services
    await _async_setup_services(hass, entry)
    
    return True

async def _migrate_config_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Migrate existing config entries to ensure proper URL format."""
    vcf_url = entry.data.get("vcf_url")
    if not vcf_url:
        return
    
    normalized_url = normalize_vcf_url(vcf_url)
    
    # If the URL changed after normalization, update the config entry
    if normalized_url != vcf_url:
        _LOGGER.info(f"Migrating VCF URL from '{vcf_url}' to '{normalized_url}'")
        new_data = dict(entry.data)
        new_data["vcf_url"] = normalized_url
        hass.config_entries.async_update_entry(entry, data=new_data)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(*[
            hass.config_entries.async_forward_entry_unload(entry, platform)
            for platform in PLATFORMS
        ])
    )

    if unload_ok:
        # Remove services
        services_to_remove = ["refresh_token", "trigger_upgrade", "download_bundle", "start_domain_upgrade", "acknowledge_upgrade_alerts"]
        for service in services_to_remove:
            hass.services.async_remove(DOMAIN, service)
        
        # Clean up data
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)

    return unload_ok

async def async_setup(hass: HomeAssistant, config):
    """Set up the component (optional)."""
    return True


async def _validate_vcf_credentials(entry: ConfigEntry):
    """Validate VCF credentials from config entry."""
    vcf_url = entry.data.get("vcf_url")
    vcf_username = entry.data.get("vcf_username", "")
    vcf_password = entry.data.get("vcf_password", "")
    
    if not vcf_url or not vcf_username or not vcf_password:
        return None, None, None
    
    # Normalize the URL to ensure proper format
    normalized_url = normalize_vcf_url(vcf_url)
    
    return normalized_url, vcf_username, vcf_password

async def _async_setup_services(hass: HomeAssistant, entry: ConfigEntry):
    """Set up services for VCF integration."""
    
    async def refresh_token_service(call: ServiceCall):
        """Service to refresh VCF token."""
        _LOGGER.info("Service: Refreshing VCF token")
        
        vcf_url, vcf_username, vcf_password = await _validate_vcf_credentials(entry)
        if not all([vcf_url, vcf_username, vcf_password]):
            _LOGGER.warning("Cannot refresh VCF token: Missing credentials")
            return
            
        try:
            session = async_get_clientsession(hass)
            login_url = build_vcf_api_url(vcf_url, "/v1/tokens")
            
            auth_data = {
                "username": vcf_username,
                "password": vcf_password
            }
            
            async with session.post(login_url, json=auth_data, ssl=False) as resp:
                if resp.status != 200:
                    _LOGGER.error(f"VCF token refresh failed: {resp.status}")
                    return
                    
                token_data = await resp.json()
                new_token = token_data.get("accessToken") or token_data.get("access_token")
                
                if new_token:
                    new_data = dict(entry.data)
                    new_data["vcf_token"] = new_token
                    expiry = int(time.time()) + 3600
                    new_data["token_expiry"] = expiry
                    
                    hass.config_entries.async_update_entry(entry, data=new_data)
                    _LOGGER.info("VCF token refreshed successfully")
                else:
                    _LOGGER.warning("Could not extract token from response")
                    
        except Exception as e:
            _LOGGER.error(f"Error refreshing VCF token: {e}")
    
    async def trigger_upgrade_service(call: ServiceCall):
        """Service to trigger VCF upgrade."""
        _LOGGER.info("Service: Triggering VCF upgrade")
        
        component_type = call.data.get("component_type")
        fqdn = call.data.get("fqdn")
        
        if not component_type or not fqdn:
            _LOGGER.error("Component type and FQDN are required for upgrade")
            return
            
        vcf_url = entry.data.get("vcf_url")
        current_token = entry.data.get("vcf_token")
        
        if not vcf_url or not current_token:
            _LOGGER.warning("Cannot execute upgrade: Missing URL or token")
            return
        
        try:
            session = async_get_clientsession(hass)
            api_url = build_vcf_api_url(vcf_url, f"/v1/system/updates/{component_type.lower()}/{fqdn}/start")
            
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Accept": "application/json",
                "Content-Type": "application/json"
            }
            
            async with session.post(api_url, headers=headers, json={}, ssl=False) as resp:
                if resp.status == 401:
                    _LOGGER.warning("Token expired during upgrade, please refresh token")
                elif resp.status not in [200, 202]:
                    error_text = await resp.text()
                    _LOGGER.error(f"Failed to start upgrade: {resp.status} {error_text}")
                else:
                    _LOGGER.info(f"Successfully initiated upgrade for {component_type} {fqdn}")
                    
        except Exception as e:
            _LOGGER.error(f"Error executing VCF upgrade: {e}")
    
    async def download_bundle_service(call: ServiceCall):
        """Service to download VCF bundle."""
        _LOGGER.info("Service: Downloading VCF bundle")
        
        bundle_id = call.data.get("bundle_id")
        
        if not bundle_id:
            _LOGGER.error("Bundle ID is required for download")
            return
            
        vcf_url = entry.data.get("vcf_url")
        current_token = entry.data.get("vcf_token")
        
        if not vcf_url or not current_token:
            _LOGGER.warning("Cannot download bundle: Missing URL or token")
            return
        
        try:
            session = async_get_clientsession(hass)
            download_url = build_vcf_api_url(vcf_url, f"/v1/bundles/{bundle_id}")
            patch_data = {"operation": "DOWNLOAD"}
            
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            
            async with session.patch(download_url, headers=headers, json=patch_data, ssl=False) as resp:
                if resp.status == 401:
                    _LOGGER.warning("Token expired during download, please refresh token")
                elif resp.status not in [200, 202]:
                    error_text = await resp.text()
                    _LOGGER.error(f"Failed to start bundle download: {resp.status} {error_text}")
                else:
                    _LOGGER.info(f"Successfully initiated bundle download: {bundle_id}")
                    
        except Exception as e:
            _LOGGER.error(f"Error downloading VCF bundle: {e}")
    
    async def start_domain_upgrade_service(call: ServiceCall):
        """Service to start VCF domain upgrade."""
        _LOGGER.info("Service: Starting VCF domain upgrade")
        
        domain_id = call.data.get("domain_id")
        
        if not domain_id:
            _LOGGER.error("Domain ID is required for upgrade")
            return
        
        try:
            # Get upgrade service from hass data
            upgrade_service = hass.data.get(DOMAIN, {}).get("upgrade_service")
            if not upgrade_service:
                _LOGGER.error("Upgrade service not available")
                return
            
            # Get coordinator data for domain information
            coordinator = hass.data.get(DOMAIN, {}).get("coordinator")
            if not coordinator or not coordinator.data:
                _LOGGER.error("Coordinator data not available")
                return
            
            domain_data = coordinator.data.get("domain_updates", {}).get(domain_id, {})
            if not domain_data:
                _LOGGER.error(f"Domain {domain_id} not found")
                return
            
            success = await upgrade_service.start_upgrade(domain_id, domain_data)
            if success:
                _LOGGER.info(f"Domain upgrade started successfully for {domain_id}")
            else:
                _LOGGER.warning(f"Domain upgrade could not be started for {domain_id}")
                
        except Exception as e:
            _LOGGER.error(f"Error starting domain upgrade: {e}")
    
    async def acknowledge_upgrade_alerts_service(call: ServiceCall):
        """Service to acknowledge upgrade alerts."""
        _LOGGER.info("Service: Acknowledging upgrade alerts")
        
        domain_id = call.data.get("domain_id")
        
        if not domain_id:
            _LOGGER.error("Domain ID is required for acknowledging alerts")
            return
        
        try:
            # Get upgrade service from hass data
            upgrade_service = hass.data.get(DOMAIN, {}).get("upgrade_service")
            if not upgrade_service:
                _LOGGER.error("Upgrade service not available")
                return
            
            success = await upgrade_service.acknowledge_alerts(domain_id)
            if success:
                _LOGGER.info(f"Alerts acknowledged successfully for domain {domain_id}")
            else:
                _LOGGER.warning(f"No alerts to acknowledge for domain {domain_id}")
                
        except Exception as e:
            _LOGGER.error(f"Error acknowledging alerts: {e}")
    
    # Register services
    hass.services.async_register(DOMAIN, "refresh_token", refresh_token_service)
    hass.services.async_register(DOMAIN, "trigger_upgrade", trigger_upgrade_service)
    hass.services.async_register(DOMAIN, "download_bundle", download_bundle_service)
    hass.services.async_register(DOMAIN, "start_domain_upgrade", start_domain_upgrade_service)
    hass.services.async_register(DOMAIN, "acknowledge_upgrade_alerts", acknowledge_upgrade_alerts_service)
