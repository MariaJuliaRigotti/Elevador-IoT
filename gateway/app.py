"""
Gateway HTTP -> MQTT.

Endpoints:
  POST /comando   texto em linguagem natural -> comando MQTT
  GET  /estado    último estado conhecido dos dispositivos (via telemetria)
  GET  /saude     status do gateway e da conexão com o broker

Documentação interativa: http://localhost:8000/docs
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from modelos import ComandoElevador, ComandoLuz, PedidoTexto, Resposta
from mqtt_cliente import PonteMQTT
from tradutor import ComandoNaoEntendido, TradutorComFallback

TIMEOUT = float(os.getenv("TIMEOUT_CONFIRMACAO", "15"))

ponte = PonteMQTT()
tradutor = TradutorComFallback()


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    ponte.iniciar(asyncio.get_running_loop())
    print(f"[gateway] tradutor: {'llm (com fallback regras)' if tradutor.llm else 'regras'}", flush=True)
    yield
    ponte.parar()


app = FastAPI(title="Gateway IoT por Linguagem Natural", lifespan=ciclo_de_vida)


def para_mqtt(cmd) -> tuple[str, dict]:
    """Converte o comando validado no tópico e payload do PROTOCOLO.md."""
    if isinstance(cmd, ComandoElevador):
        return "casa/elevador/comando", {"acao": "mover", "andar": cmd.andar}
    if isinstance(cmd, ComandoLuz):
        return f"casa/luz/{cmd.local}/comando", {"acao": cmd.acao}
    raise ValueError("comando desconhecido")


@app.post("/comando", response_model=Resposta)
async def comando(pedido: PedidoTexto):
    t0 = time.perf_counter()

    # 1. Linguagem natural -> comando estruturado (já validado)
    try:
        cmd, nome_tradutor = await asyncio.to_thread(tradutor.traduzir, pedido.texto)
    except ComandoNaoEntendido as e:
        raise HTTPException(status_code=422, detail=f"Não entendi: {e}")

    if not ponte.conectado:
        raise HTTPException(status_code=503, detail="Gateway sem conexão com o broker MQTT")
    if isinstance(cmd, ComandoElevador) and ponte.estado["elevador_online"] is False:
        raise HTTPException(status_code=503, detail="Elevador offline")

    # 2. Publica no broker e (opcionalmente) espera a confirmação
    topico, payload = para_mqtt(cmd)
    try:
        request_id, enviado, evento = await ponte.enviar(topico, payload, pedido.aguardar, TIMEOUT)
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # 3. Monta a resposta HTTP
    if not pedido.aguardar:
        status, codigo = "aceito", 202
    elif evento is None:
        status, codigo = "timeout", 504
    elif evento["tipo"] == "rejeitado":
        status, codigo = "rejeitado", 409
    else:
        status, codigo = "concluido", 200

    corpo = Resposta(
        request_id=request_id,
        status=status,
        comando={"topico": topico, "payload": enviado},
        detalhe=evento,
        tradutor=nome_tradutor,
        tempo_ms=round((time.perf_counter() - t0) * 1000, 1),
    )
    return JSONResponse(status_code=codigo, content=corpo.model_dump())


@app.get("/estado")
async def estado():
    return ponte.estado


@app.get("/saude")
async def saude():
    return {
        "broker_conectado": ponte.conectado,
        "elevador_online": ponte.estado["elevador_online"],
        "tradutor": "llm" if tradutor.llm else "regras",
    }
