"""
Tradução de linguagem natural -> comando estruturado.

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

from pydantic import TypeAdapter, ValidationError

from modelos import ANDAR_MAX, LUZES, Comando

_validador = TypeAdapter(Comando)


class ComandoNaoEntendido(Exception):
    pass


def _sem_acento(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def validar(dados: dict) -> Comando:
    try:
        return _validador.validate_python(dados)
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

    def traduzir(self, texto: str) -> Comando:
        t = _sem_acento(texto)

        # --- Luz ---
        if re.search(r"\b(luz|luzes|lampada|iluminacao)\b", t):
            if re.search(r"\b(desliga\w*|apaga\w*)\b", t):
                acao = "desligar"
            elif re.search(r"\b(liga\w*|acende\w*|acenda)\b", t):
                acao = "ligar"
            else:
                raise ComandoNaoEntendido("não entendi se é para ligar ou desligar a luz")
            local = next((l for l in LUZES if re.search(rf"\b{l}\b", t)), None)
            if local is None:
                raise ComandoNaoEntendido(f"qual luz? opções: {', '.join(LUZES)}")
            return validar({"dispositivo": "luz", "acao": acao, "local": local})

        # --- Elevador ---
        if re.search(r"\b(elevador|andar|sobe|subir|desce|descer|terreo)\b", t):
            m = re.search(r"\b(\d+)", t)
            if m:
                andar = int(m.group(1))
            else:
                # "quarto andar" é andar, "luz do quarto" já foi tratado acima
                palavra = next((p for p in _ORDINAIS if re.search(rf"\b{p}\b", t)), None)
                if palavra is None:
                    raise ComandoNaoEntendido("para qual andar?")
                andar = _ORDINAIS[palavra]
            return validar({"dispositivo": "elevador", "acao": "mover", "andar": andar})

        raise ComandoNaoEntendido("não reconheci nenhum dispositivo no comando")


# --------------------------------------------------------------------------
# Estratégia 2: LLM
# --------------------------------------------------------------------------

PROMPT_SISTEMA = f"""Você traduz comandos de automação residencial em português para JSON.

Dispositivos disponíveis:
- elevador: andares de 0 (térreo) a {ANDAR_MAX}
- luz: locais {", ".join(LUZES)}

Responda APENAS com um objeto JSON, sem texto antes ou depois, sem markdown.
Formatos válidos:
{{"dispositivo": "elevador", "acao": "mover", "andar": <inteiro>}}
{{"dispositivo": "luz", "acao": "ligar" | "desligar", "local": "<local>"}}

Se o comando for ambíguo, impossível ou não for sobre esses dispositivos, responda:
{{"erro": "<explicação curta>"}}"""


class TradutorLLM:
    nome = "llm"

    def __init__(self):
        from anthropic import Anthropic  # import aqui: só exige a lib se usar LLM

        self.cliente = Anthropic()  # lê ANTHROPIC_API_KEY do ambiente
        self.modelo = os.getenv("MODELO_LLM", "claude-haiku-5-5")

    def traduzir(self, texto: str) -> Comando:
        resp = self.cliente.messages.create(
            model=self.modelo,
            max_tokens=150,
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

    def traduzir(self, texto: str) -> tuple[Comando, str]:
        if self.llm:
            try:
                return self.llm.traduzir(texto), "llm"
            except ComandoNaoEntendido:
                raise  # o LLM entendeu que o comando é inválido: não insiste
            except Exception as e:  # erro de rede, limite de taxa, chave inválida...
                print(f"[tradutor] LLM falhou ({type(e).__name__}: {e}), usando regras", flush=True)
        return self.regras.traduzir(texto), "regras"
