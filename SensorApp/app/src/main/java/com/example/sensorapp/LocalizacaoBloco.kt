package com.example.sensorapp

import org.json.JSONObject

/** Versao do bloco "localizacao" da telemetria (docs/schemas.md, 4.3). */
const val SCHEMA_VERSION_LOCALIZACAO = 3

/** Posicao mais velha que isto nao e enviada. */
const val IDADE_MAX_LOCALIZACAO_MS = 120_000L

/** Tolerancia para relogio adiantado: alem disso, um instante no futuro e absurdo. */
const val FUTURO_MAX_LOCALIZACAO_MS = 60_000L

/**
 * Monta o bloco "localizacao" da telemetria. O app so DESCARTA o absurdo; nao corrige nem adivinha valores, e quem
 * valida de verdade e o servidor. Devolve null (o bloco nao e enviado) se:
 *  - latitude ou longitude nao sao finitas ou estao fora da faixa (-90..90, -180..180);
 *  - a precisao e negativa ou nao finita;
 *  - a posicao e mais velha que [IDADE_MAX_LOCALIZACAO_MS] (ou esta alem de [FUTURO_MAX_LOCALIZACAO_MS] no futuro).
 *
 * [instanteMs] e o instante da medicao (relogio de parede); [agoraMs] e o instante do envio.
 * Nunca registra latitude nem longitude em log.
 */
fun montarBlocoLocalizacao(
    latitude: Double,
    longitude: Double,
    precisaoM: Double,
    instanteMs: Long,
    agoraMs: Long
): JSONObject? {
    if (!latitude.isFinite() || latitude < -90.0 || latitude > 90.0) return null
    if (!longitude.isFinite() || longitude < -180.0 || longitude > 180.0) return null
    if (!precisaoM.isFinite() || precisaoM < 0.0) return null
    val idadeMs = agoraMs - instanteMs
    if (idadeMs > IDADE_MAX_LOCALIZACAO_MS || idadeMs < -FUTURO_MAX_LOCALIZACAO_MS) return null
    return JSONObject().apply {
        put("schemaVersion", SCHEMA_VERSION_LOCALIZACAO)
        put("latitude", latitude)
        put("longitude", longitude)
        put("precisao_m", precisaoM)
        put("instante_medicao", formatarIso8601(instanteMs))
    }
}
