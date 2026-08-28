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

    /** Intervalo, em milissegundos, entre cada envio de dados para o servidor. */
    private val SEND_INTERVAL_MS: Long = 10_000L

    /**
     * IP e porta padrao do servidor, usados apenas na PRIMEIRA vez que o app abre
     * (antes de o usuario configurar algo pelo botao "Socket de Rede").
     * Depois disso, o app passa a usar o valor salvo no proprio celular.
     */
    private val DEFAULT_SERVER_IP: String = "192.168.0.10"
    private val DEFAULT_SERVER_PORT: String = "5000"

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

    private val serverUrl: String
        get() = "http://$serverIp:$serverPort/dados"

    // Guarda a leitura mais recente de cada sensor (chave = tipo do sensor)
    private val latestReadings = mutableMapOf<Int, SensorReading>()

    // Lista de sensores que conseguimos registrar com sucesso
    private val activeSensors = mutableListOf<Sensor>()

    private data class SensorReading(
        val sensorName: String,
        val values: List<Float>,
        val timestamp: Long
    )

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

        etIp.setText(serverIp)
        etPorta.setText(serverPort)

        AlertDialog.Builder(this)
            .setTitle("Socket de Rede")
            .setView(dialogView)
            .setPositiveButton("Salvar") { _, _ ->
                val novoIp = etIp.text.toString().trim()
                val novaPorta = etPorta.text.toString().trim()

                if (novoIp.isEmpty() || novaPorta.isEmpty()) {
                    Toast.makeText(this, "Preencha IP e porta", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }

                // Se o app estiver enviando dados no momento, para antes de trocar o endereco
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
                Toast.makeText(this, "Servidor atualizado", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancelar", null)
            .show()
    }

    private fun atualizarTextoServidor() {
        binding.tvServidor.text = "Servidor: $serverIp:$serverPort"
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
            timestamp = event.timestamp
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

        val mediaType = "application/json; charset=utf-8".toMediaType()
        val body = json.toString().toRequestBody(mediaType)

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

        return root
    }

    // ---------------------------------------------------------------------------------------
    // UI
    // ---------------------------------------------------------------------------------------

    private fun atualizarStatus(texto: String, conectado: Boolean) {
        binding.tvStatus.text = texto
    }
}
