#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${SCRIPT_DIR}/deploy-nas-dev.env"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
fi

: "${NAS_SSH_TARGET:?Set NAS_SSH_TARGET in scripts/deploy-nas-dev.env, for example user@192.168.1.100}"
: "${NAS_COMPOSE_DIR:?Set NAS_COMPOSE_DIR in scripts/deploy-nas-dev.env, for example /volume1/docker/chillposter}"

NAS_COMPOSE_SERVICE="${NAS_COMPOSE_SERVICE:-chillposter}"
NAS_SSH_PORT="${NAS_SSH_PORT:-22}"
NAS_SSH_OPTS="${NAS_SSH_OPTS:--o IPQoS=none}"
NAS_PLATFORM="${NAS_PLATFORM:-linux/amd64}"
NAS_IMAGE_NAME="${NAS_IMAGE_NAME:-chillposter:dev}"
NAS_BASE_IMAGE="${NAS_BASE_IMAGE:-chillne/chillposter:latest}"
NAS_BASE_ALIAS="${NAS_BASE_ALIAS:-chillposter-dev-base:latest}"
NAS_BASE_PULL="${NAS_BASE_PULL:-missing}"
NAS_DOCKERFILE="${NAS_DOCKERFILE:-Dockerfile.dev-nas}"
NAS_DOCKER_CMD="${NAS_DOCKER_CMD:-docker}"
NAS_COMPOSE_CMD="${NAS_COMPOSE_CMD:-docker compose}"
NAS_BUILD_ARGS="${NAS_BUILD_ARGS:-}"
NAS_TRANSFER_METHOD="${NAS_TRANSFER_METHOD:-smb}"
NAS_SMB_URL="${NAS_SMB_URL:-}"
NAS_SMB_MOUNT="${NAS_SMB_MOUNT:-}"
NAS_SMB_TAR_PATH="${NAS_SMB_TAR_PATH:-}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not available locally." >&2
  exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh is not available locally." >&2
  exit 1
fi

if [[ "${NAS_TRANSFER_METHOD}" != "smb" ]] && ! command -v scp >/dev/null 2>&1; then
  echo "scp is not available locally." >&2
  exit 1
fi

read -r -a NAS_SSH_OPTS_ARRAY <<< "${NAS_SSH_OPTS}"
NAS_SSH=(ssh "${NAS_SSH_OPTS_ARRAY[@]}" -p "${NAS_SSH_PORT}" "${NAS_SSH_TARGET}")
NAS_SCP=(scp "${NAS_SSH_OPTS_ARRAY[@]}" -P "${NAS_SSH_PORT}")

if git -C "${ROOT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  GIT_SHA="$(git -C "${ROOT_DIR}" rev-parse --short HEAD)"
else
  GIT_SHA="nogit"
fi

BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DEV_VERSION="dev-${GIT_SHA}-$(date +%Y%m%d%H%M%S)"
NAS_IMAGE_TAR_LOCAL="${NAS_IMAGE_TAR_LOCAL:-/tmp/chillposter-nas-dev-${DEV_VERSION}.tar}"
NAS_IMAGE_TAR_REMOTE="${NAS_IMAGE_TAR_REMOTE:-${NAS_COMPOSE_DIR}/chillposter-nas-dev.tar}"

echo "==> Building ${NAS_IMAGE_NAME} for ${NAS_PLATFORM}"
cd "${ROOT_DIR}"

if [[ "${NAS_BASE_IMAGE}" == "${NAS_IMAGE_NAME}" ]]; then
  echo "NAS_BASE_IMAGE must not equal NAS_IMAGE_NAME, otherwise dev builds stack on top of themselves." >&2
  echo "Set NAS_BASE_IMAGE to a published release tag, for example chillne/chillposter:v4.8.5.7." >&2
  exit 1
fi

if ! docker image inspect "${NAS_BASE_IMAGE}" >/dev/null 2>&1; then
  if [[ "${NAS_BASE_PULL}" == "never" ]]; then
    echo "Base image ${NAS_BASE_IMAGE} is not available locally and NAS_BASE_PULL=never." >&2
    exit 1
  fi
  echo "==> Pulling base image ${NAS_BASE_IMAGE}"
  docker pull "${NAS_BASE_IMAGE}"
