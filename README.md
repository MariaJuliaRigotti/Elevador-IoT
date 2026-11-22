# ElevadorIoT: gateway distribuído de LLMs com automação IoT

## Descrição

O ElevadorIoT é um gateway de LLMs abertos (Projeto 1 da disciplina de Redes de Computadores) aplicado à automação de um prédio. O usuário fala ou digita em português e o sistema **move um elevador** ("sobe pro segundo andar") ou **responde perguntas sobre ele** ("onde está o elevador?").

Os modelos rodam localmente no Ollama. O elevador é um gêmeo digital que conversa por MQTT exatamente como um ESP32 faria, então trocar o simulador pelo hardware real não exige mudanças no gateway.

```
Cliente  ──HTTP + SSE──►  Gateway (FastAPI)  ──HTTP (streaming)──►  Nós Ollama
(texto ou voz)                  │
                                └──MQTT──►  Broker (HiveMQ)  ──MQTT──►  Elevador (simulador)
```

Desafios abordados:

- **Streaming token a token** do nó de IA até o cliente (SSE)
- **Dois domínios** (`comando` e `consulta`), cada um com modelo, prompt e parâmetros próprios
- **Fila por nó** com limite de concorrência e métricas (TTFT, tokens/s, latência e vazão)
- **Supervisão da IA:** nenhuma saída do LLM chega ao elevador sem passar por validação
- Incrementos em duas trilhas: **roteamento semântico** (Inteligência) e **token + autorização + rate limiting** (Segurança)
- **Política de fila** configurável: ordem de chegada (FIFO) ou prioridade para respostas curtas

O contrato completo entre as partes (endpoints, eventos SSE, tópicos MQTT e payloads) está em [PROTOCOLO.md](PROTOCOLO.md).

## Tecnologias Utilizadas

- **Linguagem:** Python 3.12
- **Gateway:** FastAPI + Uvicorn (HTTP e SSE), Pydantic (validação), httpx (chamadas ao Ollama), asyncio (concorrência)
- **IA:** Ollama com os modelos `qwen2.5:1.5b` e `qwen2.5:3b`
- **Mensageria:** MQTT com paho-mqtt, broker público `broker.hivemq.com`
- **Voz:** SpeechRecognition + PyAudio (transcrição do Google, `pt-BR`)
- **Infraestrutura:** Docker e Docker Compose
- **Experimentos:** matplotlib (gráficos)

## Como Executar

### Requisitos

- Docker com o plugin Compose (`docker compose version` deve funcionar)
- Python 3.12, para os clientes (`cliente.py`, `voz.py` e os scripts de teste)
- Cerca de 4 GB de RAM livres para os modelos e 3 GB de disco
- Internet, para o broker HiveMQ e para a transcrição de voz
- Para a voz no Windows: Python **3.12** (o PyAudio ainda não tem versão pronta para o 3.15). No Linux: `sudo apt install portaudio19-dev`

### Instruções de Execução

1. Clone o repositório:
   ```bash
   git clone https://github.com/Clarice-Curtinhas/Automacao-IoT-via-protocolos-de-rede.git
   cd Automacao-IoT-via-protocolos-de-rede
   ```

2. Suba o gateway, o simulador e o Ollama:
   ```bash
   docker compose up --build -d
   ```

3. Baixe os modelos (só na primeira vez; ficam guardados no volume do Docker):
   ```bash
   docker compose exec ollama ollama pull qwen2.5:1.5b
   docker compose exec ollama ollama pull qwen2.5:3b
   ```

4. Instale as dependências dos clientes:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   pip install -r requirements-voz.txt          # opcional, para a voz
   pip install -r requirements-experimento.txt  # opcional, para os gráficos
   ```

5. Execute o cliente:
   ```bash
   python3 cliente.py                  # modo interativo (texto)
   python voz.py --loop                # por voz
   ```

Para ver os logs: `docker compose logs -f gateway simulador`. Para desligar: `docker compose down` (sem `-v`, para não apagar os modelos).

**Cuidado:** rode o simulador em **um computador só**. Dois simuladores no mesmo broker público usam o mesmo `client_id` e se derrubam o tempo todo.

## Como Testar

### Uso básico

```bash
python3 cliente.py "sobe pro segundo andar"        # comando: o elevador anda
python3 cliente.py "em que andar está o elevador?" # consulta: resposta palavra por palavra
python3 cliente.py                                 # modo interativo; "estado" mostra a telemetria
```

Por padrão o cliente usa o domínio `auto`: o gateway decide sozinho se a frase é comando ou pergunta. Com `--manual`, frases terminadas em `?` vão para a consulta e as demais para o comando.

Pelo navegador: `http://localhost:8000/docs`. Clique em **Authorize**, cole um token e teste o `POST /chat`.

