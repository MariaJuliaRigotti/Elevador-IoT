# Roteador semântico: decide se a mensagem é comando ou consulta e se vai para o modelo pequeno ou o grande
# Na dúvida, escolhe consulta e modelo grande: errar para comando poderia mover o elevador sem querer

import os
import re
import time
from dataclasses import dataclass, asdict
import httpx
from dominios import OLLAMA_URL, MODELO_CLASSIFICADOR, modelo_por_complexidade
from fila import FILAS, FilaNo, liberar_vaga, ocupar_vaga
from metricas import metricas
from modelos import ANDAR_MAX
from tradutor import sem_acento

# Configuracoes
METODO_PADRAO = os.getenv("ROTEADOR", "hibrido")   # hibrido (padrão), llm ou regras
NO_ROTEADOR = os.getenv("NO_ROTEADOR", OLLAMA_URL)   # Nó Ollama que roda o classificador
LIMITE_ROTEADOR = int(os.getenv("LIMITE_ROTEADOR", "2"))   # Classificações ao mesmo tempo
MAX_TOKENS_ROTULO = 12   # O rótulo ocupa poucos tokens; o limite corta respostas longas
VERSAO_PROMPT = os.getenv("PROMPT_ROTEADOR", "v2")   # v1 é o prompt original, mantido para comparação

METODOS = ("hibrido", "llm", "regras")

# A classificação tem fila própria, que aparece no /saude e no /metricas
FILAS.setdefault("roteador", FilaNo("roteador", LIMITE_ROTEADOR))

DOMINIOS_ROTEAVEIS = ("comando", "consulta")
COMPLEXIDADES = ("simples", "dificil")


# Decisão do roteador para uma mensagem
@dataclass
class Rota:

    dominio: str   # comando ou consulta
    complexidade: str   # simples ou dificil
    metodo: str   # Quem decidiu: regras, llm ou regras (fallback)
    modelo: str   # Modelo escolhido para atender o pedido
    classificacao_ms: float = 0.0   # Tempo gasto para decidir, incluindo a espera na fila
    espera_fila_ms: float = 0.0
    bruto: str = ""   # Resposta do modelo como veio, útil para investigar erros

    # Converte a decisão em dicionário, para enviar na resposta HTTP
    def como_dict(self) -> dict:
        return asdict(self)


# Classificador por regras
# As regras só decidem quando encontram sinais claros; sem eles, o híbrido pergunta ao LLM

# Frases que começam como pergunta
INTERROGATIVA = re.compile(
    r"^(em que|qual|quais|quando|onde|quanto|quantos|quantas|como|por que|porque|o que|quem|que|"
    r"ha|tem|esta|estao|ja|sera que)\b"
)

# Verbos de movimento (inclui "volta", comum em pedidos com duas ações)
VERBO_DE_MOVIMENTO = re.compile(
    r"\b(sobe|subir|suba|desce|descer|desca|vai|ir|va|leva|levar|leve|manda|mandar|chama|chamar|"
    r"volta|voltar)\b"
)
# "vai subir", "vai demorar": aqui o "vai" indica futuro, não é um segundo comando
VAI_AUXILIAR = re.compile(r"\b(vai|vao|va)\s+(?!por\b)(?=\w+r\b)")

# Pedido educado em forma de pergunta: "pode subir pro 3?", "dá pra me levar pro térreo?"
PEDIDO_EDUCADO = re.compile(
    r"^(por favor,?\s*)?(voce\s+|vc\s+)?(pode|poderia|podia|consegue|conseguiria|da pra|daria pra|sera que (da|pode|consegue))\b"
)
# Quem pede informação não está mandando o elevador andar ("pode me dizer se ele vai subir?")
INFORMATIVO = re.compile(
    r"\b(dizer|diz|saber|sabe|informar|informa|falar|fala|explicar|mostrar|mostra)\b"
    r"|\b(compar|expliq|descrev|resum|analis)\w*"
)

# Frase com sujeito ("eu vou descer", "ele sobe") é uma afirmação, não uma ordem
SUJEITO_EXPLICITO = re.compile(r"\b(eu|vou|vamos|ele|ela|alguem)\b")

# Frase com o elevador como sujeito é uma pergunta sobre ele, não uma ordem
SUJEITO_ELEVADOR = re.compile(r"^(o\s+)?(elevador|ele)\b")

