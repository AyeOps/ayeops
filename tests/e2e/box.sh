#!/bin/sh
set -eu

box="${AYE_E2E_BOX:-aye-e2e}"
image="docker.io/library/docker:29.9.1-dind@sha256:3e5756e2b6dad6a5f1f5deacf0a19f9eaa2581b76ae092b2d4a118162e1d4d4e"
uv_image="ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336"
memory=8g
lifetime=1800
render=/tmp/aye-e2e
work=/work

start() {
  cd "$(dirname "$0")/../.."
  docker run --detach --rm --privileged \
    --name "$box" \
    --label io.ayeops.e2e=box \
    --memory "$memory" --memory-swap "$memory" \
    --env DOCKER_IPTABLES_LEGACY= \
    --entrypoint timeout \
    "$image" "$lifetime" dockerd-entrypoint.sh dockerd --host=unix:///var/run/docker.sock
  tries=0
  until docker exec "$box" docker info > /dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge 90 ] || ! docker container inspect "$box" > /dev/null 2>&1; then
      docker logs "$box" 2>&1 || true
      echo "the Docker daemon in $box did not start" >&2
      exit 1
    fi
    sleep 1
  done
  docker exec "$box" docker info --format '{{.ServerVersion}} {{.Driver}} {{.DockerRootDir}}'
  docker exec --env UV_IMAGE="$uv_image" "$box" sh -c \
    'docker create --name uv "$UV_IMAGE" /uv > /dev/null && docker cp uv:/uv /usr/local/bin/uv && docker rm uv > /dev/null'
  docker exec "$box" mkdir -p "$work"
  git ls-files -z --cached --others --exclude-standard \
    | tar --null -T - -cf - \
    | docker exec -i "$box" tar -xf - -C "$work"
  docker exec -w "$work" "$box" uv sync --locked
}

logs() {
  out="$1"
  mkdir -p "$out"
  docker logs "$box" > "$out/box.log" 2>&1 || true
  docker exec -w "$work" "$box" uv run python -m tests.e2e.stack logs "$render" --out "$work/e2e-logs"
  docker cp "$box:$work/e2e-logs/." "$out"
}

stop() {
  if docker container inspect "$box" > /dev/null 2>&1; then
    docker rm --force --volumes "$box" > /dev/null
  fi
}

command="${1:-}"
if [ "$#" -gt 0 ]; then
  shift
fi
case "$command" in
  start) start ;;
  exec) docker exec -w "$work" "$box" "$@" ;;
  logs) logs "${1:-e2e-logs}" ;;
  stop) stop ;;
  *)
    echo "usage: $0 start | exec COMMAND... | logs [DIR] | stop" >&2
    exit 2
    ;;
esac
