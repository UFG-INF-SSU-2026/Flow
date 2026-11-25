package com.example.sensorapp

import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import androidx.appcompat.app.AppCompatActivity
import com.example.sensorapp.databinding.ActivityCuidadorBinding
import okhttp3.Request
import org.json.JSONObject

/**
 * Tela do cuidador: estado atual e historico da pessoa escolhida, so leitura. Atualiza por polling a cada
 * [INTERVALO_MS], so com a tela visivel (onResume liga, onPause desliga). O ultimo dado fica so em memoria.
 * Nao liga sensores, localizacao nem o [MonitorService].
 */
class CuidadorActivity : AppCompatActivity() {

    private enum class Aviso { NENHUM, SEM_CONEXAO, SEM_ACESSO }

    /** O que um ciclo trouxe. Resposta invalida vem como null: o ultimo dado daquela parte fica como estava. */
    private class Resultado(val estado: Estado?, val historico: Historico?, val aviso: Aviso)

    private lateinit var binding: ActivityCuidadorBinding
    private val app: FlowApp get() = application as FlowApp
    private val handler = Handler(Looper.getMainLooper())

    private var pessoas: List<Int> = emptyList()
    private var pessoaAtual = -1
    private var ativo = false          // entre onResume e onPause
    private var emCiclo = false        // um ciclo so comeca se o anterior terminou
    private var geracao = 0            // sobe a cada troca de pessoa: resposta da pessoa anterior e descartada

    private var estado: Estado? = null
    private var historico: Historico? = null
    private var chegadaEstado = 0L     // SystemClock.elapsedRealtime() da ultima resposta valida
    private var aviso = Aviso.NENHUM

    private val aoExpirarSessao: () -> Unit = { runOnUiThread { voltarAoLogin() } }

