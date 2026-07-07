# Portfolio Platform

Plataforma personal de gestión de portafolio de inversiones: dashboard con memoria histórica, radar de oportunidades, alertas automáticas con track record, noticias personalizadas y análisis con IA.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Flask](https://img.shields.io/badge/Flask-3.1-green)
![SQLite](https://img.shields.io/badge/SQLite-persistente-lightgrey)
![License](https://img.shields.io/badge/License-Private-red)

## Características

- **Dashboard "Hoy"** — Resumen del portfolio, evolución histórica (gráfico de snapshots diarios), smart alerts sin ver, alertas disparadas y noticias de tus activos, todo en una vista.
- **Explorar** — Buscador de activos con vista de detalle unificada (quote, gráfico, análisis IA, alertas y comparación desde el mismo lugar) + radar de oportunidades con scoring Entry/Growth/Risk.
- **Comparador de activos** — Normalizado a base 100 con período seleccionable (1M a Max), métricas de retorno/volatilidad/Sharpe/**max drawdown/beta vs SPY** y matriz de correlación para detectar diversificación falsa.
- **Watchlist** — Seguí activos sin crear una alerta; se integran automáticamente al radar y al escaneo de smart alerts.
- **Alertas de precio y Smart Alerts** — Verificación server-side vía scheduler (no depende de tener el navegador abierto) + **track record**: cada señal automática se compara contra el rendimiento de SPY en el mismo período.
- **Noticias personalizadas** — Agregador multi-fuente (RSS + Reddit) con 5 secciones, contexto generado por IA **por sección** (no todo de una), feedback de like/leídas, y un ranking que aprende tus preferencias (afinidad por fuente/símbolo/tema).
- **Salud de fuentes** — Health check automático semanal de todos los feeds RSS, con panel visual en Ajustes.
- **Análisis con IA** — Análisis fundamental/técnico por activo y chat conversacional con memoria de contexto, usando Groq (gratis) o Anthropic (fallback), con validación y reintento del JSON de salida.
- **Integración IBKR** — Importación automática vía Flex Query, CSV o carga manual.
- **Persistencia real** — SQLite con backup diario automático (rotación de 14 días); nada se pierde al reiniciar el servidor.

## Requisitos

- Python 3.10+
- API key de [Groq](https://console.groq.com/keys) (gratis) y/o [Anthropic](https://console.anthropic.com/)
- (Opcional) Token de IBKR Flex Query para importar posiciones automáticamente

## Instalación

```bash
git clone https://github.com/xaviergodoy04/portfolio-platform.git
cd portfolio-platform
pip install -r requirements.txt
```

## Configuración

Copiá el archivo de ejemplo y completá con tus API keys:

```bash
cp .env.example .env
```

Editá `.env` con tus credenciales:

```env
# Groq es gratis — registrate en https://console.groq.com/keys
GROQ_API_KEY=tu_groq_api_key

# Anthropic es opcional (pago) — https://console.anthropic.com/
ANTHROPIC_API_KEY=tu_anthropic_api_key

# IBKR solo si tenés cuenta en Interactive Brokers
IBKR_FLEX_TOKEN=tu_token
IBKR_FLEX_QUERY_ID=tu_query_id

# Opcional: puerto del servidor (default 5000)
# En macOS, el puerto 5000 suele estar tomado por AirPlay — usá otro si falla
APP_PORT=5001
```

> **Nota:** Sin IBKR podés cargar posiciones manualmente o por CSV desde Ajustes.

## Uso

```bash
python app.py
```

Abrir `http://localhost:5000` (o el puerto que hayas configurado) en el navegador.

Al arrancar, un scheduler en background (APScheduler) empieza a correr solo: verifica alertas de precio cada 5 minutos, escanea smart alerts según tu configuración, y genera el snapshot diario del portfolio + backup de la base de datos al final del día — todo sin necesidad de tener la pestaña abierta.

## API Endpoints

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/api/portfolio` | Portfolio actual (cache en DB, se hidrata solo al reiniciar) |
| POST | `/api/portfolio/upload` | Cargar portfolio desde CSV |
| POST | `/api/portfolio/manual` | Ingresar posiciones manualmente |
| GET | `/api/portfolio/history` | Evolución histórica (snapshots diarios) |
| GET | `/api/quote/<symbol>` | Cotización de un activo (cacheada) |
| GET | `/api/history/<symbol>` | Datos históricos |
| GET | `/api/compare?symbols=X,Y&period=1y` | Comparar activos: retorno, volatilidad, drawdown, beta, correlación |
| GET | `/api/analyze/<symbol>` | Análisis fundamental/técnico con IA |
| POST | `/api/chat` | Chat conversacional con IA (con memoria e historial) |
| GET | `/api/ai/status` | Proveedor de IA activo y su estado |
| GET/POST | `/api/alerts` | Listar / crear alertas de precio |
| DELETE | `/api/alerts/<id>` | Borrar alerta |
| GET | `/api/alerts/check` | Verificar alertas activas |
| GET/POST | `/api/watchlist` | Listar / agregar símbolos a la watchlist |
| DELETE | `/api/watchlist/<symbol>` | Quitar símbolo de la watchlist |
| GET | `/api/radar` | Escaneo de oportunidades (universo + watchlist) |
| GET | `/api/smart-alerts/check` | Escanear smart alerts |
| GET | `/api/smart-alerts/track-record` | Rendimiento histórico de las señales vs SPY |
| GET/POST | `/api/smart-alerts/config` | Configuración de umbrales del escaneo automático |
| GET | `/api/news` | Noticias agregadas (5 secciones + marcado de portfolio/watchlist) |
| GET | `/api/news?enrich=true&section=X` | Contexto IA para una sección puntual |
| GET | `/api/news/health` | Salud de las fuentes RSS |
| POST | `/api/news/feedback` | Marcar noticia como leída / dar like |
| GET | `/api/news/feedback/stats` | Estadísticas de feedback y afinidad |
| GET | `/api/stats/usage` | Uso de la plataforma por endpoint |

## Estructura del proyecto

```
portfolio-platform/
├── app.py                    # Servidor Flask, rutas API y scheduler
├── config.py                 # Configuración y credenciales
├── requirements.txt          # Dependencias Python
├── static/
│   └── index.html            # Frontend SPA: Hoy · Explorar · Alertas · Noticias · Ajustes
├── modules/
│   ├── db.py                 # Capa de persistencia SQLite (portfolio, alertas, watchlist, backups)
│   ├── scheduler.py           # Jobs en background: alertas, snapshots, backup, salud de feeds
│   ├── ibkr.py                # Integración Interactive Brokers
│   ├── market_data.py         # Fachada de datos de mercado con cache TTL
│   ├── market_provider.py     # Proveedor de datos (yfinance, intercambiable)
│   ├── ai_analysis.py         # Análisis con IA (con validación y retry del JSON)
│   ├── ai_provider.py         # Abstracción multi-proveedor de IA (Groq / Anthropic)
│   ├── alerts.py              # Alertas de precio
│   ├── smart_alerts.py        # Alertas inteligentes automáticas
│   ├── radar.py               # Radar de oportunidades (Entry/Growth/Risk)
│   ├── track_record.py        # Rendimiento histórico de smart alerts vs SPY
│   └── news/
│       ├── collector.py       # Recolección multi-fuente (RSS + Reddit)
│       ├── enricher.py        # Contexto con IA por sección
│       ├── affinity.py        # Ranking personalizado por feedback del usuario
│       ├── health.py          # Health check de fuentes RSS
│       ├── cache.py           # Cache de noticias
│       └── models.py          # Modelos de datos
└── data/
    ├── portfolio.db           # Base de datos SQLite
    └── backups/                # Backups diarios rotados (14 días)
```

## Stack tecnológico

- **Backend:** Flask, SQLite, APScheduler, yfinance, feedparser
- **Frontend:** HTML/CSS/JS vanilla, Chart.js
- **IA:** Groq (Llama 3.3 70B / Llama 3.1 8B), Anthropic Claude (fallback)
- **Broker:** Interactive Brokers Flex Query API

## Roadmap

El plan de producto completo (auditoría UX, transición a móvil, escenarios de evolución, KPIs y riesgos) se mantiene en un documento de planificación separado (`ESTRATEGIA_PRODUCTO_2026.md`), fuera de este repositorio.
