FROM node:22-alpine AS base

# ==========================================
# Mirrors for Chinese users (build args)
#   NPM_REGISTRY: npm registry mirror URL
# ==========================================
ARG NPM_REGISTRY=https://registry.npmjs.org/

WORKDIR /repo

# 使用 corepack 管理 pnpm（与仓库 Volta 版本一致）
RUN npm install -g pnpm@10.28.0

# 配置 npm 镜像源
RUN npm config set registry ${NPM_REGISTRY}

FROM base AS deps

# 先拷贝依赖清单以利用缓存（workspace 需要根配置）
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml turbo.json /repo/
COPY apps/web/package.json /repo/apps/web/package.json

# 只安装 web 所需依赖（含其依赖链）
RUN pnpm install --frozen-lockfile --filter web... --registry ${NPM_REGISTRY}

FROM deps AS build

# BOB 版本构建参数
ARG BOB_RELEASE=false

# 限制 Node.js 内存以防止 OOM（服务器内存有限）
ENV NODE_OPTIONS="--max-old-space-size=2048"

COPY apps/web/ /repo/apps/web/

# 传入 BOB_RELEASE 环境变量进行构建
RUN BOB_RELEASE=${BOB_RELEASE} pnpm --filter web build

FROM base AS runner

ENV NODE_ENV=production \
    PORT=3000

# 生产依赖
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml /repo/
COPY apps/web/package.json /repo/apps/web/package.json
RUN pnpm install --frozen-lockfile --prod --filter web...

# 构建产物
COPY --from=build /repo/apps/web/build /repo/apps/web/build

EXPOSE 3000

CMD ["pnpm", "--filter", "web", "start"]