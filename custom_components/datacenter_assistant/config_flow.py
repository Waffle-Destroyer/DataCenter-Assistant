from homeassistant import config_entries
import voluptuous as vol
import logging
import aiohttp
import time
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from . import DOMAIN
from .utils import validate_vcf_url, build_vcf_api_url, normalize_vcf_url

_LOGGER = logging.getLogger(__name__)

class DataCenterAssistantConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DataCenter Assistant."""

    def __init__(self):
        """Initialize config flow."""
        super().__init__()
        self._failed_attempts = 0

    async def _test_credentials(self, vcf_url, username, password):
        """Test VCF credentials by attempting to get a token."""
        try:
            session = async_get_clientsession(self.hass)
            login_url = build_vcf_api_url(vcf_url, "/v1/tokens")
            
            auth_data = {
                "username": username,
                "password": password
            }
            
            _LOGGER.debug(f"Testing credentials for URL: {login_url}")
            
            async with session.post(login_url, json=auth_data, ssl=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    token_data = await resp.json()
                    token = token_data.get("accessToken") or token_data.get("access_token")
                    if token:
                        _LOGGER.info("Credential validation successful")
                        return True, None
                    else:
                        _LOGGER.warning("Token not found in response")
                        return False, "invalid_credentials"
                elif resp.status == 401:
                    _LOGGER.warning("Invalid credentials provided")
                    return False, "invalid_credentials"
                elif resp.status == 403:
                    _LOGGER.warning("Access forbidden - possible IP ban")
                    return False, "access_forbidden"
                else:
                    error_text = await resp.text()
                    _LOGGER.error(f"Credential test failed with status {resp.status}: {error_text}")
                    return False, "connection_error"
                    
        except aiohttp.ClientError as e:
            _LOGGER.error(f"Connection error during credential test: {e}")
            return False, "connection_error"
        except Exception as e:
            _LOGGER.error(f"Unexpected error during credential test: {e}")
            return False, "unknown_error"

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        _LOGGER.debug("DataCenter Assistant config flow started")
        errors = {}
        
        if user_input is not None:
            _LOGGER.info("Processing DataCenter Assistant configuration submission")
            _LOGGER.debug(f"Received config data with URL: {user_input.get('vcf_url', 'NOT_PROVIDED')}")
            
            # Validate and normalize VCF URL
            vcf_url = user_input.get('vcf_url', '').strip()
            if not vcf_url:
                _LOGGER.warning("Missing VCF URL")
                errors['vcf_url'] = "missing_vcf_url"
            else:
                is_valid, normalized_url, error_msg = validate_vcf_url(vcf_url)
                if not is_valid:
                    _LOGGER.warning(f"Invalid VCF URL '{vcf_url}': {error_msg}")
                    errors['vcf_url'] = "invalid_vcf_url"
                else:
                    # Update user input with normalized URL
                    user_input['vcf_url'] = normalized_url
                    _LOGGER.info(f"VCF URL normalized from '{vcf_url}' to '{normalized_url}'")
              # Validate other required fields
            required_fields = {
                "vcf_username": "missing_vcf_username", 
                "vcf_password": "missing_vcf_password"
            }
            
            for field, error_key in required_fields.items():
                if not user_input.get(field, "").strip():
                    _LOGGER.warning(f"Missing required field: {field}")
                    errors[field] = error_key
            
            if not errors:
                # Test credentials before creating entry
                _LOGGER.info("Testing VCF credentials...")
                is_valid, error_type = await self._test_credentials(
                    user_input['vcf_url'],
                    user_input['vcf_username'], 
                    user_input['vcf_password']
                )
                
                if is_valid:
                    _LOGGER.info("DataCenter Assistant configuration validation successful")
                    _LOGGER.debug(f"Creating config entry for VCF URL: {user_input.get('vcf_url')}")
                    # Reset failed attempts on success
                    self._failed_attempts = 0
                    return self.async_create_entry(title="DataCenter Assistant", data=user_input)
                else:
                    self._failed_attempts += 1
                    _LOGGER.warning(f"Credential validation failed (attempt {self._failed_attempts}): {error_type}")
                    
                    if error_type == "access_forbidden":
                        errors['base'] = "access_forbidden"
                    elif error_type == "invalid_credentials":
                        if self._failed_attempts >= 3:
                            errors['base'] = "too_many_failed_attempts"
                        else:
                            errors['base'] = "invalid_credentials"
                    elif error_type == "connection_error":
                        errors['base'] = "connection_error"
                    else:
                        errors['base'] = "unknown_error"
            else:
                _LOGGER.warning(f"Configuration validation failed with errors: {list(errors.keys())}")

        # Show the form with any errors
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("vcf_url"): str,
                vol.Required("vcf_username"): str,
                vol.Required("vcf_password"): str,
            }),
            errors=errors
        )