# Andar dito de forma explícita: "andar 3", "térreo", "segundo", "último" (aceita "terre", erro comum da voz)
ANDAR_EXPLICITO = re.compile(
    r"\bandar\s+(\d+|zero|um|dois|tres|quatro)\b"
    r"|\b\d+\s*o?\s*andar\b"
    r"|\b(terr\w*|zero|primeiro|segundo|terceiro|ultimo|cobertura|topo)\b"
)
# "andar N" sem verbo pode ser um comando mal reconhecido pela voz, então o LLM decide
ANDAR_COM_NUMERO = re.compile(r"\bandar\s+\d+|\b\d+\s*o?\s*andar")

# Sinais de pedido difícil: andar relativo, estimativa, comparação, explicação, condição ou mais de uma ação
ANDARES_RELATIVOS = re.compile(r"\b(um|uma|dois|duas|tres|quatro|cinco|\d+)\s+andar(es)?\b")
CONDICIONAL = re.compile(r"\b(se|senao|caso)\b")
PALAVRAS_DIFICEIS = re.compile(
    r"\b(acima|abaixo|cima|baixo|anterior|seguinte|proximo|penultimo|antepenultimo|metade|meio|dobro|triplo|"
    r"depois|antes|tempo|demora|demorar|demorou|quanto|quantos|quantas|por que|porque|diferenca|estimativa|"
    r"daqui|passar|passa|ainda|faltam|falta|telemetria|"
    r"mais\s+(perto|proximo|longe|distante|rapido|demorado)|"
    r"compar\w*|expliq\w*|descrev\w*|resum\w*|analis\w*)\b"
)

# Perguntas diretas sobre o estado do elevador (andar, porta, movimento): consulta simples
ESTADO_SIMPLES = re.compile(
    r"\b(onde|andar|porta|parad[oa]|subindo|descendo|online|offline|funcionando|aberta|fechada|"
    r"abriu|fechou|chegou|terreo)\b"
)
# Palavras ligadas ao elevador; sem nenhuma delas, a pergunta é sobre outro assunto
VOCABULARIO_ELEVADOR = re.compile(
    r"\b(elevador|ele|andar|andares|porta|terreo|sobe|subir|desce|descer|cabine|predio|subindo|descendo)\b"
)

LIMITE_PALAVRAS_SIMPLES = 10   # Frases maiores que isso não contam como simples
LIMITE_PALAVRAS_DIFICIL = 14   # Frases maiores que isso contam como difíceis


# Decide domínio e complexidade com as regras e diz se tem certeza da decisão
def classificar_com_confianca(mensagem: str) -> tuple[str, str, bool]:

    t = sem_acento(mensagem).strip()
    palavras = t.split()

    tem_pergunta = "?" in t or bool(INTERROGATIVA.search(t))
    educado = bool(PEDIDO_EDUCADO.search(t))
    informativo = bool(INFORMATIVO.search(t))
    verbos = len(VERBO_DE_MOVIMENTO.findall(VAI_AUXILIAR.sub("", t)))

    explicito = bool(ANDAR_EXPLICITO.search(t))
    sinais_dificeis = bool(
        ANDARES_RELATIVOS.search(t)
        or PALAVRAS_DIFICEIS.search(t)
        or CONDICIONAL.search(t)
        or verbos >= 2
        or len(palavras) > LIMITE_PALAVRAS_DIFICIL
    )

    # Comando: tem verbo de movimento e não é pergunta (a não ser que seja um pedido educado)
    if verbos and not informativo and (not tem_pergunta or educado):
        if sinais_dificeis:
            return "comando", "dificil", True   # Errar para difícil só custa tempo
        if explicito and verbos == 1 and len(palavras) <= LIMITE_PALAVRAS_SIMPLES and not SUJEITO_EXPLICITO.search(t):
            return "comando", "simples", True   # Verbo e andar explícito, nada mais
        return "consulta", "dificil", False   # Tem verbo, mas o destino não ficou claro: o LLM decide

    # Consulta: só tem certeza se a frase pede informação (pergunta, "explique", "compare")
    # Uma afirmação como "avança três andares" pode ser um comando com verbo desconhecido
    if sinais_dificeis and (tem_pergunta or informativo):
        return "consulta", "dificil", True

    curta = len(palavras) <= LIMITE_PALAVRAS_SIMPLES
    if (tem_pergunta or informativo) and curta:
        if SUJEITO_ELEVADOR.search(t) or ESTADO_SIMPLES.search(t):
            return "consulta", "simples", True
        if not VOCABULARIO_ELEVADOR.search(t) and not ANDAR_COM_NUMERO.search(t):
            return "consulta", "simples", True   # Assunto sem relação com o elevador

    # Sem sinais claros (ex.: "andar 3" sem verbo): o LLM decide
    return "consulta", "dificil", False