### Usuários de teste (`gateway/usuarios.json`)

| Usuário | Token | Domínios | Limite |
|---|---|---|---|
| morador (padrão do `cliente.py` e do `voz.py`) | `token-morador-2m8x4p` | comando e consulta | 10/min |
| visitante | `token-visitante-9d1w6z` | só consulta | 5/min |
| admin | `token-admin-7f3k9q` | comando e consulta | 30/min; pode zerar métricas e trocar a política |
| experimento (scripts de teste) | `token-experimento-5h2r7t` | comando e consulta | 1000/min |

Para usar outro token: `GATEWAY_TOKEN=token-visitante-9d1w6z python3 cliente.py` (Windows: `$env:GATEWAY_TOKEN="..."`).

### Acompanhar o sistema

- `http://localhost:8000/saude`: broker, elevador online, filas e nós
- `http://localhost:8000/metricas`: TTFT, tokens/s, latência e vazão por domínio
- Mensagens MQTT: no MQTT Explorer, conecte em `broker.hivemq.com:1883` e, em **Advanced**, troque a assinatura `#` por `maju-ana-prefixo/#` (o broker público não aceita `#`)

### Roteiros de teste

| O que testar | Como |
|---|---|
| Vários clientes ao mesmo tempo (fila) | `python3 teste_concorrencia.py 5` |
| Experimento obrigatório (1, 2, 4, 8 e 16 clientes) | `python3 carga.py` → CSVs e gráfico em `resultados/` |
| Avaliação do roteador semântico | `python3 avaliar_roteador.py --conjunto conjunto_roteador_v2.csv --saida resultados_roteador_v2` |
| Escalonador da fila, sem Ollama nem MQTT | `python3 testar_filas.py` |
| FIFO x prioridade para respostas curtas | Passo a passo em `COMPARACAO_FILAS.md` (usa `comparar_filas.py`) |
| Segurança (401, 403, 429) | Passo a passo em `testes_seguranca.txt` |

### Respostas do gateway

| HTTP | Quando |
|---|---|
| 200 | Comando concluído (o elevador chegou) ou consulta respondida |
| 202 | Comando publicado sem esperar a confirmação (`"aguardar": false`) |
| 400 | Domínio desconhecido |
| 401 / 403 | Token ausente ou inválido / sem permissão para o domínio |
| 409 | O elevador recusou (ocupado, já está no andar) |
| 422 | Frase não entendida ou andar fora de 0 a 3 |
| 429 | Limite de pedidos excedido (cabeçalho `Retry-After`) |
| 503 | Broker fora do ar, elevador offline ou nó de IA indisponível |
| 504 | O elevador não confirmou a tempo |

## Funcionalidades Implementadas

**Núcleo do Projeto 1**

- Resposta token a token: Ollama → gateway em NDJSON, gateway → cliente em SSE
- Domínios `comando` (qwen2.5:1.5b, temperatura 0, saída JSON que vira MQTT) e `consulta` (qwen2.5:3b, temperatura 0,3, texto em streaming), escolhidos pelo campo `dominio` do `POST /chat`
- Fila por nó com limite de pedidos simultâneos e duas políticas (`POLITICA_FILA`): `fifo`, por ordem de chegada, ou `curtas`, em que respostas menores (`max_tokens_resposta`) passam na frente
- Métricas por pedido e agregadas: TTFT, tokens/s, latência fim a fim, espera na fila e vazão, com média e p95

**Automação IoT**

- Elevador simulado (andares 0 a 3, porta, telemetria a cada 2 s), sem threads, no estilo de um firmware
- MQTT com QoS 1 para comandos e eventos, QoS 0 + retain para a telemetria e Last Will para detectar a queda do elevador
- Resposta síncrona (espera o elevador chegar) ou assíncrona (202)
- Supervisão da IA: a saída do LLM passa pelo Pydantic; a telemetria recebida também é validada antes de ir para o LLM
- Fallback para regras quando o LLM não responde
- Entrada por voz com perguntas e comandos

