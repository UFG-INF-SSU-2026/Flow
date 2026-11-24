package com.example.sensorapp

import java.security.cert.CertificateException
import javax.net.ssl.SSLException
import javax.net.ssl.SSLPeerUnverifiedException

/*
 * Como o app reconhece que o CERTIFICADO DE CLIENTE foi recusado (e nao o do servidor). Sem android.*: roda na JVM.
 */

/**
 * "certificado_ausente" para 401 e "certificado_incompativel" para 403, quando o corpo de erro da borda
 * ({"status": "erro", "codigo": ...}) traz esse codigo. Qualquer outro 401/403 (token, papel) devolve null.
 */
fun erroDeCertificadoNoCorpo(codigoHttp: Int, corpo: String): String? = when {
    codigoHttp == 401 && corpo.contains("\"certificado_ausente\"") -> "certificado_ausente"
    codigoHttp == 403 && corpo.contains("\"certificado_incompativel\"") -> "certificado_incompativel"
    else -> null
}

/**
 * Handshake recusado pelo SERVIDOR por causa do nosso certificado (vencido, de outra CA, sem clientAuth): falha de TLS
 * que nao e a verificacao do certificado do servidor (cadeia ou nome). Esta, ao contrario, nunca e tratada como motivo
 * para pedir certificado novo: a confianca no servidor nao se negocia.
 */
fun falhaTlsPorCertificadoDeCliente(e: Throwable): Boolean {
    if (e !is SSLException) return false
    var causa: Throwable? = e
    var voltas = 0
    while (causa != null && voltas++ < 10) {
        if (causa is CertificateException || causa is SSLPeerUnverifiedException) return false
        causa = causa.cause
    }
    return true
}
