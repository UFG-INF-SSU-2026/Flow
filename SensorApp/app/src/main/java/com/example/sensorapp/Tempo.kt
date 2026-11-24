package com.example.sensorapp

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Formata um timestamp em milissegundos como ISO 8601 com offset (ex.: 2026-09-11T22:00:20.000-03:00). */
fun formatarIso8601(millis: Long): String {
    val formato = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US)
    return formato.format(Date(millis))
}

private val FRACAO_DE_SEGUNDO = Regex("""\.\d+""")

/**
 * Le uma data ISO 8601 com offset (ex.: 2026-11-24T19:39:26.123456+00:00) e devolve os milissegundos, ou null se o texto
 * for nulo ou invalido. A fracao de segundo, quando existe, e descartada (o Python escreve ate microssegundos e a
 * SimpleDateFormat nao le essa precisao).
 */
fun lerIso8601(texto: String?): Long? {
    if (texto == null) return null
    return try {
        val formato = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US)
        formato.isLenient = false
        formato.parse(texto.replaceFirst(FRACAO_DE_SEGUNDO, ""))?.time
    } catch (e: java.text.ParseException) {
        null
    }
}
