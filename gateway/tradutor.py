# Tradutor: transforma a frase do usuário no comando do elevador, sempre validado antes de ir para o MQTT

import json
import os
import re
import unicodedata
from pydantic import ValidationError
from modelos import ComandoElevador
from dominios import PROMPT_COMANDO, obter_dominio

# Erro usado quando a frase não vira um comando válido
class ComandoNaoEntendido(Exception):
    pass

# Deixa o texto em minúsculas e sem acentos (ex.: "Térreo" vira "terreo")
def sem_acento(texto: str) -> str:

    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))

# Confere se o comando é válido (ação "mover" e andar de 0 a ANDAR_MAX)
def validar(dados: dict) -> ComandoElevador:

    try:
        return ComandoElevador.model_validate(dados)
    except ValidationError as e:
        raise ComandoNaoEntendido(f"comando fora do contrato: {e.errors()[0]['msg']}") from e

# Monta a mensagem para o LLM, com o andar atual quando ele é conhecido
def montar_mensagem(texto: str, andar_atual: int | None) -> str:

    if andar_atual is None:
        return texto
    return f"Andar atual: {andar_atual}\nComando: {texto}"

# Transforma a resposta do LLM em um comando validado
def interpretar_resposta(bruto: str) -> ComandoElevador:

    bruto = re.sub(r"^```(?:json)?|```$", "", bruto.strip()).strip()   # Tira a formatação markdown, se vier

    try:
        dados = json.loads(bruto)
    except json.JSONDecodeError as e:
        raise ComandoNaoEntendido(f"LLM não devolveu JSON: {bruto[:80]}") from e

    if not isinstance(dados, dict):
        raise ComandoNaoEntendido(f"LLM não devolveu um objeto JSON: {bruto[:80]}")
    if "erro" in dados:
        raise ComandoNaoEntendido(dados["erro"])

    return validar(dados)

# Tradutor por regras: funciona sem LLM e é usado quando ele falha

# Palavras que representam andares
ORDINAIS = {
    "terreo": 0, "zero": 0,
    "primeiro": 1, "um": 1,
    "segundo": 2, "dois": 2,
    "terceiro": 3, "tres": 3,
}

# Andar relativo ao atual ("sobe um andar", "andar de cima"): as regras não fazem essa conta
ANDAR_RELATIVO = re.compile(
    r"\b(um|uma|dois|duas|tres|quatro)\s+andar(es)?\b|\b\d+\s+andares\b"
    r"|\b(de cima|de baixo|acima|abaixo|proximo|anterior|seguinte)\b"
)
# Verbos de movimento, para achar frases com mais de uma ação ("vai pro 2 e volta pro térreo")
VERBOS_MOVIMENTO = re.compile(r"\b(sobe|subir|suba|desce|descer|desca|vai|ir|leva|levar|volta|voltar)\b")

class TradutorRegras:

    nome = "regras"

    # Procura o andar na frase; o andar atual não é usado, porque as regras não fazem conta
    def traduzir(self, texto: str, andar_atual: int | None = None) -> ComandoElevador:

        t = sem_acento(texto)

        # Verifica se a frase fala do elevador
        if not re.search(r"\b(elevador|andar|sobe|subir|suba|desce|descer|desca|terreo|vai|leva|chama)\b", t):
            raise ComandoNaoEntendido("não reconheci um comando para o elevador")

        # Recusa o que as regras não conseguem traduzir com segurança, em vez de arriscar o andar errado
        if ANDAR_RELATIVO.search(t):
            raise ComandoNaoEntendido("comando relativo ao andar atual; diga o andar de destino")
        if len(VERBOS_MOVIMENTO.findall(t)) >= 2 or re.search(r"\bdepois\b", t):
            raise ComandoNaoEntendido("mais de uma ação na mesma frase; envie um comando por vez")

        # Procura um número na frase (ex.: "andar 2")
        m = re.search(r"\b(\d+)", t)
        if m:
            andar = int(m.group(1))
        else:
            # Se não houver número, procura uma palavra da lista ORDINAIS
            palavra = None
            for p in ORDINAIS:
                if re.search(rf"\b{p}\b", t):
                    palavra = p
                    break
            if palavra is None:
                raise ComandoNaoEntendido("para qual andar?")
            andar = ORDINAIS[palavra]

        return validar({"acao": "mover", "andar": andar})

