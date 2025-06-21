"""Utility functions for the DataCenter Assistant integration."""
import logging
import aiohttp

_LOGGER = logging.getLogger(__name__)

# Icon mappings for different resource types
RESOURCE_ICONS = {
    "cpu": "mdi:cpu-64-bit",
    "memory": "mdi:memory",
    "storage": "mdi:harddisk",
    "default": "mdi:server"
}

def get_resource_icon(resource_type):
    """Get the appropriate icon for a resource type."""
    return RESOURCE_ICONS.get(resource_type, RESOURCE_ICONS["default"])

def truncate_description(text, max_length=61):
    """Truncate description text to max_length characters + '...' if needed."""
    if not text or not isinstance(text, str):
        return text
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."

def is_valid_version_format(version_string):
    """Validate if version string follows proper X.X.X.X format."""
    if not version_string or not isinstance(version_string, str):
        return False
    
    parts = version_string.split('.')
    # Must have at least 1 part, max 4 parts
    if len(parts) < 1 or len(parts) > 4:
        return False
    
    # All parts must be numeric
    for part in parts:
        if not part.isdigit():
            return False
    
    return True

def version_tuple(version_string):
    """Convert version string to tuple for comparison.
    
    Returns:
        tuple: Integer tuple for valid versions, None for invalid versions
    """
    if not version_string:
        _LOGGER.debug("Empty version string provided, returning default (0,0,0,0)")
        return (0, 0, 0, 0)
    
    # Validate version format first
    if not is_valid_version_format(version_string):
        _LOGGER.warning(f"Invalid version format '{version_string}': Must follow X.X.X.X pattern with numeric parts")
        return None
    
    parts = version_string.split('.')
    # Normalize to 4 parts
    while len(parts) < 4:
        parts.append('0')
    
    try:
        result = tuple(map(int, parts[:4]))
        _LOGGER.debug(f"Converted version '{version_string}' to tuple {result}")
        return result
    except ValueError as e:
        # This should not happen due to pre-validation, but just in case
        _LOGGER.error(f"Unexpected error converting version '{version_string}' to tuple: {e}")
        return None

def safe_name_conversion(name):
    """Convert domain/host names to safe entity names."""
    return name.lower().replace(' ', '_').replace('-', '_')

async def make_vcf_api_request(session, url, headers, retry_refresh_func=None):
    """Make a VCF API request with automatic token refresh on 401."""
    _LOGGER.debug(f"Making VCF API request to: {url}")
    
    try:
        async with session.get(url, headers=headers, ssl=False) as resp:
            if resp.status == 401 and retry_refresh_func:
                _LOGGER.info("Token expired, refreshing...")
                new_token = await retry_refresh_func()
                if new_token:
                    headers["Authorization"] = f"Bearer {new_token}"
                    _LOGGER.debug("Retrying request with new token")
                    # Retry with new token
                    async with session.get(url, headers=headers, ssl=False) as retry_resp:
                        if retry_resp.status == 200:
                            _LOGGER.debug(f"Request successful after token refresh")
                            return retry_resp.status, await retry_resp.json()
                        else:
                            _LOGGER.error(f"Request failed even after token refresh: {retry_resp.status}")
                            return retry_resp.status, None
                else:
                    _LOGGER.error("Failed to refresh token")
                    raise aiohttp.ClientError("Failed to refresh token")
            elif resp.status != 200:
                _LOGGER.error(f"API request failed: {resp.status} for URL: {url}")
                raise aiohttp.ClientError(f"API request failed: {resp.status}")
            else:
                _LOGGER.debug(f"Request successful: {resp.status}")
                return resp.status, await resp.json()
    except Exception as e:
        _LOGGER.error(f"Error making API request to {url}: {e}")
        raise

def create_base_entity_attributes(domain_id, domain_name, domain_prefix):
    """Create base attributes for all VCF entities."""
    return {
        "domain_id": domain_id,
        "domain_name": domain_name, 
        "domain_prefix": domain_prefix
    }

def validate_and_normalize_version(version_string):
    """Validate and normalize a version string.
    
    Args:
        version_string: The version string to validate
        
    Returns:
        tuple: (is_valid, normalized_version, error_message)
    """
    if not version_string or not isinstance(version_string, str):
        return False, None, "Version string is empty or not a string"
    
    # Remove whitespace
    version_string = version_string.strip()
    
    if not version_string:
        return False, None, "Version string is empty after trimming"
    
    parts = version_string.split('.')
    
    # Must have at least 1 part, max 4 parts
    if len(parts) < 1:
        return False, None, "Version string has no parts"
    
    if len(parts) > 4:
        return False, None, f"Version string has too many parts ({len(parts)}), maximum is 4"
    
    # All parts must be numeric
    for i, part in enumerate(parts):
        if not part.isdigit():
            return False, None, f"Part {i+1} '{part}' is not numeric"
        
        # Check for reasonable bounds (0-9999)
        try:
            part_int = int(part)
            if part_int < 0 or part_int > 9999:
                return False, None, f"Part {i+1} '{part}' is out of reasonable range (0-9999)"
        except ValueError:
            return False, None, f"Part {i+1} '{part}' cannot be converted to integer"
    
    # Normalize to 4 parts
    while len(parts) < 4:
        parts.append('0')
    
    normalized = '.'.join(parts[:4])
    return True, normalized, None
