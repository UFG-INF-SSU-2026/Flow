package com.example.sensorapp

import android.app.AlertDialog
import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.example.sensorapp.databinding.ActivityMainBinding
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

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
     * ID padrao do usuario (idoso), usado apenas na PRIMEIRA vez que o app
     * abre, antes de o usuario configurar o valor real pelo botao
     * "Configurações". Precisa ser o MESMO numero inteiro configurado no
     * wokwi correspondente (ver WokwiBridge/bridge.py, WOKWI_USER_ID), para
     * que o servidor entenda que os dados vem da mesma pessoa.
     */
    private val DEFAULT_USER_ID: Int = 1

    /**
     * Faixa fisica plausivel para o evento LeituraAmbiente (Marco 2).
     * Leituras fora dessa faixa sao descartadas ANTES do envio (validacao no
     * dispositivo, conforme decidido na Atividade 02), para nao gastar banda/
     * bateria enviando ruido de sensor.
     */
    private val CCT_MIN_VALIDO: Float = 1000f
    private val CCT_MAX_VALIDO: Float = 12000f

    // =========================================================================================

    private lateinit var binding: ActivityMainBinding
    private lateinit var sensorManager: SensorManager
    private lateinit var prefs: android.content.SharedPreferences
    private val handler = Handler(Looper.getMainLooper())
    private val httpClient = OkHttpClient()

    private var isRunning = false

    // IP e porta atualmente configurados (carregados do SharedPreferences em onCreate)
    private var serverIp: String = DEFAULT_SERVER_IP
    private var serverPort: String = DEFAULT_SERVER_PORT

    // ID do usuario (idoso) atualmente configurado (carregado do SharedPreferences em onCreate).
    // Enviado em TODO envio (app e wokwi) para que o servidor saiba que os dados
    // pertencem a mesma pessoa.
    private var userId: Int = DEFAULT_USER_ID

    private val serverUrl: String
        get() = "http://$serverIp:$serverPort/dados"

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

    private val deviceId: String by lazy {
        Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: "desconhecido"
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

        // Carrega o IP/porta salvos anteriormente (ou usa o padrao, na primeira vez)
        serverIp = prefs.getString("server_ip", DEFAULT_SERVER_IP) ?: DEFAULT_SERVER_IP
        serverPort = prefs.getString("server_port", DEFAULT_SERVER_PORT) ?: DEFAULT_SERVER_PORT
        userId = prefs.getInt("user_id", DEFAULT_USER_ID)
        atualizarTextoServidor()

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
        val etUserId = dialogView.findViewById<EditText>(R.id.etUserId)

        etIp.setText(serverIp)
        etPorta.setText(serverPort)
        etUserId.setText(userId.toString())

        AlertDialog.Builder(this)
            .setTitle("Configurações")
            .setView(dialogView)
            .setPositiveButton("Salvar") { _, _ ->
                val novoIp = etIp.text.toString().trim()
                val novaPorta = etPorta.text.toString().trim()
                val novoUserIdTexto = etUserId.text.toString().trim()

                if (novoIp.isEmpty() || novaPorta.isEmpty() || novoUserIdTexto.isEmpty()) {
                    Toast.makeText(this, "Preencha IP, porta e ID do usuário", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }

                val novoUserId = novoUserIdTexto.toIntOrNull()
                if (novoUserId == null) {
                    Toast.makeText(this, "ID do usuário precisa ser um número inteiro", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }

                // Se o app estiver enviando dados no momento, para antes de trocar a configuracao
                if (isRunning) {
                    pararEnvio()
                }

                serverIp = novoIp
                serverPort = novaPorta
                userId = novoUserId

                prefs.edit()
                    .putString("server_ip", serverIp)
                    .putString("server_port", serverPort)
                    .putInt("user_id", userId)
                    .apply()

                atualizarTextoServidor()
                Toast.makeText(this, "Configuração atualizada", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancelar", null)
            .show()
    }

    private fun atualizarTextoServidor() {
        binding.tvServidor.text = "Servidor: $serverIp:$serverPort | ID: $userId"
    }

    // ---------------------------------------------------------------------------------------
    // Controle de inicio / parada (apenas em primeiro plano)
    // ---------------------------------------------------------------------------------------

    private fun iniciarEnvio() {
        try {
            registrarTodosSensores()
            isRunning = true
            binding.btnStartStop.text = "Parar"
            atualizarStatus("Coletando...", true)
            handler.post(sendRunnable)
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
        sensorManager.unregisterListener(this)
        activeSensors.clear()
    }

    /** Para automaticamente o envio se o app sair de primeiro plano. */
    override fun onPause() {
        super.onPause()
        if (isRunning) {
            pararEnvio()
        }
    }

    // ---------------------------------------------------------------------------------------
    // Sensores
    // ---------------------------------------------------------------------------------------

    private fun registrarTodosSensores() {
        activeSensors.clear()
        val todosSensores = sensorManager.getSensorList(Sensor.TYPE_ALL)
        for (sensor in todosSensores) {
            try {
                val ok = sensorManager.registerListener(
                    this,
                    sensor,
                    SensorManager.SENSOR_DELAY_NORMAL
                )
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
            .url(serverUrl)
            .post(body)
            .build()

        httpClient.newCall(request).enqueue(object : Callback {
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
                    } else {
                        atualizarStatus("Erro do servidor (codigo ${response.code})", false)
                    }
                }
            }
        })
    }

    private fun montarJson(): JSONObject {
        val root = JSONObject()
        // Marco 3: identifica o usuario (idoso) dono deste envio, para o
        // servidor conseguir juntar com os dados vindos do wokwi da mesma
        // pessoa. "source" identifica de qual fonte este payload veio.
        root.put("user_id", userId)
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
            put("device_id", deviceId)
            put("event_time", eventTimeIso)
            put("luminosidade", luminosidade.toDouble())
            put("cct", cct.toDouble())
            put("seq_num", ambientSeqNum)
        }
    }

    /** Formata um timestamp em milissegundos como ISO 8601 com offset (ex.: 2026-09-11T22:00:20.000-03:00). */
    private fun formatarIso8601(millis: Long): String {
        val formato = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US)
        return formato.format(Date(millis))
    }

    // ---------------------------------------------------------------------------------------
    // UI
    // ---------------------------------------------------------------------------------------

    private fun atualizarStatus(texto: String, conectado: Boolean) {
        binding.tvStatus.text = texto
    }
}
