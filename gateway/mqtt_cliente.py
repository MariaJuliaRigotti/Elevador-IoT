# Ponte entre o mundo assíncrono do FastAPI e o cliente MQTT (paho)
# Publica comandos com um request_id único
# Assina os tópicos de evento/telemetria e guarda o último estado
# Permite que uma requisição HTTP "espere" o evento com o seu request_id

import asyncio
import json
import os
import threading
import uuid
import paho.mqtt.client as mqtt
from pydantic import ValidationError
from modelos import TelemetriaElevador

BROKER_HOST = os.getenv("BROKER_HOST", "broker.hivemq.com")   # Onde está rodando o broker MQTT
BROKER_PORT = int(os.getenv("BROKER_PORT", "1883"))   # Porta do broker MQTT

# Prefixo dos tópicos (deve ser igual ao do simulador). Em broker público,
# Usamos um valor único para não misturar mensagens com outras pessoas
PREFIXO = os.getenv("MQTT_PREFIXO", "maju-ana-prefixo/")
if PREFIXO and not PREFIXO.endswith("/"):
    PREFIXO += "/"

topico_evento = f"{PREFIXO}elevador/evento"     # Topico pelo qual o cliente recebe eventos (chegou, rejeitado, aceito)
topico_telemetria = f"{PREFIXO}elevador/telemetria"     # Topico pelo qual o cliente recebe o status do elevador
topico_online = f"{PREFIXO}elevador/online"     # Indica se o simulador esta conectado e disponivel

# Tipos de evento que encerram a espera de uma requisição
EVENTOS_FINAIS = {"chegou", "rejeitado"}


class PonteMQTT:

    def __init__(self):

        self.cliente = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"gateway-{uuid.uuid4().hex[:6]}"
        )
        self.cliente.on_connect = self._on_connect
        self.cliente.on_disconnect = self._on_disconnect
        self.cliente.on_message = self._on_message
        self.cliente.reconnect_delay_set(min_delay=1, max_delay=10)   # Tenta reconectar sozinho, de 1 a 10 s

        self.loop: asyncio.AbstractEventLoop | None = None   # Loop do FastAPI, onde as respostas sao entregues
        self.pendentes: dict[str, asyncio.Future] = {}   # request_id -> pedido HTTP esperando o evento
        self.lock = threading.Lock()   # Protege 'pendentes', usado pela thread do paho e pelo FastAPI

        self.conectado = False
        self.estado = {"elevador": None, "elevador_online": None}

    # Conecta ao broker em segundo plano; o paho roda numa thread propria
    def iniciar(self, loop: asyncio.AbstractEventLoop):

        self.loop = loop
        self.cliente.connect_async(BROKER_HOST, BROKER_PORT, keepalive=30)
        self.cliente.loop_start()

    # Para a thread do paho e desconecta do broker
    def parar(self):

        self.cliente.loop_stop()
        self.cliente.disconnect()

    # Chamado pelo paho ao conectar (ou reconectar): assina os topicos do elevador
    def _on_connect(self, c, userdata, flags, reason_code, properties):

        self.conectado = not reason_code.is_failure
        print(f"[gateway] conectado ao broker {BROKER_HOST}:{BROKER_PORT} ({reason_code})", flush=True)
        c.subscribe([
            (topico_evento, 1),
            (topico_telemetria, 0),
            (topico_online, 1),
        ])

    # Chamado pelo paho quando a conexao cai: o /saude passa a mostrar o broker desconectado
    def _on_disconnect(self, c, userdata, flags, reason_code, properties):

        self.conectado = False
        print(f"[gateway] desconectado do broker ({reason_code}), tentando reconectar...", flush=True)

    # Chamado pelo paho a cada mensagem recebida: atualiza o estado ou entrega o evento esperado
    def _on_message(self, c, userdata, msg):

        topico = msg.topic

        if topico == topico_online:
            valor = msg.payload.decode(errors="ignore")
            if valor in ("true", "false"):   # Qualquer outro valor e ignorado
                self.estado["elevador_online"] = valor == "true"
            return
        try:
            dados = json.loads(msg.payload)
        except ValueError:
            return

        if topico == topico_telemetria:
            try:
                # Guarda so os campos validos; telemetria fora do formato e descartada
                self.estado["elevador"] = TelemetriaElevador.model_validate(dados).model_dump()
            except ValidationError:
                print(f"[gateway] telemetria inválida ignorada: {msg.payload[:80]!r}", flush=True)
        elif topico == topico_evento:
            self._resolver(dados)

    # Entrega o evento final (chegou ou rejeitado) ao pedido HTTP que tem o mesmo request_id
    # Roda na thread do paho, por isso usa call_soon_threadsafe para falar com o loop do FastAPI
    def _resolver(self, evento: dict):

        if evento.get("tipo") not in EVENTOS_FINAIS:
            return
        with self.lock:
            fut = self.pendentes.pop(evento.get("request_id"), None)
        if fut and self.loop:
            self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(evento))

    # Publica o comando com um request_id novo e, se 'aguardar', espera o evento do elevador ate 'timeout'
    # Retorna (request_id, payload enviado, evento recebido ou None)
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