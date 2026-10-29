package com.example.sensorapp

import android.content.SharedPreferences
import okhttp3.Authenticator
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.Route
import org.json.JSONObject

/**
 * Autenticacao do app (Marco 3): login, renovacao do token e injecao do cabecalho Authorization.
 *
 * - O corpo de /auth/login e /auth/refresh vai CIFRADO (mesmo envelope AES-GCM do resto do sistema):
 *   a senha nunca trafega em claro.
 * - A senha NAO e guardada no aparelho; so os tokens (SharedPreferences privado).
 * - O papel e o user_id vem do servidor. Este app so aceita o papel "idoso".
 *
 * login() e renovar() sao BLOQUEANTES: chame fora da thread principal.
 * As chamadas saem pelo cliente unico [FlowHttp]; o interceptor e o autenticador abaixo ignoram rotas de autenticacao (/auth/...)
 * (que nao leva Authorization nem pode disparar renovacao dentro da propria renovacao).
 */
class AuthClient(
    private val prefs: SharedPreferences,
    private val baseUrl: () -> String,
    private val deviceId: String
) {
    data class Sessao(val accessToken: String, val refreshToken: String, val papel: String, val userId: Int)
    class LoginResult(val ok: Boolean, val mensagem: String)

    private val jsonType = "application/json; charset=utf-8".toMediaType()

    @Volatile
    var sessao: Sessao? = carregar()
        private set

    /** Chamado (em qualquer thread) quando o refresh e recusado: o usuario precisa logar de novo. */
    var onSessaoExpirada: (() -> Unit)? = null

    val userId: Int? get() = sessao?.userId

    // ---------------------------------------------------------------- persistencia
    private fun carregar(): Sessao? {
        val a = prefs.getString("auth_access", null) ?: return null
        val r = prefs.getString("auth_refresh", null) ?: return null
        val p = prefs.getString("auth_papel", null) ?: return null
        val u = prefs.getInt("auth_user_id", -1)
        return if (u >= 0) Sessao(a, r, p, u) else null
    }

    private fun salvar(s: Sessao) {
        prefs.edit()
            .putString("auth_access", s.accessToken)
            .putString("auth_refresh", s.refreshToken)
            .putString("auth_papel", s.papel)
            .putInt("auth_user_id", s.userId)
            .apply()
        sessao = s
    }

    fun sair() {
        prefs.edit().remove("auth_access").remove("auth_refresh").remove("auth_papel").remove("auth_user_id").apply()
        sessao = null
    }

    // ---------------------------------------------------------------- chamadas de autenticacao (/auth/...)
    /** Retorna (codigoHttp, corpoDecifrado ou null se a resposta nao foi 2xx). */
    private fun postCifrado(path: String, corpo: JSONObject): Pair<Int, JSONObject?> {
        val (nonce, ct) = CryptoUtils.encrypt(corpo.toString())
        val envelope = JSONObject().put("nonce", nonce).put("ciphertext", ct)
        val req = Request.Builder()
            .url(baseUrl() + path)
            .post(envelope.toString().toRequestBody(jsonType))
            .build()
        FlowHttp.cliente.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) return Pair(resp.code, null)
            val e = JSONObject(resp.body?.string().orEmpty())
            val claro = CryptoUtils.decrypt(e.getString("nonce"), e.getString("ciphertext"))
            return Pair(resp.code, JSONObject(claro))
        }
    }

    fun login(identificador: String, senha: String): LoginResult {
        try {
            val corpo = JSONObject()
                .put("schemaVersion", 3)
                .put("identificador", identificador)
                .put("segredo", senha)
                .put("fonte", "app")
                .put("device_id", deviceId)
            val (codigo, resp) = postCifrado("/auth/login", corpo)
            if (codigo == 429) return LoginResult(false, "Muitas tentativas. Aguarde alguns minutos.")
            if (codigo >= 500) return LoginResult(false, "Servidor indisponível. Tente de novo em instantes.")
            if (resp == null) return LoginResult(false, "Usuário ou senha inválidos.")
            if (resp.getString("papel") != "idoso") {
                return LoginResult(false, "Tipo de conta ainda não suportado neste app.")
            }
            salvar(
                Sessao(
                    accessToken = resp.getString("access_token"),
                    refreshToken = resp.getString("refresh_token"),
                    papel = resp.getString("papel"),
                    userId = resp.getJSONArray("user_ids").getInt(0)
                )
            )
            return LoginResult(true, "Login realizado.")
        } catch (e: Exception) {
            return LoginResult(false, "Falha de rede ou resposta inesperada: ${e.message}")
        }
    }

    /**
     * Renova o par de tokens. [tokenUsado] = o access token que acabou de receber 401 (ou null para forcar):
     * se outra thread ja renovou nesse meio tempo, nao renova de novo.
     * Refresh recusado (400, 401 ou 403) -> sessao apagada + onSessaoExpirada. Falha de rede ou do servidor (500 ou
     * mais, como o 503 com o banco fora) -> mantem a sessao e devolve false (tenta depois).
     */
    @Synchronized
    fun renovar(tokenUsado: String?): Boolean {
        val atual = sessao ?: return false
        if (tokenUsado != null && atual.accessToken != tokenUsado) return true
        return try {
            val corpo = JSONObject().put("schemaVersion", 3).put("refresh_token", atual.refreshToken)
            val (codigo, resp) = postCifrado("/auth/refresh", corpo)
            if (resp == null) {
                if (refreshEncerraSessao(codigo)) {
                    sair()
                    onSessaoExpirada?.invoke()
                }
                false
            } else {
                salvar(
                    Sessao(
                        resp.getString("access_token"), resp.getString("refresh_token"),
                        atual.papel, atual.userId
                    )
                )
                true
            }
        } catch (e: Exception) {
            false
        }
    }

    // ---------------------------------------------------------------- OkHttp
    /** Injeta "Authorization: Bearer ..." em toda requisicao, menos nas de rotas de autenticacao (/auth/...). */
    val interceptor: Interceptor = object : Interceptor {
        override fun intercept(chain: Interceptor.Chain): Response {
            val s = sessao
            val req = if (s != null && !ehRotaDeAuth(chain.request())) {
                chain.request().newBuilder().header("Authorization", "Bearer ${s.accessToken}").build()
            } else {
                chain.request()
            }
            return chain.proceed(req)
        }
    }

    /** Em caso de 401: renova o token e repete a requisicao UMA vez (X-Retry-Auth evita laco). */
    val authenticator: Authenticator = object : Authenticator {
        override fun authenticate(route: Route?, response: Response): Request? {
            if (ehRotaDeAuth(response.request)) return null
            if (response.request.header("X-Retry-Auth") != null) return null
            val usado = response.request.header("Authorization")?.removePrefix("Bearer ")
            if (!renovar(usado)) return null
            val s = sessao ?: return null
            return response.request.newBuilder()
                .header("Authorization", "Bearer ${s.accessToken}")
                .header("X-Retry-Auth", "1")
                .build()
        }
    }

    private fun ehRotaDeAuth(req: Request): Boolean = req.url.encodedPath.startsWith("/auth/")
}
