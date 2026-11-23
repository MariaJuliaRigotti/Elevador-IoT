# Formato dos dados do gateway (pedidos, respostas, comando e telemetria), validado com Pydantic

import os
from typing import Literal
from pydantic import BaseModel, Field

# Configuracoes
ANDAR_MAX = int(os.getenv("ANDAR_MAX", "3"))   # Elevador vai do andar 0 ao 3


# Comando gerado pelo tradutor e enviado ao elevador pelo MQTT
class ComandoElevador(BaseModel):

    acao: Literal["mover"]   # Por enquanto, a única ação aceita
    andar: int = Field(ge=0, le=ANDAR_MAX)   # Recusa andares fora de 0 a ANDAR_MAX


# Telemetria recebida do elevador pelo MQTT
# Só aceita os campos conhecidos, com o tipo e a faixa certos, para proteger a consulta de dados falsos
class TelemetriaElevador(BaseModel):

    model_config = {"extra": "ignore"}   # Campos desconhecidos são descartados
    andar_atual: int = Field(ge=0, le=ANDAR_MAX)
    andar_destino: int | None = Field(default=None, ge=0, le=ANDAR_MAX)
    movendo: bool
    direcao: Literal["subindo", "descendo", "parado"]
    porta_aberta: bool
    ts: float | None = None


# Pedido do /comando: uma frase para o elevador
class PedidoTexto(BaseModel):

    texto: str = Field(min_length=1, max_length=500, examples=["sobe o elevador pro andar 3"])
    aguardar: bool = Field(
        default=True,
        description="true: espera o elevador confirmar. false: responde 202 logo após publicar.",
    )


# Pedido do /chat: a mensagem do usuário e o domínio que deve atendê-la
class PedidoChat(BaseModel):

    dominio: str = Field(examples=["auto"])   # "comando", "consulta" ou "auto" (o roteador escolhe)
    mensagem: str = Field(min_length=1, max_length=500, examples=["em que andar o elevador está?"])
    max_tokens_resposta: int | None = Field(default=None, ge=1, le=2048, description="Limite de tokens de resposta para consulta; usado tambem como estimativa de prioridade.")
    aguardar: bool = Field(
        default=True,
        description="Usado só no domínio comando: true espera o elevador confirmar.",
    )


# Resposta de um comando
class Resposta(BaseModel):

    request_id: str   # Liga o comando ao evento de resposta do elevador
    status: Literal["concluido", "aceito", "rejeitado", "timeout"]
    comando: dict   # Tópico e payload publicados no MQTT
    detalhe: dict | None = None   # Evento recebido do elevador (chegou ou rejeitado)
    tradutor: str   # Tradutor usado (regras, ollama ou llm)
    modelo: str | None = None   # Modelo escolhido pelo roteador (só no domínio auto)
    rota: dict | None = None   # Decisão do roteador: domínio, complexidade, método e tempo de classificação
    tempo_ms: float   # Tempo total do pedido, em milissegundos
    espera_fila_ms: float = 0   # Tempo que o pedido esperou nas filas


# Pedido do /rota: só classifica a mensagem, sem executar nada (usado na avaliação do roteador)
class PedidoRota(BaseModel):

    mensagem: str = Field(min_length=1, max_length=500, examples=["desce dois andares"])
    metodo: Literal["hibrido", "llm", "regras"] | None = Field(
        default=None,
        description="Sobrescreve o classificador padrão (variável ROTEADOR). Útil para comparar os métodos.",
    )