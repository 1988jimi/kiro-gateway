# -*- coding: utf-8 -*-
"""
Social Authentication for Kiro Gateway.

Implements OAuth 2.0 + PKCE flow for Google/GitHub login:
1. Generate PKCE code_verifier and code_challenge
2. Build login URL and open browser
3. Start local callback server to receive authorization code
4. Exchange authorization code for tokens
"""

import time
import secrets
import hashlib
import base64
import asyncio
import httpx
from dataclasses import dataclass
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, quote

from loguru import logger


@dataclass
class SocialAuthState:
    """Social Auth login state."""
    provider: str  # Google / Github
    code_verifier: str
    code_challenge: str
    oauth_state: str
    expires_at: int
    started_at: float


# Global state
_social_auth_state: Optional[SocialAuthState] = None
_callback_result: Optional[dict] = None
_callback_event: Optional[asyncio.Event] = None

# Kiro Auth endpoint
KIRO_AUTH_ENDPOINT = "https://prod.us-east-1.auth.desktop.kiro.dev"
CALLBACK_PORT = 19823
CALLBACK_URI = f"http://127.0.0.1:{CALLBACK_PORT}/kiro-social-callback"


def _generate_code_verifier() -> str:
    """Generate PKCE code_verifier."""
    return secrets.token_urlsafe(64)[:128]


