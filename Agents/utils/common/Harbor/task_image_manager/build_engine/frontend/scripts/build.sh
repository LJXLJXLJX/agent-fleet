#!/usr/bin/env bash
set -euo pipefail

# Build only a local OCI layout. Source selection uses ecosystem-native mirrors.
: "${FRONTEND_WORK_DIR:?set a fresh /data build directory}"
component=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
go_proxy=${HARBOR_TASK_IMAGE_GOPROXY:-https://goproxy.cn,direct}
go_sumdb=${HARBOR_TASK_IMAGE_GOSUMDB:-sum.golang.google.cn}
github_mirror=${HARBOR_TASK_IMAGE_GITHUB_MIRROR_URL:-}
clone_timeout=${HARBOR_TASK_IMAGE_FRONTEND_CLONE_TIMEOUT_SEC:-600}
if [[ ! "$clone_timeout" =~ ^[1-9][0-9]*$ ]] ||
    [[ ${#clone_timeout} -gt 4 ]] ||
    (( 10#$clone_timeout > 3600 )); then
    echo "HARBOR_TASK_IMAGE_FRONTEND_CLONE_TIMEOUT_SEC must be an integer from 1 to 3600" >&2
    exit 2
fi
buildkit_source=https://github.com/moby/buildkit.git
if [[ -n "$github_mirror" ]]; then
    buildkit_source="${github_mirror%/}/moby/buildkit.git"
fi
mapfile -t upstream < "$component/upstream.version"
# Setup owns Go installation; cold builds only select a validated compiler.
source "$component/../../../../../../../scripts/prerequisites.sh"
go_binary=$(agent_fleet_find_frontend_go)
test ! -e "$FRONTEND_WORK_DIR"
mkdir -p "$FRONTEND_WORK_DIR/image"
export GOPROXY="$go_proxy"
export GOSUMDB="$go_sumdb"
export GOTOOLCHAIN=local
unset GOROOT
export GOCACHE="$FRONTEND_WORK_DIR/go-cache"
export GOPATH="$FRONTEND_WORK_DIR/go"
export CGO_ENABLED=0
timeout "$clone_timeout" git clone --single-branch --branch "${upstream[0]}" \
    "$buildkit_source" \
    "$FRONTEND_WORK_DIR/upstream"
test "$(git -C "$FRONTEND_WORK_DIR/upstream" rev-parse HEAD)" = "${upstream[1]}"
git -C "$FRONTEND_WORK_DIR/upstream" apply --check "$component/patches/0001-opensandbox-run-instrumentation.patch"
git -C "$FRONTEND_WORK_DIR/upstream" apply "$component/patches/0001-opensandbox-run-instrumentation.patch"
cp "$component/instrumentation.go" "$FRONTEND_WORK_DIR/upstream/frontend/dockerfile/dockerfile2llb/opensandbox.go"
(
    cd "$FRONTEND_WORK_DIR/upstream"
    timeout 600 "$go_binary" build -buildvcs=false -mod=vendor -trimpath \
        -tags=dfrunsecurity,dfrundevice \
        -o "$FRONTEND_WORK_DIR/image/dockerfile-frontend" \
        ./frontend/dockerfile/cmd/dockerfile-frontend
)
binary_digest=$(sha256sum "$FRONTEND_WORK_DIR/image/dockerfile-frontend" | awk '{print $1}')
binary_name="dockerfile-frontend-${binary_digest}"
mv "$FRONTEND_WORK_DIR/image/dockerfile-frontend" \
    "$FRONTEND_WORK_DIR/image/${binary_name}"
sed "s|COPY dockerfile-frontend /bin/dockerfile-frontend|COPY ${binary_name} /bin/dockerfile-frontend|" \
    "$component/Dockerfile" > "$FRONTEND_WORK_DIR/image/Dockerfile"
grep -F "COPY ${binary_name} /bin/dockerfile-frontend" \
    "$FRONTEND_WORK_DIR/image/Dockerfile" >/dev/null
printf '*\n!Dockerfile\n!%s\n' "$binary_name" \
    > "$FRONTEND_WORK_DIR/image/.dockerignore"
source_epoch=$(git -C "$FRONTEND_WORK_DIR/upstream" show -s --format=%ct "${upstream[1]}")
touch -d "@$source_epoch" "$FRONTEND_WORK_DIR/image/${binary_name}"
timeout 180 docker buildx build --no-cache --network=none --provenance=false \
    --output "type=oci,dest=$FRONTEND_WORK_DIR/layout,tar=false,rewrite-timestamp=true" \
    --build-arg "SOURCE_DATE_EPOCH=$source_epoch" \
    --progress=plain "$FRONTEND_WORK_DIR/image"
