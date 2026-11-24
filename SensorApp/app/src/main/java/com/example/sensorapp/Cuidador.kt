package com.example.sensorapp

import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

/** Ultima comunicacao de um dispositivo. [instante] e null se a data veio em formato ilegivel. */
data class Comunicacao(val deviceId: String, val fonte: String, val instante: Long?)

data class AlertaCuidador(val instante: Long?, val deviceId: String, val tipo: String, val mensagem: String, val episodioId: String?)

data class ConfirmacaoCuidador(
    val instante: Long?,
    val tipo: String,
    val episodioId: String?,
    val resultado: String?,
    val estado: String?,
    val motivo: String?
)

data class EpisodioCuidador(
    val episodioId: String,
    val deviceId: String,
    val tipo: String,
    val estado: String,
    val abertoEm: Long?,
    val atualizadoEm: Long?,
    val encerradoEm: Long?,
    val resultado: String?
)

/** Resposta de GET /cuidador/<user_id>/estado. [geradoEm] e o relogio do servidor, em milissegundos. */
data class Estado(
    val userId: Int,
    val geradoEm: Long,
    val comunicacoes: List<Comunicacao>,
    val episodioAberto: EpisodioCuidador?,
    val alertas: List<AlertaCuidador>
)

/** Resposta de GET /cuidador/<user_id>/historico, mais recentes primeiro. */
data class Historico(
    val userId: Int,
    val geradoEm: Long,
    val alertas: List<AlertaCuidador>,
    val confirmacoes: List<ConfirmacaoCuidador>,
    val episodios: List<EpisodioCuidador>
)

/**
 * Le a resposta (ja decifrada) do estado. Devolve null, sem lancar excecao, se faltar campo obrigatorio ou se algum tipo
 * estiver errado: a tela ignora a resposta e mantem o ultimo dado.
 */
fun lerEstado(json: String): Estado? {
    val raiz = objeto(json) ?: return null
    val (userId, geradoEm) = cabecalho(raiz) ?: return null
    val comunicacoes = lista(raiz.opt("comunicacoes")) { comunicacao(it) } ?: return null
    if (!raiz.has("episodioAberto")) return null
    val bruto = raiz.opt("episodioAberto")
    val episodioAberto = when (bruto) {
        JSONObject.NULL -> null
        is JSONObject -> episodio(bruto) ?: return null
        else -> return null
    }
    val alertas = lista(raiz.opt("alertas")) { alerta(it) } ?: return null
    return Estado(userId, geradoEm, comunicacoes, episodioAberto, alertas)
}

/** Como [lerEstado], para a resposta do historico. */
fun lerHistorico(json: String): Historico? {
    val raiz = objeto(json) ?: return null
    val (userId, geradoEm) = cabecalho(raiz) ?: return null
    val alertas = lista(raiz.opt("alertas")) { alerta(it) } ?: return null
    val confirmacoes = lista(raiz.opt("confirmacoes")) { confirmacao(it) } ?: return null
    val episodios = lista(raiz.opt("episodios")) { episodio(it) } ?: return null
    return Historico(userId, geradoEm, alertas, confirmacoes, episodios)
}

/**
 * Linhas de texto do estado. [decorridoMs] e o tempo local desde a chegada da resposta: a idade da ultima comunicacao e
 * (geradoEm - instante), os dois do servidor, mais esse tempo; o relogio do celular nao entra.
 */
fun linhasDoEstado(estado: Estado, decorridoMs: Long, fuso: TimeZone = TimeZone.getDefault()): List<String> {
    val linhas = mutableListOf<String>()
    if (estado.comunicacoes.isEmpty()) {
        linhas.add("Sem comunicação registrada.")
    }
    for (c in estado.comunicacoes) {
        val quando = c.instante?.let { "última comunicação " + ha(estado.geradoEm - it + decorridoMs) } ?: "sem data da última comunicação"
        linhas.add("${c.deviceId} (${c.fonte}): $quando")
    }
    val aberto = estado.episodioAberto
    if (aberto == null) {
        linhas.add("Nenhum episódio aberto.")
    } else {
        val desde = aberto.abertoEm?.let { ", desde " + dataHora(it, fuso) } ?: ""
        linhas.add("Episódio aberto: ${rotulo(aberto.tipo)}, ${estadoLegivel(aberto.estado)}$desde")
    }
    if (estado.alertas.isEmpty()) {
        linhas.add("Nenhum alerta recente.")
    } else {
        linhas.add("Últimos alertas:")
        for (a in estado.alertas) linhas.add(comData(a.instante, descricao(a), fuso))
    }
    return linhas
}

/**
 * Alertas, confirmacoes e episodios numa lista so, do mais recente para o mais antigo. Item sem data legivel vai para o
 * fim e mostra so a descricao.
 */
fun linhasDoHistorico(historico: Historico, fuso: TimeZone = TimeZone.getDefault()): List<String> {
    val itens = mutableListOf<Pair<Long?, String>>()
    for (a in historico.alertas) itens.add(a.instante to descricao(a))
    for (c in historico.confirmacoes) itens.add(c.instante to descricao(c))
    for (e in historico.episodios) itens.add(e.abertoEm to descricao(e))
    // sortedByDescending e estavel; null conta como o mais antigo.
    return itens.sortedByDescending { it.first ?: Long.MIN_VALUE }.map { comData(it.first, it.second, fuso) }
}

