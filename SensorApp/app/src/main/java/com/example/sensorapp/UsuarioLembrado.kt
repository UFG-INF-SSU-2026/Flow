package com.example.sensorapp

/*
 * Ultimo usuario que entrou com sucesso, para preencher o campo no dialogo de Configuracoes. A senha nunca e guardada.
 * Sem android.*: roda na JVM.
 */

/** Chave nas preferencias do app. */
const val PREF_ULTIMO_USUARIO = "ultimo_usuario"

/** O usuario que vale guardar: sem espacos nas pontas; vazio, so espacos ou null nao se guardam (devolve null). */
fun usuarioParaGuardar(usuario: String?): String? = usuario?.trim()?.takeIf { it.isNotEmpty() }
