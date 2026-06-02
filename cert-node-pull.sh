#!/usr/bin/env bash
################################################################################
# cert-node-pull.sh
# SSL 证书自动分发系统 - Node 端核心脚本（多域名版）
# 功能: 循环处理每个域名: 拉取 SHA256 → 对比 → 下载 → 双重校验 → 原子更新
#       所有域名处理完成后，统一执行一次服务重载（避免多次抖动）
#
# 部署路径: /usr/local/bin/cert-node-pull.sh
# 配置加载: /etc/default/cert-node
# 权限:     chmod 750 /usr/local/bin/cert-node-pull.sh
################################################################################

set -euo pipefail
IFS=$'\n\t'

# systemd 的默认 PATH 可能缺少 /usr/sbin 或自定义 nginx 路径，导致脚本内
# `nginx -t` 与手动登录 shell 命中的命令不同。
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:${PATH}}"

# ── 0. 加载配置 ───────────────────────────────────────────────────────────────
CONFIG_FILE="/etc/default/cert-node"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_FILE="${2:?missing config path}"
            shift 2
            ;;
        *)
            echo "[FATAL] 未知参数: $1" >&2
            exit 1
            ;;
    esac
done

if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "[FATAL] 配置文件不存在: ${CONFIG_FILE}" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "${CONFIG_FILE}"

if ! declare -p DOMAINS >/dev/null 2>&1; then
    DOMAINS=()
fi

WEBDAV_URL="${WEBDAV_URL:-}"
WEBDAV_AUTH="${WEBDAV_AUTH:-}"
MASTER_URL="${MASTER_URL:-}"
NODE_TOKEN="${NODE_TOKEN:-}"
TELEGRAM_ENABLED="${TELEGRAM_ENABLED:-1}"
TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"
CERT_BASE_DIR="${CERT_BASE_DIR:-/etc/ssl/certs/acme}"
TMP_BASE="${TMP_BASE:-/tmp/ssl_update}"
LOG_FILE="${LOG_FILE:-/var/log/cert-node-pull.log}"
SERVICE_TEST_CMD="${SERVICE_TEST_CMD:-nginx -t}"
SERVICE_RELOAD_CMD="${SERVICE_RELOAD_CMD:-systemctl reload nginx}"
NODE_NAME="${NODE_NAME:-$(hostname -s)}"

