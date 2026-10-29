package com.example.sensorapp

/**
 * Resposta do /auth/refresh que ENCERRA a sessao: so 400, 401 e 403 (refresh recusado).
 * Qualquer outra (503 com o banco fora, 500, 429...) mantem a sessao e a renovacao e tentada de novo depois.
 */
fun refreshEncerraSessao(codigoHttp: Int): Boolean = codigoHttp == 400 || codigoHttp == 401 || codigoHttp == 403
