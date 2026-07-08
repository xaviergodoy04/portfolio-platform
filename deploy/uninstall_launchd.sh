#!/bin/bash
# Detiene y desinstala el servicio launchd de la app (revierte install_launchd.sh).
set -euo pipefail

LABEL="com.portfolio-platform.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "✅ Servicio $LABEL detenido y desinstalado."
echo "   La app vuelve a correrse a mano con: ./venv/bin/python app.py"
