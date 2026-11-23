# Domínio consulta: responde perguntas sobre o elevador usando o estado recebido pela telemetria

import json
import httpx
from dominios import obter_dominio

# Monta o texto com o estado atual do elevador, que vai junto com a pergunta para o LLM
def montar_contexto(estado: dict) -> str:

    elevador = dict(estado.get("elevador") or {})
    elevador.pop("ts", None)   # O horário da telemetria não ajuda a responder
    elevador["online"] = estado.get("elevador_online")
    return json.dumps(elevador, ensure_ascii=False)


# Conecta ao nó da consulta e pede a resposta em streaming
# Se o nó estiver fora do ar, o erro acontece aqui, antes de qualquer token ser enviado ao cliente
async def abrir_stream(pergunta: str, estado: dict, modelo: str | None = None, max_tokens_resposta: int | None = None):

    config = obter_dominio("consulta")
    mensagem = f"Estado do elevador: {montar_contexto(estado)}\nPergunta: {pergunta}"

    cliente = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))   # 5 s para conectar, 60 s por leitura
    opcoes = {"temperature": config["temperatura"]}
    
    if max_tokens_resposta is not None:
        opcoes["num_predict"] = max_tokens_resposta   # Para de gerar ao chegar nesse número de tokens

    pedido = cliente.build_request(
        "POST",
        f"{config['no']}/api/chat",
        json={
            "model": modelo or config["modelo"],   # O roteador pode escolher outro modelo
            "messages": [
                {"role": "system", "content": config["prompt"]},
                {"role": "user", "content": mensagem},
            ],
            "stream": True,   # O Ollama envia a resposta aos poucos, uma linha JSON por token
            "options": opcoes,
        },
    )

    try:
        resposta = await cliente.send(pedido, stream=True)
        resposta.raise_for_status()
    except httpx.HTTPError:
        await cliente.aclose()
        raise

    return cliente, resposta


# Lê a resposta do Ollama linha por linha e devolve cada token assim que ele chega
async def ler_tokens(cliente: httpx.AsyncClient, resposta: httpx.Response, estatisticas: dict):

    try:
        async for linha in resposta.aiter_lines():
            if not linha:
                continue
            dados = json.loads(linha)   # Ex.: {"message": {"content": "O"}, "done": false}
            token = dados.get("message", {}).get("content", "")
            if token:
                yield token
            if dados.get("done"):
                # A última linha traz quantos tokens foram gerados e o tempo de geração (em nanossegundos)
                estatisticas["tokens"] = dados.get("eval_count")
                estatisticas["duracao_geracao_ns"] = dados.get("eval_duration")
                break
    finally:
        # Fecha a conexão mesmo se o cliente desistir no meio da resposta
        await resposta.aclose()
        await cliente.aclose()