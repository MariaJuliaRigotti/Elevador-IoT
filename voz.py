# Cliente por voz: transcreve a fala no próprio computador e envia só o texto ao gateway (--loop para continuar ouvindo)

import json
import os
import sys
from ctypes import CFUNCTYPE, c_char_p, c_int, cdll
import httpx

# Esconde os avisos de áudio (ALSA) que aparecem no Linux; não muda o funcionamento
ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)

# Recebe os avisos do ALSA e não faz nada com eles
def _ignorar_erro_alsa(filename, line, function, err, fmt):
    pass

_handler = ERROR_HANDLER_FUNC(_ignorar_erro_alsa)   # Precisa ficar guardado numa variável, senão para de funcionar
try:
    cdll.LoadLibrary("libasound.so.2").snd_lib_error_set_handler(_handler)
except OSError:
    pass   # No Windows e no macOS não existe ALSA

import speech_recognition as sr   # noqa: E402  (precisa vir depois de silenciar o ALSA)

# Configuracoes
GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8000")   # Endereço do gateway
TOKEN = os.getenv("GATEWAY_TOKEN", "token-morador-2m8x4p")   # Token de acesso (ver gateway/usuarios.json)
CABECALHOS = {"Authorization": f"Bearer {TOKEN}"}   # Enviado em todo pedido ao gateway
IDIOMA = "pt-BR"   # Idioma da transcrição

# Escuta o microfone e transforma a fala em texto (None se não entendeu)
def ouvir(r: sr.Recognizer, mic: sr.Microphone) -> str | None:

    with mic as source:
        print("\nFale...")
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

# Mostra o que o roteador decidiu: domínio, complexidade e modelo
def mostrar_rota(rota: dict, modelo: str):

    print(f"  ⇢ roteador: {rota['dominio']}/{rota['complexidade']} → modelo {modelo}")

# Envia o texto com o domínio auto e mostra a resposta
# Se for comando, a resposta é um JSON; se for pergunta, chega palavra por palavra
def enviar(texto: str):

    pedido = {"dominio": "auto", "mensagem": texto}

    try:
        with httpx.stream("POST", f"{GATEWAY}/chat", json=pedido, headers=CABECALHOS, timeout=90) as r:
            eh_streaming = "text/event-stream" in r.headers.get("content-type", "")

            # Comando (ou erro): a resposta é um JSON só
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

            # Pergunta: cada evento chega como uma linha "data: {...}"
            print("  ← ", end="", flush=True)
            for linha in r.iter_lines():
                if not linha.startswith("data: "):
                    continue
                evento = json.loads(linha[len("data: "):])
                if "token" in evento:
                    print(evento["token"], end="", flush=True)   # flush mostra a palavra na hora, sem esperar a linha acabar
                elif "erro" in evento:
                    print(f"\n  ✗ {evento['erro']}")
                elif evento.get("fim"):
                    print(f"\n  (primeiro token em {evento.get('ttft_ms')} ms, total {evento['tempo_ms']} ms)")
                    if evento.get("rota"):
                        mostrar_rota(evento["rota"], evento["modelo"])
    except httpx.HTTPError as e:
        print(f"\n  ✗ gateway inacessível: {e}")

# Calibra o microfone e escuta uma frase (ou várias, com --loop)
def main():

    r = sr.Recognizer()
    r.pause_threshold = 1.0   # Espera 1 s de silêncio para considerar que a frase acabou
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