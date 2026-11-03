"""
Tradução de linguagem natural -> comando estruturado do elevador.

Duas estratégias com a mesma interface:
  - TradutorLLM: usa a API da Anthropic (precisa de ANTHROPIC_API_KEY)
  - TradutorRegras: regex, funciona offline e serve de fallback

Em ambos os casos a saída é validada com Pydantic antes de virar MQTT.
O LLM nunca publica nada diretamente: ele só traduz.
"""

import json
import os
import re
import unicodedata

from pydantic import ValidationError

from modelos import ANDAR_MAX, ComandoElevador


class ComandoNaoEntendido(Exception):
    pass


def _sem_acento(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def validar(dados: dict) -> ComandoElevador:
    try:
        return ComandoElevador.model_validate(dados)
    except ValidationError as e:
        raise ComandoNaoEntendido(f"comando fora do contrato: {e.errors()[0]['msg']}") from e


# --------------------------------------------------------------------------
# Estratégia 1: regras (regex)
# --------------------------------------------------------------------------

_ORDINAIS = {
    "terreo": 0, "zero": 0,
    "primeiro": 1, "um": 1,
    "segundo": 2, "dois": 2,
    "terceiro": 3, "tres": 3,
    "quarto": 4, "quatro": 4,
    "quinto": 5, "cinco": 5,
}


class TradutorRegras:
    nome = "regras"

    def traduzir(self, texto: str) -> ComandoElevador:
        t = _sem_acento(texto)

        if not re.search(r"\b(elevador|andar|sobe|subir|suba|desce|descer|desca|terreo|vai|leva|chama)\b", t):
            raise ComandoNaoEntendido("não reconheci um comando para o elevador")

        m = re.search(r"\b(\d+)", t)
        if m:
            andar = int(m.group(1))
        else:
            palavra = next((p for p in _ORDINAIS if re.search(rf"\b{p}\b", t)), None)
            if palavra is None:
                raise ComandoNaoEntendido("para qual andar?")
            andar = _ORDINAIS[palavra]

        return validar({"acao": "mover", "andar": andar})


# --------------------------------------------------------------------------
# Estratégia 2: LLM
# --------------------------------------------------------------------------

PROMPT_SISTEMA = f"""Você traduz comandos em português para controlar um elevador.
O elevador vai do andar 0 (térreo) ao andar {ANDAR_MAX}.

Responda APENAS com um objeto JSON, sem texto antes ou depois, sem markdown:
{{"acao": "mover", "andar": <inteiro>}}

Se o comando for ambíguo, pedir um andar inexistente ou não for sobre o elevador, responda:
{{"erro": "<explicação curta>"}}"""


class TradutorLLM:
    nome = "llm"

    def __init__(self):
        from anthropic import Anthropic  # import aqui: só exige a lib se usar LLM

        self.cliente = Anthropic()  # lê ANTHROPIC_API_KEY do ambiente
        self.modelo = os.getenv("MODELO_LLM", "claude-haiku-5-5")

    def traduzir(self, texto: str) -> ComandoElevador:
        resp = self.cliente.messages.create(
            model=self.modelo,
            max_tokens=100,
            system=PROMPT_SISTEMA,
            messages=[{"role": "user", "content": texto}],
        )
        bruto = resp.content[0].text.strip()
        bruto = re.sub(r"^```(?:json)?|```$", "", bruto).strip()  # caso venha em markdown
        try:
            dados = json.loads(bruto)
        except json.JSONDecodeError as e:
            raise ComandoNaoEntendido(f"LLM não devolveu JSON: {bruto[:80]}") from e
        if "erro" in dados:
            raise ComandoNaoEntendido(dados["erro"])
        return validar(dados)


# --------------------------------------------------------------------------
# Escolha da estratégia
# --------------------------------------------------------------------------

class TradutorComFallback:
    """Tenta o LLM; se falhar por erro de rede/API, usa as regras."""

    def __init__(self):
        self.regras = TradutorRegras()
        modo = os.getenv("TRADUTOR", "auto")  # auto | llm | regras
        self.llm = None
        if modo in ("auto", "llm") and os.getenv("ANTHROPIC_API_KEY"):
            self.llm = TradutorLLM()
        elif modo == "llm":
            raise RuntimeError("TRADUTOR=llm mas ANTHROPIC_API_KEY não está definida")

    def traduzir(self, texto: str) -> tuple[ComandoElevador, str]:
        if self.llm:
            try:
                return self.llm.traduzir(texto), "llm"
            except ComandoNaoEntendido:
                raise  # o LLM entendeu que o comando é inválido: não insiste
            except Exception as e:  # erro de rede, limite de taxa, chave inválida...
                print(f"[tradutor] LLM falhou ({type(e).__name__}: {e}), usando regras", flush=True)
        return self.regras.traduzir(texto), "regras"
