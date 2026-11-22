# Roteamento semantico: um classificador le a mensagem e decide sozinho
#   1. o DOMINIO que vai atende-la (comando ou consulta)
#   2. a COMPLEXIDADE do pedido (simples ou dificil), que define o tamanho do modelo:
#      simples -> modelo pequeno (rapido), dificil -> modelo grande (mais capaz)
#
# Tres metodos, que podem ser comparados no experimento:
#   - "regras":  regex, sem custo de inferencia; tambem serve de fallback se o no do LLM cair
#   - "llm":     modelo pequeno do Ollama, que responde com UM rotulo (ex.: "comando_simples")
#   - "hibrido": (padrao) as regras decidem quando TEM CERTEZA; so as frases em que elas
#                duvidam vao para o LLM. Mantem o custo de ~0 ms na maioria dos pedidos
#
# Decisao de projeto: na duvida, o destino e SEMPRE "consulta" e a complexidade e "dificil".
# Errar para consulta so produz uma resposta inutil; errar para comando poderia mover o elevador
# sem o usuario querer. Errar para "dificil" so custa tempo (modelo maior); errar para "simples"
# num pedido que exige conta pode mandar o elevador para o andar errado
# Mesmo quando a rota e comando, o comando ainda passa pela validacao do Pydantic

import os
import re
import time
from dataclasses import dataclass, asdict

import httpx

from dominios import OLLAMA_URL, MODELO_CLASSIFICADOR, modelo_por_complexidade
from fila import FILAS, FilaNo
from metricas import metricas
from modelos import ANDAR_MAX
from tradutor import sem_acento

# Configuracoes
METODO_PADRAO = os.getenv("ROTEADOR", "hibrido")   # hibrido | llm | regras
NO_ROTEADOR = os.getenv("NO_ROTEADOR", OLLAMA_URL)   # No Ollama que roda o classificador
LIMITE_ROTEADOR = int(os.getenv("LIMITE_ROTEADOR", "2"))   # Classificacoes simultaneas
MAX_TOKENS_ROTULO = 12   # O rotulo mais longo ("consulta_dificil") ocupa poucos tokens; o limite evita respostas longas
VERSAO_PROMPT = os.getenv("PROMPT_ROTEADOR", "v2")   # v1 = prompt original (para comparar no experimento)

METODOS = ("hibrido", "llm", "regras")

# A classificacao tem fila propria, que aparece no /saude e no /metricas
FILAS.setdefault("roteador", FilaNo("roteador", LIMITE_ROTEADOR))

DOMINIOS_ROTEAVEIS = ("comando", "consulta")
COMPLEXIDADES = ("simples", "dificil")


@dataclass
class Rota:

    dominio: str   # comando | consulta
    complexidade: str   # simples | dificil
    metodo: str   # regras | llm | regras (fallback)  -- quem de fato decidiu
    modelo: str   # Modelo escolhido para atender o pedido
    classificacao_ms: float = 0.0   # Tempo gasto so para decidir a rota (inclui a espera na fila)
    espera_fila_ms: float = 0.0
    bruto: str = ""   # Resposta crua do classificador (util para depurar erros)

    def como_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- classificador por regras
#
# As regras nao tentam "entender" tudo. Elas procuram EVIDENCIA POSITIVA de que a frase e de um
# tipo conhecido; se nao encontram, devolvem confiante=False e o hibrido pergunta ao LLM

# Frases que comecam como pergunta
INTERROGATIVA = re.compile(
    r"^(em que|qual|quais|quando|onde|quanto|quantos|quantas|como|por que|porque|o que|quem|que|"
    r"ha|tem|esta|estao|ja|sera que)\b"
)

# Verbos de movimento (a lista inclui "volta", usada em pedidos de duas acoes)
VERBO_DE_MOVIMENTO = re.compile(
    r"\b(sobe|subir|suba|desce|descer|desca|vai|ir|va|leva|levar|leve|manda|mandar|chama|chamar|"
    r"volta|voltar)\b"
)
# "vai subir", "vai demorar": aqui o "vai" e auxiliar do futuro e nao um segundo comando
VAI_AUXILIAR = re.compile(r"\b(vai|vao|va)\s+(?!por\b)(?=\w+r\b)")

# Pedido educado em forma de pergunta: "pode subir pro 3?", "da pra me levar pro terreo?"
PEDIDO_EDUCADO = re.compile(
    r"^(por favor,?\s*)?(voce\s+|vc\s+)?(pode|poderia|podia|consegue|conseguiria|da pra|daria pra|sera que (da|pode|consegue))\b"
)
# Quem pede informacao nao esta mandando o elevador andar ("pode me dizer se ele vai subir?")
INFORMATIVO = re.compile(
    r"\b(dizer|diz|saber|sabe|informar|informa|falar|fala|explicar|mostrar|mostra)\b"
    r"|\b(compar|expliq|descrev|resum|analis)\w*"
)

