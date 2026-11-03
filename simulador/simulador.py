"""
Simulador (gêmeo digital) do elevador.

Responde aos mesmos tópicos MQTT que o hardware real responderia
(ver PROTOCOLO.md). Para trocar pelo ESP32, basta desligar este script.
"""

import json
import os
import threading
import time

import paho.mqtt.client as mqtt

BROKER_HOST = os.getenv("BROKER_HOST", "localhost")
BROKER_PORT = int(os.getenv("BROKER_PORT", "1883"))
ANDAR_MAX = int(os.getenv("ANDAR_MAX", "5"))
TEMPO_POR_ANDAR = float(os.getenv("TEMPO_POR_ANDAR", "1.5"))  # segundos
TEMPO_PORTA = float(os.getenv("TEMPO_PORTA", "2.0"))           # porta aberta
INTERVALO_TELEMETRIA = float(os.getenv("INTERVALO_TELEMETRIA", "2.0"))

T_EL_CMD = "elevador/comando"
T_EL_EVT = "elevador/evento"
T_EL_TEL = "elevador/telemetria"
T_EL_ON = "elevador/online"


def log(msg):
    print(f"[simulador] {msg}", flush=True)


class Elevador:
    def __init__(self, cliente: mqtt.Client):
        self.cliente = cliente
        self.andar_atual = 0
        self.andar_destino = None
        self.porta_aberta = False
        self.lock = threading.Lock()

    @property
    def movendo(self):
        return self.andar_destino is not None

    def direcao(self):
        if self.andar_destino is None:
            return "parado"
        return "subindo" if self.andar_destino > self.andar_atual else "descendo"

    def telemetria(self):
        with self.lock:
            payload = {
                "andar_atual": self.andar_atual,
                "andar_destino": self.andar_destino,
                "movendo": self.movendo,
                "direcao": self.direcao(),
                "porta_aberta": self.porta_aberta,
                "ts": time.time(),
            }
        self.cliente.publish(T_EL_TEL, json.dumps(payload), qos=0, retain=True)

    def evento(self, request_id, tipo, **extra):
        payload = {"request_id": request_id, "tipo": tipo, "ts": time.time(), **extra}
        self.cliente.publish(T_EL_EVT, json.dumps(payload), qos=1)
        log(f"evento {tipo} {extra} (req={request_id})")

    def receber_comando(self, dados: dict):
        rid = dados.get("request_id")
        andar = dados.get("andar")

        if dados.get("acao") != "mover" or not isinstance(andar, int):
            return self.evento(rid, "rejeitado", motivo="payload_invalido")
        if not 0 <= andar <= ANDAR_MAX:
            return self.evento(rid, "rejeitado", motivo="andar_invalido")

        with self.lock:
            if self.movendo or self.porta_aberta:
                ocupado = True
            elif andar == self.andar_atual:
                ocupado = None
            else:
                ocupado = False
                self.andar_destino = andar

        if ocupado:
            return self.evento(rid, "rejeitado", motivo="ocupado")
        if ocupado is None:
            return self.evento(rid, "rejeitado", motivo="ja_esta_no_andar")

        self.evento(rid, "aceito", andar=andar)
        # Movimento em outra thread para não travar o loop do MQTT
        threading.Thread(target=self._mover, args=(rid,), daemon=True).start()

    def _mover(self, rid):
        log(f"movendo {self.andar_atual} -> {self.andar_destino}")
        self.telemetria()
        while True:
            time.sleep(TEMPO_POR_ANDAR)  # tempo físico de subir/descer um andar
            with self.lock:
                passo = 1 if self.andar_destino > self.andar_atual else -1
                self.andar_atual += passo
                chegou = self.andar_atual == self.andar_destino
                if chegou:
                    self.andar_destino = None
                    self.porta_aberta = True
            log(f"passando pelo andar {self.andar_atual}")
            self.telemetria()
            if chegou:
                break

        self.evento(rid, "chegou", andar=self.andar_atual)
        time.sleep(TEMPO_PORTA)
        with self.lock:
            self.porta_aberta = False
        self.telemetria()


def main():
    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="simulador-elevador")
    # Last Will: se o simulador cair, o broker avisa todo mundo
    cliente.will_set(T_EL_ON, "false", qos=1, retain=True)

    elevador = Elevador(cliente)

    def on_connect(c, userdata, flags, reason_code, properties):
        log(f"conectado ao broker {BROKER_HOST}:{BROKER_PORT} ({reason_code})")
        c.subscribe(T_EL_CMD, qos=1)
        c.publish(T_EL_ON, "true", qos=1, retain=True)
        elevador.telemetria()

    def on_message(c, userdata, msg):
        try:
            dados = json.loads(msg.payload)
            if not isinstance(dados, dict):
                raise ValueError
        except ValueError:
            log(f"payload inválido em {msg.topic}: {msg.payload!r}")
            return

        if msg.topic == T_EL_CMD:
            elevador.receber_comando(dados)

    cliente.on_connect = on_connect
    cliente.on_message = on_message
    cliente.reconnect_delay_set(min_delay=1, max_delay=10)

    while True:
        try:
            cliente.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
            break
        except OSError:
            log("broker indisponível, tentando de novo em 2 s...")
            time.sleep(2)

    cliente.loop_start()
    try:
        while True:  # telemetria periódica (heartbeat)
            time.sleep(INTERVALO_TELEMETRIA)
            elevador.telemetria()
    except KeyboardInterrupt:
        cliente.publish(T_EL_ON, "false", qos=1, retain=True)
        cliente.loop_stop()
        cliente.disconnect()


if __name__ == "__main__":
    main()
