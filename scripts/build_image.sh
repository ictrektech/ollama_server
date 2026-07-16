#!/usr/bin/env bash
set -euo pipefail

# =========================
# build_image.sh
# =========================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DOCKER_DIR="${REPO_ROOT}/docker"

DEFAULT_COMPONENT_NAME="ollama_server"

# 飞书配置
FEISHU_CONFIG_FILE="${HOME}/.feishu.json"
FEISHU_SPREADSHEET_TOKEN="Htotsn3oahO1zxt73YMcaB1zn8e"

# 发布目标决定飞书 sheet 和镜像 tag 前缀，与使用哪个 Dockerfile 构建解耦。
declare -A TARGET_TO_SHEET_TITLE=(
  ["amd"]="AMD_with_cuda"
  ["arm"]="ARM_with_cuda"
  ["l4t"]="l4t"
  ["thor"]="thor_spark"
  ["amd_cu128"]="AMD_with_cuda"
  ["arm_cu128"]="ARM_with_cuda"
)

declare -A TARGET_TO_TAG_PREFIX=(
  ["amd"]="amd"
  ["arm"]="arm"
  ["l4t"]="l4t"
  ["thor"]="thor"
  ["amd_cu128"]="amd_cu128"
  ["arm_cu128"]="arm_cu128"
)

echo "Ollama server will be included in the build."
echo "Detected:"
echo "  ARCH=$(uname -m)"

# -------------------------
# 基础函数
# -------------------------

log() {
  echo "[INFO] $*"
}

err() {
  echo "[ERROR] $*" >&2
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    err "missing command: $1"
    exit 1
  }
}

usage() {
  cat <<'EOF'
Usage:
  scripts/build_image.sh --profile Dockerfile --target arm
  scripts/build_image.sh --target thor --source-image ollama_server:0.31.1
  OLLAMA_TAG=0.31.1 scripts/build_image.sh --profile Dockerfile --target arm

Options:
  --profile FILE         Dockerfile under docker/ used to build (default: Dockerfile)
  --target TARGET        Publish target: amd, arm, l4t, thor, amd_cu128, arm_cu128
  --component-name NAME  Feishu column and Huawei SWR repository (default: ollama_server)
  --sheet-title TITLE    Override the target's Feishu sheet
  --tag-prefix PREFIX    Override the target's image tag prefix
  --source-image IMAGE   Push an existing local image instead of rebuilding
  --skip-build           Push ollama_server:<OLLAMA_TAG> instead of rebuilding
  --builder BUILDER      Build backend: auto, buildx, docker (default: auto)
  --dry-run              Print the resolved publish plan without building or pushing
  -h, --help             Show this help

The pushed image and Feishu value always match:
  swr.cn-southwest-2.myhuaweicloud.com/ictrek/<component-name>:<tag-prefix>_<OLLAMA_TAG>

If OLLAMA_TAG is empty or latest, the script detects the latest Ollama release.
EOF
}

read_feishu_field() {
  local field="$1"
  python3 - "$FEISHU_CONFIG_FILE" "$field" <<'PY'
import json, sys
path, field = sys.argv[1], sys.argv[2]
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)
val = data.get(field, "")
if not isinstance(val, str):
    val = str(val)
print(val)
PY
}

json_extract_or_fail() {
  local resp="$1"
  local py="$2"
  python3 - "$resp" "$py" <<'PY'
import json, sys
resp = sys.argv[1]
code = sys.argv[2]
if not resp:
    raise SystemExit("empty response")
try:
    data = json.loads(resp)
except Exception as e:
    raise SystemExit(f"invalid json response: {resp[:500]!r}, error={e}")
ns = {"data": data}
exec(code, ns, ns)
PY
}

get_feishu_token() {
  local app_id="$1"
  local app_secret="$2"
  local resp

  resp=$(
    curl --fail -sS -X POST 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal' \
      -H 'Content-Type: application/json' \
      -d "{
        \"app_id\": \"${app_id}\",
        \"app_secret\": \"${app_secret}\"
      }"
  ) || {
    err "get_feishu_token: curl failed"
    return 1
  }

  python3 - "$resp" <<'PY'
import json, sys
resp = sys.argv[1]
if not resp:
    raise SystemExit("get_feishu_token: empty response")
try:
    data = json.loads(resp)
