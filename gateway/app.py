# Gateway do elevador. Recebe pedidos por HTTP e envia comandos ao elevador (MQTT) ou perguntas ao LLM

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
from fila import FILAS, FILAS_NO, liberar_vaga, ocupar_vaga
from metricas import metricas
from modelos import PedidoChat, PedidoRota, PedidoTexto, Resposta
from mqtt_cliente import PREFIXO, PonteMQTT
from roteador import classificar
from seguranca import autenticar, autorizar, exigir_admin, limitar_taxa, verificar_acesso
from tradutor import ComandoNaoEntendido, TradutorComFallback

# Configuracoes
TIMEOUT = float(os.getenv("TIMEOUT_CONFIRMACAO", "15"))   # Tempo máximo esperando o elevador confirmar o comando
TOPICO_COMANDO = f"{PREFIXO}elevador/comando"   # Tópico em que o gateway publica os comandos
TOKENS_COMANDO = 20   # Tamanho estimado da resposta de um comando, usado na prioridade da fila

ponte = PonteMQTT()   # Conexão com o broker MQTT
tradutor = TradutorComFallback()   # Transforma a frase em comando 


# Retorna o nome do tradutor em uso
def nome_tradutor_ativo():

    if tradutor.llm:
        return tradutor.llm.nome
    return "regras"


# Roda quando o gateway liga e quando desliga: conecta e desconecta do broker
@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):

    ponte.iniciar(asyncio.get_running_loop())
    print(f"[gateway] prefixo MQTT: '{PREFIXO}'", flush=True)

    if tradutor.llm:
        print(f"[gateway] tradutor: {tradutor.llm.nome} (com fallback regras)", flush=True)
    else:
        print("[gateway] tradutor: regras", flush=True)

    yield   

    ponte.parar()

app = FastAPI(title="ElevadorIoT - Gateway por Linguagem Natural", lifespan=ciclo_de_vida)


# Recebe uma frase, transforma em comando e envia ao elevador
# Antes, confere se o usuário pode mover o elevador e se não passou do limite de pedidos
@app.post("/comando", response_model=Resposta)
async def comando(pedido: PedidoTexto, usuario: dict = Depends(autenticar)):
    verificar_acesso(usuario, "comando")
    return await executar_comando(pedido)


# Executa um comando do começo ao fim: traduz, publica no MQTT e espera o elevador responder
# Quando vem do domínio auto, 'modelo' e 'rota' trazem a escolha feita pelo roteador
async def executar_comando(pedido: PedidoTexto, modelo: str | None = None, rota: dict | None = None):

    inicio = time.perf_counter()   # Para medir o tempo total do pedido
    horario_inicio = time.time()   # Horário de chegada, usado no cálculo da vazão

    # O andar atual ajuda o LLM a entender pedidos como "desce um andar"
    andar_atual = (ponte.estado.get("elevador") or {}).get("andar_atual")

    # Espera a vez na fila do comando e, se o tradutor usa o Ollama, também na fila do nó
    fila = FILAS["comando"]
    no = obter_dominio("comando")["no"]
    usa_no = nome_tradutor_ativo() == "ollama"   # As regras e a API externa não usam o nó local

    if usa_no:
        espera = await ocupar_vaga(fila, no, TOKENS_COMANDO)
    else:
        espera = await fila.entrar(TOKENS_COMANDO)

    # Traduz a frase em comando 
    try:
        cmd, nome_tradutor = await asyncio.to_thread(tradutor.traduzir, pedido.texto, andar_atual, modelo)
    except ComandoNaoEntendido as e:
        raise HTTPException(status_code=422, detail=f"Não entendi: {e}")
    finally:
        # Libera a vaga logo depois de traduzir, sem esperar o elevador chegar
        if usa_no:
            liberar_vaga(fila, no)
        else:
            fila.sair()

    # Verifica se o gateway está conectado ao broker
    if not ponte.conectado:
        raise HTTPException(status_code=503, detail="Gateway sem conexão com o broker MQTT")
    # Verifica se o elevador está online (tópico online e Last Will)
    if ponte.estado["elevador_online"] is False:
        raise HTTPException(status_code=503, detail="Elevador offline")

    # Publica o comando e, se o cliente pediu, espera a confirmação do elevador
    topico = TOPICO_COMANDO
    payload = cmd.model_dump()
    try:
        request_id, enviado, evento = await ponte.enviar(topico, payload, pedido.aguardar, TIMEOUT)
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Escolhe o status e o código HTTP de acordo com a resposta do elevador
    if not pedido.aguardar:
        status, codigo = "aceito", 202   # Publicou e respondeu sem esperar
    elif evento is None:
        status, codigo = "timeout", 504   # O elevador não respondeu a tempo
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

    # Guarda as métricas do pedido (o comando não tem TTFT nem tokens, porque a saída é um JSON curto)
    metricas.registrar(
        "comando",
        inicio=horario_inicio,
        fim=time.time(),
        tempo_ms=corpo.tempo_ms,
        espera_fila_ms=corpo.espera_fila_ms,
    )
    return JSONResponse(status_code=codigo, content=corpo.model_dump())


