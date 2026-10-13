# 备份与恢复

## 正常运行

服务器每天北京时间 **04:15** 运行 `mai-journal-backup.timer`。重启后会补跑错过的任务；失败最多重试两次，间隔 15 分钟。使用 restic 加密增量备份，保留最近 **7 日、4 周、6 月**的快照（分组按 host/tag，保留规则取并集）。服务器仓库在 `/var/backups/mai-journal/repository`，密码独立保存在 `/etc/mai-journal/backup-password`，两者都只允许 root 访问。不要丢失密码；仓库本身无法找回密码。

备份覆盖：

- 个人 SQLite：正式 AP、待核 buffer、成绩、机厅、偏好、同步记录和投稿整理任务。
- 人工裁决 SQLite、当前及前一版曲库、曲绘、封面素材。
- 当前代码及依赖锁文件、应用与部署环境配置、Caddy 和本站 systemd 单元。
- 登录会话数据库、水鱼凭据文件及 `shared/runtime/bilibili-session.json` 中的 B 站登录凭据（若存在）。凭据只进入加密备份，不进入 Git。
- 华立代理的四个固定恢复文件（若安装）：`/etc/mai-journal-wahlap-proxy/egress.json`、`tun.json`、`/etc/netns/mai-wahlap/resolv.conf`、`/etc/systemd/system/mai-journal-wahlap@.service.d/proxy.conf`。不会扫描代理目录中的其他节点、订阅或临时配置。

代理恢复文件全部不存在时会记录缺失；已有部分文件却缺少其余文件时，备份验证失败，不能把缺失网络隔离配置的快照报告为完整。这些文件在临时备份目录中均收紧到 `0600`，目录 `0700`，不改变线上源文件权限。新版清单使用 schema 3，并继续校验已有的 schema 1 和 2 快照；旧快照没有代理配置时必须重新准备代理后才能启用华立同步，没有 B 站凭据时重新扫码即可。

导出图片、缩略图、缓存、历史部署包、旧曲库快照、Wine 状态和外部华立运行环境不备份。可以重新生成的内容不占长期保留空间。当前及前一曲库可恢复；状态文件内更早的历史记录不代表那些版本仍可回退。

为避免 SQLite、曲库指针和素材之间不一致，备份先持有部署锁，停止主应用并等它完成当前请求，再锁定曲库发布并复制。应用恢复且 `/api/ready` 校验通过后才开始加密写库及保留期清理。网站仅在这段复制期间短暂不可用；不会为了备份强制终止独立华立会话。任何阶段异常都会尝试恢复原先运行的应用；独立 `ExecStopPost` 也会处理进程意外退出留下的恢复标记。原先停用的应用保持停用。

每次备份检查仓库结构及十二分之一的数据包，每月首日做全量读取检查。清理只发生在备份和检查都成功之后。失败不会清除已有快照，保留上次成功时间。状态及日志：

```sh
systemctl list-timers mai-journal-backup.timer
systemctl status mai-journal-backup.service
journalctl -u mai-journal-backup.service -n 30
cat /var/backups/mai-journal/status.json
```

手动执行不会提交二维码或访问游戏账号：

```sh
systemctl start mai-journal-backup.service
python3 /opt/mai-journal/backup.py check
```

## 安装服务器任务

安装 restic 后，把 `backup.py`、`backup_data.py` 放到 `/opt/mai-journal/`，root 所有，0755。创建 root 0700 的 `/var/backups/mai-journal`，使用密码管理器或 `secrets.token_urlsafe(48)` 生成密码，直接写进 root 0600 文件，不要放进命令参数或日志。现有密码不得覆盖。初始化与注册：

```sh
python3 /opt/mai-journal/backup.py init
install -m 644 deploy/mai-journal-backup.service /etc/systemd/system/
install -m 644 deploy/mai-journal-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mai-journal-backup.timer
systemctl start mai-journal-backup.service
```

这些 root 管理的文件与部署引导脚本一样，不会被普通代码发布自动替换。升级备份工具后需单独安装，并再次做隔离恢复演练。

## Mac 异地副本

Mac 通过专用 SSH 配置拉取 restic 快照，再用独立密码加密写入 `~/Backups/mai-journal`。这是独立仓库，不使用镜像删除；服务器清理快照不会直接删除 Mac 的副本。Mac 自己同样保留 7 日、4 周、6 月。

私有配置放在 `~/Library/Application Support/mai-journal-backup/`，目录 0700。内含 `source-password`（服务器仓库密码）、`password`（Mac 仓库独立密码）、`ssh_config`、`known_hosts`，均 0600。Host 使用 `mai-journal-backup`；固定目标 IP、严格校验主机密钥并启用 BatchMode。已知主机密钥须从已验证的 SSH 连接配置复制，不自动信任新的主机。

安装 `backup_offsite.py` 到该目录，使用 `com.maijournal.backup.plist.example` 生成用户 LaunchAgent：填写绝对路径的 Python、restic、配置目录、仓库和日志。先运行 `--init` 再运行一次常规拉取。任务每小时及登录时运行；Mac 休眠或离线不会影响服务器备份，醒来联网后下次任务补拉。无需网站或浏览器保持打开。检查 `status.json` 的 `last_success_at`，不要把“定时任务已加载”当成“副本已完成”。

