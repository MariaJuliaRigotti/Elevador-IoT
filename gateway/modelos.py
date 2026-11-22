# Modelos de dados do gateway, validados com Pydantic.
# Definem o formato dos pedidos HTTP, das respostas e do comando enviado ao elevador.

import os
from typing import Literal
from pydantic import BaseModel, Field

# Configuracoes
ANDAR_MAX = int(os.getenv("ANDAR_MAX", "3"))   # Elevador vai do andar 0 ao 3

# Comando estruturado gerado pelo tradutor e enviado ao elevador pelo MQTT
class ComandoElevador(BaseModel):

    acao: Literal["mover"]   # Por enquanto, a unica acao aceita
    andar: int = Field(ge=0, le=ANDAR_MAX)   # Recusa andares fora de 0 a ANDAR_MAX

# Pedido enviado ao /comando: frase em linguagem natural para o elevador
class PedidoTexto(BaseModel):

    texto: str = Field(min_length=1, max_length=500, examples=["sobe o elevador pro andar 3"])
    aguardar: bool = Field(
        default=True,
        description="true: espera o elevador confirmar. false: responde 202 logo após publicar.",
    )

# Pedido enviado ao /chat: o dominio escolhe quem atende e a mensagem e o que o usuario escreveu
class PedidoChat(BaseModel):

    dominio: str = Field(examples=["consulta"])   # Nome do dominio (ex.: "comando" ou "consulta")
    mensagem: str = Field(min_length=1, max_length=500, examples=["em que andar o elevador está?"])
    aguardar: bool = Field(
        default=True,
        description="Usado só no domínio comando: true espera o elevador confirmar.",
    )

# Resposta do /comando com o resultado do pedido
class Resposta(BaseModel):

    request_id: str   # ID que liga o comando ao evento de resposta do elevador
    status: Literal["concluido", "aceito", "rejeitado", "timeout"]
    comando: dict   # Topico e payload publicados no MQTT
    detalhe: dict | None = None   # Evento recebido do elevador (chegou ou rejeitado)
    tradutor: str   # Qual tradutor foi usado (regras, ollama ou llm)
    tempo_ms: float   # Tempo total da requisicao em milissegundos