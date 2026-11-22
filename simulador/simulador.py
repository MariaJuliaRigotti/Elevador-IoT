# Simulador do elevador. Responde aos mesmos tópicos MQTT que o hardware real responderia. 

import json
import os
import threading
import time
import paho.mqtt.client as mqtt

# Configuracoes
BROKER_HOST = os.getenv("BROKER_HOST", "broker.hivemq.com")   # Onde está rodando o broker MQTT
BROKER_PORTA = int(os.getenv("BROKER_PORTA", "1883"))   # Porta do broker MQTT
ANDAR_MAX = int(os.getenv("ANDAR_MAX", "3"))   # Elevador vai do andar 0 ao 3
TEMPO_POR_ANDAR = float(os.getenv("TEMPO_POR_ANDAR", "1.5"))   # Tempo em segundos para subir ou descer um andar
TEMPO_PORTA = float(os.getenv("TEMPO_PORTA", "2.0"))   # Quanto tempo a porta fica aberta ao chegar em um andar
INTERVALO_TELEMETRIA = float(os.getenv("INTERVALO_TELEMETRIA", "2.0"))   # Intervalo entre envios de status do elevador
PREFIXO = os.getenv("MQTT_PREFIXO", "maju-ana-prefixo/")   # Prefixo dos topicos usados pelo simulador

# Nomes dos topicos MQTT
topico_comando = f"{PREFIXO}elevador/comando"   # Topico pelo qual o simulador recebe os comandos
topico_evento = f"{PREFIXO}elevador/evento"   # Topico pelo qual o simulador publica eventos (chegou, rejeitado, aceito)
topico_telemetria = f"{PREFIXO}elevador/telemetria"   # Topico pelo qual o simulador publica o estado atual do elevador
topico_online = f"{PREFIXO}elevador/online"   # Indica se o simulador esta conectado e disponivel

def log(msg):
    print(f"[simulador] {msg}", flush=True)

# Usamos time.monotonic() para obter uma referencia crescente de tempo e medir intervalos
def tempo_atual():
    return time.monotonic()

class Elevador:

    def __init__(self, cliente: mqtt.Client):

        self.cliente = cliente
        self.andar_atual = 0   # Comeca no andar 0
        self.andar_destino = None
        self.porta_aberta = False
        self.request_id_atual = None   # ID do comando que está sendo executado

        # Controlam em que tempo cada coisa deve acontecer
        self.tempo_prox_andar = None    # Quando o elevador chega ao próximo andar
        self.tempo_fechar_porta = None     # Quando a porta deve fechar

    @property
    # Verifica se existe um andar de destino definido, ou seja, se o elevador está em movimento
    def movendo(self):
        return self.andar_destino is not None

    # Verifica se o elevador está subindo, descendo ou parado
    def direcao(self):

        if self.andar_destino is None:
            return "parado"
        if self.andar_destino > self.andar_atual:
            return "subindo"
        else:
            return "descendo"

    # Reune as informacoes atuais do elevador, que serao enviadas pelo MQTT
    def telemetria(self):

        payload = {
            "andar_atual": self.andar_atual,
            "andar_destino": self.andar_destino,
            "movendo": self.movendo,
            "direcao": self.direcao(),
            "porta_aberta": self.porta_aberta,
            "ts": time.time(),
        }
        self.cliente.publish(topico_telemetria, json.dumps(payload), qos=0, retain=True)

    # Registra os eventos do elevador e envia as informacoes correspondentes pelo MQTT
    def evento(self, request_id, tipo, **extra):

        payload = {"request_id": request_id, "tipo": tipo, "ts": time.time(), **extra}
        self.cliente.publish(topico_evento, json.dumps(payload), qos=1)
        log(f"evento {tipo} {extra} (req={request_id})")

    # Recebe os comandos pelo MQTT e, caso sejam validos, inicia a movimentacao do elevador
    def receber_comando(self, dados: dict):

        id_solicitacao = dados.get("request_id")
        andar = dados.get("andar")

        # Verifica a acao e o tipo do andar
        if dados.get("acao") != "mover" or not isinstance(andar, int):
            return self.evento(id_solicitacao, "rejeitado", motivo="payload_invalido")
        # Verifica se o andar eh valido (0 a 3)
        if not 0 <= andar <= ANDAR_MAX:
            return self.evento(id_solicitacao, "rejeitado", motivo="andar_invalido")
        # Verifica se o elevador esta ocupado
        if self.movendo or self.porta_aberta:
            return self.evento(id_solicitacao, "rejeitado", motivo="ocupado")
        # Verifica se o elevador ja esta no andar solicitado
        if andar == self.andar_atual:
            return self.evento(id_solicitacao, "rejeitado", motivo="ja_esta_no_andar")

        # Se as validacoes forem aceitas, agenda o movimento do elevador
        self.andar_destino = andar
        self.request_id_atual = id_solicitacao
        self.tempo_prox_andar = tempo_atual() + TEMPO_POR_ANDAR

        # Envia um evento MQTT indicando que o comando foi aceito
        self.evento(id_solicitacao, "aceito", andar=andar)   
        log(f"movendo {self.andar_atual} -> {self.andar_destino}")
        # Publica as informacoes atualizadas do elevador
        self.telemetria()

    # Simula a movimentacao do elevador, atualizando o andar atual e o estado da porta
    def atualizar(self):

        # Armazena o instante atual
        t = tempo_atual()

        # Verifica se o elevador deve avancar um andar
        if self.movendo and t >= self.tempo_prox_andar:
            
            if self.andar_destino > self.andar_atual:
                self.andar_atual += 1 
            else:
                self.andar_atual -= 1
            log(f"passando pelo andar {self.andar_atual}")

            # Verifica se o elevador chegou ao andar de destino
            if self.andar_atual == self.andar_destino:   
                self.andar_destino = None
                self.tempo_prox_andar = None
                self.porta_aberta = True
                self.tempo_fechar_porta = t + TEMPO_PORTA
                self.telemetria()
                self.evento(self.request_id_atual, "chegou", andar=self.andar_atual)
                self.request_id_atual = None
            else:                                         
                self.tempo_prox_andar += TEMPO_POR_ANDAR
                self.telemetria()

        # Verifica se ja deu o tempo de fechar a porta do elevador
        if self.porta_aberta and t >= self.tempo_fechar_porta:
            self.porta_aberta = False
            self.tempo_fechar_porta = None
            log("porta fechada")
            self.telemetria()

