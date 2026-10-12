#!/usr/bin/env bash

set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
  echo "Installer must be run as root"
  exit 1
fi

INSTALL_DIR="/usr/bin"
TARGET="$INSTALL_DIR/postgresbackup"
SOURCE="./postgresbackup.py"

echo "Installing postgresbackup..."

if [ ! -f "$SOURCE" ]; then
  echo "Error: $SOURCE not found in current directory"
  exit 1
fi

mkdir -p "$INSTALL_DIR"
cp "$SOURCE" "$TARGET"
chmod +x "$TARGET"

echo "Installed to $TARGET"