except Exception as e:
    raise SystemExit(f"get_feishu_token: invalid json: {resp[:500]!r}, error={e}")
if data.get("code") != 0:
    raise SystemExit(f"get_feishu_token failed: {data}")
print(data["tenant_access_token"])
PY
}

feishu_api_json() {
  local method="$1"
  local url="$2"
  local token="$3"
  local body="${4:-}"

  if [[ -n "$body" ]]; then
    curl --fail -sS -X "$method" "$url" \
      -H "Authorization: Bearer ${token}" \
      -H "Content-Type: application/json" \
      --data "$body"
  else
    curl --fail -sS -X "$method" "$url" \
      -H "Authorization: Bearer ${token}"
  fi
}

get_sheet_id_by_title() {
  local token="$1"
  local spreadsheet_token="$2"
  local target_title="$3"
  local resp

  resp=$(
    feishu_api_json "GET" \
      "https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/${spreadsheet_token}/sheets/query" \
      "$token"
  ) || {
    err "get_sheet_id_by_title: curl failed"
    return 1
  }

  python3 - "$target_title" "$resp" <<'PY'
import sys, json
target = sys.argv[1]
resp = sys.argv[2]
if not resp:
    raise SystemExit("get_sheet_id_by_title: empty response")
try:
    data = json.loads(resp)
except Exception as e:
    raise SystemExit(f"get_sheet_id_by_title invalid json: {resp[:500]!r}, error={e}")
if data.get("code") != 0:
    raise SystemExit(f"query sheets failed: {data}")
for s in data["data"]["sheets"]:
    if s.get("title") == target:
        print(s["sheet_id"])
        raise SystemExit(0)
raise SystemExit(f"sheet title not found: {target}")
PY
}

get_range_values() {
  local token="$1"
  local spreadsheet_token="$2"
  local range="$3"

  feishu_api_json "GET" \
    "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/${spreadsheet_token}/values/${range}" \
    "$token"
}

find_component_column_letter() {
  local token="$1"
  local spreadsheet_token="$2"
  local sheet_id="$3"
  local component_name="$4"
  local resp

  resp=$(get_range_values "$token" "$spreadsheet_token" "${sheet_id}!A1:AZ1") || {
    err "find_component_column_letter: read range failed"
    return 1
  }

  python3 - "$component_name" "$resp" <<'PY'
import sys, json
target = sys.argv[1]
resp = sys.argv[2]
if not resp:
    raise SystemExit("find_component_column_letter: empty response")
try:
    data = json.loads(resp)
except Exception as e:
    raise SystemExit(f"find_component_column_letter invalid json: {resp[:500]!r}, error={e}")
if data.get("code") != 0:
    raise SystemExit(f"read header failed: {data}")
values = data.get("data", {}).get("valueRange", {}).get("values", [])
row = values[0] if values else []
for i, v in enumerate(row, start=1):
    if str(v).strip() == target:
        n = i
        s = ""
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(ord("A") + r) + s
        print(s)
        raise SystemExit(0)
raise SystemExit(f"component column not found in row1: {target}")
PY
}

find_date_row() {
  local token="$1"
  local spreadsheet_token="$2"
  local sheet_id="$3"
  local target_date="$4"
  local resp

  resp=$(get_range_values "$token" "$spreadsheet_token" "${sheet_id}!A4:A2000") || {
    err "find_date_row: read range failed"
    return 1
  }

  python3 - "$target_date" "$resp" <<'PY'
import sys, json
target = sys.argv[1]
resp = sys.argv[2]
if not resp:
    raise SystemExit("find_date_row: empty response")
try:
    data = json.loads(resp)
except Exception as e:
    raise SystemExit(f"find_date_row invalid json: {resp[:500]!r}, error={e}")
if data.get("code") != 0:
    raise SystemExit(f"read date column failed: {data}")
values = data.get("data", {}).get("valueRange", {}).get("values", [])
for idx, row in enumerate(values, start=4):
    if row and str(row[0]).strip() == target:
        print(idx)
        raise SystemExit(0)
print("")
PY
}

