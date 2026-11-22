# Entrada por voz: microfone -> texto (Google Speech) -> gateway
# A transcricao acontece aqui no cliente; pela rede so vai o texto
# O gateway decide sozinho se a frase e um comando ou uma pergunta (dominio "auto", roteamento semantico):
#   - comando ("sobe pro segundo andar"): o elevador anda e a resposta e um JSON
#   - pergunta ("onde está o elevador?"): a resposta chega em streaming, palavra por palavra
# Uso:
#   python voz.py            # escuta uma frase
#   python voz.py --loop     # fica escutando ate Ctrl+C
# Para usar outro token (ex.: o do visitante), defina a variavel GATEWAY_TOKEN antes de rodar
# Dependencias (Linux: antes rode `sudo apt install portaudio19-dev`):
#   pip install -r requirements-voz.txt

import json
import os
import sys
from ctypes import CFUNCTYPE, c_char_p, c_int, cdll
import httpx

# Silencia os avisos do ALSA no Linux (nao muda o funcionamento)
ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)

def _ignorar_erro_alsa(filename, line, function, err, fmt):
    pass

_handler = ERROR_HANDLER_FUNC(_ignorar_erro_alsa)   # Precisa manter a referencia viva
try:
    cdll.LoadLibrary("libasound.so.2").snd_lib_error_set_handler(_handler)
except OSError:
    pass   # Windows/macOS: nao ha ALSA

import speech_recognition as sr   # noqa: E402  (precisa vir depois do ALSA)

# Configuracoes
GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8000")   # Endereco do gateway
TOKEN = os.getenv("GATEWAY_TOKEN", "token-morador-2m8x4p")   # Token de acesso (ver gateway/usuarios.json)
CABECALHOS = {"Authorization": f"Bearer {TOKEN}"}   # Enviado em todo pedido ao gateway
IDIOMA = "pt-BR"

# Escuta o microfone e transforma a fala em texto (None se nao entendeu)
def ouvir(r: sr.Recognizer, mic: sr.Microphone) -> str | None:

    with mic as source:
        print("\n🎤 Fale...")
        try:
            audio = r.listen(source, timeout=5, phrase_time_limit=6)
        except sr.WaitTimeoutError:
            print("  (nenhuma fala detectada)")
            return None

    try:
        texto = r.recognize_google(audio, language=IDIOMA)
    except sr.UnknownValueError:
        print("  ✗ não entendi o áudio")
        return None
    except sr.RequestError as e:
        print(f"  ✗ sem conexão com o serviço de reconhecimento: {e}")
        return None

    print(f"  Você disse: \"{texto}\"")
    return texto

# Mostra a decisao do roteador semantico (dominio, complexidade e modelo)
def mostrar_rota(rota: dict, modelo: str):

    print(f"  ⇢ roteador: {rota['dominio']}/{rota['complexidade']} → modelo {modelo}")

# Envia o texto ao /chat com dominio "auto" e mostra a resposta
# Comando: resposta JSON unica. Pergunta: resposta em streaming (SSE), palavra por palavra
def enviar(texto: str):

    pedido = {"dominio": "auto", "mensagem": texto}
    
    try:
        with httpx.stream("POST", f"{GATEWAY}/chat", json=pedido, headers=CABECALHOS, timeout=90) as r:
            eh_streaming = "text/event-stream" in r.headers.get("content-type", "")

            # Caminho do comando (ou de um erro): resposta JSON unica
            if not eh_streaming:
                r.read()
                dados = r.json()
                if r.status_code >= 400 and "detail" in dados:
                    print(f"  ✗ [{r.status_code}] {dados['detail']}")
                    return
                if dados.get("rota"):
                    mostrar_rota(dados["rota"], dados["modelo"])
                print(f"  → {dados['comando']['topico']}  {dados['comando']['payload']}")
                print(f"  ← {dados['status'].upper()} em {dados['tempo_ms']} ms (tradutor: {dados['tradutor']})")
                return

            # Caminho da pergunta: cada evento SSE chega como uma linha "data: {...}"
            print("  ← ", end="", flush=True)
            for linha in r.iter_lines():
                if not linha.startswith("data: "):
                    continue
                evento = json.loads(linha[len("data: "):])
                if "token" in evento:
                    print(evento["token"], end="", flush=True)   # flush: mostra a palavra na hora
                elif "erro" in evento:
                    print(f"\n  ✗ {evento['erro']}")
                elif evento.get("fim"):
                    print(f"\n  (primeiro token em {evento.get('ttft_ms')} ms, total {evento['tempo_ms']} ms)")
                    if evento.get("rota"):
                        mostrar_rota(evento["rota"], evento["modelo"])
    except httpx.HTTPError as e:
        print(f"\n  ✗ gateway inacessível: {e}")

def main():

    r = sr.Recognizer()
    r.pause_threshold = 1.0   # Espera 1 s de silencio antes de considerar que a frase acabou
    mic = sr.Microphone()

    with mic as source:
        print("Calibrando ruído ambiente... (fique em silêncio)")
        r.adjust_for_ambient_noise(source, duration=2)

    continuar = "--loop" in sys.argv
    while True:
        texto = ouvir(r, mic)
        if texto:
            enviar(texto)
        if not continuar:
            break

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ntchau!")