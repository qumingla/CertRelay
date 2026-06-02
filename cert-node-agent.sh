#!/usr/bin/env bash
################################################################################
# cert-node-agent.sh
# API-aware wrapper for cert-node-pull.sh
# 功能:
#   - API 模式: 心跳 -> 拉取分配/命令 -> 调用 puller -> 回报状态 -> ACK 命令
#   - 兼容模式: 未配置 MASTER_URL / NODE_TOKEN 时，退回旧版 puller 直连 WebDAV
################################################################################

set -euo pipefail
IFS=$'\n\t'

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:${PATH}}"

CONFIG_FILE="/etc/default/cert-node"
PULL_SCRIPT="/usr/local/bin/cert-node-pull.sh"
TMP_ROOT="/tmp/ssl-node-agent"
NODE_API_BASE_SUFFIX="/api/node/v1"
AGENT_VERSION="2026.05.15"

usage() {
    cat >&2 <<'EOF'
Usage: cert-node-agent.sh [--config /etc/default/cert-node] [--pull-script /usr/local/bin/cert-node-pull.sh]
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_FILE="${2:?missing config path}"
            shift 2
            ;;
        --pull-script)
            PULL_SCRIPT="${2:?missing pull-script path}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[FATAL] 未知参数: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "[FATAL] 配置文件不存在: ${CONFIG_FILE}" >&2
    exit 1
fi
if [[ ! -x "${PULL_SCRIPT}" ]]; then
    echo "[FATAL] puller 脚本不存在或不可执行: ${PULL_SCRIPT}" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "${CONFIG_FILE}"
if ! declare -p DOMAINS >/dev/null 2>&1; then
    DOMAINS=()
fi

MASTER_URL="${MASTER_URL:-}"
NODE_TOKEN="${NODE_TOKEN:-}"
WEBDAV_URL="${WEBDAV_URL:-}"
WEBDAV_AUTH="${WEBDAV_AUTH:-}"
TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"
CERT_BASE_DIR="${CERT_BASE_DIR:-/etc/ssl/certs/acme}"
TMP_BASE="${TMP_BASE:-/tmp/ssl_update}"
LOG_FILE="${LOG_FILE:-/var/log/cert-node-pull.log}"
SERVICE_TEST_CMD="${SERVICE_TEST_CMD:-nginx -t}"
SERVICE_RELOAD_CMD="${SERVICE_RELOAD_CMD:-systemctl reload nginx}"
NODE_NAME="${NODE_NAME:-$(hostname -s)}"

mkdir -p "$(dirname "${LOG_FILE}")"

log() {
    local level="$1"; shift
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[${ts}] [${level}] [${NODE_NAME}] [agent] $*" | tee -a "${LOG_FILE}" >&2
}

shell_quote() {
    local value="$1"
    value="${value//\'/\'\"\'\"\'}"
    printf "'%s'" "${value}"
}

if [[ -z "${MASTER_URL}" || -z "${NODE_TOKEN}" ]]; then
    log "INFO" "未检测到 MASTER_URL/NODE_TOKEN，退回传统 WebDAV 模式"
    exec "${PULL_SCRIPT}" --config "${CONFIG_FILE}"
fi

MASTER_URL="${MASTER_URL%/}"
NODE_API_BASE="${MASTER_URL}${NODE_API_BASE_SUFFIX}"
STATE_DIR="$(mktemp -d "${TMP_ROOT}.XXXXXX")"
ASSIGNMENTS_JSON="${STATE_DIR}/assignments.json"
ASSIGNMENTS_TSV="${STATE_DIR}/assignments.tsv"
COMMANDS_JSON="${STATE_DIR}/commands.json"
REPORT_JSON="${STATE_DIR}/report.json"
RUNTIME_CONFIG="${STATE_DIR}/cert-node-runtime.conf"

cleanup() {
    rm -rf "${STATE_DIR}"
}
trap cleanup EXIT

