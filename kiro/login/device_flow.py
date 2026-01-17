# -*- coding: utf-8 -*-
"""
Device Code Flow for Kiro Gateway.

Implements AWS OIDC Device Authorization Flow:
1. Register OIDC client -> get clientId + clientSecret
2. Start device authorization -> get deviceCode + userCode + verificationUri
3. User enters userCode in browser to complete authorization
4. Poll for Token -> get accessToken + refreshToken
"""

import time
import httpx
from dataclasses import dataclass
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta

from loguru import logger


@dataclass
class DeviceFlowState:
    """Device authorization flow state."""
    client_id: str
    client_secret: str
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: int
    region: str
    started_at: float


# Global login state
_login_state: Optional[DeviceFlowState] = None

# Kiro OIDC configuration
KIRO_START_URL = "https://view.awsapps.com/start"
KIRO_SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
    "codewhisperer:transformations",
    "codewhisperer:taskassist",
]


def get_login_state() -> Optional[dict]:
    """Get current login state."""
    global _login_state
    if _login_state is None:
        return None
    
    # Check if expired
    if time.time() > _login_state.expires_at:
        _login_state = None
        return None
    
    return {
        "user_code": _login_state.user_code,
        "verification_uri": _login_state.verification_uri,
        "expires_in": int(_login_state.expires_at - time.time()),
        "interval": _login_state.interval,
    }


async def start_device_flow(region: str = "us-east-1") -> Tuple[bool, dict]:
    """
    Start device authorization flow.
    
    Returns:
        (success, result_or_error)
    """
    global _login_state
    
    oidc_base = f"https://oidc.{region}.amazonaws.com"
    
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        # Step 1: Register OIDC client
        logger.info("Device Flow Step 1: Registering OIDC client...")
        
        reg_body = {
            "clientName": "Kiro Gateway",
            "clientType": "public",
            "scopes": KIRO_SCOPES,
            "grantTypes": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"],
            "issuerUrl": KIRO_START_URL
        }
        
        try:
            reg_resp = await client.post(
                f"{oidc_base}/client/register",
                json=reg_body,
                headers={"Content-Type": "application/json"}
            )
        except Exception as e:
            return False, {"error": f"Client registration request failed: {e}"}
        
        if reg_resp.status_code != 200:
            return False, {"error": f"Client registration failed: {reg_resp.text}"}
        
        reg_data = reg_resp.json()
        client_id = reg_data.get("clientId")
        client_secret = reg_data.get("clientSecret")
        
        if not client_id or not client_secret:
            return False, {"error": "Registration response missing clientId or clientSecret"}
        
        logger.info(f"Client registered successfully: {client_id[:20]}...")
        
        # Step 2: Start device authorization
        logger.info("Device Flow Step 2: Starting device authorization...")
        
        auth_body = {
            "clientId": client_id,
            "clientSecret": client_secret,
            "startUrl": KIRO_START_URL
        }
        
        try:
            auth_resp = await client.post(
                f"{oidc_base}/device_authorization",
                json=auth_body,
                headers={"Content-Type": "application/json"}
            )
        except Exception as e:
            return False, {"error": f"Device authorization request failed: {e}"}
        
        if auth_resp.status_code != 200:
            return False, {"error": f"Device authorization failed: {auth_resp.text}"}
        
        auth_data = auth_resp.json()
        device_code = auth_data.get("deviceCode")
        user_code = auth_data.get("userCode")
        verification_uri = auth_data.get("verificationUriComplete") or auth_data.get("verificationUri")
        interval = auth_data.get("interval", 5)
        expires_in = auth_data.get("expiresIn", 600)
        
        if not device_code or not user_code or not verification_uri:
            return False, {"error": "Device authorization response missing required fields"}
        
        logger.info(f"Device code obtained: {user_code}")
        
        # Save state
        _login_state = DeviceFlowState(
            client_id=client_id,
            client_secret=client_secret,
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            interval=interval,
            expires_at=int(time.time() + expires_in),
            region=region,
            started_at=time.time()
        )
        
        return True, {
            "user_code": user_code,
            "verification_uri": verification_uri,
            "expires_in": expires_in,
            "interval": interval,
        }


async def poll_device_flow() -> Tuple[bool, dict]:
    """
    Poll device authorization status.
    
    Returns:
        (success, result_or_error)
        - success=True, result={"completed": True, "credentials": {...}} - authorization complete
        - success=True, result={"completed": False, "status": "pending"} - waiting
        - success=False, result={"error": "..."} - error
    """
    global _login_state
    
    if _login_state is None:
        return False, {"error": "No login in progress"}
    
    # Check if expired
    if time.time() > _login_state.expires_at:
        _login_state = None
        return False, {"error": "Authorization expired, please restart"}
    
    oidc_base = f"https://oidc.{_login_state.region}.amazonaws.com"
    
    token_body = {
        "clientId": _login_state.client_id,
        "clientSecret": _login_state.client_secret,
        "grantType": "urn:ietf:params:oauth:grant-type:device_code",
        "deviceCode": _login_state.device_code
    }
    
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        try:
            token_resp = await client.post(
                f"{oidc_base}/token",
                json=token_body,
                headers={"Content-Type": "application/json"}
            )
        except Exception as e:
            return False, {"error": f"Token request failed: {e}"}
        
        if token_resp.status_code == 200:
            # Authorization successful
            token_data = token_resp.json()
            
            credentials = {
                "accessToken": token_data.get("accessToken"),
                "refreshToken": token_data.get("refreshToken"),
                "expiresAt": datetime.now(timezone.utc).isoformat(),
                "clientId": _login_state.client_id,
                "clientSecret": _login_state.client_secret,
                "region": _login_state.region,
                "authMethod": "idc",
            }
            
            # Calculate expiration time
            if expires_in := token_data.get("expiresIn"):
                expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                credentials["expiresAt"] = expires_at.isoformat()
            
            # Clear state
            _login_state = None
            
            logger.info("Device Flow authorization successful!")
            return True, {"completed": True, "credentials": credentials}
        
        # Check error type
        try:
            error_data = token_resp.json()
            error_code = error_data.get("error", "")
        except:
            error_code = ""
        
        if error_code == "authorization_pending":
            return True, {"completed": False, "status": "pending"}
        elif error_code == "slow_down":
            return True, {"completed": False, "status": "slow_down"}
        elif error_code == "expired_token":
            _login_state = None
            return False, {"error": "Authorization expired, please restart"}
        elif error_code == "access_denied":
            _login_state = None
            return False, {"error": "User denied authorization"}
        else:
            return False, {"error": f"Token request failed: {token_resp.text}"}


def cancel_device_flow() -> bool:
    """Cancel device authorization flow."""
    global _login_state
    if _login_state is not None:
        _login_state = None
        return True
    return False
