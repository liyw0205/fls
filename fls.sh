#!/bin/sh
# ============================================================
# FLS Manager 启停脚本
#
# 兼容：
#   Termux / Linux
#   bash / ash / sh
#
# 用法：
#   sh fls.sh start [-t Token] [-p 端口]
#   sh fls.sh stop
#   sh fls.sh restart [-t Token] [-p 端口]
#   sh fls.sh status
#   sh fls.sh log
#   sh fls.sh update
#   sh fls.sh clone
#   sh fls.sh bstart
#   sh fls.sh rstart
#   sh fls.sh ensure-repo
# ============================================================

set -u

REPO_URL="${FLS_REPO_URL:-https://github.com/liyw0205/fls.git}"
REPO_BRANCH="${FLS_REPO_BRANCH:-main}"

SCRIPT_PATH="$0"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd)"
CURRENT_DIR="$(pwd 2>/dev/null || echo .)"

DEFAULT_PORT="5700"

say() {
  echo "[FLS] $*"
}

err() {
  echo "[FLS][ERROR] $*" >&2
}

is_termux() {
  if [ -n "${PREFIX:-}" ] && echo "$PREFIX" | grep -q "com.termux"; then
    return 0
  fi

  if [ -n "${TERMUX_VERSION:-}" ]; then
    return 0
  fi

  case "${HOME:-}" in
    /data/data/com.termux*)
      return 0
      ;;
  esac

  return 1
}

detect_base_dir() {
  if [ -n "${FLS_BASE_DIR:-}" ]; then
    cd "$FLS_BASE_DIR" 2>/dev/null && pwd
    return 0
  fi

  if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/fls-manager.py" ] && [ -d "$SCRIPT_DIR/fls_manager" ]; then
    cd "$SCRIPT_DIR" 2>/dev/null && pwd
    return 0
  fi

  if [ -n "$CURRENT_DIR" ] && [ -f "$CURRENT_DIR/fls-manager.py" ] && [ -d "$CURRENT_DIR/fls_manager" ]; then
    cd "$CURRENT_DIR" 2>/dev/null && pwd
    return 0
  fi

  if is_termux; then
    echo "$HOME/fls"
    return 0
  fi

  if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
    echo "/root/fls"
    return 0
  fi

  if [ -d "/root/fls" ] && [ -r "/root/fls" ]; then
    echo "/root/fls"
    return 0
  fi

  if [ -n "${HOME:-}" ]; then
    echo "$HOME/fls"
    return 0
  fi

  echo "$SCRIPT_DIR"
}

BASE_DIR="$(detect_base_dir)"

MANAGER_FILE="$BASE_DIR/fls-manager.py"
PACKAGE_DIR="$BASE_DIR/fls_manager"
DATA_DIR="$BASE_DIR/data"
LOG_DIR="$BASE_DIR/log"
PID_FILE="$DATA_DIR/fls-manager.pid"
DAEMON_LOG="$LOG_DIR/fls-manager-daemon.log"
VENV_DIR="$BASE_DIR/.venv"

mkdir -p "$DATA_DIR" "$LOG_DIR" 2>/dev/null || true

try_install_pkg() {
  pkg_name="$1"

  if command -v pkg >/dev/null 2>&1; then
    pkg update -y >/dev/null 2>&1 || true
    pkg install -y "$pkg_name" >/dev/null 2>&1 && return 0
  fi

  if command -v apt >/dev/null 2>&1; then
    apt update >/dev/null 2>&1 || true
    apt install -y "$pkg_name" >/dev/null 2>&1 && return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    apt-get update >/dev/null 2>&1 || true
    apt-get install -y "$pkg_name" >/dev/null 2>&1 && return 0
  fi

  if command -v dnf >/dev/null 2>&1; then
    dnf install -y "$pkg_name" >/dev/null 2>&1 && return 0
  fi

  if command -v yum >/dev/null 2>&1; then
    yum install -y "$pkg_name" >/dev/null 2>&1 && return 0
  fi

  if command -v apk >/dev/null 2>&1; then
    apk add --no-cache "$pkg_name" >/dev/null 2>&1 && return 0
  fi

  if command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm "$pkg_name" >/dev/null 2>&1 && return 0
  fi

  return 1
}

