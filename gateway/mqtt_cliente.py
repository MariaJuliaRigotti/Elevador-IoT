"""
Ponte entre o mundo assíncrono do FastAPI e o cliente MQTT (paho).

- Publica comandos com um request_id único.
- Assina os tópicos de evento/telemetria e guarda o último estado.
- Permite que uma requisição HTTP "espere" o evento com o seu request_id
  (é isso que torna o retorno síncrono possível).
"""

import asyncio
import json
import os
import threading
import uuid

import paho.mqtt.client as mqtt

BROKER_HOST = os.getenv("BROKER_HOST", "localhost")
BROKER_PORT = int(os.getenv("BROKER_PORT", "1883"))

# Tipos de evento que encerram a espera de uma requisição
EVENTOS_FINAIS = {"chegou", "rejeitado"}


class PonteMQTT:
    def __init__(self):
        self.cliente = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"gateway-{uuid.uuid4().hex[:6]}"
        )
        self.cliente.on_connect = self._on_connect
        self.cliente.on_message = self._on_message
        self.cliente.reconnect_delay_set(min_delay=1, max_delay=10)

        self.loop: asyncio.AbstractEventLoop | None = None
        self.pendentes: dict[str, asyncio.Future] = {}
        self.lock = threading.Lock()

        self.conectado = False
        self.estado = {"elevador": None, "elevador_online": None}

    # ---- ciclo de vida ----

    def iniciar(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.cliente.connect_async(BROKER_HOST, BROKER_PORT, keepalive=30)
        self.cliente.loop_start()  # thread própria do paho

    def parar(self):
        self.cliente.loop_stop()
        self.cliente.disconnect()

    # ---- callbacks (rodam na thread do paho, NÃO no event loop) ----

    def _on_connect(self, c, userdata, flags, reason_code, properties):
        self.conectado = not reason_code.is_failure
        print(f"[gateway] conectado ao broker {BROKER_HOST}:{BROKER_PORT} ({reason_code})", flush=True)
        c.subscribe([
            ("elevador/evento", 1),
            ("elevador/telemetria", 0),
            ("elevador/online", 1),
        ])

    def _on_message(self, c, userdata, msg):
        topico = msg.topic
        if topico == "elevador/online":
            self.estado["elevador_online"] = msg.payload.decode() == "true"
            return
        try:
            dados = json.loads(msg.payload)
        except ValueError:
            return

        if topico == "elevador/telemetria":
            self.estado["elevador"] = dados
        elif topico == "elevador/evento":
            self._resolver(dados)

    def _resolver(self, evento: dict):
        if evento.get("tipo") not in EVENTOS_FINAIS:
            return  # "aceito" é intermediário: continua esperando "chegou"
        with self.lock:
            fut = self.pendentes.pop(evento.get("request_id"), None)
        if fut and self.loop:
            # Passa o resultado da thread do paho para o event loop do FastAPI
            self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(evento))

    # ---- API usada pelo FastAPI ----

    async def enviar(self, topico: str, payload: dict, aguardar: bool, timeout: float):
        request_id = uuid.uuid4().hex[:12]
        payload = {"request_id": request_id, **payload}

        fut = None
        if aguardar:
            # Registra ANTES de publicar, para não perder uma resposta muito rápida
            fut = self.loop.create_future()
            with self.lock:
                self.pendentes[request_id] = fut

        info = self.cliente.publish(topico, json.dumps(payload), qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            with self.lock:
                self.pendentes.pop(request_id, None)
            raise ConnectionError("falha ao publicar no broker")

        if not aguardar:
            return request_id, payload, None

        try:
            evento = await asyncio.wait_for(fut, timeout)
            return request_id, payload, evento
        except asyncio.TimeoutError:
            with self.lock:
                self.pendentes.pop(request_id, None)
            return request_id, payload, None
