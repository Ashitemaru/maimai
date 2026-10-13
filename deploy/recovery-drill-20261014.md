# 2026-10-14 恢复演练

本次用真实加密备份恢复到全新的 Ubuntu 26.04 rootfs，在独立 systemd 容器中启动 HTTPS 和应用，网页与数据库验收通过。容器共享宿主内核；这不是独立服务器切换，也不是当前线上数据的快照。

## 恢复材料

- 备份清单时间：2026-10-13 17:08:37（Asia/Shanghai）。
- 恢复代码：`501e510c640364d011ff6083c36972d4c4fdf27e`。晚间新增成绩以及之后发布的代码不在这份快照中。
- 官方 Ubuntu 26.04 minimal amd64 基础镜像 SHA-256：`5f8b1deb393886bd8c5d906d642ef220b656766a5f16fda98a19e0ad0c39c7db`。
- restic 解密内容和应用备份清单均通过校验；恢复文件、SQLite 完整性与行数已核对。
- Python 3.14.4、新建 venv；使用与备份锁文件完全匹配的 19 个固定哈希 wheel 离线安装，未复制线上 venv。
- 恢复的生产 Master Key 哈希可解析。演练使用另行生成的临时 Master Key，清除了恢复出的 6 个旧登录会话；测试密钥不写入报告。

## 已完成的真实请求验收

2026-10-14 00:07:42（Asia/Shanghai），独立 Caddy 和应用进程返回：

| 检查 | 结果 |
| --- | --- |
| HTTPS | 使用演练 CA 验证证书通过；未跳过 TLS 校验 |
| 登录 | Master Key 登录成功；Cookie 带 Secure、HttpOnly |
| 未登录访问 | 私有 API 返回 401 |
| `/api/ready` | 成功，代码 SHA 与恢复快照相同 |
| AP 图鉴 | 5502 张参与统计、1447 AP、4055 未完成、0 冲突 |
| 成绩 | provider 表 2330 条；这是备份当时的数量 |
| 机厅 | 45 家，45 个定位 |
| 待核 buffer | 0 条 |
| 其他 API | 完成统计、偏好、人工裁决和一张真实曲绘读取通过 |
| 数据库 | personal SQLite integrity_check 为 ok |
| 自动任务 | 部署、备份 timer 均保持 disabled |

## 运行环境与边界

开始尝试 QEMU TCG 独立内核 VM。其 AppArmor profile 加载耗时过长，netplan 的 udev reload 超时；最小化启动后网页服务进入启动阶段，SSH 因云镜像关闭 cloud-init 后尚未生成 host keys 而失败。因此未把这次 VM 启动记为完整验收。新机器首次设置 SSH 时应生成自己的 host keys，不能恢复旧机器的 host keys。

随后在同一新 rootfs 使用 systemd-nspawn，独立 PID/mount/network 命名空间，以原应用用户和 systemd 单元运行。应用由容器自己的 Caddy 提供 HTTPS。外网由用户态 SLIRP 接入，不更改宿主路由或防火墙；未切换公网域名。云初始化、snap、SSH 以及整批 AppArmor profile 加载不属于此次容器验收。宿主内核的既有安全策略仍然有效。

## 华立依赖复建

外部运行库从固定上游提交重新下载，六个文件逐一核对哈希；运行库 venv 重新创建，未复制线上 Wine prefix。Mihomo 使用固定 SHA-256 的官方二进制，节点从加密备份私密提取，重新生成本地 SOCKS 凭据并恢复专用用户、权限、broker drop-in。容器内的网络 namespace、nftables 与 TUN 启动，网络结构检查通过。

2026-10-14 00:20:48（Asia/Shanghai），后续验收通过：

| 检查 | 结果 |
| --- | --- |
| 新 Wine prefix | 首次初始化超时后，第二次 DLL exports 探测成功 |
| 实际应用 API | HTTPS 鉴权后的华立配置探测返回 HTTP 200、`probe=network`、`account_access=false` |
| 代理失效 | 容器内停止 egress 后出站请求失败；恢复后同一请求成功，无直连回退 |
| 网络边界 | 容器默认路由及 forwarding 开关在检查前后不变；未修改宿主路由或防火墙 |
| 代理配置 | 新 SOCKS 凭据、专用用户与 0640 文件权限检查通过 |
| 安装文件 | app venv 1181 个、runtime venv 775 个带 RECORD 哈希的文件均匹配 |
| 源码与外部文件 | 恢复 release 的全部 45 个文件与原始备份一致；6 个上游文件 SHA-256 均匹配 |

容器默认遮蔽的 `/proc` 曾阻止嵌套 bubblewrap 挂载自己的 `/proc`，因此只在容器挂载了新的私有 procfs，再使用原 broker 用户、systemd 限制、bubblewrap 及网络 namespace 完成探测；宿主挂载未改变。此次证明的是依赖和网络可重建，没有提交玩家二维码、登录游戏账号或上传成绩。

## 演练中发现的问题

本次 QEMU 磁盘挂载一度传播到已有服务的 mount namespace；断开 NBD 前没有完全卸载所有副本，使演练中新建 runtime venv 的部分文件未写回。原始加密备份没有损坏。清理仅针对演练挂载，在临时复制的 mount namespace 中移除，未重启或改动生产服务。

重新安装外部 venv 后对两套 venv 全量验证 RECORD，对恢复代码验证原备份哈希，再重复真实配置和代理断网探测，全部通过。以后挂载恢复磁盘应先创建私有 mount namespace，再挂载磁盘；结束时先停进程、sync、正常卸载所有挂载、确认无残留，再断开 NBD。不能仅凭 pip check 或“安装命令成功”判断文件完整。

## 验收范围

此结果覆盖真实备份到独立新 rootfs、新 venv、新服务进程的恢复。未进行公网 DNS/证书切换、新服务器 SSH 信任更换、用户设备重新登录或新的玩家账号同步；也未把未完成的独立内核 VM 启动记为通过。正式灾备切换仍按 [备份与恢复](backup.md) 执行这些步骤。

## 收尾

服务停止后已执行 sync、正常卸载恢复磁盘、断开 NBD；所有进程的 mountinfo 中演练挂载残留为 0，两块 NBD 设备容量均回到 0。容器、用户态网络进程及 VM 均已停止。明文恢复目录、临时 SSH/测试密钥、下载的私有运行库和离线安装临时目录已删除，只保留不含凭据的验收结果。生产服务恢复检查使用生产自身 `/api/ready`，没有把演练代码或数据写回线上。
