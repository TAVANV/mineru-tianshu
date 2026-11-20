#!/bin/bash
# Tianshu - Docker Entrypoint Script
# Container startup script for initialization and health checks

set -e

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Log functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# ============================================================================
# Environment check
# ============================================================================
check_environment() {
    local service_type=$1

    log_info "Checking environment configuration..."

    # Check Python version
    PYTHON_VERSION=$(python --version 2>&1 | awk '{print $2}')
    log_info "Python version: $PYTHON_VERSION"

    # Check CUDA
    if command -v nvidia-smi &> /dev/null; then
        log_success "NVIDIA GPU detected"
        nvidia-smi --query-gpu=gpu_name,driver_version,memory.total --format=csv,noheader
    else
        log_warning "NVIDIA GPU or driver not detected"
    fi

    # Check necessary environment variables (only API Server needs JWT)
    if [ "$service_type" != "worker" ] && [ "$service_type" != "mcp" ]; then
        if [ -z "$JWT_SECRET_KEY" ]; then
            log_error "JWT_SECRET_KEY is not set! Please configure in .env"
            exit 1
        fi

        # 已知占位符一律拒绝启动（fail-closed），防止弱密钥上线
        JWT_PLACEHOLDERS="your-secret-key-change-in-production dev-secret-key-change-in-production cpu-dev-secret-key-change-in-production temp-secret-key CHANGE_THIS_TO_A_SECURE_RANDOM_STRING_IN_PRODUCTION"
        for placeholder in $JWT_PLACEHOLDERS; do
            if [ "$JWT_SECRET_KEY" = "$placeholder" ]; then
                log_error "JWT_SECRET_KEY is a known placeholder: $placeholder"
                log_error "Generate a random one: openssl rand -hex 32 (or run setup.sh)"
                exit 1
            fi
        done
    fi
}

# ============================================================================
# Directory initialization
# ============================================================================
initialize_directories() {
    log_info "Initializing directory structure..."

    # /app/models 允许只读挂载（离线部署直接把 models-offline 挂成 :ro），
    # 因此建目录失败不视为错误
    mkdir -p /app/models 2> /dev/null || true
    mkdir -p /app/data/uploads
    mkdir -p /app/data/output
    mkdir -p /app/data/db
    mkdir -p /app/logs

    # 真正需要写入的目录：不可写直接退出，避免任务跑到一半才失败
    local dir
    for dir in /app/data/uploads /app/data/output /app/data/db /app/logs; do
        if [ ! -w "$dir" ]; then
            log_error "Directory $dir is not writable by the container user (UID $(id -u))."
            log_error "Fix on host: 让部署目录属主与 TIANSHU_UID/TIANSHU_GID 一致，"
            log_error "             或执行 chmod -R a+rwX <host dir>（参考 setup.sh create_directories）"
            exit 1
        fi
    done

    # /app/models 只读即可：config.yaml 与模型权重都只被读取。
    # 只有需要在容器内下载模型时才必须可写，这里降级为提示。
    if [ ! -w /app/models ]; then
        log_info "/app/models is read-only (expected for offline deployments; models are only read)"
    fi

    log_success "Directory structure initialized"
}

# ============================================================================
# MinerU 直接读取挂载的配置。
# ============================================================================
setup_mineru_config() {
    local config="${MINERU_CONFIG:-/app/models/config.yaml}"
    if [ -f "$config" ]; then
        export MINERU_CONFIG="$config"
        log_success "已加载 MinerU 配置路径 $MINERU_CONFIG"
    elif [ -n "${MINERU_CONFIG:-}" ]; then
        log_warning "MinerU 配置文件不存在：$config"
    fi
}

