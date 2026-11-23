# Métricas dos pedidos atendidos pelo gateway: TTFT, tokens/s, latência, espera na fila e vazão

import math
import time
from collections import deque

# Configuracoes
MAX_REGISTROS = 1000   # Guarda só os últimos pedidos de cada domínio, para não ocupar memória sem limite
JANELA_VAZAO = 60   # A vazão considera os pedidos que terminaram nos últimos 60 segundos

# Média de uma lista de números 
def media(valores: list) -> float | None:

    if not valores:
        return None

    return round(sum(valores) / len(valores), 1)

# Percentil 95: 95% dos pedidos ficaram abaixo desse valor 
def p95(valores: list) -> float | None:

    if not valores:
        return None

    ordenados = sorted(valores)
    posicao = math.ceil(0.95 * len(ordenados)) - 1

    return round(ordenados[posicao], 1)

class RegistroMetricas:

    def __init__(self):

        self.registros = {}   # Para cada domínio, os últimos pedidos atendidos
        self.erros = {}   # Para cada domínio, quantos pedidos terminaram com erro

    # Guarda as métricas de um pedido que terminou com sucesso
    def registrar(self, dominio: str, inicio: float, fim: float, **dados):

        if dominio not in self.registros:
            self.registros[dominio] = deque(maxlen=MAX_REGISTROS)
        self.registros[dominio].append({"inicio": inicio, "fim": fim, **dados})

    # Conta um pedido que terminou com erro
    def registrar_erro(self, dominio: str):

        self.erros[dominio] = self.erros.get(dominio, 0) + 1

    # Apaga tudo 
    def zerar(self):

        self.registros = {}
        self.erros = {}

    # Calcula quantos pedidos e tokens o sistema entregou por segundo, somando todos os clientes
    def vazao(self, lista: list) -> dict:

        agora = time.time()
        recentes = [r for r in lista if agora - r["fim"] <= JANELA_VAZAO]

        if not recentes:
            return {"pedidos_por_s": 0, "tokens_por_s": 0, "janela_s": 0}

        # Tempo entre o primeiro pedido começar e o último terminar
        duracao = max(r["fim"] for r in recentes) - min(r["inicio"] for r in recentes)
        if duracao <= 0:
            return {"pedidos_por_s": 0, "tokens_por_s": 0, "janela_s": 0}

        total_tokens = sum(r.get("tokens") or 0 for r in recentes)

        return {
            "pedidos_por_s": round(len(recentes) / duracao, 2),
            "tokens_por_s": round(total_tokens / duracao, 1),
            "janela_s": round(duracao, 1),   
        }

    # Resumo de cada domínio, mostrado no /metricas
    def resumo(self) -> dict:

        resultado = {}
        nomes = set(self.registros) | set(self.erros)

        for dominio in nomes:
            lista = list(self.registros.get(dominio, []))

            # Junta os valores de cada métrica, pulando os pedidos que não têm aquela métrica
            ttft = [r["ttft_ms"] for r in lista if r.get("ttft_ms") is not None]
            velocidade = [r["tokens_por_s"] for r in lista if r.get("tokens_por_s") is not None]
            latencia = [r["tempo_ms"] for r in lista]
            espera = [r["espera_fila_ms"] for r in lista]

            resultado[dominio] = {
                "pedidos": len(lista),
                "erros": self.erros.get(dominio, 0),
                "ttft_ms": {"media": media(ttft), "p95": p95(ttft)},
                "tokens_por_s_por_pedido": media(velocidade),
                "latencia_ms": {"media": media(latencia), "p95": p95(latencia)},
                "espera_fila_ms": {"media": media(espera), "p95": p95(espera)},
                "vazao": self.vazao(lista),
            }

        return resultado

metricas = RegistroMetricas()   # Registro único, usado por todo o gateway