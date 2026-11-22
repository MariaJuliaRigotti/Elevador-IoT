# Cliente de linha de comando para testar o gateway (camada de entrada do projeto)
# Frases terminadas em "?" vao para o dominio consulta e a resposta aparece em streaming
# As demais frases sao comandos para o elevador
# Uso:
#   python cliente.py                       # modo interativo
#   python cliente.py "sobe pro andar 3"    # um comando so
#   python cliente.py "onde está o elevador?"

import json
import sys
import httpx

GATEWAY = "http://localhost:8000"   # Endereco do gateway

# Envia um comando ao elevador pelo /comando e mostra o resultado
def enviar_comando(texto: str):

    try:
        r = httpx.post(f"{GATEWAY}/comando", json={"texto": texto}, timeout=90)
    except httpx.HTTPError as e:
        print(f"  ✗ gateway inacessível: {e}")
        return

    dados = r.json()
    if r.status_code >= 400 and "detail" in dados:
        print(f"  ✗ [{r.status_code}] {dados['detail']}")
        return

    cmd = dados["comando"]
    print(f"  → {cmd['topico']}  {cmd['payload']}")
    print(f"  ← {dados['status'].upper()} em {dados['tempo_ms']} ms "
          f"(tradutor: {dados['tradutor']})  {dados.get('detalhe') or ''}")

# Envia uma pergunta ao dominio consulta e imprime cada token assim que ele chega
def enviar_consulta(pergunta: str):

    pedido = {"dominio": "consulta", "mensagem": pergunta}
    try:
        with httpx.stream("POST", f"{GATEWAY}/chat", json=pedido, timeout=90) as r:

            # Erros (400, 422, 503) chegam antes do streaming, como JSON normal
            if r.status_code >= 400:
                r.read()
                print(f"  ✗ [{r.status_code}] {r.json().get('detail')}")
                return

            print("  ← ", end="", flush=True)
            for linha in r.iter_lines():
                # Cada evento SSE chega como uma linha "data: {...}"
                if not linha.startswith("data: "):
                    continue
                dados = json.loads(linha[len("data: "):])

                if "token" in dados:
                    print(dados["token"], end="", flush=True)   # flush: mostra o token na hora
                elif "erro" in dados:
                    print(f"\n  ✗ {dados['erro']}")
                elif dados.get("fim"):
                    print(f"\n  (primeiro token em {dados.get('ttft_ms')} ms, "
                          f"{dados.get('tokens_por_s')} tokens/s, total {dados['tempo_ms']} ms)")
    except httpx.HTTPError as e:
        print(f"\n  ✗ gateway inacessível: {e}")

# Decide se a frase e uma pergunta (consulta) ou um comando
def enviar(texto: str):

    if texto.endswith("?"):
        enviar_consulta(texto)
    else:
        enviar_comando(texto)

def main():

    # Modo de um comando so: a frase vem como argumento
    if len(sys.argv) > 1:
        enviar(" ".join(sys.argv[1:]))
        return

    # Modo interativo
    print("Digite um comando ou uma pergunta terminada em '?' ('estado' / 'sair'):")
    while True:
        try:
            texto = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if texto in ("sair", "exit"):
            break
        if texto == "estado":
            try:
                print(httpx.get(f"{GATEWAY}/estado", timeout=5).json())
            except httpx.HTTPError as e:
                print(f"  ✗ gateway inacessível: {e}")
        elif texto:
            enviar(texto)

if __name__ == "__main__":
    main()