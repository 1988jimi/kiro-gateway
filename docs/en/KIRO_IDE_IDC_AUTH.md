# Kiro IDE IdC Authentication Support

## Problem Discovery

### Background

When using Kiro Gateway as a reverse proxy for Claude Code CLI, users encountered persistent 401/400 errors during token refresh:

```
WARNING  | Token pre-refresh failed: Client error '401 Unauthorized' for url 
'https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken'
```

### Root Cause Analysis

Investigation revealed that **Kiro IDE with IdC (AWS Identity Center) authentication** uses a different token mechanism than the standard Kiro Desktop Auth or AWS SSO OIDC flows:

| Auth Type | Token File | Refresh Endpoint | Refresh Works |
|-----------|------------|------------------|---------------|
| Social (Google/GitHub) | `kiro-gateway-auth.json` | `auth.desktop.kiro.dev/refreshToken` | ✅ Yes |
| AWS SSO OIDC (kiro-cli) | `data.sqlite3` | `oidc.{region}.amazonaws.com/token` | ✅ Yes |
| **Kiro IDE IdC** | `kiro-auth-token.json` | **Neither endpoint works** | ❌ No |

### Token File Structure Comparison

**Standard Social Auth Token:**
```json
{
  "accessToken": "...",
  "refreshToken": "...",
  "expiresAt": "2026-01-17T06:14:44.461Z",
  "authMethod": "social",
  "region": "us-east-1"
}
```

**Kiro IDE IdC Token:**
```json
{
  "accessToken": "...",
  "refreshToken": "...",
  "expiresAt": "2026-01-17T06:14:44.461Z",
  "clientIdHash": "1ddcd4f2ad58b98803886f6576aeb7a23a9e1667",
  "authMethod": "IdC",
  "provider": "Enterprise",
  "region": "us-east-1"
}
```

Key difference: **IdC tokens have `clientIdHash` pointing to a separate device registration file**, but this doesn't mean they can be refreshed via AWS SSO OIDC.

### Why Standard Refresh Doesn't Work

1. **Kiro Desktop Auth (401)**: The IdC refresh token format is incompatible with the social auth endpoint
2. **AWS SSO OIDC (400)**: The token was not obtained via standard OIDC flow, so OIDC refresh fails

### How Kiro IDE Handles This

Kiro IDE manages its own token lifecycle:
- It monitors token expiration internally
- It refreshes tokens using its proprietary mechanism (possibly involving AWS Cognito)
- It updates the `kiro-auth-token.json` file automatically

## Solution

### Approach: File-Based Token Synchronization

Since we cannot refresh IdC tokens programmatically, we synchronize with Kiro IDE's token file:

1. **Direct file usage**: Configure gateway to use Kiro IDE's token file directly
2. **File monitoring**: Watch for file changes and reload credentials automatically
3. **Graceful fallback**: When token expires and refresh fails, reload from file

### Configuration

```env
# Point directly to Kiro IDE's token file
KIRO_CREDS_FILE="~/.aws/sso/cache/kiro-auth-token.json"
```

### Implementation Details

#### New AuthType: KIRO_IDE_IDC

Added a new authentication type for Kiro IDE IdC credentials:

```python
class AuthType(Enum):
    KIRO_DESKTOP = "kiro_desktop"      # Social auth (Google/GitHub)
    AWS_SSO_OIDC = "aws_sso_oidc"       # kiro-cli with AWS SSO
    KIRO_IDE_IDC = "kiro_ide_idc"       # Kiro IDE with Identity Center
```

#### Detection Logic

```python
def _detect_auth_type(self) -> None:
    # Check for Kiro IDE IdC first (has clientIdHash but no inline clientId/clientSecret)
    if self._client_id_hash and not (self._client_id and self._client_secret):
        self._auth_type = AuthType.KIRO_IDE_IDC
    elif self._client_id and self._client_secret:
        self._auth_type = AuthType.AWS_SSO_OIDC
    else:
        self._auth_type = AuthType.KIRO_DESKTOP
```

