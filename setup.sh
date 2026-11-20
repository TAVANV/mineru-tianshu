#!/usr/bin/env bash
# ============================================================================
# Tianshu (天枢) - 一键部署脚本（统一入口）
#
# 用法:
#   bash setup.sh                            # 交互式菜单选择部署模式
#   bash setup.sh --mode gpu                 # GPU 标准部署（docker-compose.yml）
#   bash setup.sh --mode pipeline            # 纯 pipeline 部署（并发/显存自动调谐）
#   bash setup.sh --mode cpu                 # Mac CPU 本地开发（.env.cpu + compose.cpu）
#   bash setup.sh --mode native              # 本机原生部署（不用 Docker，Apple Silicon 自动 MPS 加速）
#   bash setup.sh --mode offline-build       # 联网机：构建离线镜像包（docker-images/）
#   bash setup.sh --mode offline-deploy      # 生产机：部署离线镜像包
#
# 非交互参数:
#   --gpus N          GPU 数量（默认 nvidia-smi -L 自动检测）
#   --concurrency N   每个 GPU 的 Worker 并发数 MAX_CONCURRENT_TASKS（默认 1）
#   --reuse-from DIR  offline-build 复用旧模型目录，可重复指定
#   --network cn|global  网络环境（cn=国内镜像加速 / global=海外官方源直连，默认交互询问，非交互默认 cn）
#   --yes             跳过全部交互提问，未指定的配置保持 .env 默认值
#   --dry-run         只生成配置文件并打印将执行的操作，不构建、不启动
#   --skip-smoke      跳过部署末尾的真实解析自检（默认会提交一个样例 PDF 验证全链路）
#   --smoke-only      不部署，只对已启动的服务跑一次解析自检（配合 --mode 选 .env 文件）
#   --port N          解析自检访问的 API 端口（默认读 .env 的 API_PORT）
#   -h, --help        显示帮助
#
# 示例:
#   bash setup.sh --mode pipeline --gpus 2 --concurrency 2 --yes
#   bash setup.sh --mode gpu --dry-run       # 预览 .env 与执行计划
#   bash setup.sh --mode cpu --yes           # Mac 上一键 CPU 开发环境
#   bash setup.sh --mode native              # Mac 原生运行（MPS 加速，性能优于 Docker CPU）
#   bash setup.sh --smoke-only               # 对已启动的服务单独跑一次解析自检
# ============================================================================

set -euo pipefail

# 镜像全部经 --load 进本地，不推仓库，buildx 默认附带的 provenance attestation 没有用处；
# 它会把产物变成 OCI index，走上更复杂的导出路径。关掉它，导出直接落普通 manifest。
export BUILDX_NO_DEFAULT_ATTESTATIONS=1

# ----------------------------------------------------------------------------
# 输出辅助
# ----------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[✓]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[⚠]${NC} $1"; }
log_error() { echo -e "${RED}[✗]${NC} $1"; }

usage() {
    # 打印文件头注释块（到第一个空行为止），新增选项时无需再同步行号
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

# ----------------------------------------------------------------------------
# 参数解析
# ----------------------------------------------------------------------------
MODE=""
OPT_GPUS=""
OPT_CONCURRENCY=""
OPT_REUSE_FROM=()
OPT_NETWORK=""
ASSUME_YES=0
DRY_RUN=0
SKIP_SMOKE=0
SMOKE_ONLY=0
OPT_PORT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)
            MODE="${2:-}"
            shift 2
            ;;
        --network)
            OPT_NETWORK="${2:-}"
            shift 2
            ;;
        --gpus | -g)
            OPT_GPUS="${2:-}"
            shift 2
            ;;
        --concurrency | -c)
            OPT_CONCURRENCY="${2:-}"
            shift 2
            ;;
        --reuse-from)
            if [ -z "${2:-}" ]; then
                log_error "--reuse-from 需要旧模型目录"
                exit 1
            fi
            OPT_REUSE_FROM+=("$2")
            shift 2
            ;;
        --yes | -y)
            ASSUME_YES=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --skip-smoke)
            SKIP_SMOKE=1
            shift
            ;;
        --smoke-only)
            SMOKE_ONLY=1
            shift
            ;;
        --port)
            OPT_PORT="${2:-}"
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            log_error "未知参数: $1"
            echo "运行 'bash setup.sh --help' 查看用法。"
            exit 1
            ;;
    esac
done

# 进入脚本所在目录（正常模式为仓库根目录；offline-deploy 为离线包目录）
cd "$(dirname "$0")" || exit 1

# 是否进行交互提问：终端 + 未指定 --yes + 非 dry-run
INTERACTIVE=0
if [ -t 0 ] && [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    INTERACTIVE=1
fi

# 环境配置文件（cpu 模式切换为 .env.cpu）
ENV_FILE=".env"

# 调谐常量
MIN_VRAM_PER_WORKER=6
RAM_PER_WORKER_GB=8
MAX_RAM_PERCENT=25
MIN_MEM_LIMIT_GB=16

# ----------------------------------------------------------------------------
# 可移植工具
# ----------------------------------------------------------------------------

# macOS 的 BSD sed 需要 -i ''，GNU sed 需要 -i
sed_i() {
    if [ "$(uname)" = "Darwin" ]; then
        sed -i '' "$@"
    else
        sed -i "$@"
    fi
}

# 交互提问：ask "问题" "默认值"，结果写入 REPLY；非交互时直接取默认值
REPLY=""
ask() {
    local prompt="$1"
    local default="$2"
    local input=""
    if [ "$INTERACTIVE" -eq 0 ]; then
        REPLY="$default"
        return 0
    fi
    printf "${BLUE}[?]${NC} %s [%s]: " "$prompt" "$default"
    read -r input
    REPLY="${input:-$default}"
}

# 是非提问：ask_yn "问题" "Y|N"，默认值为 Y 时非交互返回 0（是）
ask_yn() {
    local prompt="$1"
    local default="$2"
    local input=""
    if [ "$INTERACTIVE" -eq 0 ]; then
        [ "$default" = "Y" ]
        return
    fi
    printf "${BLUE}[?]${NC} %s [%s]: " "$prompt" "$default"
    read -r input
    input="${input:-$default}"
    case "$input" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

# ----------------------------------------------------------------------------
# 网络环境选择（决定 apt / pip / npm 镜像源）
# ----------------------------------------------------------------------------
# cn:     国内镜像加速（阿里云 apt + 清华 PyPI + npmmirror）
# global: 海外/代理直连（官方源）
NETWORK_ENV=""
APT_MIRROR=""
PIP_MIRROR=""
NPM_REGISTRY=""

choose_network() {
    NETWORK_ENV="$OPT_NETWORK"
    if [ -z "$NETWORK_ENV" ]; then
        if [ "$INTERACTIVE" -eq 1 ]; then
            echo ""
            echo "  网络环境: 1) 国内（镜像加速，默认）  2) 海外/代理（官方源直连）"
            ask "请选择网络环境 [1/2]" "1"
            case "$REPLY" in
                2 | global | overseas) NETWORK_ENV="global" ;;
                *) NETWORK_ENV="cn" ;;
            esac
        else
            NETWORK_ENV="cn"
        fi
    fi

    case "$NETWORK_ENV" in
        cn)
            APT_MIRROR="mirrors.aliyun.com"
            PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
            NPM_REGISTRY="https://registry.npmmirror.com"
            ;;
        global)
            APT_MIRROR=""
            PIP_MIRROR="https://pypi.org/simple"
            NPM_REGISTRY="https://registry.npmjs.org"
            ;;
        *)
            log_error "未知网络环境: ${NETWORK_ENV}（可选: cn / global）"
            exit 1
            ;;
    esac
    export APT_MIRROR PIP_MIRROR NPM_REGISTRY
    log_info "网络环境: ${NETWORK_ENV}（apt=${APT_MIRROR:-官方源} / pip=${PIP_MIRROR} / npm=${NPM_REGISTRY}）"
}

# ----------------------------------------------------------------------------
# Docker Compose 命令
# ----------------------------------------------------------------------------
DC=()

detect_compose() {
    if docker compose version > /dev/null 2>&1; then
        DC=(docker compose)
    elif command -v docker-compose > /dev/null 2>&1; then
        DC=(docker-compose)
    elif [ "$DRY_RUN" -eq 1 ]; then
        DC=(docker compose) # dry-run 仅用于打印
    else
        log_error "未找到 Docker Compose，安装指南: https://docs.docker.com/compose/install/"
        exit 1
    fi
}