# Recebe uma mensagem e encaminha para o domínio certo
# No domínio auto, o roteador decide se é comando ou consulta e qual modelo usar
@app.post("/chat")
async def chat(pedido: PedidoChat, usuario: dict = Depends(autenticar)):

    inicio = time.perf_counter()   # No domínio auto, o TTFT inclui o tempo de classificar
    horario_inicio = time.time()   # Horário de chegada, usado no cálculo da vazão

    # Confere o limite de pedidos antes de classificar, para ninguém usar o roteador sem limite
    limitar_taxa(usuario)

    dominio = pedido.dominio
    modelo = None   # None: usa o modelo configurado no domínio
    rota = None

    if dominio == "auto":
        try:
            decisao = await classificar(pedido.mensagem)
        except ValueError as e:
            raise HTTPException(status_code=500, detail=str(e))
        dominio, modelo, rota = decisao.dominio, decisao.modelo, decisao.como_dict()
        print(f"[roteador] {pedido.mensagem!r} -> {dominio}/{decisao.complexidade} "
              f"({decisao.modelo}, {decisao.metodo}, {decisao.classificacao_ms} ms)", flush=True)

    # Verifica se o domínio existe
    try:
        config = obter_dominio(dominio)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=e.args[0])

    # Verifica se o usuário pode usar esse domínio (no auto, o domínio escolhido pelo roteador)
    autorizar(usuario, dominio)

    # Comando: segue o mesmo caminho do /comando
    if config["tipo"] == "comando":
        return await executar_comando(PedidoTexto(texto=pedido.mensagem, aguardar=pedido.aguardar), modelo, rota)

    # Consulta: espera a vez na fila do domínio e depois na fila do nó
    # Na política curtas, quem pediu uma resposta menor (max_tokens_resposta) passa na frente
    fila = FILAS[dominio]
    no = config["no"]
    espera = await ocupar_vaga(fila, no, pedido.max_tokens_resposta or 256)

    # Conecta ao nó antes de começar a responder, para ainda poder devolver o erro 503
    try:
        cliente_http, resposta_ollama = await abrir_stream(
            pedido.mensagem, ponte.estado, modelo, pedido.max_tokens_resposta)
    except httpx.HTTPError as e:
        liberar_vaga(fila, no)
        metricas.registrar_erro(dominio)
        raise HTTPException(status_code=503, detail=f"Nó do domínio {dominio} indisponível: {e}")

    # Responde em streaming; a vaga só é liberada quando a resposta terminar (em enviar_eventos)
    return StreamingResponse(
        enviar_eventos(dominio, cliente_http, resposta_ollama, inicio, horario_inicio, fila, no, espera,
                       modelo, rota, pedido.max_tokens_resposta),
        media_type="text/event-stream",   # SSE: cada evento chega ao cliente assim que é enviado
    )


