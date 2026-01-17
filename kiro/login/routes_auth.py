# -*- coding: utf-8 -*-
"""
Authentication Routes for Kiro Gateway.

Provides API endpoints for:
- Device Code Flow login
- Social Auth login (Google/GitHub)
- Token management
- Credential storage
"""

import json
import webbrowser
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from loguru import logger

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
    start_callback_server,
    wait_for_callback,
    stop_callback_server,
    CALLBACK_PORT,
)
from .credential_types import KiroCredentials

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


# ==================== Request Models ====================

class DeviceFlowStartRequest(BaseModel):
    region: str = "us-east-1"


class SocialAuthStartRequest(BaseModel):
    provider: str  # "google" or "github"
    open_browser: bool = True


class TokenExchangeRequest(BaseModel):
    code: str
    state: str


class ManualCredentialsRequest(BaseModel):
    refresh_token: str
    access_token: Optional[str] = None
    profile_arn: Optional[str] = None
    region: str = "us-east-1"
    auth_method: str = "social"
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


# ==================== Helper Functions ====================

def _get_credentials_path() -> Path:
    """Get default credentials file path."""
    return Path.home() / ".aws" / "sso" / "cache" / "kiro-gateway-auth.json"


async def _save_credentials(credentials: dict) -> str:
    """Save credentials to file."""
    creds_path = _get_credentials_path()
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(creds_path, "w") as f:
        json.dump(credentials, f, indent=2)
    
    logger.info(f"Credentials saved to: {creds_path}")
    return str(creds_path)


def _reload_auth_manager(request: Request, creds_path: str):
    """Reload auth manager with new credentials."""
    auth_manager = request.app.state.auth_manager
    
    # Load credentials from file
    auth_manager._load_credentials_from_file(creds_path)
    auth_manager._detect_auth_type()
    
    logger.info(f"Auth manager reloaded with credentials from: {creds_path}")


# ==================== Device Flow Endpoints ====================

@router.post("/device-flow/start")
async def api_start_device_flow(req: Request, body: DeviceFlowStartRequest):
    """
    Start Device Code Flow login.
    
    Returns user_code and verification_uri for user to complete authorization.
    """
    success, result = await start_device_flow(body.region)
    
    if not success:
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    
    # Auto open browser
    try:
        webbrowser.open(result["verification_uri"])
    except Exception as e:
        logger.warning(f"Failed to open browser: {e}")
    
    return {
        "status": "pending",
        "user_code": result["user_code"],
        "verification_uri": result["verification_uri"],
        "expires_in": result["expires_in"],
        "interval": result["interval"],
        "message": f"Please visit {result['verification_uri']} and enter code: {result['user_code']}"
    }


@router.get("/device-flow/poll")
async def api_poll_device_flow(request: Request):
    """
    Poll Device Code Flow status.
    
    Returns credentials when authorization is complete.
    """
    success, result = await poll_device_flow()
    
    if not success:
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    
    if result.get("completed"):
        # Save credentials
        credentials = result["credentials"]
        creds_path = await _save_credentials(credentials)
        
        # Reload auth manager with new credentials
        _reload_auth_manager(request, creds_path)
        
        return {
            "status": "completed",
            "credentials_path": creds_path,
            "message": "Login successful! Credentials saved and loaded."
        }
    else:
        return {
            "status": result.get("status", "pending"),
            "message": "Waiting for user authorization..."
        }


@router.post("/device-flow/cancel")
async def api_cancel_device_flow():
    """Cancel Device Code Flow."""
    cancelled = cancel_device_flow()
    return {"cancelled": cancelled}


@router.get("/device-flow/status")
async def api_device_flow_status():
    """Get current Device Flow status."""
    state = get_login_state()
    if state:
        return {"active": True, **state}
    return {"active": False}


# ==================== Social Auth Endpoints ====================

@router.post("/social/start")
async def api_start_social_auth(req: Request, body: SocialAuthStartRequest):
    """
    Start Social Auth login (Google/GitHub).
    
    Returns login URL for user to complete authorization.
    """
    success, result = await start_social_auth(body.provider)
    
    if not success:
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    
    # Optionally open browser
    if body.open_browser:
        try:
            webbrowser.open(result["login_url"])
        except Exception as e:
            logger.warning(f"Failed to open browser: {e}")
    
    return {
        "status": "pending",
        "login_url": result["login_url"],
        "provider": result["provider"],
        "callback_port": result["callback_port"],
        "message": f"Please complete {result['provider']} login in your browser"
    }


@router.post("/social/exchange")
async def api_exchange_social_token(request: Request, body: TokenExchangeRequest):
    """
    Exchange authorization code for tokens.
    
    Called by callback handler after user completes login.
    """
    success, result = await exchange_social_auth_token(body.code, body.state)
    
    if not success:
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    
    # Save credentials
    credentials = result["credentials"]
    creds_path = await _save_credentials(credentials)
    
    # Reload auth manager with new credentials
    _reload_auth_manager(request, creds_path)
    
    return {
        "status": "completed",
        "provider": result["provider"],
        "credentials_path": creds_path,
        "message": f"{result['provider']} login successful! Credentials saved and loaded."
    }