curl_node_api() {
    local method="$1"
    local path="$2"
    local body="${3:-}"
    local output_file="${4:?missing output file}"
    local http_code
    if [[ -n "${body}" ]]; then
        http_code="$(curl -sS -o "${output_file}" -w "%{http_code}" \
            -X "${method}" \
            -H "Authorization: Bearer ${NODE_TOKEN}" \
            -H "Content-Type: application/json" \
            --data "${body}" \
            "${NODE_API_BASE}${path}")"
    else
        http_code="$(curl -sS -o "${output_file}" -w "%{http_code}" \
            -X "${method}" \
            -H "Authorization: Bearer ${NODE_TOKEN}" \
            "${NODE_API_BASE}${path}")"
    fi

    if [[ "${http_code}" -lt 200 || "${http_code}" -ge 300 ]]; then
        local detail=""
        if [[ -s "${output_file}" ]]; then
            detail="$(tr '\n' ' ' < "${output_file}")"
        fi
        log "ERROR" "Node API ${method} ${path} 失败 (HTTP ${http_code}) ${detail}"
        return 1
    fi
}

node_heartbeat() {
    local body
    body="$(python3 - "${AGENT_VERSION}" "${CERT_BASE_DIR}" <<'PY'
import json, socket, sys
print(json.dumps({
    "hostname": socket.gethostname(),
    "ip": "",
    "version": sys.argv[1],
    "certDir": sys.argv[2],
}, ensure_ascii=False))
PY
)"
    curl_node_api "POST" "/heartbeat" "${body}" "${STATE_DIR}/heartbeat.json"
}

load_assignments() {
    curl_node_api "GET" "/assignments" "" "${ASSIGNMENTS_JSON}"
    python3 - "${ASSIGNMENTS_JSON}" "${ASSIGNMENTS_TSV}" <<'PY'
import json, sys
source = json.load(open(sys.argv[1], encoding="utf-8"))
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    for item in source.get("assignments", []):
        fields = [
            item.get("domainId") or "",
            item.get("domainName") or "",
            item.get("desiredSha256") or "",
            item.get("expiresAt") or "",
        ]
        fh.write("\t".join(fields) + "\n")
PY
    WEBDAV_URL="$(python3 - "${ASSIGNMENTS_JSON}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print((data.get("webdav") or {}).get("url") or "")
PY
)"
    WEBDAV_AUTH="$(python3 - "${ASSIGNMENTS_JSON}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print((data.get("webdav") or {}).get("auth") or "")
PY
)"

    DOMAINS=()
    declare -gA DOMAIN_IDS=()
    declare -gA DOMAIN_NAMES_BY_ID=()
    ALL_DOMAIN_IDS_CSV=""
    while IFS=$'\t' read -r domain_id domain_name desired_sha _expires_at; do
        [[ -n "${domain_name}" ]] || continue
        DOMAINS+=("${domain_name}")
        DOMAIN_IDS["${domain_name}"]="${domain_id}"
        DOMAIN_NAMES_BY_ID["${domain_id}"]="${domain_name}"
        if [[ -n "${ALL_DOMAIN_IDS_CSV}" ]]; then
            ALL_DOMAIN_IDS_CSV+=","
        fi
        ALL_DOMAIN_IDS_CSV+="${domain_id}"
    done < "${ASSIGNMENTS_TSV}"
}

load_commands() {
    curl_node_api "GET" "/commands" "" "${COMMANDS_JSON}"
    mapfile -t COMMAND_ROWS < <(python3 - "${COMMANDS_JSON}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for item in data.get("commands", []):
    payload = item.get("payload") or {}
    domain_ids = payload.get("domainIds") or []
    print("\t".join([
        item.get("id") or "",
        item.get("type") or "",
        ",".join(domain_ids),
    ]))
PY
)
}

resolve_domain_names_from_ids() {
    local domain_ids_csv="$1"
    local result=()
    if [[ -z "${domain_ids_csv}" ]]; then
        return 0
    fi

    local domain_id
    IFS=',' read -r -a _domain_ids <<< "${domain_ids_csv}"
    for domain_id in "${_domain_ids[@]}"; do
        [[ -n "${domain_id}" ]] || continue
        if [[ -n "${DOMAIN_NAMES_BY_ID[${domain_id}]:-}" ]]; then
            result+=("${DOMAIN_NAMES_BY_ID[${domain_id}]}")
        fi
    done

    printf '%s\n' "${result[@]}"
}

