# Filas do gateway: cada nó Ollama tem um limite de vagas e, dentro dele, cada domínio tem as suas

import asyncio
import heapq
import itertools
import os
import time
from dominios import DOMINIOS

# Configuracoes
POLITICA_FILA = os.getenv("POLITICA_FILA", "fifo").strip().lower()   # FIFO (ordem de chegada) ou curtas (respostas menores primeiro)
POLITICAS = ("fifo", "curtas")
if POLITICA_FILA not in POLITICAS:
    raise ValueError("POLITICA_FILA deve ser fifo ou curtas")
LIMITE_NO = int(os.getenv("LIMITE_NO", "2"))   # Pedidos ao mesmo tempo em cada nó (igual ao OLLAMA_NUM_PARALLEL)


# Fila com limite de vagas, usada tanto para os domínios quanto para os nós
# Quem já está sendo atendido nunca é interrompido, só a ordem de quem espera pode mudar
class FilaNo:

    def __init__(self, nome: str, limite: int, politica: str | None = None):

        self.nome = nome
        self.limite = limite   # Quantos pedidos podem ser atendidos ao mesmo tempo
        self.politica = politica or POLITICA_FILA
        if self.politica not in POLITICAS:
            raise ValueError("politica invalida")
        self.atendendo = 0   # Pedidos sendo atendidos agora
        self.atendidos = 0   # Total de pedidos que já saíram da fila
        self._sequencia = itertools.count()   # Ordem de chegada, usada para desempatar
        self._espera = []   # Pedidos esperando: (prioridade, ordem de chegada, future)

    # Quantos pedidos estão esperando (sem contar os que desistiram)
    @property
    def esperando(self):
        return sum(not futuro.done() for _, _, futuro in self._espera)

    # Espera uma vaga e retorna quanto tempo o pedido ficou na fila
    async def entrar(self, tokens_estimados: int = 100) -> float:

        if tokens_estimados < 1:
            raise ValueError("tokens_estimados deve ser positivo")
        inicio = time.perf_counter()

        # Se há vaga e ninguém esperando, entra direto
        if self.atendendo < self.limite and not self._espera:
            self.atendendo += 1
            return time.perf_counter() - inicio

        # Sem vaga: entra na fila e espera ser chamado por sair()
        futuro = asyncio.get_running_loop().create_future()
        if self.politica == "curtas":
            prioridade = tokens_estimados   # Quanto menor a resposta, mais cedo é atendido
        else:
            prioridade = 0   # Na fifo todos têm a mesma prioridade, então vale a ordem de chegada
        heapq.heappush(self._espera, (prioridade, next(self._sequencia), futuro))

        try:
            await futuro
        except asyncio.CancelledError:
            # O cliente desistiu. Se a vaga já era dele, passa para o próximo
            if futuro.done() and not futuro.cancelled():
                self.sair()
            else:
                futuro.cancel()
            raise

        return time.perf_counter() - inicio

    # Libera a vaga: passa para o próximo da fila ou, se ninguém espera, deixa a vaga livre
    def sair(self):

        if self.atendendo <= 0:
            raise RuntimeError("saida sem entrada na fila")
        self.atendidos += 1

        while self._espera:
            _, _, futuro = heapq.heappop(self._espera)
            if not futuro.done():   # Pula quem desistiu enquanto esperava
                futuro.set_result(None)   # A vaga passa direto para esse pedido
                return

        self.atendendo -= 1

    # Situação da fila, mostrada no /saude e no /metricas
    def estado(self) -> dict:

        return {
            "limite": self.limite,
            "atendendo": self.atendendo,
            "na_fila": self.esperando,
            "atendidos": self.atendidos,
            "politica": self.politica,
        }


# Uma fila para cada domínio, com as vagas definidas em dominios.py
FILAS = {nome: FilaNo(nome, config["max_simultaneos"]) for nome, config in DOMINIOS.items()}

# Uma fila para cada nó Ollama, identificado pelo endereço
FILAS_NO = {}


# Retorna a fila do nó com esse endereço, criando-a na primeira vez
def fila_do_no(url_no: str) -> FilaNo:

    if url_no not in FILAS_NO:
        FILAS_NO[url_no] = FilaNo(url_no, LIMITE_NO)
    return FILAS_NO[url_no]


# Cria as filas dos nós já no início, para elas aparecerem no /saude antes do primeiro pedido
for config in DOMINIOS.values():
    fila_do_no(config["no"])


# Ocupa uma vaga no domínio e depois no nó, sempre nessa ordem, e retorna o tempo total de espera
async def ocupar_vaga(fila_dominio: FilaNo, url_no: str, tokens_estimados: int = 100) -> float:

    espera = await fila_dominio.entrar(tokens_estimados)

    try:
        espera += await fila_do_no(url_no).entrar(tokens_estimados)
    except BaseException:
        fila_dominio.sair()   # Desistiu esperando o nó: devolve a vaga do domínio
        raise
    
    return espera


# Libera as duas vagas ocupadas por ocupar_vaga
def liberar_vaga(fila_dominio: FilaNo, url_no: str):

    fila_do_no(url_no).sair()
    fila_dominio.sair()