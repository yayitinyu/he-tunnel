# HE IPv6 tunnel for Linux

为使用 systemd 的 Linux 主机配置 Hurricane Electric Tunnelbroker 的 6in4 隧道。需要 Python 3.8+、`iproute2`、固定公网 IPv4，以及网络允许 IPv4 **协议 41** 双向通过。脚本不会修改防火墙、DNS 或现有网络管理器。

脚本会创建 `he-ipv6` SIT 接口，配置 HE 的 Client IPv6 地址，并将 Routed /64 中的 `::1/128` 配给该主机。隧道 MTU 为 1480。默认只在当前没有 IPv6 默认路由时添加一条经 HE 服务器的默认路由。

## 预览和安装

先从 HE 页面填写五项地址。下面的变量需要替换为你自己的值；不要把主机配置或凭据提交到公开仓库。

```sh
HE_SERVER_IPV4='从 HE 页面复制 Server IPv4 Address'
HE_CLIENT_IPV4='从 HE 页面复制 Client IPv4 Address'
HE_SERVER_IPV6='从 HE 页面复制 Server IPv6 Address，包含 /64'
HE_CLIENT_IPV6='从 HE 页面复制 Client IPv6 Address，包含 /64'
HE_ROUTED_PREFIX='从 HE 页面复制 Routed /64'

python3 he_tunnel.py preview \
  --server-ipv4 "$HE_SERVER_IPV4" \
  --client-ipv4 "$HE_CLIENT_IPV4" \
  --server-ipv6 "$HE_SERVER_IPV6" \
  --client-ipv6 "$HE_CLIENT_IPV6" \
  --routed-prefix "$HE_ROUTED_PREFIX"
```

`preview` 只校验地址和显示配置，不修改系统。确认输出后，将 `preview` 改为 `install` 并以 root 执行：

```sh
sudo python3 he_tunnel.py install \
  --server-ipv4 "$HE_SERVER_IPV4" \
  --client-ipv4 "$HE_CLIENT_IPV4" \
  --server-ipv6 "$HE_SERVER_IPV6" \
  --client-ipv6 "$HE_CLIENT_IPV6" \
  --routed-prefix "$HE_ROUTED_PREFIX"
```

如果 HE 的公网 Client IPv4 经云平台 NAT 映射到主机的私网 IPv4，在上述命令中**额外**加入 `--local-ipv4 '主机网卡上的 IPv4'`。只有云平台能将协议 41 双向转发到该私网地址时，6in4 才能连通。脚本会检查该本机地址是否存在、到 HE 服务器的 IPv4 路由是否使用它。

`--default-route auto` 是默认值；`yes` 总是添加度量值为 2048 的 IPv6 默认路由，`no` 不添加。即使设为 `yes`，已有度量值更低的原生 IPv6 默认路由仍可能优先使用。

安装会写入 `/etc/he-tunnel/config.json`、`/usr/local/sbin/he-tunnel` 和 `/etc/systemd/system/he-tunnel.service`，然后启动并启用服务。已有同名文件或接口时会拒绝覆盖。

## 防火墙和检查

如果主机使用 UFW 且默认拒绝入站，需要单独放行来自 HE 服务器的 IPv4 协议 41。UFW 的协议名称是 `ipv6`，它表示 IPv4 协议号 41，不是任意 IPv6 入站流量：

```sh
sudo ufw allow in from "$HE_SERVER_IPV4" to any proto ipv6
```

云平台安全组也需允许相同协议。安装后检查：

```sh
systemctl status he-tunnel.service
ip tunnel show he-ipv6
ip -6 addr show dev he-ipv6
ip -6 route show default
ping -6 -I he-ipv6 "${HE_SERVER_IPV6%/*}"
```

暂停服务：`sudo systemctl stop he-tunnel.service`；重新启动：`sudo systemctl start he-tunnel.service`。服务停止时只删除与配置中的本机和 HE 服务器 IPv4 端点匹配的 `he-ipv6` 接口。故障日志：`journalctl -u he-tunnel.service -b`。

HE 页面上的 Tunnel ID、DNS、rDNS 和可选 Routed /48 不参与接口配置；Routed /48 未分配不影响已分配的 Routed /64。本脚本只给本机配置 Routed /64 的 `::1`。若要将整个 /64 提供给容器或下游设备，还需另行配置地址、IPv6 转发及防火墙策略。
