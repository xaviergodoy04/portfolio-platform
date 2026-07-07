"""
AI Provider — capa de abstracción para múltiples proveedores de LLM.

Soporta:
  - groq:      Modelos open source (Llama, Qwen) via Groq Cloud (gratis)
  - anthropic: Claude via Anthropic API (pago)
  - auto:      Intenta Groq primero, fallback a Anthropic
"""

import json
import httpx
import anthropic


# ── Extracción robusta de JSON ────────────────────────────────────────────────

def _balanced_json_candidates(text: str):
    """Genera los substrings con llaves balanceadas de `text`, respetando
    strings JSON (comillas y escapes) para no cortar en llaves dentro de un
    valor. Cada candidato empieza en un '{' y termina en su '}' de cierre."""
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[start:i + 1]
                        break


def extract_first_json(text: str) -> dict | None:
    """
    Extrae el primer objeto JSON válido de la respuesta de un LLM, aunque
    venga rodeado de texto, dentro de un fence ```json ... ``` o con varios
    bloques. Retorna el dict parseado, o None si no hay JSON válido.
    """
    if not text:
        return None
    text = text.strip()

    # 1) Intento directo: la respuesta ES el JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # 2) Buscar bloques de llaves balanceadas (cubre fences y texto alrededor)
    for candidate in _balanced_json_candidates(text):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue

    return None


class AIProvider:
    def __init__(self, provider: str, groq_api_key: str = "",
                 groq_model_analysis: str = "", groq_model_fast: str = "",
                 anthropic_api_key: str = "", anthropic_model: str = ""):
        self.provider = provider
        self.groq_api_key = groq_api_key
        self.groq_model_analysis = groq_model_analysis
        self.groq_model_fast = groq_model_fast
        self.anthropic_api_key = anthropic_api_key
        self.anthropic_model = anthropic_model
        self._groq_base = "https://api.groq.com/openai/v1"

    def _groq_available(self) -> bool:
        return bool(self.groq_api_key and self.groq_api_key != "TU_GROQ_API_KEY_AQUI")

    def _anthropic_available(self) -> bool:
        return bool(self.anthropic_api_key and self.anthropic_api_key != "TU_ANTHROPIC_API_KEY_AQUI")

    def _resolve_provider(self) -> str:
        if self.provider == "auto":
            if self._groq_available():
                return "groq"
            if self._anthropic_available():
                return "anthropic"
            return "none"
        return self.provider

    def generate(self, prompt: str, system_prompt: str = "",
                 max_tokens: int = 1024, tier: str = "analysis",
                 history: list = None) -> str:
        provider = self._resolve_provider()

        if provider == "groq":
            return self._call_groq(prompt, system_prompt, max_tokens, tier, history)
        elif provider == "anthropic":
            return self._call_anthropic(prompt, system_prompt, max_tokens, history)
        else:
            raise RuntimeError("No hay proveedor de IA configurado. "
                               "Configurá GROQ_API_KEY o ANTHROPIC_API_KEY en config.py")

    def generate_with_fallback(self, prompt: str, system_prompt: str = "",
                               max_tokens: int = 1024, tier: str = "analysis",
                               history: list = None) -> tuple[str, str]:
        """Genera texto, con fallback automático. Retorna (respuesta, proveedor_usado).
        `history` (opcional): mensajes previos de la conversación,
        lista de {"role": "user"|"assistant", "content": str}."""
        provider = self._resolve_provider()

        if provider == "groq":
            try:
                result = self._call_groq(prompt, system_prompt, max_tokens, tier, history)
                return result, "groq"
            except Exception:
                if self._anthropic_available():
                    result = self._call_anthropic(prompt, system_prompt, max_tokens, history)
                    return result, "anthropic"
                raise

        if provider == "anthropic":
            result = self._call_anthropic(prompt, system_prompt, max_tokens, history)
            return result, "anthropic"

        raise RuntimeError("No hay proveedor de IA configurado.")

    @staticmethod
    def _clean_history(history: list) -> list:
        """Filtra el historial a mensajes válidos {role: user|assistant, content: str}."""
        if not history:
            return []
        return [
            {"role": m["role"], "content": str(m["content"])}
            for m in history
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and m.get("content")
        ]

    def _call_groq(self, prompt: str, system_prompt: str,
                   max_tokens: int, tier: str, history: list = None) -> str:
        model = self.groq_model_analysis if tier == "analysis" else self.groq_model_fast

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(self._clean_history(history))
        messages.append({"role": "user", "content": prompt})

        response = httpx.post(
            f"{self._groq_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.groq_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=120.0,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def _call_anthropic(self, prompt: str, system_prompt: str,
                        max_tokens: int, history: list = None) -> str:
        client = anthropic.Anthropic(api_key=self.anthropic_api_key)
        messages = self._clean_history(history)
        messages.append({"role": "user", "content": prompt})
        kwargs = {
            "model": self.anthropic_model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        message = client.messages.create(**kwargs)
        return message.content[0].text

    def status(self) -> dict:
        """Retorna estado de los proveedores configurados."""
        info = {
            "active_provider": self._resolve_provider(),
            "groq": {
                "configured": self._groq_available(),
                "model_analysis": self.groq_model_analysis,
                "model_fast": self.groq_model_fast,
            },
            "anthropic": {
                "configured": self._anthropic_available(),
                "model": self.anthropic_model,
            },
        }

        if self._groq_available():
            try:
                response = httpx.get(
                    f"{self._groq_base}/models",
                    headers={"Authorization": f"Bearer {self.groq_api_key}"},
                    timeout=10.0,
                )
                info["groq"]["connected"] = response.status_code == 200
            except Exception:
                info["groq"]["connected"] = False

        return info