# 把本次部署使用的 compose 文件组合写进 .env 的 COMPOSE_FILE。
# 之后用户直接敲 docker compose ps / logs / down，或用 make 的各个目标时，
# 无需再手动拼 -f，避免误操作到错误的 compose 文件（离线部署尤其容易踩）。
persist_compose_file() {
    # COMPOSE_FILE 只对 docker compose 默认读取的 ./.env 生效
    if [ "$ENV_FILE" != ".env" ]; then
        return 0
    fi

    local files=""
    local i
    for ((i = 0; i < ${#DC[@]}; i++)); do
        if [ "${DC[$i]}" = "-f" ]; then
            if [ -z "$files" ]; then
                files="${DC[$((i + 1))]}"
            else
                files="${files}:${DC[$((i + 1))]}"
            fi
        fi
    done

    if [ -n "$files" ]; then
        set_env_key COMPOSE_FILE "$files"
        log_info "COMPOSE_FILE = ${files}（后续 docker compose / make 命令无需再带 -f）"
    fi

    # COMPOSE_PROFILES 同理：Redis 定义在 profile 里，裸 docker compose 命令看不见它。
    # 最典型的后果是 `docker compose down` 不会停 redis，网络因仍有活动端点而删不掉：
    #   Error: network ... has active endpoints (name:"tianshu-redis")
    local profiles=""
    for ((i = 0; i < ${#DC[@]}; i++)); do
        if [ "${DC[$i]}" = "--profile" ]; then
            if [ -z "$profiles" ]; then
                profiles="${DC[$((i + 1))]}"
            else
                profiles="${profiles},${DC[$((i + 1))]}"
            fi
        fi
    done

    if [ -n "$profiles" ]; then
        set_env_key COMPOSE_PROFILES "$profiles"
        log_info "COMPOSE_PROFILES = ${profiles}（后续命令无需再带 --profile）"
    fi
}

# 启用 Redis 时 compose 命令附加 --profile redis（保证 stop/logs 等也带上）
add_redis_profile() {
    if [ -f "$ENV_FILE" ] && grep -qE '^REDIS_QUEUE_ENABLED=true' "$ENV_FILE"; then
        DC+=(--profile redis)
        log_info "Redis 队列已启用，compose 附加 --profile redis"
    fi
}

# ----------------------------------------------------------------------------
# .env 读写
# ----------------------------------------------------------------------------
get_env_key() {
    local key="$1"
    if [ ! -f "$ENV_FILE" ]; then
        return 0
    fi
    grep -E "^${key}=" "$ENV_FILE" | head -n1 | cut -d= -f2- || true
}

set_env_key() {
    local key="$1"
    local val="$2"

    # 值不得含换行：写进去会把一行配置拆成两行，.env 结构被破坏后
    # 表现为"值被莫名截断"，极难排查（探测类函数返回多行文本时就会这样）
    local val_oneline
    val_oneline=$(printf '%s' "$val" | tr -d '\n\r')
    if [ "${#val_oneline}" -ne "${#val}" ]; then
        log_error "拒绝写入 ${key}：值包含换行符，可能来自某个探测命令的异常输出"
        return 1
    fi

    if grep -qE "^${key}=" "$ENV_FILE"; then
        # 不能用 sed 的替换串写值：& 会被展开成整个匹配，	 之类会被解释成控制字符，
        # 密码/密钥里带这些字符时会被静默改写。awk 从 ENVIRON 取值则是纯字面量。
        local tmp="${ENV_FILE}.tmp.$$"
        if TIANSHU_K="$key" TIANSHU_V="$val" awk '
            BEGIN { k = ENVIRON["TIANSHU_K"]; v = ENVIRON["TIANSHU_V"]; done = 0 }
            !done && index($0, k "=") == 1 { print k "=" v; done = 1; next }
            { print }
        ' "$ENV_FILE" > "$tmp"; then
            mv "$tmp" "$ENV_FILE"
        else
            rm -f "$tmp"
            log_warning "写入 ${key} 失败，$ENV_FILE 保持原样"
            return 1
        fi
    else
        printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
    fi
}

# ----------------------------------------------------------------------------
# 环境探测
# ----------------------------------------------------------------------------
detect_gpu_count() {
    local n
    n=$(nvidia-smi -L 2> /dev/null | grep -c '^GPU ' || true)
    echo "${n:-0}"
}

# 全机最小单卡显存（GB），混插显卡时按最小卡规划预算
detect_min_vram_gb() {
    local mib
    mib=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2> /dev/null \
        | tr -d ' ' | sort -n | head -n1 || true)
    if [ -z "$mib" ]; then
        echo 0
    else
        echo $((mib / 1024))
    fi
}

detect_total_ram_gb() {
    local kb=""
    local bytes=""
    if [ -r /proc/meminfo ]; then
        kb=$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo || true)
    fi
    if [ -n "$kb" ]; then
        echo $((kb / 1024 / 1024))
        return 0
    fi
    # macOS 回退
    if command -v sysctl > /dev/null 2>&1; then
        bytes=$(sysctl -n hw.memsize 2> /dev/null || true)
        if [ -n "$bytes" ]; then
            echo $((bytes / 1024 / 1024 / 1024))
            return 0
        fi
    fi
    echo 0
}

detect_server_ip() {
    local ip=""
    if command -v ip > /dev/null 2>&1; then
        ip=$(ip route get 1.1.1.1 2> /dev/null \
            | awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}' | head -n1 || true)
    fi
    if [ -z "$ip" ] && command -v hostname > /dev/null 2>&1; then
        ip=$(hostname -I 2> /dev/null | awk '{print $1}' || true)
    fi
    # macOS 回退（注意 Windows 上也有同名的 ipconfig，但用法完全不同，
    # 会输出一整段用法说明 —— 所以下面必须做格式校验）
    if [ -z "$ip" ] && command -v ipconfig > /dev/null 2>&1; then
        ip=$(ipconfig getifaddr en0 2> /dev/null || true)
    fi

    # 只接受单行合法 IPv4：探测命令在不同平台上可能返回多行文本或错误提示，
    # 原样写进 .env 会破坏文件结构（值被截断成 http:// 之类），且很难排查
    ip=$(printf '%s' "$ip" | tr -d '
' | head -n1 | tr -d '[:space:]')
    case "$ip" in
        [0-9]*.[0-9]*.[0-9]*.[0-9]*) echo "$ip" ;;
        *) echo "" ;;
    esac
}

# 拼接经前端 nginx 反代的 RustFS 公网地址。
# 端口为 80 时必须省略：写成 http://host:80/s3 虽然能用，但不规范，
# 且前端日后切到 HTTPS 时这个写死的 :80 会直接失效。
build_rustfs_public_url() {
    local host="$1"
    local port="$2"
    if [ "$port" = "80" ] || [ -z "$port" ]; then
        echo "http://${host}/s3"
    else
        echo "http://${host}:${port}/s3"
    fi
}

container_running() {
    docker ps --filter "name=$1" --filter "status=running" --format '{{.Names}}' 2> /dev/null \
        | grep -q "$1"
}

# ----------------------------------------------------------------------------
# 依赖检查（GPU 模式）
# ----------------------------------------------------------------------------
check_dependencies() {
    local strict_gpu="$1" # 1 = 无 GPU 直接报错（pipeline 模式）
    log_info "检查系统依赖..."

    if ! command -v docker > /dev/null 2>&1; then
        log_error "未安装 Docker，安装指南: https://docs.docker.com/get-docker/"
        exit 1
    fi
    log_success "Docker: $(docker --version)"
    detect_compose
    log_success "Compose: $("${DC[@]}" version 2> /dev/null | head -n1)"

    if ! command -v nvidia-smi > /dev/null 2>&1; then
        if [ "$strict_gpu" -eq 1 ]; then
            log_error "未找到 nvidia-smi，MinerU pipeline 引擎必须使用 GPU。"
            log_error "纯 CPU 环境请改用: bash setup.sh --mode cpu"
            exit 1
        fi
        log_warning "未检测到 NVIDIA GPU，将以 CPU 方式运行（性能大幅下降）"
        return 0
    fi
    log_success "NVIDIA 驱动正常:"
    nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader || true

    if docker run --rm --gpus all nvidia/cuda:12.6.2-base-ubuntu22.04 nvidia-smi > /dev/null 2>&1; then
        log_success "NVIDIA Container Toolkit 工作正常"
    elif [ "$strict_gpu" -eq 1 ]; then
        log_error "Docker 无法访问 GPU（NVIDIA Container Toolkit 缺失或配置错误）。"
        log_error "安装指南: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
        exit 1
    else
        log_warning "NVIDIA Container Toolkit 未正确配置，GPU 容器可能无法启动"
        log_info "安装指南: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
    fi
}

# ----------------------------------------------------------------------------
# 环境配置
# ----------------------------------------------------------------------------
prepare_env() {
    # 示例文件名与 ENV_FILE 派生一致：.env -> .env.example，.env.cpu -> .env.cpu.example
    local example_file="${ENV_FILE}.example"
    if [ ! -f "$ENV_FILE" ]; then
        if [ ! -f "$example_file" ]; then
            log_error "$example_file 不存在，无法创建 $ENV_FILE"
            exit 1
        fi
        cp "$example_file" "$ENV_FILE"
        log_success "已从 $example_file 创建 $ENV_FILE"
    else
        log_info "$ENV_FILE 已存在，仅更新部署相关键"
    fi

    ensure_runtime_user
    ensure_free_subnet
    migrate_service_hosts
}

# 容器间寻址从服务名迁移到容器名（tianshu-*）。
# compose 登记的服务名别名（rustfs / redis ...）线上出现过丢失：容器照常运行，
# 但 Docker 内置 DNS 查不到服务名，上传图片、连 Redis 全部失败；容器名则始终登记。
# 只改写仍等于旧默认值的键，用户自定义的地址（外部 S3 / Redis 等）保持不动。
migrate_service_hosts() {
    [ -f "$ENV_FILE" ] || return 0

    local suffix=""
    [ "$MODE" = "cpu" ] && suffix="-cpu"

    local key old new current
    while IFS='|' read -r key old new; do
        [ -n "$key" ] || continue
        current=$(get_env_key "$key")
        if [ "$current" = "$old" ]; then
            set_env_key "$key" "$new" && log_info "${key}: ${old} → ${new}（按容器名寻址）"
        fi
    done << EOF
RUSTFS_ENDPOINT|rustfs:9000|tianshu-rustfs${suffix}:9000
API_BASE_URL|http://backend:8000|http://tianshu-backend${suffix}:8000
WORKER_URL|http://worker:8001|http://tianshu-worker${suffix}:8001
EOF

    # CPU 栈没有 redis 服务，不迁移
    if [ "$MODE" != "cpu" ] && [ "$(get_env_key REDIS_HOST)" = "redis" ]; then
        set_env_key REDIS_HOST "tianshu-redis" && log_info "REDIS_HOST: redis → tianshu-redis（按容器名寻址）"
    fi
}

# Docker 网段避让：compose 里的固定 /16 一旦被机器上其它项目占用，
# 创建网络会直接失败（Pool overlaps with other one on this address space）。
# 这里扫描已占用网段，冲突时自动挑一个空闲的写入 .env。
ensure_free_subnet() {
    if ! command -v docker > /dev/null 2>&1; then
        return 0
    fi

    local configured
    configured=$(get_env_key TIANSHU_SUBNET)

    # 已显式配置且不冲突就直接沿用
    local used
    used=$(docker network ls --format '{{.Name}}' 2> /dev/null | while read -r net; do
        docker network inspect "$net" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2> /dev/null
    done | tr ' ' '
' | grep -E '^[0-9]' || true)

    # 各模式的默认网段（与对应 compose 文件保持一致）
    local candidate="${configured}"
    if [ -z "$candidate" ]; then
        case "$MODE" in
            cpu) candidate="172.29.0.0/16" ;;
            offline-deploy) candidate="172.30.0.0/16" ;;
            *) candidate="172.28.0.0/16" ;;
        esac
    fi

    if ! printf '%s
' "$used" | grep -qxF "$candidate"; then
        log_info "Docker 网段 ${candidate} 可用"
        return 0
    fi

    log_warning "Docker 网段 ${candidate} 已被占用，自动挑选空闲网段..."
    local third
    for third in 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45; do
        local try="172.${third}.0.0/16"
        if ! printf '%s
' "$used" | grep -qxF "$try"; then
            set_env_key TIANSHU_SUBNET "$try"
            set_env_key TIANSHU_GATEWAY "172.${third}.0.1"
            log_success "已切换到空闲网段 ${try}"
            return 0
        fi
    done

    log_warning "172.28~172.45 均被占用，请手动在 $ENV_FILE 设置 TIANSHU_SUBNET / TIANSHU_GATEWAY"
    return 0
}

# 容器运行身份：默认 root（0:0），规避各类挂载目录属主不匹配问题。
# 需要非 root 运行（企业安全审计等）时，把 .env 里的 TIANSHU_UID/TIANSHU_GID
# 改成宿主机部署目录的属主即可，无需改动任何 compose 文件。
ensure_runtime_user() {
    local uid
    uid=$(get_env_key TIANSHU_UID)
    if [ -z "$uid" ]; then
        set_env_key TIANSHU_UID 0
        set_env_key TIANSHU_GID 0
        log_info "容器运行身份 TIANSHU_UID:TIANSHU_GID = 0:0（root）"
    else
        log_info "容器运行身份 TIANSHU_UID:TIANSHU_GID = ${uid}:$(get_env_key TIANSHU_GID)（沿用 $ENV_FILE 设置）"
    fi
}

# 生成随机密钥（优先 openssl，缺失时回退 /dev/urandom）
gen_random_hex() {
    local bytes="${1:-32}"
    if command -v openssl > /dev/null 2>&1; then
        openssl rand -hex "$bytes"
    else
        cat /dev/urandom | LC_ALL=C tr -dc 'a-f0-9' | head -c $((bytes * 2))
    fi
}

# JWT_SECRET_KEY 为空或为已知占位符时自动生成
ensure_jwt_secret() {
    local jwt
    jwt=$(get_env_key JWT_SECRET_KEY)
    case "$jwt" in
        "" | your-secret-key-change-in-production | dev-secret-key-change-in-production | cpu-dev-secret-key-change-in-production | temp-secret-key | CHANGE_THIS_TO_A_SECURE_RANDOM_STRING_IN_PRODUCTION)
            local secret
            secret=$(gen_random_hex 32)
            if [ -n "$secret" ]; then
                set_env_key JWT_SECRET_KEY "$secret"
                log_success "JWT_SECRET_KEY 已自动生成（openssl rand -hex 32）"
            else
                log_warning "未找到 openssl，请手动修改 $ENV_FILE 中的 JWT_SECRET_KEY"
            fi
            ;;
        *)
            log_info "JWT_SECRET_KEY 已自定义，保持不变"
            ;;
    esac
}

