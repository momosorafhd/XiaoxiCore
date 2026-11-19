# 局域网管理台与可选 DSH 单点登录

本机用户直接使用 `http://127.0.0.1:18910`。下面两步可以独立选择。第一步让局域网设备通过 HTTPS 登录 Core；第二步让同一主人的浏览器会话也能进入 DSH 网页。

## 让局域网浏览器访问 Core

示例域名为 `assistant.home.arpa`，示例网段为 `10.50.0.0/24`。将其替换为使用者自己的内网 DNS 名称和网段。为域名准备可信证书，在管理台机器上配置 Nginx，并将示例中的证书路径替换为实际位置。Core 和 Nginx 使用同一网络命名空间。

将下列 `http` 对象合并到现有实例配置，保留其余字段。域名有非默认端口时，将完整的 `主机:端口` 同时用于 `origin` 和反代的 `Host`。

```json
{"http":{"host":"127.0.0.1","port":18910,"origin":"https://assistant.home.arpa","trusted_proxy":"127.0.0.1","allowed_networks":["127.0.0.0/8","10.50.0.0/24"]}}
```

采用 [Core 站点示例](../examples/lan/core.nginx.conf)，配置完成后运行 `nginx -t` 并重载 Nginx、重启 Core。浏览器访问域名，检查登录、模块列表和退出。匿名请求管理页应转到登录，外部访问 `/api/bridge/` 和内部认证路径应得到 404。

## 给 DSH 网页加统一登录

以下示例使用同一主机名的另一个 HTTPS 端口 `9443`。两个入口共享主人 Cookie，代理只向 DSH 转发由宿主签发的 DSH Cookie。普通 DSH 会话桥和 QQ 收发不依赖此项。需要可用的 DSH Web profile、运行于同一本机用户下的 Core 和 DSH，以及带 `auth_request` 模块的 Nginx。

1. 在仓库外建立由服务用户拥有的 `0700` 专用目录。选择其中尚未使用的 Unix 套接字路径。将 [网页插件](../integrations/owner-sso/plugin.mjs) 加到 DSH profile 的 `insert` 数组。例中的 `origin` 为实际 DSH 网页的公开来源，端口必须和浏览器一致。

   ```yaml
   - insert:
       - id: xixi-owner-gateway
         name: file:///absolute/path/to/integrations/owner-sso/plugin.mjs
         config:
           origin: https://assistant.home.arpa:9443
           socketPath: /absolute/private/directory/owner.sock
   ```

2. 使用 `secrets.token_hex(32)` 单独生成一个 64 位小写十六进制网关令牌。在服务环境中设置 `XIXI_PLATFORM_GATEWAY_TOKEN`、`XIXI_PLATFORM_GATEWAY_SSO_SOCKET`（上面的套接字）和 `XIXI_PLATFORM_GATEWAY_RETURN_URLS=https://assistant.home.arpa:9443/`。在 Nginx 的受限 include 文件中设置指令 `proxy_set_header X-Xixi-Gateway-Token "实际令牌";`。只有反代内部认证位置使用这个 include；Core 和代理配置使用同一令牌。避免将展开的配置转储到日志、聊天或工单。
3. 采用 [DSH 站点示例](../examples/lan/dsh-sso.nginx.conf)。示例的上游假设 DSH Web 仅在本机 `http://127.0.0.1:3080` 监听。按部署者的实际设置替换；若上游使用 TLS，应配置可信上游证书和名称校验。浏览器从代理访问 DSH。套接字由插件创建并设为 `0600`，停用插件会关闭由它跟踪的网页连接。
4. 测试匿名打开 DSH 页会跳到 Core 登录，完成后回到 DSH；浏览器打开会话后退出主人登录，再访问 DSH 应再次要求登录。Core 的 `serviceLinks` 可添加 DSH 入口。只有显式列入 `RETURN_URLS` 的 HTTPS 地址可用于回跳。

不同系统账号、分容器或已有多个门户时，需设计套接字访问权限、浏览器可达域名和回跳清单。不要放宽整个私有目录的权限。去掉可选站点、卸载网页插件、撤销本段专属环境变量后，Core 可以继续使用自己的主人登录。
