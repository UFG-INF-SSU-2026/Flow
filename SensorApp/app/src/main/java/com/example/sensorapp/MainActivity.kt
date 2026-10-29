package com.example.sensorapp

import android.Manifest
import android.app.AlertDialog
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.WindowManager
import android.widget.EditText
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.example.sensorapp.databinding.ActivityMainBinding
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.sqrt

class MainActivity : AppCompatActivity(), SensorEventListener {

    // =========================================================================================
    // CONFIGURACOES EDITAVEIS
    // Altere estes valores antes de gerar o APK, conforme necessario.
    // =========================================================================================

    /**
     * Intervalo, em milissegundos, entre cada envio de dados para o servidor.
     * Marco 3: passou de 10s para 1s, para casar com o ciclo de leitura do
     * wokwi (tambem 1x/s) e permitir que o servidor agregue dados de app e
     * wokwi na mesma janela de 1 segundo.
     */
    private val SEND_INTERVAL_MS: Long = 1_000L

    /**
     * IP e porta padrao do servidor, usados apenas na PRIMEIRA vez que o app abre
     * (antes de o usuario configurar algo pelo botao "Configurações").
     * Depois disso, o app passa a usar o valor salvo no proprio celular.
     */
    private val DEFAULT_SERVER_IP: String = "192.168.0.10"
    private val DEFAULT_SERVER_PORT: String = "5000"

    /**
     * Faixa fisica plausivel para o evento LeituraAmbiente (Marco 2).
     * Leituras fora dessa faixa sao descartadas ANTES do envio (validacao no
     * dispositivo, conforme decidido na Atividade 02), para nao gastar banda/
     * bateria enviando ruido de sensor.
     */
    private val CCT_MIN_VALIDO: Float = 1000f
    private val CCT_MAX_VALIDO: Float = 12000f

    /**
     * Versao do schema do payload LeituraAmbiente (mesma ideia aplicada ao
     * contrato do wokwi, ver docs/schemas.md secao 1, e espelhada aqui
     * no lado do app: SensorServer/regra_luz.py sabe interpretar esta versao
     * e assume 1 quando o campo nao vem, sem rejeitar o evento por isso).
     */
    private val SCHEMA_VERSION_LEITURA_AMBIENTE: Int = 3

    /** Versao do schema do payload do app (raiz). Marco 3 = 3 (docs/schemas.md). */
    private val SCHEMA_VERSION: Int = 3

    /** Teto plausivel da magnitude do acelerometro (m/s2); mesmo teto do servidor. */
    private val ACELEROMETRO_MAX_PLAUSIVEL: Float = 160f

    // =========================================================================================

    private lateinit var binding: ActivityMainBinding
    private lateinit var sensorManager: SensorManager
    private lateinit var prefs: android.content.SharedPreferences
    private val handler = Handler(Looper.getMainLooper())

    // Estado do processo (sessao, cliente HTTP unico, eventos, localizacao): compartilhado com o MonitorService
    private val app: FlowApp get() = application as FlowApp
    private val auth: AuthClient get() = app.auth
    private lateinit var alerta: AlertaController

    private var isRunning = false

    // IP e porta atualmente configurados (carregados do SharedPreferences em onCreate)
    private var serverIp: String = DEFAULT_SERVER_IP
    private var serverPort: String = DEFAULT_SERVER_PORT

    // URL base do servidor. O user_id NAO e mais configurado a mao: vem do login (auth.userId).
    private val baseUrl: String
        get() = app.baseUrl()   // so HTTPS (res/xml/network_security_config.xml)

    // Janela do acelerometro desde o ultimo envio (Marco 3): alimenta o bloco "acelerometro" do payload.
    private var accMagUltima = 0f
    private var accMagMax = 0f
    private var accAmostras = 0
    private var accJanelaInicioMs = 0L

    // Guarda a leitura mais recente de cada sensor (chave = tipo do sensor)
    private val latestReadings = mutableMapOf<Int, SensorReading>()