# RustFS 访问密钥为空或为已知占位符/默认凭据时自动生成
ensure_rustfs_keys() {
    local rustfs_enabled
    rustfs_enabled=$(get_env_key RUSTFS_ENABLED)
    if [ "$rustfs_enabled" = "false" ]; then
        return 0
    fi

    local access_key
    access_key=$(get_env_key RUSTFS_ACCESS_KEY)
    case "$access_key" in
        "" | rustfsadmin | CHANGE_THIS_TO_A_RANDOM_VALUE)
            set_env_key RUSTFS_ACCESS_KEY "$(gen_random_hex 16)"
            log_success "RUSTFS_ACCESS_KEY 已自动生成（openssl rand -hex 16）"
            ;;
        *)
            log_info "RUSTFS_ACCESS_KEY 已自定义，保持不变"
            ;;
    esac

    local secret_key
    secret_key=$(get_env_key RUSTFS_SECRET_KEY)
    case "$secret_key" in
        "" | rustfsadmin | CHANGE_THIS_TO_A_RANDOM_VALUE)
            set_env_key RUSTFS_SECRET_KEY "$(gen_random_hex 32)"
            log_success "RUSTFS_SECRET_KEY 已自动生成（openssl rand -hex 32）"
            ;;
        *)
            log_info "RUSTFS_SECRET_KEY 已自定义，保持不变"
            ;;
    esac
}

# 启用 Redis 队列时，REDIS_PASSWORD 为空或占位符则自动生成
ensure_redis_password() {
    local redis_enabled
    redis_enabled=$(get_env_key REDIS_QUEUE_ENABLED)
    if [ "$redis_enabled" != "true" ]; then
        return 0
    fi

    local redis_password
    redis_password=$(get_env_key REDIS_PASSWORD)
    case "$redis_password" in
        "" | CHANGE_THIS_TO_A_RANDOM_VALUE)
            set_env_key REDIS_PASSWORD "$(gen_random_hex 24)"
            log_success "REDIS_PASSWORD 已自动生成（openssl rand -hex 24）"
            ;;
        *)
            log_info "REDIS_PASSWORD 已自定义，保持不变"
            ;;
    esac
}

# 初始管理员密码为空或占位符时自动生成（首次部署后端强制要求）
# 不明文回显：部署完成后提示用户到 $ENV_FILE 查看
ensure_admin_password() {
    local admin_password
    admin_password=$(get_env_key TIANSHU_ADMIN_PASSWORD)
    case "$admin_password" in
        "" | CHANGE_THIS_TO_A_RANDOM_VALUE)
            set_env_key TIANSHU_ADMIN_PASSWORD "$(gen_random_hex 16)"
            log_success "TIANSHU_ADMIN_PASSWORD 已自动生成（写入 ${ENV_FILE}，请部署后查看并妥善保存）"
            ;;
        *)
            log_info "TIANSHU_ADMIN_PASSWORD 已自定义，保持不变"
            ;;
    esac
}

# GPU/pipeline 模式的交互式提问（--yes / dry-run / 非终端时保持 .env 默认值）
interactive_configure() {
    [ "$INTERACTIVE" -eq 1 ] || return 0

    echo ""
    log_info "部署配置（回车使用默认值）"

    local detected
    detected=$(detect_gpu_count)
    local gpu_default=1
    [ "$detected" -gt 0 ] && gpu_default="$detected"
    ask "GPU 数量" "$gpu_default"
    OPT_GPUS="$REPLY"

    ask "每个 GPU 的 Worker 并发数（MAX_CONCURRENT_TASKS）" "1"
    OPT_CONCURRENCY="$REPLY"

    echo "  模型下载源: 1) auto（自动）  2) huggingface  3) modelscope（国内推荐）"
    ask "请选择模型源" "1"
    case "$REPLY" in
        2 | huggingface) set_env_key MODEL_DOWNLOAD_SOURCE huggingface ;;
        3 | modelscope) set_env_key MODEL_DOWNLOAD_SOURCE modelscope ;;
        *) set_env_key MODEL_DOWNLOAD_SOURCE auto ;;
    esac

    if ask_yn "是否启用 Redis 队列（高并发场景推荐）" "N"; then
        set_env_key REDIS_QUEUE_ENABLED true
    else
        set_env_key REDIS_QUEUE_ENABLED false
    fi

    # 对象存储是必需依赖，不提供关闭选项：解析结果里的图片必须有浏览器可达的地址
    set_env_key RUSTFS_ENABLED true
    local ip
    local front_port
    ip=$(detect_server_ip)
    front_port=$(get_env_key FRONTEND_PORT)
    front_port="${front_port:-80}"
    # RustFS 端口仅绑定回环，图片统一经前端 nginx /s3/ 反代访问
    ask "RustFS 公网访问地址（经前端 nginx /s3/ 反代，需浏览器可达）" "$(build_rustfs_public_url "${ip:-127.0.0.1}" "$front_port")"
    set_env_key RUSTFS_PUBLIC_URL "$REPLY"

    local api_default
    local front_default
    api_default=$(get_env_key API_PORT)
    api_default="${api_default:-8000}"
    front_default=$(get_env_key FRONTEND_PORT)
    front_default="${front_default:-80}"
    ask "API 端口" "$api_default"
    set_env_key API_PORT "$REPLY"
    set_env_key VITE_API_BASE_URL "http://localhost:${REPLY}"
    ask "前端端口" "$front_default"
    set_env_key FRONTEND_PORT "$REPLY"
    echo ""
}

