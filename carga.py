# Experimento obrigatório: mede TTFT e vazão com 1, 2, 4, 8 e 16 clientes e compara com a previsão do modelo de fila

import argparse
import asyncio
import csv
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
import httpx

# Configuracoes
TOKEN = os.getenv("GATEWAY_TOKEN", "token-experimento-5h2r7t")   # Token com limite alto, para o limite de pedidos não atrapalhar a medição
CABECALHOS = {"Authorization": f"Bearer {TOKEN}"}   # Enviado em todo pedido ao gateway


# Um cliente: envia a pergunta ao /chat e lê a resposta até o evento final com as métricas
async def um_cliente(http: httpx.AsyncClient, args, numero: int, t0: float) -> dict:

    pedido = {"dominio": args.dominio, "mensagem": args.pergunta}

    try:
        async with http.stream("POST", f"{args.gateway}/chat", json=pedido) as r:
            if r.status_code >= 400:
                await r.aread()
                return {"cliente": numero, "erro": f"[{r.status_code}] {r.text[:150]}"}

            async for linha in r.aiter_lines():
                if not linha.startswith("data: "):
                    continue
                dados = json.loads(linha[len("data: "):])
                if "erro" in dados:
                    return {"cliente": numero, "erro": dados["erro"]}
                if dados.get("fim"):
                    dados["cliente"] = numero
                    dados["terminou_s"] = time.perf_counter() - t0   # Instante em que este cliente terminou
                    return dados
    except httpx.HTTPError as e:
        return {"cliente": numero, "erro": f"gateway inacessível: {e}"}

    return {"cliente": numero, "erro": "resposta terminou sem o evento final"}


# Uma rodada: N clientes ao mesmo tempo. Retorna os que deram certo, os que deram erro e a duração da rodada
async def rodada(http: httpx.AsyncClient, args, n: int):

    t0 = time.perf_counter()
    resultados = await asyncio.gather(*[um_cliente(http, args, i + 1, t0) for i in range(n)])
    ok = [r for r in resultados if "erro" not in r]
    erros = [r for r in resultados if "erro" in r]
    duracao = max((r["terminou_s"] for r in ok), default=time.perf_counter() - t0)
    return ok, erros, duracao


# Lê no /saude quantos pedidos o domínio atende ao mesmo tempo (c)
async def obter_vagas(http: httpx.AsyncClient, args) -> int:

    saude = (await http.get(f"{args.gateway}/saude")).json()
    return int(saude["filas"][args.dominio]["limite"])


# Média de uma lista (NaN se estiver vazia, para não quebrar o gráfico)
def media(valores):
    return statistics.mean(valores) if valores else float("nan")


# Desvio padrão entre as repetições (0 se houver uma só)
def desvio(valores):
    return statistics.stdev(valores) if len(valores) > 1 else 0.0


# Resume uma rodada em uma linha: TTFT, vazão total e velocidade por cliente
def resumir(n: int, rep: int, ok: list, erros: list, duracao: float) -> dict:

    tokens_total = sum(r["tokens"] for r in ok)
    por_cliente = [r["tokens_por_s"] for r in ok if r.get("tokens_por_s")]

    return {
        "n": n,
        "rep": rep,
        "ok": len(ok),
        "erros": len(erros),
        "ttft_medio_ms": round(media([r["ttft_ms"] for r in ok]), 1),
        "ttft_max_ms": round(max((r["ttft_ms"] for r in ok), default=float("nan")), 1),
        "espera_media_ms": round(media([r["espera_fila_ms"] for r in ok]), 1),
        "tokens_total": tokens_total,
        "duracao_s": round(duracao, 2),
        "vazao_tok_s": round(tokens_total / duracao, 2) if duracao > 0 else 0,   # Tokens/s somando todos os clientes
        "tok_s_por_cliente": round(media(por_cliente), 2) if por_cliente else "",
    }


