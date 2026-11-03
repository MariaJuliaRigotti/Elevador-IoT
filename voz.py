"""
Entrada por voz: microfone -> texto (Google Speech) -> gateway.

A transcrição acontece aqui no cliente; pela rede só vai o texto.

Uso:
    python voz.py            # escuta um comando
    python voz.py --loop     # fica escutando até Ctrl+C

Dependências (Linux: antes rode `sudo apt install portaudio19-dev`):
    pip install -r requirements-voz.txt
"""

import sys
from ctypes import CFUNCTYPE, c_char_p, c_int, cdll

import httpx

# ---- Silencia os avisos do ALSA no Linux (não muda o funcionamento) ----
ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)


def _ignorar_erro_alsa(filename, line, function, err, fmt):
    pass


_handler = ERROR_HANDLER_FUNC(_ignorar_erro_alsa)  # manter referência viva
try:
    cdll.LoadLibrary("libasound.so.2").snd_lib_error_set_handler(_handler)
except OSError:
    pass  # Windows/macOS: não há ALSA

import speech_recognition as sr  # noqa: E402  (precisa vir depois do ALSA)

GATEWAY = "http://localhost:8000/comando"
IDIOMA = "pt-BR"


def ouvir(r: sr.Recognizer, mic: sr.Microphone) -> str | None:
    with mic as source:
        print("\n🎤 Fale o comando...")
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


def enviar(texto: str):
    try:
        resp = httpx.post(GATEWAY, json={"texto": texto}, timeout=30)
    except httpx.HTTPError as e:
        print(f"  ✗ gateway inacessível: {e}")
        return
    dados = resp.json()
    if resp.status_code >= 400 and "detail" in dados:
        print(f"  ✗ [{resp.status_code}] {dados['detail']}")
        return
    print(f"  → {dados['comando']['topico']}  {dados['comando']['payload']}")
    print(f"  ← {dados['status'].upper()} em {dados['tempo_ms']} ms (tradutor: {dados['tradutor']})")


def main():
    r = sr.Recognizer()
    mic = sr.Microphone()
    with mic as source:
        print("Calibrando ruído ambiente...")
        r.adjust_for_ambient_noise(source, duration=1)

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