# 自动调谐：并发/显存/内存按 GPU 资源自动计算
tune_env() {
    local preserve_storage_config="${1:-0}"
    local detected_gpus
    local min_vram
    detected_gpus=$(detect_gpu_count)
    min_vram=$(detect_min_vram_gb)

    if [ "$preserve_storage_config" -eq 0 ]; then
        ensure_jwt_secret
        ensure_rustfs_keys
    fi
    ensure_redis_password
    ensure_admin_password

    # --- GPU 数量 -----------------------------------------------------------
    local gpu_count
    if [ -n "$OPT_GPUS" ]; then
        gpu_count="$OPT_GPUS"
        set_env_key GPU_COUNT "$gpu_count"
        log_success "GPU_COUNT = ${gpu_count}（--gpus 指定）"
    elif [ "$detected_gpus" -gt 0 ]; then
        gpu_count="$detected_gpus"
        set_env_key GPU_COUNT "$gpu_count"
        log_success "GPU_COUNT = ${gpu_count}（自动检测）"
    else
        gpu_count=$(get_env_key GPU_COUNT)
        gpu_count="${gpu_count:-1}"
        log_warning "未检测到 GPU，GPU_COUNT 保持 ${gpu_count}"
    fi

    # --- Worker 并发 --------------------------------------------------------
    # litserve_worker.py 只读 MAX_CONCURRENT_TASKS，这是控制并发的唯一入口
    local concurrency
    if [ -n "$OPT_CONCURRENCY" ]; then
        concurrency="$OPT_CONCURRENCY"
    else
        concurrency=$(get_env_key MAX_CONCURRENT_TASKS)
        concurrency="${concurrency:-1}"
    fi
    if ! [ "$concurrency" -ge 1 ] 2> /dev/null; then
        log_error "非法并发数: ${concurrency}"
        exit 1
    fi
    set_env_key MAX_CONCURRENT_TASKS "$concurrency"
    log_success "MAX_CONCURRENT_TASKS = ${concurrency}（每卡 Worker 进程数）"

    # --- 每 Worker 显存预算 --------------------------------------------------
    # MinerU 按 MINERU_VIRTUAL_VRAM_SIZE 规划 batch，同卡多进程必须平分显存
    local vram
    if [ "$min_vram" -gt 0 ]; then
        vram=$((min_vram / concurrency))
        if [ "$vram" -lt "$MIN_VRAM_PER_WORKER" ]; then
            vram="$MIN_VRAM_PER_WORKER"
        fi
    else
        vram=$(get_env_key MINERU_VIRTUAL_VRAM_SIZE)
        vram="${vram:-8}"
    fi
    set_env_key MINERU_VIRTUAL_VRAM_SIZE "$vram"
    log_success "MINERU_VIRTUAL_VRAM_SIZE = ${vram} GB / worker（单卡 ${min_vram} GB）"

    if [ "$min_vram" -gt 0 ] && [ $((vram * concurrency)) -gt "$min_vram" ]; then
        log_warning "显存超配（${vram} x ${concurrency} > ${min_vram} GB），如遇 CUDA OOM 请降低并发"
    fi

    if [ "$preserve_storage_config" -eq 0 ]; then
        # --- RustFS 公网地址 -----------------------------------------------------
        # 解析结果中的图片会改写为该地址，必须浏览器可达；
        # RustFS 端口仅绑定回环，默认经前端 nginx /s3/ 路径反代
        # 对象存储是必需依赖：Worker 侧对含图片的任务已是 fail-closed，
        # 这里把开关强制打开，并保证公网地址一定有值，否则拒绝部署
        if [ "$(get_env_key RUSTFS_ENABLED)" != "true" ]; then
            set_env_key RUSTFS_ENABLED true
            log_info "RUSTFS_ENABLED = true（对象存储为必需依赖，已强制启用）"
        fi

        local rustfs_url
        local front_port
        local server_ip
        rustfs_url=$(get_env_key RUSTFS_PUBLIC_URL)
        front_port=$(get_env_key FRONTEND_PORT)
        front_port="${front_port:-80}"
        server_ip=$(detect_server_ip)
        case "$rustfs_url" in
            "" | *192.168.1.100* | http://localhost/s3 | http://127.0.0.1/s3)
                if [ -n "$server_ip" ]; then
                    local derived_url
                    derived_url=$(build_rustfs_public_url "$server_ip" "$front_port")
                    set_env_key RUSTFS_PUBLIC_URL "$derived_url"
                    log_success "RUSTFS_PUBLIC_URL = ${derived_url}（经前端 nginx 反代）"
                elif [ "$DRY_RUN" -eq 1 ]; then
                    log_warning "未能探测服务器 IP（dry-run 继续；正式部署时会直接报错退出）"
                    log_warning "正式部署前请手动设置 RUSTFS_PUBLIC_URL=$(build_rustfs_public_url "<服务器IP>" "$front_port")"
                else
                    log_error "未能探测服务器 IP，无法确定 RUSTFS_PUBLIC_URL"
                    log_error "请手动在 $ENV_FILE 中设置，例如 RUSTFS_PUBLIC_URL=$(build_rustfs_public_url "<服务器IP>" "$front_port")"
                    log_error "该地址必须是浏览器可达的；留空会让所有含图片的解析任务失败"
                    exit 1
                fi
                ;;
            *)
                log_info "RUSTFS_PUBLIC_URL 已配置（${rustfs_url}），保持不变"
                ;;
        esac
    fi

    # --- Worker 内存上限 -----------------------------------------------------
    # cgroup 硬上限而非预留：随 worker 进程数放大，防止失控进程拖垮宿主机
    local total_ram
    local workers
    local mem_limit_gb=""
    total_ram=$(detect_total_ram_gb)
    workers=$((gpu_count * concurrency))

    if [ "$total_ram" -gt 0 ]; then
        local want=$((workers * RAM_PER_WORKER_GB))
        local cap=$((total_ram * MAX_RAM_PERCENT / 100))
        if [ "$want" -le "$cap" ]; then
            mem_limit_gb="$want"
        else
            mem_limit_gb="$cap"
            log_warning "${workers} 个 Worker 期望 ${want}G，但主机内存的 ${MAX_RAM_PERCENT}% 仅为 ${cap}G，已按上限收敛"
        fi
        if [ "$mem_limit_gb" -lt "$MIN_MEM_LIMIT_GB" ]; then
            mem_limit_gb="$MIN_MEM_LIMIT_GB"
        fi
    fi

    if [ -n "$mem_limit_gb" ]; then
        local mem_res_gb=$((mem_limit_gb / 4))
        if [ "$mem_res_gb" -lt 8 ]; then
            mem_res_gb=8
        fi
        set_env_key WORKER_MEMORY_LIMIT "${mem_limit_gb}G"
        set_env_key WORKER_MEMORY_RESERVATION "${mem_res_gb}G"
        log_success "WORKER_MEMORY_LIMIT = ${mem_limit_gb}G（主机内存 ${total_ram}G，${workers} 个 Worker 进程）"
        log_info "该值为上限而非预留，实际占用可用 docker stats tianshu-worker 观察"
    else
        log_warning "无法读取主机内存，WORKER_MEMORY_LIMIT 保持 $(get_env_key WORKER_MEMORY_LIMIT)"
        log_warning "请按每 Worker 约 ${RAM_PER_WORKER_GB}G（当前 ${workers} 个）手动估算"
    fi
}

create_directories() {
    log_info "创建宿主机目录..."
    # models/{huggingface,modelscope}_cache 被 compose 显式挂载，必须由脚本建出来：
    # 交给 Docker 自动创建会得到 root 属主的目录，非 root 运行时缓存写不进去
    mkdir -p models models/huggingface_cache models/modelscope_cache \
        data/uploads data/output data/db \
        logs/backend logs/worker logs/mcp logs/scheduler

    migrate_legacy_data_dirs
    check_database_filesystem

    # 挂载目录需对容器内的运行身份可写（best-effort，失败不中断）
    local uid gid
    uid=$(get_env_key TIANSHU_UID)
    gid=$(get_env_key TIANSHU_GID)
    if [ -n "$uid" ] && [ "$uid" != "0" ]; then
        chown -R "${uid}:${gid:-$uid}" models data logs 2> /dev/null \
            || log_warning "chown 到 ${uid}:${gid:-$uid} 失败（网络盘或权限不足），已回退为 chmod 放权"
    fi
    chmod -R a+rwX models data logs 2> /dev/null || true
    log_success "目录就绪"
}

# SQLite 的 WAL 依赖 POSIX 文件锁，NFS/CIFS 上锁语义不可靠，会导致数据库**静默损坏**
# （task_db 与 auth_db 共用同一个文件，损坏意味着任务、用户、API Key 一起丢）。
# 容器内 TaskDB 启动时还会再校验一次，这里是给宿主机侧的早期提示。
check_database_filesystem() {
    local fs_type
    fs_type=$(stat -f -c %T data/db 2> /dev/null || echo "")
    [ -z "$fs_type" ] && return 0

    case "$fs_type" in
        nfs* | cifs* | smb* | "fuseblk")
            log_error "data/db 位于网络文件系统（${fs_type}）上，SQLite 会静默损坏数据"
            log_error "请把部署目录（至少 data/db）放到本地磁盘，或用 DATABASE_PATH 指向本地路径"
            log_error "明确知晓风险时可设置 TIANSHU_ALLOW_NETWORK_DB=true 跳过（不推荐）"
            exit 1
            ;;
        9p | v9fs | virtiofs | drvfs | vboxsf)
            log_warning "data/db 位于宿主机共享目录（${fs_type}）：本机开发可用，生产部署建议用本地磁盘"
            ;;
    esac
}