def main():

    # Cria o cliente que sera usado pelo simulador para se comunicar com o broker
    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{PREFIXO}simulador")    
    cliente.will_set(topico_online, "false", qos=1, retain=True)
    elevador = Elevador(cliente)

    # Inscreve o simulador nos topicos de comando e informa que ele esta online 
    def conectado(c, userdata, flags, reason_code, properties):

        log(f"conectado ao broker {BROKER_HOST}:{BROKER_PORTA} ({reason_code})")
        c.subscribe(topico_comando, qos=1)
        c.publish(topico_online, "true", qos=1, retain=True)
        elevador.telemetria()

    # Interpreta as mensagens recebidas e encaminha o comando para o elevador
    def mensagem(c, userdata, msg):

        try:
            dados = json.loads(msg.payload)
            if not isinstance(dados, dict):
                raise ValueError
        except ValueError:
            log(f"payload inválido em {msg.topic}: {msg.payload!r}")
            return

        if msg.topic == topico_comando:
            elevador.receber_comando(dados)

    cliente.on_connect = conectado
    cliente.on_message = mensagem

    # Tenta fazer a primeira conexao do simulador com o broker MQTT
    while True:
        try:
            cliente.connect(BROKER_HOST, BROKER_PORTA, keepalive=30)
            break
        except OSError:
            log("broker indisponível, tentando de novo em 2 s...")
            time.sleep(2)

    prox_telemetria = tempo_atual() + INTERVALO_TELEMETRIA

    # Loop principal do simulador
    try:
        while True:
          
            rc = cliente.loop(timeout=0.1)

            # Verifica a conexao
            if rc != mqtt.MQTT_ERR_SUCCESS:
                log("conexão perdida, tentando reconectar em 2 s...")
                time.sleep(2)
                try:
                    cliente.reconnect()   
                except OSError:
                    pass
                continue

            # Atualiza a movimentacao do elevador
            elevador.atualizar()

            # Controla o envio periodico de telemetria do elevador
            if tempo_atual() >= prox_telemetria:
                elevador.telemetria()
                prox_telemetria += INTERVALO_TELEMETRIA

    # Trata o encerramento do simulador
    except KeyboardInterrupt:

        cliente.publish(topico_online, "false", qos=1, retain=True)
        cliente.loop(timeout=0.5)   
        cliente.disconnect()

if __name__ == "__main__":
    main()