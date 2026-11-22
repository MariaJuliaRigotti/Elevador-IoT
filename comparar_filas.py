"""Teste de carga misto para comparar FIFO e prioridade para respostas curtas.
Execute uma vez por politica, reiniciando o gateway entre execucoes.
"""
import argparse
import asyncio
import csv
import json
import os
import statistics
import time
from pathlib import Path
import httpx

async def cliente(http, url, numero, tokens, pergunta, inicio):
    try:
        async with http.stream('POST', url + '/chat', json={
            'dominio': 'consulta', 'mensagem': pergunta,
            'max_tokens_resposta': tokens
        }) as resposta:
            if resposta.status_code != 200:
                await resposta.aread()
                return {'cliente': numero, 'tipo': tokens, 'erro': f'HTTP {resposta.status_code}: {resposta.text[:300]}'}
            async for linha in resposta.aiter_lines():
                if linha.startswith('data: '):
                    evento = json.loads(linha[6:])
                    if evento.get('erro'):
                        return {'cliente': numero, 'tipo': tokens, 'erro': evento['erro']}
                    if evento.get('fim'):
                        return {'cliente': numero, 'tipo': tokens, 'erro': '',
                                'duracao_cliente_s': round(time.perf_counter() - inicio, 3), **evento}
    except Exception as exc:
        return {'cliente': numero, 'tipo': tokens, 'erro': str(exc)}
    return {'cliente': numero, 'tipo': tokens, 'erro': 'sem evento final'}

async def executar(args):
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + os.getenv('GATEWAY_TOKEN', 'token-experimento-5h2r7t')}, timeout=httpx.Timeout(300, connect=10),
                                 limits=httpx.Limits(max_connections=args.clientes + 5)) as http:
        saude = (await http.get(args.gateway + '/saude')).json()
        politica = saude['filas']['consulta']['politica']
        print('Politica:', politica)
        # Aquecimento fora das metricas do teste.
        aquecimento = await cliente(http, args.gateway, 0, args.curta, 'Qual o andar atual do elevador? Responda brevemente.', time.perf_counter())
        if aquecimento['erro']:
            raise RuntimeError(f'Falha no aquecimento: {aquecimento["erro"]}')
        await asyncio.sleep(0.2)
        # Enfileira inicialmente longas; curtas chegam enquanto o no esta ocupado.
        tarefas = []
        inicio = time.perf_counter()
        for i in range(args.clientes):
            tokens = args.longa if i % 2 == 0 else args.curta
            pergunta = ('Descreva detalhadamente o estado do elevador e suas informacoes.' if tokens == args.longa
                        else 'Qual e o andar atual? Responda em uma frase.')
            tarefas.append(asyncio.create_task(cliente(http, args.gateway, i + 1, tokens, pergunta, inicio)))
            await asyncio.sleep(args.intervalo_ms / 1000)
        resultados = await asyncio.gather(*tarefas)
    pasta = Path(args.saida)
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f'{politica}_requisicoes.csv'
    colunas = ['cliente', 'tipo', 'erro', 'politica_fila', 'max_tokens_resposta',
               'ttft_ms', 'espera_fila_ms', 'tempo_ms', 'tokens', 'tokens_por_s', 'duracao_cliente_s']
    with caminho.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=colunas, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(resultados)
    print('CSV:', caminho)
    for tipo in (args.curta, args.longa):
        ok = [r for r in resultados if r['tipo'] == tipo and not r['erro']]
        if not ok:
            print(tipo, 'tokens: nenhum sucesso')
            continue
        print(f'{tipo} tokens: {len(ok)} respostas | TTFT medio {statistics.mean(r["ttft_ms"] for r in ok):.1f} ms | '
              f'espera media {statistics.mean(r["espera_fila_ms"] for r in ok):.1f} ms | '
              f'latencia media {statistics.mean(r["tempo_ms"] for r in ok):.1f} ms')
    print('Erros:', sum(bool(r['erro']) for r in resultados))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gateway', default='http://localhost:8000')
    parser.add_argument('--clientes', type=int, default=16)
    parser.add_argument('--curta', type=int, default=30)
    parser.add_argument('--longa', type=int, default=200)
    parser.add_argument('--intervalo-ms', type=float, default=15)
    parser.add_argument('--saida', default='resultados_filas')
    args = parser.parse_args()
    asyncio.run(executar(args))