# Sujeito explicito ("eu vou descer", "vamos pro 3", "ele sobe"): afirmacao, nao ordem. Comando confiante exige forma de ordem
SUJEITO_EXPLICITO = re.compile(r"\b(eu|vou|vamos|ele|ela|alguem)\b")

# A frase tem o proprio elevador como sujeito: e uma pergunta SOBRE ele, nao uma ordem a ele
SUJEITO_ELEVADOR = re.compile(r"^(o\s+)?(elevador|ele)\b")

# Andar dito de forma explicita: "andar 3", "andar tres", "terreo", "segundo", "ultimo" (aceita "terre", erro de voz)
ANDAR_EXPLICITO = re.compile(
    r"\bandar\s+(\d+|zero|um|dois|tres|quatro)\b"
    r"|\b\d+\s*o?\s*andar\b"
    r"|\b(terr\w*|zero|primeiro|segundo|terceiro|ultimo|cobertura|topo)\b"
)
# "andar N" sem verbo: pode ser um comando mal reconhecido pela voz, entao o LLM decide
ANDAR_COM_NUMERO = re.compile(r"\bandar\s+\d+|\b\d+\s*o?\s*andar")

# Sinais de pedido dificil: andar relativo, estimativa, comparacao, explicacao, condicao ou mais de uma acao
ANDARES_RELATIVOS = re.compile(r"\b(um|uma|dois|duas|tres|quatro|cinco|\d+)\s+andar(es)?\b")
CONDICIONAL = re.compile(r"\b(se|senao|caso)\b")
PALAVRAS_DIFICEIS = re.compile(
    r"\b(acima|abaixo|cima|baixo|anterior|seguinte|proximo|penultimo|antepenultimo|metade|meio|dobro|triplo|"
    r"depois|antes|tempo|demora|demorar|demorou|quanto|quantos|quantas|por que|porque|diferenca|estimativa|"
    r"daqui|passar|passa|ainda|faltam|falta|telemetria|"
    r"mais\s+(perto|proximo|longe|distante|rapido|demorado)|"
    r"compar\w*|expliq\w*|descrev\w*|resum\w*|analis\w*)\b"
)

# Perguntas diretas sobre o estado do elevador (andar, porta, movimento, conexao): consulta simples
ESTADO_SIMPLES = re.compile(
    r"\b(onde|andar|porta|parad[oa]|subindo|descendo|online|offline|funcionando|aberta|fechada|"
    r"abriu|fechou|chegou|terreo)\b"
)
# Qualquer assunto ligado ao elevador; sem nenhuma dessas palavras, a pergunta e de outro assunto
VOCABULARIO_ELEVADOR = re.compile(
    r"\b(elevador|ele|andar|andares|porta|terreo|sobe|subir|desce|descer|cabine|predio|subindo|descendo)\b"
)

LIMITE_PALAVRAS_SIMPLES = 10
LIMITE_PALAVRAS_DIFICIL = 14


# Decide dominio e complexidade com regex e diz se TEM CERTEZA (confiante). Retorna (dominio, complexidade, confiante)
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

    # 1. Comando: ha verbo de movimento e a frase nao e uma pergunta (a menos que seja um pedido educado)
    if verbos and not informativo and (not tem_pergunta or educado):
        if sinais_dificeis:
            return "comando", "dificil", True   # Errar para "dificil" so custa tempo
        if explicito and verbos == 1 and len(palavras) <= LIMITE_PALAVRAS_SIMPLES and not SUJEITO_EXPLICITO.search(t):
            return "comando", "simples", True   # Verbo + andar explicito e nada mais: caso classico
        return "consulta", "dificil", False   # Tem verbo mas nao ficou claro o destino: o LLM decide

    # 2. Consulta, com evidencia positiva
    # So ha certeza de que e consulta se a frase PEDE informacao (pergunta ou "explique", "compare"...).
    # Uma afirmacao com sinal dificil ("avanca tres andares pra cima") pode ser comando com verbo desconhecido
    if sinais_dificeis and (tem_pergunta or informativo):
        return "consulta", "dificil", True

    curta = len(palavras) <= LIMITE_PALAVRAS_SIMPLES
    if (tem_pergunta or informativo) and curta:
        if SUJEITO_ELEVADOR.search(t) or ESTADO_SIMPLES.search(t):
            return "consulta", "simples", True
        if not VOCABULARIO_ELEVADOR.search(t) and not ANDAR_COM_NUMERO.search(t):
            return "consulta", "simples", True   # Assunto sem relacao com o elevador

    # 3. Sem evidencia: "andar 3" sem verbo, frase sem pontuacao e sem pergunta, vocabulario desconhecido...
    return "consulta", "dificil", False


