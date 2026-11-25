package com.example.sensorapp

/*
 * Textos da tela que dependem de uma regra pequena. Sem android.*: roda na JVM.
 */

/**
 * Status de falha do envio com a idade do ultimo envio que deu certo. [msDesdeUltimoSucesso] e o tempo decorrido desde
 * esse envio, ou null se ainda nao houve sucesso nesta execucao (entao o texto fica como esta, sem idade).
 * Ex.: "Servidor indisponivel (tentando novamente), ultimo envio há 40 s".
 */
fun textoDeFalhaDeEnvio(base: String, msDesdeUltimoSucesso: Long?): String =
    if (msDesdeUltimoSucesso == null) base else "$base, último envio ${ha(msDesdeUltimoSucesso)}"

/** Titulo da tela inicial: nome da versao do app e versao do schema do payload. Nome vazio ou nulo: so o schema. */
fun textoDaVersao(nomeDaVersao: String?, schema: Int): String {
    val nome = nomeDaVersao?.trim().orEmpty()
    return if (nome.isEmpty()) "Sensor App · schema $schema" else "Sensor App v$nome · schema $schema"
}