    // Lista de sensores que conseguimos registrar com sucesso
    private val activeSensors = mutableListOf<Sensor>()

    private data class SensorReading(
        val sensorName: String,
        val values: List<Float>,
        val timestamp: Long,        // timestamp interno do SensorEvent (relogio do sistema, nao horario de parede)
        val wallClockMillis: Long   // System.currentTimeMillis() no instante da leitura, usado no event_time
    )

    /**
     * Contador incremental do evento LeituraAmbiente (Marco 2), conforme
     * contrato definido na Atividade 02 (identificador = device_id + seq_num).
     * So avanca quando um evento de ambiente valido e de fato incluido no
     * envio. Reinicia em 0 se o app for reaberto (limitacao aceita nesta
     * fase de protótipo - nao persistido em disco).
     */
    private var ambientSeqNum: Long = 0L

    private val deviceId: String get() = app.deviceId

    // Localizacao (opcional, desligada por padrao): so a posicao nova ainda nao enviada entra no lote
    private var ultimaLocalizacaoEnviadaMs = 0L
    private var ajustandoSwitchLocalizacao = false
    private val enviarLocalizacao: Boolean get() = prefs.getBoolean("enviar_localizacao", false)

    // Permissoes pedidas em tempo de execucao (nunca no inicio do app, e o app funciona sem elas)
    private val pedirNotificacao = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        atualizarAvisoNotificacao()
    }
    private val pedirLocalizacao = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { resultado ->
        val concedida = resultado[Manifest.permission.ACCESS_FINE_LOCATION] == true ||
            resultado[Manifest.permission.ACCESS_COARSE_LOCATION] == true
        if (concedida) {
            ativarLocalizacao()
        } else {
            definirSwitchLocalizacao(false)
            Toast.makeText(this, "Sem permissão de localização: o app segue funcionando sem enviar a posição.", Toast.LENGTH_LONG).show()
            atualizarIndicadorLocalizacao()
        }
    }

    // O servico de monitoramento avisa a tela (sempre na thread principal)
    private val ouvinteServico = object : MonitorService.Ouvinte {
        override fun aoEstadoPush(texto: String) {
            binding.tvPush.text = "Alertas: $texto"
        }

        override fun aoEpisodio(episodio: MonitorService.Episodio?) {
            if (episodio != null && MonitorService.telaVisivel) alerta.exibir(episodio.prazoElapsedMs) else alerta.esconder()
        }

        override fun aoParar() {
            if (isRunning) pararEnvio()
        }
    }

    private val sendRunnable = object : Runnable {
        override fun run() {
            enviarDados()
            if (isRunning) {
                handler.postDelayed(this, SEND_INTERVAL_MS)
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        sensorManager = getSystemService(SENSOR_SERVICE) as SensorManager
        prefs = getSharedPreferences("sensor_app_prefs", Context.MODE_PRIVATE)

        // Carrega o IP/porta salvos anteriormente (ou usa o padrao, na primeira vez). O FlowApp le a URL das preferencias.
        if (!prefs.contains("server_ip") || !prefs.contains("server_port")) {
            prefs.edit()
                .putString("server_ip", prefs.getString("server_ip", DEFAULT_SERVER_IP))
                .putString("server_port", prefs.getString("server_port", DEFAULT_SERVER_PORT))
                .apply()
        }
        serverIp = prefs.getString("server_ip", DEFAULT_SERVER_IP) ?: DEFAULT_SERVER_IP
        serverPort = prefs.getString("server_port", DEFAULT_SERVER_PORT) ?: DEFAULT_SERVER_PORT

        // Marco 3: sessao expirada, falha de retorno e alerta de confirmacao (o canal de push vive no MonitorService)
        app.aoSessaoExpirada = {
            runOnUiThread {
                if (isRunning) pararEnvio()
                atualizarStatus("Sessão expirada: faça login em Configurações", false)
                atualizarTextoServidor()
            }
        }
        app.eventos.aoFalhar = { texto -> atualizarStatus(texto, false) }
        alerta = AlertaController(this, binding.painelAlerta, binding.tvAlerta, binding.btnEstouBem) {
            MonitorService.responder("ESTOU_BEM")
        }
        MonitorService.ouvinte = ouvinteServico
        atualizarTextoServidor()

        // Localizacao: desligada por padrao; so fica ligada se a permissao ainda valer
        if (enviarLocalizacao && !Localizacao.temPermissao(this)) prefs.edit().putBoolean("enviar_localizacao", false).apply()
        definirSwitchLocalizacao(enviarLocalizacao)
        binding.swLocalizacao.setOnCheckedChangeListener { _, marcado ->
            if (ajustandoSwitchLocalizacao) return@setOnCheckedChangeListener
            if (marcado) pedirOuAtivarLocalizacao() else desativarLocalizacao()
        }
        atualizarIndicadorLocalizacao()

        binding.btnStartStop.setOnClickListener {
            try {
                if (isRunning) pararEnvio() else iniciarEnvio()
            } catch (e: Exception) {
                atualizarStatus("Erro: ${e.message}", false)
            }
        }

        binding.btnSocketRede.setOnClickListener {
            abrirDialogoSocket()
        }

        atualizarStatus("Parado", false)
    }

    // ---------------------------------------------------------------------------------------
    // Configuracao de IP/porta ("Socket de Rede")
    // ---------------------------------------------------------------------------------------

    private fun abrirDialogoSocket() {
        val dialogView = layoutInflater.inflate(R.layout.dialog_socket, null)
        val etIp = dialogView.findViewById<EditText>(R.id.etIp)
        val etPorta = dialogView.findViewById<EditText>(R.id.etPorta)
        val etUsuario = dialogView.findViewById<EditText>(R.id.etUsuario)
        val etSenha = dialogView.findViewById<EditText>(R.id.etSenha)

        etIp.setText(serverIp)
        etPorta.setText(serverPort)
        // Usuario e senha ficam em branco: preencha so para (re)fazer o login. A senha nao e guardada.

        AlertDialog.Builder(this)
            .setTitle("Configurações")
            .setView(dialogView)
            .setPositiveButton("Salvar") { _, _ ->
                val novoIp = etIp.text.toString().trim()
                val novaPorta = etPorta.text.toString().trim()
                val usuario = etUsuario.text.toString().trim()
                val senha = etSenha.text.toString()

                if (novoIp.isEmpty() || novaPorta.isEmpty()) {
                    Toast.makeText(this, "Preencha IP e porta", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                if (isRunning) {
                    pararEnvio()
                }

                serverIp = novoIp
                serverPort = novaPorta
                prefs.edit()
                    .putString("server_ip", serverIp)
                    .putString("server_port", serverPort)
                    .apply()
                atualizarTextoServidor()

                if (usuario.isNotEmpty() && senha.isNotEmpty()) {
                    // login e uma chamada de rede bloqueante: fora da thread principal
                    Thread {
                        val r = auth.login(usuario, senha)
                        runOnUiThread {
                            atualizarTextoServidor()
                            Toast.makeText(this, r.mensagem, Toast.LENGTH_LONG).show()
                        }
                    }.start()
                } else {
                    Toast.makeText(this, "Configuração atualizada", Toast.LENGTH_SHORT).show()
                }
            }
            .setNegativeButton("Cancelar", null)
            .show()
    }

    private fun atualizarTextoServidor() {
        val pessoa = auth.userId?.let { "pessoa $it" } ?: "sem login"
        binding.tvServidor.text = "Servidor: $serverIp:$serverPort | $pessoa"
    }

    // ---------------------------------------------------------------------------------------
    // Controle de inicio / parada (apenas em primeiro plano)
    // ---------------------------------------------------------------------------------------

    private fun iniciarEnvio() {
        if (auth.sessao == null) {
            atualizarStatus("Faça login em Configurações", false)
            return
        }
        try {
            registrarTodosSensores()
            isRunning = true
            binding.btnStartStop.text = "Parar"
            atualizarStatus("Coletando...", true)
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)   // vale so com a tela visivel
            val comGps = enviarLocalizacao && Localizacao.temPermissao(this)
            ultimaLocalizacaoEnviadaMs = 0L
            if (comGps) app.localizacao.iniciar()
            MonitorService.iniciar(this, comGps)      // o canal de push vive no servico em primeiro plano
            binding.tvPush.text = "Alertas: conectando"
            handler.post(sendRunnable)
            pedirPermissaoNotificacaoSeNecessario()
            atualizarAvisoNotificacao()
            atualizarIndicadorLocalizacao()
        } catch (e: Exception) {
            atualizarStatus("Erro ao iniciar: ${e.message}", false)
            isRunning = false
        }
    }

    private fun pararEnvio() {
        isRunning = false
        binding.btnStartStop.text = "Iniciar"
        atualizarStatus("Parado", false)
        handler.removeCallbacks(sendRunnable)
        MonitorService.parar(this)
        app.localizacao.parar()
        alerta.esconder()
        binding.tvPush.text = "Alertas: desconectado"
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        sensorManager.unregisterListener(this)
        activeSensors.clear()
        atualizarAvisoNotificacao()
        atualizarIndicadorLocalizacao()
    }

    /**
     * O monitoramento continua com o app em segundo plano ou a tela apagada (o servico em primeiro plano mantem o
     * processo vivo e o aviso vira notificacao). A tela so avisa o servico de que esta visivel ou nao.
     */
    override fun onStart() {
        super.onStart()
        MonitorService.definirTelaVisivel(true)
        if (isRunning) {
            binding.tvPush.text = "Alertas: ${MonitorService.estadoPush}"
            MonitorService.episodio?.let { alerta.exibir(it.prazoElapsedMs) }
        }
        atualizarAvisoNotificacao()
        atualizarIndicadorLocalizacao()
    }

    override fun onStop() {
        super.onStop()
        MonitorService.definirTelaVisivel(false)
        alerta.esconder()
    }

    /** Fechar a tela encerra o monitoramento: nao ha coleta nem servico com o app fechado. */
    override fun onDestroy() {
        if (isRunning) pararEnvio()
        if (MonitorService.ouvinte === ouvinteServico) MonitorService.ouvinte = null
        app.aoSessaoExpirada = null
        app.eventos.aoFalhar = null
        super.onDestroy()
    }

    // ---------------------------------------------------------------------------------------
    // Permissoes (pedidas na hora, com explicacao; o app funciona sem elas)
    // ---------------------------------------------------------------------------------------

    /** Android 13 ou superior: pergunta uma vez, quando o monitoramento comeca, por que quer mostrar notificacoes. */
    private fun pedirPermissaoNotificacaoSeNecessario() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return
        if (prefs.getBoolean("notificacao_pedida", false)) return
        prefs.edit().putBoolean("notificacao_pedida", true).apply()
        AlertDialog.Builder(this)
            .setTitle("Avisos em segundo plano")
            .setMessage(
                "Para avisar você quando a tela estiver apagada ou o app em segundo plano, o app precisa mostrar " +
                    "notificações. Se preferir não permitir, o alarme continua tocando com o app aberto."
            )
            .setPositiveButton("Continuar") { _, _ -> pedirNotificacao.launch(Manifest.permission.POST_NOTIFICATIONS) }
            .setNegativeButton("Agora não") { _, _ -> atualizarAvisoNotificacao() }
            .setOnCancelListener { atualizarAvisoNotificacao() }
            .show()
    }

    private fun atualizarAvisoNotificacao() {
        val desligado = isRunning && !NotificationManagerCompat.from(this).areNotificationsEnabled()
        binding.tvAvisoNotificacao.visibility = if (desligado) View.VISIBLE else View.GONE
    }

    // ---------------------------------------------------------------------------------------
    // Localizacao (dado sensivel: desligada por padrao, pedida na hora, nunca em log)
    // ---------------------------------------------------------------------------------------

    private fun definirSwitchLocalizacao(ligado: Boolean) {
        ajustandoSwitchLocalizacao = true
        binding.swLocalizacao.isChecked = ligado
        ajustandoSwitchLocalizacao = false
    }

    private fun pedirOuAtivarLocalizacao() {
        if (Localizacao.temPermissao(this)) {
            ativarLocalizacao()
            return
        }
        AlertDialog.Builder(this)
            .setTitle("Enviar localização")
            .setMessage(
                "A posição do aparelho será enviada ao servidor, junto com os dados do monitoramento (cifrada), " +
                    "só enquanto o monitoramento estiver ligado. Você pode desligar a qualquer momento nesta tela."
            )
            .setPositiveButton("Continuar") { _, _ ->
                pedirLocalizacao.launch(
                    arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
                )
            }
            .setNegativeButton("Cancelar") { _, _ -> definirSwitchLocalizacao(false) }
            .setOnCancelListener { definirSwitchLocalizacao(false) }
            .show()
    }

    private fun ativarLocalizacao() {
        prefs.edit().putBoolean("enviar_localizacao", true).apply()
        definirSwitchLocalizacao(true)
        if (isRunning) {
            ultimaLocalizacaoEnviadaMs = 0L
            app.localizacao.iniciar()
            MonitorService.atualizarTipo(this, true)
        }
        atualizarIndicadorLocalizacao()
    }

    /** Desligar para a coleta e o envio imediatamente. */
    private fun desativarLocalizacao() {
        prefs.edit().putBoolean("enviar_localizacao", false).apply()
        app.localizacao.parar()
        if (isRunning) MonitorService.atualizarTipo(this, false)
        atualizarIndicadorLocalizacao()
    }

    private fun atualizarIndicadorLocalizacao() {
        binding.tvLocalizacao.text = when {
            !enviarLocalizacao -> "Localização: desligada"
            !isRunning -> "Localização: será enviada quando o monitoramento começar"
            app.localizacao.posicao == null -> "Localização: ligada, aguardando posição"
            app.localizacao.precisa -> "Localização: enviando (precisa)"
            else -> "Localização: enviando (aproximada)"
        }
    }

    // ---------------------------------------------------------------------------------------
    // Sensores
    // ---------------------------------------------------------------------------------------

    private fun registrarTodosSensores() {
        activeSensors.clear()
        accMagUltima = 0f
        accMagMax = 0f
        accAmostras = 0
        accJanelaInicioMs = System.currentTimeMillis()
        val todosSensores = sensorManager.getSensorList(Sensor.TYPE_ALL)
        for (sensor in todosSensores) {
            try {
                // Acelerometro em taxa maior (~50 Hz): precisamos do PICO de uma queda, que dura fracoes de segundo.
                // Nao exige permissao (o limite de 200 Hz do Android 12 so afeta taxas acima disso).
                val taxa = if (sensor.type == Sensor.TYPE_ACCELEROMETER) {
                    SensorManager.SENSOR_DELAY_GAME
                } else {
                    SensorManager.SENSOR_DELAY_NORMAL
                }
                val ok = sensorManager.registerListener(this, sensor, taxa)
                if (ok) {
                    activeSensors.add(sensor)
                }
            } catch (e: Exception) {
                // Sensor exige permissao especial (ex: BODY_SENSORS) ou nao suportado
                // -> ignora esse sensor especifico e continua com os demais
            }
        }
    }

    override fun onSensorChanged(event: SensorEvent) {
        if (event.sensor.type == Sensor.TYPE_ACCELEROMETER && event.values.size >= 3) {
            val m = sqrt(
                event.values[0] * event.values[0] +
                    event.values[1] * event.values[1] +
                    event.values[2] * event.values[2]
            )
            accMagUltima = m
            if (m > accMagMax) accMagMax = m
            accAmostras++
        }
        latestReadings[event.sensor.type] = SensorReading(
            sensorName = event.sensor.name,
            values = event.values.toList(),
            timestamp = event.timestamp,
            wallClockMillis = System.currentTimeMillis()
        )
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {
        // Nao utilizado
    }

    // ---------------------------------------------------------------------------------------
    // Envio para o servidor
    // ---------------------------------------------------------------------------------------

    private fun enviarDados() {
        val json = try {
            montarJson()
        } catch (e: Exception) {
            atualizarStatus("Erro ao montar dados: ${e.message}", false)
            return
        }
        atualizarIndicadorLocalizacao()

        // Cifra o JSON inteiro (Marco 2) antes de enviar. O servidor decifra
        // o envelope e recupera o payload original (ver CryptoUtils.kt).
        val (nonceB64, cipherB64) = CryptoUtils.encrypt(json.toString())
        val envelope = JSONObject().apply {
            put("nonce", nonceB64)
            put("ciphertext", cipherB64)
        }

        val mediaType = "application/json; charset=utf-8".toMediaType()
        val body = envelope.toString().toRequestBody(mediaType)

        val request = Request.Builder()
            .url("$baseUrl/dados")
            .post(body)
            .build()

        FlowHttp.cliente.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnUiThread {
                    atualizarStatus("Servidor indisponivel (tentando novamente)", false)
                }
            }

            override fun onResponse(call: Call, response: okhttp3.Response) {
                response.close()
                runOnUiThread {
                    if (response.isSuccessful) {
                        val hora = SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date())
                        atualizarStatus("Conectado", true)
                        binding.tvUltimoEnvio.text = "Ultimo envio: $hora"
                    } else if (response.code == 401 || response.code == 403) {
                        atualizarStatus("Sem autorização (${response.code}): faça login em Configurações", false)
                    } else {
                        atualizarStatus("Erro do servidor (codigo ${response.code})", false)
                    }
                }
            }
        })
    }

    private fun montarJson(): JSONObject {
        val root = JSONObject()
        root.put("schemaVersion", SCHEMA_VERSION)
        // Marco 3: identifica o usuario (idoso) dono deste envio, para o
        // servidor conseguir juntar com os dados vindos do wokwi da mesma
        // pessoa. "source" identifica de qual fonte este payload veio.
        root.put("user_id", auth.userId ?: -1)
        root.put("source", "app")
        root.put("device_id", deviceId)
        root.put("device_model", android.os.Build.MODEL)
        root.put("timestamp_envio", System.currentTimeMillis())

        val sensoresJson = JSONObject()
        for ((tipo, leitura) in latestReadings) {
            val sensorJson = JSONObject()
            sensorJson.put("nome", leitura.sensorName)

            val valoresJson = JSONArray()
            for (v in leitura.values) {
                valoresJson.put(v.toDouble())
            }
            sensorJson.put("valores", valoresJson)

            sensorJson.put("timestamp_leitura", leitura.timestamp)
            sensoresJson.put(tipo.toString(), sensorJson)
        }
        root.put("sensores", sensoresJson)

        // Fluxo priorizado do Marco 2: evento LeituraAmbiente (luminosidade + CCT).
        // So inclui a chave se houver leitura valida e recente de ambos os sensores;
        // caso contrario, a chave fica ausente e o servidor simplesmente ignora
        // este ciclo para fins da regra de luz inadequada.
        montarLeituraAmbiente()?.let { root.put("leitura_ambiente", it) }

        // Marco 3: bloco do acelerometro (entrada da regra de queda no servidor)
        montarAcelerometro()?.let { root.put("acelerometro", it) }

        // Localizacao (so com o interruptor ligado): vai no lote que ja existe, uma vez por posicao nova
        if (enviarLocalizacao) montarLocalizacao()?.let { root.put("localizacao", it) }

        return root
    }

    /**
     * Extrai o evento LeituraAmbiente (contrato definido na Atividade 02) a
     * partir das ultimas leituras dos sensores de luz (Sensor.TYPE_LIGHT) e
     * de temperatura de cor (sensor especifico do fabricante, identificado
     * pelo nome contendo "CCT").
     *
     * Validacao de faixa fisica plausivel e feita AQUI, no dispositivo, antes
     * do envio (responsabilidade definida na Atividade 02, item 10): evita
     * gastar banda/bateria enviando ruido de sensor a cada 10s.
     */
    private fun montarLeituraAmbiente(): JSONObject? {
        val leituraLuz = latestReadings[Sensor.TYPE_LIGHT] ?: return null
        val leituraCct = latestReadings.values.firstOrNull {
            it.sensorName.contains("CCT", ignoreCase = true)
        } ?: return null

        val luminosidade = leituraLuz.values.getOrNull(0) ?: return null
        val cct = leituraCct.values.getOrNull(0) ?: return null

        if (luminosidade < 0f) return null
        if (cct < CCT_MIN_VALIDO || cct > CCT_MAX_VALIDO) return null

        // event_time = instante da leitura no dispositivo (nao o de chegada ao servidor)
        val eventTimeMillis = maxOf(leituraLuz.wallClockMillis, leituraCct.wallClockMillis)
        val eventTimeIso = formatarIso8601(eventTimeMillis)

        ambientSeqNum += 1

        return JSONObject().apply {
            put("schemaVersion", SCHEMA_VERSION_LEITURA_AMBIENTE)
            put("device_id", deviceId)
            put("event_time", eventTimeIso)
            put("luminosidade", luminosidade.toDouble())
            put("cct", cct.toDouble())
            put("seq_num", ambientSeqNum)
        }
    }

    /**
     * Bloco "acelerometro" (schemaVersion 3): magnitude da ultima amostra e o MAXIMO da janela desde o ultimo envio
     * (o pico de uma queda dura fracoes de segundo; uma amostra por segundo o perderia). Zera a janela.
     * Retorna null se nao houve amostra na janela.
     */
    private fun montarAcelerometro(): JSONObject? {
        if (accAmostras == 0) return null
        val agora = System.currentTimeMillis()
        val max = accMagMax.coerceAtMost(ACELEROMETRO_MAX_PLAUSIVEL)
        val ultima = accMagUltima.coerceAtMost(max)
        val bloco = JSONObject().apply {
            put("magnitude", ultima.toDouble())
            put("magnitude_max_janela", max.toDouble())
            put("amostras", accAmostras)
            put("janela_ms", maxOf(1L, agora - accJanelaInicioMs))
        }
        accMagMax = 0f
        accAmostras = 0
        accJanelaInicioMs = agora
        return bloco
    }

    /**
     * Bloco "localizacao" (docs/schemas.md, 4.3) a partir da ultima posicao, enviado UMA vez por posicao nova (a coleta
     * e a cada 30 s; o lote sai a cada 1 s). So descarta o absurdo ([montarBlocoLocalizacao]); o servidor valida.
     * Posicao sem precisao informada nao e enviada. Latitude e longitude nunca vao para log.
     */
    private fun montarLocalizacao(): JSONObject? {
        val local = app.localizacao.posicao ?: return null
        if (local.time <= ultimaLocalizacaoEnviadaMs) return null
        if (!local.hasAccuracy()) return null
        val bloco = montarBlocoLocalizacao(
            latitude = local.latitude,
            longitude = local.longitude,
            precisaoM = local.accuracy.toDouble(),
            instanteMs = local.time,
            agoraMs = System.currentTimeMillis()
        ) ?: return null
        ultimaLocalizacaoEnviadaMs = local.time
        return bloco
    }

    // ---------------------------------------------------------------------------------------
    // UI
    // ---------------------------------------------------------------------------------------

    private fun atualizarStatus(texto: String, conectado: Boolean) {
        binding.tvStatus.text = texto
    }
}
