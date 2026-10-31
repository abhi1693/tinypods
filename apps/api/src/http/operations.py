"""Stable operation identifiers independent of function names and file locations."""

import re


def build_operation_id(domain: str, resource: str, action: str) -> str:
    """Build a lowerCamelCase domain/resource/action identifier.

    Args:
        domain: Owning API area, such as auth or health.
        resource: Stable resource or flow name, such as session or zitadelLogin.
        action: Behavior, such as get, list, login, logout, start or callback.

    Returns:
        Stable identifier used by generated API clients.

    Raises:
        ValueError: A component is empty or is not a lowerCamelCase identifier.
    """
    for component in (domain, resource, action):
        if not re.fullmatch(r"[a-z][A-Za-z0-9]*", component):
            raise ValueError("Operation ID components must use lowerCamelCase")
    return domain + resource[0].upper() + resource[1:] + action[0].upper() + action[1:]