if [[ ${#DOMAINS[@]} -eq 0 ]]; then
    echo "[FATAL] DOMAINS 数组为空" >&2
    exit 1
fi

if [[ ( -z "${WEBDAV_URL}" || -z "${WEBDAV_AUTH}" ) && ( -z "${MASTER_URL}" || -z "${NODE_TOKEN}" ) ]]; then
    echo "[FATAL] 未配置可用的证书拉取来源（需要 WebDAV 或 Master API）" >&2
    exit 1
fi

MASTER_URL="${MASTER_URL%/}"
MASTER_CERT_API_BASE="${MASTER_URL}/api/node/v1/certificates"

# ── 1. 日志 ───────────────────────────────────────────────────────────────────
log() {
    local level="$1"; shift
    local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[${ts}] [${level}] [${NODE_NAME}] $*" >> "${LOG_FILE}"
    echo "[${level}] $*" >&2
}

LAST_CMD_OUTPUT=""

tail_cmd_output() {
    local output="$1"
    if [[ -z "${output}" ]]; then
        printf '无输出'
        return 0
    fi
    printf '%s\n' "${output}" | tail -n 20
}

run_service_cmd() {
    local label="$1"
    local cmd="$2"
    local output status=0

    output="$(eval "${cmd}" 2>&1)" || status=$?
    LAST_CMD_OUTPUT="${output}"

    if [[ -n "${output}" ]]; then
        while IFS= read -r line; do
            log "INFO" "[${label}] ${line}"
        done <<< "${output}"
    fi

    if [[ ${status} -ne 0 ]]; then
        log "ERROR" "${label}失败 (exit ${status})"
        return "${status}"
    fi
    return 0
}

# ── 2. Telegram 告警 ──────────────────────────────────────────────────────────
html_escape() {
    local s="$1"
    s="${s//&/&amp;}"
    s="${s//</&lt;}"
    s="${s//>/&gt;}"
    printf '%s' "${s}"
}

send_tg_msg() {
    local title="$1"; local body="${2:-}"

    if [[ "${TELEGRAM_ENABLED}" != "1" ]]; then
        log "INFO" "TG 已由 Master 接管，跳过节点直发: ${title}"
        return 0
    fi

    if [[ -z "${TG_BOT_TOKEN}" || -z "${TG_CHAT_ID}" ]]; then
        log "INFO" "TG 未配置，跳过通知: ${title}"
        return 0
    fi

    # 自动获取公网 IP 并追加到 body（带缓存避免频繁请求）
    if [[ -z "${NODE_PUBLIC_IP:-}" ]]; then
        NODE_PUBLIC_IP="$(curl -s -4 --max-time 2 ifconfig.me 2>/dev/null || echo '未知IP')"
    fi
    local ip_info="[角色: Node] [IP: ${NODE_PUBLIC_IP}]"
    if [[ -n "${body}" ]]; then
        body="${body}"$'\n\n'"${ip_info}"
    else
        body="${ip_info}"
    fi

    local safe_title safe_body text
    safe_title="$(html_escape "${title}")"
    safe_body="$(html_escape "${body}")"
    text="$(printf '<b>%s</b>\n<pre>%s</pre>' "${safe_title}" "${safe_body}")"
    local http_code
    # 全部参数用 --data-urlencode，避免混用导致 HTTP 400
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        --max-time 15 \
        "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT_ID}" \
        --data-urlencode "parse_mode=HTML" \
        --data-urlencode "text=${text}" 2>/dev/null) || true
    log "INFO" "TG → ${title} (HTTP ${http_code:-err})"
}

# ── 3. 下载单个文件（WebDAV GET）─────────────────────────────────────────────
fetch_file_via_webdav() {
    local remote_subpath="$1"; local local_path="$2"
    local http_code
    http_code=$(curl -s -L -o "${local_path}" -w "%{http_code}" \
        --max-time 60 --retry 3 --retry-delay 5 --retry-connrefused \
        -u "${WEBDAV_AUTH}" "${WEBDAV_URL}/${remote_subpath}")
    if [[ "${http_code}" == "200" ]]; then
        log "INFO" "✅ 下载成功: ${remote_subpath}"; return 0
    else
        log "ERROR" "❌ 下载失败 (HTTP ${http_code}): ${remote_subpath}"; return 1
    fi
}

fetch_sha_via_master() {
    local domain="$1"; local local_path="$2"
    local http_code
    http_code="$(curl -sS -L -o "${local_path}" -w "%{http_code}" \
        --max-time 60 --retry 3 --retry-delay 5 --retry-connrefused \
        -H "Authorization: Bearer ${NODE_TOKEN}" \
        "${MASTER_CERT_API_BASE}/${domain}/sha256")"
    if [[ "${http_code}" == "200" ]]; then
        log "INFO" "✅ 通过 Master API 获取 SHA256 成功: ${domain}"
        return 0
    fi
    log "ERROR" "❌ 通过 Master API 获取 SHA256 失败 (HTTP ${http_code}): ${domain}"
    return 1
}

fetch_bundle_via_master() {
    local domain="$1"; local tmp_dir="$2"
    local bundle_path="${tmp_dir}/bundle.tar.gz"
    local http_code
    http_code="$(curl -sS -L -o "${bundle_path}" -w "%{http_code}" \
        --max-time 120 --retry 3 --retry-delay 5 --retry-connrefused \
        -H "Authorization: Bearer ${NODE_TOKEN}" \
        "${MASTER_CERT_API_BASE}/${domain}/bundle")"
    if [[ "${http_code}" != "200" ]]; then
        log "ERROR" "❌ 通过 Master API 获取证书包失败 (HTTP ${http_code}): ${domain}"
        return 1
    fi
    if ! tar -xzf "${bundle_path}" -C "${tmp_dir}"; then
        log "ERROR" "❌ Master 证书包解压失败: ${domain}"
        return 1
    fi
    if [[ ! -f "${tmp_dir}/fullchain.pem" || ! -f "${tmp_dir}/privkey.pem" ]]; then
        log "ERROR" "❌ Master 证书包内容不完整: ${domain}"
        return 1
    fi
    log "INFO" "✅ 通过 Master API 获取证书包成功: ${domain}"
    return 0
}

fetch_remote_sha256() {
    local domain="$1"; local local_path="$2"

    if [[ -n "${WEBDAV_URL}" && -n "${WEBDAV_AUTH}" ]]; then
        if fetch_file_via_webdav "${domain}/${domain}.sha256" "${local_path}"; then
            return 0
        fi
        if [[ -n "${MASTER_URL}" && -n "${NODE_TOKEN}" ]]; then
            log "WARN" "[${domain}] WebDAV SHA256 拉取失败，回退到 Master API"
        fi
    fi

    if [[ -n "${MASTER_URL}" && -n "${NODE_TOKEN}" ]]; then
        fetch_sha_via_master "${domain}" "${local_path}"
        return $?
    fi
    return 1
}

fetch_cert_files() {
    local domain="$1"; local tmp_dir="$2"

    if [[ -n "${WEBDAV_URL}" && -n "${WEBDAV_AUTH}" ]]; then
        local failed=0
        fetch_file_via_webdav "${domain}/${domain}.cer" "${tmp_dir}/fullchain.pem" || failed=1
        fetch_file_via_webdav "${domain}/${domain}.key" "${tmp_dir}/privkey.pem" || failed=1
        if [[ ${failed} -eq 0 ]]; then
            return 0
        fi
        rm -f "${tmp_dir}/fullchain.pem" "${tmp_dir}/privkey.pem"
        if [[ -n "${MASTER_URL}" && -n "${NODE_TOKEN}" ]]; then
            log "WARN" "[${domain}] WebDAV 证书下载失败，回退到 Master API"
        fi
    fi

    if [[ -n "${MASTER_URL}" && -n "${NODE_TOKEN}" ]]; then
        fetch_bundle_via_master "${domain}" "${tmp_dir}"
        return $?
    fi
    return 1
}

# ── 4. 清理单个域名的临时文件 ─────────────────────────────────────────────────
cleanup_tmp() {
    local tmp_dir="$1"
    find "${tmp_dir}" -type f -exec shred -u {} \; 2>/dev/null || \
        rm -f "${tmp_dir}"/*.pem "${tmp_dir}"/*.sha256 2>/dev/null || true
}

# ── 5. 处理单个域名 ───────────────────────────────────────────────────────────
# 返回值:
#   0 = 已更新（需要 reload）
#   2 = 无变化（SHA256 一致，跳过）
#   1 = 错误
process_domain() {
    local domain="$1"
    local cert_dir="${CERT_BASE_DIR}/${domain}"
    local tmp_dir="${TMP_BASE}/${domain}"
    local sha256_file="${cert_dir}/cert.sha256"
    local key_file="${cert_dir}/${domain}.key"
    local chain_file="${cert_dir}/${domain}.cer"

    install -d -m 700 "${tmp_dir}"
    log "INFO" "---- 检查域名: *.${domain} ----"

    # 5a. 预检：拉取远程 SHA256
    local remote_sha256_tmp="${tmp_dir}/remote.sha256"
    if ! fetch_remote_sha256 "${domain}" "${remote_sha256_tmp}"; then
        send_tg_msg "🚨 [ERROR] [${NODE_NAME}] 无法获取 SHA256: ${domain}" \
            "请检查 WebDAV 或 Master API 拉取通道可用性"
        cleanup_tmp "${tmp_dir}"
        return 1
    fi
    local remote_sha256; remote_sha256="$(tr -d '[:space:]' < "${remote_sha256_tmp}")"

    # 5b. 对比本地 SHA256（一致则静默跳过）
    if [[ -f "${sha256_file}" ]]; then
        local local_sha256; local_sha256="$(tr -d '[:space:]' < "${sha256_file}")"
        if [[ "${remote_sha256}" == "${local_sha256}" ]]; then
            log "INFO" "[${domain}] SHA256 一致，无需更新"
            cleanup_tmp "${tmp_dir}"
            return 2  # 特殊返回值：跳过
        fi
        log "INFO" "[${domain}] SHA256 变化，触发更新"
    else
        log "INFO" "[${domain}] 首次部署"
    fi

    # 5c. 下载证书文件
    if ! fetch_cert_files "${domain}" "${tmp_dir}"; then
        send_tg_msg "🚨 [ERROR] [${NODE_NAME}] 证书下载失败: ${domain}" \
            "请检查 WebDAV 或 Master API 拉取通道可用性"
        cleanup_tmp "${tmp_dir}"; return 1
    fi

    # 5d. 完整性校验（SHA256）
    local actual_sha256; actual_sha256="$(sha256sum "${tmp_dir}/fullchain.pem" | awk '{print $1}')"
    if [[ "${actual_sha256}" != "${remote_sha256}" ]]; then
        log "ERROR" "[${domain}] SHA256 校验失败 期望:${remote_sha256} 实际:${actual_sha256}"
        send_tg_msg "🚨 [ERROR] [${NODE_NAME}] SHA256 校验失败: ${domain}" \
            "期望: ${remote_sha256}"$'\n'"实际: ${actual_sha256}"$'\n'"已中止"
        cleanup_tmp "${tmp_dir}"; return 1
    fi
    log "INFO" "[${domain}] ✅ SHA256 完整性校验通过"

    # 5e. 一致性校验（公钥比对，兼容 RSA 和 ECC/ECDSA）
    # 注意: openssl pkey 不加 -noout，否则 -noout 会抑制 -pubout 的输出
    local key_pub cert_pub
    key_pub="$(openssl pkey -pubout -in "${tmp_dir}/privkey.pem"    2>/dev/null | sha256sum | awk '{print $1}')"
    cert_pub="$(openssl x509 -pubkey -noout -in "${tmp_dir}/fullchain.pem" 2>/dev/null | sha256sum | awk '{print $1}')"
    if [[ -z "${key_pub}" ]] || [[ "${key_pub}" != "${cert_pub}" ]]; then
        log "ERROR" "[${domain}] 私钥与证书公钥不匹配 (key=${key_pub:0:12}... cert=${cert_pub:0:12}...)"
        send_tg_msg "🚨 [ERROR] [${NODE_NAME}] 证书不匹配: ${domain}" "私钥与证书公钥不匹配，已中止"
        cleanup_tmp "${tmp_dir}"; return 1
    fi
    log "INFO" "[${domain}] ✅ 私钥与证书一致性校验通过"

    local cert_expiry; cert_expiry="$(openssl x509 -noout -enddate -in "${tmp_dir}/fullchain.pem" | cut -d= -f2)"

    # 5f. 原子部署（备份 → 安装 → 记录 SHA256）
    install -d -m 700 "${cert_dir}"

    if [[ -f "${chain_file}" ]]; then
        local bak; bak="$(date '+%Y%m%d_%H%M%S')"
        cp -p "${key_file}"   "${cert_dir}/privkey.pem.bak.${bak}"   2>/dev/null || true
        cp -p "${chain_file}" "${cert_dir}/fullchain.pem.bak.${bak}" 2>/dev/null || true
        # 只保留最近 3 份备份
        find "${cert_dir}" -name "*.bak.*" | sort | head -n -6 | xargs rm -f 2>/dev/null || true
        log "INFO" "[${domain}] 旧证书已备份 (${bak})"
    fi

    install -m 600 "${tmp_dir}/privkey.pem"   "${key_file}"
    install -m 644 "${tmp_dir}/fullchain.pem" "${chain_file}"
    echo "${remote_sha256}" > "${sha256_file}"; chmod 644 "${sha256_file}"

    cleanup_tmp "${tmp_dir}"

    log "INFO" "[${domain}] ✅ 证书已部署，到期: ${cert_expiry}"

    # 把域名和到期时间传出去（通过全局数组）
    UPDATED_DOMAINS+=("${domain}")
    UPDATED_EXPIRY+=("${cert_expiry}")
    return 0
}

# ── 主流程 ────────────────────────────────────────────────────────────────────
main() {
    install -m 640 /dev/null "${LOG_FILE}" 2>/dev/null || true
    install -d -m 700 "${TMP_BASE}"

    log "INFO" "======== [${NODE_NAME}] 开始证书拉取（共 ${#DOMAINS[@]} 个域名）========"

    # 全局结果跟踪
    UPDATED_DOMAINS=()
    UPDATED_EXPIRY=()
    local failed_domains=()
    local skipped=0

    for domain in "${DOMAINS[@]}"; do
        local rc=0
        process_domain "${domain}" || rc=$?   # || 防止 set -e 在返回非零时提前退出
        case ${rc} in
            0) :;; # 已更新，记录在 UPDATED_DOMAINS
            2) (( skipped++ )) || true;;
            *) failed_domains+=("${domain}");;
        esac
    done

    # 若有更新，统一执行一次服务重载
    if [[ ${#UPDATED_DOMAINS[@]} -gt 0 ]]; then
        log "INFO" "执行服务校验: ${SERVICE_TEST_CMD}"
        if ! run_service_cmd "服务校验" "${SERVICE_TEST_CMD}"; then
            local test_output
            test_output="$(tail_cmd_output "${LAST_CMD_OUTPUT}")"
            log "ERROR" "❌ 服务配置校验失败，已更新证书但服务未重载"
            send_tg_msg "🚨 [ERROR] [${NODE_NAME}] 服务校验失败" \
                "命令: ${SERVICE_TEST_CMD}"$'\n'"退出输出:"$'\n'"${test_output}"$'\n\n'"已更新域名: $(printf '%s\n' "${UPDATED_DOMAINS[@]}")"$'\n'"请手动检查 Nginx 配置"
            exit 1
        fi

        log "INFO" "执行服务重载: ${SERVICE_RELOAD_CMD}"
        if ! run_service_cmd "服务重载" "${SERVICE_RELOAD_CMD}"; then
            local reload_output
            reload_output="$(tail_cmd_output "${LAST_CMD_OUTPUT}")"
            send_tg_msg "🚨 [ERROR] [${NODE_NAME}] 服务重载失败" \
                "命令: ${SERVICE_RELOAD_CMD}"$'\n'"退出输出:"$'\n'"${reload_output}"
            exit 1
        fi
        log "INFO" "✅ 服务重载成功"

        # 构造成功通知
        local update_list=""
        for i in "${!UPDATED_DOMAINS[@]}"; do
            update_list+="${UPDATED_DOMAINS[$i]} → 到期: ${UPDATED_EXPIRY[$i]}"$'\n'
        done

        local summary="更新: ${#UPDATED_DOMAINS[@]} 个 | 跳过: ${skipped} 个 | 失败: ${#failed_domains[@]} 个"$'\n\n'"${update_list}"
        [[ ${#failed_domains[@]} -gt 0 ]] && summary+="失败: $(printf '%s\n' "${failed_domains[@]}")"
        local icon="✅"; [[ ${#failed_domains[@]} -gt 0 ]] && icon="⚠️"
        send_tg_msg "${icon} [INFO] [${NODE_NAME}] 证书更新汇总" "${summary}"
    else
        log "INFO" "所有域名证书均无变化，无需重载服务"
        if [[ ${#failed_domains[@]} -gt 0 ]]; then
            send_tg_msg "⚠️ [WARN] [${NODE_NAME}] 部分域名拉取失败" \
                "失败: $(printf '%s\n' "${failed_domains[@]}")"
        fi
    fi

    log "INFO" "======== [${NODE_NAME}] 拉取完成 更新:${#UPDATED_DOMAINS[@]} 跳过:${skipped} 失败:${#failed_domains[@]} ========"
    [[ ${#failed_domains[@]} -eq 0 ]] || exit 1
}

trap 'log "ERROR" "脚本意外退出 (line ${LINENO})"; \
      send_tg_msg "🚨 [FATAL] [${NODE_NAME}] cert-node-pull 意外退出" "行号: ${LINENO}"' ERR

main "$@"