# ============================================================================
# Model initialization
# ============================================================================
initialize_models() {
    log_info "Initializing models..."

    # 检查是否有外部模型卷挂载
    if [ -d "/models-external" ]; then
        # 调用统一的模型初始化脚本
        INIT_SCRIPT="/usr/local/bin/init-models.sh"

        if [ -f "$INIT_SCRIPT" ]; then
            log_info "Running model initialization script: $INIT_SCRIPT"
            bash "$INIT_SCRIPT" || log_warning "Model initialization script failed, continuing..."
        else
            log_warning "Model initialization script not found: $INIT_SCRIPT"
        fi
    else
        log_warning "External models directory (/models-external) not found"
        log_warning "Models will be downloaded on first use"
    fi
}

# ============================================================================
# Model check
# ============================================================================
check_models() {
    log_info "Checking model files..."

    MODEL_PATH=${MODEL_PATH:-/app/models}

    if [ ! -d "$MODEL_PATH" ]; then
        log_warning "Model directory does not exist, will create $MODEL_PATH"
        mkdir -p "$MODEL_PATH"
    fi

    # Check key models
    if [ -d "$MODEL_PATH/sensevoice" ]; then
        log_success "SenseVoice model found"
    else
        log_warning "SenseVoice model not found, audio processing features will be limited"
    fi
}

# ============================================================================
# Database initialization
# ============================================================================
initialize_database() {
    log_info "Checking database..."

    DB_PATH=${DATABASE_PATH:-/app/data/db/mineru_tianshu.db}

    if [ -f "$DB_PATH" ]; then
        log_success "Database exists: $DB_PATH"
    else
        log_info "First run, database will be automatically created"
    fi
}

# ============================================================================
# Health check
# ============================================================================
wait_for_service() {
    local service_url=$1
    local service_name=$2
    local max_retries=30
    local retry_count=0

    log_info "Waiting for $service_name to start..."

    while [ $retry_count -lt $max_retries ]; do
        if curl -f -s "$service_url" > /dev/null 2>&1; then
            log_success "$service_name is ready"
            return 0
        fi

        retry_count=$((retry_count + 1))
        log_info "Waiting... ($retry_count/$max_retries)"
        sleep 2
    done

    log_error "$service_name startup timeout"
    return 1
}

# ============================================================================
# GPU check
# ============================================================================
check_gpu() {
    log_info "Checking GPU availability..."

    # Check PyTorch
    python -c "import torch; print('PyTorch CUDA:', torch.cuda.is_available())" 2>&1 | while read -r line; do
        log_info "$line"
    done

    # Check device information
    if python -c "import torch; print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'No GPU')" 2>&1 | grep -q "No GPU"; then
        log_warning "No available GPU device detected"
    else
        GPU_NAME=$(python -c "import torch; print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')")
        log_success "GPU device: $GPU_NAME"
    fi
}

# ============================================================================
# Main entry point
# ============================================================================
main() {
    log_info "=========================================="
    log_info "Tianshu Starting..."
    log_info "=========================================="

    # First determine service type
    SERVICE_TYPE=${1:-api}

    # Run checks (pass service type)
    check_environment "$SERVICE_TYPE"
    initialize_directories

    initialize_database

    # Initialize models before checking (for worker only)
    if [ "$SERVICE_TYPE" = "worker" ]; then
        initialize_models
    fi

    # 先完成外置模型初始化，再设置配置路径。
    setup_mineru_config

    check_models

    # Execute different checks based on service type

    if [ "$SERVICE_TYPE" = "worker" ]; then
        log_info "Startup type: LitServe Worker"
        check_gpu
        shift  # Remove first argument (service type)
    elif [ "$SERVICE_TYPE" = "mcp" ]; then
        log_info "Startup type: MCP Server"
        shift  # Remove first argument (service type)
    else
        log_info "Startup type: API Server"
        # If first argument is "api", also need to remove it
        if [ "$1" = "api" ]; then
            shift
        fi
    fi

    log_info "=========================================="
    log_success "Initialization complete, starting service..."
    log_info "=========================================="

    # Execute the passed command (at this point $@ no longer contains service type argument)
    exec "$@"
}

# Catch signals for graceful shutdown
trap 'log_warning "Received termination signal, shutting down..."; exit 0' SIGTERM SIGINT

# Execute main function
main "$@"
