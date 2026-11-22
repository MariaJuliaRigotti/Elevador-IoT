# Traducao de linguagem natural para o comando estruturado do elevador
# Estrategias disponiveis:
#   - TradutorRegras: regex, funciona offline e serve de fallback
#   - TradutorLLM: usa a API da Anthropic (precisa de ANTHROPIC_API_KEY)
#   - TradutorOllama: LLM local via Ollama (gratis, sem chave de API)
# Em todos os casos a saida eh validada com Pydantic antes de virar MQTT
# O LLM nunca publica nada diretamente: ele so traduz
# O prompt, o modelo e os parametros dos LLMs ficam no dominios.py

import json
import os
import re
import unicodedata
from pydantic import ValidationError
from modelos import ComandoElevador
from dominios import PROMPT_COMANDO, obter_dominio

# Erro usado quando a frase nao pode ser convertida em um comando valido
class ComandoNaoEntendido(Exception):
    pass

# Deixa o texto em minusculas e sem acentos (ex.: "Térreo" -> "terreo")
def sem_acento(texto: str) -> str:

    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))

# Confere se o comando segue o contrato (acao "mover" e andar de 0 a ANDAR_MAX)
def validar(dados: dict) -> ComandoElevador:

    try:
        return ComandoElevador.model_validate(dados)
    except ValidationError as e:
        raise ComandoNaoEntendido(f"comando fora do contrato: {e.errors()[0]['msg']}") from e

# Monta a mensagem enviada ao LLM, incluindo o andar atual quando ele for conhecido
def montar_mensagem(texto: str, andar_atual: int | None) -> str:

    if andar_atual is None:
        return texto
    return f"Andar atual: {andar_atual}\nComando: {texto}"

# Converte a resposta em texto do LLM em um comando validado
def interpretar_resposta(bruto: str) -> ComandoElevador:

    bruto = re.sub(r"^```(?:json)?|```$", "", bruto.strip()).strip()   # Remove markdown, caso venha

    try:
        dados = json.loads(bruto)
    except json.JSONDecodeError as e:
        raise ComandoNaoEntendido(f"LLM não devolveu JSON: {bruto[:80]}") from e

    if not isinstance(dados, dict):
        raise ComandoNaoEntendido(f"LLM não devolveu um objeto JSON: {bruto[:80]}")
    if "erro" in dados:
        raise ComandoNaoEntendido(dados["erro"])

    return validar(dados)

# Estrategia 1: regras (regex)

# Palavras que representam andares
ORDINAIS = {
    "terreo": 0, "zero": 0,
    "primeiro": 1, "um": 1,
    "segundo": 2, "dois": 2,
    "terceiro": 3, "tres": 3,
}

class TradutorRegras:

    nome = "regras"

    # Procura palavras-chave e o numero do andar na frase
    # O andar atual e ignorado: as regras nao entendem comandos relativos
    def traduzir(self, texto: str, andar_atual: int | None = None) -> ComandoElevador:

        t = sem_acento(texto)

        # Verifica se a frase fala do elevador
        if not re.search(r"\b(elevador|andar|sobe|subir|suba|desce|descer|desca|terreo|vai|leva|chama)\b", t):
            raise ComandoNaoEntendido("não reconheci um comando para o elevador")

        # Procura um numero na frase (ex.: "andar 2")
        m = re.search(r"\b(\d+)", t)
        if m:
            andar = int(m.group(1))
        else:
            # Se nao houver numero, procura uma palavra da lista ORDINAIS
            palavra = None
            for p in ORDINAIS:
                if re.search(rf"\b{p}\b", t):
                    palavra = p
                    break
            if palavra is None:
                raise ComandoNaoEntendido("para qual andar?")
            andar = ORDINAIS[palavra]

        return validar({"acao": "mover", "andar": andar})

# Estrategia 2: LLM

# LLM na nuvem, pela API da Anthropic
class TradutorLLM:

    nome = "llm"

    def __init__(self):

        from anthropic import Anthropic   # Importado aqui: so exige a biblioteca se usar esse tradutor

        self.cliente = Anthropic()   # Le ANTHROPIC_API_KEY do ambiente
        self.modelo = os.getenv("MODELO_LLM", "claude-haiku-5-5")

    # Envia a frase para a API e interpreta a resposta (o parametro 'modelo' so vale para o Ollama)
    def traduzir(self, texto: str, andar_atual: int | None = None, modelo: str | None = None) -> ComandoElevador:

        resp = self.cliente.messages.create(
            model=self.modelo,
            max_tokens=100,
            system=PROMPT_COMANDO,
            messages=[{"role": "user", "content": montar_mensagem(texto, andar_atual)}],
        )

        return interpretar_resposta(resp.content[0].text)

# LLM local via Ollama
class TradutorOllama:

    nome = "ollama"

    def __init__(self):

        # Le a configuracao do dominio comando (no, modelo, prompt e temperatura)
        config = obter_dominio("comando")
        self.url = config["no"]   # Endereco do no Ollama
        self.modelo = config["modelo"]   # Modelo usado na traducao
        self.prompt = config["prompt"]   # Instrucoes enviadas antes de cada frase
        self.temperatura = config["temperatura"]   # 0 = respostas sempre iguais para a mesma frase

    # Envia a frase para o Ollama e interpreta a resposta
    # 'modelo' permite ao roteador semantico escolher o modelo de cada pedido (padrao: o do dominio)
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
                "format": "json",   # Forca a saida em JSON
                "options": {"temperature": self.temperatura},
            },
            timeout=60,
        )
        resp.raise_for_status()

        return interpretar_resposta(resp.json()["message"]["content"])

# Escolha da estrategia

# Tenta o LLM; se ele falhar por erro de rede/API, usa as regras
class TradutorComFallback:

    def __init__(self):

        self.regras = TradutorRegras()
        modo = os.getenv("TRADUTOR", "auto")   # auto | llm | ollama | regras
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
                raise   # O LLM entendeu que o comando e invalido: nao insiste
            except Exception as e:   # Erro de rede, limite de taxa, chave invalida...
                print(f"[tradutor] LLM falhou ({type(e).__name__}: {e}), usando regras", flush=True)

        return self.regras.traduzir(texto), "regras"