# Mesma decisão sem a certeza, usada no método regras e quando o LLM falha
def classificar_por_regras(mensagem: str) -> tuple[str, str]:

    dominio, complexidade, _ = classificar_com_confianca(mensagem)
    return dominio, complexidade


# Classificador por LLM

# Prompt original (v1). 5 dos 9 exemplos estão no conjunto_roteador.csv, o que melhora o resultado artificialmente
PROMPT_CLASSIFICADOR_V1 = f"""Você classifica pedidos feitos a um assistente de um prédio com elevador (andares 0 a {ANDAR_MAX}, sendo 0 o térreo).
Responda com UMA palavra, exatamente um destes rótulos:
comando_simples, comando_dificil, consulta_simples, consulta_dificil

- comando: o usuário quer que o elevador se MOVA (sobe, desce, vai, leva).
- consulta: o usuário pergunta ou quer saber algo (andar atual, se está parado, tempo, porta). Assunto que não tem relação com o elevador também é consulta.
- simples: o pedido é direto, com o andar dito explicitamente.
- dificil: exige conta ou raciocínio: andar relativo ao atual, mais de uma ação, estimativa de tempo ou condição.
Na dúvida entre comando e consulta, responda consulta.
O texto pode vir de reconhecimento de voz e ter erros de grafia.

Exemplos:
sobe pro andar 3 => comando_simples
desce pro térreo => comando_simples
me leva até o segundo => comando_simples
desce dois andares => comando_dificil
vai pro andar de cima e depois volta pro térreo => comando_dificil
onde está o elevador? => consulta_simples
a porta está aberta? => consulta_simples
quanto tempo até ele chegar no andar 3 se eu chamar agora? => consulta_dificil
qual a capital da França? => consulta_simples"""

# Prompt v2: os exemplos não estão em nenhum conjunto de avaliação e cobrem as frases em que as regras têm dúvida
PROMPT_CLASSIFICADOR_V2 = f"""Você classifica pedidos feitos a um assistente de um prédio com elevador (andares 0 a {ANDAR_MAX}, sendo 0 o térreo).
Responda com UMA palavra, exatamente um destes rótulos:
comando_simples, comando_dificil, consulta_simples, consulta_dificil

DOMÍNIO
- comando: o usuário quer que o elevador se MOVA para um andar (sobe, desce, vai, leva, "pro térreo, por favor"). Pedido educado ("pode me levar pro 2?") também é comando.
- consulta: o usuário quer SABER algo: onde está o elevador, se a porta está aberta, se está subindo, tempo, motivos. Pergunta cujo sujeito é o elevador ("ele vai subir?") é consulta. Assunto sem relação com o elevador também é consulta.

COMPLEXIDADE
- simples: um fato direto. Em comando: uma ação com o andar dito explicitamente. Em consulta: uma pergunta de estado atual, ou de outro assunto, que não exige conta.
- dificil: exige conta ou raciocínio. Em comando: andar relativo ao atual ("sobe 2 andares", "andar de cima"), mais de uma ação, condição. Em consulta: estimativa de tempo ou distância, comparação, explicação, "se ... então".

Na dúvida entre comando e consulta, responda consulta.
O texto pode vir de reconhecimento de voz e ter erros de grafia.

Exemplos:
leva a gente pro quarto andar => comando_simples
sobi pro andar 2 => comando_simples
pro último, por favor => comando_simples
sobe quatro andares => comando_dificil
desce pro 1 e volta pro 3 => comando_dificil
o elevador já chegou? => consulta_simples
tem alguém na cabine? => consulta_simples
quem descobriu o Brasil? => consulta_simples
em quanto tempo ele chega aqui? => consulta_dificil
ele está mais perto do 0 ou do 2? => consulta_dificil
por que ele não abre a porta? => consulta_dificil"""

PROMPTS = {"v1": PROMPT_CLASSIFICADOR_V1, "v2": PROMPT_CLASSIFICADOR_V2}
PROMPT_CLASSIFICADOR = PROMPTS.get(VERSAO_PROMPT, PROMPT_CLASSIFICADOR_V2)

