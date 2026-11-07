#!/usr/bin/env bash
# Run a command in the embedded dev container (edge/docker/Dockerfile.dev) with the repo
# mounted at /src, starting in /src/edge. Builds the image on first use.
#
#   scripts/edge-dev.sh cmake --preset native
#   scripts/edge-dev.sh bash -c 'cmake --build --preset native && ctest --preset native'
#   scripts/edge-dev.sh                       # interactive shell
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
image=openfleet-edge-dev
if ! docker image inspect "$image" >/dev/null 2>&1; then
  docker build -t "$image" -f "$repo/edge/docker/Dockerfile.dev" "$repo/edge/docker"
fi
tty=(); [ -t 0 ] && tty=(-it)
# TSan needs ASLR off on some kernels; --cap-add lets `setarch -R` do that per process.
exec docker run --rm ${tty[@]+"${tty[@]}"} --cap-add SYS_PTRACE -v "$repo:/src" -w /src/edge "$image" "${@:-bash}"
