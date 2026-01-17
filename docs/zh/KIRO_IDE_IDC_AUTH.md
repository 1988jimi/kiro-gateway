# Kiro IDE IdC 认证支持

## 问题发现

### 背景

在使用 Kiro Gateway 作为 Claude Code CLI 的反向代理时,用户遇到了持续的 401/400 错误:

```
WARNING  | Token pre-refresh failed: Client error '401 Unauthorized' for url 
'https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken'
```

### 根本原因分析

调查发现 **Kiro IDE 的 IdC (AWS Identity Center) 认证** 使用了与标准 Kiro Desktop Auth 或 AWS SSO OIDC 流程不同的 token 机制:

| 认证类型 | Token 文件 | 刷新端点 | 刷新可用 |
|----------|------------|----------|----------|
| Social (Google/GitHub) | `kiro-gateway-auth.json` | `auth.desktop.kiro.dev/refreshToken` | ✅ 是 |
| AWS SSO OIDC (kiro-cli) | `data.sqlite3` | `oidc.{region}.amazonaws.com/token` | ✅ 是 |
| **Kiro IDE IdC** | `kiro-auth-token.json` | **两个端点都不行** | ❌ 否 |

### Token 文件结构对比

**标准 Social Auth Token:**
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

关键区别: **IdC token 有 `clientIdHash` 字段指向单独的设备注册文件**,但这并不意味着可以通过 AWS SSO OIDC 刷新。

### 为什么标准刷新不起作用

1. **Kiro Desktop Auth (401)**: IdC refresh token 格式与 social auth 端点不兼容
2. **AWS SSO OIDC (400)**: Token 不是通过标准 OIDC 流程获取的,所以 OIDC 刷新失败

### Kiro IDE 如何处理这个问题

Kiro IDE 管理自己的 token 生命周期:
- 内部监控 token 过期时间
- 使用其专有机制刷新 token (可能涉及 AWS Cognito)
- 自动更新 `kiro-auth-token.json` 文件

## 解决方案

### 方法: 基于文件的 Token 同步

由于我们无法以编程方式刷新 IdC token,我们与 Kiro IDE 的 token 文件同步:

1. **直接使用文件**: 配置网关直接使用 Kiro IDE 的 token 文件
2. **文件监控**: 监视文件变化并自动重新加载凭证
3. **优雅降级**: 当 token 过期且刷新失败时,从文件重新加载

### 配置

```env
# 直接指向 Kiro IDE 的 token 文件
KIRO_CREDS_FILE="~/.aws/sso/cache/kiro-auth-token.json"
```

### 实现细节

#### 新增 AuthType: KIRO_IDE_IDC

为 Kiro IDE IdC 凭证添加了新的认证类型:

```python
class AuthType(Enum):
    KIRO_DESKTOP = "kiro_desktop"      # Social auth (Google/GitHub)
    AWS_SSO_OIDC = "aws_sso_oidc"       # kiro-cli with AWS SSO
    KIRO_IDE_IDC = "kiro_ide_idc"       # Kiro IDE with Identity Center
```

#### 检测逻辑

```python
def _detect_auth_type(self) -> None:
    # 首先检查 Kiro IDE IdC (有 clientIdHash 但没有内联 clientId/clientSecret)
    if self._client_id_hash and not (self._client_id and self._client_secret):
        self._auth_type = AuthType.KIRO_IDE_IDC
    elif self._client_id and self._client_secret:
        self._auth_type = AuthType.AWS_SSO_OIDC
    else:
        self._auth_type = AuthType.KIRO_DESKTOP
```

#### 文件监听器

调度器现在监控凭证文件的变化:

