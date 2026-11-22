# Gateway HTTP -> MQTT. Recebe frases em linguagem natural e publica o comando no broker.
# Endpoints:
#   POST /comando   texto em linguagem natural -> comando MQTT
#   GET  /estado    ultimo estado conhecido do elevador (via telemetria)
#   GET  /saude     status do gateway e da conexao com o broker
# Documentacao interativa: http://localhost:8000/docs

import asyncio
import os
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import httpx
from consulta import responder
from dominios import obter_dominio
from modelos import PedidoChat, PedidoTexto, Resposta
from mqtt_cliente import PREFIXO, PonteMQTT
from tradutor import ComandoNaoEntendido, TradutorComFallback

# Configuracoes
TIMEOUT = float(os.getenv("TIMEOUT_CONFIRMACAO", "15"))   # Tempo maximo de espera pela confirmacao do elevador
TOPICO_COMANDO = f"{PREFIXO}elevador/comando"   # Topico pelo qual o gateway envia os comandos ao elevador

ponte = PonteMQTT()   # Conexao com o broker MQTT
tradutor = TradutorComFallback()   # Converte frases em comandos


# Retorna o nome do tradutor em uso, para mostrar no log e no /saude
def nome_tradutor_ativo():

    if tradutor.llm:
        return tradutor.llm.nome
    return "regras"


# Executado ao ligar e ao desligar o gateway: abre e fecha a conexao com o broker
@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):

    ponte.iniciar(asyncio.get_running_loop())
    print(f"[gateway] prefixo MQTT: '{PREFIXO}'", flush=True)
    if tradutor.llm:
        print(f"[gateway] tradutor: {tradutor.llm.nome} (com fallback regras)", flush=True)
    else:
        print("[gateway] tradutor: regras", flush=True)

    yield   # O gateway fica rodando aqui ate ser desligado

    ponte.parar()


app = FastAPI(title="ElevadorIoT - Gateway por Linguagem Natural", lifespan=ciclo_de_vida)


# Recebe uma frase, traduz para comando, publica no MQTT e devolve o resultado
@app.post("/comando", response_model=Resposta)
async def comando(pedido: PedidoTexto):

    inicio = time.perf_counter()   # Usado para medir o tempo total da requisicao

    # 1. Traduz a frase em um comando estruturado (ja validado)
    # O andar atual vem da telemetria MQTT e ajuda o LLM com comandos relativos ("desce um andar")
    andar_atual = (ponte.estado.get("elevador") or {}).get("andar_atual")
    try:
        cmd, nome_tradutor = await asyncio.to_thread(tradutor.traduzir, pedido.texto, andar_atual)
    except ComandoNaoEntendido as e:
        raise HTTPException(status_code=422, detail=f"Não entendi: {e}")

    # Verifica se o gateway esta conectado ao broker
    if not ponte.conectado:
        raise HTTPException(status_code=503, detail="Gateway sem conexão com o broker MQTT")
    # Verifica se o elevador esta online (topico online / Last Will)
    if ponte.estado["elevador_online"] is False:
        raise HTTPException(status_code=503, detail="Elevador offline")

    # 2. Publica no broker e, se pedido, espera o elevador confirmar
    topico = TOPICO_COMANDO
    payload = cmd.model_dump()
    try:
        request_id, enviado, evento = await ponte.enviar(topico, payload, pedido.aguardar, TIMEOUT)
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # 3. Escolhe o status e o codigo HTTP da resposta
    if not pedido.aguardar:
        status, codigo = "aceito", 202   # Publicou e respondeu sem esperar
    elif evento is None:
        status, codigo = "timeout", 504   # O elevador nao respondeu a tempo
    elif evento["tipo"] == "rejeitado":
        status, codigo = "rejeitado", 409   # O elevador recusou o comando
    else:
        status, codigo = "concluido", 200   # O elevador chegou ao andar

    corpo = Resposta(
        request_id=request_id,
        status=status,
        comando={"topico": topico, "payload": enviado},
        detalhe=evento,
        tradutor=nome_tradutor,
        tempo_ms=round((time.perf_counter() - inicio) * 1000, 1),
    )
    return JSONResponse(status_code=codigo, content=corpo.model_dump())


# Recebe uma mensagem e o dominio que deve atende-la, e encaminha para o dominio certo
@app.post("/chat")
async def chat(pedido: PedidoChat):

    # Verifica se o dominio pedido existe
    try:
        config = obter_dominio(pedido.dominio)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=e.args[0])

    # Dominio comando: reaproveita o fluxo do /comando (traduz, valida e publica no MQTT)
    if config["tipo"] == "comando":
        return await comando(PedidoTexto(texto=pedido.mensagem, aguardar=pedido.aguardar))

    # Dominio consulta: responde a pergunta usando o estado do elevador vindo da telemetria
    inicio = time.perf_counter()
    try:
        resposta = await asyncio.to_thread(responder, pedido.mensagem, ponte.estado)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Nó do domínio {pedido.dominio} indisponível: {e}")

    return {
        "dominio": pedido.dominio,
        "resposta": resposta,
        "tempo_ms": round((time.perf_counter() - inicio) * 1000, 1),
    }


# Retorna o ultimo estado do elevador recebido pela telemetria
@app.get("/estado")
async def estado():

    return ponte.estado


# Retorna se o gateway esta conectado ao broker, se o elevador esta online e qual tradutor esta em uso
@app.get("/saude")
async def saude():

    return {
        "broker_conectado": ponte.conectado,
        "elevador_online": ponte.estado["elevador_online"],
        "tradutor": nome_tradutor_ativo(),
    }