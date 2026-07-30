# ===== 本地修改：slim 基础镜像 + 国内镜像加速 =====
# slim 比完整版小 ~1GB（纯 Python 项目 + manylinux 预编译 wheel，无需编译工具链）
# docker.m.daocloud.io 为 docker.io 镜像（本网络 docker.io 被 DNS 污染不可达）
FROM docker.m.daocloud.io/library/python:3.11-slim

# 安装 Node.js （满足 >=18）及必要工具
# apt 源切换清华镜像（容器构建网络到 deb.debian.org 的 80 端口不通）
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
  && apt-get update \
  && apt-get install -y --no-install-recommends nodejs npm \
  && rm -rf /var/lib/apt/lists/*

# 从 uv 官方镜像复制 uv（ghcr.nju.edu.cn 为 ghcr.io 镜像）
COPY --from=ghcr.nju.edu.cn/astral-sh/uv:0.9.26 /uv /uvx /bin/

WORKDIR /app

# 先复制依赖描述文件以利用缓存
COPY package.json package-lock.json ./
COPY frontend/package.json frontend/package-lock.json ./frontend/
COPY backend/pyproject.toml backend/uv.lock ./backend/

# 安装依赖（Node + Python）
# npm 走 npmmirror（registry.npmjs.org 本网络单请求 10s+，npm ci 必超时）
RUN npm ci --registry=https://registry.npmmirror.com \
  && npm ci --prefix frontend --registry=https://registry.npmmirror.com \
  && cd backend && uv sync --frozen

# 复制项目源码
COPY . .

EXPOSE 3000 5001

# 同时启动前后端（开发模式）
CMD ["npm", "run", "dev"]