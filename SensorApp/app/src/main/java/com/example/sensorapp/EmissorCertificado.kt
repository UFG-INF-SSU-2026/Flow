package com.example.sensorapp

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.File
import java.net.Socket
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.Principal
import java.security.PrivateKey
import java.security.Signature
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.security.spec.ECGenParameterSpec
import javax.net.ssl.KeyManager
import javax.net.ssl.X509KeyManager

/**
 * Certificado de cliente do app, emitido pela borda (POST /auth/certificado). O app comeca sem certificado, pede um
 * depois do login e o apresenta em todas as conexoes novas. Nada disso vai no APK:
 * - a chave EC P-256 e gerada no Android Keystore e nunca sai do aparelho (so o CSR sai);
 * - o certificado (publico) fica em arquivo privado do app, junto da conta e do device_id a que pertence.
 *
 * O certificado vale por conexao: depois de emitido ou descartado, [FlowHttp.recarregarTls] abre conexoes novas.
 * Chamadas bloqueantes ([garantir]) devem sair da thread principal. A verificacao do servidor nunca e desligada.
 */
class EmissorCertificado(
    private val ctx: Context,
    private val prefs: SharedPreferences,
    private val auth: AuthClient,
    private val baseUrl: () -> String,
    private val deviceId: String
) {
    private val arquivo = File(ctx.filesDir, "cert_cliente.pem")
    private val jsonType = "application/json; charset=utf-8".toMediaType()

    // Lidos sem trava por FlowHttp (evita impasse com a trava de FlowHttp durante a emissao)
    @Volatile private var atual: X509Certificate? = null
    @Volatile private var contaDoCert = -1
    @Volatile private var deviceDoCert: String? = null
    @Volatile private var gerente: KeyManager? = null

    // Recuperacao em segundo plano: uma por vez e com espera crescente quando falha (sem laco de pedidos)
    @Volatile private var emAndamento = false
    @Volatile private var liberadoEmMs = 0L
    private var esperaMs = ESPERA_MIN_MS

    init {
        carregar()
    }

    /** Gerenciador de chave para o TLS (null sem certificado: a conexao segue sem certificado de cliente). */
    fun gerenteDeChave(): KeyManager? = gerente

    fun temCertificado(): Boolean = atual != null

    /** O certificado guardado nao serve a esta sessao: vencido, ou de outra conta ou device_id. Apresenta-lo so atrapalharia. */
    private fun incompativel(): Boolean {
        val c = atual ?: return false
        val s = auth.sessao
        val outraIdentidade = s != null && (contaDoCert != s.userId || deviceDoCert != deviceId)
        return outraIdentidade || System.currentTimeMillis() >= c.notAfter.time
    }

    /** Falta certificado, ele e de outra conta ou device_id, ou restam menos da metade da validade. Sem sessao: false. */
    fun precisaEmitir(): Boolean {
        val s = auth.sessao ?: return false
        val c = atual ?: return true
        if (contaDoCert != s.userId || deviceDoCert != deviceId) return true
        return precisaRenovar(c)
    }

    /** Emite um certificado se precisar. Bloqueante. true se, ao fim, ha certificado valido para esta conta. */
    @Synchronized
    fun garantir(): Boolean {
        if (auth.sessao == null) return false
        if (!precisaEmitir()) return true
        return emitir()
    }

    /**
     * Recuperacao automatica (falha de conexao feita com certificado, ou 401/403 de certificado): pede um certificado
     * novo sem descartar o atual, que so e trocado se a emissao der certo (o pedido sai por um cliente sem certificado,
     * entao o recusado nao o atrapalha). Roda em outra thread, uma por vez, e respeita a espera entre tentativas.
     */
    fun renovarEmSegundoPlano() {
        if (auth.sessao == null || emAndamento) return
        if (System.currentTimeMillis() < liberadoEmMs) return
        emAndamento = true
        Thread {
            val ok = try {
                synchronized(this) { emitir() }
            } catch (e: Exception) {
                false
            } finally {
                emAndamento = false
            }
            esperaMs = if (ok) ESPERA_MIN_MS else minOf(esperaMs * 2, ESPERA_MAX_MS)
            liberadoEmMs = System.currentTimeMillis() + (if (ok) ESPERA_MIN_MS else esperaMs)
        }.start()
    }

    // ---------------------------------------------------------------- emissao
    private fun emitir(): Boolean {
        try {
            if (incompativel()) descartarCertificado()      // vencido ou de outra conta/device_id: nao serve a ninguem
            val chave = chavePublica()
            val csr = csrEmPem(
                Base64.encodeToString(
                    montarCsr(chave) { info ->
                        Signature.getInstance("SHA256withECDSA").run {
                            initSign(chavePrivada())
                            update(info)
                            sign()
                        }
                    },
                    Base64.NO_WRAP
                )
            )
            val sessao = auth.sessao ?: return false
            val pem = pedir(csr, sessao.accessToken) ?: return false
            val cert = CertificateFactory.getInstance("X.509")
                .generateCertificate(ByteArrayInputStream(pem.toByteArray())) as X509Certificate
            cert.checkValidity()
            if (!cert.publicKey.encoded.contentEquals(chave.encoded)) return false   // nao e o certificado da nossa chave
            arquivo.writeText(pem)
            prefs.edit().putInt(PREF_CONTA, sessao.userId).putString(PREF_DEVICE, deviceId).apply()
            aplicar(cert, sessao.userId, deviceId)
            FlowHttp.recarregarTls()        // o certificado so vale em conexao nova
            return true
        } catch (e: Exception) {
            Log.w(TAG, "certificado nao emitido: ${e.javaClass.simpleName}")
            return false
        }
    }

    /** POST /auth/certificado (envelope AES), sem apresentar certificado de cliente. Devolve o PEM ou null. Renova o token uma vez se vier 401. */
    private fun pedir(csr: String, token: String): String? {
        var tokenAtual = token
        repeat(2) { tentativa ->
            val corpo = JSONObject().put("schemaVersion", 1).put("csr", csr)
            val (nonce, ct) = CryptoUtils.encrypt(corpo.toString())
            val envelope = JSONObject().put("nonce", nonce).put("ciphertext", ct)
            val req = Request.Builder()
                .url(baseUrl() + "/auth/certificado")
                .header("Authorization", "Bearer $tokenAtual")      // o interceptor nao injeta em /auth/...
                .post(envelope.toString().toRequestBody(jsonType))
                .build()
            FlowHttp.semCertificado.newCall(req).execute().use { resp ->
                when {
                    resp.isSuccessful -> {
                        val e = JSONObject(resp.body?.string().orEmpty())
                        val claro = CryptoUtils.decrypt(e.getString("nonce"), e.getString("ciphertext"))
                        return JSONObject(claro).getString("certificado")
                    }
                    resp.code == 401 && tentativa == 0 -> {
                        if (!auth.renovar(tokenAtual)) return null
                        tokenAtual = auth.sessao?.accessToken ?: return null
                    }
                    else -> {
                        // 400 csr_invalido (nao repetir em laco), 503 emissao_indisponivel (espera crescente), outros
                        Log.w(TAG, "POST /auth/certificado recusado: ${resp.code}")
                        return null
                    }
                }
            }
        }
        return null
    }

    // ---------------------------------------------------------------- chave e armazenamento
    private fun lojaDeChaves(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun chavePublica(): java.security.PublicKey {
        val loja = lojaDeChaves()
        if (!loja.containsAlias(ALIAS)) {
            KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore").apply {
                initialize(
                    KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                        .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                        // SHA-256 assina o CSR; os demais (e NONE) sao os que o TLS pode pedir ao assinar com esta chave
                        .setDigests(
                            KeyProperties.DIGEST_SHA256, KeyProperties.DIGEST_NONE,
                            KeyProperties.DIGEST_SHA384, KeyProperties.DIGEST_SHA512
                        )
                        .build()
                )
            }.generateKeyPair()
        }
        return lojaDeChaves().getCertificate(ALIAS).publicKey
    }

    private fun chavePrivada(): PrivateKey = lojaDeChaves().getKey(ALIAS, null) as PrivateKey

    /** Le o certificado guardado; so vale se a chave do Keystore ainda for a dele. */
    private fun carregar() {
        try {
            if (!arquivo.isFile) return
            val cert = CertificateFactory.getInstance("X.509")
                .generateCertificate(ByteArrayInputStream(arquivo.readBytes())) as X509Certificate
            val loja = lojaDeChaves()
            if (!loja.containsAlias(ALIAS) || !cert.publicKey.encoded.contentEquals(loja.getCertificate(ALIAS).publicKey.encoded)) {
                descartarCertificado()
                return
            }
            aplicar(cert, prefs.getInt(PREF_CONTA, -1), prefs.getString(PREF_DEVICE, null))
        } catch (e: Exception) {
            descartarCertificado()
        }
    }

    private fun aplicar(cert: X509Certificate, conta: Int, device: String?) {
        contaDoCert = conta
        deviceDoCert = device
        gerente = GerenteDeChave(arrayOf(cert)) { chavePrivada() }
        atual = cert
    }

    /** Apaga o certificado (a chave do Keystore fica: o proximo CSR reaproveita a mesma chave) e abre conexoes sem ele. */
    private fun descartarCertificado() {
        arquivo.delete()
        prefs.edit().remove(PREF_CONTA).remove(PREF_DEVICE).apply()
        val tinha = atual != null
        atual = null
        gerente = null
        contaDoCert = -1
        deviceDoCert = null
        if (tinha) FlowHttp.recarregarTls()
    }

    private companion object {
        const val TAG = "EmissorCertificado"
        const val ALIAS = "flow_cliente"
        const val PREF_CONTA = "cert_user_id"
        const val PREF_DEVICE = "cert_device_id"
        const val ESPERA_MIN_MS = 30_000L
        const val ESPERA_MAX_MS = 300_000L
    }
}

/** Entrega ao TLS o certificado e a chave (do Keystore, nao extraivel) quando o servidor pede certificado de cliente. */
class GerenteDeChave(
    private val cadeia: Array<X509Certificate>,
    private val chave: () -> PrivateKey
) : X509KeyManager {
    override fun chooseClientAlias(tipos: Array<out String>?, emissores: Array<out Principal>?, socket: Socket?): String? =
        if (tipos == null || tipos.any { it == "EC" }) ALIAS else null

    override fun getClientAliases(tipo: String?, emissores: Array<out Principal>?): Array<String> = arrayOf(ALIAS)
    override fun getCertificateChain(alias: String?): Array<X509Certificate> = cadeia
    override fun getPrivateKey(alias: String?): PrivateKey = chave()
    override fun chooseServerAlias(tipo: String?, emissores: Array<out Principal>?, socket: Socket?): String? = null
    override fun getServerAliases(tipo: String?, emissores: Array<out Principal>?): Array<String>? = null

    private companion object {
        const val ALIAS = "flow_cliente"
    }
}
