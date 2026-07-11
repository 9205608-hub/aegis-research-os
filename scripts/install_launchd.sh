#!/usr/bin/env bash
# 安装 Aegis 监控扫描 LaunchAgent（com.aegis.scan）到当前用户。
#
# 做三件事：
#   1. 把 configs/launchd/com.aegis.scan.plist 里的 __PROJECT_ROOT__ 占位符
#      替换为真实项目根，写到 ~/Library/LaunchAgents/com.aegis.scan.plist；
#   2. 先 unload（忽略「未加载」报错）再 load，保证可重复执行（幂等重装）；
#   3. 打印中文成功提示 + 手动触发 / 卸载指令。
#
# 用法：bash scripts/install_launchd.sh

set -e

# 项目根 = 本脚本所在目录（scripts/）的上一级。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SRC_PLIST="${PROJECT_ROOT}/configs/launchd/com.aegis.scan.plist"
DEST_DIR="${HOME}/Library/LaunchAgents"
DEST_PLIST="${DEST_DIR}/com.aegis.scan.plist"
LABEL="com.aegis.scan"

if [ ! -f "${SRC_PLIST}" ]; then
    echo "[错误] 找不到 plist 模板：${SRC_PLIST}" >&2
    exit 1
fi

# 日志目录（plist 里 StandardOutPath/ErrorPath 指向这里）。
mkdir -p "${PROJECT_ROOT}/logs"
mkdir -p "${DEST_DIR}"

# 把 __PROJECT_ROOT__ 占位符替换成真实项目根，写到 LaunchAgents。
# 审查发现 #8 附注：先转义替换串里 sed 会特殊解释的字符（& \ 及分隔符 |），
# 兼容含这些字符的项目路径，避免替换出错生成坏 plist。
ESC_ROOT="$(printf '%s' "${PROJECT_ROOT}" | sed -e 's/[&\\|]/\\&/g')"
sed "s|__PROJECT_ROOT__|${ESC_ROOT}|g" "${SRC_PLIST}" > "${DEST_PLIST}"

# 先 unload（若未加载会报错，忽略之）再 load，保证可重复执行。
launchctl unload "${DEST_PLIST}" 2>/dev/null || true
launchctl load "${DEST_PLIST}"

echo "[成功] 已安装 LaunchAgent：${DEST_PLIST}"
echo "       项目根：${PROJECT_ROOT}"
echo "       计划：每日 16:30（A 股盘后）自动扫描 + 到期复盘；睡眠错过则唤醒后补跑。"
echo ""
echo "常用命令："
echo "  立即手动触发一次：  launchctl start ${LABEL}"
echo "  查看运行日志：      tail -f ${PROJECT_ROOT}/logs/scan.out ${PROJECT_ROOT}/logs/scan.err"
echo "  卸载（停止调度）：  launchctl unload ${DEST_PLIST} && rm ${DEST_PLIST}"
