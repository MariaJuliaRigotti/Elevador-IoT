# Segurança do gateway: autenticação por token (401), autorização por domínio (403) e limite de pedidos (429)

import json
import os
import time
from collections import deque
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Configuracoes
ARQUIVO_USUARIOS = os.getenv("USUARIOS_ARQUIVO", "usuarios.json")   # Arquivo com os usuários, tokens e permissões
JANELA_LIMITE = 60   # O limite de pedidos vale para os últimos 60 segundos

# Lê o arquivo de usuários uma vez, quando o gateway liga
with open(ARQUIVO_USUARIOS, encoding="utf-8") as arquivo:
    USUARIOS = json.load(arquivo)

# Lê o cabeçalho "Authorization: Bearer <token>"; a falta do token é tratada em autenticar()
leitor_token = HTTPBearer(auto_error=False)

# Horários dos últimos pedidos de cada usuário, usados no limite de pedidos
pedidos_recentes = {}

# Registra no log cada pedido negado: o motivo, o usuário e o que ele tentou
def registrar_negado(motivo: str, usuario: str = "-", detalhe: str = ""):

    print(f"[seguranca] NEGADO {motivo} usuario={usuario} {detalhe}", flush=True)

# Confere se o token existe e retorna os dados do usuário
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

# Confere se o usuário pode usar o domínio pedido
def autorizar(usuario: dict, dominio: str):

    if dominio not in usuario["dominios"]:
        registrar_negado("403 dominio nao permitido", usuario["usuario"], f"dominio={dominio}")
        raise HTTPException(
            status_code=403,
            detail=f"O usuário '{usuario['usuario']}' não tem permissão para o domínio '{dominio}'.",
        )

# Confere se o usuário é administrador (ex.: para zerar as métricas)
def exigir_admin(usuario: dict):

    if not usuario.get("admin"):
        registrar_negado("403 nao e admin", usuario["usuario"])
        raise HTTPException(status_code=403, detail="Operação permitida só para administradores.")

# Limita os pedidos de cada usuário, contando os que ele fez nos últimos 60 segundos
def limitar_taxa(usuario: dict):

    nome = usuario["usuario"]
    limite = usuario["limite_por_minuto"]
    agora = time.monotonic()

    if nome not in pedidos_recentes:
        pedidos_recentes[nome] = deque()
    horarios = pedidos_recentes[nome]

    # Remove os pedidos que já passaram de 60 segundos
    while horarios and agora - horarios[0] > JANELA_LIMITE:
        horarios.popleft()

    if len(horarios) >= limite:
        # Quantos segundos faltam para o pedido mais antigo sair da conta
        espera = int(JANELA_LIMITE - (agora - horarios[0])) + 1
        registrar_negado("429 limite excedido", nome, f"limite={limite}/min")
        raise HTTPException(
            status_code=429,
            detail=f"Limite de {limite} pedidos por minuto excedido. Tente de novo em {espera} s.",
            headers={"Retry-After": str(espera)},   # Diz ao cliente quantos segundos esperar
        )

    horarios.append(agora)

# Confere a permissão e depois o limite de pedidos
def verificar_acesso(usuario: dict, dominio: str):

    autorizar(usuario, dominio)
    limitar_taxa(usuario)