package com.example.sensorapp

/*
 * Login em um passo so: quando o app precisa buscar a CA no servidor antes de entrar. Sem android.*: roda na JVM.
 */

/** "ip:porta" normalizado (sem espacos), a chave com que o app lembra para QUAL servidor a CA guardada foi confirmada. */
fun destinoDoServidor(ip: String, porta: String): String = "${ip.trim()}:${porta.trim()}"

/**
 * Baixar a CA do servidor antes do login?
 *  - [CaProjeto.Origem.NENHUMA]: sim, sempre (sem CA toda conexao e recusada).
 *  - Ha CA (importada ou embutida) e o servidor digitado e outro que o da ultima confirmacao ([destinoConfirmado]):
 *    sim, a CA antiga nao serve a outro servidor.
 *  - Ha CA e nada foi confirmado ainda (instalacao antiga, ou CA embutida): nao; tenta o login direto, e o sucesso
 *    do login passa a ser a confirmacao.
 *  - Ha CA e o servidor e o mesmo: nao.
 * [destinoConfirmado]: o "ip:porta" para o qual a CA guardada ja funcionou (download ou login), ou null.
 */
fun precisaBaixarCa(origem: CaProjeto.Origem, destinoDigitado: String, destinoConfirmado: String?): Boolean = when {
    origem == CaProjeto.Origem.NENHUMA -> true
    destinoConfirmado == null -> false
    else -> destinoConfirmado != destinoDigitado
}

/**
 * O login falhou: vale baixar a CA e repetir o login UMA vez? So se a falha foi de TLS (a CA guardada pode ter ficado
 * velha) e a CA ainda nao foi baixada nesta mesma execucao (senao ja era a CA nova e repetir nao muda nada).
 */
fun repetirLoginComCaNova(falhaDeTls: Boolean, caBaixadaNestaExecucao: Boolean): Boolean =
    falhaDeTls && !caBaixadaNestaExecucao
