"""
Cliente de linha de comando para testar o gateway.

Uso:
    python cliente.py                      # modo interativo
    python cliente.py "sobe pro andar 3"   # um comando só
"""

import sys

import httpx

GATEWAY = "http://localhost:8000"


def enviar(texto: str):
    r = httpx.post(f"{GATEWAY}/comando", json={"texto": texto}, timeout=30)
    dados = r.json()
    if r.status_code >= 400 and "detail" in dados:
        print(f"  ✗ [{r.status_code}] {dados['detail']}")
        return
    cmd = dados["comando"]
    print(f"  → {cmd['topico']}  {cmd['payload']}")
    print(f"  ← {dados['status'].upper()} em {dados['tempo_ms']} ms "
          f"(tradutor: {dados['tradutor']})  {dados.get('detalhe') or ''}")


def main():
    if len(sys.argv) > 1:
        enviar(" ".join(sys.argv[1:]))
        return
    print("Digite um comando (ou 'estado' / 'sair'):")
    while True:
        try:
            texto = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if texto in ("sair", "exit"):
            break
        if texto == "estado":
            print(httpx.get(f"{GATEWAY}/estado").json())
        elif texto:
            enviar(texto)


if __name__ == "__main__":
    main()