ensure_git() {
  if command -v git >/dev/null 2>&1; then
    return 0
  fi

  say "未找到 git，尝试自动安装..."

  if try_install_pkg git && command -v git >/dev/null 2>&1; then
    say "git 安装成功"
    return 0
  fi

  err "未找到 git，且自动安装失败"
  err "请手动安装 git 后重试"
  exit 1
}

ensure_python_cmd() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi

  say "未找到 Python，尝试自动安装..."

  if command -v pkg >/dev/null 2>&1; then
    try_install_pkg python || true
  elif command -v apk >/dev/null 2>&1; then
    try_install_pkg python3 || true
  else
    try_install_pkg python3 || true
  fi

  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi

  err "未找到 python3 / python，且自动安装失败"
  exit 1
}

repo_ready() {
  if [ -f "$MANAGER_FILE" ] && [ -d "$PACKAGE_DIR" ]; then
    return 0
  fi

  return 1
}

clone_repo_to_dir() {
  target="$1"
  parent="$(dirname "$target")"

  mkdir -p "$parent" 2>/dev/null || true

  if [ ! -e "$target" ]; then
    say "准备克隆 FLS 仓库到：$target"
    git clone --depth 1 -b "$REPO_BRANCH" "$REPO_URL" "$target"
    return $?
  fi

  tmp="${target}.clone.$$"
  rm -rf "$tmp" 2>/dev/null || true

  say "工作目录已存在，先克隆到临时目录：$tmp"

  git clone --depth 1 -b "$REPO_BRANCH" "$REPO_URL" "$tmp" || {
    rm -rf "$tmp" 2>/dev/null || true
    return 1
  }

  mkdir -p "$target" 2>/dev/null || true

  if command -v tar >/dev/null 2>&1; then
    (
      cd "$tmp" || exit 1
      tar cf - .
    ) | (
      cd "$target" || exit 1
      tar xf -
    )
  else
    cp -R "$tmp"/. "$target"/
  fi

  rm -rf "$tmp" 2>/dev/null || true
  return 0
}

ensure_repo() {
  ensure_git

  if repo_ready; then
    return 0
  fi

  say "未检测到完整模块化 FLS 程序"
  say "仓库地址：$REPO_URL"
  say "分支：$REPO_BRANCH"
  say "工作目录：$BASE_DIR"

  clone_repo_to_dir "$BASE_DIR" || {
    err "git clone 失败"
    err "请检查网络，或手动执行："
    err "git clone -b $REPO_BRANCH $REPO_URL $BASE_DIR"
    exit 1
  }

  if ! repo_ready; then
    err "仓库拉取完成，但未找到 fls-manager.py 或 fls_manager 目录"
    err "请检查仓库内容是否正确：$BASE_DIR"
    exit 1
  fi

  say "FLS 仓库已就绪：$BASE_DIR"
}

force_clone_repo() {
  ensure_git

  say "准备强制重新 clone FLS 仓库"
  say "仓库地址：$REPO_URL"
  say "分支：$REPO_BRANCH"
  say "工作目录：$BASE_DIR"

  tmp="${BASE_DIR}.reclone.$$"
  rm -rf "$tmp" 2>/dev/null || true

  git clone --depth 1 -b "$REPO_BRANCH" "$REPO_URL" "$tmp" || {
    rm -rf "$tmp" 2>/dev/null || true
    err "git clone 失败"
    exit 1
  }

  mkdir -p "$BASE_DIR" 2>/dev/null || true

  if command -v tar >/dev/null 2>&1; then
    (
      cd "$tmp" || exit 1
      tar cf - .
    ) | (
      cd "$BASE_DIR" || exit 1
      tar xf -
    )
  else
    cp -R "$tmp"/. "$BASE_DIR"/
  fi

  rm -rf "$tmp" 2>/dev/null || true

  if ! repo_ready; then
    err "重新 clone 后程序仍不完整"
    exit 1
  fi

  say "强制重新 clone 完成"
}

validate_python_files() {
  py="$1"

  if [ ! -f "$MANAGER_FILE" ]; then
    err "未找到 $MANAGER_FILE"
    exit 1
  fi

  "$py" -m py_compile "$MANAGER_FILE" >/dev/null 2>&1 || {
    err "fls-manager.py 语法检查失败"
    exit 1
  }
}