prepend_date_row() {
  local token="$1"
  local spreadsheet_token="$2"
  local sheet_id="$3"
  local today="$4"
  local resp

  resp=$(
    feishu_api_json "POST" \
      "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/${spreadsheet_token}/values_prepend" \
      "$token" \
      "{\"valueRange\":{\"range\":\"${sheet_id}!A4:A4\",\"values\":[[\"${today}\"]]}}"
  ) || {
    err "prepend_date_row: curl failed"
    return 1
  }

  python3 - "$resp" <<'PY'
import json, sys
resp = sys.argv[1]
if not resp:
    raise SystemExit("prepend_date_row: empty response")
try:
    data = json.loads(resp)
except Exception as e:
    raise SystemExit(f"prepend_date_row invalid json: {resp[:500]!r}, error={e}")
if data.get("code") != 0:
    raise SystemExit(f"prepend_date_row failed: {data}")
print("ok")
PY
}

write_cell() {
  local token="$1"
  local spreadsheet_token="$2"
  local sheet_id="$3"
  local cell="$4"
  local value="$5"
  local resp

  resp=$(
    feishu_api_json "PUT" \
      "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/${spreadsheet_token}/values" \
      "$token" \
      "{\"valueRange\":{\"range\":\"${sheet_id}!${cell}:${cell}\",\"values\":[[\"${value}\"]]}}"
  ) || {
    err "write_cell: curl failed"
    return 1
  }

  python3 - "$resp" <<'PY'
import json, sys
resp = sys.argv[1]
if not resp:
    raise SystemExit("write_cell: empty response")
try:
    data = json.loads(resp)
except Exception as e:
    raise SystemExit(f"write_cell invalid json: {resp[:500]!r}, error={e}")
if data.get("code") != 0:
    raise SystemExit(f"write_cell failed: {data}")
print("ok")
PY
}

# -------------------------
# 参数与架构
# -------------------------

PROFILE="Dockerfile"
SKIP_BUILD=false
DRY_RUN=false
SOURCE_IMAGE=""
BUILDER="auto"
PUBLISH_TARGET=""
TARGET_SHEET_TITLE=""
COMPONENT_NAME="$DEFAULT_COMPONENT_NAME"
TAG_PREFIX=""

ARCH=$(uname -m)

if [[ "$ARCH" == "aarch64" ]]; then
  BUILD_PLATFORM="linux/arm64"
  MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "")

  if [[ -f "/etc/nv_tegra_release" ]]; then
    # Jetson（Orin / NX / Xavier）
    P="l4t"

  elif echo "$MODEL" | grep -qi "thor"; then
    # Thor
    P="arm"

  else
    # 普通 ARM
    P="arm"
  fi

elif [[ "$ARCH" == "x86_64" ]]; then
  BUILD_PLATFORM="linux/amd64"
  P="amd"

else
  BUILD_PLATFORM=""
  P="unknown"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --target)
      PUBLISH_TARGET="$2"
      shift 2
      ;;
    --sheet-title)
      TARGET_SHEET_TITLE="$2"
      shift 2
      ;;
    --component-name)
      COMPONENT_NAME="$2"
      shift 2
      ;;
    --tag-prefix)
      TAG_PREFIX="$2"
      shift 2
      ;;
    --source-image)
      SOURCE_IMAGE="$2"
      SKIP_BUILD=true
      shift 2
      ;;
    --skip-build)
      SKIP_BUILD=true
      shift
      ;;
    --builder)
      BUILDER="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

case "$BUILDER" in
  auto)
    if docker buildx version >/dev/null 2>&1; then
      BUILDER="buildx"
    else
      BUILDER="docker"
    fi
    ;;
  buildx)
    docker buildx version >/dev/null 2>&1 || {
      err "buildx was requested but is not available"
      exit 1
    }
    ;;
  docker)
    ;;
  *)
    err "Unsupported builder: ${BUILDER}; expected auto, buildx, or docker"
    exit 1
    ;;
esac

case "$PROFILE" in
  Dockerfile)
    if [[ "$P" == "l4t" ]]; then
      DEFAULT_TARGET="arm"
    else
      DEFAULT_TARGET="${P}"
    fi
    ;;

  Dockerfile_l4t)
    DEFAULT_TARGET="l4t"
    ;;

  Dockerfile_cu128)
    if [[ "$P" == "amd" ]]; then
      DEFAULT_TARGET="amd_cu128"
    else
      DEFAULT_TARGET="arm_cu128"
    fi
    ;;

  Dockerfile_thor)
    DEFAULT_TARGET="thor"
    ;;

  *)
    echo "Unsupported profile: $PROFILE"
    exit 1
    ;;