```python
async def _check_file_updates(self):
    """检查凭证文件是否被 Kiro IDE 更新。"""
    if not self._auth_manager or not self._auth_manager._creds_file:
        return
    
    path = Path(self._auth_manager._creds_file).expanduser()
    if not path.exists():
        return
    
    current_mtime = path.stat().st_mtime
    if current_mtime != self._last_file_mtime:
        logger.info("凭证文件已更新,重新加载...")
        self._auth_manager._load_credentials_from_file(str(path))
        self._last_file_mtime = current_mtime
```

#### IdC 的刷新策略

```python
async def _refresh_token_request(self) -> None:
    if self._auth_type == AuthType.KIRO_IDE_IDC:
        # 无法刷新 IdC token - 改为从文件重新加载
        await self._reload_from_file()
    elif self._auth_type == AuthType.AWS_SSO_OIDC:
        await self._refresh_token_aws_sso_oidc()
    else:
        await self._refresh_token_kiro_desktop()
```

## 使用指南

### 前提条件

1. **安装并登录 Kiro IDE**,使用 IdC (Identity Center / Enterprise SSO)
2. **Kiro IDE 保持运行** - 它自动管理 token 刷新

### 设置步骤

1. **找到你的 token 文件:**
   ```bash
   ls -la ~/.aws/sso/cache/
   # 查找 kiro-auth-token.json 或类似文件
   ```

2. **验证是 IdC 认证:**
   ```bash
   cat ~/.aws/sso/cache/kiro-auth-token.json | grep authMethod
   # 应该显示: "authMethod": "IdC"
   ```

3. **配置网关:**
   ```env
   KIRO_CREDS_FILE="~/.aws/sso/cache/kiro-auth-token.json"
   PROXY_API_KEY="your-secret-key"
   ```

4. **启动网关:**
   ```bash
   python main.py --port 8085
   ```

5. **配置 Claude Code:**
   ```bash
   export ANTHROPIC_BASE_URL="http://localhost:8085"
   export ANTHROPIC_AUTH_TOKEN="your-secret-key"
   unset ANTHROPIC_API_KEY
   ```

### 重要说明

- **保持 Kiro IDE 运行** - 它处理 token 刷新
- **网关自动重载** - 当 Kiro IDE 更新 token 文件时
- 如果你看到 401/403 错误,检查 Kiro IDE 是否仍然登录

## 故障排除

### 错误: 刷新时 401 Unauthorized

**原因**: IdC token 无法通过标准端点刷新

**解决方案**: 
1. 确保 Kiro IDE 正在运行且已登录
2. 网关会自动重新加载更新后的 token 文件

### 错误: API 调用时 403 Forbidden

**原因**: Token 过期且 Kiro IDE 尚未刷新

**解决方案**:
1. 打开 Kiro IDE 并执行任何操作以触发 token 刷新
2. 网关会检测到文件变化并重新加载

### 错误: Token 文件未找到

**原因**: 路径错误或 Kiro IDE 未登录

**解决方案**:
```bash
# 查找所有 token 文件
ls -la ~/.aws/sso/cache/*.json

# 检查哪个有有效的 token
for f in ~/.aws/sso/cache/*.json; do
  echo "=== $f ==="
  cat "$f" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'authMethod: {d.get(\"authMethod\", \"N/A\")}')"
done
```

## 技术细节

### Token 文件位置

| 来源 | 位置 |
|------|------|
| Kiro IDE (Social) | `~/.aws/sso/cache/kiro-gateway-auth.json` |
| Kiro IDE (IdC) | `~/.aws/sso/cache/kiro-auth-token.json` |
| kiro-cli | `~/.local/share/kiro-cli/data.sqlite3` |
| 设备注册 | `~/.aws/sso/cache/{clientIdHash}.json` |

### 文件变化检测

网关使用轮询式文件监控:
- 检查间隔: 30 秒
- 比较文件修改时间
- 变化时重新加载凭证

### 安全注意事项

- Token 文件包含敏感凭证
- 确保正确的文件权限: `chmod 600 ~/.aws/sso/cache/*.json`
- 不要将 token 文件提交到版本控制
