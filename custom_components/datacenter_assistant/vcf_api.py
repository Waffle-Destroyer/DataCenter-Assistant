"""VCF API Client and Data Models for the DataCenter Assistant integration."""
import aiohttp
import asyncio
import logging
import time
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .utils import version_tuple, build_vcf_api_url, normalize_vcf_url

_LOGGER = logging.getLogger(__name__)


class VCFAPIClient:
    """Centralized VCF API client to handle all VCF operations."""
    
    def __init__(self, hass, config_entry):
        self.hass = hass
        self.config_entry = config_entry
        # Normalize VCF URL to ensure proper format
        raw_url = config_entry.data.get("vcf_url")
        self.vcf_url = normalize_vcf_url(raw_url) if raw_url else None
        self.vcf_username = config_entry.data.get("vcf_username", "")
        self.vcf_password = config_entry.data.get("vcf_password", "")
        
        # Track API outage state for graceful handling during upgrades
        self._is_sddc_upgrade_in_progress = False
        self._api_outage_start_time = None
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
            _LOGGER.info(f"VCF API Client: Received API outage notification for domain {domain_id} due to SDDC Manager upgrade")
            self._is_sddc_upgrade_in_progress = True
            self._api_outage_start_time = time.time()
    
    def _handle_api_restored(self, event):
        """Handle notification of API restoration."""
        reason = event.data.get("reason", "unknown")
        domain_id = event.data.get("domain_id", "unknown")
        
        _LOGGER.info(f"VCF API Client: Received API restoration notification for domain {domain_id}, reason: {reason}")
        self._is_sddc_upgrade_in_progress = False
        self._api_outage_start_time = None
        
        # Immediately refresh token when API comes back online to ensure it's valid
        if reason.startswith("sddc_manager_upgrade"):
            _LOGGER.info("VCF API Client: Scheduling immediate token refresh after SDDC Manager upgrade")
            # Schedule token refresh on the event loop
            async def refresh_token_async():
                try:
                    await self.refresh_token()
                    _LOGGER.info("VCF API Client: Token refreshed successfully after API restoration")
                except Exception as e:
                    _LOGGER.warning(f"VCF API Client: Failed to refresh token after API restoration: {e}")
            
            if hasattr(self.hass, 'loop') and self.hass.loop.is_running():
                self.hass.loop.call_soon_threadsafe(
                    lambda: self.hass.async_create_task(refresh_token_async())
                )
            else:
                # Fallback for non-async context
                pass
    
    def _is_upgrade_in_progress(self):
        """Check if any domain has an SDDC Manager upgrade in progress."""
        try:
            # Primary check: Are we in a known SDDC upgrade state via events?
            if self._is_sddc_upgrade_in_progress and self._api_outage_start_time:
                # Check if we're within the timeout window
                if time.time() - self._api_outage_start_time < self._outage_timeout:
                    return True
                else:
                    _LOGGER.warning("VCF API Client: API outage timeout exceeded, resuming normal operations")
                    self._api_outage_start_time = None
                    self._is_sddc_upgrade_in_progress = False
                    return False
            
            # Fallback check: Look for SDDC Manager upgrade in progress via upgrade service
            upgrade_service = self.hass.data.get("datacenter_assistant", {}).get("upgrade_service")
            if not upgrade_service:
                return False
            
            # Check all domains for SDDC Manager upgrade status
            for domain_id in upgrade_service._upgrade_states:
                status = upgrade_service.get_upgrade_status(domain_id)
                if status == "upgrading_sddcmanager":
                    if self._api_outage_start_time is None:
                        self._api_outage_start_time = time.time()
                        _LOGGER.info("VCF API Client: SDDC Manager upgrade detected (fallback), enabling graceful API handling")
                    return True
            
            return False
        except Exception as e:
            _LOGGER.debug(f"VCF API Client: Error checking upgrade status: {e}")
            return False
    
    def _should_silence_token_refresh(self):
        """Determine if token refresh should be silenced during expected API outage."""
        return self._is_upgrade_in_progress()
    
    async def get_session_with_headers(self):
        """Get session with current authentication headers."""
        current_token = self.config_entry.data.get("vcf_token")
        current_expiry = self.config_entry.data.get("token_expiry", 0)
        
        # Check if token expires in less than 10 minutes
        if current_expiry > 0 and time.time() > current_expiry - 600:
            # Always attempt proactive token refresh - refresh_token() handles upgrade silencing
            _LOGGER.info("VCF token will expire soon, refreshing proactively")
            current_token = await self.refresh_token()
        
        session = async_get_clientsession(self.hass)
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Accept": "application/json"
        }
        return session, headers
    
    async def refresh_token(self):
        """Refresh VCF API token with upgrade-aware handling."""
        if not self.vcf_url or not self.vcf_username or not self.vcf_password:
            _LOGGER.warning("Cannot refresh VCF token: Missing credentials")
            return None
            
        # Always attempt token refresh, just handle errors differently during upgrades
        try:
            session = async_get_clientsession(self.hass)
            login_url = build_vcf_api_url(self.vcf_url, "/v1/tokens")
            
            auth_data = {
                "username": self.vcf_username,
                "password": self.vcf_password
            }
            
            async with session.post(login_url, json=auth_data, ssl=False) as resp:
                if resp.status != 200:
                    # Check if this is during an upgrade before logging error
                    if self._is_upgrade_in_progress():
                        _LOGGER.debug(f"VCF token refresh silently failed during SDDC Manager upgrade: {resp.status}")
                    else:
                        _LOGGER.error(f"VCF token refresh failed: {resp.status}")
                    return None
                    
                token_data = await resp.json()
                new_token = token_data.get("accessToken") or token_data.get("access_token")
                
                if new_token:
                    # Update the configuration with new token and expiry time
                    new_data = dict(self.config_entry.data)
                    new_data["vcf_token"] = new_token
                    
                    expiry = int(time.time()) + 3600  # 1 hour in seconds
                    new_data["token_expiry"] = expiry
                    _LOGGER.info(f"New token will expire at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry))}")
                    
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, 
                        data=new_data
                    )
                    return new_token
                else:
                    _LOGGER.warning("Could not extract token from response")
                    return None
        except Exception as e:
            # Use upgrade-aware error logging
            if self._should_silence_token_refresh():
                _LOGGER.debug(f"VCF token refresh failed during SDDC Manager upgrade (expected): {e}")
            else:
                _LOGGER.error(f"Error refreshing VCF token: {e}")
            return None
    
    async def api_request(self, endpoint, method="GET", data=None, params=None, timeout=None):
        """Make a VCF API request with automatic token handling and upgrade-aware error handling."""
        if not self.vcf_url:
            raise ValueError("VCF URL not configured")
        
        session, headers = await self.get_session_with_headers()
        url = build_vcf_api_url(self.vcf_url, endpoint)
        
        _LOGGER.debug(f"Making VCF API request to: {url}")
        
        # Set up timeout - use longer timeout for bundle operations
        if timeout is None:
            if "/bundles/" in endpoint:
                # Bundle operations can take longer due to large file downloads
                timeout = aiohttp.ClientTimeout(total=300)  # 5 minutes
            else:
                # Standard timeout for other operations
                timeout = aiohttp.ClientTimeout(total=60)  # 1 minute
        
        try:
            async with getattr(session, method.lower())(
                url, headers=headers, json=data, params=params, ssl=False, timeout=timeout
            ) as resp:
                if resp.status == 401:
                    # Try refreshing token once
                    if self._should_silence_token_refresh():
                        _LOGGER.debug("Token expired during SDDC Manager upgrade, silencing token refresh attempt")
                        raise aiohttp.ClientError("API unavailable during SDDC Manager upgrade")
                    
                    _LOGGER.info("Token expired, refreshing...")
                    new_token = await self.refresh_token()
                    if new_token:
                        headers["Authorization"] = f"Bearer {new_token}"
                        async with getattr(session, method.lower())(
                            url, headers=headers, json=data, params=params, ssl=False, timeout=timeout
                        ) as retry_resp:
                            if retry_resp.status not in [200, 202, 204]:
                                error_text = await retry_resp.text()
                                # Check if this is during an upgrade before logging error
                                if self._is_upgrade_in_progress():
                                    _LOGGER.debug(f"API request silently failed during SDDC Manager upgrade after token refresh: {retry_resp.status} for URL: {url}")
                                else:
                                    _LOGGER.error(f"API request failed after token refresh: {retry_resp.status} - {error_text} for URL: {url}")
                                raise aiohttp.ClientError(f"API request failed: {retry_resp.status}")
                            
                            # Try to parse as JSON, but handle empty responses for PATCH/PUT/DELETE
                            try:
                                return await retry_resp.json()
                            except aiohttp.ContentTypeError:
                                if method.upper() in ['PATCH', 'PUT', 'DELETE'] and retry_resp.status in [200, 202, 204]:
                                    return {"status": "success", "message": f"Operation completed with status {retry_resp.status}"}
                                else:
                                    raise
                    else:
                        raise aiohttp.ClientError("Failed to refresh token")
                elif resp.status not in [200, 202, 204]:
                    error_text = await resp.text()
                    # Check if this is during an upgrade before logging error
                    if self._is_upgrade_in_progress():
                        _LOGGER.debug(f"API request silently failed during SDDC Manager upgrade: {resp.status} for URL: {url}")
                    else:
                        _LOGGER.error(f"API request failed: {resp.status} - {error_text} for URL: {url}")
                    raise aiohttp.ClientError(f"API request failed: {resp.status}")
                else:
                    # Try to parse as JSON, but handle empty responses for PATCH/PUT/DELETE
                    try:
                        return await resp.json()
                    except aiohttp.ContentTypeError:
                        if method.upper() in ['PATCH', 'PUT', 'DELETE'] and resp.status in [200, 202, 204]:
                            return {"status": "success", "message": f"Operation completed with status {resp.status}"}
                        else:
                            raise
        except aiohttp.ClientError:
            # Re-raise ClientError as-is for coordinator handling
            raise
        except asyncio.TimeoutError as e:
            # Handle timeout errors specifically - these may be expected for long-running operations
            timeout_msg = f"Connection timeout to host {url}"
            if self._is_upgrade_in_progress():
                _LOGGER.debug(f"API request timeout during SDDC Manager upgrade: {timeout_msg}")
            else:
                _LOGGER.warning(f"API request timeout (this may be expected for long-running operations): {timeout_msg}")
            raise aiohttp.ClientError(timeout_msg)
        except Exception as e:
            # Handle other exceptions (connection errors, etc.)
            if self._is_upgrade_in_progress():
                _LOGGER.debug(f"API request silently failed during SDDC Manager upgrade: {e}")
            else:
                _LOGGER.error(f"API request failed: {e}")
            raise aiohttp.ClientError(f"API request failed: {e}")


