# Testa as filas do gateway sem precisar do Docker, do Ollama ou do broker

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "gateway"))   # Permite importar os arquivos da pasta gateway
from fila import FilaNo, fila_do_no, liberar_vaga, ocupar_vaga   # noqa: E402


# Enfileira três pedidos (longa, curta e média) e confere a ordem em que são atendidos
async def testar_ordem(politica: str):

    fila = FilaNo("teste", 1, politica)
    await fila.entrar()   # Ocupa a única vaga, para os próximos precisarem esperar
    ordem = []

    # Um pedido: espera a vez, anota o nome e libera a vaga
    async def pedido(nome: str, tamanho: int):

        await fila.entrar(tamanho)
        ordem.append(nome)
        fila.sair()

    tarefas = [
        asyncio.create_task(pedido("longa", 200)),
        asyncio.create_task(pedido("curta", 30)),
        asyncio.create_task(pedido("media", 100)),
    ]
    await asyncio.sleep(0.01)   # Deixa os três entrarem na fila
    assert fila.esperando == 3

    fila.sair()   # Libera a vaga: a partir daqui os pedidos são atendidos um a um
    await asyncio.gather(*tarefas)

    if politica == "fifo":
        esperado = ["longa", "curta", "media"]   # Ordem de chegada
    else:
        esperado = ["curta", "media", "longa"]   # Menor resposta primeiro
    assert ordem == esperado, ordem
    assert fila.atendendo == 0 and fila.esperando == 0
    print(f"{politica} OK: {ordem}")


# Um pedido desiste enquanto espera: a fila não pode ficar com vaga presa nem com pedido esquecido
async def testar_cancelamento():

    fila = FilaNo("teste", 1, "curtas")
    await fila.entrar()
    tarefa = asyncio.create_task(fila.entrar(10))
    await asyncio.sleep(0)   # Deixa o pedido entrar na fila
    tarefa.cancel()
    try:
        await tarefa
    except asyncio.CancelledError:
        pass

    assert fila.esperando == 0
    fila.sair()
    assert fila.atendendo == 0
    print("cancelamento OK")


# Dois domínios num nó com uma vaga só: o segundo espera, mesmo tendo vaga na fila dele
async def testar_limite_do_no():

    url = "http://no-de-teste:11434"
    fila_do_no(url).limite = 1
    dominio_a = FilaNo("a", 1, "fifo")
    dominio_b = FilaNo("b", 1, "fifo")

    await ocupar_vaga(dominio_a, url)   # O domínio a ocupa a única vaga do nó
    tarefa_b = asyncio.create_task(ocupar_vaga(dominio_b, url))
    await asyncio.sleep(0.01)
    assert not tarefa_b.done()   # O domínio b tem vaga, mas o nó está cheio
    assert dominio_b.atendendo == 1 and fila_do_no(url).esperando == 1

    liberar_vaga(dominio_a, url)   # O nó libera e passa a vaga para o domínio b
    await asyncio.wait_for(tarefa_b, 1)
    liberar_vaga(dominio_b, url)
    assert fila_do_no(url).atendendo == 0 and dominio_a.atendendo == 0 and dominio_b.atendendo == 0
    print("limite do no OK")


# Roda os quatro testes em sequência
async def main():

    await testar_ordem("fifo")
    await testar_ordem("curtas")
    await testar_cancelamento()
    await testar_limite_do_no()


if __name__ == "__main__":
    asyncio.run(main())