    private val ciclo = object : Runnable {
        override fun run() {
            if (!ativo) return
            if (app.auth.sessao == null) {      // sessao encerrada (refresh recusado): volta ao login
                voltarAoLogin()
                return
            }
            redesenhar()                        // recalcula o "ha X" mesmo sem resposta nova
            buscar()
            handler.postDelayed(this, INTERVALO_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val sessao = app.auth.sessao
        if (sessao == null || sessao.papel != "cuidador") {
            voltarAoLogin()
            return
        }
        binding = ActivityCuidadorBinding.inflate(layoutInflater)
        setContentView(binding.root)

        pessoas = sessao.userIds
        pessoaAtual = pessoas.first()
        if (pessoas.size > 1) {
            val adaptador = ArrayAdapter(this, android.R.layout.simple_spinner_item, pessoas.map { "Pessoa $it" })
            adaptador.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
            binding.spPessoa.adapter = adaptador
            binding.spPessoa.visibility = View.VISIBLE
            binding.spPessoa.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(pai: AdapterView<*>?, v: View?, posicao: Int, id: Long) {
                    if (pessoas[posicao] != pessoaAtual) trocarDePessoa(pessoas[posicao])
                }

                override fun onNothingSelected(pai: AdapterView<*>?) {}
            }
        }
        binding.btnSair.setOnClickListener {
            app.auth.sair()
            app.prefs.edit().remove(PREF_ULTIMO_USUARIO).apply()
            voltarAoLogin()
        }
        app.aoSessaoExpirada = aoExpirarSessao
        redesenhar()
    }

    override fun onResume() {
        super.onResume()
        if (!::binding.isInitialized) return
        ativo = true
        handler.removeCallbacks(ciclo)
        handler.post(ciclo)
    }

    override fun onPause() {
        ativo = false
        handler.removeCallbacks(ciclo)
        super.onPause()
    }

    override fun onDestroy() {
        if (app.aoSessaoExpirada === aoExpirarSessao) app.aoSessaoExpirada = null
        super.onDestroy()
    }

    private fun voltarAoLogin() {
        startActivity(Intent(this, MainActivity::class.java))
        finish()
    }

    private fun trocarDePessoa(userId: Int) {
        pessoaAtual = userId
        geracao++
        emCiclo = false
        estado = null
        historico = null
        aviso = Aviso.NENHUM
        redesenhar()
        if (ativo) {
            handler.removeCallbacks(ciclo)
            handler.post(ciclo)                 // busca na hora
        }
    }

    // ---------------------------------------------------------------- rede (thread propria)

    private fun buscar() {
        if (emCiclo) return
        emCiclo = true
        val minhaGeracao = geracao
        val userId = pessoaAtual
        Thread {
            val resultado = baixarCiclo(userId)
            runOnUiThread {
                if (isDestroyed || minhaGeracao != geracao) return@runOnUiThread
                emCiclo = false
                aplicar(resultado)
            }
        }.start()
    }

    private fun baixarCiclo(userId: Int): Resultado {
        val (novoEstado, avisoEstado) = baixar(userId, "estado", ::lerEstado)
        if (avisoEstado != Aviso.NENHUM) return Resultado(null, null, avisoEstado)
        val (novoHistorico, avisoHistorico) = baixar(userId, "historico", ::lerHistorico)
        return Resultado(novoEstado, novoHistorico, avisoHistorico)
    }

    private fun <T> baixar(userId: Int, rota: String, ler: (String) -> T?): Pair<T?, Aviso> {
        try {
            val req = Request.Builder().url("${app.baseUrl()}/cuidador/$userId/$rota").get().build()
            FlowHttp.cliente.newCall(req).execute().use { resp ->
                val corpo = resp.body?.string().orEmpty()
                return when {
                    resp.code == 200 -> Pair(decifrar(corpo)?.let(ler), Aviso.NENHUM)
                    resp.code == 403 && corpo.contains("fora_de_escopo") -> Pair(null, Aviso.SEM_ACESSO)
                    else -> Pair(null, Aviso.SEM_CONEXAO)
                }
            }
        } catch (e: Exception) {       // qualquer falha vira "sem conexao": uma excecao solta travaria o polling
            Log.w("CuidadorActivity", "GET /cuidador/$rota falhou: ${e.javaClass.simpleName}")
            return Pair(null, Aviso.SEM_CONEXAO)
        }
    }

    /** Abre o envelope {nonce, ciphertext}; null se a resposta nao for um envelope valido. */
    private fun decifrar(corpo: String): String? = try {
        val e = JSONObject(corpo)
        CryptoUtils.decrypt(e.getString("nonce"), e.getString("ciphertext"))
    } catch (e: Exception) {
        Log.w("CuidadorActivity", "resposta ilegivel: ${e.javaClass.simpleName}")
        null
    }

    // ---------------------------------------------------------------- tela (thread principal)

    private fun aplicar(r: Resultado) {
        val agora = SystemClock.elapsedRealtime()
        aviso = r.aviso
        if (aviso == Aviso.SEM_ACESSO) {
            estado = null
            historico = null
        }
        r.estado?.let { estado = it; chegadaEstado = agora }
        r.historico?.let { historico = it }
        redesenhar()
    }

    private fun redesenhar() {
        val agora = SystemClock.elapsedRealtime()
        val e = estado
        val h = historico
        val atualizado = if (e != null) "Atualizado " + ha(agora - chegadaEstado) else null
        binding.tvAviso.text = when (aviso) {
            Aviso.SEM_ACESSO -> "Sem acesso a esta pessoa."
            Aviso.SEM_CONEXAO -> if (atualizado != null) "Sem conexão. $atualizado." else "Sem conexão."
            Aviso.NENHUM -> atualizado ?: "Carregando…"
        }
        binding.tvEstado.text = e?.let { linhasDoEstado(it, agora - chegadaEstado).joinToString("\n") } ?: ""
        binding.tvHistorico.text = h?.let {
            linhasDoHistorico(it).joinToString("\n").ifEmpty { "Nenhum registro nos últimos 90 dias." }
        } ?: ""
    }

    private companion object {
        const val INTERVALO_MS = 5_000L
    }
}
