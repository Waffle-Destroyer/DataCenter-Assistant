import asyncio
import aiohttp
import logging
import time
from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .utils import truncate_description, version_tuple
from .vcf_api import VCFAPIClient, VCFDomain

_LOGGER = logging.getLogger(__name__)
_DOMAIN = "datacenter_assistant"


class VCFCoordinatorManager:
    """Manager class for VCF coordinators to handle upgrades and resources."""
    
    def __init__(self, hass, config_entry):
        self.hass = hass
        self.config_entry = config_entry
        self.vcf_client = VCFAPIClient(hass, config_entry)
        self._domain_cache = {}
        
        # State preservation for API outages during upgrades
        self._last_successful_data = None
        self._last_successful_resource_data = None
        self._api_outage_start_time = None
        self._is_sddc_upgrade_in_progress = False
        self._outage_timeout = 7200  # 2 hour timeout for SDDC Manager upgrades
        
        # Set up event listeners for API outage notifications
        self._setup_api_outage_listeners()
    
    def _setup_api_outage_listeners(self):
        """Set up event listeners for API outage notifications from upgrade service."""
        self.hass.bus.async_listen("vcf_api_outage_expected", self._handle_api_outage_expected)
        self.hass.bus.async_listen("vcf_api_restored", self._handle_api_restored)
    
    def _handle_api_outage_expected(self, event):
        """Handle notification of expected API outage."""
        reason = event.data.get("reason", "unknown")
        domain_id = event.data.get("domain_id", "unknown")
        
        if reason == "sddc_manager_upgrade":
            _LOGGER.info(f"Received API outage notification for domain {domain_id} due to SDDC Manager upgrade")
            self._is_sddc_upgrade_in_progress = True
            self._api_outage_start_time = time.time()
    
    def _handle_api_restored(self, event):
        """Handle notification of API restoration."""
        reason = event.data.get("reason", "unknown")
        domain_id = event.data.get("domain_id", "unknown")
        
        _LOGGER.info(f"Received API restoration notification for domain {domain_id}, reason: {reason}")
        self._is_sddc_upgrade_in_progress = False
        self._api_outage_start_time = None
    
    def _is_upgrade_in_progress(self):
        """Check if any domain has an SDDC Manager upgrade in progress."""
        try:
            upgrade_service = self.hass.data.get("datacenter_assistant", {}).get("upgrade_service")
            if not upgrade_service:
                return False
            
            # Check all domains for SDDC Manager upgrade status
            for domain_id in upgrade_service._upgrade_states:
                status = upgrade_service.get_upgrade_status(domain_id)
                if status == "upgrading_sddcmanager":
                    return True
            
            return False
        except Exception as e:
            _LOGGER.debug(f"Error checking upgrade status: {e}")
            return False
    
    def _should_preserve_state(self, error):
        """Determine if we should preserve the last known state during an API error."""
        try:
            # Check for specific API outage error patterns that suggest temporary infrastructure issues
            error_str = str(error).lower()
            is_api_outage_error = (
                "502" in error_str or  # Bad Gateway errors
                "503" in error_str or  # Service Unavailable
                "connect call failed" in error_str or  # Connection failures
                "connection timeout" in error_str or  # Timeout errors
                "cannot connect to host" in error_str  # Host unreachable
            )
            
            # Primary check: Are we in a known SDDC upgrade state via events?
            if self._is_sddc_upgrade_in_progress and self._api_outage_start_time:
                # Check if we're within the timeout window
                if time.time() - self._api_outage_start_time < self._outage_timeout:
                    return True
                else:
                    _LOGGER.warning("API outage timeout exceeded, resuming normal error handling")
                    self._api_outage_start_time = None
                    self._is_sddc_upgrade_in_progress = False
                    return False
            
            # Secondary check: If we see typical API outage errors, check for upgrade in progress
            if is_api_outage_error:
                # Fallback check: Look for SDDC Manager upgrade in progress (less reliable)
                if self._is_upgrade_in_progress():
                    if self._api_outage_start_time is None:
                        self._api_outage_start_time = time.time()
                        _LOGGER.info("SDDC Manager upgrade detected (fallback), preserving last known state during API outage")
                    return True
                
                # Even without confirmed upgrade, temporarily preserve state for common outage errors
                # This provides better UX during brief API instabilities
                if not hasattr(self, '_temporary_outage_start'):
                    self._temporary_outage_start = time.time()
                    _LOGGER.info(f"Detected potential API outage ({error_str}), temporarily preserving state")
                    return True
                elif time.time() - self._temporary_outage_start < 300:  # 5 minutes max for temporary preservation
                    return True
                else:
                    _LOGGER.warning("Temporary state preservation timeout exceeded, resuming normal error handling")
                    delattr(self, '_temporary_outage_start')
                    return False
            
            # Reset temporary outage tracking for non-outage errors
            if hasattr(self, '_temporary_outage_start'):
                delattr(self, '_temporary_outage_start')
            
            return False
            
        except Exception as e:
            _LOGGER.error(f"Error in state preservation logic: {e}")
            return False
    
    async def get_active_domains(self):
        """Get active domains with caching."""
        domains_data = await self.vcf_client.api_request("/v1/domains")
        domains = []
        domain_counter = 1
        
        for domain_data in domains_data.get("elements", []):
            if domain_data.get("status") == "ACTIVE":
                vcf_domain = VCFDomain(domain_data, domain_counter)
                domains.append(vcf_domain)
                self._domain_cache[vcf_domain.id] = vcf_domain
                domain_counter += 1
                
        return domains
    
    async def fetch_upgrades_data(self):
        """Fetch VCF domain and update information with state preservation during outages."""
        _LOGGER.debug("VCF Coordinator refreshing domain and update data")
        
        if not self.vcf_client.vcf_url:
            _LOGGER.warning("VCF not configured with URL")
            return {"domains": [], "domain_updates": {}}

        try:
            # Get active domains
            domains = await self.get_active_domains()
            if not domains:
                _LOGGER.warning("No active domains found")
                return {"domains": [], "domain_updates": {}, "setup_failed": True}

            # Map SDDC managers to domains
            await self._map_sddc_managers(domains)
            
            # Check for updates
            domain_updates = await self._check_domain_updates(domains)
            
            # Store successful data
            current_data = {
                "domains": [domain.to_dict() for domain in domains],
                "domain_updates": domain_updates
            }
            self._last_successful_data = current_data
              # Reset outage tracking on successful fetch
            self._reset_temporary_outage_tracking()
            if self._api_outage_start_time and not self._is_upgrade_in_progress():
                _LOGGER.info("API connectivity restored, resuming normal operations")
                self._api_outage_start_time = None
                self._is_sddc_upgrade_in_progress = False
            
            return current_data
            
        except Exception as e:
            # Check if we should preserve state during expected outage
            if self._should_preserve_state(e):
                _LOGGER.debug(f"VCF update check temporarily unavailable during SDDC Manager upgrade: {e}")
                if self._last_successful_data:
                    _LOGGER.info("Preserving last known state during SDDC Manager upgrade API outage")
                    # Log what we're preserving for verification
                    domain_updates = self._last_successful_data.get("domain_updates", {})
                    for domain_id, domain_data in domain_updates.items():
                        domain_name = domain_data.get("domain_name", "Unknown")
                        current_version = domain_data.get("current_version", "Unknown")
                        update_status = domain_data.get("update_status", "Unknown")
                        next_release = domain_data.get("next_release", {})
                        next_version = next_release.get("version", "N/A") if next_release else "N/A"
                        
                        _LOGGER.debug(f"Preserving domain {domain_name}: status={update_status}, "
                                    f"current_version={current_version}, next_version={next_version}")
                    
                    return self._last_successful_data
                else:
                    _LOGGER.warning("No previous state to preserve during API outage")
            else:
                # Only log as ERROR if this is not an expected outage
                _LOGGER.error(f"Error in VCF update check workflow: {e}")
            
            return {"domains": [], "domain_updates": {}, "error": str(e)}
    
    async def _map_sddc_managers(self, domains):
        """Map SDDC managers to domains."""
        sddc_data = await self.vcf_client.api_request("/v1/sddc-managers")
        
        for domain in domains:
            for sddc in sddc_data.get("elements", []):
                if sddc.get("domain", {}).get("id") == domain.id:
                    domain.set_sddc_manager(sddc.get("id"), sddc.get("fqdn"))
                    break
    
    async def _check_domain_updates(self, domains):
        """Check for updates across all domains."""
        domain_updates = {}
        
        for domain in domains:
            try:
                # Get current version
                releases_data = await self.vcf_client.api_request("/v1/releases", params={"domainId": domain.id})
                current_version = releases_data.get("elements", [{}])[0].get("version") if releases_data.get("elements") else None
                
                if not current_version:
                    _LOGGER.warning(f"Domain {domain.name}: No current version found")
                    domain.set_update_info(None, "error")
                    domain_updates[domain.id] = domain.update_dict()
                    continue
                
                # Validate current version format
                from .utils import is_valid_version_format, validate_and_normalize_version
                is_valid, normalized_version, error_msg = validate_and_normalize_version(current_version)
                
                if not is_valid:
                    _LOGGER.error(f"Domain {domain.name}: Invalid current version '{current_version}': {error_msg}")
                    domain.set_update_info(current_version, "error")
                    domain_updates[domain.id] = domain.update_dict()
                    continue
                
                # Use normalized version for consistency
                current_version = normalized_version
                _LOGGER.debug(f"Domain {domain.name}: Validated and normalized current version to '{current_version}'")
                
                # Set current version on domain BEFORE calling find_applicable_releases
                domain.current_version = current_version
                _LOGGER.debug(f"Domain {domain.name}: Set current version to {current_version}")
                
                # Get future releases and find applicable ones
                future_releases_data = await self.vcf_client.api_request(f"/v1/releases/domains/{domain.id}/future-releases")
                future_releases = future_releases_data.get("elements", [])
                _LOGGER.debug(f"Domain {domain.name}: Retrieved {len(future_releases)} future releases")
                
                applicable_releases = domain.find_applicable_releases(future_releases)
                _LOGGER.info(f"Domain {domain.name}: Found {len(applicable_releases)} applicable releases")
                
                if applicable_releases:
                    # Validate the next release version format
                    next_release = applicable_releases[0]
                    next_version = next_release.get("version")
                    
                    is_next_valid, normalized_next_version, next_error_msg = validate_and_normalize_version(next_version)
                    if not is_next_valid:
                        _LOGGER.error(f"Domain {domain.name}: Invalid next release version '{next_version}': {next_error_msg}")
                        domain.set_update_info(current_version, "error")
                    else:
                        # Update the next release with normalized version for consistency
                        next_release["version"] = normalized_next_version
                        applicable_releases.sort(key=lambda x: version_tuple(x.get("version", "0.0.0.0")))
                        domain.set_update_info(current_version, "updates_available", applicable_releases[0])
                        _LOGGER.debug(f"Domain {domain.name}: Next release version validated and normalized to '{normalized_next_version}'")
                else:
                    domain.set_update_info(current_version, "up_to_date")
                
                domain_updates[domain.id] = domain.update_dict()
                
            except Exception as e:
                _LOGGER.error(f"Error checking updates for domain {domain.name}: {e}")
                domain.set_update_info(current_version if 'current_version' in locals() else None, "error")
                domain_updates[domain.id] = domain.update_dict()
        
        return domain_updates
    
    async def fetch_resources_data(self):
        """Fetch VCF domain resource information with state preservation during outages."""
        _LOGGER.info("VCF Resource Coordinator refreshing resource data")
        
        if not self.vcf_client.vcf_url:
            _LOGGER.warning("VCF not configured with URL")
            return {"domains": [], "domain_resources": {}}

        try:
            # Get active domains (simpler structure for resources)
            domains_data = await self.vcf_client.api_request("/v1/domains")
            active_domains = self._extract_active_domains(domains_data)
            
            if not active_domains:
                return {"domains": [], "domain_resources": {}, "setup_failed": True}

            # Get resource information for each domain
            domain_resources = await self._collect_domain_resources(active_domains)
            
            # Store successful data for potential preservation
            current_data = {
                "domains": active_domains,
                "domain_resources": domain_resources
            }
              # Update last successful data if this is not a preserved state fetch
            if not hasattr(self, '_last_successful_resource_data'):
                self._last_successful_resource_data = current_data
            elif not self._is_sddc_upgrade_in_progress:
                self._last_successful_resource_data = current_data
              # Reset temporary outage tracking on successful resource collection
            self._reset_temporary_outage_tracking()
            
            return current_data
            
        except Exception as e:
            # Check if we should preserve state during expected outage
            if self._should_preserve_state(e):
                _LOGGER.debug(f"VCF resource collection temporarily unavailable during SDDC Manager upgrade: {e}")
                if hasattr(self, '_last_successful_resource_data') and self._last_successful_resource_data:
                    _LOGGER.info("Preserving last known resource state during SDDC Manager upgrade API outage")
                    return self._last_successful_resource_data
                else:
                    _LOGGER.warning("No previous resource state to preserve during API outage")
            else:
                # Only log as ERROR if this is not an expected outage
                _LOGGER.error(f"Error in VCF resource collection workflow: {e}")
            
            return {"domains": [], "domain_resources": {}, "error": str(e)}
    
    def _extract_active_domains(self, domains_data):
        """Extract active domains with basic info."""
        active_domains = []
        domain_counter = 1
        
        for domain_data in domains_data.get("elements", []):
            if domain_data.get("status") == "ACTIVE":
                active_domains.append({
                    "id": domain_data.get("id"),
                    "name": domain_data.get("name"),
                    "status": domain_data.get("status"),
                    "prefix": f"domain{domain_counter}"
                })
                domain_counter += 1
        
        return active_domains
    
    async def _collect_domain_resources(self, active_domains):
        """Collect resource information for all domains."""
        domain_resources = {}
        
        for domain in active_domains:
            try:
                domain_id = domain["id"]
                domain_resource_data = await self._get_domain_resource_data(domain)
                domain_resources[domain_id] = domain_resource_data
                
            except Exception as e:
                # Use upgrade-aware error logging
                if self._should_preserve_state(e):
                    _LOGGER.debug(f"Silencing resource collection error for domain {domain['name']} during SDDC Manager upgrade: {e}")
                else:
                    _LOGGER.error(f"Error getting resource information for domain {domain['name']}: {e}")
                
                domain_resources[domain["id"]] = {
                    "domain_name": domain["name"],
                    "domain_prefix": domain["prefix"],
                    "error": str(e),
                    "capacity": {},
                    "clusters": []
                }
        
        return domain_resources
    
    async def _get_domain_resource_data(self, domain):
        """Get detailed resource data for a single domain."""
        domain_id = domain["id"]
        domain_details = await self.vcf_client.api_request(f"/v1/domains/{domain_id}")
        
        domain_resource_data = {
            "domain_name": domain["name"],
            "domain_prefix": domain["prefix"],
            "capacity": domain_details.get("capacity", {}),
            "clusters": []
        }
          # Process clusters
        clusters_info = domain_details.get("clusters", [])
        for cluster_ref in clusters_info:
            cluster_id = cluster_ref.get("id")
            if cluster_id:
                try:
                    cluster_data = await self._get_cluster_data(cluster_id)
                    domain_resource_data["clusters"].append(cluster_data)
                except Exception as e:
                    # Use upgrade-aware error logging
                    if self._should_preserve_state(e):
                        _LOGGER.debug(f"Silencing cluster data collection error for {cluster_id} during SDDC Manager upgrade: {e}")
                    else:
                        _LOGGER.error(f"Error getting cluster details for {cluster_id}: {e}")
        
        return domain_resource_data
    
    async def _get_cluster_data(self, cluster_id):
        """Get cluster data including hosts."""
        cluster_details = await self.vcf_client.api_request(f"/v1/clusters/{cluster_id}")
        
        cluster_data = {
            "id": cluster_id,
            "name": cluster_details.get("name", "Unknown"),
            "host_count": len(cluster_details.get("hosts", [])),
            "hosts": []
        }
          # Process hosts
        hosts_info = cluster_details.get("hosts", [])
        for host_ref in hosts_info:
            host_id = host_ref.get("id")
            if host_id:
                try:
                    host_data = await self._get_host_data(host_id)
                    cluster_data["hosts"].append(host_data)
                except Exception as e:
                    # Use upgrade-aware error logging
                    if self._should_preserve_state(e):
                        _LOGGER.debug(f"Silencing host data collection error for {host_id} during SDDC Manager upgrade: {e}")
                    else:
                        _LOGGER.error(f"Error getting host details for {host_id}: {e}")
        
        return cluster_data
    
    async def _get_host_data(self, host_id):
        """Get host resource data."""
        host_details = await self.vcf_client.api_request(f"/v1/hosts/{host_id}")
        
        fqdn = host_details.get("fqdn", "")
        hostname = fqdn.split(".")[0] if fqdn else "Unknown"
        
        # Extract resource information
        cpu_info = host_details.get("cpu", {})
        memory_info = host_details.get("memory", {})
        storage_info = host_details.get("storage", {})
        
        return {
            "id": host_id,
            "fqdn": fqdn,
            "hostname": hostname,
            "cpu": {
                "used_mhz": cpu_info.get("usedFrequencyMHz", 0),
                "total_mhz": cpu_info.get("frequencyMHz", 0),
                "cores": cpu_info.get("cores", 0)
            },
            "memory": {
                "used_mb": memory_info.get("usedCapacityMB", 0),
                "total_mb": memory_info.get("totalCapacityMB", 0)
            },
            "storage": {
                "used_mb": storage_info.get("usedCapacityMB", 0),
                "total_mb": storage_info.get("totalCapacityMB", 0)
            }
        }

    def _reset_temporary_outage_tracking(self):
        """Reset temporary outage tracking when normal operations resume."""
        if hasattr(self, '_temporary_outage_start'):
            outage_duration = time.time() - self._temporary_outage_start
            _LOGGER.info(f"API connectivity restored after {outage_duration:.1f} seconds, resuming normal operations")
            delattr(self, '_temporary_outage_start')
