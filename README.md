# Portfolio Platform

Plataforma de gestión de portafolio de inversiones con dashboard en tiempo real, análisis con IA y alertas inteligentes.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Flask](https://img.shields.io/badge/Flask-3.0-green)
![License](https://img.shields.io/badge/License-Private-red)

## Características

- **Dashboard interactivo** — Visualización del portafolio con gráficos (Chart.js), P&L en tiempo real y distribución de activos
- **Integración IBKR** — Importación automática vía Flex Query o carga manual por CSV
- **Datos de mercado** — Cotizaciones y datos históricos en tiempo real vía yfinance
- **Análisis con IA** — Análisis fundamental de activos y chat conversacional usando Groq (gratis) o Anthropic (fallback)
- **Alertas de precio** — Alertas por precio objetivo o por caída porcentual
- **Smart Alerts** — Escaneo automático de oportunidades basado en criterios configurables
- **Radar de oportunidades** — Detección de activos con movimientos significativos
- **Agregador de noticias** — Recolección multi-fuente con enriquecimiento opcional por IA

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
```

> **Nota:** Sin IBKR podés cargar posiciones manualmente o por CSV desde el dashboard.

## Uso

```bash
python app.py
```

Abrir http://localhost:5000 en el navegador.

## API Endpoints

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/api/portfolio` | Portfolio actual (desde IBKR o caché) |
| POST | `/api/portfolio/upload` | Cargar portfolio desde CSV |
| POST | `/api/portfolio/manual` | Ingresar posiciones manualmente |
| GET | `/api/quote/<symbol>` | Cotización de un activo |
| GET | `/api/history/<symbol>` | Datos históricos |
| GET | `/api/compare?symbols=X,Y` | Comparar activos |
| GET | `/api/analyze/<symbol>` | Análisis con IA |
| POST | `/api/chat` | Chat conversacional con IA |
| GET | `/api/alerts` | Listar alertas |
| POST | `/api/alerts` | Crear alerta |
| GET | `/api/alerts/check` | Verificar alertas activas |
| GET | `/api/radar` | Escaneo de oportunidades |
| GET | `/api/smart-alerts/check` | Escanear smart alerts |
| GET | `/api/news` | Noticias agregadas |

## Estructura del proyecto

```
portfolio-platform/
├── app.py                 # Servidor Flask y rutas API
├── config.py              # Configuración y credenciales
├── requirements.txt       # Dependencias Python
├── static/
│   └── index.html         # Frontend SPA (dashboard)
├── modules/
│   ├── ibkr.py            # Integración Interactive Brokers
│   ├── market_data.py     # Datos de mercado (yfinance)
│   ├── ai_analysis.py     # Análisis con IA
│   ├── ai_provider.py     # Abstracción multi-proveedor de IA
│   ├── alerts.py          # Sistema de alertas de precio
│   ├── smart_alerts.py    # Alertas inteligentes automáticas
│   ├── radar.py           # Radar de oportunidades
│   └── news/              # Módulo de noticias
│       ├── collector.py   # Recolección multi-fuente
│       ├── enricher.py    # Enriquecimiento con IA
│       ├── cache.py       # Cache de noticias
│       └── models.py      # Modelos de datos
└── data/                  # Datos persistentes (JSON)
```

## Stack tecnológico

- **Backend:** Flask, yfinance, feedparser
- **Frontend:** HTML/CSS/JS vanilla, Chart.js
- **IA:** Groq (Llama 3.3 70B / Llama 3.1 8B), Anthropic Claude (fallback)
- **Broker:** Interactive Brokers Flex Query API
