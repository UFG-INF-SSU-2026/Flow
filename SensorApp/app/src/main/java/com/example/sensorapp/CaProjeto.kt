package com.example.sensorapp

import android.content.Context
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.security.KeyStore
import java.security.MessageDigest
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.Date
import javax.net.ssl.KeyManager
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

/**
 * CA do projeto: a UNICA confianca do app para falar com o servidor (nao confia nas autoridades do sistema).
 *
 * Cada instalacao do servidor tem a sua CA (SensorServer/certs/ca.crt). Por isso o app a importa em tempo de execucao
 * (Configuracoes > Importar CA), em vez de depender de uma copia embutida na compilacao: em outra maquina, a CA muda e o
 * APK continua valendo. Se nada foi importado, usa a copia opcional res/raw/flow_cert.pem (o script do servidor a
 * copia para la antes de compilar). Sem nenhuma das duas, nenhum servidor e aceito (falha fechada).
 */
object CaProjeto {
    private const val ARQUIVO_IMPORTADA = "ca_projeto.pem"

    enum class Origem { IMPORTADA, EMBUTIDA, NENHUMA }

    class Carregada(val certificados: List<X509Certificate>, val origem: Origem)

    /** A CA importada (se valida) tem prioridade sobre a embutida. */
    fun carregar(ctx: Context): Carregada {
        val importada = File(ctx.filesDir, ARQUIVO_IMPORTADA)
        if (importada.isFile) {
            val r = interpretarCa(importada.readBytes())
            if (r is ResultadoCa.Ok) return Carregada(r.certificados, Origem.IMPORTADA)
        }
        // getIdentifier: a copia embutida e opcional; uma referencia direta (R.raw) quebraria a compilacao sem ela
        val id = ctx.resources.getIdentifier("flow_cert", "raw", ctx.packageName)
        if (id != 0) {
            val r = ctx.resources.openRawResource(id).use { interpretarCa(lerLimitado(it, LIMITE_BYTES)) }
            if (r is ResultadoCa.Ok) return Carregada(r.certificados, Origem.EMBUTIDA)
        }
        return Carregada(emptyList(), Origem.NENHUMA)
    }

    /** Valida e guarda a CA escolhida pela pessoa. So grava se for valida; nunca guarda chave privada. */
    fun importar(ctx: Context, bytes: ByteArray): ResultadoCa {
        val r = interpretarCa(bytes)
        if (r is ResultadoCa.Ok) {
            val tmp = File(ctx.filesDir, "$ARQUIVO_IMPORTADA.tmp")
            tmp.writeBytes(bytes)
            if (!tmp.renameTo(File(ctx.filesDir, ARQUIVO_IMPORTADA))) {
                tmp.delete()
                return ResultadoCa.Erro("não foi possível guardar a CA")
            }
        }
        return r
    }

    fun descricao(c: Carregada): String = when (c.origem) {
        Origem.IMPORTADA -> "CA importada (SHA-256 ${impressaoDigital(c.certificados.first())})"
        Origem.EMBUTIDA -> "CA embutida no app (SHA-256 ${impressaoDigital(c.certificados.first())})"
        Origem.NENHUMA -> "Sem CA: será baixada do servidor ao salvar com usuário e senha"
    }

    /**
     * Fabrica de sockets que so confia em [certificados]. Verificacao de cadeia, de validade e de nome do servidor
     * continuam ligadas. Sem certificados, o gerenciador nao tem ancora e recusa toda conexao. [chaveDeCliente] (se
     * houver) e o certificado de cliente que o app apresenta quando o servidor o pede.
     */
    fun confianca(
        certificados: List<X509Certificate>,
        chaveDeCliente: KeyManager? = null
    ): Pair<SSLSocketFactory, X509TrustManager> {
        val loja = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
            load(null, null)
            certificados.forEachIndexed { i, c -> setCertificateEntry("ca$i", c) }
        }
        val fabrica = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(loja) }
        val gerente = fabrica.trustManagers.filterIsInstance<X509TrustManager>().first()
        val contexto = SSLContext.getInstance("TLS").apply { init(chaveDeCliente?.let { arrayOf(it) }, arrayOf(gerente), null) }
        return contexto.socketFactory to gerente
    }
}

sealed class ResultadoCa {
    class Ok(val certificados: List<X509Certificate>) : ResultadoCa()
    class Erro(val motivo: String) : ResultadoCa()
}

/** Tamanho maximo aceito para o arquivo da CA (um ca.crt tem ~1 KB). */
const val LIMITE_BYTES = 64 * 1024

/**
 * Confere se [bytes] sao a CA do projeto (certificado X.509, PEM ou DER): dentro do prazo e com a marca de CA. Recusa o
 * arquivo que traga chave privada e o certificado de servidor (o erro comum e escolher borda.crt no lugar de ca.crt).
 */
fun interpretarCa(bytes: ByteArray, agora: Date = Date()): ResultadoCa {
    if (bytes.isEmpty()) return ResultadoCa.Erro("arquivo vazio")
    if (bytes.size > LIMITE_BYTES) return ResultadoCa.Erro("arquivo grande demais para ser um ca.crt")
    if (String(bytes, Charsets.ISO_8859_1).contains("PRIVATE KEY")) {
        return ResultadoCa.Erro("o arquivo contém uma chave privada: não use; importe só o ca.crt")
    }
    val lidos = try {
        CertificateFactory.getInstance("X.509").generateCertificates(ByteArrayInputStream(bytes))
    } catch (e: Exception) {
        return ResultadoCa.Erro("não é um certificado X.509 (esperado o ca.crt)")
    }
    val certificados = lidos.filterIsInstance<X509Certificate>()
    if (certificados.isEmpty()) return ResultadoCa.Erro("não é um certificado X.509 (esperado o ca.crt)")
    for (c in certificados) {
        try {
            c.checkValidity(agora)
        } catch (e: Exception) {
            return ResultadoCa.Erro("certificado vencido ou ainda não válido")
        }
        if (c.basicConstraints < 0) {
            return ResultadoCa.Erro("não é uma CA: use o ca.crt, não o certificado do servidor")
        }
    }
    return ResultadoCa.Ok(certificados)
}

/** Primeiros 8 bytes do SHA-256 do certificado, em hexadecimal separado por dois pontos, para comparar com o do servidor. */
fun impressaoDigital(certificado: X509Certificate): String =
    MessageDigest.getInstance("SHA-256").digest(certificado.encoded)
        .take(8).joinToString(":") { "%02X".format(it) }

/** Le no maximo [max] bytes (mais um bloco): um arquivo gigante escolhido por engano nao enche a memoria. */
fun lerLimitado(entrada: InputStream, max: Int): ByteArray {
    val saida = ByteArrayOutputStream()
    val bloco = ByteArray(4096)
    while (saida.size() <= max) {
        val n = entrada.read(bloco)
        if (n < 0) break
        saida.write(bloco, 0, n)
    }
    return saida.toByteArray()
}
