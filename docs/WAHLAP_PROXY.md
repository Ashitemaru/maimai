# 华立独立代理出口

只有华立取分进程使用代理。网站、数据库、曲库更新、水鱼和部署程序继续使用宿主网络。HTTPX 与 Wine DLL 都在同一个独立网络命名空间内，由 TUN 接管连接，不依赖它们是否识别 `HTTP_PROXY`。

```text
主网站 ── 私有 Unix socket ── 取分 broker / bubblewrap / Wine
                                      │ mai-wahlap 网络命名空间
                                      ▼
                                 Mihomo TUN
                                      │ 固定 SOCKS TCP 通道
                                      ▼
宿主网络                       Mihomo 单节点出口 ── 代理节点 ── 华立
```

## 固定资源和权限

| 资源 | 值 |
| --- | --- |
| 命名空间 | `mai-wahlap` |
| 宿主 veth / 命名空间 veth | `mw-host` / `mw-peer` |
| 专用子网 | `10.203.240.0/30`，宿主 `.1`，命名空间 `.2` |
| 内部 SOCKS | `10.203.240.1:17891`，仅允许 `.2` 访问，另有随机口令 |
| TUN | `mw-tun` |
| 二进制及管理程序 | `/opt/mai-journal-wahlap-proxy/` |
| 私密配置 | `/etc/mai-journal-wahlap-proxy/{egress,tun}.json` |
| 状态目录 | `/var/lib/mai-journal-wahlap-{egress,tun}/` |
| DNS | 命名空间内 `127.0.0.1:53` |
| broker 配置 | `/etc/systemd/system/mai-journal-wahlap@.service.d/proxy.conf` |

宿主上的 Mihomo 使用 `mai-wahlap-proxy` 用户，无额外 capability。命名空间内的 Mihomo 使用 `mai-wahlap-tun` 用户，只获得创建 TUN、修改本命名空间路由以及监听 DNS 端口所需的能力。取分 broker 和游戏工作进程不获得这些权限。两个代理配置分别只有 root 与对应的代理用户可读；节点密钥只出现在宿主代理配置里，不挂载给游戏沙箱。代理配置目录与应用的 `/etc/mai-journal` 分离，不放宽应用配置目录权限，也不把代理用户加入 `maimai` 组。

命名空间内的 nftables 仅允许回环、TUN 和通往固定宿主 SOCKS 端口的 TCP。不存在通过宿主的普通默认路由；默认路由在 TUN 就绪后指向 `mw-tun`。不启用宿主 IP forwarding，不添加宿主 NAT，不修改宿主默认路由或防火墙。DNS 使用本地 fake-IP，并经 SOCKS 出口访问 DoH；IPv6 与 SOCKS UDP 关闭。当前链路服务于华立的 TCP/HTTPS 接口，不提供任意 UDP 转发。

## 二进制来源

固定 [MetaCubeX/mihomo v1.19.32](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.32)，Linux amd64 compatible 版本。官方发布资产的压缩包 SHA-256 为：

```text
ba3ce607747a07f948fc35780e108a4a7c7f552a38b9bd4d115f313ebcb89c20
```

管理程序验证该压缩包哈希后才解压安装，并限制压缩包与解压结果尺寸。程序不包含在应用发布包中。Mihomo 的许可证和使用条件以其上游仓库为准。

## 准备节点

从用户已有的 Clash/Mihomo 配置中导出**一个节点对象**到私密 JSON 文件，权限 `0600`。不要导入整个配置，不在命令行传节点密码或订阅链接，不把私密文件放入 Git、应用数据目录或网站可访问路径。

目前支持：

- Shadowsocks：指定的 AEAD cipher。
- Trojan：TCP、WebSocket、gRPC。
- VMess AEAD：TCP、WebSocket、gRPC。
- VLESS：TLS，包括 Reality 的 `public-key`、`short-id` 及 `xtls-rprx-vision`。

只接受已实现的字段；拒绝脚本、插件、外部文件路径、自定义拨号链、导入规则以及关闭证书验证的设置。输出配置始终只有一个 `EGRESS` 节点和 `MATCH,EGRESS`，没有 DIRECT、自动切换或失败回退。节点更换需要重新生成配置；不在运行中的游戏会话内自动切换订阅或出口。

