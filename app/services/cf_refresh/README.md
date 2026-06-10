# cf_refresh - Cloudflare cf_clearance 自动刷新

通过 [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) 自动获取 Cloudflare `cf_clearance` cookie 和 `user_agent`，并更新到 Grok2API 服务配置中。

全自动、无需 GUI、服务器友好。

## 工作原理

1. FlareSolverr（独立 Docker 容器）内部运行 Chrome，自动通过 CF 挑战
2. cf_refresh 作为 grok2api 的后台任务，调用 FlareSolverr HTTP API 获取 `cf_clearance` 和 `user_agent`
3. 直接在进程内调用 `config.update()` 更新运行时配置并持久化到 `data/config.toml`
4. 按设定间隔重复以上步骤

## 配置方式

所有配置均可在管理面板 `/admin/config` 的 **CF 自动刷新** 区域中设置，也可直接编辑 `data/config.toml` 的 `[proxy]` 区域：

| 配置项 | TOML 键 | 推荐值 | 说明 |
|--------|----------|--------|------|
| 启用自动刷新 | `proxy.enabled` | `true` | 是否开启自动刷新 |
| FlareSolverr 地址 | `proxy.flaresolverr_url` | `http://flaresolverr:8191` | FlareSolverr 服务的 HTTP 地址 |
| 刷新间隔（秒） | `proxy.refresh_interval` | `600` | 定期刷新间隔 |
| 挑战超时（秒） | `proxy.timeout` | `180` | CF 挑战等待超时 |
| 浏览器指纹 | `proxy.browser` | `chrome142` | curl_cffi / CF 刷新使用的浏览器指纹 |

旧版环境变量 `FLARESOLVERR_URL`、`CF_REFRESH_INTERVAL`、`CF_TIMEOUT` 仍作为兼容入口保留：仅当 `data/config.toml` 中没有 `proxy.flaresolverr_url` 时，启动阶段才会用环境变量写入初始配置。

> **代理**：自动使用「代理配置 → 基础代理 URL」，无需单独设置，保证出口 IP 一致。

## 使用方式

### Docker Compose 部署

推荐把 `grok2api` 和 `flaresolverr` 放在同一个 Docker Compose 网络里，`grok2api` 通过服务名访问 FlareSolverr：

```yaml
services:
  grok2api:
    depends_on:
      - flaresolverr

  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    restart: unless-stopped
```

对应配置写在 `data/config.toml`：

```toml
[proxy]
enabled = true
flaresolverr_url = "http://flaresolverr:8191"
refresh_interval = 600
timeout = 180
browser = "chrome142"
```

## 注意事项

- `cf_clearance` 与请求来源 IP 绑定，FlareSolverr 自动使用代理配置中的基础代理 URL 保证出口 IP 一致
- 启用自动刷新后，代理配置中的 CF Clearance、浏览器指纹和 User-Agent 由系统自动管理（面板中变灰）
- 建议刷新间隔不低于 5 分钟，避免触发 Cloudflare 频率限制
- FlareSolverr 需要约 500MB 内存（内部运行 Chrome）
