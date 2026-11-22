"""Testes locais do escalonador; nao requer Ollama nem MQTT."""
import asyncio
import sys
sys.path.insert(0, 'gateway')
from fila import FilaNo

async def testar(politica):
    fila = FilaNo('teste', 1, politica)
    await fila.entrar()
    ordem = []
    async def tarefa(nome, tamanho):
        await fila.entrar(tamanho)
        ordem.append(nome)
        fila.sair()
    tarefas = [asyncio.create_task(tarefa('longa', 200)),
               asyncio.create_task(tarefa('curta', 30)),
               asyncio.create_task(tarefa('media', 100))]
    await asyncio.sleep(0.01)
    assert fila.esperando == 3
    fila.sair()
    await asyncio.gather(*tarefas)
    assert ordem == (['longa', 'curta', 'media'] if politica == 'fifo' else ['curta', 'media', 'longa']), ordem
    assert fila.atendendo == 0 and fila.esperando == 0
    print(politica, 'OK:', ordem)

async def cancelamento():
    fila = FilaNo('teste', 1, 'curtas')
    await fila.entrar()
    tarefa = asyncio.create_task(fila.entrar(10))
    await asyncio.sleep(0)
    tarefa.cancel()
    try:
        await tarefa
    except asyncio.CancelledError:
        pass
    assert fila.esperando == 0
    fila.sair()
    assert fila.atendendo == 0
    print('cancelamento OK')

async def main():
    await testar('fifo')
    await testar('curtas')
    await cancelamento()

asyncio.run(main())