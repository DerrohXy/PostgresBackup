#!/usr/bin/env bash

set -e

if [[ "$EUID" -ne 0 ]]; then
  echo "Uninstaller must be run as root"
  exit 1
fi

INSTALL_DIR="/usr/bin"
TARGET="$INSTALL_DIR/postgresbackup"

echo "Uninstalling postgresbackup..."

if [[ -f "$TARGET" ]]; then
  rm "$TARGET"
  echo "postgresbackup uninstalled."
else
  echo "postgresbackup installation not found."
fi