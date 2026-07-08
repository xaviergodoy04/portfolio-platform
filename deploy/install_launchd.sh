#!/bin/bash
#
# Instala la app como servicio launchd de usuario (macOS).
#
# Por qué: los jobs del scheduler (snapshot 18:30, backup 18:45, alertas)
# solo corren si el server está vivo. Con launchd la app arranca sola al
# login y se relevanta si se cae — la promesa "alertas sin navegador" deja
# de depender de acordarse de correr `python app.py`.
#
# Uso:      ./deploy/install_launchd.sh
# Revertir: ./deploy/uninstall_launchd.sh
#
# El plist se genera acá (no hay template versionado con paths hardcodeados):
# usa el venv y el directorio reales de esta copia del repo.
set -euo pipefail

LABEL="com.portfolio-platform.app"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO_DIR/venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$REPO_DIR/data/logs"

if [ ! -x "$PYTHON" ]; then
  echo "❌ No existe $PYTHON — creá el venv primero (python -m venv venv && ./venv/bin/pip install -r requirements.txt)"
  exit 1
fi

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

# DEBUG_MODE=false: sin reloader de Flask bajo launchd (el reloader relanza
# el proceso y confunde a launchd; además así el scheduler inicia directo).
# launchctl setenv no aplica: EnvironmentVariables del plist pisa al .env
# porque load_dotenv() no sobreescribe variables ya presentes en el entorno.
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$REPO_DIR/app.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DEBUG_MODE</key>
    <string>false</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/app.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/app.err.log</string>
</dict>
</plist>
EOF

# Recargar si ya estaba instalado (idempotente)
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "✅ Servicio instalado y arrancado: $LABEL"
echo "   Plist:  $PLIST"
echo "   Logs:   $LOG_DIR/app.log (+ app.err.log)"
echo "   Estado: launchctl print gui/$(id -u)/$LABEL | head -20"
echo ""
echo "⚠️  Si tenías 'python app.py' corriendo a mano, cerralo: el puerto ya lo usa el servicio."
