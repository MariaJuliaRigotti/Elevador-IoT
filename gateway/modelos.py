"""Modelos de dados validados com Pydantic."""

import os
from typing import Literal

from pydantic import BaseModel, Field

ANDAR_MAX = int(os.getenv("ANDAR_MAX", "5"))


# ---- Comando estruturado (saída do tradutor) ----

class ComandoElevador(BaseModel):
    acao: Literal["mover"]
    andar: int = Field(ge=0, le=ANDAR_MAX)


# ---- API HTTP ----

class PedidoTexto(BaseModel):
    texto: str = Field(min_length=1, max_length=500, examples=["sobe o elevador pro andar 3"])
    aguardar: bool = Field(
        default=True,
        description="true: espera o elevador confirmar. false: responde 202 logo após publicar.",
    )


class Resposta(BaseModel):
    request_id: str
    status: Literal["concluido", "aceito", "rejeitado", "timeout"]
    comando: dict
    detalhe: dict | None = None
    tradutor: str
    tempo_ms: float