#### File Watcher

The scheduler now monitors the credentials file for changes:

```python
async def _check_file_updates(self):
    """Check if credentials file has been updated by Kiro IDE."""
    if not self._auth_manager or not self._auth_manager._creds_file:
        return
    
    path = Path(self._auth_manager._creds_file).expanduser()
    if not path.exists():
        return
    
    current_mtime = path.stat().st_mtime
    if current_mtime != self._last_file_mtime:
        logger.info("Credentials file updated, reloading...")
        self._auth_manager._load_credentials_from_file(str(path))
        self._last_file_mtime = current_mtime
```

#### Refresh Strategy for IdC

```python
async def _refresh_token_request(self) -> None:
    if self._auth_type == AuthType.KIRO_IDE_IDC:
        # Cannot refresh IdC tokens - reload from file instead
        await self._reload_from_file()
    elif self._auth_type == AuthType.AWS_SSO_OIDC:
        await self._refresh_token_aws_sso_oidc()
    else:
        await self._refresh_token_kiro_desktop()
```

## Usage Guide

### Prerequisites

1. **Kiro IDE installed and logged in** with IdC (Identity Center / Enterprise SSO)
2. **Kiro IDE running** - it manages token refresh automatically

### Setup Steps

1. **Find your token file:**
   ```bash
   ls -la ~/.aws/sso/cache/
   # Look for kiro-auth-token.json or similar
   ```

2. **Verify it's IdC auth:**
   ```bash
   cat ~/.aws/sso/cache/kiro-auth-token.json | grep authMethod
   # Should show: "authMethod": "IdC"
   ```

3. **Configure gateway:**
   ```env
   KIRO_CREDS_FILE="~/.aws/sso/cache/kiro-auth-token.json"
   PROXY_API_KEY="your-secret-key"
   ```

4. **Start gateway:**
   ```bash
   python main.py --port 8085
   ```

5. **Configure Claude Code:**
   ```bash
   export ANTHROPIC_BASE_URL="http://localhost:8085"
   export ANTHROPIC_AUTH_TOKEN="your-secret-key"
   unset ANTHROPIC_API_KEY
   ```

### Important Notes

- **Keep Kiro IDE running** - it handles token refresh
- **Gateway auto-reloads** when Kiro IDE updates the token file
- If you see 401/403 errors, check if Kiro IDE is still logged in

## Troubleshooting

### Error: 401 Unauthorized during refresh

**Cause**: IdC token cannot be refreshed via standard endpoints

**Solution**: 
1. Ensure Kiro IDE is running and logged in
2. Gateway will automatically reload the updated token file

### Error: 403 Forbidden on API calls

**Cause**: Token expired and Kiro IDE hasn't refreshed it yet

**Solution**:
1. Open Kiro IDE and perform any action to trigger token refresh
2. Gateway will detect the file change and reload

### Error: Token file not found

**Cause**: Wrong path or Kiro IDE not logged in

**Solution**:
```bash
# Find all token files
ls -la ~/.aws/sso/cache/*.json

# Check which one has valid tokens
for f in ~/.aws/sso/cache/*.json; do
  echo "=== $f ==="
  cat "$f" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'authMethod: {d.get(\"authMethod\", \"N/A\")}')"
done
```

## Technical Details

### Token File Locations

| Source | Location |
|--------|----------|
| Kiro IDE (Social) | `~/.aws/sso/cache/kiro-gateway-auth.json` |
| Kiro IDE (IdC) | `~/.aws/sso/cache/kiro-auth-token.json` |
| kiro-cli | `~/.local/share/kiro-cli/data.sqlite3` |
| Device Registration | `~/.aws/sso/cache/{clientIdHash}.json` |

### File Change Detection

The gateway uses polling-based file monitoring:
- Check interval: 30 seconds
- Compares file modification time
- Reloads credentials on change

### Security Considerations

- Token files contain sensitive credentials
- Ensure proper file permissions: `chmod 600 ~/.aws/sso/cache/*.json`
- Do not commit token files to version control
