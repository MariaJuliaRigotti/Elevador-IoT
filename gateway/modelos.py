"""Modelos de dados validados com Pydantic."""

import os
from typing import Literal, Union

from pydantic import BaseModel, Field

ANDAR_MAX = int(os.getenv("ANDAR_MAX", "5"))
LUZES = ("corredor", "sala", "cozinha", "quarto")


# ---- Comandos estruturados (saída do tradutor) ----

class ComandoElevador(BaseModel):
    dispositivo: Literal["elevador"]
    acao: Literal["mover"]
    andar: int = Field(ge=0, le=ANDAR_MAX)


class ComandoLuz(BaseModel):
    dispositivo: Literal["luz"]
    acao: Literal["ligar", "desligar"]
    local: Literal["corredor", "sala", "cozinha", "quarto"]


Comando = Union[ComandoElevador, ComandoLuz]


# ---- API HTTP ----

class PedidoTexto(BaseModel):
    texto: str = Field(min_length=1, max_length=500, examples=["sobe o elevador pro andar 3"])
    aguardar: bool = Field(
        default=True,
        description="true: espera o dispositivo confirmar. false: responde 202 logo após publicar.",
    )


class Resposta(BaseModel):
    request_id: str
    status: Literal["concluido", "aceito", "rejeitado", "timeout"]
    comando: dict
    detalhe: dict | None = None
    tradutor: str
    tempo_ms: float
