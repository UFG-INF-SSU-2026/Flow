"""
Efeitos das decisoes do nucleo (`analise/nucleo.py`) do lado da ANALISE: arquivos e tabelas que so ela escreve
(`alertas`, `confirmacoes`, `episodios`, `comandos_push`, `estado_dispositivo`).

O nucleo decide sem I/O; aqui esta o I/O. Cada efeito tem a sua protecao: a falha de um nao impede os demais e nunca
levanta (o dado ja foi aceito pela borda). Na S4 o laco da analise chama `aplicar` sem `entregar`: a entrega do push
passa a ser da borda, que le `comandos_push` do banco.

Ordem de `aplicar` (a mesma de sempre): registros de confirmacao, alertas, estado e, por ultimo, cada comando de push
(episodio + comando na MESMA transacao, DS5/DS6) e, so entao, a entrega.
"""
import json
import os
import re
from datetime import datetime

from comum import const as const_comum
from flow_db import repositorios

# (user_id, device_id) -> ultima "assinatura" do estado gravada no banco: so grava quando algo relevante mudou.
_estado_gravado: dict[tuple, tuple] = {}


def nome_seguro(valor: str) -> str:
    """Valor vindo do cliente (device_id) para compor nome de arquivo: qualquer caractere fora de [A-Za-z0-9_.-]
    vira '_' (impede '/' e '..' de sair da pasta de dados) e o resultado e limitado a const_comum.MAX_DEVICE_ID_LEN."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", valor)[:const_comum.MAX_DEVICE_ID_LEN]


def gravar_confirmacao_banco(user_id: int, tipo: str, linha: dict) -> None:
    """Registro do ciclo de confirmacao (episodio.*, push.ack, confirmacao.resposta) -> tabela `confirmacoes`."""
    instante = datetime.fromisoformat(linha["timestamp"])
    repositorios.tentar_analise("confirmacao", lambda a: a.gravar_confirmacao({
        "instante": instante, "user_id": user_id, "tipo": tipo, "dados": linha,
        "episodio_id": linha.get("episodeId"), "message_id": linha.get("messageId"),
        "schema_version": const_comum.SCHEMA_VERSION_MENSAGEM}))


def gravar_alerta_banco(user_id: int, device_id: str, tipo: str, alerta: dict, schema_version: int,
                        instante: datetime) -> None:
    repositorios.tentar_analise("alerta", lambda a: a.gravar_alerta({
        "instante": instante, "user_id": user_id, "device_id": device_id, "tipo": tipo,
        "mensagem": alerta.get("mensagem"), "dados": alerta, "schema_version": schema_version}))


def gravar_comando_banco(cmd) -> None:
    """Episodio (aguardando confirmacao ou encerrado) e o comando de push, na MESMA transacao (DS5/DS6).
    Se o episodio nao chegou ao banco (queda na abertura), a linha de encerramento o cria com tipo DESCONHECIDO."""
    def grava(a):
        with a.transacao() as tx:
            tx.gravar_episodio(cmd.episodio.como_linha())
            tx.gravar_comando(cmd.como_linha())
    repositorios.tentar_analise("comando", grava)


def gravar_estado_banco(resumo) -> None:
    """Estado persistente da regra de imobilidade -> estado_dispositivo. So grava quando algo relevante mudou."""
    try:
        chave = (resumo.user_id, resumo.device_id)
        if _estado_gravado.get(chave) == resumo.assinatura:
            return
        if repositorios.tentar_analise("estado", lambda a: a.gravar_estado_dispositivo(resumo.como_linha())):
            _estado_gravado[chave] = resumo.assinatura
    except Exception as e:  # noqa: BLE001
        print(f"  [db] falha ao preparar estado_dispositivo user={resumo.user_id}: {type(e).__name__}: {str(e)[:200]}")


def aplicar_alerta(alerta) -> None:
    """Arquivo alerta_*.json (enquanto const_comum.GRAVAR_ARQUIVOS), tabela `alertas` e log."""
    prefixo = "alerta" if alerta.tipo == "luz_inadequada" else "alerta_imobilidade"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")   # so o nome do arquivo (dupla escrita de transicao)
    filename = f"{prefixo}_{nome_seguro(alerta.device_id)}_{timestamp}.json"
    if const_comum.GRAVAR_ARQUIVOS:
        with open(os.path.join(const_comum.DATA_DIR, filename), "w", encoding="utf-8") as f:
            json.dump(alerta.dados, f, ensure_ascii=False, indent=2)
    gravar_alerta_banco(alerta.user_id, alerta.device_id, alerta.tipo, alerta.dados, alerta.schema_version,
                        alerta.instante)
    print(f"  >>> ALERTA gerado: {filename} -> {alerta.dados['mensagem']}")


def _isolar(nome: str, fn) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        print(f"  [{nome}] falha ao aplicar a decisao (ignorada): {type(e).__name__}: {str(e)[:200]}")


def aplicar(decisoes, entregar=None) -> None:
    """Efeitos das decisoes. `entregar(cmd)`, se dado, e chamado depois de gravar cada comando (caminho direto);
    sem ele o comando fica so no banco, para a borda entregar."""
    for c in decisoes.confirmacoes:
        _isolar("confirmacao", c.aplicar)
    for a in decisoes.alertas:
        _isolar("alerta", lambda a=a: aplicar_alerta(a))
    for r in decisoes.estados:
        _isolar("estado", lambda r=r: gravar_estado_banco(r))
    for cmd in decisoes.comandos:
        def comando(cmd=cmd):
            gravar_comando_banco(cmd)
            if entregar is not None:
                entregar(cmd)
        _isolar(f"comando episodeId={cmd.episodio.episodio_id} user={cmd.user_id}", comando)
