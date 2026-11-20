# 备份与恢复

## 正常运行

服务器每天北京时间 **04:15** 由 `mai-journal-backup.timer` 运行 `backup.py backup`，失败后 15 分钟重试，最多三次。网站不停机。

1. 用 SQLite 在线备份 API 复制个人库、人工裁决库和登录会话库（不存在时跳过），每份都做 `integrity_check`。
2. 复制水鱼凭据 `secrets.json`、B 站登录 `bilibili-session.json`（若存在）。
3. 持有曲库发布锁，复制曲库目录（不含 `updates/` 和 `remy-cache/`）、`updates/status.json`、当前及前一个曲库快照，以及 `current` 指针。
4. 复制 `shared/assets`、`/etc/mai-journal/app.env`、Caddyfile、sudoers 和本站 systemd 单元。
5. 写入 `manifest.json`（schema 4），用 restic 加密增量写入 `/var/backups/mai-journal/repository`（host `mai-journal`，tag `daily`），抽查十二分之一数据包（每月 1 日全量读取），然后按 **7 日、4 周、6 月** 清理旧快照。

代码不备份：每个部署过的提交都有 GitHub release `app-<SHA>`。缩略图、导出图片、缓存和更早的曲库快照也不备份。仓库密码在 `/etc/mai-journal/backup-password`（root 0600），另存密码管理器；丢失后无法解密。

状态：

```sh
systemctl list-timers mai-journal-backup.timer
journalctl -u mai-journal-backup.service -n 30
cat /var/backups/mai-journal/status.json
```

## 安装

```sh
install -o root -g root -m 0755 deploy/backup.py /opt/mai-journal/
python3 /opt/mai-journal/backup.py init      # 仓库已存在时不做任何事
install -m 644 deploy/mai-journal-backup.service deploy/mai-journal-backup.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now mai-journal-backup.timer
systemctl start mai-journal-backup.service
```

## Mac 异地副本

Mac 上的 `backup_offsite.py` 每小时通过 SSH 用 `restic copy` 把服务器仓库的快照复制到 `~/Backups/mai-journal`（独立密码，独立保留 7 日、4 周、6 月）。配置在 `~/Library/Application Support/mai-journal-backup/`，安装方法见 `com.maijournal.backup.plist.example`。服务器端只要保持仓库路径、密码和 host/tag 不变，Mac 端无需改动。

## 恢复

恢复到一个新目录并校验，不会碰线上数据：

```sh
python3 /opt/mai-journal/backup.py restore --snapshot latest --destination /var/tmp/mai-journal-restore
```

`--verify` 让 restic 核对解密后的内容，脚本再检查 manifest、每个数据库的 `integrity_check` 和曲库快照是否齐全。目录里是明文个人数据，检查完删除。

整机故障时：

1. 按 [部署文档](README.md) 准备新服务器（用户、Python、Caddy、目录、sudoers、单元），先不启动应用。
2. 把恢复出的 `payload/shared` 复制到 `/srv/mai-journal/shared`，属主 `maimai:maimai`；`payload/system/etc/mai-journal/app.env` 放回 `/etc/mai-journal/`（root 0600）。其余 `payload/system` 下的文件对照新机器手动安装。
3. 清空 `shared/runtime/auth.sqlite3` 的 `sessions` 表，让设备重新登录。
4. 在 GitHub Actions 里重跑 manifest 中 `release_sha` 对应提交（或最新 main）的部署 job，CI 会上传代码并启动应用。
5. 检查 `/api/ready`、登录、AP 数量、待核记录、机厅和人工裁决；更新 Mac 的 `known_hosts` 后恢复异地拉取。