write_runtime_config_for_domains() {
    local domains=("$@")
    if [[ -z "${WEBDAV_URL}" || -z "${WEBDAV_AUTH}" ]]; then
        log "ERROR" "Master 未返回 WebDAV 配置，无法执行同步"
        return 1
    fi

    {
        printf "DOMAINS=("
        local domain
        for domain in "${domains[@]}"; do
            printf "%s " "$(shell_quote "${domain}")"
        done
        printf ")\n"
        printf "WEBDAV_URL=%s\n" "$(shell_quote "${WEBDAV_URL}")"
        printf "WEBDAV_AUTH=%s\n" "$(shell_quote "${WEBDAV_AUTH}")"
        printf "MASTER_URL=%s\n" "$(shell_quote "${MASTER_URL}")"
        printf "NODE_TOKEN=%s\n" "$(shell_quote "${NODE_TOKEN}")"
        printf "CERT_BASE_DIR=%s\n" "$(shell_quote "${CERT_BASE_DIR}")"
        printf "TMP_BASE=%s\n" "$(shell_quote "${TMP_BASE}")"
        printf "TELEGRAM_ENABLED='0'\n"
        printf "TG_BOT_TOKEN=%s\n" "$(shell_quote "${TG_BOT_TOKEN}")"
        printf "TG_CHAT_ID=%s\n" "$(shell_quote "${TG_CHAT_ID}")"
        printf "SERVICE_TEST_CMD=%s\n" "$(shell_quote "${SERVICE_TEST_CMD}")"
        printf "SERVICE_RELOAD_CMD=%s\n" "$(shell_quote "${SERVICE_RELOAD_CMD}")"
        printf "LOG_FILE=%s\n" "$(shell_quote "${LOG_FILE}")"
        printf "NODE_NAME=%s\n" "$(shell_quote "${NODE_NAME}")"
    } > "${RUNTIME_CONFIG}"
    chmod 600 "${RUNTIME_CONFIG}"
}

run_logged_cmd() {
    local label="$1"
    local cmd="$2"
    local output
    local status=0

    output="$(eval "${cmd}" 2>&1)" || status=$?
    if [[ -n "${output}" ]]; then
        while IFS= read -r line; do
            log "INFO" "[${label}] ${line}"
        done <<< "${output}"
    fi
    return "${status}"
}

delete_certificates_for_domains() {
    local domains=("$@")
    local changed=0
    local domain
    for domain in "${domains[@]}"; do
        local cert_dir="${CERT_BASE_DIR}/${domain}"
        local key_file="${cert_dir}/${domain}.key"
        local chain_file="${cert_dir}/${domain}.cer"
        local sha_file="${cert_dir}/cert.sha256"

        if [[ -f "${key_file}" || -f "${chain_file}" || -f "${sha_file}" ]]; then
            rm -f "${key_file}" "${chain_file}" "${sha_file}"
            find "${cert_dir}" -maxdepth 1 -name "*.bak.*" -delete 2>/dev/null || true
            rmdir "${cert_dir}" 2>/dev/null || true
            changed=1
            log "INFO" "已删除节点本地证书: ${domain}"
        else
            log "INFO" "节点本地不存在证书，跳过删除: ${domain}"
        fi
    done

    if [[ "${changed}" -eq 0 ]]; then
        return 0
    fi

    log "INFO" "执行服务校验: ${SERVICE_TEST_CMD}"
    if ! run_logged_cmd "服务校验" "${SERVICE_TEST_CMD}"; then
        log "ERROR" "删除证书后服务校验失败"
        return 1
    fi

    log "INFO" "执行服务重载: ${SERVICE_RELOAD_CMD}"
    if ! run_logged_cmd "服务重载" "${SERVICE_RELOAD_CMD}"; then
        log "ERROR" "删除证书后服务重载失败"
        return 1
    fi
    return 0
}