# 历史布局迁移：早期 docker-compose.yml 把上传/输出挂在 ./input 与 ./output，
# 而 offline/cpu 用的是 ./data/uploads 与 ./data/output —— 同一台机器换个模式，
# 数据落盘位置就变了。现已统一到 ./data/*，这里把旧目录的内容搬过去。
migrate_legacy_data_dirs() {
    local pair old_dir new_dir
    for pair in "input:data/uploads" "output:data/output"; do
        old_dir="${pair%%:*}"
        new_dir="${pair##*:}"

        [ -d "$old_dir" ] || continue

        # .gitkeep 是仓库里的占位文件，不算数据
        if [ -z "$(_dir_payload "$old_dir")" ]; then
            continue
        fi

        # 新目录已有数据时不自动合并，避免覆盖，交由用户决定
        if [ -n "$(_dir_payload "$new_dir")" ]; then
            log_warning "旧目录 ./${old_dir} 与新目录 ./${new_dir} 都有数据，未自动迁移"
            log_warning "确认后手动合并: mv ./${old_dir}/* ./${new_dir}/"
            continue
        fi

        # 用 find 搬运，确保点文件（如 .cache）也能迁移；保留旧目录本身与 .gitkeep
        if find "$old_dir" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -exec mv -t "$new_dir/" {} + 2> /dev/null; then
            log_success "已迁移历史数据目录: ./${old_dir} -> ./${new_dir}"
        else
            log_warning "迁移 ./${old_dir} -> ./${new_dir} 失败，请手动处理"
        fi
    done
}

# 返回目录中除占位文件以外的内容（为空表示该目录没有真实数据）
_dir_payload() {
    [ -d "$1" ] || return 0
    find "$1" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' 2> /dev/null | head -1
}

# ----------------------------------------------------------------------------
# 构建 / 启动 / 验证（GPU 类模式共用）
# ----------------------------------------------------------------------------
build_images() {
    log_info "构建镜像（首次需拉取大量依赖，约 10-30 分钟）..."
    DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1 "${DC[@]}" build --parallel
    log_success "镜像构建完成"
}

start_services() {
    log_info "启动服务..."
    "${DC[@]}" up -d
    log_success "容器已启动"
    log_info "init-models 首次运行会下载模型，worker 会等待其完成"
}

# 健康检查：轮询 API 180s + 前端 nginx 代理路径（端口读 $ENV_FILE）
verify_deployment() {
    local api_port
    local frontend_port
    local rc=0
    api_port=$(get_env_key API_PORT)
    api_port="${api_port:-8000}"
    frontend_port=$(get_env_key FRONTEND_PORT)
    frontend_port="${frontend_port:-80}"

    log_info "等待 API 就绪（最长 180s）..."
    local ok=0
    local waited=0
    while [ "$waited" -lt 180 ]; do
        if curl -fsS "http://localhost:${api_port}/api/v1/health" > /dev/null 2>&1; then
            ok=1
            break
        fi
        sleep 3
        waited=$((waited + 3))
    done

    if [ "$ok" -eq 1 ]; then
        log_success "API 健康（端口 ${api_port}）"
    else
        log_error "API 健康检查超时，排查: ${DC[*]} logs backend"
        return 1
    fi

    # 浏览器经前端 nginx 反代访问 API，与上面的直连路径不同，需单独验证
    if curl -fsS "http://localhost:${frontend_port}/api/v1/health" > /dev/null 2>&1; then
        log_success "前端 -> API 代理健康（端口 ${frontend_port}）"
    else
        log_error "nginx 无法访问 API：端口 ${frontend_port} 的 /api/v1/health 失败"
        log_error "检查 frontend/Dockerfile 中 'location /api/' 的 proxy_pass 配置"
        rc=1
    fi

    # 容器间 DNS 自检：地址解析不了时，含图片的任务会全部失败、Redis 队列会静默退回 SQLite，
    # 而 /health 对此毫无感知。经 compose exec 按服务定位容器，与容器名后缀无关
    local host_port host
    local -a dns_targets=()
    host_port=$(get_env_key RUSTFS_ENDPOINT)
    dns_targets+=("${host_port:-tianshu-rustfs:9000}")
    if [ "$(get_env_key REDIS_QUEUE_ENABLED)" = "true" ]; then
        host_port=$(get_env_key REDIS_HOST)
        dns_targets+=("${host_port:-tianshu-redis}")
    fi
    for host_port in "${dns_targets[@]}"; do
        host="${host_port%%:*}"
        if "${DC[@]}" exec -T backend getent hosts "$host" > /dev/null 2>&1; then
            log_success "容器内可解析 ${host}"
        else
            log_error "backend 容器内无法解析 ${host}：图片上传 / Redis 队列将不可用"
            log_error "排查: ${DC[*]} exec backend getent hosts ${host}"
            rc=1
        fi
    done

    if container_running tianshu-worker; then
        log_success "Worker 容器运行中"
    else
        log_warning "Worker 尚未就绪（在等待 init-models 下载模型）"
        log_warning "跟踪进度: ${DC[*]} logs -f init-models worker"
    fi

    return "$rc"
}

