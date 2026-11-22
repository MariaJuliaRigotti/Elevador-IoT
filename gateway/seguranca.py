# Seguranca do gateway (trilha de Seguranca do Projeto 1)
#   - Autenticacao por token: so quem envia um token valido usa o gateway (senao, erro 401)
#   - Autorizacao por dominio: cada usuario so usa os dominios permitidos (senao, erro 403)
#   - Rate limiting por usuario: limite de pedidos por minuto (senao, erro 429)
# O token vai no cabecalho HTTP:  Authorization: Bearer <token>
# Os usuarios, tokens e permissoes ficam no arquivo usuarios.json

import json
import os
import time
from collections import deque
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Configuracoes
ARQUIVO_USUARIOS = os.getenv("USUARIOS_ARQUIVO", "usuarios.json")   # Arquivo com os tokens e permissoes
JANELA_LIMITE = 60   # O limite de pedidos vale para os ultimos 60 segundos

# Le o arquivo de usuarios uma vez, quando o gateway liga
with open(ARQUIVO_USUARIOS, encoding="utf-8") as arquivo:
    USUARIOS = json.load(arquivo)

# Le o cabecalho "Authorization: Bearer <token>" (auto_error=False: nos mesmos tratamos a falta do token)
leitor_token = HTTPBearer(auto_error=False)

# Horarios dos ultimos pedidos de cada usuario, usados no rate limiting
pedidos_recentes = {}

# Registra no log as tentativas negadas (quem, o que tentou e por que foi negado)
def registrar_negado(motivo: str, usuario: str = "-", detalhe: str = ""):

    print(f"[seguranca] NEGADO {motivo} usuario={usuario} {detalhe}", flush=True)

# Autenticacao: confere se o token existe e retorna os dados do usuario
# Usado pelos endpoints com Depends(autenticar)
def autenticar(credenciais: HTTPAuthorizationCredentials | None = Depends(leitor_token)) -> dict:

    if credenciais is None:
        registrar_negado("401 sem token")
        raise HTTPException(
            status_code=401,
            detail="Token ausente. Envie o cabeçalho 'Authorization: Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    usuario = USUARIOS.get(credenciais.credentials)
    if usuario is None:
        registrar_negado("401 token invalido")
        raise HTTPException(
            status_code=401,
            detail="Token inválido.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return usuario

# Autorizacao: confere se o usuario pode usar o dominio pedido
def autorizar(usuario: dict, dominio: str):

    if dominio not in usuario["dominios"]:
        registrar_negado("403 dominio nao permitido", usuario["usuario"], f"dominio={dominio}")
        raise HTTPException(
            status_code=403,
            detail=f"O usuário '{usuario['usuario']}' não tem permissão para o domínio '{dominio}'.",
        )

# Autorizacao para operacoes administrativas (ex.: zerar as metricas)
def exigir_admin(usuario: dict):

    if not usuario.get("admin"):
        registrar_negado("403 nao e admin", usuario["usuario"])
        raise HTTPException(status_code=403, detail="Operação permitida só para administradores.")

# Rate limiting com janela deslizante: conta os pedidos do usuario nos ultimos 60 segundos
def limitar_taxa(usuario: dict):

    nome = usuario["usuario"]
    limite = usuario["limite_por_minuto"]
    agora = time.monotonic()

    if nome not in pedidos_recentes:
        pedidos_recentes[nome] = deque()
    horarios = pedidos_recentes[nome]

    # Remove os pedidos que ja sairam da janela de 60 segundos
    while horarios and agora - horarios[0] > JANELA_LIMITE:
        horarios.popleft()

    if len(horarios) >= limite:
        # Quantos segundos faltam para o pedido mais antigo sair da janela
        espera = int(JANELA_LIMITE - (agora - horarios[0])) + 1
        registrar_negado("429 limite excedido", nome, f"limite={limite}/min")
        raise HTTPException(
            status_code=429,
            detail=f"Limite de {limite} pedidos por minuto excedido. Tente de novo em {espera} s.",
            headers={"Retry-After": str(espera)},   # Cabecalho padrao do HTTP para o erro 429
        )

    horarios.append(agora)

# Verificacao completa usada antes de atender um pedido: autorizacao e depois rate limiting
def verificar_acesso(usuario: dict, dominio: str):

    autorizar(usuario, dominio)
    limitar_taxa(usuario)