build_report_for_domain_ids() {
    local domain_ids_csv="$1"
    local mode="$2"
    local command_exit_code="$3"
    python3 - "${ASSIGNMENTS_TSV}" "${CERT_BASE_DIR}" "${domain_ids_csv}" "${mode}" "${command_exit_code}" > "${REPORT_JSON}" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

assignments_path = Path(sys.argv[1])
cert_base_dir = Path(sys.argv[2])
selected_ids = {item for item in sys.argv[3].split(",") if item}
mode = sys.argv[4]
command_exit_code = int(sys.argv[5])

items = []
for raw_line in assignments_path.read_text(encoding="utf-8").splitlines():
    if not raw_line.strip():
        continue
    domain_id, domain_name, desired_sha, _expires_at = (raw_line.split("\t") + ["", "", "", ""])[:4]
    if selected_ids and domain_id not in selected_ids:
        continue
    cert_dir = cert_base_dir / domain_name
    sha_file = cert_dir / "cert.sha256"
    chain_file = cert_dir / f"{domain_name}.cer"

    deployed_sha = sha_file.read_text(encoding="utf-8").strip() if sha_file.exists() else None
    local_expiry = None
    if chain_file.exists():
        completed = subprocess.run(
            ["openssl", "x509", "-noout", "-enddate", "-in", str(chain_file)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0 and "=" in completed.stdout:
            raw_expiry = completed.stdout.strip().split("=", 1)[1].strip()
            local_expiry = datetime.strptime(raw_expiry, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    if mode == "delete":
        if deployed_sha:
            status = "synced"
        else:
            status = "pending"
        last_error = None
    else:
        if deployed_sha and (not desired_sha or deployed_sha == desired_sha):
            status = "synced"
            last_error = None
        elif deployed_sha:
            status = "error" if command_exit_code != 0 else "pending"
            last_error = "Desired certificate SHA is not deployed on this node yet."
        else:
            status = "error"
            last_error = "Certificate files are missing locally."

    items.append(
        {
            "domainId": domain_id,
            "domainName": domain_name,
            "deployedSha256": deployed_sha,
            "status": status,
            "expiresAt": local_expiry,
            "lastError": last_error,
        }
    )

print(json.dumps({"items": items}, ensure_ascii=False))
PY
}

submit_report() {
    curl_node_api "POST" "/reports" "$(cat "${REPORT_JSON}")" "${STATE_DIR}/report-response.json"
}

ack_command_single() {
    local command_id="$1"
    local ack_status="$2"
    local ack_error="$3"
    local ack_summary="${4:-}"
    local payload
    payload="$(python3 - "${ack_status}" "${ack_error}" "${ack_summary}" <<'PY'
import json
import sys
print(json.dumps({"status": sys.argv[1], "error": sys.argv[2] or None, "summary": sys.argv[3] or None}, ensure_ascii=False))
PY
)"
    curl_node_api "POST" "/commands/${command_id}/ack" "${payload}" "${STATE_DIR}/ack-${command_id}.json"
}

build_ack_summary() {
    local mode="$1"
    local command_exit_code="$2"
    python3 - "${REPORT_JSON}" "${NODE_NAME}" "${mode}" "${command_exit_code}" <<'PY'
import json
import sys

report_path = sys.argv[1]
node_name = sys.argv[2]
mode = sys.argv[3]
command_exit_code = int(sys.argv[4])

data = json.load(open(report_path, encoding="utf-8"))
items = data.get("items", [])

def item_name(item: dict[str, object]) -> str:
    return str(item.get("domainName") or item.get("domainId") or "unknown")

if mode == "delete":
    names = [item_name(item) for item in items]
    error_items = [item for item in items if item.get("lastError")]
    status = "failed" if command_exit_code != 0 or error_items else "completed"
    if status == "completed":
        summary = f"节点 {node_name} 已删除 {len(names)} 个域名的本地证书: {', '.join(names)}"
        error = None
    else:
        failed_parts = [
            f"{item_name(item)} ({item.get('lastError') or '删除失败'})"
            for item in error_items
        ] or [", ".join(names)]
        summary = f"节点 {node_name} 删除本地证书失败。\n目标域名: {', '.join(names)}\n失败详情: {'; '.join(failed_parts)}"
        error = str(error_items[0].get("lastError") or "Node certificate deletion failed.")
else:
    synced = [item_name(item) for item in items if item.get("status") == "synced"]
    pending = [item_name(item) for item in items if item.get("status") == "pending"]
    errors = [item for item in items if item.get("status") == "error"]
    total = len(items)
    status = "failed" if command_exit_code != 0 or errors else "completed"
    lines = [f"节点 {node_name} 证书下发完成：成功 {len(synced)} / {total}。"]
    if synced:
        lines.append(f"已同步: {', '.join(synced)}")
    if pending:
        lines.append(f"待同步: {', '.join(pending)}")
    if errors:
        failed_parts = [
            f"{item_name(item)} ({item.get('lastError') or '同步失败'})"
            for item in errors
        ]
        lines.append(f"失败: {'; '.join(failed_parts)}")
        error = str(errors[0].get("lastError") or "Node synchronization failed.")
    elif command_exit_code != 0:
        lines.append("执行失败，请检查节点日志。")
        error = "Node synchronization failed."
    else:
        error = None
    summary = "\n".join(lines)

print(json.dumps({"status": status, "error": error, "summary": summary}, ensure_ascii=False))
PY
}

process_sync_command() {
    local command_id="$1"
    local domain_ids_csv="$2"
    shift 2
    local domains=("$@")
    local command_exit_code=0

    log "INFO" "执行下发命令 ${command_id}: ${domains[*]}"
    write_runtime_config_for_domains "${domains[@]}"
    "${PULL_SCRIPT}" --config "${RUNTIME_CONFIG}" || command_exit_code=$?
    build_report_for_domain_ids "${domain_ids_csv}" "sync" "${command_exit_code}"
    submit_report
    local ack_payload ack_status ack_error ack_summary
    ack_payload="$(build_ack_summary "sync" "${command_exit_code}")"
    ack_status="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("status","completed"))' <<< "${ack_payload}")"
    ack_error="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("error") or "")' <<< "${ack_payload}")"
    ack_summary="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("summary") or "")' <<< "${ack_payload}")"
    ack_command_single "${command_id}" "${ack_status}" "${ack_error}" "${ack_summary}"
    [[ "${ack_status}" == "completed" ]]
}

process_delete_command() {
    local command_id="$1"
    local domain_ids_csv="$2"
    shift 2
    local domains=("$@")
    local command_exit_code=0

    log "INFO" "执行删除命令 ${command_id}: ${domains[*]}"
    delete_certificates_for_domains "${domains[@]}" || command_exit_code=$?
    build_report_for_domain_ids "${domain_ids_csv}" "delete" "${command_exit_code}"
    submit_report
    local ack_payload ack_status ack_error ack_summary
    ack_payload="$(build_ack_summary "delete" "${command_exit_code}")"
    ack_status="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("status","completed"))' <<< "${ack_payload}")"
    ack_error="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("error") or "")' <<< "${ack_payload}")"
    ack_summary="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("summary") or "")' <<< "${ack_payload}")"
    ack_command_single "${command_id}" "${ack_status}" "${ack_error}" "${ack_summary}"
    [[ "${ack_status}" == "completed" ]]
}

main() {
    log "INFO" "开始 API 模式节点同步"
    node_heartbeat
    load_assignments
    load_commands

    if [[ ${#COMMAND_ROWS[@]} -eq 0 ]]; then
        if [[ ${#DOMAINS[@]} -eq 0 ]]; then
            log "INFO" "当前节点没有分配任何域名"
        else
            log "INFO" "当前没有待执行命令"
        fi
        return 0
    fi

    local overall_exit_code=0
    local row
    for row in "${COMMAND_ROWS[@]}"; do
        local command_id command_type domain_ids_csv
        IFS=$'\t' read -r command_id command_type domain_ids_csv <<< "${row}"
        [[ -n "${command_id}" ]] || continue

        local selected_domain_ids_csv="${domain_ids_csv}"
        if [[ "${command_type}" == "sync_all" ]]; then
            selected_domain_ids_csv="${ALL_DOMAIN_IDS_CSV}"
        fi

        mapfile -t selected_domains < <(resolve_domain_names_from_ids "${selected_domain_ids_csv}")
        if [[ ${#selected_domains[@]} -eq 0 ]]; then
            ack_command_single "${command_id}" "failed" "No matching assigned domains found on this node."
            overall_exit_code=1
            continue
        fi

        case "${command_type}" in
            sync_all|sync_domains)
                process_sync_command "${command_id}" "${selected_domain_ids_csv}" "${selected_domains[@]}" || overall_exit_code=1
                ;;
            delete_domains)
                process_delete_command "${command_id}" "${selected_domain_ids_csv}" "${selected_domains[@]}" || overall_exit_code=1
                ;;
            *)
                ack_command_single "${command_id}" "failed" "Unsupported node command type: ${command_type}"
                overall_exit_code=1
                ;;
        esac
    done

    if [[ "${overall_exit_code}" -ne 0 ]]; then
        log "ERROR" "节点命令执行完成，但存在失败项"
    else
        log "INFO" "节点命令执行完成"
    fi
    return "${overall_exit_code}"
}

main "$@"
