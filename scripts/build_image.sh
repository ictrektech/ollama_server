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
GITHUB_MIRRORS="${GITHUB_MIRRORS:-https://gh-proxy.com https://ghfast.top https://gh.llkk.cc}"

# 发布目标决定飞书 sheet 和镜像 tag 前缀，与使用哪个 Dockerfile 构建解耦。
# 一个目标可以写入多个 sheet，避免 ARM 通用镜像只更新部分标签页。
target_sheet_titles() {
  case "$1" in
    amd) echo "AMD_with_cuda" ;;
    arm) echo "ARM_with_cuda ARM_without_cuda SOPHON_bm1688" ;;
    l4t) echo "l4t" ;;
    thor|thor_spark) echo "thor_spark" ;;
    amd_cu128) echo "AMD_with_cuda" ;;
    arm_cu128) echo "ARM_with_cuda ARM_without_cuda SOPHON_bm1688" ;;
    *) echo "" ;;
  esac
}

target_tag_prefix() {
  case "$1" in
    amd) echo "amd" ;;
    arm) echo "arm" ;;
    l4t) echo "l4t" ;;
    thor|thor_spark) echo "thor" ;;
    amd_cu128) echo "amd_cu128" ;;
    arm_cu128) echo "arm_cu128" ;;
    *) echo "" ;;
  esac
}

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
  --sheet-title TITLE    Override the target's Feishu sheet; repeat or comma-separate for multiple sheets
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