**Incrementos**

- **Inteligência:** roteamento semântico híbrido (regras quando têm certeza, LLM nos casos ambíguos), escolhendo o domínio e o tamanho do modelo
- **Segurança:** autenticação por token, autorização por domínio, rate limiting por usuário com `Retry-After` e log dos acessos negados

### Estrutura do projeto

```
PROTOCOLO.md              contrato entre cliente, gateway, nós e elevador
gateway/
  app.py                  endpoints HTTP e streaming SSE
  dominios.py             domínios: nó, modelo, prompt e parâmetros de cada um
  roteador.py             roteamento semântico (domínio auto)
  tradutor.py             frase -> comando (LLM com fallback para regras)
  consulta.py             perguntas sobre o elevador, em streaming
  fila.py                 fila com limite de concorrência por nó (FIFO ou curtas primeiro)
  metricas.py             TTFT, tokens/s, latência e vazão
  seguranca.py            token, autorização por domínio e rate limiting
  usuarios.json           usuários, tokens e permissões
  modelos.py              validação Pydantic (pedidos, comando, telemetria)
  mqtt_cliente.py         ponte entre o asyncio e o MQTT
simulador/simulador.py    gêmeo digital do elevador
cliente.py, voz.py        clientes por texto e por voz
teste_concorrencia.py     vários clientes ao mesmo tempo
carga.py                  experimento obrigatório
comparar_filas.py         FIFO x prioridade para respostas curtas
testar_filas.py           teste do escalonador da fila
avaliar_roteador.py       avaliação do roteador semântico
```

### Configuração (variáveis de ambiente)

| Variável | Padrão | Onde |
|---|---|---|
| `BROKER_HOST` / `BROKER_PORT` | `broker.hivemq.com` / `1883` | gateway e simulador |
| `MQTT_PREFIXO` | `maju-ana-prefixo/` | gateway e simulador |
| `ANDAR_MAX` | `3` | gateway e simulador |
| `TEMPO_POR_ANDAR` / `TEMPO_PORTA` | `1.5` / `2.0` s | simulador |
| `INTERVALO_TELEMETRIA` | `2.0` s | simulador |
| `TRADUTOR` | `auto` (`ollama`, `llm`, `regras`) | gateway |
| `OLLAMA_URL` | `http://localhost:11434` (`http://ollama:11434` no compose) | gateway |
| `MODELO_COMANDO` / `MODELO_CONSULTA` | `qwen2.5:1.5b` / `qwen2.5:3b` | gateway |
| `MODELO_PEQUENO` / `MODELO_GRANDE` | `qwen2.5:1.5b` / `qwen2.5:3b` | gateway (roteador) |
| `ROTEADOR` | `hibrido` (`llm`, `regras`) | gateway |
| `POLITICA_FILA` | `fifo` (`curtas`) | gateway |
| `TIMEOUT_CONFIRMACAO` | `15` s | gateway |
| `GATEWAY_URL` / `GATEWAY_TOKEN` | `http://localhost:8000` / token do morador | clientes |

### Trocando o simulador pelo ESP32

Desligue o simulador e grave no ESP32 um firmware que siga a seção 3 do [PROTOCOLO.md](PROTOCOLO.md), conectado ao mesmo broker e com o mesmo prefixo. O gateway não precisa de nenhuma alteração.

## Possíveis Melhorias Futuras

- **Broker privado com senha e ACL por tópico:** hoje qualquer pessoa que conheça o prefixo pode publicar no broker público (telemetria falsa, `online = false`, eventos forjados)
- **TLS** no HTTP e no MQTT (porta 8883)
- **Tokens fora do repositório**, em variáveis de ambiente ou num cofre de segredos
- **Prioridade entre domínios** (por exemplo, comandos antes de consultas quando dividem o mesmo hardware)
- **Balanceamento entre vários nós** de IA, com failover quando um nó cai
- **Fila de chamados no elevador** (algoritmo SCAN), em vez de recusar pedidos quando está ocupado
- **Transcrição local** com faster-whisper, sem depender do Google
- **Testes automatizados** com pytest para o tradutor, o roteador e o rate limiting