esac

PUBLISH_TARGET="${PUBLISH_TARGET:-$DEFAULT_TARGET}"
TARGET_SHEET_TITLE="${TARGET_SHEET_TITLE:-${TARGET_TO_SHEET_TITLE[$PUBLISH_TARGET]:-}}"
TAG_PREFIX="${TAG_PREFIX:-${TARGET_TO_TAG_PREFIX[$PUBLISH_TARGET]:-}}"

if [[ -z "$TARGET_SHEET_TITLE" ]]; then
  err "No sheet configured for target '${PUBLISH_TARGET}'; use --sheet-title"
  exit 1
fi

if [[ -z "$TAG_PREFIX" ]]; then
  err "No tag prefix configured for target '${PUBLISH_TARGET}'; use --tag-prefix"
  exit 1
fi

PROFILE_PATH="${DOCKER_DIR}/${PROFILE}"
if [[ ! -f "$PROFILE_PATH" ]]; then
  err "Dockerfile profile not found: ${PROFILE_PATH}"
  exit 1
fi

# -------------------------
# 版本与 tag
# -------------------------

DATE=$(date +%Y%m%d)

require_cmd curl
require_cmd python3
require_cmd docker

detect_latest_ollama_tag() {
  local url
  url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
    https://github.com/ollama/ollama/releases/latest 2>/dev/null || true)"
  if [[ ! "$url" =~ /v[0-9]+\.[0-9]+\.[0-9]+ ]]; then
    url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
      https://ghfast.top/https://github.com/ollama/ollama/releases/latest 2>/dev/null || true)"
  fi
  [[ "$url" =~ /v([0-9]+\.[0-9]+\.[0-9]+) ]] || return 1
  echo "${BASH_REMATCH[1]}"
}

if [[ -z "${OLLAMA_TAG:-}" || "${OLLAMA_TAG}" == "latest" ]]; then
  OLLAMA_TAG="$(detect_latest_ollama_tag)" || {
    err "failed to detect latest Ollama release version"
    exit 1
  }
  log "Detected latest OLLAMA_TAG=${OLLAMA_TAG}"
fi

OLLAMA_VERSION="${OLLAMA_TAG#v}"
TAG=${TAG_PREFIX}_${OLLAMA_VERSION}
IMAGE_URI="swr.cn-southwest-2.myhuaweicloud.com/ictrek/${COMPONENT_NAME}:${TAG}"

# -------------------------
# build args
# -------------------------

BUILD_ARGS=()
PLATFORM_ARGS=()
if [[ -n "$BUILD_PLATFORM" ]]; then
  PLATFORM_ARGS+=(--platform "$BUILD_PLATFORM")
fi
if [[ -n "${PROXY:-}" ]]; then
  echo "Using PROXY=${PROXY}"
  BUILD_ARGS+=(--build-arg "PROXY=${PROXY}")
fi
if [[ -n "${CUDA_BASE_IMAGE:-}" ]]; then
  echo "Using CUDA_BASE_IMAGE=${CUDA_BASE_IMAGE}"
  BUILD_ARGS+=(--build-arg "CUDA_BASE_IMAGE=${CUDA_BASE_IMAGE}")
fi

echo "Using OLLAMA_TAG=${OLLAMA_TAG}"
BUILD_ARGS+=(--build-arg "OLLAMA_TAG=${OLLAMA_TAG}")

if [[ -n "${USE_OLD_TRANSFORMERS:-}" ]]; then
  echo "Using USE_OLD_TRANSFORMERS=${USE_OLD_TRANSFORMERS}"
  BUILD_ARGS+=(--build-arg "USE_OLD_TRANSFORMERS=${USE_OLD_TRANSFORMERS}")
fi


# -------------------------
# 构建并推送
# -------------------------

log "PROFILE=${PROFILE}"
log "PUBLISH_TARGET=${PUBLISH_TARGET}"
log "TARGET_SHEET_TITLE=${TARGET_SHEET_TITLE}"
log "COMPONENT_NAME=${COMPONENT_NAME}"
log "TAG_PREFIX=${TAG_PREFIX}"
log "TAG=${TAG}"
log "IMAGE_URI=${IMAGE_URI}"
log "BUILDER=${BUILDER}"
log "BUILD_PLATFORM=${BUILD_PLATFORM:-auto}"

