# mai journal

个人 maimai AP 图鉴、完成统计、封面生成与曲库维护工具。

前端使用 React、TypeScript 和 Vite；后端使用 FastAPI 和 SQLite。曲库以 `(id, difficulty)` 为主键，字段冲突由用户裁决，版本解析不调用 LLM。详见 [曲库结构与来源](docs/catalog-schema-and-sources.txt) 和 [更新流程](docs/catalog-update.txt)。

## 开发检查

需要 Python 3.12 或 3.14、Node.js 24。CI 在 Linux 上执行同样的检查。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip==26.2.1
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m ruff check backend scripts tests
.venv/bin/python -m ruff format --check backend scripts tests
.venv/bin/python scripts/test_python.py

cd frontend
npm ci
npx playwright install --with-deps chromium
npm run lint
npm run format:check
npm run test:ci
```

Python 测试使用临时曲库、合成 TSV 和 Remy 页面，禁止外网；浏览器测试使用本地 mock API 与生成的图片。测试不需要个人成绩、查分 token、真实游戏素材或任何服务器凭据。

格式修改使用 `ruff format backend scripts tests` 和前端的 `npm run format`。页面样式统一放在 `frontend/styles.css`，遵循 [前端约定](frontend/AGENTS.md)。

## 运行现有应用

测试和前端构建可从干净 checkout 执行；完整应用还需要单独准备曲库、素材和个人数据。这些不随源码发布：

- `data/catalog/`：曲库快照、曲绘、原始来源、人工裁决及 Remy 证据。
- `data/assets/`：版本底图、logo、字体及游戏 UI 素材。
- `backend/runtime/`：个人成绩、偏好、凭据及临时导出。
- 初次导入需要四份 AP TSV；已经完成导入的个人数据库应连同导入元信息迁移。

已有数据的本机环境使用 `./start-app.sh`，打开 `http://127.0.0.1:8766/`。数据相关接口见 [API 说明](backend/API.txt)。

当前运行模型为本机单用户、单 Uvicorn worker。公网登录认证、服务器部署、QQ 接入和完整 PWA 发布仍待实现；不要仅改变监听地址就公开服务，也不要使用多个 worker 共享现有任务目录。

## 持续集成

推送、Pull Request 或手动触发 [CI](../../actions/workflows/ci.yml) 时执行：

- Python 3.12/3.14：Ruff lint、格式、隔离回归测试。
- Node.js 24：ESLint、Prettier、样式规则、TypeScript、构建及 Chromium UI/导出测试。
- Python/npm 已知依赖漏洞检查。
- 仓库内容检查：禁止提交个人数据库、凭据、生成媒体和大文件。

Actions 固定提交 SHA，默认只有读取权限，不使用部署凭据；成功构建的前端作为短期 artifact 保存。Dependabot 每周检查 Actions、npm 和 Python 依赖。CI 不访问个人服务器，不自动部署。

公开仓库不包含下载素材或字体的再分发授权。添加新资源时应单独确认其来源和使用条件。
