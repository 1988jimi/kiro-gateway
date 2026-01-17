# -*- coding: utf-8 -*-
"""
Kiro Gateway Login Module.

Provides authentication functionality:
- Device Code Flow (AWS OIDC)
- Social Login (Google/GitHub)
- Token refresh and management
- Background token refresh scheduler
"""

from .credential_types import KiroCredentials, CredentialStatus
from .device_flow import (
    start_device_flow,
    poll_device_flow,
    cancel_device_flow,
    get_login_state,
)
from .social_auth import (
    start_social_auth,
    exchange_social_auth_token,
    cancel_social_auth,
    get_social_auth_state,
)
from .refresher import TokenRefresher
from .scheduler import BackgroundScheduler, scheduler
from .routes_auth import router as auth_router

__all__ = [
    "KiroCredentials",
    "CredentialStatus",
    "start_device_flow",
    "poll_device_flow",
    "cancel_device_flow",
    "get_login_state",
    "start_social_auth",
    "exchange_social_auth_token",
    "cancel_social_auth",
    "get_social_auth_state",
    "TokenRefresher",
    "BackgroundScheduler",
    "scheduler",
    "auth_router",
]
