# Avaliacao do roteamento semantico: compara o classificador por LLM com o classificador por regras
# em um conjunto de frases rotuladas (conjunto_roteador.csv: mensagem, dominio, complexidade)
#
# Mede, para cada metodo:
#   - acuracia do dominio, da complexidade e dos dois juntos
#   - "falsos comandos": frases que NAO eram comando mas foram roteadas para comando (o erro perigoso:
#     poderia mover o elevador sem o usuario pedir) e "comandos perdidos" (erro inofensivo)
#   - custo: tempo gasto so para classificar (mediana e p95), que e somado a todo pedido do dominio auto
#   - quantas vezes o LLM falhou e o gateway caiu para as regras
#
# O endpoint /rota so classifica, nao executa nada: pode rodar sem simulador e sem broker
# Atencao: os rotulos do conjunto sao julgamento humano. Defina-os ANTES de medir e nao ajuste
# prompt ou regras olhando o resultado neste mesmo conjunto (use um conjunto novo para isso)
#
# Uso:
#   python avaliar_roteador.py                          # os 3 metodos, 3 repeticoes por frase
#   python avaliar_roteador.py --conjunto conjunto_roteador_v2.csv --saida resultados_roteador_v2
#   python avaliar_roteador.py --repeticoes 5
#   python avaliar_roteador.py --so-grafico             # refaz so o grafico a partir dos CSVs
#
# Saida (pasta "resultados_roteador_v2" por padrao): avaliacao.csv, resumo.csv, grafico.png

import argparse
import csv
import math
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

METODOS = ["regras", "hibrido", "llm"]


def ler_csv(caminho: Path) -> list:

    with open(caminho, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def gravar_csv(caminho: Path, linhas: list):

    with open(caminho, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0].keys()))
        w.writeheader()
        w.writerows(linhas)


def p95(valores: list) -> float:

    ordenados = sorted(valores)
    return ordenados[math.ceil(0.95 * len(ordenados)) - 1]


# Classifica uma frase 'repeticoes' vezes com um metodo. A decisao vem da 1a chamada (com temperatura 0
# o LLM deve repetir a mesma resposta); as demais servem para medir o tempo e checar a consistencia
def classificar(http: httpx.Client, gateway: str, mensagem: str, metodo: str, repeticoes: int) -> dict:

    respostas, tempos_cliente = [], []
    for _ in range(repeticoes):
        t0 = time.perf_counter()
        r = http.post(f"{gateway}/rota", json={"mensagem": mensagem, "metodo": metodo})
        tempos_cliente.append((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        respostas.append(r.json())

    primeira = respostas[0]
    consistente = all((x["dominio"], x["complexidade"]) == (primeira["dominio"], primeira["complexidade"])
                      for x in respostas)
    return {
        "dominio_obtido": primeira["dominio"],
        "complexidade_obtida": primeira["complexidade"],
        "metodo_usado": primeira["metodo"],
        "bruto": primeira.get("bruto", ""),
        "classificacao_ms": round(statistics.median(x["classificacao_ms"] for x in respostas), 2),
        "viagem_ms": round(statistics.median(tempos_cliente), 1),   # Inclui a ida e volta HTTP ao gateway
        "consistente": consistente,
    }


def resumir(linhas: list) -> list:

    resumo = []
    for metodo in METODOS:
        L = [l for l in linhas if l["metodo"] == metodo]
        if not L:
            continue
        n = len(L)
        acerto_dom = [l["dominio"] == l["dominio_obtido"] for l in L]
        acerto_comp = [l["complexidade"] == l["complexidade_obtida"] for l in L]
        tempos = [float(l["classificacao_ms"]) for l in L]
        resumo.append({
            "metodo": metodo,
            "frases": n,
            "acuracia_dominio": round(sum(acerto_dom) / n, 3),
            "acuracia_complexidade": round(sum(acerto_comp) / n, 3),
            "acuracia_conjunta": round(sum(a and b for a, b in zip(acerto_dom, acerto_comp)) / n, 3),
            "falsos_comandos": sum(l["dominio"] == "consulta" and l["dominio_obtido"] == "comando" for l in L),
            "comandos_perdidos": sum(l["dominio"] == "comando" and l["dominio_obtido"] == "consulta" for l in L),
            "fallbacks_para_regras": sum("fallback" in l["metodo_usado"] for l in L),
            "fracao_enviada_ao_llm": round(sum(l["metodo_usado"] == "llm" for l in L) / n, 3),
            "respostas_inconsistentes": sum(str(l["consistente"]) == "False" for l in L),
            "tempo_classificar_mediana_ms": round(statistics.median(tempos), 2),
            "tempo_classificar_p95_ms": round(p95(tempos), 2),
        })
    return resumo


def mostrar(resumo: list, linhas: list):

    print("\nResumo:")
    print(f"{'método':>7} {'dom.':>6} {'compl.':>7} {'conj.':>6} {'falsos cmd':>11} {'cmd perdidos':>13} "
          f"{'fallbacks':>10} {'no LLM':>7} {'mediana':>9} {'p95':>9}")
    for r in resumo:
        print(f"{r['metodo']:>7} {r['acuracia_dominio']:>6.1%} {r['acuracia_complexidade']:>7.1%} "
              f"{r['acuracia_conjunta']:>6.1%} {r['falsos_comandos']:>11} {r['comandos_perdidos']:>13} "
              f"{r['fallbacks_para_regras']:>10} {r['fracao_enviada_ao_llm']:>7.0%} {r['tempo_classificar_mediana_ms']:>6.1f} ms {r['tempo_classificar_p95_ms']:>6.1f} ms")

    for metodo in METODOS:
        erros = [l for l in linhas if l["metodo"] == metodo and
                 (l["dominio"] != l["dominio_obtido"] or l["complexidade"] != l["complexidade_obtida"])]
        if erros:
            print(f"\nErros do método {metodo} ({len(erros)}):")
            for l in erros:
                print(f"  {l['mensagem']!r}: esperado {l['dominio']}/{l['complexidade']}, "
                      f"obtido {l['dominio_obtido']}/{l['complexidade_obtida']}")


def grafico(pasta: Path):

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib não está instalado (pip install -r requirements-experimento.txt). Os CSVs já foram gravados.")
        return

    resumo = {r["metodo"]: r for r in ler_csv(pasta / "resumo.csv")}
    linhas = ler_csv(pasta / "avaliacao.csv")

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5))

    # Painel 1: acuracia por metodo
    metricas = [("acuracia_dominio", "domínio"), ("acuracia_complexidade", "complexidade"),
                ("acuracia_conjunta", "os dois")]
    presentes = [m for m in METODOS if m in resumo]
    largura = 0.8 / len(presentes)
    for i, metodo in enumerate(presentes):
        valores = [float(resumo[metodo][campo]) * 100 for campo, _ in metricas]
        posicoes = [x + (i - (len(presentes) - 1) / 2) * largura for x in range(len(metricas))]
        barras = ax1.bar(posicoes, valores, largura, label=metodo)
        ax1.bar_label(barras, fmt="%.0f%%", fontsize=8)
    ax1.set_xticks(range(len(metricas)))
    ax1.set_xticklabels([nome for _, nome in metricas])
    ax1.set_ylim(0, 110)
    ax1.set_ylabel("acerto (%)")
    ax1.set_title(f"Acurácia do roteador ({resumo[presentes[0]]['frases']} frases rotuladas)")
    ax1.legend(loc="lower right")
    ax1.grid(axis="y", alpha=0.3)

    # Painel 2: custo de classificar (escala log, porque regras e LLM diferem em ordens de grandeza)
    dados = [[float(l["classificacao_ms"]) for l in linhas if l["metodo"] == m] for m in METODOS
             if any(l["metodo"] == m for l in linhas)]
    ax2.boxplot(dados, tick_labels=[m for m in METODOS if any(l["metodo"] == m for l in linhas)])
    ax2.set_yscale("log")
    ax2.set_ylabel("tempo para classificar (ms, escala log)")
    ax2.set_title("Custo do roteamento (somado a todo pedido do domínio auto)")
    ax2.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(pasta / "grafico.png", dpi=150)
    print(f"Gráfico salvo em {pasta / 'grafico.png'}")


