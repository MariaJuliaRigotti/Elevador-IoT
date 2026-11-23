# Protocolo do ElevadorIoT

Este documento é o **contrato** entre as partes do sistema. São três trechos, cada um com seu protocolo:

```
Cliente  ──HTTP + SSE──►  Gateway  ──HTTP (streaming NDJSON)──►  Nós Ollama
                             │
                             └──MQTT──►  Broker  ──MQTT──►  Elevador (simulador ou ESP32)
```

| Trecho | Protocolo | Seção |
|---|---|---|
| Cliente ↔ gateway | HTTP/REST, com respostas em streaming por SSE | [1](#1-cliente--gateway-http--sse) |
| Gateway ↔ nós de IA | HTTP, API do Ollama com streaming NDJSON | [2](#2-gateway--nós-de-ia-http-do-ollama) |
| Gateway ↔ elevador | MQTT (publish/subscribe) | [3](#3-gateway--elevador-mqtt) |

Codificação: JSON em UTF-8 em todos os trechos.

---

## 1. Cliente ↔ gateway (HTTP + SSE)

Endereço padrão: `http://localhost:8000`. Documentação interativa: `http://localhost:8000/docs`.

### Autenticação

Os endpoints que executam algo exigem um token no cabeçalho:

```
Authorization: Bearer <token>
```

Os tokens, os domínios que cada usuário pode usar e o limite de pedidos por minuto ficam em `gateway/usuarios.json`.

### Endpoints

| Método e caminho | Token | O que faz |
|---|---|---|
| `POST /chat` | sim | Envia uma mensagem a um domínio (`comando`, `consulta` ou `auto`) |
| `POST /comando` | sim | Atalho para o domínio `comando` |
| `POST /rota` | sim | Só classifica a mensagem (domínio e complexidade), sem executar nada |
| `GET /metricas` | não | TTFT, tokens/s, latência e vazão por domínio e situação das filas (`filas` por domínio, `nos` por nó) |
| `DELETE /metricas` | admin | Zera as métricas (usado entre as rodadas do experimento) |
| `GET /saude` | não | Conexão com o broker, elevador online, tradutor em uso e filas (`filas` por domínio, `nos` por nó) |
| `GET /estado` | não | Última telemetria recebida do elevador |

### `POST /chat`: pedido

```json
{"dominio": "auto", "mensagem": "sobe pro segundo andar", "aguardar": true}
```

- `dominio`: `"comando"`, `"consulta"` ou `"auto"` (o roteador semântico escolhe o domínio e o tamanho do modelo)
- `mensagem`: de 1 a 500 caracteres
- `aguardar` (só no comando): `true` espera o elevador confirmar; `false` responde logo após publicar
- `max_tokens_resposta` (opcional, só na consulta): de 1 a 2048. Limita o tamanho da resposta no Ollama e, com `POLITICA_FILA=curtas`, define a prioridade na fila (respostas menores passam na frente). Sem ele, a fila estima 256 tokens

### Resposta do domínio `comando`: um JSON único

```json
{
  "request_id": "2f75ca8b4238",
  "status": "concluido",
  "comando": {"topico": "maju-ana-prefixo/elevador/comando", "payload": {"request_id": "2f75ca8b4238", "acao": "mover", "andar": 2}},
  "detalhe": {"request_id": "2f75ca8b4238", "tipo": "chegou", "andar": 2, "ts": 1795327424.0},
  "tradutor": "ollama",
  "modelo": "qwen2.5:1.5b",
  "rota": {"dominio": "comando", "complexidade": "simples", "metodo": "regras", "modelo": "qwen2.5:1.5b", "classificacao_ms": 0.05, "espera_fila_ms": 0.0, "bruto": ""},
  "tempo_ms": 3021.7,
  "espera_fila_ms": 0.0
}
```

| `status` | Código HTTP | Significado |
|---|---|---|
| `concluido` | 200 | O elevador chegou ao andar |
| `aceito` | 202 | Publicado sem esperar a confirmação (`aguardar: false`) |
| `rejeitado` | 409 | O elevador recusou (motivo em `detalhe.motivo`) |
| `timeout` | 504 | O elevador não confirmou a tempo |

`modelo` e `rota` só vêm preenchidos no domínio `auto`.

### Resposta do domínio `consulta`: streaming SSE

Cabeçalho `Content-Type: text/event-stream`. Cada evento é uma linha `data: {...}` seguida de uma linha em branco:

```
data: {"token": "O "}

data: {"token": "elevador "}

data: {"token": "está no 2º andar."}

data: {"fim": true, "tokens": 9, "espera_fila_ms": 0.0, "politica_fila": "fifo", "max_tokens_resposta": null, "tempo_ms": 1056.0, "ttft_ms": 122.7, "tokens_por_s": 9.6}
```

| Evento | Quando | Campos |
|---|---|---|
| `token` | Cada pedaço de texto gerado | `token` |
| `erro` | A conexão com o nó caiu no meio da resposta | `erro` |
| `fim` | Último evento: métricas da resposta | `tokens`, `ttft_ms`, `tokens_por_s`, `tempo_ms`, `espera_fila_ms`, `politica_fila`, `max_tokens_resposta`; no `auto` também `modelo` e `rota` |

### Erros (antes de qualquer resposta)

Sempre no formato `{"detail": "<explicação>"}`.

| Código | Quando |
|---|---|
| 400 | Domínio desconhecido |
| 401 | Token ausente ou inválido |
| 403 | O usuário não pode usar esse domínio, ou a operação é só para administradores |
| 422 | Mensagem vazia ou longa demais, `max_tokens_resposta` fora de 1 a 2048, frase não reconhecida como comando, ou andar fora de 0 a `ANDAR_MAX` |
| 429 | Limite de pedidos por minuto excedido; o cabeçalho `Retry-After` diz quantos segundos esperar |
| 503 | Gateway sem broker, elevador offline, ou nó do domínio indisponível |

---

## 2. Gateway ↔ nós de IA (HTTP do Ollama)

O gateway chama `POST <nó>/api/chat`. Cada domínio tem seu próprio nó, modelo, prompt e temperatura (`gateway/dominios.py`), e uma fila com limite de pedidos simultâneos (1 no comando, 2 na consulta). Além disso, cada nó tem a própria fila (`LIMITE_NO`, padrão 2): um pedido entra primeiro na fila do domínio e depois na do nó, então o nó nunca atende mais que `LIMITE_NO` pedidos ao mesmo tempo, mesmo quando vários domínios usam o mesmo Ollama.

### Domínio `comando`: resposta inteira, em JSON

```json
{
  "model": "qwen2.5:1.5b",
  "messages": [
    {"role": "system", "content": "<PROMPT_COMANDO>"},
    {"role": "user", "content": "Andar atual: 0\nComando: sobe pro segundo andar"}
  ],
  "stream": false,
  "format": "json",
  "options": {"temperature": 0}
}
```

O modelo deve devolver `{"acao": "mover", "andar": 2}` ou `{"erro": "<explicação>"}`. A saída sempre passa pela validação do Pydantic antes de virar uma mensagem MQTT.

### Domínio `consulta`: streaming NDJSON

```json
{
  "model": "qwen2.5:3b",
  "messages": [
    {"role": "system", "content": "<PROMPT_CONSULTA>"},
    {"role": "user", "content": "Estado do elevador: {\"andar_atual\": 2, ...}\nPergunta: onde está o elevador?"}
  ],
  "stream": true,
  "options": {"temperature": 0.3, "num_predict": 128}
}
```

O `num_predict` só é enviado quando o cliente informa `max_tokens_resposta`.

O Ollama responde em pedaços (*chunked transfer*), uma linha JSON por token. A última linha traz `done: true` e a contagem exata de tokens:

```
{"message": {"content": "O "}, "done": false}
{"message": {"content": "elevador "}, "done": false}
{"message": {"content": ""}, "done": true, "eval_count": 9, "eval_duration": 937500000}
```

---

## 3. Gateway ↔ elevador (MQTT)

O simulador e o futuro firmware do ESP32 devem seguir exatamente estes tópicos e payloads. Se o contrato for respeitado, trocar o simulador pelo hardware real não exige nenhuma mudança no gateway.

- Broker: `broker.hivemq.com`, porta `1883`, sem TLS e sem autenticação
- Prefixo dos tópicos: `maju-ana-prefixo/` (variável `MQTT_PREFIXO`). Como o broker é público, o prefixo separa as mensagens deste projeto das de outras pessoas.
- `request_id`: texto gerado pelo gateway para ligar o comando à resposta
- `ts`: horário Unix em segundos (número com casas decimais)

### Tópicos

| Tópico | Quem publica | Quem assina | QoS | Retain |
|---|---|---|---|---|
| `maju-ana-prefixo/elevador/comando` | Gateway | Elevador | 1 | não |
| `maju-ana-prefixo/elevador/evento` | Elevador | Gateway | 1 | não |
| `maju-ana-prefixo/elevador/telemetria` | Elevador | Gateway | 0 | sim |
| `maju-ana-prefixo/elevador/online` | Elevador (e o Last Will) | Gateway | 1 | sim |

### `elevador/comando`

```json
{"request_id": "a1b2c3", "acao": "mover", "andar": 3}
```

- `acao`: por enquanto, só `"mover"`
- `andar`: inteiro de `0` (térreo) a `ANDAR_MAX` (padrão `3`)

### `elevador/evento`

Resposta a um comando específico, sempre com o mesmo `request_id`.

```json
{"request_id": "a1b2c3", "tipo": "aceito",    "andar": 3, "ts": 1730000000.0}
{"request_id": "a1b2c3", "tipo": "chegou",    "andar": 3, "ts": 1730000004.5}
{"request_id": "a1b2c3", "tipo": "rejeitado", "motivo": "ocupado", "ts": 1730000000.0}
```

- `tipo`: `"aceito"` | `"chegou"` | `"rejeitado"`. O gateway só encerra a espera em `chegou` ou `rejeitado`.
- `motivo` (só em `rejeitado`): `"ocupado"` | `"andar_invalido"` | `"payload_invalido"` | `"ja_esta_no_andar"`

O elevador atende um comando por vez: enquanto está se movendo ou com a porta aberta, rejeita novos comandos com `"ocupado"`.

### `elevador/telemetria`

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

- `andar_atual` e `andar_destino`: de 0 a `ANDAR_MAX`; `andar_destino` é `null` quando parado
- `direcao`: `"subindo"` | `"descendo"` | `"parado"`

O gateway **valida** cada telemetria (`TelemetriaElevador` em `gateway/modelos.py`): mensagens fora desse formato são ignoradas, e campos extras são descartados antes de o estado ser enviado ao LLM.

### `elevador/online`

Texto puro: `true` ao conectar e `false` ao desligar normalmente. O `false` também fica configurado como **Last Will**: o broker o publica sozinho se o elevador cair sem se despedir (cerca de 45 s sem sinal, com `keepalive` de 30 s). O gateway ignora qualquer valor que não seja `true` ou `false`.

---

## Fluxo de um comando

```
Cliente          Gateway                     Broker              Elevador
  | POST /chat     |                            |                     |
  |--------------->| token e limite de pedidos  |                     |
  |                | roteador (auto)            |                     |
  |                | fila do nó + LLM           |                     |
  |                | valida (Pydantic)          |                     |
  |                |-- comando ---------------->|-- comando --------->|
  |                |<-- evento: aceito ---------|<--------------------|
  |                |<-- telemetria -------------|<-- (a cada andar) --|
  |                |<-- evento: chegou ---------|<--------------------|
  |<-- 200 OK -----|                            |                     |
```

Com `"aguardar": false`, o gateway responde `202` logo após publicar, sem esperar o evento.

## Fluxo de uma consulta

```
Cliente          Gateway                          Nó Ollama
  | POST /chat     |                                  |
  |--------------->| token, roteador, fila do nó      |
  |                | junta pergunta + última telemetria|
  |                |-- POST /api/chat (stream) ------>|
  |<-- data: token |<-- {"message": ...} (por token) -|
  |<-- data: token |<-- ...                          |
  |<-- data: fim --|<-- {"done": true, ...} ---------|
```

A consulta não publica nada no MQTT: ela só lê o último estado recebido pela telemetria.