class VCFDomain:
    """Data model for VCF Domain with business logic."""
    
    def __init__(self, domain_data, domain_counter=1):
        self.id = domain_data.get("id")
        self.name = domain_data.get("name")
        self.status = domain_data.get("status")
        self.prefix = f"domain{domain_counter}"
        self.sddc_manager_id = None
        self.sddc_manager_fqdn = None
        self.current_version = None
        self.update_status = "unknown"
        self.next_release = None
    
    def set_sddc_manager(self, sddc_id, sddc_fqdn):
        """Set SDDC manager information."""
        self.sddc_manager_id = sddc_id
        self.sddc_manager_fqdn = sddc_fqdn
    
    def set_update_info(self, current_version, update_status, next_release=None):
        """Set update information."""
        self.current_version = current_version
        self.update_status = update_status
        self.next_release = next_release
    
    def find_applicable_releases(self, future_releases):
        """Find applicable releases for this domain."""
        if not self.current_version:
            _LOGGER.warning(f"Domain {self.name}: No current version set, cannot find applicable releases")
            return []
        
        _LOGGER.debug(f"Domain {self.name}: Finding applicable releases from {len(future_releases)} future releases")
        applicable_releases = []
        
        for release in future_releases:
            applicability_status = release.get("applicabilityStatus")
            is_applicable = release.get("isApplicable", False)
            release_version = release.get("version")
            min_compatible_version = release.get("minCompatibleVcfVersion")
            
            _LOGGER.debug(f"Domain {self.name}: Evaluating release {release_version}: "
                        f"status={applicability_status}, applicable={is_applicable}, "
                        f"minCompatible={min_compatible_version}")
            
            if (applicability_status == "APPLICABLE" and 
                is_applicable and 
                release_version and 
                min_compatible_version):
                
                try:
                    current_tuple = version_tuple(self.current_version)
                    release_tuple = version_tuple(release_version)
                    min_compatible_tuple = version_tuple(min_compatible_version)
                    
                    # Check if any version parsing failed
                    if current_tuple is None:
                        _LOGGER.warning(f"Domain {self.name}: Invalid current version format '{self.current_version}', skipping version comparison")
                        continue
                    if release_tuple is None:
                        _LOGGER.warning(f"Domain {self.name}: Invalid release version format '{release_version}', skipping this release")
                        continue
                    if min_compatible_tuple is None:
                        _LOGGER.warning(f"Domain {self.name}: Invalid min compatible version format '{min_compatible_version}', skipping this release")
                        continue
                    
                    if release_tuple > current_tuple >= min_compatible_tuple:
                        applicable_releases.append(release)
                        _LOGGER.debug(f"Domain {self.name}: Release {release_version} is applicable")
                    else:
                        _LOGGER.debug(f"Domain {self.name}: Release {release_version} does not meet version criteria: "
                                    f"{release_version} > {self.current_version} >= {min_compatible_version}")
                
                except Exception as ve:
                    _LOGGER.warning(f"Domain {self.name}: Error comparing versions for release {release_version}: {ve}")
                    continue
            else:
                _LOGGER.debug(f"Domain {self.name}: Release {release_version} does not meet applicability criteria")
        
        _LOGGER.info(f"Domain {self.name}: Found {len(applicable_releases)} applicable releases")
        return applicable_releases
    
    def to_dict(self):
        """Convert to dictionary for coordinator data."""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "prefix": self.prefix,
            "sddc_manager_id": self.sddc_manager_id,
            "sddc_manager_fqdn": self.sddc_manager_fqdn
        }
    
    def update_dict(self):
        """Convert to dictionary for domain updates."""
        return {
            "domain_name": self.name,
            "domain_prefix": self.prefix,
            "current_version": self.current_version,
            "update_status": self.update_status,
            "next_release": self.next_release
        }
