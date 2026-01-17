# -*- coding: utf-8 -*-
"""
Background Scheduler for Kiro Gateway.

Handles:
- Token pre-refresh before expiration
- Credentials file monitoring (for Kiro IDE IdC auth)
- Account health checks
- Statistics updates
"""

import asyncio
import time
from typing import Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from kiro.auth import KiroAuthManager


class BackgroundScheduler:
    """
    Background task scheduler.
    
    Responsible for:
    - Token expiration pre-refresh
    - Credentials file monitoring (reload when Kiro IDE updates the file)
    - Account health checks
    """
    
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._refresh_interval = 300  # Check every 5 minutes
        self._file_check_interval = 30  # Check file changes every 30 seconds
        self._health_check_interval = 600  # Health check every 10 minutes
        self._last_health_check = 0
        self._last_file_check = 0
        self._auth_manager: Optional["KiroAuthManager"] = None
    
    def set_auth_manager(self, auth_manager: "KiroAuthManager"):
        """Set the auth manager to monitor."""
        self._auth_manager = auth_manager
    
    def _has_valid_credentials(self) -> bool:
        """Check if auth manager has valid credentials configured."""
        if not self._auth_manager:
            return False
        
        # Check if refresh_token is set
        if hasattr(self._auth_manager, '_refresh_token') and self._auth_manager._refresh_token:
            return True
        
        # Check if access_token is set
        if hasattr(self._auth_manager, '_access_token') and self._auth_manager._access_token:
            return True
        
        return False
    
    async def start(self):
        """Start background tasks."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Background scheduler started")
    
    async def stop(self):
        """Stop background tasks."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Background scheduler stopped")
    
    async def _run(self):
        """Main loop."""
        # Wait a bit before first check to allow credentials to be loaded
        await asyncio.sleep(5)
        
        while self._running:
            try:
                now = time.time()
                
                # File change monitoring (every 30 seconds)
                # This is especially important for Kiro IDE IdC auth
                if now - self._last_file_check > self._file_check_interval:
                    await self._check_file_updates()
                    self._last_file_check = now
                
                # Only run other checks if we have credentials
                if self._has_valid_credentials():
                    # Token pre-refresh
                    await self._refresh_expiring_tokens()
                    
                    # Health check
                    if now - self._last_health_check > self._health_check_interval:
                        await self._health_check()
                        self._last_health_check = now
                else:
                    logger.debug("No credentials configured, skipping token refresh check")
                
                # Sleep for a shorter interval to enable responsive file monitoring
                await asyncio.sleep(min(self._file_check_interval, self._refresh_interval))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(60)
    
    async def _check_file_updates(self):
        """
        Check if credentials file has been updated by Kiro IDE.
        
        This is especially important for Kiro IDE IdC authentication,
        where the gateway cannot refresh tokens programmatically.
        """
        if not self._auth_manager:
            return
        
        if not self._auth_manager.creds_file:
            return
        
        try:
            if self._auth_manager.check_file_updated():
                logger.info("Credentials file updated by Kiro IDE, reloading...")
                await self._auth_manager.reload_from_file()
                logger.info("Credentials reloaded successfully")
        except Exception as e:
            logger.warning(f"Failed to check/reload credentials file: {e}")
    
    async def _refresh_expiring_tokens(self):
        """Refresh tokens that are about to expire."""
        if not self._auth_manager:
            return
        
        # Double check credentials exist
        if not self._has_valid_credentials():
            return
        
        # Import AuthType here to avoid circular imports
        from kiro.auth import AuthType
        
        # Check if token is expiring within 15 minutes
        if self._auth_manager.is_token_expiring_soon():
            # For Kiro IDE IdC, first try to reload from file
            if self._auth_manager.auth_type == AuthType.KIRO_IDE_IDC:
                logger.info("Token expiring soon (IdC auth), checking for file updates...")
                if self._auth_manager.check_file_updated():
                    try:
                        await self._auth_manager.reload_from_file()
                        logger.info("Token reloaded from file successfully")
                        return
                    except Exception as e:
                        logger.warning(f"Failed to reload token from file: {e}")
                else:
                    logger.warning("Token expiring but file not updated. Please ensure Kiro IDE is running.")
            else:
                # For other auth types, use normal refresh
                logger.info("Token expiring soon, pre-refreshing...")
                try:
                    await self._auth_manager.force_refresh()
                    logger.info("Token pre-refresh successful")
                except Exception as e:
                    logger.warning(f"Token pre-refresh failed: {e}")
    
    async def _health_check(self):
        """Perform health check."""
        if not self._auth_manager:
            return
        
        # Skip health check if no credentials
        if not self._has_valid_credentials():
            return
        
        try:
            import httpx
            from kiro.auth import AuthType
            
            token = await self._auth_manager.get_access_token()
            if not token:
                logger.debug("Health check: No valid token available")
                return
            
            # Check against models endpoint
            models_url = f"https://q.{self._auth_manager.region}.amazonaws.com/ListAvailableModels"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            async with httpx.AsyncClient(verify=False, timeout=10) as client:
                resp = await client.get(
                    models_url,
                    headers=headers,
                    params={"origin": "AI_EDITOR"}
                )
                
                if resp.status_code == 200:
                    logger.debug("Health check: OK")
                elif resp.status_code == 401:
                    logger.warning("Health check: Authentication failed")
                    # For IdC auth, try reloading from file
                    if self._auth_manager.auth_type == AuthType.KIRO_IDE_IDC:
                        logger.info("IdC auth: Attempting to reload credentials from file...")
                        await self._check_file_updates()
                elif resp.status_code == 403:
                    logger.warning("Health check: Access forbidden (token may be expired)")
                    # For IdC auth, try reloading from file
                    if self._auth_manager.auth_type == AuthType.KIRO_IDE_IDC:
                        logger.info("IdC auth: Attempting to reload credentials from file...")
                        await self._check_file_updates()
                elif resp.status_code == 429:
                    logger.debug("Health check: Rate limited (quota exceeded)")
                else:
                    logger.warning(f"Health check: Unexpected status {resp.status_code}")
                    
        except Exception as e:
            logger.warning(f"Health check failed: {e}")


# Global scheduler instance
scheduler = BackgroundScheduler()
