# Testa só as regras do roteador num conjunto de frases rotuladas, sem precisar do gateway nem do Ollama

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "gateway"))   # Permite importar os arquivos da pasta gateway
from roteador import classificar_com_confianca   # noqa: E402


# Classifica cada frase com as regras e mostra os acertos, quantas iriam ao LLM e os erros
def main():

    caminho = sys.argv[1] if len(sys.argv) > 1 else "conjunto_roteador.csv"   # Conjunto padrão, se nenhum for passado
    with open(caminho, newline="", encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))

    n = len(linhas)
    certos = confiantes = certos_conf = falsos_cmd = perdidos = 0
    erros = []
    for l in linhas:
        dom, comp, conf = classificar_com_confianca(l["mensagem"])
        ok = (dom, comp) == (l["dominio"], l["complexidade"])
        certos += ok
        confiantes += conf   # Frases que as regras decidem sozinhas, sem o LLM
        certos_conf += ok and conf
        falsos_cmd += l["dominio"] == "consulta" and dom == "comando"   # O erro perigoso: moveria o elevador
        perdidos += l["dominio"] == "comando" and dom == "consulta"
        if not ok:
            erros.append((conf, l, dom, comp))

    print(f"{caminho}: {n} frases")
    print(f"  acurácia conjunta das regras (sempre decidindo): {certos}/{n} = {certos / n:.1%}")
    print(f"  decididas pelas regras com certeza: {confiantes}/{n} = {confiantes / n:.1%} "
          f"(o resto, {n - confiantes}, iria ao LLM)")
    print(f"  precisão nas frases confiantes: {certos_conf}/{confiantes} = {certos_conf / max(confiantes, 1):.1%}")
    print(f"  falsos comandos: {falsos_cmd} | comandos perdidos: {perdidos}")

    # Separa os erros em que as regras tinham certeza (esses não passam pelo LLM no híbrido)
    for titulo, filtro in (("ERROS CONFIANTES (nao passam pelo LLM)", True), ("erros em frases duvidosas (o LLM decide)", False)):
        grupo = [e for e in erros if e[0] is filtro]
        if grupo:
            print(f"\n{titulo}: {len(grupo)}")
            for _, l, dom, comp in grupo:
                print(f"  {l['mensagem']!r}: esperado {l['dominio']}/{l['complexidade']}, regras {dom}/{comp}")


if __name__ == "__main__":
    main()