find_or_create_component_column_letter() {
  local token="$1"
  local spreadsheet_token="$2"
  local sheet_id="$3"
  local component_name="$4"
  local image_repo="$5"
  local resp

  resp=$(get_range_values "$token" "$spreadsheet_token" "${sheet_id}!A1:ZZ2") || {
    err "find_or_create_component_column_letter: read range failed"
    return 1
  }

  local resolved
  resolved="$(python3 - "$component_name" "$resp" <<'PY'
import sys, json

target = sys.argv[1]
resp = sys.argv[2]

def letters(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s

if not resp:
    raise SystemExit("find_or_create_component_column_letter: empty response")
try:
    data = json.loads(resp)
except Exception as e:
    raise SystemExit(f"find_or_create_component_column_letter invalid json: {resp[:500]!r}, error={e}")
if data.get("code") != 0:
    raise SystemExit(f"read header failed: {data}")

def cell_text(value):
    if value is None:
        return ""
    return str(value).strip()

values = data.get("data", {}).get("valueRange", {}).get("values", [])
header = values[0] if values else []
last_used = 1
for i, value in enumerate(header, start=1):
    text = cell_text(value)
    if text:
        last_used = i
    if text == target:
        print(f"exists {letters(i)}")
        raise SystemExit(0)

print(f"create {letters(last_used + 1)}")
PY
)"

  local action col
  action="${resolved%% *}"
  col="${resolved#* }"
  if [[ "$action" == "create" ]]; then
    log "Component column ${component_name} not found; creating ${col}" >&2
    write_cell "$token" "$spreadsheet_token" "$sheet_id" "${col}1" "$component_name" >/dev/null
    write_cell "$token" "$spreadsheet_token" "$sheet_id" "${col}2" "$image_repo" >/dev/null
  fi
  echo "$col"
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
TARGET_SHEET_TITLES=()
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
      IFS=',' read -ra _SHEETS <<< "$2"
      for _sheet in "${_SHEETS[@]}"; do
        [[ -n "$_sheet" ]] && TARGET_SHEET_TITLES+=("$_sheet")
      done
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
if [[ ${#TARGET_SHEET_TITLES[@]} -eq 0 ]]; then
  TARGET_SHEET_TITLE="$(target_sheet_titles "$PUBLISH_TARGET")"
  read -ra TARGET_SHEET_TITLES <<< "$TARGET_SHEET_TITLE"
else
  TARGET_SHEET_TITLE="${TARGET_SHEET_TITLES[*]}"
fi
TAG_PREFIX="${TAG_PREFIX:-$(target_tag_prefix "$PUBLISH_TARGET")}"

if [[ ${#TARGET_SHEET_TITLES[@]} -eq 0 ]]; then
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

detect_latest_ollama_tag() {
  local url api_tag
  url="$(curl --connect-timeout 5 --max-time 20 -fsSLI -o /dev/null -w '%{url_effective}' \
    https://ghfast.top/https://github.com/ollama/ollama/releases/latest 2>/dev/null || true)"
  if [[ "$url" =~ /v([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    echo "${BASH_REMATCH[1]}"
    return 0
  fi

  url="$(curl --connect-timeout 5 --max-time 15 -fsSLI -o /dev/null -w '%{url_effective}' \
    https://github.com/ollama/ollama/releases/latest 2>/dev/null || true)"
  if [[ "$url" =~ /v([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    echo "${BASH_REMATCH[1]}"
    return 0
  fi

  api_tag="$(curl --connect-timeout 5 --max-time 15 -fsSL \
    https://api.github.com/repos/ollama/ollama/releases/latest 2>/dev/null \
    | python3 -c 'import json,sys; print((json.load(sys.stdin).get("tag_name") or "").lstrip("v"))' 2>/dev/null || true)"
  if [[ "$api_tag" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "$api_tag"
    return 0
  fi

  return 1
}

fetch_latest_ollama_release_json() {
  local url mirror
  for mirror in $GITHUB_MIRRORS ""; do
    if [[ -n "$mirror" ]]; then
      url="${mirror}/https://api.github.com/repos/ollama/ollama/releases/latest"
    else
      url="https://api.github.com/repos/ollama/ollama/releases/latest"
    fi
    curl --connect-timeout 5 --max-time 20 -fsSL "$url" 2>/dev/null && return 0
  done
  return 1
}

select_ollama_asset_url() {
  local release_json="$1"
  local asset_kind="$2"
  python3 - "$release_json" "$asset_kind" <<'PY'
import json
import sys

data = json.loads(sys.argv[1])
asset_kind = sys.argv[2]
assets = data.get("assets") or []

def tokens(name):
    return name.lower().replace("_", "-").split("-")

def is_archive(name):
    lowered = name.lower()
    return lowered.endswith(".tar.zst") and "linux" in lowered

def has_any(name, values):
    lowered = name.lower()
    return any(value in lowered for value in values)

def score(asset, required, rejected=()):
    name = asset.get("name") or ""
    lowered = name.lower()
    if not is_archive(name):
        return -1
    if any(word in lowered for word in rejected):
        return -1
    if not all(has_any(name, group) for group in required):
        return -1
    result = 0
    if lowered.startswith("ollama-linux-"):
        result += 10
    if lowered.endswith(".tar.zst"):
        result += 3
    result -= len(name) / 1000
    return result

rules = {
    "linux-amd64": {
        "required": [["amd64", "x86-64", "x86_64"]],
        "rejected": ["rocm", "jetpack", "cuda", "windows", "darwin"],
    },
    "linux-arm64": {
        "required": [["arm64", "aarch64"]],
        "rejected": ["jetpack", "windows", "darwin"],
    },
    "linux-arm64-jetpack5": {
        "required": [["arm64", "aarch64"], ["jetpack5", "jetpack-5", "jp5"]],
        "rejected": ["windows", "darwin"],
    },
    "linux-arm64-jetpack6": {
        "required": [["arm64", "aarch64"], ["jetpack6", "jetpack-6", "jp6"]],
        "rejected": ["windows", "darwin"],
    },
}

rule = rules.get(asset_kind)
if not rule:
    raise SystemExit(1)

matches = []
for asset in assets:
    value = score(asset, rule["required"], rule["rejected"])
    if value >= 0:
        matches.append((value, asset))

matches.sort(key=lambda item: item[0], reverse=True)
if matches:
    print(matches[0][1].get("browser_download_url") or "")
    raise SystemExit(0)

raise SystemExit(1)
PY
}

mirror_github_url() {
  local url="$1"
  local mirrored=()
  local mirror
  if [[ "$url" == https://github.com/* ]]; then
    for mirror in $GITHUB_MIRRORS; do
      mirrored+=("${mirror}/${url}")
    done
    mirrored+=("$url")
    (IFS=' '; echo "${mirrored[*]}")
  else
    echo "$url"
  fi
}

if [[ -z "${OLLAMA_TAG:-}" || "${OLLAMA_TAG}" == "latest" ]]; then
  OLLAMA_TAG="$(detect_latest_ollama_tag)" || {
    err "failed to detect latest Ollama release version"
    exit 1
  }
  log "Detected latest OLLAMA_TAG=${OLLAMA_TAG}"
fi

OLLAMA_RELEASE_JSON=""
if [[ "$PROFILE" != "Dockerfile" ]]; then
  OLLAMA_RELEASE_JSON="$(fetch_latest_ollama_release_json)" || {
    err "failed to fetch latest Ollama release asset list"
    exit 1
  }
fi

OLLAMA_VERSION="${OLLAMA_TAG#v}"
TAG=${TAG_PREFIX}_${OLLAMA_VERSION}
IMAGE_URI="swr.cn-southwest-2.myhuaweicloud.com/ictrek/${COMPONENT_NAME}:${TAG}"
IMAGE_REPO="swr.cn-southwest-2.myhuaweicloud.com/ictrek/${COMPONENT_NAME}"

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

if [[ -n "$OLLAMA_RELEASE_JSON" ]]; then
  ASSET_KIND=""
  OVERLAY_ASSET_KIND=""
  case "$PUBLISH_TARGET" in
    l4t)
      ASSET_KIND="linux-arm64"
      OVERLAY_ASSET_KIND="linux-arm64-jetpack6"
      ;;
    thor|thor_spark|arm_cu128)
      ASSET_KIND="linux-arm64"
      ;;
    amd_cu128)
      ASSET_KIND="linux-amd64"
      ;;
  esac

  if [[ -n "$ASSET_KIND" ]]; then
    OLLAMA_ARCHIVE_URL="$(select_ollama_asset_url "$OLLAMA_RELEASE_JSON" "$ASSET_KIND")" || {
      err "failed to find Ollama release asset for ${ASSET_KIND}"
      exit 1
    }
    OLLAMA_ARCHIVE_URLS="$(mirror_github_url "$OLLAMA_ARCHIVE_URL")"
    echo "Using OLLAMA_ARCHIVE_URL=$(echo "$OLLAMA_ARCHIVE_URL" | sed 's/[?].*$//')"
    BUILD_ARGS+=(--build-arg "OLLAMA_ARCHIVE_URLS=${OLLAMA_ARCHIVE_URLS}")
  fi

  if [[ -n "$OVERLAY_ASSET_KIND" ]]; then
    OLLAMA_JETPACK_ARCHIVE_URL="$(select_ollama_asset_url "$OLLAMA_RELEASE_JSON" "$OVERLAY_ASSET_KIND")" || {
      err "failed to find Ollama release asset for ${OVERLAY_ASSET_KIND}"
      exit 1
    }
    OLLAMA_JETPACK_ARCHIVE_URLS="$(mirror_github_url "$OLLAMA_JETPACK_ARCHIVE_URL")"
    echo "Using OLLAMA_JETPACK_ARCHIVE_URL=$(echo "$OLLAMA_JETPACK_ARCHIVE_URL" | sed 's/[?].*$//')"
    BUILD_ARGS+=(--build-arg "OLLAMA_JETPACK_ARCHIVE_URLS=${OLLAMA_JETPACK_ARCHIVE_URLS}")
  fi
fi

if [[ -n "${USE_OLD_TRANSFORMERS:-}" ]]; then
  echo "Using USE_OLD_TRANSFORMERS=${USE_OLD_TRANSFORMERS}"
  BUILD_ARGS+=(--build-arg "USE_OLD_TRANSFORMERS=${USE_OLD_TRANSFORMERS}")
fi


# -------------------------
# 构建并推送
# -------------------------

log "PROFILE=${PROFILE}"
log "PUBLISH_TARGET=${PUBLISH_TARGET}"
log "TARGET_SHEET_TITLES=${TARGET_SHEET_TITLES[*]}"
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

require_cmd docker

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
      --provenance=false \
      "${PLATFORM_ARGS[@]}" \
      "${BUILD_ARGS[@]}" \
      -t "${IMAGE_URI}" \
      -f "$PROFILE_PATH" "$REPO_ROOT"
  else
    DOCKER_BUILDKIT=0 docker build \
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

for SHEET_TITLE in "${TARGET_SHEET_TITLES[@]}"; do
  FEISHU_TOKEN="$(get_feishu_token "$FEISHU_APP_ID" "$FEISHU_APP_SECRET")"
  SHEET_ID="$(get_sheet_id_by_title "$FEISHU_TOKEN" "$FEISHU_SPREADSHEET_TOKEN" "$SHEET_TITLE")"
  log "Resolved sheet: ${SHEET_TITLE} -> ${SHEET_ID}"

  FEISHU_TOKEN="$(get_feishu_token "$FEISHU_APP_ID" "$FEISHU_APP_SECRET")"
  COMPONENT_COL="$(find_or_create_component_column_letter "$FEISHU_TOKEN" "$FEISHU_SPREADSHEET_TOKEN" "$SHEET_ID" "$COMPONENT_NAME" "$IMAGE_REPO")"
  log "Resolved component column: ${COMPONENT_NAME} -> ${COMPONENT_COL}"

  FEISHU_TOKEN="$(get_feishu_token "$FEISHU_APP_ID" "$FEISHU_APP_SECRET")"
  DATE_ROW="$(find_date_row "$FEISHU_TOKEN" "$FEISHU_SPREADSHEET_TOKEN" "$SHEET_ID" "$DATE")"

  if [[ -z "$DATE_ROW" ]]; then
    log "Date ${DATE} not found in ${SHEET_TITLE}, creating a new row at top of data area"
    FEISHU_TOKEN="$(get_feishu_token "$FEISHU_APP_ID" "$FEISHU_APP_SECRET")"
    prepend_date_row "$FEISHU_TOKEN" "$FEISHU_SPREADSHEET_TOKEN" "$SHEET_ID" "$DATE" >/dev/null
    DATE_ROW=4
  else
    log "Date ${DATE} already exists in ${SHEET_TITLE} at row ${DATE_ROW}"
  fi

  FEISHU_TOKEN="$(get_feishu_token "$FEISHU_APP_ID" "$FEISHU_APP_SECRET")"
  write_cell "$FEISHU_TOKEN" "$FEISHU_SPREADSHEET_TOKEN" "$SHEET_ID" "${COMPONENT_COL}${DATE_ROW}" "$TAG" >/dev/null

  log "Feishu updated: ${SHEET_TITLE}!${COMPONENT_COL}${DATE_ROW} = ${TAG}"
done
