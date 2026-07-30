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
