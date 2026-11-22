# Dominio consulta: responde perguntas sobre o predio usando o estado do elevador
# O estado vem da telemetria MQTT recebida pelo gateway
# A resposta do LLM chega em streaming: o Ollama envia um pedaco (token) por vez

import json
import httpx
from dominios import obter_dominio

# Monta o texto com o estado atual do elevador, que sera enviado ao LLM
def montar_contexto(estado: dict) -> str:

    elevador = dict(estado.get("elevador") or {})
    elevador.pop("ts", None)   # O horario da telemetria nao ajuda a responder
    elevador["online"] = estado.get("elevador_online")
    return json.dumps(elevador, ensure_ascii=False)


# Abre a conexao com o no do dominio consulta e pede a resposta em streaming
# Retorna o cliente HTTP e a resposta aberta, que sao lidos depois por ler_tokens
# Se o no estiver fora do ar, gera httpx.HTTPError antes de qualquer token ser enviado
# 'modelo' permite ao roteador semantico escolher o modelo do pedido (padrao: o do dominio consulta)
async def abrir_stream(pergunta: str, estado: dict, modelo: str | None = None, max_tokens_resposta: int | None = None, url_no: str | None = None):

    config = obter_dominio("consulta")
    mensagem = f"Estado do elevador: {montar_contexto(estado)}\nPergunta: {pergunta}"

    cliente = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))   # 5 s para conectar, 60 s por leitura
    pedido = cliente.build_request(
        "POST",
        f"{url_no or config['no']}/api/chat",
        json={
            "model": modelo or config["modelo"],
            "messages": [
                {"role": "system", "content": config["prompt"]},
                {"role": "user", "content": mensagem},
            ],
            "stream": True,   # O Ollama envia a resposta aos poucos, uma linha JSON por token
            "options": {"temperature": config["temperatura"], **({"num_predict": max_tokens_resposta} if max_tokens_resposta is not None else {})},
        },
    )

    try:
        resposta = await cliente.send(pedido, stream=True)
        resposta.raise_for_status()
    except httpx.HTTPError:
        await cliente.aclose()
        raise

    return cliente, resposta


# Le a resposta do Ollama linha por linha e devolve cada token assim que ele chega
# No final, guarda em 'estatisticas' a contagem exata de tokens que o proprio Ollama informa
async def ler_tokens(cliente: httpx.AsyncClient, resposta: httpx.Response, estatisticas: dict):

    try:
        async for linha in resposta.aiter_lines():
            if not linha:
                continue
            dados = json.loads(linha)   # Ex.: {"message": {"content": "O"}, "done": false}
            token = dados.get("message", {}).get("content", "")
            if token:
                yield token
            if dados.get("done"):   # O Ollama avisa quando terminou
                # A ultima mensagem traz quantos tokens foram gerados e quanto tempo levou (em nanossegundos)
                estatisticas["tokens"] = dados.get("eval_count")
                estatisticas["duracao_geracao_ns"] = dados.get("eval_duration")
                break
    finally:
        # Fecha a conexao mesmo se o cliente desistir no meio da resposta
        await resposta.aclose()
        await cliente.aclose()