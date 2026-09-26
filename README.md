# IPv6 SIT tunnel for Linux

为使用 systemd 的 Linux 主机配置 SIT（6in4）IPv6 隧道，适用于 Hurricane Electric Tunnelbroker、Route64 等提供 SIT 的服务商。需要 Python 3.8+、`iproute2`、固定公网 IPv4，以及网络允许 IPv4 **协议 41** 双向通过。脚本不会修改防火墙、DNS 或现有网络管理器。

脚本会创建 `he-ipv6` SIT 接口，配置客户端 IPv6 地址。提供 Routed /48 至 /64 时，还会将其中的第一个地址 `::1/128` 配给该主机；没有提供时只使用隧道端点地址。隧道 MTU 为 1480。默认只在当前没有 IPv6 默认路由时添加一条经隧道服务器的默认路由。`he-ipv6` 和 `he-tunnel.service` 是历史名称，使用 Route64 时也保留。

## 预览和安装

先从隧道服务商页面填写四项端点地址。页面显示的 IPv6 地址如果没有前缀长度，按同一 /64 添加 `/64`。下面的变量需要替换为你自己的值；不要把主机配置或凭据提交到公开仓库。

```sh
SERVER_IPV4='服务商的 IPv4 端点'
CLIENT_IPV4='本机的公网 IPv4 端点'
SERVER_IPV6='服务商的 IPv6 端点/64'
CLIENT_IPV6='本机的 IPv6 端点/64'

python3 he_tunnel.py preview \
  --server-ipv4 "$SERVER_IPV4" \
  --client-ipv4 "$CLIENT_IPV4" \
  --server-ipv6 "$SERVER_IPV6" \
  --client-ipv6 "$CLIENT_IPV6"
```

`preview` 只校验地址和显示配置，不修改系统。确认输出后，将 `preview` 改为 `install` 并以 root 执行：

```sh
sudo python3 he_tunnel.py install \
  --server-ipv4 "$SERVER_IPV4" \
  --client-ipv4 "$CLIENT_IPV4" \
  --server-ipv6 "$SERVER_IPV6" \
  --client-ipv6 "$CLIENT_IPV6"
```

已分配单独的 Routed /48 至 /64 时，在命令中额外加入 `--routed-prefix '服务商路由到本隧道的前缀'`。不要从隧道端点地址推测路由前缀。

如果公网 Client IPv4 经云平台 NAT 映射到主机的私网 IPv4，在上述命令中**额外**加入 `--local-ipv4 '主机网卡上的 IPv4'`。只有云平台能将协议 41 双向转发到该私网地址时，6in4 才能连通。脚本会检查该本机地址是否存在、到服务器的 IPv4 路由是否使用它。

`--default-route auto` 是默认值；`yes` 总是添加度量值为 2048 的 IPv6 默认路由，`no` 不添加。即使设为 `yes`，已有度量值更低的原生 IPv6 默认路由仍可能优先使用。

安装会写入 `/etc/he-tunnel/config.json`、`/usr/local/sbin/he-tunnel` 和 `/etc/systemd/system/he-tunnel.service`，然后启动并启用服务。已有同名文件或接口时会拒绝覆盖。

## 防火墙和检查

如果主机使用 UFW 且默认拒绝入站，需要单独放行来自服务器的 IPv4 协议 41。UFW 的协议名称是 `ipv6`，它表示 IPv4 协议号 41，不是任意 IPv6 入站流量：

```sh
sudo ufw allow in from "$SERVER_IPV4" to any proto ipv6
```

云平台安全组也需允许相同协议。安装后检查：

```sh
systemctl status he-tunnel.service
ip tunnel show he-ipv6
ip -6 addr show dev he-ipv6
ip -6 route show default
ping -6 -I he-ipv6 "${SERVER_IPV6%/*}"
```

暂停服务：`sudo systemctl stop he-tunnel.service`；重新启动：`sudo systemctl start he-tunnel.service`。服务停止时只删除与配置中的本机和隧道服务器 IPv4 端点匹配的 `he-ipv6` 接口。故障日志：`journalctl -u he-tunnel.service -b`。

服务商页面上的 Tunnel ID、DNS、rDNS 不参与接口配置。本脚本只给本机配置路由前缀的第一个地址。若要将整个前缀提供给容器或下游设备，还需另行配置地址、IPv6 转发及防火墙策略。
