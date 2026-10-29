package com.example.sensorapp

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Formata um timestamp em milissegundos como ISO 8601 com offset (ex.: 2026-09-11T22:00:20.000-03:00). */
fun formatarIso8601(millis: Long): String {
    val formato = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US)
    return formato.format(Date(millis))
}