def main():

    ap = argparse.ArgumentParser(description="Avalia o roteamento semântico (LLM x regras)")
    ap.add_argument("--gateway", default="http://localhost:8000")
    ap.add_argument("--token", default=os.getenv("TOKEN_GATEWAY", ""), help="token Bearer para /rota (ou variável TOKEN_GATEWAY)")
    ap.add_argument("--conjunto", default="conjunto_roteador.csv")
    ap.add_argument("--repeticoes", type=int, default=3, help="chamadas por frase e método (mede tempo e consistência)")
    ap.add_argument("--saida", default="resultados_roteador_v2")
    ap.add_argument("--metodos", default=",".join(METODOS), help="metodos a avaliar, separados por virgula")
    ap.add_argument("--so-grafico", action="store_true")
    args = ap.parse_args()

    metodos = [m.strip() for m in args.metodos.split(",") if m.strip()]
    desconhecidos = set(metodos) - set(METODOS)
    if desconhecidos:
        sys.exit(f"método(s) desconhecido(s): {', '.join(sorted(desconhecidos))} (use: {', '.join(METODOS)})")

    pasta = Path(args.saida)
    if args.so_grafico:
        grafico(pasta)
        return
    pasta.mkdir(parents=True, exist_ok=True)

    conjunto = ler_csv(Path(args.conjunto))
    print(f"{len(conjunto)} frases | gateway: {args.gateway} | {args.repeticoes} repetições por frase e método")

    linhas = []
    if not args.token:
        sys.exit("Informe o token com --token ou defina TOKEN_GATEWAY no ambiente.")

    with httpx.Client(timeout=60, headers={"Authorization": f"Bearer {args.token}"}) as http:
        try:
            # Aquecimento: a primeira chamada ao LLM carrega o modelo na memoria e nao deve entrar na medicao
            print("Aquecendo o classificador...")
            for _ in range(2):
                http.post(f"{args.gateway}/rota", json={"mensagem": "onde está o elevador?", "metodo": "llm"}).raise_for_status()

            for metodo in metodos:
                for i, item in enumerate(conjunto, 1):
                    res = classificar(http, args.gateway, item["mensagem"], metodo, args.repeticoes)
                    linhas.append({"metodo": metodo, "mensagem": item["mensagem"],
                                   "dominio": item["dominio"], "complexidade": item["complexidade"], **res})
                print(f"  método {metodo}: {len(conjunto)} frases classificadas")
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                sys.exit(f"Acesso negado pelo gateway (HTTP {e.response.status_code}). Verifique o token e as permissões para /rota.")
            sys.exit(f"Erro HTTP do gateway em {args.gateway}: {e}")
        except httpx.RequestError as e:
            sys.exit(f"Falha de conexão com o gateway em {args.gateway}: {e}")

    gravar_csv(pasta / "avaliacao.csv", linhas)
    resumo = resumir(linhas)
    gravar_csv(pasta / "resumo.csv", resumo)
    mostrar(resumo, linhas)
    grafico(pasta)


if __name__ == "__main__":
    main()