ensure_python_env() {
  ensure_repo

  py_sys="$(ensure_python_cmd)"

  if [ -x "$VENV_DIR/bin/python" ]; then
    echo "$VENV_DIR/bin/python"
    return 0
  fi

  if "$py_sys" -m venv "$VENV_DIR" >/dev/null 2>&1; then
    py_bin="$VENV_DIR/bin/python"
    "$py_bin" -m pip install --upgrade pip >/dev/null 2>&1 || true
    echo "$py_bin"
    return 0
  fi

  echo "$py_sys"
}

install_basic_deps() {
  py="$1"

  "$py" - <<'PY' >/dev/null 2>&1
import importlib.util
mods = ["flask", "requests", "apscheduler", "socks"]
raise SystemExit(0 if all(importlib.util.find_spec(m) for m in mods) else 1)
PY

  if [ "$?" = "0" ]; then
    return 0
  fi

  say "检测到基础 Python 依赖缺失，尝试安装..."

  "$py" -m pip install flask requests apscheduler PySocks tzdata setproctitle >/dev/null 2>&1 || \
  "$py" -m pip install --break-system-packages flask requests apscheduler PySocks tzdata setproctitle >/dev/null 2>&1 || \
  "$py" -m pip install --user flask requests apscheduler PySocks tzdata setproctitle >/dev/null 2>&1 || true
}

read_config_value() {
  key="$1"
  default_value="$2"
  py="$(ensure_python_cmd)"

  "$py" - "$BASE_DIR" "$key" "$default_value" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

base = Path(sys.argv[1])
key = sys.argv[2]
default = sys.argv[3]
cfg_file = base / "data" / "config.json"

try:
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    value = data.get(key, default)
    if value is None or value == "":
        value = default
    print(value)
except Exception:
    print(default)
PY
}

get_pid() {
  if [ -f "$PID_FILE" ]; then
    cat "$PID_FILE" 2>/dev/null | tr -dc '0-9'
    return 0
  fi

  echo ""
}

is_running_pid() {
  pid="$1"

  if [ -z "$pid" ]; then
    return 1
  fi

  if kill -0 "$pid" >/dev/null 2>&1; then
    return 0
  fi

  return 1
}

find_running_pids() {
  if command -v pgrep >/dev/null 2>&1; then
    pgrep -f "fls-manager.py" 2>/dev/null || true
    return 0
  fi

  ps -ef 2>/dev/null | grep "fls-manager.py" | grep -v grep | awk '{print $2}' || true
}

parse_start_opts() {
  TEMP_TOKEN=""
  TEMP_PORT=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      -t|--token)
        shift
        if [ "$#" -eq 0 ]; then
          err "-t / --token 后面需要填写 Token"
          exit 1
        fi
        TEMP_TOKEN="$1"
        ;;
      -p|--port)
        shift
        if [ "$#" -eq 0 ]; then
          err "-p / --port 后面需要填写端口"
          exit 1
        fi
        TEMP_PORT="$1"
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        err "未知参数：$1"
        usage
        exit 1
        ;;
    esac
    shift
  done

  if [ -n "$TEMP_PORT" ]; then
    case "$TEMP_PORT" in
      *[!0-9]*)
        err "端口必须是数字：$TEMP_PORT"
        exit 1
        ;;
    esac

    if [ "$TEMP_PORT" -lt 1 ] || [ "$TEMP_PORT" -gt 65535 ]; then
      err "端口范围必须是 1-65535"
      exit 1
    fi
  fi
}