def get_coordinator(hass, config_entry):
    """Get the data update coordinator using OOP approach."""
    coordinator_manager = VCFCoordinatorManager(hass, config_entry)
    
    _LOGGER.debug(f"Initializing VCF coordinator with URL: {coordinator_manager.vcf_client.vcf_url}")

    # Create coordinators using the manager's methods
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="VCF Upgrades",
        update_method=coordinator_manager.fetch_upgrades_data,
        update_interval=timedelta(minutes=15),
    )
    
    resource_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="VCF Resources",
        update_method=coordinator_manager.fetch_resources_data,
        update_interval=timedelta(seconds=10),
    )    # Store both coordinators globally for other components
    hass.data.setdefault(_DOMAIN, {})["coordinator"] = coordinator
    hass.data.setdefault(_DOMAIN, {})["resource_coordinator"] = resource_coordinator
    hass.data.setdefault(_DOMAIN, {})["coordinator_manager"] = coordinator_manager
    
    # Store manager reference in coordinator for sensor access
    coordinator._coordinator_manager = coordinator_manager
    resource_coordinator._coordinator_manager = coordinator_manager
    
    _LOGGER.info(f"Created VCF coordinators - Upgrades: {coordinator.name}, Resources: {resource_coordinator.name}")
    _LOGGER.info(f"Resource coordinator update interval: {resource_coordinator.update_interval}")
    
    return coordinator

def get_resource_coordinator(hass, config_entry):
    """Get the resource data update coordinator."""
    return hass.data.get(_DOMAIN, {}).get("resource_coordinator")