# ----------------------------------------------------------------------------
# 部署自检：提交一个样例 PDF，轮询到 completed 才算真正跑通
#
# /health 只能证明进程活着。真正会出问题的是模型路径、目录权限、GPU 可见性、
# 存储链路 —— 这些只有跑完一次真实解析才暴露得出来（issue #84 的用户正是
# 「部署成功、解析失败」）。自检失败只告警，不影响部署结果。
# ----------------------------------------------------------------------------
json_field() {
    # 从 JSON 中取出字符串字段值（不引入 jq 依赖）
    local key="$1"
    grep -o "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 | sed 's/.*"\([^"]*\)"$/\1/'
}

json_escape() {
    # 转义 JSON 字符串中的反斜杠与双引号
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

run_smoke_test() {
    if [ "$SKIP_SMOKE" -eq 1 ]; then
        log_info "已跳过解析自检（--skip-smoke）"
        return 0
    fi

    local sample="scripts/smoke-sample.pdf"
    if [ ! -f "$sample" ]; then
        log_warning "未找到样例文件 ${sample}，跳过解析自检"
        return 0
    fi

    # 端口优先取调用方传入值（--port），其次读 .env 的 API_PORT
    local api_port username password timeout
    api_port="${1:-}"
    if [ -z "$api_port" ]; then
        api_port=$(get_env_key API_PORT)
        api_port="${api_port:-8000}"
    fi
    username=$(get_env_key TIANSHU_ADMIN_USERNAME)
    username="${username:-admin}"
    password=$(get_env_key TIANSHU_ADMIN_PASSWORD)
    timeout="${TIANSHU_SMOKE_TIMEOUT:-600}"

    if [ -z "$password" ]; then
        log_warning "$ENV_FILE 中未找到 TIANSHU_ADMIN_PASSWORD，跳过解析自检"
        return 0
    fi

    log_info "解析自检：提交样例 PDF 验证全链路（最长 ${timeout}s）..."

    # --- 1. 登录取 token ---
    local login_body token
    login_body="{\"username\":\"$(json_escape "$username")\",\"password\":\"$(json_escape "$password")\"}"
    token=$(curl -fsS -X POST "http://localhost:${api_port}/api/v1/auth/login"         -H "Content-Type: application/json" -d "$login_body" 2> /dev/null | json_field access_token)

    if [ -z "$token" ]; then
        log_warning "解析自检：登录失败，跳过（可手动在 Web UI 上传文件验证）"
        return 1
    fi

    # --- 2. 提交任务 ---
    local submit_resp task_id
    submit_resp=$(curl -fsS -X POST "http://localhost:${api_port}/api/v1/tasks/submit"         -H "Authorization: Bearer ${token}" -F "file=@${sample}" 2> /dev/null)
    task_id=$(printf '%s' "$submit_resp" | json_field task_id)

    if [ -z "$task_id" ]; then
        log_warning "解析自检：任务提交失败，响应: ${submit_resp}"
        return 1
    fi
    log_info "解析自检：任务已提交（task_id=${task_id}），等待 Worker 处理..."

    # --- 3. 轮询结果（首次解析含模型加载，耗时较长属正常） ---
    local waited=0 interval=5 status="" resp=""
    while [ "$waited" -lt "$timeout" ]; do
        resp=$(curl -fsS "http://localhost:${api_port}/api/v1/tasks/${task_id}"             -H "Authorization: Bearer ${token}" 2> /dev/null)
        status=$(printf '%s' "$resp" | json_field status)

        case "$status" in
            completed)
                log_success "解析自检通过：样例 PDF 已解析完成（耗时约 ${waited}s）"
                log_success "模型加载、目录权限、GPU 与存储链路均正常"
                return 0
                ;;
            failed | cancelled)
                local err
                err=$(printf '%s' "$resp" | json_field error_message)
                log_error "解析自检失败：任务状态 ${status}"
                [ -n "$err" ] && log_error "错误信息: ${err}"
                log_error "排查: ${DC[*]} logs --tail 80 worker"
                return 1
                ;;
        esac

        sleep "$interval"
        waited=$((waited + interval))
        if [ $((waited % 60)) -eq 0 ]; then
            log_info "解析自检：已等待 ${waited}s，当前状态 ${status:-unknown}（首次解析需加载模型）"
        fi
    done

    log_warning "解析自检超时（${timeout}s），任务仍处于 ${status:-unknown} 状态"
    log_warning "模型首次加载较慢时属正常，可稍后在 Web UI 查看任务 ${task_id}"
    return 1
}

show_info() {
    local api_port
    local frontend_port
    local gpu_count
    local concurrency
    local server_ip
    api_port=$(get_env_key API_PORT)
    api_port="${api_port:-8000}"
    frontend_port=$(get_env_key FRONTEND_PORT)
    frontend_port="${frontend_port:-80}"
    gpu_count=$(get_env_key GPU_COUNT)
    gpu_count="${gpu_count:-1}"
    concurrency=$(get_env_key MAX_CONCURRENT_TASKS)
    concurrency="${concurrency:-1}"
    server_ip=$(detect_server_ip)
    server_ip="${server_ip:-localhost}"

    echo ""
    log_success "=========================================="
    log_success " Tianshu (天枢) 部署完成"
    log_success "=========================================="
    echo ""
    echo "  Web UI:   http://${server_ip}:${frontend_port}"
    echo "  API 文档: http://${server_ip}:${api_port}/docs"
    echo "  并发能力: ${gpu_count} GPU x ${concurrency} Worker = $((gpu_count * concurrency)) 并发任务"
    echo "  资源预算: 每 Worker $(get_env_key MINERU_VIRTUAL_VRAM_SIZE)G 显存，内存上限 $(get_env_key WORKER_MEMORY_LIMIT)（非预留）"
    echo ""
    echo "  查看日志: ${DC[*]} logs -f"
    echo "  查看状态: ${DC[*]} ps"
    echo "  停止服务: ${DC[*]} down"
    echo ""
    echo "  调整并发: bash setup.sh --mode ${MODE} --concurrency N --yes --dry-run"
    echo "            （只重算 .env，显存预算会同步调整），随后 docker compose up -d worker"
    echo "  解析自检: bash setup.sh --smoke-only"
    echo ""
    log_info "COMPOSE_FILE / COMPOSE_PROFILES 已写入 ${ENV_FILE}，可直接用裸 docker compose 命令"
    echo ""
    log_warning "管理员账号: $(get_env_key TIANSHU_ADMIN_USERNAME)（初始密码见 ${ENV_FILE} 中 TIANSHU_ADMIN_PASSWORD，请妥善保存）"
    echo ""
}

# ----------------------------------------------------------------------------
# dry-run 摘要
# ----------------------------------------------------------------------------
dry_run_summary() {
    local api_port
    local frontend_port
    api_port=$(get_env_key API_PORT 2> /dev/null || true)
    api_port="${api_port:-8000}"
    frontend_port=$(get_env_key FRONTEND_PORT 2> /dev/null || true)
    frontend_port="${frontend_port:-80}"

    echo ""
    log_success "================= DRY-RUN 摘要 ================="
    log_info "模式: ${MODE}"
    if [ -f "$ENV_FILE" ]; then
        log_info "配置文件 ${ENV_FILE} 已生成/更新，关键项:"
        echo "    GPU_COUNT                 = $(get_env_key GPU_COUNT)"
        echo "    MAX_CONCURRENT_TASKS      = $(get_env_key MAX_CONCURRENT_TASKS)"
        echo "    MINERU_VIRTUAL_VRAM_SIZE  = $(get_env_key MINERU_VIRTUAL_VRAM_SIZE)"
        echo "    WORKER_MEMORY_LIMIT       = $(get_env_key WORKER_MEMORY_LIMIT)"
        echo "    WORKER_MEMORY_RESERVATION = $(get_env_key WORKER_MEMORY_RESERVATION)"
        echo "    REDIS_QUEUE_ENABLED       = $(get_env_key REDIS_QUEUE_ENABLED)"
        echo "    RUSTFS_ENABLED            = $(get_env_key RUSTFS_ENABLED)"
        echo "    RUSTFS_PUBLIC_URL         = $(get_env_key RUSTFS_PUBLIC_URL)"
        echo "    MODEL_DOWNLOAD_SOURCE     = $(get_env_key MODEL_DOWNLOAD_SOURCE)"
        echo "    API_PORT / FRONTEND_PORT  = ${api_port} / ${frontend_port}"
        echo "    JWT_SECRET_KEY            = $(get_env_key JWT_SECRET_KEY | cut -c1-8)...（已生成，仅显示前 8 位）"
        echo "    TIANSHU_ADMIN_USERNAME    = $(get_env_key TIANSHU_ADMIN_USERNAME)（初始密码见 ${ENV_FILE}）"
    fi
    log_info "将创建目录: models/{,huggingface_cache,modelscope_cache} data/{uploads,output,db} logs/{backend,worker,mcp,scheduler}"
    if [ "${1:-}" = "compose" ]; then
        log_info "将执行构建: ${DC[*]} build --parallel"
        log_info "将启动服务: ${DC[*]} up -d"
        log_info "健康检查:   http://localhost:${api_port}/api/v1/health（最长 180s）"
        log_info "            http://localhost:${frontend_port}/api/v1/health（前端 nginx 代理）"
    fi
    log_warning "dry-run 模式：未执行依赖检查、构建与启动"
    echo ""
}

# ----------------------------------------------------------------------------
# 模式：gpu / pipeline
# ----------------------------------------------------------------------------
deploy_gpu_mode() {
    MODE="$1"
    local strict=0
    [ "$MODE" = "pipeline" ] && strict=1

    if [ "$DRY_RUN" -eq 1 ]; then
        detect_compose
        if ! command -v nvidia-smi > /dev/null 2>&1; then
            if [ "$strict" -eq 1 ]; then
                log_warning "dry-run：未检测到 GPU（正式执行 pipeline 模式时会直接报错退出）"
            else
                log_warning "dry-run：未检测到 GPU，将使用 .env 默认值继续预览"
            fi
        fi
    else
        check_dependencies "$strict"
    fi

    # compose 组合
    # 默认(gpu)模式使用本机 vLLM，需要 docker-compose.vllm.yml 叠加 docker.sock 挂载；
    # pipeline 模式不用 vLLM，因此不授予 worker 宿主机 Docker 权限
    DC+=(-f docker-compose.yml -f docker-compose.build.yml)
    if [ "$MODE" = "pipeline" ]; then
        DC+=(-f docker-compose.pipeline.yml)
    else
        DC+=(-f docker-compose.vllm.yml)
    fi

    prepare_env
    interactive_configure
    tune_env
    add_redis_profile
    persist_compose_file

    if [ "$DRY_RUN" -eq 1 ]; then
        dry_run_summary compose
        return 0
    fi

    create_directories
    build_images
    if [ "$MODE" = "gpu" ]; then
        "${DC[@]}" --profile manual create vllm-mineru
    fi
    start_services
    if ! verify_deployment; then
        log_warning "部署完成但存在告警，请查看上方信息"
    fi
    run_smoke_test || log_warning "解析自检未通过：服务已启动，但解析链路可能存在问题"
    show_info
}

# ----------------------------------------------------------------------------
# 模式：cpu（Mac Apple Silicon 本地开发）
# ----------------------------------------------------------------------------
deploy_cpu() {
    MODE="cpu"
    ENV_FILE=".env.cpu"

    log_info "CPU 本地开发模式（Dockerfile.cpu，linux/amd64）"

    if [ "$DRY_RUN" -eq 0 ]; then
        if ! command -v docker > /dev/null 2>&1; then
            log_error "未安装 Docker，请安装 Docker Desktop for Mac: https://www.docker.com/products/docker-desktop"
            exit 1
        fi
        if ! docker info > /dev/null 2>&1; then
            log_error "Docker 未运行，请先启动 Docker Desktop"
            exit 1
        fi
        log_success "Docker 正常: $(docker --version)"
    fi
    detect_compose

    if [ "$(uname -m)" = "arm64" ]; then
        log_info "检测到 Apple Silicon (arm64)"
        log_warning "请确认 Docker Desktop 已启用 Rosetta 2 模拟:"
        log_warning "  Settings → General → Use Rosetta for x86_64/amd64 emulation"
    fi

    # 离线模型确认
    if [ ! -d "./models-offline" ] || [ -z "$(ls -A ./models-offline 2> /dev/null)" ]; then
        log_warning "未找到离线模型目录 ./models-offline（或为空）"
        log_info "可先下载: python3 backend/download_models.py --output ./models-offline"
        if [ "$INTERACTIVE" -eq 1 ]; then
            if ! ask_yn "是否在无离线模型的情况下继续（运行时联网下载）" "N"; then
                log_error "已取消"
                exit 1
            fi
        else
            log_warning "非交互模式：继续执行，模型将在运行时按需下载"
        fi
    else
        log_success "离线模型已就绪: ./models-offline"
    fi

    # compose 组合
    DC+=(-f docker-compose.cpu.yml --env-file .env.cpu)

    # 生成 .env.cpu 并强制 CPU 配置
    prepare_env
    set_env_key ACCELERATOR cpu
    set_env_key CUDA_VISIBLE_DEVICES ""
    set_env_key MODEL_DOWNLOAD_SOURCE local
    set_env_key HF_HUB_OFFLINE 1
    ensure_jwt_secret
    ensure_rustfs_keys
    ensure_admin_password
    log_success ".env.cpu 已配置（ACCELERATOR=cpu / CUDA_VISIBLE_DEVICES=空 / MODEL_DOWNLOAD_SOURCE=local / HF_HUB_OFFLINE=1）"

    if [ "$DRY_RUN" -eq 1 ]; then
        dry_run_summary
        log_info "将执行构建: docker buildx build --platform linux/amd64 -f backend/Dockerfile.cpu --build-arg APT_MIRROR=... --build-arg PIP_MIRROR=... -t tianshu-backend-cpu:latest --load ."
        log_info "将启动服务: ${DC[*]} up -d"
        log_info "健康检查:   Backend /api/v1/health、Worker /health、前端 /、RustFS /health（端口读 .env.cpu）"
        return 0
    fi

    create_directories

    # 构建镜像
    log_info "构建后端 CPU 镜像（linux/amd64，首次约 30-60 分钟）..."
    DOCKER_BUILDKIT=1 docker buildx build \
        --platform linux/amd64 \
        --file backend/Dockerfile.cpu \
        --build-arg APT_MIRROR="$APT_MIRROR" \
        --build-arg PIP_MIRROR="$PIP_MIRROR" \
        --tag tianshu-backend-cpu:latest \
        --load \
        .
    log_success "后端 CPU 镜像构建完成: tianshu-backend-cpu:latest"

    if ! docker images | grep -q "tianshu-frontend"; then
        log_info "构建前端镜像..."
        DOCKER_BUILDKIT=1 docker buildx build \
            --platform linux/amd64 \
            --file frontend/Dockerfile \
            --build-arg NPM_REGISTRY="$NPM_REGISTRY" \
            --tag tianshu-frontend:latest \
            --load \
            .
        log_success "前端镜像构建完成"
    else
        log_info "前端镜像已存在，跳过（重建: docker rmi tianshu-frontend:latest 后重跑）"
    fi

    log_info "启动服务..."
    "${DC[@]}" up -d
    log_success "容器已启动，等待服务就绪..."
    sleep 10

    # 逐一健康检查（端口读 .env.cpu）
    local api_port
    local worker_port
    local frontend_port
    local rustfs_port
    api_port=$(get_env_key API_PORT)
    api_port="${api_port:-8000}"
    worker_port=$(get_env_key WORKER_PORT)
    worker_port="${worker_port:-8001}"
    frontend_port=$(get_env_key FRONTEND_PORT)
    frontend_port="${frontend_port:-80}"
    rustfs_port=$(get_env_key RUSTFS_PORT)
    rustfs_port="${rustfs_port:-9000}"

    if curl -fsS "http://localhost:${api_port}/api/v1/health" > /dev/null 2>&1; then
        log_success "Backend API 健康（:${api_port}）"
    else
        log_warning "Backend API 暂未响应（首次加载模型较慢，可稍后重试）"
    fi
    if curl -fsS "http://localhost:${worker_port}/health" > /dev/null 2>&1; then
        log_success "Worker 健康（:${worker_port}）"
    else
        log_warning "Worker 暂未响应（:${worker_port}）"
    fi
    if curl -fsS "http://localhost:${frontend_port}/" > /dev/null 2>&1; then
        log_success "前端健康（:${frontend_port}）"
    else
        log_warning "前端暂未响应（:${frontend_port}）"
    fi
    if curl -fsS "http://localhost:${rustfs_port}/health" > /dev/null 2>&1; then
        log_success "RustFS 健康（:${rustfs_port}）"
    else
        log_warning "RustFS 暂未响应（:${rustfs_port}）"
    fi

    # CPU 解析比 GPU 慢一个数量级，自检超时相应放宽
    TIANSHU_SMOKE_TIMEOUT="${TIANSHU_SMOKE_TIMEOUT:-1200}" run_smoke_test         || log_warning "解析自检未通过：服务已启动，但解析链路可能存在问题"

    echo ""
    log_success "=========================================="
    log_success " CPU 开发环境已启动"
    log_success "=========================================="
    echo ""
    echo "  Web UI:        http://localhost:${frontend_port}"
    echo "  API 文档:      http://localhost:${api_port}/docs"
    echo "  RustFS（回环）: http://127.0.0.1:${rustfs_port}"
    echo ""
    echo "  查看日志: ${DC[*]} logs -f"
    echo "  停止服务: ${DC[*]} down"
    echo ""
    log_warning "管理员账号: $(get_env_key TIANSHU_ADMIN_USERNAME)（初始密码见 ${ENV_FILE} 中 TIANSHU_ADMIN_PASSWORD，请妥善保存）"
    log_warning "CPU 模式比 GPU 慢 10-20 倍，仅适合开发调试"
    echo ""
}

# ----------------------------------------------------------------------------
# 模式：offline-build（联网机构建离线包）
# ----------------------------------------------------------------------------
offline_sha256() {
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum "$@"
    else
        shasum -a 256 "$@"
    fi
}

build_offline() {
    MODE="offline-build"
    local platform="${PLATFORM:-amd64}"
    local output_dir="./docker-images"
    local models_dir="./models-offline"
    # 镜像名与离线 Compose 一致。
    local images=(
        tianshu-backend:latest tianshu-frontend:latest
        "rustfs/rustfs:${RUSTFS_VERSION:-1.0.1}" vllm/vllm-openai:v0.31.0-cu129 redis:7-alpine
    )
    local archives=(
        "tianshu-backend-${platform}.tar.gz" "tianshu-frontend-${platform}.tar.gz"
        "rustfs-${platform}.tar.gz" "vllm-mineru-${platform}.tar.gz" "redis-${platform}.tar.gz"
    )

    log_info "离线镜像包构建（linux/${platform}，输出 ${output_dir}）"
    if [ "$DRY_RUN" -eq 1 ]; then
        log_info "将准备 ${models_dir} 中的 MinerU 4.2.3 模型与离线缓存，可用 --reuse-from 复用旧模型"
        log_info "将通过 backend/Dockerfile.offline 和 frontend/Dockerfile 构建后端和前端"
        log_info "将打包镜像 ${images[*]}、模型、配置文件并生成 SHA256SUMS"
        return 0
    fi

    if ! command -v docker > /dev/null 2>&1 || ! command -v python3 > /dev/null 2>&1; then
        log_error "离线打包需要 Docker、Python 3 和 Python venv"
        exit 1
    fi
    log_info "准备离线模型..."
    (
        download_venv=$(mktemp -d "${TMPDIR:-/tmp}/tianshu-models.XXXXXX")
        trap 'rm -rf "$download_venv"' EXIT
        python3 -m venv "$download_venv"
        "$download_venv/bin/python" -m pip install -i "$PIP_MIRROR" \
            'huggingface-hub[hf-xet]>=1.16' 'modelscope>=1.39.1' PyYAML loguru socksio
        local model_endpoint="${HF_ENDPOINT:-https://huggingface.co}"
        if [ "$NETWORK_ENV" = "cn" ]; then
            model_endpoint="${HF_ENDPOINT:-https://hf-mirror.com}"
        fi
        local model_args=(--output "$models_dir" --offline)
        local source
        for source in "${OPT_REUSE_FROM[@]}"; do
            model_args+=(--reuse-from "$source")
        done
        HF_ENDPOINT="$model_endpoint" "$download_venv/bin/python" backend/download_models.py "${model_args[@]}"
    )

    log_info "构建离线后端镜像..."
    docker buildx build --platform "linux/${platform}" --file backend/Dockerfile.offline \
        --build-arg APT_MIRROR="$APT_MIRROR" --build-arg PIP_MIRROR="$PIP_MIRROR" --tag "${images[0]}" --load .
    log_info "构建离线前端镜像..."
    docker buildx build --platform "linux/${platform}" --file frontend/Dockerfile \
        --build-arg NPM_REGISTRY="$NPM_REGISTRY" --tag "${images[1]}" --load .

    local i
    for i in 2 3 4; do
        docker pull --platform "linux/${platform}" "${images[$i]}"
    done

    log_info "导出离线镜像及模型..."
    mkdir -p "$output_dir/scripts"
    rm -f "$output_dir/SHA256SUMS"
    for i in "${!images[@]}"; do
        docker save "${images[$i]}" | gzip > "$output_dir/${archives[$i]}"
    done
    tar czf "$output_dir/models-offline.tar.gz" "${models_dir#./}"

    cp docker-compose.offline.yml "$output_dir/docker-compose.yml"
    cp docker-compose.offline.yml .env.example mcp_config.example.json "$output_dir/"
    cp setup.sh "$output_dir/setup.sh"
    cp scripts/smoke-sample.pdf "$output_dir/scripts/"
    (
        cd "$output_dir"
        offline_sha256 "${archives[@]}" models-offline.tar.gz docker-compose.yml docker-compose.offline.yml \
            .env.example mcp_config.example.json setup.sh scripts/smoke-sample.pdf > SHA256SUMS.tmp
        mv SHA256SUMS.tmp SHA256SUMS
    )
    log_success "离线包已生成：${output_dir}（$(du -sh "$output_dir" | cut -f1)）"
}

# ----------------------------------------------------------------------------
# 模式：offline-deploy（离线部署）
# ----------------------------------------------------------------------------
deploy_offline() {
    MODE="offline-deploy"

    local compose_file="docker-compose.offline.yml"
    local platform
    case "$(uname -m)" in
        x86_64) platform=amd64 ;;
        aarch64 | arm64) platform=arm64 ;;
        *)
            log_error "不支持的离线部署架构：$(uname -m)"
            exit 1
            ;;
    esac
    local archives=(
        "tianshu-backend-${platform}.tar.gz" "tianshu-frontend-${platform}.tar.gz"
        "rustfs-${platform}.tar.gz" "vllm-mineru-${platform}.tar.gz" "redis-${platform}.tar.gz"
    )
    if [ "$DRY_RUN" -eq 1 ]; then
        log_info "将校验 SHA256SUMS，加载后端、前端、RustFS、vLLM 和 Redis 镜像"
        log_info "将通过已加载的镜像测试 GPU，解压模型，配置环境并启动服务"
        log_info "将创建 vLLM 容器，由 Worker 在解析请求到达时启动"
        return 0
    fi

    detect_compose
    if ! command -v nvidia-smi > /dev/null 2>&1 || ! nvidia-smi > /dev/null 2>&1; then
        log_error "未检测到可用的 NVIDIA 驱动，离线 GPU 部署无法继续"
        exit 1
    fi
    nvidia-smi --query-gpu=index,name,driver_version,memory.total --format=csv,noheader

    if [ ! -f SHA256SUMS ]; then
        log_error "缺少 SHA256SUMS，请重新传输完整的 4.2.3 离线包"
        exit 1
    fi
    local file
    for file in "${archives[@]}" models-offline.tar.gz "$compose_file" .env.example mcp_config.example.json setup.sh scripts/smoke-sample.pdf; do
        if [ ! -f "$file" ] || ! awk -v name="$file" '$2 == name { found=1 } END { exit !found }' SHA256SUMS; then
            log_error "离线包缺少文件或校验记录：$file"
            exit 1
        fi
    done
    if ! offline_sha256 -c SHA256SUMS; then
        log_error "离线包校验失败，请重新传输报错的文件"
        exit 1
    fi

    log_info "加载离线镜像..."
    for file in "${archives[@]}"; do
        docker load < "$file"
    done
    local image
    for image in tianshu-backend:latest vllm/vllm-openai:v0.31.0-cu129; do
        if ! docker run --rm --pull never --gpus all --entrypoint nvidia-smi "$image" > /dev/null; then
            log_error "镜像 $image 无法访问 GPU，请检查 NVIDIA Container Toolkit 和宿主机驱动"
            exit 1
        fi
    done
    log_success "离线后端与 vLLM 镜像可访问 GPU"

    log_info "解压离线模型..."
    tar xzf models-offline.tar.gz
    mkdir -p models-offline/huggingface_cache models-offline/modelscope_cache models-offline/ultralytics_cfg
    chmod a+rwx models-offline/huggingface_cache models-offline/modelscope_cache 2> /dev/null || true

    local existing_env=0
    if [ -f "$ENV_FILE" ]; then
        existing_env=1
    fi
    prepare_env
    tune_env "$existing_env"
    create_directories

    # 保证复制来的 manual profile 不会使 vLLM 随常规服务一起启动。
    export COMPOSE_PROFILES=""
    set_env_key COMPOSE_PROFILES ""
    DC+=(-f "$compose_file")
    add_redis_profile
    persist_compose_file

    log_info "创建按需启动的 vLLM 容器..."
    "${DC[@]}" --profile manual create --pull never vllm-mineru
    log_info "启动离线服务..."
    "${DC[@]}" up -d --pull never
    verify_deployment || log_warning "健康检查存在告警，请查看上方信息"
    run_smoke_test || log_warning "解析自检未通过，请查看 Worker 日志"

    if "${DC[@]}" exec -T worker nvidia-smi > /dev/null 2>&1; then
        log_success "Worker 容器可访问 GPU"
    else
        log_warning "当前无法在 Worker 容器内验证 GPU"
    fi
    show_info
}