## 安装

需要 Linux x86_64、systemd、iproute2、nftables 和 `/dev/net/tun`。先停止 `mai-journal-wahlap.socket` 接受新连接，再等待所有 `mai-journal-wahlap@*.service` 实例自然结束，避免页面轮询或新请求在安装期间启动工作进程。若已经安装过代理，此时再停止 TUN、出口、网络服务；不要为了改配置中断已登录游戏的工作进程。安装及无账号验收结束后恢复 socket 监听。

在服务器上以 root，从包含管理程序及同目录 systemd 模板的代码目录运行：

```sh
python3 deploy/wahlap_proxy.py install --node-file /root/selected-wahlap-node.json
```

GitHub 下载不稳定时，可预先从上述官方发布下载压缩包，传到服务器后执行：

```sh
python3 deploy/wahlap_proxy.py install \
  --node-file /root/selected-wahlap-node.json \
  --binary-archive /root/mihomo-linux-amd64-compatible-v1.19.32.gz
```

本地压缩包仍必须通过同一个固定哈希。安装器检查节点、工作进程及服务状态，创建低权限用户，离线检验 Mihomo 配置，再安装文件和 broker drop-in。**安装不会启动代理，也不会访问游戏账号。** 安装后新建的 broker 依赖代理；依赖启动失败时 broker 也不能回退到宿主网络。

```sh
systemctl start mai-journal-wahlap-tun.service
python3 /opt/mai-journal-wahlap-proxy/wahlap_proxy.py network-check
```

TUN 服务会依次拉起命名空间和宿主出口。`ExecStartPost` 确认 TUN 设备存在后安装该命名空间的默认路由。`NetworkNamespacePath` 只用于取分 broker；DNS 文件显式通过 `BindReadOnlyPaths` 绑定，原有 bubblewrap 会继承其网络与 DNS。

验收通过后启用开机启动：

```sh
systemctl enable mai-journal-wahlap-tun.service
```

应用的普通发布不会改写这些 root 管理的服务单元或节点配置。更新此基础设施需要重新执行安装步骤。

## 验收与失败处理

按顺序验证，不使用过期二维码，也不使用伪造的账号凭据：

1. 比较安装前后宿主默认路由、`net.ipv4.ip_forward` 与网站健康状态。
2. 在实际 broker/bubblewrap 中进行无账号华立配置探测，要求成功解码内容；HTTP 200 本身不算成功。
3. 通过 DLL 无账号运行库探测确认 Wine 环境仍可用。
4. 确认无活动取分任务后停止代理出口，再从同一网络环境发起无账号探测；必须失败，不得走直连。
5. 恢复出口并确认探测恢复，然后才允许用新二维码测试完整成绩同步及退出会话。

`network-check` 只检查本地网络前置条件，不证明节点或华立可访问。Mihomo 不记录连接日志；管理程序只显示固定的安全错误，禁止把私密配置或原始子进程输出粘贴进日志。节点停机、TUN 退出或底层通道断开时请求失败，不会自动重复登录。游戏会话中途掉线时仍可能无法确认退出，须由应用明确报告该状态。

命名空间创建会拒绝同名外来资源及与宿主路由冲突的子网。首次创建 `/run/netns` 时显式设为 `0755`，使低权限 TUN 进程可以检查命名空间句柄；已有目录权限不合适时会停止，不自动修改别人的目录。失败时只清理本次创建的资源；重复启动已有的受管网络只做检查。停止命名空间会拒绝删除仍有进程运行的网络，避免遗留看不见的命名空间。

停用代理时先禁用华立同步，再等待工作进程结束，最后停止并禁用 TUN、出口和命名空间服务。不要在仍启用华立同步时删除 broker drop-in；那会重新允许取分程序使用宿主直连。

参考：[Mihomo TUN](https://wiki.metacubex.one/en/config/inbound/tun/)、[代理 DNS](https://wiki.metacubex.one/en/config/dns/)、[systemd 网络命名空间](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#NetworkNamespacePath=)。
