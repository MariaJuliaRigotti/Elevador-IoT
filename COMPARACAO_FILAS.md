# Comparacao das politicas de fila

O gateway aceita `POLITICA_FILA=fifo` (padrao) ou `POLITICA_FILA=curtas`.
Cada dominio possui sua propria fila e seu proprio limite de concorrencia.
O escalonamento e nao preemptivo: apenas pedidos que aguardam sao reordenados.

Para consultas, o campo opcional `max_tokens_resposta` define simultaneamente
um limite de geracao no Ollama (`num_predict`) e o tamanho estimado usado pela
prioridade. Sem o campo, a estimativa e 256 tokens e a geracao permanece sem
limite adicional. Para comandos, a prioridade padrao e a mesma para todos.

## Executar

```bash
python testar_filas.py
POLITICA_FILA=fifo docker compose up -d --build --force-recreate gateway
python comparar_filas.py --clientes 16 --saida resultados_filas
POLITICA_FILA=curtas docker compose up -d --build --force-recreate gateway
python comparar_filas.py --clientes 16 --saida resultados_filas
```

Os arquivos `resultados_filas/fifo_requisicoes.csv` e
`resultados_filas/curtas_requisicoes.csv` registram cada pedido. Execute varias
repeticoes, com os mesmos parametros, em condicoes comparaveis de hardware.
O teste usa o token de `GATEWAY_TOKEN`, se definido, e o token de morador do
cliente de exemplo como padrao. O rate limit pode rejeitar testes muito grandes.

**Importante:** `max_tokens_resposta` e um teto, nao uma garantia de comprimento.
O modelo pode responder com menos tokens; compare tambem `tokens` reais. A
prioridade so produz diferenca quando ha pedidos esperando ao mesmo tempo.
As consultas podem sofrer interferencia de outros dominios se compartilham
hardware no Ollama. O experimento nao foi executado contra um Ollama real aqui.
