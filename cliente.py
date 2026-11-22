# Cliente de linha de comando para testar o gateway (camada de entrada do projeto)
# Por padrao, o gateway decide sozinho se a frase e comando ou consulta (dominio "auto", roteamento semantico)
# Com --manual, o proprio cliente decide: frases terminadas em "?" vao para a consulta, as demais sao comandos
# Uso:
#   python cliente.py                       # modo interativo
#   python cliente.py "sobe pro andar 3"    # um comando so
#   python cliente.py "onde está o elevador?"
#   python cliente.py --manual "sobe pro andar 3"
# Para usar outro token (ex.: o do visitante):
#   GATEWAY_TOKEN=token-visitante-9d1w6z python cliente.py

import json
import os
import sys
import httpx

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8000")   # Endereco do gateway
TOKEN = os.getenv("GATEWAY_TOKEN", "token-morador-2m8x4p")   # Token de acesso (ver gateway/usuarios.json)
CABECALHOS = {"Authorization": f"Bearer {TOKEN}"}   # Enviado em todo pedido ao gateway

# Envia um comando ao elevador pelo /comando e mostra o resultado
def enviar_comando(texto: str):

    try:
        r = httpx.post(f"{GATEWAY}/comando", json={"texto": texto}, headers=CABECALHOS, timeout=90)
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
        with httpx.stream("POST", f"{GATEWAY}/chat", json=pedido, headers=CABECALHOS, timeout=90) as r:

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

# Mostra a decisao do roteador semantico: dominio, complexidade, modelo escolhido e custo de classificar
def mostrar_rota(rota: dict, modelo: str):

    print(f"  ⇢ roteador: {rota['dominio']}/{rota['complexidade']} → modelo {modelo} "
          f"({rota['metodo']}, classificou em {rota['classificacao_ms']} ms)")

# Envia a frase ao /chat com dominio "auto": o gateway escolhe o dominio e o tamanho do modelo
# A resposta e um JSON (se for comando) ou um streaming SSE (se for consulta)
def enviar_auto(texto: str):

    pedido = {"dominio": "auto", "mensagem": texto}
    try:
        with httpx.stream("POST", f"{GATEWAY}/chat", json=pedido, headers=CABECALHOS, timeout=90) as r:
            if r.status_code >= 400 and "text/event-stream" not in r.headers.get("content-type", ""):
                r.read()
                corpo = r.json()
                if "detail" in corpo:
                    print(f"  ✗ [{r.status_code}] {corpo['detail']}")
                    return
                dados = corpo   # 409 e 504 tambem trazem o corpo normal do comando
            elif "text/event-stream" not in r.headers.get("content-type", ""):
                r.read()
                dados = r.json()
            else:
                dados = None

            # Caminho do comando: resposta JSON unica
            if dados is not None:
                if dados.get("rota"):
                    mostrar_rota(dados["rota"], dados["modelo"])
                cmd = dados["comando"]
                print(f"  → {cmd['topico']}  {cmd['payload']}")
                print(f"  ← {dados['status'].upper()} em {dados['tempo_ms']} ms "
                      f"(tradutor: {dados['tradutor']})  {dados.get('detalhe') or ''}")
                return

            # Caminho da consulta: tokens em streaming; a decisao do roteador chega no evento final
            print("  ← ", end="", flush=True)
            for linha in r.iter_lines():
                if not linha.startswith("data: "):
                    continue
                evento = json.loads(linha[len("data: "):])
                if "token" in evento:
                    print(evento["token"], end="", flush=True)
                elif "erro" in evento:
                    print(f"\n  ✗ {evento['erro']}")
                elif evento.get("fim"):
                    print(f"\n  (primeiro token em {evento.get('ttft_ms')} ms, "
                          f"{evento.get('tokens_por_s')} tokens/s, total {evento['tempo_ms']} ms)")
                    if evento.get("rota"):
                        mostrar_rota(evento["rota"], evento["modelo"])
    except httpx.HTTPError as e:
        print(f"\n  ✗ gateway inacessivel: {e}")

# Modo manual: decide aqui se a frase e uma pergunta (consulta) ou um comando
def enviar_manual(texto: str):

    if texto.endswith("?"):
        enviar_consulta(texto)
    else:
        enviar_comando(texto)

MODO_MANUAL = "--manual" in sys.argv   # O roteamento fica no gateway, a menos que se peca o modo manual

def enviar(texto: str):

    if MODO_MANUAL:
        enviar_manual(texto)
    else:
        enviar_auto(texto)

def main():

    # Modo de um comando so: a frase vem como argumento (as opcoes "--..." nao fazem parte da frase)
    argumentos = [a for a in sys.argv[1:] if not a.startswith("--")]
    if argumentos:
        enviar(" ".join(argumentos))
        return

    # Modo interativo
    print("Digite um comando ou uma pergunta ('estado' / 'sair'):")
    print(f"Roteamento: {'manual (pelo ?)' if MODO_MANUAL else 'semântico (o gateway decide)'}")
    
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