# Calcula os parâmetros do modelo de fila a partir de um nível medido (usa a mediana para ignorar rodadas fora do normal)
def parametros(requisicoes: list) -> dict:

    return {
        "ttft0": statistics.median([r["ttft_ms"] - r["espera_fila_ms"] for r in requisicoes]),   # Tempo até o primeiro token, sem contar a fila
        "servico": statistics.median([r["tempo_ms"] - r["espera_fila_ms"] for r in requisicoes]),   # S: tempo em que o pedido ocupa a vaga
        "tokens": statistics.mean([r["tokens"] for r in requisicoes]),
    }


# Prevê o TTFT e a vazão de cada nível; 'p_n1', se vier, é usado para N = 1, que roda sem concorrência
def prever(niveis: list, vagas: int, modelo: str, calibrado_com: int, p: dict, p_n1: dict = None) -> list:

    previsao = []

    for n in niveis:
        q = p_n1 if (p_n1 and n == 1) else p
        esperas = [(k // vagas) * q["servico"] for k in range(n)]   # O cliente k espera floor(k/c) * S
        duracao_s = math.ceil(n / vagas) * q["servico"] / 1000
        previsao.append({
            "n": n,
            "modelo": modelo,
            "calibrado_com_n": calibrado_com,
            "vagas": vagas,
            "servico_ms": round(q["servico"], 1),
            "ttft0_ms": round(q["ttft0"], 1),
            "ttft_medio_prev_ms": round(q["ttft0"] + media(esperas), 1),
            "ttft_max_prev_ms": round(q["ttft0"] + max(esperas), 1),
            "vazao_prev_tok_s": round(n * q["tokens"] / duracao_s, 2),
        })

    return previsao


# Mostra a previsão no terminal
def mostrar_previsao(linhas: list, titulo: str):

    p = linhas[-1]
    print(f"\n{titulo} (S = {p['servico_ms']:.0f} ms, TTFT0 = {p['ttft0_ms']:.0f} ms, c = {p['vagas']}):")

    for p in linhas:
        print(f"  N={p['n']:>2}: TTFT médio previsto {p['ttft_medio_prev_ms']:>8.0f} ms | "
              f"vazão prevista {p['vazao_prev_tok_s']:>6.1f} tok/s")
    print()


# Grava uma lista de dicionários num CSV
def gravar_csv(caminho: Path, linhas: list):

    if not linhas:
        return
    with open(caminho, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0].keys()))
        w.writeheader()
        w.writerows(linhas)


# Lê um CSV e retorna uma lista de dicionários, um por linha
def ler_csv(caminho: Path) -> list:

    with open(caminho, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# Agrupa um campo por N e retorna os níveis, as médias e os desvios entre as repetições
def agrupar(rodadas: list, campo: str):

    por_n = {}

    for r in rodadas:
        if r[campo] != "":
            por_n.setdefault(int(r["n"]), []).append(float(r[campo]))
    ns = sorted(por_n)

    return ns, [media(por_n[n]) for n in ns], [desvio(por_n[n]) for n in ns]


# Gera o gráfico com três painéis: TTFT, vazão total e velocidade por cliente
def grafico(pasta: Path):

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib não está instalado (pip install matplotlib). Os CSVs já foram gravados.")
        return

    rodadas = ler_csv(pasta / "rodadas.csv")
    previsoes = {}   # Para cada modelo, a previsão de cada N

    for p in ler_csv(pasta / "previsao.csv"):
        previsoes.setdefault(p.get("modelo", "ingenuo"), {})[int(p["n"])] = p
    vagas = int(next(iter(next(iter(previsoes.values())).values()))["vagas"])

    estilos = {
        "ingenuo": ("s--", "previsto: S medido com N=1"),
        "calibrado": ("^-.", f"previsto: S medido com N={vagas}"),
    }
    paineis = [
        ("ttft_medio_ms", "ttft_medio_prev_ms", "TTFT médio (ms)", "TTFT x clientes simultâneos"),
        ("vazao_tok_s", "vazao_prev_tok_s", "tokens/s agregados", "Vazão agregada"),
        ("tok_s_por_cliente", None, "tokens/s por cliente", "Velocidade percebida por cliente"),
    ]

    fig, eixos = plt.subplots(1, 3, figsize=(16, 4.5))
    
    for ax, (medido, previsto, rotulo, titulo) in zip(eixos, paineis):
        ns, medias, desvios = agrupar(rodadas, medido)
        ax.errorbar(ns, medias, yerr=desvios, fmt="o-", capsize=4, label="medido (média ± desvio)")
        if previsto:
            for modelo, linhas in previsoes.items():
                estilo, nome = estilos.get(modelo, ("x:", modelo))
                ax.plot(ns, [float(linhas[n][previsto]) for n in ns if n in linhas], estilo, label=nome)
        ax.axvline(vagas, color="gray", linestyle=":", label=f"limite do nó (c = {vagas})")
        ax.set_xscale("log", base=2)
        ax.set_xticks(ns)
        ax.set_xticklabels([str(n) for n in ns])
        ax.minorticks_off()
        ax.set_ylim(bottom=0)   # Eixo começando no zero, para não exagerar pequenas variações
        ax.set_xlabel("clientes simultâneos")
        ax.set_ylabel(rotulo)
        ax.set_title(titulo)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)

    fig.tight_layout()
    fig.savefig(pasta / "grafico.png", dpi=150)
    print(f"Gráfico salvo em {pasta / 'grafico.png'}")


