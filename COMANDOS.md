# Comandos de terminal — chuleta de la app

Todos se corren parado en la carpeta del proyecto:

```bash
cd /Users/xavier/Desktop/VC/Portafolio_de_Inversiones/portfolio-platform
```

---

## Correr y parar la app

```bash
# Arrancar el server (queda ocupando esa terminal; los logs se ven ahí)
./venv/bin/python app.py

# Pararlo: Ctrl+C en esa terminal

# ¿Quién está usando el puerto 5001? (útil si "Address already in use")
lsof -nP -iTCP:5001 -sTCP:LISTEN

# Matar lo que esté en el 5001 (si quedó un server zombie)
kill $(lsof -t -iTCP:5001 -sTCP:LISTEN)
```

## Actualizar la app (después de que se mergea algo)

```bash
git checkout master   # pararse en la rama principal
git pull              # bajar lo nuevo
# ...y reiniciar el server (Ctrl+C + arrancar de nuevo)
```

El pie del sidebar muestra branch + hash del código corriendo — si actualizaste
y el hash no cambió, te falta reiniciar el server.

## Correr como servicio (arranca sola al login, se relevanta si se cae)

```bash
./deploy/install_launchd.sh      # instalar y arrancar el servicio
./deploy/uninstall_launchd.sh    # detenerlo y desinstalarlo

# Estado del servicio
launchctl print gui/$(id -u)/com.portfolio-platform.app | head -20

# Reiniciarlo (ej: después de un git pull)
launchctl kickstart -k gui/$(id -u)/com.portfolio-platform.app

# Ver sus logs
tail -f data/logs/app.log
tail -f data/logs/app.err.log
```

⚠️ El servicio y el server manual comparten el puerto: usá uno u otro.

## Usuarios (cuentas de la app)

```bash
# Crear un usuario nuevo O resetear la contraseña de uno existente
# (si el usuario ya existe, ofrece resetear)
./venv/bin/python -m modules.admin_create_user
```

## Tailscale Funnel (la URL pública https://...ts.net)

```bash
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale

$TS funnel status              # ¿está activo? ¿qué URL?
$TS funnel --bg 5001           # prenderlo (tras reiniciar la Mac, si no volvió solo)
$TS funnel --https=443 off     # apagarlo (la app deja de ser accesible desde internet)
```

## Diagnóstico rápido

```bash
# ¿El server está vivo y qué versión corre?
curl -s http://localhost:5001/api/version

# Semáforo de dependencias (mercado, IA, feeds, scheduler)
curl -s http://localhost:5001/api/health | python3 -m json.tool

# ¿Qué devolvió IBKR la última vez? (XML crudo del Flex Report)
open data/last_flex_report.xml     # o: head -50 data/last_flex_report.xml
```

## Base de datos y backups

```bash
# Backup manual de la DB, ahora mismo (además del automático de las 18:45)
./venv/bin/python -c "from modules import db; print(db.backup_db())"

# Ver los backups que hay (rotan solos, quedan 14 días)
ls -la data/backups/

# Restaurar un backup (¡con el server APAGADO!)
cp data/backups/portfolio-AAAA-MM-DD.db data/portfolio.db

# Mirar la DB a mano (consultas sueltas)
sqlite3 data/portfolio.db "SELECT date, total_value FROM portfolio_snapshots ORDER BY date;"
sqlite3 data/portfolio.db "SELECT id, username FROM users;"
```

## Git / PRs (flujo de cambios)

```bash
git status                    # ¿hay cambios sin commitear?
git log --oneline -10         # últimos commits
git branch --show-current     # ¿en qué rama estoy?

gh pr list                    # PRs abiertos
gh pr merge <número> --merge  # mergear un PR (regla: en orden, uno por vez)
```

## El .env (configuración)

Variables que viven en `.env` (no está en git): `APP_PORT` (5001),
`APP_HOST` (0.0.0.0 = accesible en la red), `SECRET_KEY` (firma de sesiones —
no la borres o todos pierden la sesión), `GROQ_API_KEY` / `ANTHROPIC_API_KEY`,
`IBKR_FLEX_TOKEN` / `IBKR_FLEX_QUERY_ID`, `DEBUG_MODE`.

Después de tocar el `.env`, reiniciá el server para que aplique.