start_fls() {
  parse_start_opts "$@"

  ensure_repo

  old_pid="$(get_pid)"
  if is_running_pid "$old_pid"; then
    say "FLS Manager 已在运行，PID: $old_pid"
    say "如需应用新的临时端口/Token，请执行：sh fls.sh restart -p 端口 -t Token"
    return 0
  fi

  py_bin="$(ensure_python_env)"
  validate_python_files "$py_bin"
  install_basic_deps "$py_bin"

  cd "$BASE_DIR" || exit 1

  export FLS_BASE_DIR="$BASE_DIR"
  export FLS_PYTHON="$py_bin"

  if [ -n "$TEMP_TOKEN" ]; then
    export FLS_TOKEN="$TEMP_TOKEN"
    say "本次启动使用临时 Token：已设置"
  fi

  if [ -n "$TEMP_PORT" ]; then
    export FLS_PORT="$TEMP_PORT"
    say "本次启动使用临时端口：$TEMP_PORT"
  fi

  say "启动 FLS Manager..."
  say "工作目录：$BASE_DIR"
  say "日志文件：$DAEMON_LOG"

  mkdir -p "$DATA_DIR" "$LOG_DIR" 2>/dev/null || true

  nohup "$py_bin" "$MANAGER_FILE" >> "$DAEMON_LOG" 2>&1 &
  pid="$!"

  echo "$pid" > "$PID_FILE"

  sleep 2

  if is_running_pid "$pid"; then
    port_show="${TEMP_PORT:-$(read_config_value port "$DEFAULT_PORT")}"
    say "启动成功，PID: $pid"
    say "访问地址：http://服务器IP:$port_show"
    say "如首次使用，请访问面板完成 Token 设置"
  else
    err "启动失败，请查看日志：$DAEMON_LOG"
    tail -n 100 "$DAEMON_LOG" 2>/dev/null || true
    exit 1
  fi
}

stop_fls() {
  pids=""

  pid="$(get_pid)"
  if is_running_pid "$pid"; then
    pids="$pid"
  fi

  found="$(find_running_pids)"
  if [ -n "$found" ]; then
    pids="$pids $found"
  fi

  pids="$(echo "$pids" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u | tr '\n' ' ')"

  if [ -z "$pids" ]; then
    say "FLS Manager 未运行"
    rm -f "$PID_FILE"
    return 0
  fi

  say "准备停止 FLS Manager：$pids"

  for p in $pids; do
    kill "$p" >/dev/null 2>&1 || true
  done

  sleep 2

  for p in $pids; do
    if kill -0 "$p" >/dev/null 2>&1; then
      kill -9 "$p" >/dev/null 2>&1 || true
    fi
  done

  rm -f "$PID_FILE"
  say "已停止"
}

restart_fls() {
  parse_start_opts "$@"

  saved_token="$TEMP_TOKEN"
  saved_port="$TEMP_PORT"

  stop_fls

  if [ -n "$saved_token" ] && [ -n "$saved_port" ]; then
    start_fls -t "$saved_token" -p "$saved_port"
  elif [ -n "$saved_token" ]; then
    start_fls -t "$saved_token"
  elif [ -n "$saved_port" ]; then
    start_fls -p "$saved_port"
  else
    start_fls
  fi
}

status_fls() {
  cfg_port="$(read_config_value port "$DEFAULT_PORT")"
  cfg_token="$(read_config_value admin_token "")"
  pid="$(get_pid)"

  echo "===================================================="
  echo "FLS Manager 状态"
  echo "仓库地址：$REPO_URL"
  echo "工作目录：$BASE_DIR"
  echo "主文件：$MANAGER_FILE"
  echo "模块目录：$PACKAGE_DIR"
  echo "PID 文件：$PID_FILE"
  echo "日志文件：$DAEMON_LOG"
  echo "配置端口：$cfg_port"

  if [ -n "$cfg_token" ]; then
    echo "配置 Token：已设置"
  else
    echo "配置 Token：未设置"
  fi

  if repo_ready; then
    echo "程序文件：完整"
  else
    echo "程序文件：不完整"
  fi

  if is_running_pid "$pid"; then
    echo "运行状态：运行中"
    echo "PID：$pid"
  else
    found="$(find_running_pids | tr '\n' ' ')"
    if [ -n "$found" ]; then
      echo "运行状态：运行中"
      echo "PID：$found"
    else
      echo "运行状态：未运行"
    fi
  fi

  echo "===================================================="
}

tail_log() {
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  touch "$DAEMON_LOG"
  say "日志文件：$DAEMON_LOG"
  tail -n 200 -f "$DAEMON_LOG"
}

