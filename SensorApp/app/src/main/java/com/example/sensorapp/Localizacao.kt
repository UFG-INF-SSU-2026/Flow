package com.example.sensorapp

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.Looper
import androidx.core.content.ContextCompat

/**
 * Coleta de localizacao pelo LocationManager do Android (sem Google Play Services).
 *
 * - Usa o que o aparelho permitir: GPS + rede com a permissao precisa; so rede com a aproximada.
 * - Atualizacoes ESPACADAS (a cada [INTERVALO_MS]), so enquanto o monitoramento estiver ligado (iniciar/parar).
 *   Sem ACCESS_BACKGROUND_LOCATION: com o app fechado nao ha coleta.
 * - A ultima posicao fica so em memoria. Latitude e longitude NUNCA vao para log.
 *
 * Todos os metodos sao chamados na thread principal.
 */
class Localizacao(context: Context) {
    private val ctx = context.applicationContext
    private val lm = ctx.getSystemService(Context.LOCATION_SERVICE) as LocationManager

    /** Ultima posicao recebida (so em memoria). */
    @Volatile
    var posicao: Location? = null
        private set

    private var ativo = false

    private val ouvinte = object : LocationListener {
        override fun onLocationChanged(local: Location) {
            val atual = posicao
            if (atual == null || local.time >= atual.time) posicao = local
        }

        override fun onProviderEnabled(provider: String) {}
        override fun onProviderDisabled(provider: String) {}

        @Deprecated("Deprecated in Java")
        override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
    }

    /** true com a permissao precisa; false com so a aproximada (ou nenhuma). */
    val precisa: Boolean get() = temPermissaoPrecisa(ctx)

    fun iniciar() {
        if (ativo) return
        if (!temPermissao(ctx)) return
        val provedores = mutableListOf<String>()
        if (temPermissaoPrecisa(ctx)) provedores.add(LocationManager.GPS_PROVIDER)
        provedores.add(LocationManager.NETWORK_PROVIDER)
        for (p in provedores) {
            try {
                if (!lm.isProviderEnabled(p)) continue
                lm.requestLocationUpdates(p, INTERVALO_MS, DISTANCIA_MIN_M, ouvinte, Looper.getMainLooper())
                ativo = true
                lm.getLastKnownLocation(p)?.let { ouvinte.onLocationChanged(it) }   // a idade e conferida no envio
            } catch (e: SecurityException) {
                // permissao revogada no meio do caminho: segue sem esse provedor
            } catch (e: IllegalArgumentException) {
                // provedor inexistente neste aparelho
            }
        }
    }

    fun parar() {
        try {
            lm.removeUpdates(ouvinte)
        } catch (e: SecurityException) {
            // nada a remover
        }
        ativo = false
        posicao = null
    }

    companion object {
        /** Intervalo entre atualizacoes (30 s). */
        const val INTERVALO_MS = 30_000L

        /** 0 = sem filtro de distancia: quem esta parado em casa continua enviando posicao recente. */
        const val DISTANCIA_MIN_M = 0f

        fun temPermissaoPrecisa(ctx: Context): Boolean =
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED

        fun temPermissao(ctx: Context): Boolean =
            temPermissaoPrecisa(ctx) ||
                ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
    }
}
