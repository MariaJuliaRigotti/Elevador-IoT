# Compara as políticas de fila (fifo e curtas) com pedidos curtos e longos ao mesmo tempo; rode uma vez para cada política

import argparse
import asyncio
import csv
import json
import os
import statistics
import time
from pathlib import Path
import httpx

# Configuracoes
TOKEN = os.getenv("GATEWAY_TOKEN", "token-experimento-5h2r7t")   # Token com limite alto, para o limite de pedidos não atrapalhar o teste
CABECALHOS = {"Authorization": f"Bearer {TOKEN}"}   # Enviado em todo pedido ao gateway
PERGUNTA_LONGA = "Descreva detalhadamente o estado do elevador e suas informacoes."
PERGUNTA_CURTA = "Qual e o andar atual? Responda em uma frase."
COLUNAS = ["cliente", "tipo", "erro", "politica_fila", "max_tokens_resposta", "ttft_ms", "espera_fila_ms", "tempo_ms", "tokens", "tokens_por_s", "duracao_cliente_s"]


# Um cliente: envia a pergunta com o limite de tokens e lê a resposta até o evento final
async def cliente(http: httpx.AsyncClient, url: str, numero: int, tokens: int, pergunta: str, inicio: float) -> dict:

    pedido = {"dominio": "consulta", "mensagem": pergunta, "max_tokens_resposta": tokens}

    try:
        async with http.stream("POST", f"{url}/chat", json=pedido) as resposta:
            if resposta.status_code != 200:
                await resposta.aread()
                return {"cliente": numero, "tipo": tokens, "erro": f"HTTP {resposta.status_code}: {resposta.text[:300]}"}

            async for linha in resposta.aiter_lines():
                if not linha.startswith("data: "):
                    continue
                evento = json.loads(linha[len("data: "):])
                if evento.get("erro"):
                    return {"cliente": numero, "tipo": tokens, "erro": evento["erro"]}
                if evento.get("fim"):
                    return {"cliente": numero, "tipo": tokens, "erro": "",
                            "duracao_cliente_s": round(time.perf_counter() - inicio, 3), **evento}
    except (httpx.HTTPError, ValueError) as e:   # Falha de conexão ou evento mal formado
        return {"cliente": numero, "tipo": tokens, "erro": str(e)}

    return {"cliente": numero, "tipo": tokens, "erro": "sem evento final"}


# Roda o teste com a política que o gateway estiver usando e grava o CSV
async def executar(args):

    limites = httpx.Limits(max_connections=args.clientes + 5)

    async with httpx.AsyncClient(headers=CABECALHOS, timeout=httpx.Timeout(300, connect=10), limits=limites) as http:

        saude = (await http.get(f"{args.gateway}/saude")).json()
        politica = saude["filas"]["consulta"]["politica"]
        print(f"Politica: {politica}")

        # Aquecimento: carrega o modelo antes de medir (não entra no resultado)
        aquecimento = await cliente(http, args.gateway, 0, args.curta, PERGUNTA_CURTA, time.perf_counter())
        if aquecimento["erro"]:
            raise RuntimeError(f"Falha no aquecimento: {aquecimento['erro']}")
        await asyncio.sleep(0.2)

        # Intercala pedidos longos e curtos, com um pequeno intervalo, para os curtos chegarem com o nó ocupado
        tarefas = []
        inicio = time.perf_counter()
        
        for i in range(args.clientes):
            if i % 2 == 0:
                tokens, pergunta = args.longa, PERGUNTA_LONGA
            else:
                tokens, pergunta = args.curta, PERGUNTA_CURTA
            tarefas.append(asyncio.create_task(cliente(http, args.gateway, i + 1, tokens, pergunta, inicio)))
            await asyncio.sleep(args.intervalo_ms / 1000)
        resultados = await asyncio.gather(*tarefas)

    # Grava uma linha por pedido
    pasta = Path(args.saida)
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"{politica}_requisicoes.csv"
    with caminho.open("w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUNAS, extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(resultados)
    print(f"CSV: {caminho}")

    # Resumo por tipo de pedido (curto e longo)
    for tipo in (args.curta, args.longa):
        ok = [r for r in resultados if r["tipo"] == tipo and not r["erro"]]
        if not ok:
            print(f"{tipo} tokens: nenhum sucesso")
            continue
        print(f"{tipo} tokens: {len(ok)} respostas | "
              f"TTFT medio {statistics.mean(r['ttft_ms'] for r in ok):.1f} ms | "
              f"espera media {statistics.mean(r['espera_fila_ms'] for r in ok):.1f} ms | "
              f"latencia media {statistics.mean(r['tempo_ms'] for r in ok):.1f} ms")
    print(f"Erros: {sum(bool(r['erro']) for r in resultados)}")


# Lê as opções e roda o teste
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default=os.getenv("GATEWAY_URL", "http://localhost:8000"))
    parser.add_argument("--clientes", type=int, default=16)
    parser.add_argument("--curta", type=int, default=30, help="max_tokens_resposta dos pedidos curtos")
    parser.add_argument("--longa", type=int, default=200, help="max_tokens_resposta dos pedidos longos")
    parser.add_argument("--intervalo-ms", type=float, default=15, help="intervalo entre o envio de cada pedido")
    parser.add_argument("--saida", default="resultados_filas")
    asyncio.run(executar(parser.parse_args()))