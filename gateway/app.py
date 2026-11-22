# Gateway HTTP -> MQTT. Recebe frases em linguagem natural e publica o comando no broker
# POST /comando   texto em linguagem natural -> comando MQTT
# POST /chat      mensagem + dominio (comando, consulta ou auto)
# POST /rota      so classifica a mensagem (dominio e complexidade), sem executar nada
# GET  /metricas  TTFT, tokens/s, latencia e vazao de cada dominio (DELETE /metricas zera)
# GET  /estado    ultimo estado conhecido do elevador (via telemetria)
# GET  /saude     status do gateway, da conexao com o broker e das filas de cada no
# Fila (fila.py): POLITICA_FILA=fifo (ordem de chegada) ou curtas (respostas menores primeiro)
# Documentacao interativa: http://localhost:8000/docs
# Seguranca (seguranca.py): /comando, /chat, /rota e DELETE /metricas exigem o cabecalho
# "Authorization: Bearer <token>". Os endpoints so de leitura (/metricas, /estado, /saude) sao abertos

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from consulta import abrir_stream, ler_tokens
from dominios import obter_dominio
from fila import FILAS
from metricas import metricas
from modelos import PedidoChat, PedidoRota, PedidoTexto, Resposta
from mqtt_cliente import PREFIXO, PonteMQTT
from roteador import classificar
from seguranca import autenticar, autorizar, exigir_admin, limitar_taxa, verificar_acesso
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
# Exige token valido, permissao para o dominio comando e respeita o limite de pedidos do usuario
@app.post("/comando", response_model=Resposta)
async def comando(pedido: PedidoTexto, usuario: dict = Depends(autenticar)):

    verificar_acesso(usuario, "comando")
    return await executar_comando(pedido)


# Fluxo do comando. 'modelo' e 'rota' vem do roteador semantico (dominio auto); sem eles usa o modelo do dominio
async def executar_comando(pedido: PedidoTexto, modelo: str | None = None, rota: dict | None = None):

    inicio = time.perf_counter()   # Usado para medir o tempo total da requisicao
    horario_inicio = time.time()   # Horario de chegada, usado no calculo da vazao

    # 1. Traduz a frase em um comando estruturado (ja validado)
    # O andar atual vem da telemetria MQTT e ajuda o LLM com comandos relativos ("desce um andar")
    andar_atual = (ponte.estado.get("elevador") or {}).get("andar_atual")

    # Espera a vez na fila do no do dominio comando antes de usar o LLM
    fila = FILAS["comando"]
    espera = await fila.entrar()
    try:
        cmd, nome_tradutor = await asyncio.to_thread(tradutor.traduzir, pedido.texto, andar_atual, modelo)
    except ComandoNaoEntendido as e:
        raise HTTPException(status_code=422, detail=f"Não entendi: {e}")
    finally:
        fila.sair()   # Libera o no assim que a traducao termina (nao espera o elevador andar)

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
        modelo=modelo,
        rota=rota,
        tempo_ms=round((time.perf_counter() - inicio) * 1000, 1),
        espera_fila_ms=round(espera * 1000, 1),
    )

    # Registra as metricas do pedido (o comando nao tem TTFT nem tokens: a saida e um JSON curto)
    metricas.registrar(
        "comando",
        inicio=horario_inicio,
        fim=time.time(),
        tempo_ms=corpo.tempo_ms,
        espera_fila_ms=corpo.espera_fila_ms,
    )
    return JSONResponse(status_code=codigo, content=corpo.model_dump())


# Recebe uma mensagem e o dominio que deve atende-la, e encaminha para o dominio certo
# Com dominio "auto", o roteador semantico decide o dominio e o tamanho do modelo
@app.post("/chat")
async def chat(pedido: PedidoChat, usuario: dict = Depends(autenticar)):

    inicio = time.perf_counter()   # Conta a partir da chegada do pedido: no auto, o TTFT inclui o tempo de classificar
    horario_inicio = time.time()   # Horario de chegada, usado no calculo da vazao

    # Confere o limite de pedidos ANTES de classificar, para ninguem gastar o roteador sem limite
    limitar_taxa(usuario)

    dominio = pedido.dominio
    modelo = None   # None = usa o modelo configurado no dominio
    rota = None

    if dominio == "auto":
        try:
            decisao = await classificar(pedido.mensagem)
        except ValueError as e:
            raise HTTPException(status_code=500, detail=str(e))
        dominio, modelo, rota = decisao.dominio, decisao.modelo, decisao.como_dict()
        print(f"[roteador] {pedido.mensagem!r} -> {dominio}/{decisao.complexidade} "
              f"({decisao.modelo}, {decisao.metodo}, {decisao.classificacao_ms} ms)", flush=True)

    # Verifica se o dominio pedido existe
    try:
        config = obter_dominio(dominio)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=e.args[0])

    # Verifica se o usuario pode usar esse dominio (no auto, o dominio escolhido pelo roteador)
    autorizar(usuario, dominio)

    # Dominio comando: reaproveita o fluxo do /comando (traduz, valida e publica no MQTT)
    if config["tipo"] == "comando":
        return await executar_comando(PedidoTexto(texto=pedido.mensagem, aguardar=pedido.aguardar), modelo, rota)

    # Dominio consulta: responde a pergunta em streaming, usando o estado do elevador vindo da telemetria
    # Espera a vez na fila do no deste dominio
    # Na politica "curtas", o tamanho estimado da resposta (max_tokens_resposta) define a prioridade
    fila = FILAS[dominio]
    espera = await fila.entrar(pedido.max_tokens_resposta or 256)

    try:
        # Conecta ao no ANTES de comecar a resposta, para ainda poder devolver erro 503
        cliente_http, resposta_ollama = await abrir_stream(
            pedido.mensagem, ponte.estado, modelo, pedido.max_tokens_resposta)
    except httpx.HTTPError as e:
        fila.sair()
        metricas.registrar_erro(dominio)
        raise HTTPException(status_code=503, detail=f"Nó do domínio {dominio} indisponível: {e}")

    # A vaga na fila so e liberada quando o streaming terminar (dentro de enviar_eventos)
    return StreamingResponse(
        enviar_eventos(dominio, cliente_http, resposta_ollama, inicio, horario_inicio, fila, espera,
                       modelo, rota, pedido.max_tokens_resposta),
        media_type="text/event-stream",   # SSE: o cliente recebe cada evento assim que ele e enviado
    )