# Roda o experimento: aquece o modelo, mede cada nível, grava as previsões e os resultados
async def executar(args):

    pasta = Path(args.saida)
    pasta.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=args.timeout, headers=CABECALHOS) as http:
        try:
            vagas = args.vagas or await obter_vagas(http, args)
        except (httpx.HTTPError, KeyError) as e:
            sys.exit(f"Não consegui ler o limite do nó em {args.gateway}/saude ({e}). "
                     f"O gateway está no ar? Ou informe com --vagas.")

        niveis = sorted(set(args.niveis) | {1, vagas})   # N=1 e N=c sempre entram, porque calibram as duas previsões

        print(f"Gateway: {args.gateway} | domínio: {args.dominio} | limite do nó (c): {vagas}")
        print(f"Aquecendo o modelo com {vagas} cliente(s) simultâneo(s) "
              f"(a primeira rodada em paralelo costuma ser mais lenta e não deve entrar na medição)...")
        for _ in range(args.aquecimentos):
            ok, erros, _ = await rodada(http, args, vagas)
            if erros:
                sys.exit(f"Falha no aquecimento: {erros[0]['erro']}\n"
                         f"Dica: erro 404 costuma ser modelo não baixado (ollama pull).")

        requisicoes, rodadas, previsao = [], [], []
        p1 = None

        for n in niveis:
            for rep in range(1, args.repeticoes + 1):
                ok, erros, duracao = await rodada(http, args, n)
                if not ok:
                    sys.exit(f"Todos os clientes falharam com N={n}: {erros[0]['erro']}")

                for r in ok:
                    requisicoes.append({
                        "n": n, "rep": rep, "cliente": r["cliente"],
                        "ttft_ms": r["ttft_ms"], "espera_fila_ms": r["espera_fila_ms"],
                        "tempo_ms": r["tempo_ms"], "tokens": r["tokens"],
                        "tokens_por_s": r.get("tokens_por_s", ""),
                    })
                linha = resumir(n, rep, ok, erros, duracao)
                rodadas.append(linha)
                aviso = f"  ({len(erros)} erros)" if erros else ""
                print(f"N={n:>2} rep {rep}/{args.repeticoes}: TTFT médio {linha['ttft_medio_ms']:>8.0f} ms | "
                      f"vazão {linha['vazao_tok_s']:>6.1f} tok/s | duração {linha['duracao_s']:>6.1f} s{aviso}")
                await asyncio.sleep(args.pausa)   # Deixa o nó esvaziar entre as rodadas

            # Depois de N=1: grava a previsão ingênua antes de medir os outros níveis
            if n == 1:
                p1 = parametros([r for r in requisicoes if r["n"] == 1])
                previsao += prever(niveis, vagas, "ingenuo", 1, p1)
                gravar_csv(pasta / "previsao.csv", previsao)
                mostrar_previsao([p for p in previsao if p["modelo"] == "ingenuo"],
                                 "Previsão ingênua gravada antes das demais medições")

            # Depois de N=c: grava a previsão calibrada, com o S medido com pedidos simultâneos
            if n == vagas and vagas > 1:
                pc = parametros([r for r in requisicoes if r["n"] == vagas])
                previsao += prever(niveis, vagas, "calibrado", vagas, pc, p1)
                gravar_csv(pasta / "previsao.csv", previsao)
                mostrar_previsao([p for p in previsao if p["modelo"] == "calibrado"],
                                 f"Previsão calibrada com N={vagas}, gravada antes de medir N > {vagas}")

    gravar_csv(pasta / "requisicoes.csv", requisicoes)
    gravar_csv(pasta / "rodadas.csv", rodadas)

    # Tabela final: medido x previsto
    prev = {(p["modelo"], p["n"]): p for p in previsao}
    ns, ttft, ttft_d = agrupar(rodadas, "ttft_medio_ms")
    _, vazao, _ = agrupar(rodadas, "vazao_tok_s")

    # Formata um valor previsto para a tabela ("-" se não houver)
    def campo(modelo, n, nome, casas=0):
        p = prev.get((modelo, n))
        return f"{p[nome]:.{casas}f}" if p else "-"

    print("\nResumo (média das repetições):")
    print(f"{'N':>3} {'TTFT medido':>12} {'± desvio':>9} {'prev. ingênua':>14} {'prev. calibrada':>16} "
          f"{'vazão medida':>13} {'ingênua':>9} {'calibrada':>10}")
    for i, n in enumerate(ns):
        print(f"{n:>3} {ttft[i]:>9.0f} ms {ttft_d[i]:>6.0f} ms "
              f"{campo('ingenuo', n, 'ttft_medio_prev_ms'):>11} ms {campo('calibrado', n, 'ttft_medio_prev_ms'):>13} ms "
              f"{vazao[i]:>8.1f} tok/s {campo('ingenuo', n, 'vazao_prev_tok_s', 1):>9} "
              f"{campo('calibrado', n, 'vazao_prev_tok_s', 1):>10}")

    grafico(pasta)