@router.post("/social/cancel")
async def api_cancel_social_auth():
    """Cancel Social Auth login."""
    cancelled = cancel_social_auth()
    return {"cancelled": cancelled}


@router.get("/social/status")
async def api_social_auth_status():
    """Get current Social Auth status."""
    state = get_social_auth_state()
    if state:
        return {"active": True, **state}
    return {"active": False}


# ==================== Callback Handler ====================

@router.get("/callback", response_class=HTMLResponse)
async def api_social_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None)
):
    """
    OAuth callback handler.
    
    Receives authorization code from OAuth provider and exchanges for tokens.
    """
    if error:
        html = f"""
        <html>
        <head><title>Login Failed</title></head>
        <body style="font-family:sans-serif;text-align:center;padding:50px;background:#1a1a2e;color:#fff">
            <h1>Login Failed</h1>
            <p>Error: {error}</p>
            <p>Please close this window and try again.</p>
        </body>
        </html>
        """
        return HTMLResponse(content=html, status_code=400)
    
    if not code or not state:
        html = """
        <html>
        <head><title>Login Failed</title></head>
        <body style="font-family:sans-serif;text-align:center;padding:50px;background:#1a1a2e;color:#fff">
            <h1>Login Failed</h1>
            <p>Missing authorization code or state.</p>
            <p>Please close this window and try again.</p>
        </body>
        </html>
        """
        return HTMLResponse(content=html, status_code=400)
    
    # Exchange code for tokens
    success, result = await exchange_social_auth_token(code, state)
    
    if not success:
        html = f"""
        <html>
        <head><title>Login Failed</title></head>
        <body style="font-family:sans-serif;text-align:center;padding:50px;background:#1a1a2e;color:#fff">
            <h1>Login Failed</h1>
            <p>{result.get('error', 'Unknown error')}</p>
            <p>Please close this window and try again.</p>
        </body>
        </html>
        """
        return HTMLResponse(content=html, status_code=400)
    
    # Save credentials
    credentials = result["credentials"]
    creds_path = await _save_credentials(credentials)
    
    # Reload auth manager with new credentials
    _reload_auth_manager(request, creds_path)
    
    html = f"""
    <html>
    <head><title>Login Successful</title></head>
    <body style="font-family:sans-serif;text-align:center;padding:50px;background:#1a1a2e;color:#fff">
        <h1>Login Successful</h1>
        <p>{result['provider']} login completed!</p>
        <p>You can close this window and return to Kiro Gateway.</p>
        <script>setTimeout(()=>window.close(),3000)</script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


# ==================== Manual Credentials ====================

@router.post("/manual")
async def api_add_manual_credentials(request: Request, body: ManualCredentialsRequest):
    """
    Add credentials manually.
    
    Useful when you already have a refresh_token from another source.
    """
    credentials = {
        "refreshToken": body.refresh_token,
        "accessToken": body.access_token,
        "profileArn": body.profile_arn,
        "region": body.region,
        "authMethod": body.auth_method,
        "clientId": body.client_id,
        "clientSecret": body.client_secret,
    }
    
    # Remove None values
    credentials = {k: v for k, v in credentials.items() if v is not None}
    
    creds_path = await _save_credentials(credentials)
    
    # Reload auth manager with new credentials
    _reload_auth_manager(request, creds_path)
    
    return {
        "status": "saved",
        "credentials_path": creds_path,
        "message": "Credentials saved and loaded successfully."
    }


# ==================== Credentials Management ====================

@router.get("/credentials")
async def api_get_credentials(request: Request):
    """Get current credentials info (without sensitive data)."""
    auth_manager = request.app.state.auth_manager
    
    # Check if auth manager has credentials
    has_refresh = bool(auth_manager._refresh_token)
    has_access = bool(auth_manager._access_token)
    
    if not has_refresh and not has_access:
        # Check if file exists
        creds_path = _get_credentials_path()
        if creds_path.exists():
            # Try to load from file
            try:
                _reload_auth_manager(request, str(creds_path))
                has_refresh = bool(auth_manager._refresh_token)
                has_access = bool(auth_manager._access_token)
            except Exception as e:
                logger.warning(f"Failed to load credentials: {e}")
    
    if not has_refresh and not has_access:
        return {"has_credentials": False}
    
    return {
        "has_credentials": True,
        "auth_type": auth_manager._auth_type.value if auth_manager._auth_type else None,
        "region": auth_manager._region,
        "has_access_token": has_access,
        "has_refresh_token": has_refresh,
        "expires_at": auth_manager._expires_at.isoformat() if auth_manager._expires_at else None,
        "is_expiring_soon": auth_manager.is_token_expiring_soon(),
    }


@router.delete("/credentials")
async def api_delete_credentials(request: Request):
    """Delete stored credentials."""
    creds_path = _get_credentials_path()
    
    if creds_path.exists():
        creds_path.unlink()
        
        # Clear auth manager credentials
        auth_manager = request.app.state.auth_manager
        auth_manager._access_token = None
        auth_manager._refresh_token = None
        auth_manager._expires_at = None
        
        return {"deleted": True, "message": "Credentials deleted."}
    
    return {"deleted": False, "message": "No credentials found."}


@router.post("/refresh")
async def api_refresh_token(request: Request):
    """
    Manually trigger token refresh.
    
    Uses the auth_manager from app state.
    """
    auth_manager = request.app.state.auth_manager
    
    if not auth_manager._refresh_token:
        raise HTTPException(status_code=400, detail="No refresh token available. Please login first.")
    
    try:
        new_token = await auth_manager.force_refresh()
        
        # Save updated credentials to file
        creds_path = _get_credentials_path()
        if creds_path.exists():
            with open(creds_path, "r") as f:
                creds = json.load(f)
            
            creds["accessToken"] = auth_manager._access_token
            if auth_manager._refresh_token:
                creds["refreshToken"] = auth_manager._refresh_token
            if auth_manager._expires_at:
                creds["expiresAt"] = auth_manager._expires_at.isoformat()
            
            with open(creds_path, "w") as f:
                json.dump(creds, f, indent=2)
        
        return {
            "status": "refreshed",
            "message": "Token refreshed successfully."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Token refresh failed: {e}")


# ==================== Token Scan ====================

@router.get("/scan")
async def api_scan_tokens(request: Request):
    """
    Scan system for existing Kiro token files.
    
    Scans ~/.aws/sso/cache/ for valid token files.
    """
    found = []
    sso_cache = Path.home() / ".aws" / "sso" / "cache"
    
    if sso_cache.exists():
        for f in sso_cache.glob("*.json"):
            try:
                with open(f) as fp:
                    data = json.load(fp)
                    if "accessToken" in data or "refreshToken" in data:
                        auth_method = data.get("authMethod", "social")
                        found.append({
                            "path": str(f),
                            "name": f.stem,
                            "expires_at": data.get("expiresAt"),
                            "auth_method": auth_method,
                            "region": data.get("region", "us-east-1"),
                            "has_access_token": "accessToken" in data,
                            "has_refresh_token": "refreshToken" in data,
                        })
            except Exception:
                pass
    
    return {"tokens": found, "count": len(found)}


@router.post("/scan/use")
async def api_use_scanned_token(request: Request, path: str = Query(...)):
    """
    Use a scanned token file as credentials.
    """
    token_path = Path(path)
    
    if not token_path.exists():
        raise HTTPException(status_code=404, detail="Token file not found")
    
    try:
        with open(token_path) as f:
            data = json.load(f)
        
        if "accessToken" not in data and "refreshToken" not in data:
            raise HTTPException(status_code=400, detail="Invalid token file")
        
        # Copy to our credentials file
        creds_path = await _save_credentials(data)
        
        # Reload auth manager
        _reload_auth_manager(request, creds_path)
        
        return {
            "status": "loaded",
            "source": str(token_path),
            "credentials_path": creds_path,
            "message": "Token loaded successfully."
        }
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")


@router.post("/auto-setup")
async def api_auto_setup(request: Request):
    """
    Automatically setup credentials.
    
    1. First, scan for existing token files
    2. If found, use the most recent one
    3. If not found, start device flow login
    """
    # Step 1: Scan for existing tokens
    sso_cache = Path.home() / ".aws" / "sso" / "cache"
    found_tokens = []
    
    if sso_cache.exists():
        for f in sso_cache.glob("*.json"):
            try:
                with open(f) as fp:
                    data = json.load(fp)
                    if "accessToken" in data or "refreshToken" in data:
                        # Get file modification time
                        mtime = f.stat().st_mtime
                        found_tokens.append({
                            "path": str(f),
                            "data": data,
                            "mtime": mtime,
                        })
            except Exception:
                pass
    
    if found_tokens:
        # Use the most recently modified token
        found_tokens.sort(key=lambda x: x["mtime"], reverse=True)
        best_token = found_tokens[0]
        
        # Save and load
        creds_path = await _save_credentials(best_token["data"])
        _reload_auth_manager(request, creds_path)
        
        return {
            "status": "loaded",
            "source": best_token["path"],
            "credentials_path": creds_path,
            "message": f"Found and loaded existing token from {best_token['path']}"
        }
    
    # Step 2: No existing tokens, start device flow
    success, result = await start_device_flow("us-east-1")
    
    if not success:
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to start login"))
    
    # Auto open browser
    try:
        webbrowser.open(result["verification_uri"])
    except Exception as e:
        logger.warning(f"Failed to open browser: {e}")
    
    return {
        "status": "login_required",
        "user_code": result["user_code"],
        "verification_uri": result["verification_uri"],
        "expires_in": result["expires_in"],
        "message": f"No existing tokens found. Please visit {result['verification_uri']} and enter code: {result['user_code']}"
    }
