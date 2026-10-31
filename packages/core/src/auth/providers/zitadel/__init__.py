"""Supported Zitadel OIDC adapter."""

from tinypods_core.auth.contracts import VerifiedIdentity
from tinypods_core.auth.providers.zitadel.oidc import ZitadelOIDC

__all__ = ["VerifiedIdentity", "ZitadelOIDC"]