if [[ "$DRY_RUN" == "true" ]]; then
  log "Dry run complete; no image was built or pushed and Feishu was not updated."
  exit 0
fi

if [[ ! -f "$FEISHU_CONFIG_FILE" ]]; then
  err "Feishu config not found: $FEISHU_CONFIG_FILE"
  exit 1
fi

FEISHU_APP_ID="$(read_feishu_field "feishu_app_id")"
FEISHU_APP_SECRET="$(read_feishu_field "feishu_app_secret")"

if [[ -z "$FEISHU_APP_ID" || -z "$FEISHU_APP_SECRET" ]]; then
  err "feishu_app_id or feishu_app_secret missing in $FEISHU_CONFIG_FILE"
  exit 1
fi

if [[ "$SKIP_BUILD" == "true" ]]; then
  if [[ -z "$SOURCE_IMAGE" ]]; then
    SOURCE_IMAGE="${DEFAULT_COMPONENT_NAME}:${OLLAMA_TAG:-latest}"
  fi
  log "SOURCE_IMAGE=${SOURCE_IMAGE}"
  docker image inspect "$SOURCE_IMAGE" >/dev/null
  docker tag "$SOURCE_IMAGE" "$IMAGE_URI"
else
  if [[ "$BUILDER" == "buildx" ]]; then
    docker buildx build \
      --load \
      "${PLATFORM_ARGS[@]}" \
      "${BUILD_ARGS[@]}" \
      -t "${IMAGE_URI}" \
      -f "$PROFILE_PATH" "$REPO_ROOT"
  else
    docker build \
      "${PLATFORM_ARGS[@]}" \
      "${BUILD_ARGS[@]}" \
      -t "${IMAGE_URI}" \
      -f "$PROFILE_PATH" "$REPO_ROOT"
  fi
fi

docker push "${IMAGE_URI}"

log "Docker push succeeded: ${IMAGE_URI}"

# -------------------------
# push 成功后写飞书
# -------------------------

FEISHU_TOKEN="$(get_feishu_token "$FEISHU_APP_ID" "$FEISHU_APP_SECRET")"
SHEET_ID="$(get_sheet_id_by_title "$FEISHU_TOKEN" "$FEISHU_SPREADSHEET_TOKEN" "$TARGET_SHEET_TITLE")"
log "Resolved sheet: ${TARGET_SHEET_TITLE} -> ${SHEET_ID}"

FEISHU_TOKEN="$(get_feishu_token "$FEISHU_APP_ID" "$FEISHU_APP_SECRET")"
COMPONENT_COL="$(find_component_column_letter "$FEISHU_TOKEN" "$FEISHU_SPREADSHEET_TOKEN" "$SHEET_ID" "$COMPONENT_NAME")"
log "Resolved component column: ${COMPONENT_NAME} -> ${COMPONENT_COL}"

FEISHU_TOKEN="$(get_feishu_token "$FEISHU_APP_ID" "$FEISHU_APP_SECRET")"
DATE_ROW="$(find_date_row "$FEISHU_TOKEN" "$FEISHU_SPREADSHEET_TOKEN" "$SHEET_ID" "$DATE")"

if [[ -z "$DATE_ROW" ]]; then
  log "Date ${DATE} not found, creating a new row at top of data area"
  FEISHU_TOKEN="$(get_feishu_token "$FEISHU_APP_ID" "$FEISHU_APP_SECRET")"
  prepend_date_row "$FEISHU_TOKEN" "$FEISHU_SPREADSHEET_TOKEN" "$SHEET_ID" "$DATE" >/dev/null
  DATE_ROW=4
else
  log "Date ${DATE} already exists at row ${DATE_ROW}"
fi

FEISHU_TOKEN="$(get_feishu_token "$FEISHU_APP_ID" "$FEISHU_APP_SECRET")"
write_cell "$FEISHU_TOKEN" "$FEISHU_SPREADSHEET_TOKEN" "$SHEET_ID" "${COMPONENT_COL}${DATE_ROW}" "$TAG" >/dev/null

log "Feishu updated: ${TARGET_SHEET_TITLE}!${COMPONENT_COL}${DATE_ROW} = ${TAG}"
