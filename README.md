# ElevadorIoT

Elevador controlado por linguagem natural, por texto ou voz ("sobe pro
andar 3", "desce pro térreo"). Um LLM (ou regras) traduz a frase em comando,
que vai ao elevador via MQTT. Nesta versão o elevador é simulado.

```
Cliente ──HTTP──▶ Gateway (FastAPI + tradutor) ──MQTT──▶ Broker (Mosquitto) ──MQTT──▶ Simulador
   ▲                        ▲                                                         │
   └──── resposta ──────────┴──────────────── eventos / telemetria ◀──────────────────┘
```

## Estrutura

```
PROTOCOLO.md            contrato MQTT: tópicos, payloads, QoS (leia primeiro)
gateway/
  app.py                API HTTP (POST /comando, GET /estado, GET /saude)
  tradutor.py           linguagem natural -> comando (LLM com fallback para regex)
  modelos.py            validação Pydantic (andar 0..5)
  mqtt_cliente.py       publica comandos e espera a confirmação pelo request_id
simulador/
  simulador.py          gêmeo digital do elevador
cliente.py              cliente de terminal para testar (texto)
voz.py                  cliente por voz: microfone -> texto -> gateway
docker-compose.yml      sobe broker + simulador + gateway
```

## Como rodar

### Opção 1: Docker (mais fácil)

```bash
docker compose up --build
```

Para usar o LLM, defina a chave antes:

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # Windows PowerShell: $env:ANTHROPIC_API_KEY="..."
docker compose up --build
```

Sem chave, o gateway usa o tradutor por regras automaticamente.

### Opção 2: sem Docker (3 terminais)

```bash
pip install -r requirements.txt
# instale o Mosquitto: https://mosquitto.org/download/

# Terminal 1
mosquitto -c mosquitto/mosquitto.conf

# Terminal 2
cd simulador && python simulador.py

# Terminal 3
cd gateway && uvicorn app:app --reload
```

## Testando

```bash
python cliente.py                         # modo interativo
python cliente.py "sobe pro terceiro andar"
```

Ou pelo navegador em http://localhost:8000/docs (Swagger), ou com curl:

```bash
curl -X POST localhost:8000/comando -H "content-type: application/json" \
     -d '{"texto": "sobe pro terceiro andar"}'
```

### Por voz

```bash
# Linux: sudo apt install portaudio19-dev
pip install -r requirements-voz.txt
python voz.py            # um comando
python voz.py --loop     # fica escutando
```

A fala é transcrita no próprio cliente (Google Speech, `pt-BR`, precisa de
internet) e só o texto vai para o gateway.

Para observar o tráfego MQTT ao vivo:

```bash
mosquitto_sub -h localhost -t 'elevador/#' -v
```

## Respostas do `POST /comando`

| HTTP | status | Quando |
|---|---|---|
| 200 | `concluido` | elevador chegou ao andar (`chegou`) |
| 202 | `aceito` | `"aguardar": false`, publicado sem esperar |
| 409 | `rejeitado` | elevador recusou (ex.: ocupado) |
| 422 | - | frase não entendida ou fora do contrato (ex.: andar 9) |
| 503 | - | broker fora do ar ou elevador offline (Last Will) |
| 504 | `timeout` | elevador não respondeu a tempo |

## Configuração (variáveis de ambiente)

| Variável | Padrão | Onde |
|---|---|---|
| `BROKER_HOST` / `BROKER_PORT` | `localhost` / `1883` | ambos |
| `ANTHROPIC_API_KEY` | vazio | gateway |
| `TRADUTOR` | `auto` (`llm`, `regras`) | gateway |
| `MODELO_LLM` | `claude-haiku-5-5` | gateway |
| `TIMEOUT_CONFIRMACAO` | `15` s | gateway |
| `ANDAR_MAX` | `5` | ambos |
| `TEMPO_POR_ANDAR` / `TEMPO_PORTA` | `1.5` / `2.0` s | simulador |
| `INTERVALO_TELEMETRIA` | `2.0` s | simulador |

## Trocando o simulador pelo ESP32

Desligue o simulador e grave no ESP32 um firmware que siga o
[PROTOCOLO.md](PROTOCOLO.md), apontando para o IP do computador que roda o
broker. O gateway não precisa de nenhuma alteração.