# ----------------------------------------------------------------------------
# 模式：native（本机原生运行，不用 Docker；Apple Silicon 自动启用 MPS 加速）
# ----------------------------------------------------------------------------
deploy_native() {
    MODE="native"

    local os_type arch
    os_type=$(uname -s)
    arch=$(uname -m)
    local is_mac_arm=0
    if [ "$os_type" = "Darwin" ] && [ "$arch" = "arm64" ]; then
        is_mac_arm=1
    fi

    log_info "原生部署模式（不依赖 Docker）"
    if [ "$is_mac_arm" -eq 1 ]; then
        log_info "检测到 Apple Silicon，MinerU 小模型自动使用 MPS 加速，本地 VLM 使用 llama.cpp"
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        log_info "将执行的操作:"
        echo "    1. 检查 Python 3.11 或 3.12、ffmpeg（音视频解析需要）"
        echo "    2. 创建 .venv 虚拟环境并安装 backend/requirements.txt"
        echo "    3. 交互选择是否下载 VLM 模型，下载模型到 ./models"
        echo "    4. 生成 models/config.yaml（指向本机 ./models 绝对路径）"
        echo "    5. 生成 backend/.env（若不存在）"
        echo "    6. 前台启动: .venv/bin/python backend/start_all.py --output-dir ./data/output"
        log_warning "dry-run 模式：未执行任何操作"
        return 0
    fi

    # --- 1. 环境检查 ---
    local py=""
    local cand ver
    for cand in python3.12 python3.11 python3; do
        if command -v "$cand" > /dev/null 2>&1; then
            ver=$("$cand" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2> /dev/null || true)
            case "$ver" in
                3.11 | 3.12)
                    py="$cand"
                    break
                    ;;
            esac
        fi
    done
    if [ -z "$py" ]; then
        log_error "未找到 Python 3.11 或 3.12"
        [ "$os_type" = "Darwin" ] && log_info "可执行: brew install python@3.12"
        exit 1
    fi
    log_success "Python: $("$py" --version 2>&1)"

    if ! command -v ffmpeg > /dev/null 2>&1; then
        log_warning "未检测到 ffmpeg（仅音视频解析需要，可稍后安装）"
        [ "$os_type" = "Darwin" ] && log_info "可执行: brew install ffmpeg"
    fi

    # --- 2. 虚拟环境与依赖 ---
    if [ ! -x ".venv/bin/python" ]; then
        log_info "创建虚拟环境 .venv ..."
        "$py" -m venv .venv || {
            log_error "创建 .venv 失败"
            exit 1
        }
    fi
    log_info "安装后端依赖（首次约 10-20 分钟）..."
    .venv/bin/pip install --upgrade pip -i "$PIP_MIRROR"
    if ! .venv/bin/pip install -r backend/requirements.txt -i "$PIP_MIRROR"; then
        log_error "依赖安装失败"
        exit 1
    fi
    log_success "依赖安装完成"

    # --- 3. 模型下载 ---
    local models="mineru_pipeline,sensevoice"
    if ask_yn "是否下载本地 VLM 模型 MinerU2.5-Pro GGUF（用于 llama.cpp）" "Y"; then
        models="mineru_pipeline,mineru_vlm_gguf,sensevoice"
    fi
    local root
    root=$(pwd)
    local model_endpoint="${HF_ENDPOINT:-https://huggingface.co}"
    if [ "$NETWORK_ENV" = "cn" ]; then
        model_endpoint="${HF_ENDPOINT:-https://hf-mirror.com}"
    fi
    log_info "下载模型到 ./models ..."
    if ! HF_ENDPOINT="$model_endpoint" .venv/bin/python backend/download_models.py \
        --output ./models --runtime-root "${root}/models" --models "$models"; then
        log_error "模型准备失败，请修复下载问题后重新执行原生部署"
        exit 1
    fi
    export MINERU_CONFIG="${root}/models/config.yaml"

    # --- 5. backend/.env ---
    if [ ! -f "backend/.env" ]; then
        cp backend/.env.example backend/.env
        # 本地运行也生成随机 JWT 密钥，避免使用示例占位符
        local jwt
        jwt=$(openssl rand -hex 32 2> /dev/null || cat /dev/urandom | LC_ALL=C tr -dc 'a-f0-9' | head -c 64)
        if [ -n "$jwt" ]; then
            sed_i "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=${jwt}|" backend/.env
        fi
        log_success "已生成 backend/.env（JWT 密钥已随机化）"
    fi

    create_directories

    # --- 6. 前台启动（Ctrl+C 停止全部服务） ---
    echo ""
    log_success "准备就绪，即将前台启动后端服务（API + Worker + Scheduler）"
    echo ""
    echo "  API 文档:  http://localhost:8000/docs"
    echo "  前端界面:  cd frontend && npm install && npm run dev"
    echo "  停止服务:  Ctrl+C"
    echo ""
    # native 模式是前台 exec 启动，装不下部署后自检；给出单独执行的命令
    echo "  解析自检:  另开一个终端执行 bash setup.sh --smoke-only --port 8000"
    echo ""
    exec .venv/bin/python backend/start_all.py --output-dir ./data/output
}

