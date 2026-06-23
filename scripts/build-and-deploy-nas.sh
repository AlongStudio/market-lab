#!/usr/bin/env bash
# market-lab → NAS 构建发布脚本
# 流程:mac local 测试通过 → buildx linux/amd64 → docker save → scp → NAS docker load → run
# 复用 trade NAS 部署已知坑:
#   1. 含特殊字符的 DB 密码用 --env-file 传(不用 -e,避免多层 SSH/bash 引号解析坏)
#   2. 外网 SSH 传文件用 cat > 管道(外网 SSH 常禁 sftp subsystem)
#   3. market-lab 与 NAS MySQL 需网络互通
#
# 用法:
#   ./scripts/build-and-deploy-nas.sh           # 仅构建+传输+load 镜像
#   ./scripts/build-and-deploy-nas.sh --start    # 并启动容器(读 .env.prod)

set -euo pipefail

NAS_USER="${NAS_USER:-along}"
NAS_HOST="${NAS_HOST:-192.168.1.99}"
NAS_PORT="${NAS_PORT:-222}"
NAS_DOCKER="/usr/local/bin/docker"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NAS_BASE_DIR="/volume1/docker/market-lab"

IMAGE="market-lab:dev"
TAR="/tmp/market-lab-dev.tar"
CONTAINER="market-lab"

START_CONTAINER=false
if [[ "${1:-}" == "--start" ]]; then
    START_CONTAINER=true
fi

echo "📦 [1/4] 构建镜像 (linux/amd64)..."
cd "$PROJECT_DIR"
docker buildx build --platform linux/amd64 -t "$IMAGE" --load .

echo "📤 [2/4] 导出镜像..."
docker save "$IMAGE" -o "$TAR"
ls -lh "$TAR"

echo "🚚 [3/4] 传输到 NAS ($NAS_HOST)..."
ssh -p "$NAS_PORT" "$NAS_USER@$NAS_HOST" "mkdir -p $NAS_BASE_DIR $NAS_BASE_DIR/reports"
scp -O -P "$NAS_PORT" "$TAR" "$NAS_USER@$NAS_HOST:$NAS_BASE_DIR/"

echo "📥 [4/4] NAS 加载镜像..."
ssh -p "$NAS_PORT" "$NAS_USER@$NAS_HOST" \
    "sudo $NAS_DOCKER load -i $NAS_BASE_DIR/market-lab-dev.tar && \
     rm $NAS_BASE_DIR/market-lab-dev.tar && \
     echo '✅ 镜像加载完成'"

rm -f "$TAR"

if [[ "$START_CONTAINER" == true ]]; then
    echo "🚀 启动容器..."
    ENV_FILE="$PROJECT_DIR/.env.prod"
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "❌ 未找到 $ENV_FILE(参考 .env.example 创建)"
        exit 1
    fi
    set -a; source "$ENV_FILE"; set +a
    : "${DB_HOST:?DB_HOST 未设置}"
    : "${DB_PORT:?DB_PORT 未设置}"
    : "${DB_NAME:?DB_NAME 未设置}"
    : "${DB_USER:?DB_USER 未设置}"
    : "${DB_PASSWORD:?DB_PASSWORD 未设置}"
    HTTP_PORT="${HTTP_PORT:-3000}"

    # 停旧容器
    ssh -p "$NAS_PORT" "$NAS_USER@$NAS_HOST" \
        "sudo $NAS_DOCKER stop $CONTAINER 2>/dev/null; \
         sudo $NAS_DOCKER rm $CONTAINER 2>/dev/null; echo '旧容器已清理'"

    # 生成 env-file 并经 SSH 管道上传(密码含特殊字符不经 shell 解析)
    echo "生成 env-file 并上传 NAS..."
    printf '%s\n' \
        "DB_HOST=${DB_HOST}" \
        "DB_PORT=${DB_PORT}" \
        "DB_NAME=${DB_NAME}" \
        "DB_USER=${DB_USER}" \
        "DB_PASSWORD=${DB_PASSWORD}" \
        "HTTP_PORT=${HTTP_PORT}" \
        "REPORTS_DIR=/reports" \
        "BACKFILL_YEARS=${BACKFILL_YEARS:-0}" \
        "AKSHARE_QPS=${AKSHARE_QPS:-2}" \
        "MINUTE_HASH_TABLES=${MINUTE_HASH_TABLES:-32}" \
        | ssh -p "$NAS_PORT" "$NAS_USER@$NAS_HOST" \
            "cat > /tmp/market-lab.env && chmod 600 /tmp/market-lab.env"

    # 启动:页面端口映射宿主(网关再对外映射 30000);reports 挂载供 NAS 文件服务读
    # bridge 网络下访问宿主 MySQL(192.168.1.99:3306)可达;API 端口不做对外映射
    echo "启动 $CONTAINER..."
    ssh -p "$NAS_PORT" "$NAS_USER@$NAS_HOST" \
        "sudo $NAS_DOCKER run -d \
            --name=$CONTAINER \
            --env-file=/tmp/market-lab.env \
            -p ${HTTP_PORT}:3000 \
            -v $NAS_BASE_DIR/reports:/reports \
            --restart unless-stopped \
            $IMAGE"

    echo "════════════════════════════════════════════════"
    echo "🎉 部署完成"
    echo "   页面: http://$NAS_HOST:${HTTP_PORT}/dashboard"
    echo "   报告: $NAS_BASE_DIR/reports/latest.html"
    echo "════════════════════════════════════════════════"
    ssh -p "$NAS_PORT" "$NAS_USER@$NAS_HOST" \
        "sudo $NAS_DOCKER ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep market-lab || true"
fi

echo "✅ 完成"
