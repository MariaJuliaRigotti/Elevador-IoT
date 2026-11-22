# Dispara varias consultas ao mesmo tempo para testar a fila de cada no do gateway
# Mostra, para cada cliente, quanto tempo esperou na fila, o TTFT e o tempo total
# Uso:
#   python teste_concorrencia.py          # 5 clientes simultaneos
#   python teste_concorrencia.py 8        # 8 clientes simultaneos

import asyncio
import json
import os
import sys
import time
import httpx

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8000")   # Endereco do gateway
TOKEN = os.getenv("GATEWAY_TOKEN", "token-experimento-5h2r7t")   # Token com limite alto, para o rate limiting nao atrapalhar o teste
CABECALHOS = {"Authorization": f"Bearer {TOKEN}"}
PERGUNTA = "em que andar o elevador está?"

# Um cliente: envia a pergunta e le a resposta em streaming ate o evento final
async def cliente(numero: int, http: httpx.AsyncClient, inicio_geral: float) -> dict:

    pedido = {"dominio": "consulta", "mensagem": PERGUNTA}

    async with http.stream("POST", f"{GATEWAY}/chat", json=pedido) as r:

        if r.status_code >= 400:
            await r.aread()
            return {"cliente": numero, "erro": f"[{r.status_code}] {r.text}"}

        async for linha in r.aiter_lines():

            if not linha.startswith("data: "):
                continue
            dados = json.loads(linha[len("data: "):])
            if dados.get("fim"):
                dados["cliente"] = numero
                dados["terminou_em_s"] = round(time.perf_counter() - inicio_geral, 1)
                return dados
            if "erro" in dados:
                return {"cliente": numero, "erro": dados["erro"]}

    return {"cliente": numero, "erro": "resposta terminou sem o evento final"}

# Consulta o /saude enquanto os clientes estao rodando, para mostrar a fila
async def mostrar_fila(http: httpx.AsyncClient):

    await asyncio.sleep(0.5)   # Espera os pedidos chegarem ao gateway
    saude = (await http.get(f"{GATEWAY}/saude")).json()
    print(f"Fila durante o teste: {saude['filas']['consulta']}\n")

async def main():

    quantidade = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    print(f"Enviando {quantidade} consultas ao mesmo tempo...\n")

    async with httpx.AsyncClient(timeout=300, headers=CABECALHOS) as http:
        
        await http.delete(f"{GATEWAY}/metricas")   # Zera as metricas do gateway: o resumo vale so para esta rodada

        inicio_geral = time.perf_counter()
        tarefas = [cliente(i + 1, http, inicio_geral) for i in range(quantidade)]
        resultados, _ = await asyncio.gather(asyncio.gather(*tarefas), mostrar_fila(http))
        duracao_total = time.perf_counter() - inicio_geral

        resumo = (await http.get(f"{GATEWAY}/metricas")).json()["dominios"].get("consulta", {})

    # Resultado de cada cliente
    print(f"{'cliente':>7} {'espera fila':>12} {'TTFT':>9} {'tokens/s':>9} {'total':>9} {'terminou em':>12}")

    for r in sorted(resultados, key=lambda x: x["cliente"]):
        if "erro" in r:
            print(f"{r['cliente']:>7}  erro: {r['erro']}")
        else:
            print(f"{r['cliente']:>7} {r['espera_fila_ms']:>9.0f} ms {r.get('ttft_ms', 0):>6.0f} ms "
                  f"{r.get('tokens_por_s', 0):>9} {r['tempo_ms']:>6.0f} ms {r['terminou_em_s']:>10} s")

    # Vazao do sistema: tudo o que foi entregue, somando todos os clientes, dividido pelo tempo total
    sucesso = [r for r in resultados if "erro" not in r]
    total_tokens = sum(r["tokens"] for r in sucesso)
    print(f"\nVazão (medida no cliente): {len(sucesso) / duracao_total:.2f} respostas/s, "
          f"{total_tokens / duracao_total:.1f} tokens/s no total ({len(sucesso)} respostas em {duracao_total:.1f} s)")

    # Resumo calculado pelo proprio gateway (GET /metricas)
    if resumo:
        print(f"Gateway (/metricas): TTFT médio {resumo['ttft_ms']['media']} ms (p95 {resumo['ttft_ms']['p95']} ms), "
              f"latência média {resumo['latencia_ms']['media']} ms, vazão {resumo['vazao']['pedidos_por_s']} respostas/s")

if __name__ == "__main__":
    asyncio.run(main())