# Lê as opções e roda o experimento (ou só refaz o gráfico)
def main():

    ap = argparse.ArgumentParser(description="Experimento de carga do gateway (TTFT e vazão x clientes simultâneos)")
    ap.add_argument("--gateway", default="http://localhost:8000")
    ap.add_argument("--dominio", default="consulta", help="domínio com resposta em streaming")
    ap.add_argument("--pergunta", default="em que andar o elevador está?")
    ap.add_argument("--niveis", type=lambda s: [int(x) for x in s.split(",")], default=[1, 2, 4, 8, 16],
                    help="números de clientes simultâneos, separados por vírgula")
    ap.add_argument("--repeticoes", type=int, default=5, help="repetições por nível")
    ap.add_argument("--vagas", type=int, default=None, help="limite do nó (padrão: lê do /saude)")
    ap.add_argument("--aquecimentos", type=int, default=1, help="rodadas de aquecimento (com c clientes), descartadas")
    ap.add_argument("--pausa", type=float, default=1.0, help="segundos de pausa entre rodadas")
    ap.add_argument("--timeout", type=float, default=600, help="timeout de cada requisição, em segundos")
    ap.add_argument("--saida", default="resultados", help="pasta dos CSVs e do gráfico")
    ap.add_argument("--so-grafico", action="store_true", help="só refaz o gráfico a partir dos CSVs")
    args = ap.parse_args()

    if args.so_grafico:
        grafico(Path(args.saida))
    else:
        asyncio.run(executar(args))


if __name__ == "__main__":
    main()