# Versao simples (sem a confianca), usada pelo metodo "regras" e como fallback quando o LLM cai
def classificar_por_regras(mensagem: str) -> tuple[str, str]:

    dominio, complexidade, _ = classificar_com_confianca(mensagem)
    return dominio, complexidade


# ---------------------------------------------------------------- classificador por LLM

# Prompt original (v1). Atencao: 5 dos 9 exemplos estao no conjunto_roteador.csv, o que infla o resultado
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

# Prompt v2: definicoes por dominio e exemplos que NAO estao em nenhum conjunto de avaliacao.
# Como o hibrido so manda ao LLM as frases em que as regras duvidam, os exemplos cobrem esse tipo de frase
# (voz com erro, pedido sem verbo, pergunta sobre o elevador feita de forma indireta)
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


# Extrai (dominio, complexidade) da resposta crua do modelo; ValueError se nao houver um rotulo valido
def interpretar_rotulo(bruto: str) -> tuple[str, str]:

    m = ROTULO.search(sem_acento(bruto))
    if not m:
        raise ValueError(f"rótulo inválido: {bruto[:60]!r}")
    return m.group(1), m.group(2)


# Pergunta ao modelo pequeno qual e o rotulo da mensagem
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


# Trava de seguranca do hibrido: pergunta que nao e pedido educado NUNCA vira comando.
# O LLM pequeno as vezes classifica "ele esta subindo?" como comando; mover o elevador por engano e o pior erro
def aplicar_trava(mensagem: str, dominio: str, complexidade: str) -> tuple[str, str]:

    t = sem_acento(mensagem).strip()
    if dominio == "comando" and "?" in t and not PEDIDO_EDUCADO.search(t):
        return "consulta", complexidade
    return dominio, complexidade


# ---------------------------------------------------------------- funcao usada pelo gateway

# Classifica com o LLM (fila propria). Se o no cair ou o rotulo vier ilegivel, usa as regras
async def classificar_com_llm(mensagem: str, trava: bool = False) -> Rota:

    fila = FILAS["roteador"]
    horario_inicio = time.time()
    inicio = time.perf_counter()
    espera = await fila.entrar()
    bruto = ""
    try:
        bruto = await perguntar_ao_modelo(mensagem)
        dominio, complexidade = interpretar_rotulo(bruto)
        if trava:
            dominio, complexidade = aplicar_trava(mensagem, dominio, complexidade)
        usado = "llm"
    except (httpx.HTTPError, ValueError, KeyError) as e:
        # No fora do ar ou rotulo ilegivel: nao derruba o pedido, decide pelas regras
        print(f"[roteador] LLM falhou ({type(e).__name__}: {e}), usando regras", flush=True)
        metricas.registrar_erro("roteador")
        dominio, complexidade = classificar_por_regras(mensagem)
        usado = "regras (fallback)"
    finally:
        fila.sair()

    total_ms = round((time.perf_counter() - inicio) * 1000, 1)
    if usado == "llm":
        metricas.registrar("roteador", inicio=horario_inicio, fim=time.time(),
                           tempo_ms=total_ms, espera_fila_ms=round(espera * 1000, 1))

    return Rota(dominio, complexidade, usado, modelo_por_complexidade(complexidade),
                classificacao_ms=total_ms, espera_fila_ms=round(espera * 1000, 1), bruto=bruto)


# Decide a rota de uma mensagem. 'metodo' (hibrido | llm | regras) sobrescreve o padrao, usado nos experimentos
async def classificar(mensagem: str, metodo: str | None = None) -> Rota:

    metodo = metodo or METODO_PADRAO
    if metodo not in METODOS:
        raise ValueError(f"método de roteamento desconhecido: {metodo}")

    # Regras (e a 1a etapa do hibrido): sem fila e sem metrica de inferencia, o custo e desprezivel
    if metodo in ("regras", "hibrido"):
        inicio = time.perf_counter()
        dominio, complexidade, confiante = classificar_com_confianca(mensagem)
        if metodo == "regras" or confiante:
            return Rota(dominio, complexidade, "regras", modelo_por_complexidade(complexidade),
                        classificacao_ms=round((time.perf_counter() - inicio) * 1000, 2))

        # Hibrido e as regras estao em duvida: o LLM decide. O tempo das regras entra na conta
        rota = await classificar_com_llm(mensagem, trava=True)
        rota.classificacao_ms = round((time.perf_counter() - inicio) * 1000, 1)
        return rota

    # LLM puro (usado so para comparar no experimento)
    return await classificar_com_llm(mensagem)