elif [[ "${NAS_BASE_PULL}" == "always" ]]; then
  echo "==> Refreshing base image ${NAS_BASE_IMAGE}"
  docker pull "${NAS_BASE_IMAGE}"
fi

docker tag "${NAS_BASE_IMAGE}" "${NAS_BASE_ALIAS}"

# Intentionally split NAS_BUILD_ARGS so users can pass normal docker flags.
# shellcheck disable=SC2086
docker buildx build \
  --platform "${NAS_PLATFORM}" \
  --load \
  -f "${NAS_DOCKERFILE}" \
  --build-arg "CHILLPOSTER_VERSION=${DEV_VERSION}" \
  --build-arg "BUILD_DATE=${BUILD_DATE}" \
  --build-arg "CHILLPOSTER_BASE_IMAGE=${NAS_BASE_ALIAS}" \
  -t "${NAS_IMAGE_NAME}" \
  ${NAS_BUILD_ARGS} \
  .

echo "==> Saving image tar: ${NAS_IMAGE_TAR_LOCAL}"
rm -f "${NAS_IMAGE_TAR_LOCAL}"
docker save -o "${NAS_IMAGE_TAR_LOCAL}" "${NAS_IMAGE_NAME}"

if [[ "${NAS_TRANSFER_METHOD}" == "smb" ]]; then
  : "${NAS_SMB_URL:?Set NAS_SMB_URL when NAS_TRANSFER_METHOD=smb, for example smb://user@host/docker}"
  : "${NAS_SMB_MOUNT:?Set NAS_SMB_MOUNT when NAS_TRANSFER_METHOD=smb, for example /Volumes/docker}"
  : "${NAS_SMB_TAR_PATH:?Set NAS_SMB_TAR_PATH when NAS_TRANSFER_METHOD=smb, for example ChillPoster/chillposter-nas-dev.tar}"
  NAS_SMB_DEST="${NAS_SMB_MOUNT%/}/${NAS_SMB_TAR_PATH}"

  if [[ ! -d "${NAS_SMB_MOUNT}" ]]; then
    echo "==> Mounting NAS SMB share: ${NAS_SMB_URL}"
    osascript -e "mount volume \"${NAS_SMB_URL}\""
  fi
  if [[ ! -d "${NAS_SMB_MOUNT}" ]]; then
    echo "SMB mount is not available: ${NAS_SMB_MOUNT}" >&2
    exit 1
  fi

  echo "==> Copying image tar to NAS over SMB: ${NAS_SMB_DEST}"
  mkdir -p "$(dirname "${NAS_SMB_DEST}")"
  rm -f "${NAS_SMB_DEST}"
  rsync --progress "${NAS_IMAGE_TAR_LOCAL}" "${NAS_SMB_DEST}"
else
  echo "==> Copying image tar to NAS: ${NAS_SSH_TARGET}:${NAS_IMAGE_TAR_REMOTE}"
  "${NAS_SSH[@]}" "rm -f '${NAS_IMAGE_TAR_REMOTE}'"
  "${NAS_SCP[@]}" "${NAS_IMAGE_TAR_LOCAL}" "${NAS_SSH_TARGET}:${NAS_IMAGE_TAR_REMOTE}"
fi

echo "==> Loading image on NAS: ${NAS_IMAGE_TAR_REMOTE}"
"${NAS_SSH[@]}" "${NAS_DOCKER_CMD} load -i '${NAS_IMAGE_TAR_REMOTE}'"

if [[ -n "${NAS_COMPOSE_SERVICE}" ]]; then
  echo "==> Recreating compose service: ${NAS_COMPOSE_SERVICE}"
  "${NAS_SSH[@]}" "cd '${NAS_COMPOSE_DIR}' && ${NAS_COMPOSE_CMD} up -d --force-recreate --no-deps '${NAS_COMPOSE_SERVICE}'"
else
  echo "==> Recreating compose project"
  "${NAS_SSH[@]}" "cd '${NAS_COMPOSE_DIR}' && ${NAS_COMPOSE_CMD} up -d --force-recreate"
fi

"${NAS_SSH[@]}" "rm -f '${NAS_IMAGE_TAR_REMOTE}'"
rm -f "${NAS_IMAGE_TAR_LOCAL}"

echo "==> Done. Deployed ${NAS_IMAGE_NAME} (${DEV_VERSION}) to ${NAS_SSH_TARGET}."