/** Idade em texto curto: "agora", "há 40 s", "há 3 min", "há 2 h", "há 5 dias". Valor negativo conta como zero. */
fun ha(ms: Long): String {
    val s = maxOf(ms, 0L) / 1000
    return when {
        s < 5 -> "agora"
        s < 60 -> "há $s s"
        s < 3600 -> "há ${s / 60} min"
        s < 86400 -> "há ${s / 3600} h"
        else -> "há ${s / 86400} dias"
    }
}

// ---------------------------------------------------------------- descricoes

private fun descricao(a: AlertaCuidador) = "Alerta: " + semSublinhado(a.tipo.removePrefix("alerta_"))

private fun descricao(c: ConfirmacaoCuidador): String {
    val texto = when (c.estado) {
        "ESTOU_BEM" -> "Estou bem"
        "SEM_RESPOSTA" -> "Sem resposta"
        else -> c.resultado?.let { semSublinhado(it.lowercase(Locale.ROOT)) } ?: semSublinhado(c.tipo)
    }
    return "Confirmação: $texto"
}

private fun descricao(e: EpisodioCuidador): String {
    val situacao = e.resultado?.let { it.lowercase(Locale.ROOT) } ?: e.estado
    return "${rotulo(e.tipo)}: ${estadoLegivel(situacao)}"
}

private fun rotulo(tipo: String): String = semSublinhado(tipo).replaceFirstChar { it.titlecase(Locale.ROOT) }

/** Estado do episodio para a tela: so "aguardando_confirmacao" precisa de acento. */
private fun estadoLegivel(estado: String) =
    if (estado == "aguardando_confirmacao") "aguardando confirmação" else semSublinhado(estado)

private fun semSublinhado(texto: String) = texto.replace('_', ' ').replace('.', ' ')

private fun comData(instante: Long?, texto: String, fuso: TimeZone) =
    if (instante == null) texto else dataHora(instante, fuso) + "  " + texto

private fun dataHora(millis: Long, fuso: TimeZone): String {
    val formato = SimpleDateFormat("dd/MM HH:mm", Locale.US)
    formato.timeZone = fuso
    return formato.format(java.util.Date(millis))
}

// ---------------------------------------------------------------- leitura (org.json)
// opt() + tipo explicito de proposito: optString/optInt convertem outros tipos em vez de recusar.

private fun objeto(json: String): JSONObject? = try {
    JSONObject(json)
} catch (e: org.json.JSONException) {
    null
}

/** userId e geradoEm; null se schemaVersion nao for 1 ou se algum dos tres faltar. */
private fun cabecalho(raiz: JSONObject): Pair<Int, Long>? {
    if ((raiz.opt("schemaVersion") as? Number)?.toInt() != 1) return null
    val userId = (raiz.opt("userId") as? Number)?.toInt() ?: return null
    val geradoEm = lerIso8601(raiz.opt("geradoEm") as? String) ?: return null
    return userId to geradoEm
}

/** Todos os itens ou nada: um item invalido invalida a lista. */
private fun <T> lista(bruto: Any?, ler: (JSONObject) -> T?): List<T>? {
    val arr = bruto as? JSONArray ?: return null
    val itens = ArrayList<T>(arr.length())
    for (i in 0 until arr.length()) {
        itens.add(ler(arr.opt(i) as? JSONObject ?: return null) ?: return null)
    }
    return itens
}

/** Texto opcional: ausente ou null viram null; outro tipo tambem (campo opcional nao invalida a resposta). */
private fun JSONObject.texto(chave: String): String? = opt(chave) as? String

private fun JSONObject.instante(chave: String): Long? = lerIso8601(texto(chave))

private fun comunicacao(o: JSONObject): Comunicacao? {
    return Comunicacao(
        deviceId = o.texto("deviceId") ?: return null,
        fonte = o.texto("fonte") ?: return null,
        instante = o.instante("instante")
    )
}

private fun alerta(o: JSONObject): AlertaCuidador? {
    return AlertaCuidador(
        instante = o.instante("instante"),
        deviceId = o.texto("deviceId") ?: return null,
        tipo = o.texto("tipo") ?: return null,
        mensagem = o.texto("mensagem") ?: return null,
        episodioId = o.texto("episodioId")
    )
}

private fun confirmacao(o: JSONObject): ConfirmacaoCuidador? {
    return ConfirmacaoCuidador(
        instante = o.instante("instante"),
        tipo = o.texto("tipo") ?: return null,
        episodioId = o.texto("episodioId"),
        resultado = o.texto("resultado"),
        estado = o.texto("estado"),
        motivo = o.texto("motivo")
    )
}

private fun episodio(o: JSONObject): EpisodioCuidador? {
    return EpisodioCuidador(
        episodioId = o.texto("episodioId") ?: return null,
        deviceId = o.texto("deviceId") ?: return null,
        tipo = o.texto("tipo") ?: return null,
        estado = o.texto("estado") ?: return null,
        abertoEm = o.instante("abertoEm"),
        atualizadoEm = o.instante("atualizadoEm"),
        encerradoEm = o.instante("encerradoEm"),
        resultado = o.texto("resultado")
    )
}
