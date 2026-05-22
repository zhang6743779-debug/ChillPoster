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
NAS_PLATFORM="${NAS_PLATFORM:-linux/amd64}"
NAS_IMAGE_NAME="${NAS_IMAGE_NAME:-chillposter:dev}"
NAS_BASE_IMAGE="${NAS_BASE_IMAGE:-chillne/chillposter:latest}"
NAS_BASE_ALIAS="${NAS_BASE_ALIAS:-chillposter-dev-base:latest}"
NAS_BASE_PULL="${NAS_BASE_PULL:-missing}"
NAS_DOCKERFILE="${NAS_DOCKERFILE:-Dockerfile.dev-nas}"
NAS_DOCKER_CMD="${NAS_DOCKER_CMD:-docker}"
NAS_COMPOSE_CMD="${NAS_COMPOSE_CMD:-docker compose}"
NAS_BUILD_ARGS="${NAS_BUILD_ARGS:-}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not available locally." >&2
  exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh is not available locally." >&2
  exit 1
fi

if git -C "${ROOT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  GIT_SHA="$(git -C "${ROOT_DIR}" rev-parse --short HEAD)"
else
  GIT_SHA="nogit"
fi

BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DEV_VERSION="dev-${GIT_SHA}-$(date +%Y%m%d%H%M%S)"

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

echo "==> Loading image into NAS: ${NAS_SSH_TARGET}"
docker save "${NAS_IMAGE_NAME}" | ssh -p "${NAS_SSH_PORT}" "${NAS_SSH_TARGET}" "${NAS_DOCKER_CMD} load"

if [[ -n "${NAS_COMPOSE_SERVICE}" ]]; then
  echo "==> Recreating compose service: ${NAS_COMPOSE_SERVICE}"
  ssh -p "${NAS_SSH_PORT}" "${NAS_SSH_TARGET}" "cd '${NAS_COMPOSE_DIR}' && ${NAS_COMPOSE_CMD} up -d --force-recreate --no-deps '${NAS_COMPOSE_SERVICE}'"
else
  echo "==> Recreating compose project"
  ssh -p "${NAS_SSH_PORT}" "${NAS_SSH_TARGET}" "cd '${NAS_COMPOSE_DIR}' && ${NAS_COMPOSE_CMD} up -d --force-recreate"
fi

echo "==> Done. Deployed ${NAS_IMAGE_NAME} (${DEV_VERSION}) to ${NAS_SSH_TARGET}."
