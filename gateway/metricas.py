# Registro das metricas de cada pedido atendido pelo gateway
# Por pedido: TTFT, tokens, tokens por segundo, latencia fim a fim e espera na fila
# Agregadas (no /metricas): medias, pior caso (p95) e vazao do sistema

import math
import time
from collections import deque

# Configuracoes
MAX_REGISTROS = 1000   # Guarda so os ultimos pedidos de cada dominio, para nao ocupar memoria sem limite
JANELA_VAZAO = 60   # A vazao considera os pedidos que terminaram nos ultimos 60 segundos

# Media de uma lista de numeros (None se a lista estiver vazia)
def media(valores: list) -> float | None:

    if not valores:
        return None

    return round(sum(valores) / len(valores), 1)

# Percentil 95: 95% dos pedidos ficaram abaixo desse valor (mostra o "pior caso" tipico)
def p95(valores: list) -> float | None:

    if not valores:
        return None

    ordenados = sorted(valores)
    posicao = math.ceil(0.95 * len(ordenados)) - 1

    return round(ordenados[posicao], 1)

class RegistroMetricas:

    def __init__(self):

        self.registros = {}   # Para cada dominio, a lista dos ultimos pedidos
        self.erros = {}   # Para cada dominio, quantos pedidos terminaram com erro

    # Guarda as metricas de um pedido que terminou com sucesso
    # inicio e fim sao horarios (time.time()), usados para calcular a vazao
    def registrar(self, dominio: str, inicio: float, fim: float, **dados):

        if dominio not in self.registros:
            self.registros[dominio] = deque(maxlen=MAX_REGISTROS)
        self.registros[dominio].append({"inicio": inicio, "fim": fim, **dados})

    # Conta um pedido que terminou com erro
    def registrar_erro(self, dominio: str):

        self.erros[dominio] = self.erros.get(dominio, 0) + 1

    # Apaga tudo (usado entre as rodadas do experimento)
    def zerar(self):

        self.registros = {}
        self.erros = {}

    # Calcula a vazao: quantos pedidos e tokens o sistema entregou por segundo, somando todos os clientes
    def vazao(self, lista: list) -> dict:

        agora = time.time()
        recentes = [r for r in lista if agora - r["fim"] <= JANELA_VAZAO]
        if not recentes:
            return {"pedidos_por_s": 0, "tokens_por_s": 0, "janela_s": 0}

        # Intervalo entre o primeiro pedido comecar e o ultimo terminar
        duracao = max(r["fim"] for r in recentes) - min(r["inicio"] for r in recentes)
        if duracao <= 0:
            return {"pedidos_por_s": 0, "tokens_por_s": 0, "janela_s": 0}

        total_tokens = sum(r.get("tokens") or 0 for r in recentes)

        return {
            "pedidos_por_s": round(len(recentes) / duracao, 2),
            "tokens_por_s": round(total_tokens / duracao, 1),
            "janela_s": round(duracao, 1),   # Periodo usado no calculo
        }

    # Resumo das metricas de cada dominio, mostrado no /metricas
    def resumo(self) -> dict:

        resultado = {}
        nomes = set(self.registros) | set(self.erros)
        
        for dominio in nomes:
            lista = list(self.registros.get(dominio, []))

            # Junta os valores de cada metrica, ignorando os pedidos que nao tem aquela metrica
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

metricas = RegistroMetricas()   # Registro unico, usado por todo o gateway