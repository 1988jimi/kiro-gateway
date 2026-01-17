# -*- coding: utf-8 -*-
"""
Credential types for Kiro Gateway.

Defines data structures for storing and managing Kiro credentials.
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional


class CredentialStatus(Enum):
    """Credential status."""
    ACTIVE = "active"
    COOLDOWN = "cooldown"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"
    SUSPENDED = "suspended"


@dataclass
class KiroCredentials:
    """Kiro credential information."""
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    profile_arn: Optional[str] = None
    expires_at: Optional[str] = None
    region: str = "us-east-1"
    auth_method: str = "social"  # "social" or "idc"
    client_id_hash: Optional[str] = None
    last_refresh: Optional[str] = None
    
    @classmethod
    def from_file(cls, path: str) -> "KiroCredentials":
        """Load credentials from file."""
        with open(path) as f:
            data = json.load(f)
        
        return cls(
            access_token=data.get("accessToken"),
            refresh_token=data.get("refreshToken"),
            client_id=data.get("clientId"),
            client_secret=data.get("clientSecret"),
            profile_arn=data.get("profileArn"),
            expires_at=data.get("expiresAt") or data.get("expire"),
            region=data.get("region", "us-east-1"),
            auth_method=data.get("authMethod", "social"),
            client_id_hash=data.get("clientIdHash"),
            last_refresh=data.get("lastRefresh"),
        )
    
    @classmethod
    def from_dict(cls, data: dict) -> "KiroCredentials":
        """Create credentials from dictionary."""
        return cls(
            access_token=data.get("accessToken"),
            refresh_token=data.get("refreshToken"),
            client_id=data.get("clientId"),
            client_secret=data.get("clientSecret"),
            profile_arn=data.get("profileArn"),
            expires_at=data.get("expiresAt"),
            region=data.get("region", "us-east-1"),
            auth_method=data.get("authMethod", "social"),
            client_id_hash=data.get("clientIdHash"),
            last_refresh=data.get("lastRefresh"),
        )
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
            "profileArn": self.profile_arn,
            "expiresAt": self.expires_at,
            "region": self.region,
            "authMethod": self.auth_method,
            "clientIdHash": self.client_id_hash,
            "lastRefresh": self.last_refresh,
        }
    
    def save_to_file(self, path: str):
        """Save credentials to file."""
        existing = {}
        p = Path(path)
        if p.exists():
            try:
                with open(p) as f:
                    existing = json.load(f)
            except Exception:
                pass
        
        # Update with non-None values
        existing.update({k: v for k, v in self.to_dict().items() if v is not None})
        
        # Ensure parent directory exists
        p.parent.mkdir(parents=True, exist_ok=True)
        
        with open(p, "w") as f:
            json.dump(existing, f, indent=2)
    
    def is_expired(self) -> bool:
        """Check if token is expired (with 5 minute buffer)."""
        if not self.expires_at:
            return True
        
        try:
            if "T" in self.expires_at:
                expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                return expires <= now + timedelta(minutes=5)
            
            # Unix timestamp format
            expires_ts = int(self.expires_at)
            now_ts = int(time.time())
            return now_ts >= (expires_ts - 300)
        except Exception:
            return True
    
    def is_expiring_soon(self, minutes: int = 10) -> bool:
        """Check if token is expiring soon."""
        if not self.expires_at:
            return False
        
        try:
            if "T" in self.expires_at:
                expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                return expires < now + timedelta(minutes=minutes)
            
            # Unix timestamp format
            expires_ts = int(self.expires_at)
            now_ts = int(time.time())
            return now_ts >= (expires_ts - minutes * 60)
        except Exception:
            return False
    
    def get_expires_in_seconds(self) -> Optional[int]:
        """Get seconds until token expires."""
        if not self.expires_at:
            return None
        
        try:
            if "T" in self.expires_at:
                expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                return int((expires - now).total_seconds())
            
            expires_ts = int(self.expires_at)
            now_ts = int(time.time())
            return expires_ts - now_ts
        except Exception:
            return None
