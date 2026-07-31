# MiroFish

## 命令

- 安装全部依赖：`npm run setup:all`（前端 npm + 后端 `uv sync`，见根 package.json）
- 开发模式一键起前后端：`npm run dev`（concurrently 并行，backend:5001 + frontend:3000）
- 后端必须用 **uv**（非 pip）：`cd backend && uv sync`；Python 版本锁定 `>=3.11,<3.13`（backend/pyproject.toml）
- 后端测试：`cd backend && uv run pytest`（测试在 `backend/tests/`，根目录 `tests/` 只是 star 统计脚本测试，非主测试集）
- 后端入口是 `backend/run.py`（`uv run python run.py`），**不是** `flask run`；它负责 Config 校验和 Windows UTF-8 控制台修复
- 前端：`cd frontend && npm run dev / build`（Vite，无 lint/test 配置）

## 约定

- 环境变量：复制根目录 `.env.example` 为 `.env` 放**项目根目录**；`LLM_API_KEY` + `ZEP_API_KEY` 必填，否则 `run.py` 启动时 `Config.validate()` 直接拒绝启动
- `LLM_BOOST_*`（加速 LLM）不用时必须**整行删除**，不能留空值（.env.example 注释明确要求）
- 前端路径别名：`@` → `frontend/src`，`@locales` → **根目录** `locales/`（i18n 文案在前端目录之外，见 frontend/vite.config.js）
- Vite 已把 `/api` 代理到 `localhost:5001`，前端代码里不要写死后端地址
- 后端分层：`app/api/` 只放路由（graph/report/simulation 三个蓝图），业务逻辑在 `app/services/`（Zep 图谱、OASIS 模拟、报告 Agent）

## 禁区与坑

- `backend/uploads/`、`backend/logs/` 是运行时生成目录（已 gitignore），不要提交，也不要手改其中内容
- `zep-cloud` 钉死在 `==3.25.0`；`backend/tests/` 里大量 `test_zep_*` 是针对该版本的契约测试，升级 Zep SDK 前必须全量跑这些测试
- 真实模拟会消耗大量 LLM token，首次验证用 **<40 轮** 小规模模拟（.env.example 官方建议）
- Windows 下不要绕过 `run.py` 直接起 Flask——控制台中文会乱码（run.py 开头的 UTF-8 reconfigure 就是为这个）
- Docker 部署时 `.env` 通过 `env_file` 注入，`backend/uploads` 挂载为 volume（docker-compose.yml），改端口需同时改 compose 和 vite proxy

## Issue 平台：Multica

本项目 issue 台账建在 **Multica**（不建在 GitHub Issues）。CLI：`~/.multica/bin/multica`（用前 `export PATH="$HOME/.multica/bin:$PATH"`）。

- **workspace**：`myhome`（ID `45ca0bde`，默认 workspace）
- **project**：`MiroFish`（ID `8271761a`）
- **label 体系**（workspace 级，用 UUID 前缀）：
  - 类型：`缺陷` bf1b / `优化` ed6b / `决策` d7c6 / `技术债` 46f3 / `疑问` f172
  - 模块：`后端` aa97 / `前端` 4aa0 / `模拟` dd2e / `Docker` 5900 / `Zep` 5b70 / `LLM` 3b64
- **命令范式**：
  - 建：`multica issue create --project 8271761a --title "..." --description-file "<绝对路径>.md" --status done|backlog --priority high|medium|low`（输出 JSON，取 `identifier` 字段即 KEY 如 MYH-208）
  - 加 label：`multica issue label add <KEY> <label-uuid前缀>`（注意是 UUID 前缀不是 name）
  - 查：`multica issue list --project 8271761a --output table`
  - 关闭重复：`multica issue status <KEY> cancelled`
- **注意**：Multica CLI 的 `table` 格式渲染中文会列宽错位，查 label 用 `--output json` + python 解析；网页端中文正常。
- **代码仓库**：代码改动仍提交到 GitHub fork（origin=difeizheng/MiroFish，upstream=666ghj/MiroFish），Multica 只放问题/决策/优化台账。
