package com.example.sensorapp

/**
 * Um dispositivo de aviso do celular (vibracao, lanterna, fala, estado visual).
 *
 * [ligar] e [desligar] podem ser chamados em qualquer ordem e repetidos (idempotentes). Podem lancar excecao: quem
 * chama ([Atuadores]) isola a falha, para que um atuador indisponivel nao impeca os demais.
 */
interface Atuador {
    fun ligar()
    fun desligar()
}

/**
 * Liga e desliga todos os atuadores juntos, na ordem da lista. A falha de um (excecao) vai para [aoFalhar] e nao
 * impede os seguintes nem chega a quem chamou. Sem estado proprio: cada atuador e idempotente.
 *
 * Sem android.* de proposito: roda na JVM (ver AtuadoresTest).
 */
class Atuadores(
    private val atuadores: List<Atuador>,
    private val aoFalhar: (Atuador, Exception) -> Unit = { _, _ -> }
) {
    fun ligar() = percorrer { it.ligar() }

    fun desligar() = percorrer { it.desligar() }

    private fun percorrer(operacao: (Atuador) -> Unit) {
        for (atuador in atuadores) {
            try {
                operacao(atuador)
            } catch (e: Exception) {
                // so Exception: erro grave da JVM (Error) nao deve ser engolido
                try {
                    aoFalhar(atuador, e)
                } catch (e2: Exception) {
                    // o aviso da falha tambem nao pode impedir os demais
                }
            }
        }
    }
}
