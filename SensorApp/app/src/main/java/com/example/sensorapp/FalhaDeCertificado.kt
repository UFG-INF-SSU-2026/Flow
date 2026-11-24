package com.example.sensorapp

import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.security.cert.CertificateException
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
 * Falha de conexao que pode ser o SERVIDOR recusando o nosso certificado de cliente (vencido, de outra CA, sem
 * clientAuth). Com TLS 1.3 a recusa chega depois do handshake do lado do cliente e costuma aparecer como conexao
 * fechada (`SocketException: Broken pipe`, `Connection reset`, fim de fluxo), nao so como SSLException; por isso vale
 * qualquer [IOException], menos:
 * - a verificacao do certificado do SERVIDOR (cadeia ou nome): a confianca no servidor nao se negocia, e pedir
 *   certificado novo nao a resolveria;
 * - as que acontecem antes de haver conexao (sem rede para o nome, conexao recusada) ou sem resposta (tempo esgotado):
 *   o servidor nao chegou a olhar o certificado.
 */
fun falhaDeConexaoComCertificado(e: Throwable): Boolean {
    if (e !is IOException) return false
    if (e is UnknownHostException || e is ConnectException || e is SocketTimeoutException) return false
    var causa: Throwable? = e
    var voltas = 0
    while (causa != null && voltas++ < 10) {
        if (causa is CertificateException || causa is SSLPeerUnverifiedException) return false
        causa = causa.cause
    }
    return true
}