两份仓库密码都应另存密码管理器。Mac 配置目录含恢复材料，不要上传到 Git 或公开分享。

## 隔离恢复演练

先恢复到**不存在的新目录**，工具拒绝覆盖线上目录、现有目录和备份仓库。恢复过程不启动服务、不覆盖个人数据：

```sh
python3 /opt/mai-journal/backup.py restore --snapshot latest \
  --destination /var/tmp/mai-journal-restore-check
```

restic 先核对解密后文件内容，`backup_data.verify` 再校验清单、每个文件 SHA-256、路径边界、数据库完整性和行数。结果为 verified 才表示数据恢复演练通过。演练目录包含明文个人数据与配置，检查后及时删除该独立目录。

Mac 副本也可独立恢复（无需服务器）：

```sh
restic -r "$HOME/Backups/mai-journal" \
  --password-file "$HOME/Library/Application Support/mai-journal-backup/password" \
  restore latest --host mai-journal --tag daily --verify --target /tmp/mai-journal-restore-check
python3 -c 'from pathlib import Path; from deploy.backup_data import verify; verify(Path("/tmp/mai-journal-restore-check/payload")); print("verified")'
```

此命令验证数据可恢复，不包含服务启动验收。完整恢复还需执行下面的整机恢复顺序；每次实际演练应分别记录文件/数据库、应用 HTTPS、华立沙箱与代理的结果，不能用其中一项代替其他项。

最近一次实际启动验收见 [2026-10-14 恢复演练](recovery-drill-20261014.md)，其中分别记录已通过项目和未覆盖的机器切换步骤。

## 整机故障后的恢复顺序

1. 在新服务器完成 [基础部署](README.md) 的用户、Python、Caddy、目录和权限准备，保持应用及部署定时器停止。取回仓库与匹配密码，完成上述隔离恢复校验。
2. 将 `payload/shared`、`payload/releases` 及相对 `current` 链接复制到空的 `/srv/mai-journal`，设为 `maimai:maimai`；数据库/凭据 0600，私有目录 0700。将 `config/mai-journal` 恢复到 `/etc/mai-journal`，root:root 0600。先将应用环境中的 `MAIMAI_WAHLAP_ENABLED` 设为 `0`，保持取分 socket 停止。手动安装并检查 `config/system` 中的代理、单元及 sudoers，不直接覆盖不同机器的系统配置。
3. 在恢复后的最终 release 路径重建 venv；不要复制旧 venv（启动脚本包含绝对路径）。新版快照包含 `dependencies/manifest.json` 和固定哈希 wheel，使用已审核部署引导代码的 `dependency_bundle(release, check_host=True)` / `Host().install(release)` 验证并离线安装，目标为 CPython 3.14 / Linux x86_64，glibc 下限以清单为准。旧快照没有 wheel 时，须在受控构建机按该快照的 `requirements.lock` 构建并验证匹配的 wheel 包，再传入恢复机；禁止随手换成最新依赖或跳过哈希。灾难恢复后清空 `shared/runtime/auth.sqlite3` 中的 `sessions` 表，让设备重新使用 Master Key 登录，避免恢复已撤销的旧会话。此操作在清单验证之后执行。
4. 从当前 release 的 `deploy/` 恢复 root 管理的部署/备份引导代码。按照 [华立运行环境](wahlap-runtime.md) 重建 Wine/bubblewrap 和独立 worker，安装脚本从备份的 `release_sha` 对应 Git 提交取得，严格使用其 `wahlap-runtime.lock.json`。按照 [独立代理出口](../docs/WAHLAP_PROXY.md) 重装固定哈希的 Mihomo、专用用户和网络服务：从备份的 `egress.json` 私密提取选定节点交给安装器，重新生成成对的本地 SOCKS 口令和 `0640` 对应组权限，恢复 DNS 及 broker drop-in。不要只恢复 broker 基础单元而漏掉 drop-in。二进制、外部运行环境和 `/run` 内的命名空间没有包含在备份里，网页恢复不等于华立链路恢复。完成无账号连接探测与断网不回退检查后，再启用 socket 和 `MAIMAI_WAHLAP_ENABLED=1`；找不到可用出口时维持禁用，网站其他功能仍可恢复。
5. `systemctl daemon-reload` 后启动应用，确认本地 `/api/ready` 中 release_sha 与备份一致，再确认 HTTPS、Master Key 登录、AP 数量、待核 buffer、机厅和人工裁决。无需真实二维码即可做这些检查。
6. 最后恢复部署、备份定时器和 Mac 拉取，重新建立一次加密快照。替换服务器后须独立验证新 SSH 主机密钥并更新 Mac 的 known_hosts。

## 华立维护时段

每天 **Asia/Shanghai 04:00（含）至 07:00（不含）** 禁止发起新的华立同步。后端在接收和真正调用 worker 前都检查；维护期间不探测 worker，不存本次二维码，返回 503、维护原因与 Retry-After。前端用 toast 提示，07:00 后再次点击即可同步，无需刷新。

维护前已经开始的任务继续读取、保存和正常退出游戏，不在边界强行打断。本站 AP 待核记录、查看图鉴以及显式上传水鱼保持可用。维护结束不自动重试，也不保留二维码等待定时执行。
