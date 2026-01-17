# -*- coding: utf-8 -*-
"""
Token Refresher for Kiro Gateway.

Handles automatic token refresh for both Social Auth and AWS SSO OIDC.
"""

import hashlib
import time
import httpx
from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional

from loguru import logger

from .credential_types import KiroCredentials


def generate_machine_id(profile_arn: Optional[str] = None, client_id: Optional[str] = None) -> str:
    """
    Generate unique machine ID for fingerprinting.
    
    Priority: profileArn > clientId > system hardware ID
    """
    import socket
    import getpass
    
    # Get unique key
    unique_key = profile_arn or client_id
    if not unique_key:
        try:
            hostname = socket.gethostname()
            username = getpass.getuser()
            unique_key = f"{hostname}-{username}-kiro-gateway"
        except Exception:
            unique_key = "KIRO_DEFAULT_MACHINE"
    
    # Add hourly time factor to avoid static fingerprint
    hour_slot = int(time.time()) // 3600
    
    hasher = hashlib.sha256()
    hasher.update(unique_key.encode())
    hasher.update(hour_slot.to_bytes(8, 'little'))
    
    return hasher.hexdigest()


def get_kiro_version() -> str:
    """Get Kiro IDE version string."""
    return "0.7.45"


class TokenRefresher:
    """Token refresher for Kiro credentials."""
    
    def __init__(self, credentials: KiroCredentials):
        self.credentials = credentials
    
    def get_refresh_url(self) -> str:
        """Get refresh URL based on auth method."""
        region = self.credentials.region or "us-east-1"
        auth_method = (self.credentials.auth_method or "social").lower()
        
        if auth_method == "idc":
            return f"https://oidc.{region}.amazonaws.com/token"
        else:
            return f"https://prod.{region}.auth.desktop.kiro.dev/refreshToken"
    
    def validate_refresh_token(self) -> Tuple[bool, str]:
        """Validate refresh_token."""
        refresh_token = self.credentials.refresh_token
        
        if not refresh_token:
            return False, "Missing refresh_token"
        
        if len(refresh_token.strip()) == 0:
            return False, "refresh_token is empty"
        
        if len(refresh_token) < 100 or refresh_token.endswith("..."):
            return False, f"refresh_token appears truncated (length: {len(refresh_token)})"
        
        return True, ""
    
    def _get_machine_id(self) -> str:
        """Get Machine ID."""
        return generate_machine_id(
            self.credentials.profile_arn,
            self.credentials.client_id
        )
    
    async def refresh(self) -> Tuple[bool, str]:
        """
        Refresh token.
        
        Returns:
            (success, new_token_or_error)
        """
        is_valid, error = self.validate_refresh_token()
        if not is_valid:
            return False, error
        
        refresh_url = self.get_refresh_url()
        auth_method = (self.credentials.auth_method or "social").lower()
        
        machine_id = self._get_machine_id()
        kiro_version = get_kiro_version()
        
        try:
            async with httpx.AsyncClient(verify=False, timeout=30) as client:
                if auth_method == "idc":
                    # AWS SSO OIDC refresh
                    if not self.credentials.client_id or not self.credentials.client_secret:
                        return False, "IdC auth requires client_id and client_secret"
                    
                    body = {
                        "refreshToken": self.credentials.refresh_token,
                        "clientId": self.credentials.client_id,
                        "clientSecret": self.credentials.client_secret,
                        "grantType": "refresh_token"
                    }
                    headers = {
                        "Content-Type": "application/json",
                        "x-amz-user-agent": f"aws-sdk-js/3.738.0 KiroIDE-{kiro_version}-{machine_id}",
                        "User-Agent": "node",
                    }
                else:
                    # Social auth refresh
                    body = {"refreshToken": self.credentials.refresh_token}
                    headers = {
                        "Content-Type": "application/json",
                        "User-Agent": f"KiroIDE-{kiro_version}-{machine_id}",
                        "Accept": "application/json, text/plain, */*",
                    }
                
                logger.debug(f"Refreshing token via {auth_method}...")
                resp = await client.post(refresh_url, json=body, headers=headers)
                
                if resp.status_code != 200:
                    error_text = resp.text
                    if resp.status_code == 401:
                        return False, "Credentials expired or invalid, please re-login"
                    elif resp.status_code == 429:
                        return False, "Rate limited, please try again later"
                    else:
                        return False, f"Refresh failed: {resp.status_code} - {error_text[:200]}"
                
                data = resp.json()
                
                new_token = data.get("accessToken") or data.get("access_token")
                if not new_token:
                    return False, "Response missing access_token"
                
                # Update credentials
                self.credentials.access_token = new_token
                
                if rt := data.get("refreshToken") or data.get("refresh_token"):
                    self.credentials.refresh_token = rt
                
                if arn := data.get("profileArn"):
                    self.credentials.profile_arn = arn
                
                if expires_in := data.get("expiresIn") or data.get("expires_in"):
                    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                    self.credentials.expires_at = expires_at.isoformat()
                
                self.credentials.last_refresh = datetime.now(timezone.utc).isoformat()
                
                logger.info(f"Token refreshed successfully, expires: {self.credentials.expires_at}")
                return True, new_token
                
        except Exception as e:
            return False, f"Refresh exception: {str(e)}"