# Tradutor pela API da Anthropic (só é usado se ANTHROPIC_API_KEY estiver definida)
class TradutorLLM:

    nome = "llm"

    def __init__(self):

        from anthropic import Anthropic   # Importado aqui para só exigir a biblioteca quando esse tradutor for usado

        self.cliente = Anthropic()   # Lê a chave ANTHROPIC_API_KEY do ambiente
        self.modelo = os.getenv("MODELO_LLM", "claude-haiku-5-5")

    # Envia a frase para a API e interpreta a resposta ('modelo' só vale para o Ollama)
    def traduzir(self, texto: str, andar_atual: int | None = None, modelo: str | None = None) -> ComandoElevador:

        resp = self.cliente.messages.create(
            model=self.modelo,
            max_tokens=100,
            system=PROMPT_COMANDO,
            messages=[{"role": "user", "content": montar_mensagem(texto, andar_atual)}],
        )

        return interpretar_resposta(resp.content[0].text)

# Tradutor com o LLM local, pelo Ollama
class TradutorOllama:

    nome = "ollama"

    def __init__(self):

        # Usa a configuração do domínio comando (nó, modelo, prompt e temperatura)
        config = obter_dominio("comando")
        self.url = config["no"]   # Endereço do nó Ollama
        self.modelo = config["modelo"]   # Modelo usado na tradução
        self.prompt = config["prompt"]   # Instruções enviadas antes de cada frase
        self.temperatura = config["temperatura"]   # Com 0, a mesma frase sempre gera a mesma resposta

    # Envia a frase para o Ollama e interpreta a resposta ('modelo' é a escolha do roteador, se houver)
    def traduzir(self, texto: str, andar_atual: int | None = None, modelo: str | None = None) -> ComandoElevador:

        import httpx

        resp = httpx.post(
            f"{self.url}/api/chat",
            json={
                "model": modelo or self.modelo,
                "messages": [
                    {"role": "system", "content": self.prompt},
                    {"role": "user", "content": montar_mensagem(texto, andar_atual)},
                ],
                "stream": False,
                "format": "json",   # Obriga o modelo a responder em JSON
                "options": {"temperature": self.temperatura},
            },
            timeout=60,
        )
        resp.raise_for_status()

        return interpretar_resposta(resp.json()["message"]["content"])

# Escolhe o tradutor pela variável TRADUTOR: tenta o LLM e, se ele falhar, usa as regras
class TradutorComFallback:

    def __init__(self):

        self.regras = TradutorRegras()
        modo = os.getenv("TRADUTOR", "auto")   # auto, llm, ollama ou regras
        self.llm = None

        if modo == "ollama":
            self.llm = TradutorOllama()
        elif modo in ("auto", "llm") and os.getenv("ANTHROPIC_API_KEY"):
            self.llm = TradutorLLM()
        elif modo == "llm":
            raise RuntimeError("TRADUTOR=llm mas ANTHROPIC_API_KEY não está definida")

    # Retorna o comando traduzido e o nome do tradutor que foi usado
    def traduzir(self, texto: str, andar_atual: int | None = None,
                 modelo: str | None = None) -> tuple[ComandoElevador, str]:

        if self.llm:
            try:
                return self.llm.traduzir(texto, andar_atual, modelo), self.llm.nome
            except ComandoNaoEntendido:
                raise   # O LLM entendeu e achou o comando inválido: não adianta tentar as regras
            except Exception as e:   # Erro de rede, limite da API ou chave inválida
                print(f"[tradutor] LLM falhou ({type(e).__name__}: {e}), usando regras", flush=True)

        return self.regras.traduzir(texto), "regras"