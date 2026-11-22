"""Escalonamento nao preemptivo por dominio: FIFO ou menor resposta estimada."""
import asyncio
import heapq
import itertools
import os
import time
from dominios import DOMINIOS

POLITICA_FILA = os.getenv('POLITICA_FILA', 'fifo').strip().lower()
if POLITICA_FILA not in ('fifo', 'curtas'):
    raise ValueError('POLITICA_FILA deve ser fifo ou curtas')

class FilaNo:
    def __init__(self, nome: str, limite: int, politica: str = None):
        self.nome = nome
        self.limite = limite
        self.politica = politica or POLITICA_FILA
        if self.politica not in ('fifo', 'curtas'):
            raise ValueError('politica invalida')
        self.atendendo = 0
        self.atendidos = 0
        self._sequencia = itertools.count()
        self._espera = []  # (prioridade, ordem, future)

    @property
    def esperando(self):
        return sum(not futuro.done() for _, _, futuro in self._espera)

    async def entrar(self, tokens_estimados: int = 100) -> float:
        if tokens_estimados < 1:
            raise ValueError('tokens_estimados deve ser positivo')
        inicio = time.perf_counter()
        # Sem await entre testar e ocupar: operacao atomica no event loop.
        if self.atendendo < self.limite and not self._espera:
            self.atendendo += 1
            return time.perf_counter() - inicio
        futuro = asyncio.get_running_loop().create_future()
        prioridade = tokens_estimados if self.politica == 'curtas' else 0
        heapq.heappush(self._espera, (prioridade, next(self._sequencia), futuro))
        try:
            await futuro
        except asyncio.CancelledError:
            # Se a vaga ja foi reservada, devolve-a a outro cliente.
            if futuro.done() and not futuro.cancelled():
                self.sair()
            else:
                futuro.cancel()
            raise
        return time.perf_counter() - inicio

    def sair(self):
        if self.atendendo <= 0:
            raise RuntimeError('saida sem entrada na fila')
        self.atendidos += 1
        while self._espera:
            _, _, futuro = heapq.heappop(self._espera)
            if not futuro.done():
                # Transfere a vaga: atendendo nao muda.
                futuro.set_result(None)
                return
        self.atendendo -= 1

    def estado(self) -> dict:
        return {'limite': self.limite, 'atendendo': self.atendendo,
                'na_fila': self.esperando, 'atendidos': self.atendidos,
                'politica': self.politica}

FILAS = {nome: FilaNo(nome, config['max_simultaneos']) for nome, config in DOMINIOS.items()}