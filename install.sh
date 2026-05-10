#!/usr/bin/env sh
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
BIN_DIR="$PREFIX/bin"
REPO_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/apple-productivity" <<EOF
#!/usr/bin/env sh
set -eu
exec "$REPO_DIR/apple-productivity" "\$@"
EOF
chmod +x "$BIN_DIR/apple-productivity"

printf 'Installed apple-productivity -> %s/apple-productivity\n' "$BIN_DIR"
printf 'Run: apple-productivity doctor\n'
