# Protocolo de Controle e Telemetria (MQTT)

Este documento é o **contrato** entre o Gateway e os dispositivos. O simulador
e o futuro firmware do ESP32 devem seguir exatamente estes tópicos e payloads.
Se o contrato for respeitado, trocar o simulador pelo hardware real não exige
nenhuma mudança no Gateway.

- Broker: Mosquitto, porta `1883` (sem TLS nesta versão base)
- Codificação: JSON em UTF-8
- `request_id`: string gerada pelo Gateway para correlacionar comando e resposta
- `ts`: timestamp Unix em segundos (float)

## Elevador

| Tópico | Quem publica | Quem assina | QoS | Retained |
|---|---|---|---|---|
| `casa/elevador/comando` | Gateway | Elevador | 1 | não |
| `casa/elevador/evento` | Elevador | Gateway | 1 | não |
| `casa/elevador/telemetria` | Elevador | Gateway | 0 | sim |
| `casa/elevador/online` | Elevador (e Last Will) | Gateway | 1 | sim |

### `casa/elevador/comando`

```json
{"request_id": "a1b2c3", "acao": "mover", "andar": 3}
```

- `acao`: por enquanto só `"mover"`
- `andar`: inteiro de `0` (térreo) a `ANDAR_MAX` (padrão `5`)

### `casa/elevador/evento`

Resposta a um comando específico, sempre com o mesmo `request_id`.

```json
{"request_id": "a1b2c3", "tipo": "aceito",    "andar": 3, "ts": 1730000000.0}
{"request_id": "a1b2c3", "tipo": "chegou",    "andar": 3, "ts": 1730000004.5}
{"request_id": "a1b2c3", "tipo": "rejeitado", "motivo": "ocupado", "ts": 1730000000.0}
```

- `tipo`: `"aceito"` | `"chegou"` | `"rejeitado"`
- `motivo` (só em `rejeitado`): `"ocupado"` | `"andar_invalido"` | `"payload_invalido"` | `"ja_esta_no_andar"`

### `casa/elevador/telemetria`

Publicada a cada mudança de andar e periodicamente (padrão: a cada 2 s).

```json
{
  "andar_atual": 2,
  "andar_destino": 3,
  "movendo": true,
  "direcao": "subindo",
  "porta_aberta": false,
  "ts": 1730000003.0
}
```

- `direcao`: `"subindo"` | `"descendo"` | `"parado"`
- `andar_destino`: `null` quando parado

### `casa/elevador/online`

Payload em texto puro: `"true"` ao conectar, `"false"` configurado como
**Last Will**, publicado pelo broker se o dispositivo cair sem se despedir.

## Luzes

`<id>` é o identificador da luz: `corredor`, `sala`, `cozinha`, `quarto`.

| Tópico | Quem publica | Quem assina | QoS | Retained |
|---|---|---|---|---|
| `casa/luz/<id>/comando` | Gateway | Luz | 1 | não |
| `casa/luz/<id>/evento` | Luz | Gateway | 1 | não |
| `casa/luz/<id>/estado` | Luz | Gateway | 1 | sim |

### `casa/luz/<id>/comando`

```json
{"request_id": "d4e5f6", "acao": "ligar"}
```

- `acao`: `"ligar"` | `"desligar"`

### `casa/luz/<id>/evento`

```json
{"request_id": "d4e5f6", "tipo": "concluido", "ligada": true, "ts": 1730000000.0}
```

### `casa/luz/<id>/estado`

```json
{"ligada": true, "ts": 1730000000.0}
```

## Fluxo de um comando

```
Cliente          Gateway                 Broker              Elevador
  | POST /comando  |                        |                     |
  |--------------->| traduz (LLM/regras)    |                     |
  |                | valida (Pydantic)      |                     |
  |                |-- comando ------------>|-- comando --------->|
  |                |<-- evento: aceito -----|<--------------------|
  |                |<-- telemetria ---------|<-- (a cada andar) --|
  |                |<-- evento: chegou -----|<--------------------|
  |<-- 200 OK -----|                        |                     |
```

Com `"aguardar": false`, o Gateway responde `202 Accepted` logo após publicar,
sem esperar o evento.
