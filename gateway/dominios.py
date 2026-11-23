# Domínios atendidos pelo gateway: cada um tem seu nó, modelo, prompt e parâmetros

import os
from modelos import ANDAR_MAX

# Configuracoes
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")   # Nó Ollama padrão
MODELO_OLLAMA = os.getenv("MODELO_OLLAMA", "qwen2.5:3b")   # Modelo padrão, usado se o domínio não definir outro
# Modelos do roteamento semântico: pedidos simples vão para o pequeno e difíceis para o grande
MODELO_PEQUENO = os.getenv("MODELO_PEQUENO", "qwen2.5:1.5b")
MODELO_GRANDE = os.getenv("MODELO_GRANDE", "qwen2.5:3b")
MODELO_CLASSIFICADOR = os.getenv("MODELO_CLASSIFICADOR", MODELO_PEQUENO)   # Modelo que decide a rota
MODELO_COMANDO = os.getenv("MODELO_COMANDO", "qwen2.5:1.5b")   # A saída do comando é um JSON curto, então um modelo menor basta
MODELO_CONSULTA = os.getenv("MODELO_CONSULTA", MODELO_OLLAMA)   # A consulta responde em texto

# Prompt do comando: transforma a frase em JSON para o elevador
PROMPT_COMANDO = f"""Você traduz comandos em português para controlar um elevador.
O prédio tem os andares 0 a {ANDAR_MAX}.
- "térreo", "entrada", "portaria" = andar 0
- "último andar", "topo", "cobertura", "lá em cima" = andar {ANDAR_MAX}
- "sobe para o X", "desce para o X" ou "vai para o X" significam sempre o andar X,
  mesmo que o elevador já esteja nele. Não verifique se o movimento é possível:
  o próprio elevador decide isso.
- Só é relativo quando a frase diz quantos andares mover ("sobe um andar",
  "desce dois andares"): aí calcule a partir do andar atual informado na mensagem.
- O texto vem de reconhecimento de voz e pode ter erros (ex.: "térre" = "térreo").

Responda APENAS com um objeto JSON, sem texto antes ou depois, sem markdown:
{{"acao": "mover", "andar": <inteiro>}}

Se o comando não for sobre o elevador ou pedir um andar fora de 0 a {ANDAR_MAX}, responda:
{{"erro": "<explicação curta>"}}"""

# Prompt da consulta: responde perguntas usando o estado do elevador
PROMPT_CONSULTA = f"""Você é o assistente de um prédio com elevador (andares 0 a {ANDAR_MAX}, sendo 0 o térreo).
Responda em português, em no máximo duas frases, usando APENAS o estado do elevador
informado na mensagem. Se a informação não estiver no estado, diga que não sabe."""

DOMINIOS = {
    # Transforma a frase em comando para o elevador
    "comando": {
        "no": os.getenv("NO_COMANDO", OLLAMA_URL),   # Endereço do nó Ollama
        "modelo": MODELO_COMANDO,
        "prompt": PROMPT_COMANDO,
        "temperatura": 0,   # Sempre a mesma resposta para a mesma frase
        "max_simultaneos": 1,   # Vagas deste domínio dentro do nó
        "tipo": "comando",   # A saída vira JSON e vai para o MQTT
    },
    # Responde perguntas sobre o elevador usando a telemetria
    "consulta": {
        "no": os.getenv("NO_CONSULTA", OLLAMA_URL),   # Endereço do nó Ollama
        "modelo": MODELO_CONSULTA,
        "prompt": PROMPT_CONSULTA,
        "temperatura": 0.3,   # Um pouco de variação, mas sem inventar
        "max_simultaneos": 2,   # Vagas deste domínio dentro do nó
        "tipo": "chat",   # A saída é texto, enviado ao usuário
    },
}

# Retorna a configuração do domínio pedido, ou gera erro se ele não existir
def obter_dominio(nome: str) -> dict:

    if nome not in DOMINIOS:
        raise KeyError(f"domínio desconhecido: {nome}")
    return DOMINIOS[nome]


# Escolhe o modelo pela complexidade do pedido: simples vai para o pequeno, difícil para o grande
def modelo_por_complexidade(complexidade: str) -> str:
    return MODELO_PEQUENO if complexidade == "simples" else MODELO_GRANDE