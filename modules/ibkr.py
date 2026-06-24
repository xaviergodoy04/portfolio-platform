"""
Módulo de conexión con Interactive Brokers via Flex Query API.
Documentación: https://www.interactivebrokers.com/en/software/am/am/reports/activityflexqueries.htm

El Flex Web Service es un proceso ASÍNCRONO de dos pasos:
  1. SendRequest  → IBKR encola la generación y devuelve un ReferenceCode.
  2. GetStatement → se descarga usando el ReferenceCode, pero recién está listo
     unos segundos después. Mientras se genera, IBKR responde con un "warn".
Además, si el servicio está ocupado / rate-limited, el propio SendRequest puede
devolver el error 1001 "Statement could not be generated at this time" — que es
TRANSITORIO. Por eso reintentamos ambos pasos con backoff.
"""
import requests
import time
import xml.etree.ElementTree as ET
from datetime import datetime


FLEX_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
FLEX_DOWNLOAD_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"

# Códigos de error de IBKR que son temporales → conviene reintentar.
# 1001: no se pudo generar ahora · 1009: generación en curso · 1018/1019: too many requests
RETRYABLE_CODES = {"1001", "1009", "1018", "1019"}


def _flex_get(url: str, params: dict) -> ET.Element:
    """Hace el GET y parsea el XML de respuesta del Flex service."""
    resp = requests.get(url, params=params, timeout=30)
    return ET.fromstring(resp.text)


def fetch_flex_report(token: str, query_id: str) -> dict:
    """
    Obtiene el reporte Flex de IBKR con las posiciones actuales.
    Reintenta en los errores transitorios típicos del Flex Web Service.
    Retorna dict con posiciones o {"error": ...}.
    """
    if not token or token == "TU_TOKEN_FLEX_QUERY_AQUI":
        return {"error": "IBKR Flex Query no configurado. Completá config.py"}

    try:
        # ── Paso 1: solicitar el reporte (con reintentos ante 1001/1009) ──
        reference_code = None
        last_error = None
        for attempt in range(5):
            root = _flex_get(FLEX_URL, {"t": token, "q": query_id, "v": 3})
            status = root.findtext(".//Status")

            if status == "Success":
                reference_code = root.findtext(".//ReferenceCode")
                break

            code = root.findtext(".//ErrorCode")
            last_error = root.findtext(".//ErrorMessage") or "Error desconocido en IBKR"
            if code not in RETRYABLE_CODES:
                # Error definitivo (token inválido/expirado, query inexistente, etc.)
                return {"error": f"IBKR ({code}): {last_error}"}
            # Transitorio: esperar y reintentar
            time.sleep(2 + attempt * 2)

        if not reference_code:
            return {"error": f"IBKR no pudo generar el reporte tras varios intentos. "
                             f"Último mensaje: {last_error}. Suele ser temporal — probá de nuevo en un minuto."}

        # ── Paso 2: descargar el reporte (polling hasta que esté listo) ──
        for attempt in range(6):
            time.sleep(2 + attempt)
            root = _flex_get(FLEX_DOWNLOAD_URL, {"t": token, "q": reference_code, "v": 3})

            # Si ya vino el statement, lo parseamos
            if root.tag == "FlexQueryResponse" or root.find(".//FlexStatement") is not None \
               or root.find(".//OpenPosition") is not None:
                return parse_flex_xml(ET.tostring(root, encoding="unicode"))

            status = root.findtext(".//Status")
            if status == "Success":
                return parse_flex_xml(ET.tostring(root, encoding="unicode"))

            code = root.findtext(".//ErrorCode")
            msg = root.findtext(".//ErrorMessage") or ""
            if code and code not in RETRYABLE_CODES:
                return {"error": f"IBKR ({code}) al descargar: {msg}"}
            # si no, seguir esperando

        return {"error": "El reporte de IBKR se solicitó pero no terminó de generarse a tiempo. "
                        "Probá de nuevo en unos segundos."}

    except requests.exceptions.Timeout:
        return {"error": "IBKR tardó demasiado en responder (timeout). Probá de nuevo."}
    except Exception as e:
        return {"error": f"Error conectando con IBKR: {str(e)}"}