# ----------------------------------------------------------------------------
# 交互式模式菜单
# ----------------------------------------------------------------------------
choose_mode() {
    echo ""
    echo "╔════════════════════════════════════════╗"
    echo "║   Tianshu (天枢) 一键部署              ║"
    echo "╚════════════════════════════════════════╝"
    echo ""
    echo "请选择部署模式:"
    echo "  1) GPU 标准部署（全引擎，docker-compose.yml）"
    echo "  2) 纯 pipeline 部署（省 4GB 模型，自动调谐并发/显存）"
    echo "  3) CPU 本地开发（Mac Apple Silicon）"
    echo "  4) 离线构建（联网机：构建离线镜像包）"
    echo "  5) 离线部署（生产机：部署离线镜像包）"
    echo "  6) 原生部署（不用 Docker，Mac Apple Silicon 自动 MPS 加速）"
    echo "  0) 退出"
    echo ""

    local choice=""
    while true; do
        printf '%b[?]%b 请输入选项 [0-6]: ' "$BLUE" "$NC"
        read -r choice
        case "$choice" in
            1) MODE="gpu"; break ;;
            2) MODE="pipeline"; break ;;
            3) MODE="cpu"; break ;;
            4) MODE="offline-build"; break ;;
            5) MODE="offline-deploy"; break ;;
            6) MODE="native"; break ;;
            0) log_info "已退出"; exit 0 ;;
            *) log_error "无效选项，请重新输入" ;;
        esac
    done
}

# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
main() {
    trap 'log_warning "操作被用户中断"; exit 130' INT TERM

    # 只跑自检：对已经启动的部署验证解析链路，不做任何部署动作。
    # native 模式（前台 exec 启动）与部署后复查都走这条路径。
    if [ "$SMOKE_ONLY" -eq 1 ]; then
        [ "$MODE" = "cpu" ] && ENV_FILE=".env.cpu"
        if [ ! -f "$ENV_FILE" ]; then
            log_error "未找到 $ENV_FILE，请在部署目录下执行，或用 --mode 指定模式"
            exit 1
        fi
        detect_compose
        run_smoke_test "$OPT_PORT"
        exit $?
    fi

    if [ -z "$MODE" ]; then
        if [ "$INTERACTIVE" -eq 1 ]; then
            choose_mode
        else
            log_error "非交互环境必须通过 --mode 指定部署模式"
            usage
            exit 1
        fi
    fi

    case "$MODE" in
        gpu | pipeline | cpu | offline-build | native) choose_network ;;
    esac

    case "$MODE" in
        gpu) deploy_gpu_mode gpu ;;
        pipeline) deploy_gpu_mode pipeline ;;
        cpu) deploy_cpu ;;
        offline-build) build_offline ;;
        offline-deploy) deploy_offline ;;
        native) deploy_native ;;
        *)
            log_error "未知模式: ${MODE}（可选: gpu / pipeline / cpu / offline-build / offline-deploy / native）"
            exit 1
            ;;
    esac
}

main
