# Testa SO a parte de regras do roteador, sem gateway, sem Ollama e sem broker (roda em ~1 s)
# Mostra, para cada conjunto rotulado:
#   - acuracia das regras e quantas frases elas decidem sozinhas (confiantes) ou mandam ao LLM
#   - precisao das regras NAS FRASES EM QUE ELAS TEM CERTEZA (e isso que o hibrido herda)
#   - os erros, separando os que sao "confiantes" (esses nao passam pelo LLM e sao os perigosos)
#
# Uso (da raiz do projeto):
#   python testar_regras_offline.py                              # conjunto_roteador.csv
#   python testar_regras_offline.py conjunto_roteador_v2.csv     # outro conjunto

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "gateway"))
from roteador import classificar_com_confianca   # noqa: E402


def main():

    caminho = sys.argv[1] if len(sys.argv) > 1 else "conjunto_roteador.csv"
    with open(caminho, newline="", encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))

    n = len(linhas)
    certos = confiantes = certos_conf = falsos_cmd = perdidos = 0
    erros = []
    for l in linhas:
        dom, comp, conf = classificar_com_confianca(l["mensagem"])
        ok = (dom, comp) == (l["dominio"], l["complexidade"])
        certos += ok
        confiantes += conf
        certos_conf += ok and conf
        falsos_cmd += l["dominio"] == "consulta" and dom == "comando"
        perdidos += l["dominio"] == "comando" and dom == "consulta"
        if not ok:
            erros.append((conf, l, dom, comp))

    print(f"{caminho}: {n} frases")
    print(f"  acurácia conjunta das regras (sempre decidindo): {certos}/{n} = {certos / n:.1%}")
    print(f"  decididas pelas regras com certeza: {confiantes}/{n} = {confiantes / n:.1%} "
          f"(o resto, {n - confiantes}, iria ao LLM)")
    print(f"  precisão nas frases confiantes: {certos_conf}/{confiantes} = {certos_conf / max(confiantes, 1):.1%}")
    print(f"  falsos comandos: {falsos_cmd} | comandos perdidos: {perdidos}")

    for titulo, filtro in (("ERROS CONFIANTES (nao passam pelo LLM)", True), ("erros em frases duvidosas (o LLM decide)", False)):
        grupo = [e for e in erros if e[0] is filtro]
        if grupo:
            print(f"\n{titulo}: {len(grupo)}")
            for _, l, dom, comp in grupo:
                print(f"  {l['mensagem']!r}: esperado {l['dominio']}/{l['complexidade']}, regras {dom}/{comp}")


if __name__ == "__main__":
    main()