def _generate_code_challenge(verifier: str) -> str:
    """Generate PKCE code_challenge (SHA256)."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()


def _generate_oauth_state() -> str:
    """Generate OAuth state."""
    return secrets.token_urlsafe(32)


def get_social_auth_state() -> Optional[dict]:
    """Get current Social Auth state."""
    global _social_auth_state
    if _social_auth_state is None:
        return None
    
    if time.time() > _social_auth_state.expires_at:
        _social_auth_state = None
        return None
    
    return {
        "provider": _social_auth_state.provider,
        "expires_in": int(_social_auth_state.expires_at - time.time()),
    }


async def start_social_auth(provider: str) -> Tuple[bool, dict]:
    """
    Start Social Auth login (Google/GitHub).
    
    Args:
        provider: "google" or "github"
    
    Returns:
        (success, result_or_error)
    """
    global _social_auth_state
    
    # Normalize provider name
    provider_normalized = provider.lower()
    if provider_normalized == "google":
        provider_normalized = "Google"
    elif provider_normalized == "github":
        provider_normalized = "Github"
    else:
        return False, {"error": f"Unsupported login provider: {provider}"}
    
    logger.info(f"Starting {provider_normalized} login flow")
    
    # Generate PKCE
    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)
    oauth_state = _generate_oauth_state()
    
    # Build login URL
    login_url = (
        f"{KIRO_AUTH_ENDPOINT}/login?"
        f"idp={provider_normalized}&"
        f"redirect_uri={quote(CALLBACK_URI)}&"
        f"code_challenge={quote(code_challenge)}&"
        f"code_challenge_method=S256&"
        f"state={quote(oauth_state)}"
    )
    
    logger.debug(f"Login URL: {login_url}")
    
    # Save state (10 minutes expiry)
    _social_auth_state = SocialAuthState(
        provider=provider_normalized,
        code_verifier=code_verifier,
        code_challenge=code_challenge,
        oauth_state=oauth_state,
        expires_at=int(time.time() + 600),
        started_at=time.time(),
    )
    
    return True, {
        "login_url": login_url,
        "state": oauth_state,
        "provider": provider_normalized,
        "callback_port": CALLBACK_PORT,
    }


async def exchange_social_auth_token(code: str, state: str) -> Tuple[bool, dict]:
    """
    Exchange authorization code for tokens.
    
    Args:
        code: Authorization code
        state: OAuth state
    
    Returns:
        (success, result_or_error)
    """
    global _social_auth_state
    
    if _social_auth_state is None:
        return False, {"error": "No social login in progress"}
    
    # Verify state
    if state != _social_auth_state.oauth_state:
        _social_auth_state = None
        return False, {"error": "OAuth state mismatch"}
    
    # Check expiry
    if time.time() > _social_auth_state.expires_at:
        _social_auth_state = None
        return False, {"error": "Login expired, please restart"}
    
    logger.info("Exchanging authorization code for tokens...")
    
    # Exchange for tokens
    token_body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK_URI,
        "code_verifier": _social_auth_state.code_verifier,
    }
    
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        try:
            token_resp = await client.post(
                f"{KIRO_AUTH_ENDPOINT}/oauth/token",
                json=token_body,
                headers={"Content-Type": "application/json"}
            )
        except Exception as e:
            _social_auth_state = None
            return False, {"error": f"Token request failed: {e}"}
        
        if token_resp.status_code != 200:
            error_text = token_resp.text
            _social_auth_state = None
            return False, {"error": f"Token exchange failed: {error_text}"}
        
        token_data = token_resp.json()
        
        credentials = {
            "accessToken": token_data.get("access_token"),
            "refreshToken": token_data.get("refresh_token"),
            "expiresAt": datetime.now(timezone.utc).isoformat(),
            "authMethod": "social",
            "region": "us-east-1",
        }
        
        # Calculate expiration time
        if expires_in := token_data.get("expires_in"):
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            credentials["expiresAt"] = expires_at.isoformat()
        
        provider = _social_auth_state.provider
        _social_auth_state = None
        
        logger.info(f"{provider} login successful!")
        return True, {"completed": True, "credentials": credentials, "provider": provider}


def cancel_social_auth() -> bool:
    """Cancel Social Auth login."""
    global _social_auth_state
    if _social_auth_state is not None:
        _social_auth_state = None
        return True
    return False


# ==================== Callback Server ====================

async def start_callback_server() -> Tuple[bool, dict]:
    """Start local callback server."""
    global _callback_result, _callback_event
    
    try:
        from aiohttp import web
    except ImportError:
        return False, {"error": "aiohttp not installed. Run: pip install aiohttp"}
    
    _callback_result = None
    _callback_event = asyncio.Event()
    
    async def handle_callback(request):
        global _callback_result
        code = request.query.get("code")
        state = request.query.get("state")
        error = request.query.get("error")
        
        if error:
            _callback_result = {"error": error}
        elif code and state:
            _callback_result = {"code": code, "state": state}
        else:
            _callback_result = {"error": "Missing authorization code"}
        
        _callback_event.set()
        
        # Return success page
        html = """
        <html>
        <head><title>Login Successful</title></head>
        <body style="font-family:sans-serif;text-align:center;padding:50px;background:#1a1a2e;color:#fff">
            <h1>✅ Login Successful</h1>
            <p>You can close this window and return to Kiro Gateway</p>
            <script>setTimeout(()=>window.close(),2000)</script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")
    
    app = web.Application()
    app.router.add_get("/kiro-social-callback", handle_callback)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    try:
        site = web.TCPSite(runner, "127.0.0.1", CALLBACK_PORT)
        await site.start()
        logger.info(f"Callback server started: http://127.0.0.1:{CALLBACK_PORT}")
        return True, {"port": CALLBACK_PORT, "runner": runner}
    except Exception as e:
        return False, {"error": f"Failed to start callback server: {e}"}


async def wait_for_callback(timeout: int = 300) -> Tuple[bool, dict]:
    """Wait for callback."""
    global _callback_result, _callback_event
    
    if _callback_event is None:
        return False, {"error": "Callback server not started"}
    
    try:
        await asyncio.wait_for(_callback_event.wait(), timeout=timeout)
        
        if _callback_result and "code" in _callback_result:
            return True, _callback_result
        elif _callback_result and "error" in _callback_result:
            return False, _callback_result
        else:
            return False, {"error": "No valid callback received"}
    except asyncio.TimeoutError:
        return False, {"error": "Callback timeout"}


async def stop_callback_server(runner) -> None:
    """Stop callback server."""
    if runner:
        await runner.cleanup()
        logger.info("Callback server stopped")