update_repo() {
  ensure_git

  if [ ! -d "$BASE_DIR/.git" ]; then
    say "当前目录不是 git 仓库，将重新拉取覆盖程序文件"
    clone_repo_to_dir "$BASE_DIR" || {
      err "更新失败"
      exit 1
    }
    say "更新完成"
    return 0
  fi

  say "准备更新 FLS 仓库..."
  cd "$BASE_DIR" || exit 1

  git fetch --all --prune || {
    err "git fetch 失败"
    exit 1
  }

  git checkout "$REPO_BRANCH" >/dev/null 2>&1 || true

  git pull --ff-only origin "$REPO_BRANCH" || {
    err "git pull --ff-only 失败，尝试普通 pull"
    git pull origin "$REPO_BRANCH" || exit 1
  }

  say "更新完成"
  say "如果面板正在运行，请执行：sh fls.sh restart"
}

bstart_fls() {
  ensure_repo

  if is_termux; then
    boot_dir="$HOME/.termux/boot"
    boot_file="$boot_dir/fls-start.sh"

    mkdir -p "$boot_dir" 2>/dev/null || true

    cat > "$boot_file" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
cd "$BASE_DIR" || exit 1
sh "$BASE_DIR/fls.sh" start
EOF

    chmod 755 "$boot_file" 2>/dev/null || true

    say "已生成 Termux:Boot 自启脚本：$boot_file"
    say "请确认已安装 Termux:Boot，并允许后台启动"

    restart_fls "$@"
    return 0
  fi

  if command -v systemctl >/dev/null 2>&1 && [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
    service_file="/etc/systemd/system/fls.service"

    cat > "$service_file" <<EOF
[Unit]
Description=FLS Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
WorkingDirectory=$BASE_DIR
PIDFile=$PID_FILE
ExecStart=/bin/sh $BASE_DIR/fls.sh start
ExecStop=/bin/sh $BASE_DIR/fls.sh stop
ExecReload=/bin/sh $BASE_DIR/fls.sh restart
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    chmod 644 "$service_file" 2>/dev/null || true
    systemctl daemon-reload
    systemctl enable fls.service
    systemctl restart fls.service

    say "已生成并启用 systemd 自启：$service_file"
    say "已重启 FLS 服务"
    return 0
  fi

  err "当前环境不支持自动生成 systemd 自启"
  err "如果是 Linux，请使用 root 执行：sudo sh fls.sh bstart"
  err "如果是 Termux，请安装 Termux:Boot"
  exit 1
}

rstart_fls() {
  if is_termux; then
    boot_file="$HOME/.termux/boot/fls-start.sh"
    rm -f "$boot_file" 2>/dev/null || true
    say "已移除 Termux:Boot 自启脚本：$boot_file"

    restart_fls "$@"
    return 0
  fi

  if command -v systemctl >/dev/null 2>&1 && [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
    systemctl disable fls.service >/dev/null 2>&1 || true
    systemctl stop fls.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/fls.service 2>/dev/null || true
    systemctl daemon-reload >/dev/null 2>&1 || true

    say "已移除 systemd 自启：fls.service"

    restart_fls "$@"
    return 0
  fi

  say "未检测到可移除的自启配置，直接重启 FLS"
  restart_fls "$@"
}

usage() {
  cat <<EOF
FLS Manager

命令：
  start [-p 端口] [-t Token]   启动
  stop                         停止
  restart [-p 端口] [-t Token] 重启
  status                       状态
  log                          日志
  update                       git pull 更新
  clone                        强制重新 clone 覆盖程序文件
  bstart                       生成自启并重启
  rstart                       移除自启并重启
  ensure-repo                  检查/拉取程序

示例：
  sh fls.sh start
  sh fls.sh restart -p 5701 -t 123456
  sh fls.sh update
  sh fls.sh clone
  sh fls.sh bstart
  sh fls.sh rstart
EOF
}

cmd="${1:-help}"
shift 2>/dev/null || true

case "$cmd" in
  start)
    start_fls "$@"
    ;;
  stop)
    stop_fls
    ;;
  restart)
    restart_fls "$@"
    ;;
  status)
    status_fls
    ;;
  log|logs)
    tail_log
    ;;
  update|upgrade|pull)
    update_repo
    ;;
  clone)
    force_clone_repo
    ;;
  bstart|boot-start|enable-autostart)
    bstart_fls "$@"
    ;;
  rstart|remove-start|disable-autostart)
    rstart_fls "$@"
    ;;
  ensure-repo|install)
    ensure_repo
    say "FLS 仓库已就绪：$BASE_DIR"
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    err "未知命令：$cmd"
    usage
    exit 1
    ;;
esac