ROTULO = re.compile(r"(comando|consulta)_(simples|dificil)")


# Tira o domínio e a complexidade da resposta do modelo (erro se não houver um rótulo válido)
def interpretar_rotulo(bruto: str) -> tuple[str, str]:

    m = ROTULO.search(sem_acento(bruto))
    if not m:
        raise ValueError(f"rótulo inválido: {bruto[:60]!r}")
    return m.group(1), m.group(2)


# Pergunta ao modelo pequeno qual é o rótulo da mensagem
async def perguntar_ao_modelo(mensagem: str) -> str:

    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as http:
        r = await http.post(
            f"{NO_ROTEADOR}/api/chat",
            json={
                "model": MODELO_CLASSIFICADOR,
                "messages": [
                    {"role": "system", "content": PROMPT_CLASSIFICADOR},
                    {"role": "user", "content": mensagem},
                ],
                "stream": False,
                "options": {"temperature": 0, "num_predict": MAX_TOKENS_ROTULO},
            },
        )
        r.raise_for_status()
        return r.json()["message"]["content"]


# Trava de segurança do híbrido: uma pergunta só vira comando se for um pedido educado
# O modelo pequeno às vezes classifica "ele está subindo?" como comando
def aplicar_trava(mensagem: str, dominio: str, complexidade: str) -> tuple[str, str]:

    t = sem_acento(mensagem).strip()
    if dominio == "comando" and "?" in t and not PEDIDO_EDUCADO.search(t):
        return "consulta", complexidade
    return dominio, complexidade


# Classifica com o LLM. Se o nó cair ou a resposta não tiver um rótulo, usa as regras
async def classificar_com_llm(mensagem: str, trava: bool = False) -> Rota:

    fila = FILAS["roteador"]
    horario_inicio = time.time()
    inicio = time.perf_counter()
    espera = await ocupar_vaga(fila, NO_ROTEADOR, MAX_TOKENS_ROTULO)   # Fila do roteador e depois a do nó
    bruto = ""
    try:
        bruto = await perguntar_ao_modelo(mensagem)
        dominio, complexidade = interpretar_rotulo(bruto)
        if trava:
            dominio, complexidade = aplicar_trava(mensagem, dominio, complexidade)
        usado = "llm"
    except (httpx.HTTPError, ValueError, KeyError) as e:
        # Nó fora do ar ou resposta sem rótulo: decide pelas regras, sem derrubar o pedido
        print(f"[roteador] LLM falhou ({type(e).__name__}: {e}), usando regras", flush=True)
        metricas.registrar_erro("roteador")
        dominio, complexidade = classificar_por_regras(mensagem)
        usado = "regras (fallback)"
    finally:
        liberar_vaga(fila, NO_ROTEADOR)

    total_ms = round((time.perf_counter() - inicio) * 1000, 1)
    if usado == "llm":
        metricas.registrar("roteador", inicio=horario_inicio, fim=time.time(),
                           tempo_ms=total_ms, espera_fila_ms=round(espera * 1000, 1))

    return Rota(dominio, complexidade, usado, modelo_por_complexidade(complexidade),
                classificacao_ms=total_ms, espera_fila_ms=round(espera * 1000, 1), bruto=bruto)


# Decide a rota de uma mensagem
async def classificar(mensagem: str, metodo: str | None = None) -> Rota:

    metodo = metodo or METODO_PADRAO
    if metodo not in METODOS:
        raise ValueError(f"método de roteamento desconhecido: {metodo}")

    # Regras (e primeira etapa do híbrido): não usam o LLM, então quase não têm custo
    if metodo in ("regras", "hibrido"):
        inicio = time.perf_counter()
        dominio, complexidade, confiante = classificar_com_confianca(mensagem)
        if metodo == "regras" or confiante:
            return Rota(dominio, complexidade, "regras", modelo_por_complexidade(complexidade),
                        classificacao_ms=round((time.perf_counter() - inicio) * 1000, 2))

        # No híbrido, quando as regras ficam em dúvida, o LLM decide
        rota = await classificar_com_llm(mensagem, trava=True)
        rota.classificacao_ms = round((time.perf_counter() - inicio) * 1000, 1)
        return rota

    # Só o LLM (usado para comparar no experimento)
    return await classificar_com_llm(mensagem)