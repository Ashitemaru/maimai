# mai journal

个人 maimai AP 图鉴、完成统计、封面生成与曲库维护工具。

前端使用 React、TypeScript 和 Vite；后端使用 FastAPI 和 SQLite。曲库以 `(id, difficulty)` 为主键，字段冲突由用户裁决，版本解析不调用 LLM。详见 [曲库结构与来源](docs/catalog-schema-and-sources.txt) 和 [更新流程](docs/catalog-update.txt)。

## 开发检查

需要 Python 3.12 或 3.14、Node.js 24。CI 在 Linux 上执行同样的检查。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip==26.2.1
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m ruff check backend scripts tests deploy
.venv/bin/python -m ruff format --check backend scripts tests deploy
.venv/bin/python scripts/test_python.py

cd frontend
npm ci
npx playwright install --with-deps chromium
npm run lint
npm run format:check
npm run test:ci
```

Python 测试使用临时曲库、合成 TSV 和 Remy 页面，禁止外网；浏览器测试使用本地 mock API 与生成的图片。测试不需要个人成绩、查分 token、真实游戏素材或任何服务器凭据。

格式修改使用 `ruff format backend scripts tests deploy` 和前端的 `npm run format`。页面样式统一放在 `frontend/styles.css`，遵循 [前端约定](frontend/AGENTS.md)。

## 运行现有应用

测试和前端构建可从干净 checkout 执行；完整应用还需要单独准备曲库、素材和个人数据。这些不随源码发布：

- `data/catalog/`：曲库快照、曲绘、原始来源、人工裁决及 Remy 证据。
- `data/assets/`：版本底图、logo、字体及游戏 UI 素材。
- `backend/runtime/`：个人成绩、偏好、凭据及临时导出。
- 初次导入需要四份 AP TSV；已经完成导入的个人数据库应连同导入元信息迁移。

已有数据的本机环境使用 `./start-app.sh`，打开 `http://127.0.0.1:8766/`。数据相关接口见 [API 说明](backend/API.txt)。

应用使用 factory 启动，保持单用户、单 Uvicorn worker：

```sh
.venv/bin/python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8766 --workers 1
```

服务器安装、数据迁移、systemd 单元和恢复步骤见 [部署文档](deploy/README.md)。公网入口使用 HTTPS 和应用内的 Master Key 登录页，无需用户名或用户系统；后端只监听本机。登录有效期为 30 天，可主动退出，刷新和服务重启不会丢失登录状态。生产模式会检查认证配置、已导入的个人数据库、曲库和素材；`/api/ready` 用于确认当前发布版本及数据库可读状态。

网页包含在线 PWA 清单和主屏幕图标，可在 iPhone 的 Safari 中添加到主屏幕。手机导出生成整张 PNG 后提供保存或分享操作；没有离线缓存和离线写入功能。QQ 接入尚未实现。

## 持续集成

推送、Pull Request 或手动触发 [CI](../../actions/workflows/ci.yml) 时执行：

- Python 3.12/3.14：Ruff lint、格式、隔离回归测试。
- Node.js 24：ESLint、Prettier、样式规则、TypeScript、构建及 Chromium UI/导出测试。
- Python/npm 已知依赖漏洞检查。
- 仓库内容检查：禁止提交个人数据库、凭据、生成媒体和大文件。

Actions 固定提交 SHA，默认只有读取权限；成功构建的前端作为短期 artifact 保存。`main` 的 push 通过所有检查后，单独的发布任务以仓库写权限创建 `app-<完整 commit SHA>` release，包含代码、构建后的前端和校验和，不包含运行数据。同一提交重跑时会核对 tag 和资产的实际内容；只有完全一致才通过，不会覆盖已有资产。Dependabot 每周检查 Actions、npm 和 Python 依赖。

服务器每两分钟检查主分支，只部署该提交已成功完成 CI 的 release。部署器校验资产、准备依赖、停止旧进程并备份数据库，然后切换代码；就绪检查失败时恢复旧代码，个人数据库不回滚。部署不需要把服务器 SSH 密钥交给 GitHub Actions，个人数据固定存放在服务器的 `shared` 目录。部署备份位于同一服务器，异机备份需要另行安排。

公开仓库不包含下载素材或字体的再分发授权。添加新资源时应单独确认其来源和使用条件。
