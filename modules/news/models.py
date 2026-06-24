"""
Modelos Pydantic compartidos entre todos los agentes de noticias.

¿Por qué Pydantic?
  - Cada agente recibe y retorna objetos tipados, no dicts ni strings.
  - Si un agente retorna algo inesperado, Pydantic lo detecta en el momento,
    no 3 pasos después cuando ya es difícil debuggear.
  - El orquestador sabe exactamente qué esperar de cada agente.
"""
from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime


# ── Entrada: noticia cruda del collector ─────────────────────────────────────

class NewsItem(BaseModel):
    """Una noticia tal como viene de la fuente, antes de cualquier análisis."""
    title: str
    summary: str = ""
    url: str
    source: str                  # "Reuters", "TechCrunch AI", etc.
    published: datetime
    relevance_score: float       # 0-10, calculado por el collector sin IA
    symbols_mentioned: list[str] = Field(default_factory=list)  # ["NVDA", "AAPL"]
    section: str = "MERCADOS"    # "IA" o "MERCADOS"
    context: str = ""            # Generado por Haiku: qué pasó + qué implica (2-3 oraciones)


# ── Agente 2: Clasificación ───────────────────────────────────────────────────

class ClassifiedNews(BaseModel):
    """
    Lo que retorna el Agente 2 (Clasificador).
    Toma una NewsItem y la enriquece con categorías y metadatos.
    """
    # Qué tipo de evento es
    category: Literal[
        'MACRO',      # Fed, tasas, inflación, GDP, empleo
        'SECTOR',     # Noticias que afectan un sector entero
        'EMPRESA',    # Earnings, M&A, IPO, escándalo de una empresa
        'GEOPOLITICA',# Guerras, elecciones, sanciones
        'REGULACION', # Leyes, antitrust, regulación de IA
        'CRIPTO',     # Bitcoin, DeFi, etc.
        'OTRO'
    ]

    # Qué sectores se ven afectados (puede ser más de uno)
    affected_sectors: list[Literal[
        'Tech/IA', 'Defensa', 'Salud', 'Finanzas',
        'Energía', 'Consumo', 'Inmobiliario', 'General'
    ]]

    # ¿Qué tan urgente es para tomar acción?
    urgency: Literal['ALTA', 'MEDIA', 'BAJA']

    # Empresas, personas o instituciones clave mencionadas
    entities: list[str]

    # Tono general de la noticia
    sentiment: Literal['POSITIVO', 'NEGATIVO', 'NEUTRO']

    # Resumen en una oración
    one_liner: str


# ── Agente 3: Contexto Histórico ─────────────────────────────────────────────

class HistoricalContext(BaseModel):
    """
    Lo que retorna el Agente 3 (Historiador).
    Conecta la noticia con patrones históricos del mercado.
    """
    # ¿Qué pasó antes en situaciones similares?
    historical_precedents: list[str] = Field(
        description="2-3 eventos históricos similares con qué pasó después"
    )

    # Análisis por plazo temporal
    short_term: str   # 0-3 meses: qué puede pasar
    medium_term: str  # 3-12 meses
    long_term: str    # 1+ años

    # Sectores que históricamente se benefician / perjudican
    sectors_that_benefit: list[str]
    sectors_at_risk: list[str]

    # Confianza en el análisis (depende de qué tan claro es el patrón histórico)
    confidence: Literal['ALTA', 'MEDIA', 'BAJA']


# ── Agente 4: Impacto en Portfolio ───────────────────────────────────────────

class PortfolioImpact(BaseModel):
    """
    Lo que retorna el Agente 4 (Portfolio Impact).
    Analiza cómo esta noticia afecta TUS posiciones específicas.
    """
    # Posiciones afectadas con descripción del impacto
    affected_positions: list[dict]  # [{"symbol": "NVDA", "impact": "positivo", "reason": "..."}]

    # Impacto general en el portfolio
    overall_impact: Literal['POSITIVO', 'NEGATIVO', 'NEUTRO', 'MIXTO']

    # Acción sugerida (no es consejo financiero, es punto de análisis)
    suggested_action: str

    # Urgencia de revisar el portfolio
    review_urgency: Literal['INMEDIATA', 'ESTA_SEMANA', 'MONITOREAR']


# ── Agente 5: Educación ───────────────────────────────────────────────────────

class EducationalContent(BaseModel):
    """
    Lo que retorna el Agente 5 (Educador).
    Convierte la noticia en una lección de inversión.
    """
    # El concepto económico principal de esta noticia
    main_concept: str   # ej: "Curva de rendimientos invertida"

    # Explicación simple (como si nunca hubieras invertido)
    simple_explanation: str

    # Por qué esto importa para un inversor retail
    why_it_matters: str

    # Conceptos relacionados para aprender después
    related_concepts: list[str]

    # Pregunta para reflexionar
    reflection_question: str


# ── Output final del Orquestador ─────────────────────────────────────────────

class NewsAnalysisReport(BaseModel):
    """
    El reporte completo que llega al dashboard.
    Combina los outputs de todos los agentes.
    """
    news: NewsItem
    classification: ClassifiedNews
    historical: HistoricalContext
    portfolio_impact: PortfolioImpact
    education: EducationalContent
    analyzed_at: datetime = Field(default_factory=datetime.now)


class DailyBrief(BaseModel):
    """El brief diario con las N noticias más relevantes analizadas."""
    reports: list[NewsAnalysisReport]
    total_collected: int     # cuántas noticias recolectó el collector
    total_analyzed: int      # cuántas pasaron el filtro de relevancia
    generated_at: datetime = Field(default_factory=datetime.now)
    portfolio_summary: str   # resumen ejecutivo del impacto en el portfolio
