#!/usr/bin/env sh
set -eu

DEFAULT_REPOSITORY="docker.io/asmir100/avarch"
DEFAULT_WRAPPER_URL="https://raw.githubusercontent.com/AsmirZukic/avarch/main/bin/avarch"

INSTALL_DIR="${AVARCH_INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${AVARCH_VERSION:-alpha}"
IMAGE="${AVARCH_IMAGE:-$DEFAULT_REPOSITORY:$VERSION}"
WRAPPER_URL="${AVARCH_WRAPPER_URL:-$DEFAULT_WRAPPER_URL}"
SKIP_PULL="${AVARCH_SKIP_PULL:-0}"

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Install Avarch's Docker-backed CLI wrapper.

Environment variables:
  AVARCH_INSTALL_DIR   Install directory for the avarch command. Default: $HOME/.local/bin
  AVARCH_VERSION       Docker image tag to install. Default: alpha
  AVARCH_IMAGE         Full runtime image override. Default: docker.io/asmir100/avarch:alpha
  AVARCH_WRAPPER_URL   URL for the wrapper script. Default: the main branch wrapper
  AVARCH_SKIP_PULL     Set to 1 to skip docker pull during installation.

Example:
  curl -fsSL https://raw.githubusercontent.com/AsmirZukic/avarch/main/scripts/install.sh | sh

  curl -fsSL https://raw.githubusercontent.com/AsmirZukic/avarch/main/scripts/install.sh | AVARCH_VERSION=0.1.0 sh
EOF
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
  "")
    ;;
  *)
    die "unknown argument: $1"
    ;;
esac

case "$IMAGE" in
  *"'"*)
    die "AVARCH_IMAGE cannot contain single quotes"
    ;;
esac

command -v docker >/dev/null 2>&1 || die "docker is required but was not found in PATH"

tmpdir=$(mktemp -d)
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT HUP INT TERM

wrapper_tmp="$tmpdir/avarch-wrapper"
installed_tmp="$tmpdir/avarch"

command -v curl >/dev/null 2>&1 || die "curl is required to download the Avarch wrapper"
curl -fsSL "$WRAPPER_URL" -o "$wrapper_tmp"

if [ "$SKIP_PULL" != "1" ]; then
  docker pull "$IMAGE"
fi

awk -v image="$IMAGE" '
  NR == 1 {
    print
    print "if [ -z \"${AVARCH_IMAGE:-}\" ]; then"
    print "  AVARCH_IMAGE='\''" image "'\''"
    print "fi"
    next
  }
  { print }
' "$wrapper_tmp" > "$installed_tmp"

chmod 0755 "$installed_tmp"
mkdir -p "$INSTALL_DIR"
mv "$installed_tmp" "$INSTALL_DIR/avarch"

printf 'Installed avarch to %s\n' "$INSTALL_DIR/avarch"
printf 'Default runtime image: %s\n' "$IMAGE"

case ":$PATH:" in
  *":$INSTALL_DIR:"*)
    ;;
  *)
    printf 'Add %s to PATH before running avarch.\n' "$INSTALL_DIR"
    ;;
esac
