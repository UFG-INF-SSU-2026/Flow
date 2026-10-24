"""
Efeitos das decisoes do nucleo (`analise/nucleo.py`) do lado da ANALISE: tabelas e arquivos que so ela escreve
(`alertas`, `confirmacoes`, `episodios`, `comandos_push`, `estado_dispositivo`).

O nucleo decide sem I/O; aqui esta o I/O. O laco da analise (`analise/laco.py`) chama `aplicar_na_transacao` com a
transacao do ciclo: as decisoes e o avanco do cursor saem na MESMA transacao (DS5), e o que nao e do banco (arquivos de
transicao e log) volta como funcoes para rodar DEPOIS do commit. A entrega do push e da borda, que le `comandos_push`.

Ordem de gravacao (a mesma de sempre): registros de confirmacao, alertas, estado e, por ultimo, cada comando de push
(episodio + comando juntos, DS5/DS6).
"""
import json
import os
import re
from datetime import datetime

from comum import const as const_comum
from analise import regra_queda


def nome_seguro(valor: str) -> str:
    """Valor vindo do cliente (device_id) para compor nome de arquivo: qualquer caractere fora de [A-Za-z0-9_.-]
    vira '_' (impede '/' e '..' de sair da pasta de dados) e o resultado e limitado a const_comum.MAX_DEVICE_ID_LEN."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", valor)[:const_comum.MAX_DEVICE_ID_LEN]


def _linha_confirmacao(user_id: int, tipo: str, linha: dict) -> dict:
    return {"instante": datetime.fromisoformat(linha["timestamp"]), "user_id": user_id, "tipo": tipo, "dados": linha,
            "episodio_id": linha.get("episodeId"), "message_id": linha.get("messageId"),
            "schema_version": const_comum.SCHEMA_VERSION_MENSAGEM}


def _arquivo_alerta(alerta) -> str:
    """Arquivo alerta_*.json (enquanto const_comum.GRAVAR_ARQUIVOS). Devolve o nome do arquivo."""
    prefixo = "alerta" if alerta.tipo == "luz_inadequada" else "alerta_imobilidade"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")   # so o nome do arquivo (dupla escrita de transicao)
    filename = f"{prefixo}_{nome_seguro(alerta.device_id)}_{timestamp}.json"
    if const_comum.GRAVAR_ARQUIVOS:
        os.makedirs(const_comum.DATA_DIR, exist_ok=True)
        with open(os.path.join(const_comum.DATA_DIR, filename), "w", encoding="utf-8") as f:
            json.dump(alerta.dados, f, ensure_ascii=False, indent=2)
    return filename


def aplicar_na_transacao(tx, decisoes, assinaturas_gravadas: dict, assinaturas_novas: dict) -> list:
    """Grava as decisoes POR `tx` (a transacao do ciclo da analise), na ordem do modulo: confirmacoes,
    alertas, estados e, por fim, cada comando (episodio + comando, DS5/DS6). Nao entrega nada (a borda le
    `comandos_push`) e NAO engole excecao: falha do banco desfaz o ciclo inteiro, cursor incluido (DS5).

    Arquivos de transicao (confirmacoes_usuario_*.jsonl, alerta_*.json) e o log nao entram na transacao: voltam como
    lista de funcoes para quem chamou executar DEPOIS do commit (senao um ciclo refeito os duplicaria).

    Estado do dispositivo: so grava quando a assinatura muda. `assinaturas_gravadas` e o que ja esta no banco (so
    leitura); `assinaturas_novas` recebe o que esta sendo gravado nesta tentativa e so deve ser incorporado ao
    anterior DEPOIS do commit (um rollback nao pode marcar um estado como gravado)."""
    depois: list = []
    for c in decisoes.confirmacoes:
        linha = {"timestamp": c.instante.isoformat(), "tipo": c.tipo, **c.dados}
        tx.gravar_confirmacao(_linha_confirmacao(c.user_id, c.tipo, linha))
        depois.append(lambda u=c.user_id, l=linha: regra_queda.gravar_arquivo_confirmacao(u, l))
    for a in decisoes.alertas:
        tx.gravar_alerta(a.como_linha())
        depois.append(lambda a=a: print(f"  >>> ALERTA gerado: {_arquivo_alerta(a)} -> {a.dados['mensagem']}"))
    for r in decisoes.estados:
        chave = (r.user_id, r.device_id)
        if assinaturas_novas.get(chave, assinaturas_gravadas.get(chave)) == r.assinatura:
            continue
        tx.gravar_estado_dispositivo(r.como_linha())
        assinaturas_novas[chave] = r.assinatura
    for cmd in decisoes.comandos:
        tx.gravar_episodio(cmd.episodio.como_linha())
        tx.gravar_comando(cmd.como_linha())
    return depois