def parse_flex_xml(xml_text: str) -> dict:
    """Parsea el XML del Flex Report y extrae posiciones."""
    try:
        root = ET.fromstring(xml_text)
        positions = []

        for pos in root.findall(".//OpenPosition"):
            symbol = pos.get("symbol", "")
            if not symbol:
                continue

            positions.append({
                "symbol": symbol,
                "asset_class": pos.get("assetClass", "STK"),
                "quantity": float(pos.get("position", 0)),
                "avg_cost": float(pos.get("costBasisPrice", 0)),
                "mark_price": float(pos.get("markPrice", 0)),
                "position_value": float(pos.get("positionValue", 0)),
                "unrealized_pnl": float(pos.get("unrealizedPnL", 0)),
                "currency": pos.get("currency", "USD"),
            })

        # Calcular métricas del portfolio
        total_value = sum(p["position_value"] for p in positions)
        total_pnl = sum(p["unrealized_pnl"] for p in positions)

        return {
            "positions": positions,
            "summary": {
                "total_value": total_value,
                "total_unrealized_pnl": total_pnl,
                "total_pnl_pct": (total_pnl / (total_value - total_pnl) * 100) if total_value != total_pnl else 0,
                "num_positions": len(positions),
                "as_of": datetime.now().isoformat()
            }
        }
    except Exception as e:
        return {"error": f"Error parseando XML de IBKR: {str(e)}"}


def parse_csv_upload(csv_content: str) -> dict:
    """
    Alternativa: parsear un CSV exportado manualmente desde IB.
    Soporta el formato de Activity Statement de IB.
    """
    import io
    import pandas as pd

    try:
        lines = csv_content.split("\n")
        # Buscar sección de "Open Positions"
        start_idx = None
        for i, line in enumerate(lines):
            if "Open Positions" in line and "Header" in line:
                start_idx = i + 1
                break

        if start_idx is None:
            return {"error": "No se encontró sección 'Open Positions' en el CSV"}

        # Leer hasta la siguiente sección
        data_lines = []
        for line in lines[start_idx:]:
            if line.startswith("Open Positions,Data,"):
                data_lines.append(line.replace("Open Positions,Data,", ""))
            elif data_lines:  # si ya empezamos y encontramos otra sección, parar
                break

        if not data_lines:
            return {"error": "No se encontraron datos de posiciones en el CSV"}

        header_line = lines[start_idx].replace("Open Positions,Header,", "")
        headers = [h.strip() for h in header_line.split(",")]

        df = pd.read_csv(io.StringIO("\n".join([header_line] + data_lines)))

        positions = []
        for _, row in df.iterrows():
            try:
                positions.append({
                    "symbol": str(row.get("Symbol", row.get("Financial Instrument", ""))).strip(),
                    "asset_class": str(row.get("Asset Class", "STK")).strip(),
                    "quantity": float(str(row.get("Quantity", 0)).replace(",", "")),
                    "avg_cost": float(str(row.get("Cost Price", row.get("Average Cost", 0))).replace(",", "")),
                    "mark_price": float(str(row.get("Close Price", row.get("Mark Price", 0))).replace(",", "")),
                    "position_value": float(str(row.get("Value", row.get("Position Value", 0))).replace(",", "")),
                    "unrealized_pnl": float(str(row.get("Unrealized P/L", 0)).replace(",", "")),
                    "currency": str(row.get("Currency", "USD")).strip(),
                })
            except (ValueError, KeyError):
                continue

        total_value = sum(p["position_value"] for p in positions)
        total_pnl = sum(p["unrealized_pnl"] for p in positions)

        return {
            "positions": positions,
            "summary": {
                "total_value": total_value,
                "total_unrealized_pnl": total_pnl,
                "total_pnl_pct": (total_pnl / (total_value - total_pnl) * 100) if (total_value - total_pnl) != 0 else 0,
                "num_positions": len(positions),
                "as_of": datetime.now().isoformat()
            }
        }
    except Exception as e:
        return {"error": f"Error parseando CSV: {str(e)}"}