# Classifica a mensagem sem executar nada: mostra qual dominio, complexidade e modelo o roteador escolheria
@app.post("/rota")
async def rotear(pedido: PedidoRota, usuario: dict = Depends(autenticar)):

    limitar_taxa(usuario)   # A classificacao pode usar o LLM, entao tambem conta no limite de pedidos
    try:
        decisao = await classificar(pedido.mensagem, pedido.metodo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return decisao.como_dict()


# Formata um evento SSE: uma linha "data: {...}" seguida de uma linha em branco
def evento_sse(dados: dict) -> str:

    return f"data: {json.dumps(dados, ensure_ascii=False)}\n\n"


# Repassa ao cliente cada token recebido do Ollama e, no final, envia e registra as metricas da resposta
async def enviar_eventos(dominio: str, cliente_http, resposta_ollama, inicio: float,
                         horario_inicio: float, fila, espera: float,
                         modelo: str | None = None, rota: dict | None = None,
                         max_tokens_resposta: int | None = None):

    tempo_primeiro_token = None   # Momento em que o primeiro token chegou
    pedacos_recebidos = 0   # Quantos pedacos de texto chegaram (aproximadamente um por token)
    estatisticas = {}   # Preenchido por ler_tokens com a contagem exata de tokens do Ollama

    try:
        async for token in ler_tokens(cliente_http, resposta_ollama, estatisticas):
            if tempo_primeiro_token is None:
                tempo_primeiro_token = time.perf_counter()
            pedacos_recebidos += 1
            yield evento_sse({"token": token})
    except (httpx.HTTPError, httpx.StreamError) as e:
        # A resposta HTTP ja comecou (codigo 200), entao o erro vai como um evento
        metricas.registrar_erro(dominio)
        yield evento_sse({"erro": f"conexão com o nó perdida: {e}"})
        return
    finally:
        fila.sair()   # Libera a vaga no no: terminou, deu erro ou o cliente desconectou

    # Metricas do Projeto 1: TTFT, tokens por segundo e latencia fim a fim
    fim = time.perf_counter()
    resultado = {
        "fim": True,
        "tokens": estatisticas.get("tokens") or pedacos_recebidos,   # Usa a contagem exata, se o Ollama enviou
        "espera_fila_ms": round(espera * 1000, 1),
        "politica_fila": fila.politica,
        "max_tokens_resposta": max_tokens_resposta,
        "tempo_ms": round((fim - inicio) * 1000, 1),
    }
    if rota:   # Dominio auto: informa ao cliente o que o roteador decidiu
        resultado["modelo"] = modelo
        resultado["rota"] = rota

    if tempo_primeiro_token is not None:
        resultado["ttft_ms"] = round((tempo_primeiro_token - inicio) * 1000, 1)

    # Tokens por segundo: usa o tempo de geracao medido pelo proprio Ollama, se disponivel
    duracao_ns = estatisticas.get("duracao_geracao_ns")
    if estatisticas.get("tokens") and duracao_ns:
        resultado["tokens_por_s"] = round(estatisticas["tokens"] / (duracao_ns / 1e9), 1)
    elif tempo_primeiro_token is not None and pedacos_recebidos > 1 and fim > tempo_primeiro_token:
        resultado["tokens_por_s"] = round((pedacos_recebidos - 1) / (fim - tempo_primeiro_token), 1)

    # Guarda as metricas no gateway para o /metricas
    metricas.registrar(
        dominio,
        inicio=horario_inicio,
        fim=time.time(),
        tokens=resultado["tokens"],
        ttft_ms=resultado.get("ttft_ms"),
        tokens_por_s=resultado.get("tokens_por_s"),
        tempo_ms=resultado["tempo_ms"],
        espera_fila_ms=resultado["espera_fila_ms"],
        politica_fila=fila.politica,
        max_tokens_resposta=max_tokens_resposta,
    )

    yield evento_sse(resultado)


# Retorna as metricas agregadas de cada dominio: medias, p95, vazao e situacao das filas
@app.get("/metricas")
async def ver_metricas():

    return {
        "dominios": metricas.resumo(),
        "filas": {nome: fila.estado() for nome, fila in FILAS.items()},
    }


# Apaga as metricas guardadas (usado antes de cada rodada do experimento); so administradores
@app.delete("/metricas")
async def zerar_metricas(usuario: dict = Depends(autenticar)):

    exigir_admin(usuario)
    metricas.zerar()
    return {"zerado": True}


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
        "filas": {nome: fila.estado() for nome, fila in FILAS.items()},   # Situacao da fila de cada no
    }