# Só classifica a mensagem, mostrando o domínio, a complexidade e o modelo que o roteador escolheria
@app.post("/rota")
async def rotear(pedido: PedidoRota, usuario: dict = Depends(autenticar)):

    limitar_taxa(usuario)   
    try:
        decisao = await classificar(pedido.mensagem, pedido.metodo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    return decisao.como_dict()


# Monta um evento SSE
def evento_sse(dados: dict) -> str:
    return f"data: {json.dumps(dados, ensure_ascii=False)}\n\n"


# Repassa ao cliente cada token que chega do Ollama e envia e guarda as métricas da resposta
async def enviar_eventos(dominio: str, cliente_http, resposta_ollama, inicio: float,
                         horario_inicio: float, fila, no: str, espera: float,
                         modelo: str | None = None, rota: dict | None = None,
                         max_tokens_resposta: int | None = None):

    tempo_primeiro_token = None   # Momento em que chegou o primeiro token
    pedacos_recebidos = 0   # Quantos pedaços de texto chegaram (mais ou menos um por token)
    estatisticas = {}   # Recebe do Ollama a contagem exata de tokens, no fim da resposta

    try:
        async for token in ler_tokens(cliente_http, resposta_ollama, estatisticas):
            if tempo_primeiro_token is None:
                tempo_primeiro_token = time.perf_counter()
            pedacos_recebidos += 1
            yield evento_sse({"token": token})
    except (httpx.HTTPError, httpx.StreamError) as e:
        # A resposta já começou com código 200, então o erro só pode ir como um evento
        metricas.registrar_erro(dominio)
        yield evento_sse({"erro": f"conexão com o nó perdida: {e}"})
        return
    finally:
        liberar_vaga(fila, no)   # Libera a vaga em qualquer caso: terminou, deu erro ou o cliente saiu

    # Métricas da resposta: TTFT, tokens por segundo e tempo total
    fim = time.perf_counter()

    resultado = {
        "fim": True,
        "tokens": estatisticas.get("tokens") or pedacos_recebidos,   # Usa a contagem do Ollama, se veio
        "espera_fila_ms": round(espera * 1000, 1),
        "politica_fila": fila.politica,
        "max_tokens_resposta": max_tokens_resposta,
        "tempo_ms": round((fim - inicio) * 1000, 1),
    }

    if rota:   # No domínio auto, conta ao cliente o que o roteador decidiu
        resultado["modelo"] = modelo
        resultado["rota"] = rota

    if tempo_primeiro_token is not None:
        resultado["ttft_ms"] = round((tempo_primeiro_token - inicio) * 1000, 1)

    # Tokens por segundo: usa o tempo de geração medido pelo próprio Ollama se ele tiver enviado
    duracao_ns = estatisticas.get("duracao_geracao_ns")
    if estatisticas.get("tokens") and duracao_ns:
        resultado["tokens_por_s"] = round(estatisticas["tokens"] / (duracao_ns / 1e9), 1)
    elif tempo_primeiro_token is not None and pedacos_recebidos > 1 and fim > tempo_primeiro_token:
        resultado["tokens_por_s"] = round((pedacos_recebidos - 1) / (fim - tempo_primeiro_token), 1)

    # Guarda as métricas para o /metricas
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


# Mostra as métricas de cada domínio (médias, p95 e vazão) e a situação das filas
@app.get("/metricas")
async def ver_metricas():

    return {
        "dominios": metricas.resumo(),
        "filas": {nome: fila.estado() for nome, fila in FILAS.items()},
        "nos": {url: fila.estado() for url, fila in FILAS_NO.items()},
    }


# Apaga as métricas guardadas, usado antes de cada rodada do experimento (só administradores)
@app.delete("/metricas")
async def zerar_metricas(usuario: dict = Depends(autenticar)):

    exigir_admin(usuario)
    metricas.zerar()
    return {"zerado": True}


# Mostra o último estado do elevador recebido pela telemetria
@app.get("/estado")
async def estado():

    return ponte.estado


# Mostra se o gateway está conectado ao broker, se o elevador está online, o tradutor em uso e as filas
@app.get("/saude")
async def saude():

    return {
        "broker_conectado": ponte.conectado,
        "elevador_online": ponte.estado["elevador_online"],
        "tradutor": nome_tradutor_ativo(),
        "filas": {nome: fila.estado() for nome, fila in FILAS.items()},   # Fila de cada domínio
        "nos": {url: fila.estado() for url, fila in FILAS_NO.items()},   # Fila de cada nó Ollama
    }