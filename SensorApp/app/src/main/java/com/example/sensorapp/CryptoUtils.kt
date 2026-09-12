package com.example.sensorapp

import android.util.Base64
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Criptografia do transporte (Marco 2).
 *
 * Mecanismo: AES-256-GCM com chave simetrica pre-compartilhada, usando apenas
 * `javax.crypto` (ja nativo do Android - nao exige nenhuma biblioteca extra).
 *
 * E o mesmo mecanismo usado no servidor (ver SensorServer/crypto_utils.py,
 * que usa `cryptography.hazmat.primitives.ciphers.aead.AESGCM`). Os dois
 * lados produzem/consomem o mesmo formato: nonce de 12 bytes + ciphertext
 * com a tag de autenticacao de 16 bytes anexada ao final (comportamento
 * padrao de Cipher.doFinal() em modo GCM e de AESGCM.encrypt() em Python).
 *
 * Por que este mecanismo: cada lote (a cada 10s) e cifrado e autenticado de
 * forma independente, sem necessidade de handshake ou sessao continua -
 * adequado a um protocolo sem estado como o HTTP simples ja usado aqui.
 *
 * Limitacao assumida (mesma do servidor): a chave e fixa e embutida no
 * codigo, sem negociacao de chaves. Aceitavel apenas nesta fase de
 * prototipo, dentro da rede local.
 */
object CryptoUtils {

    // Precisa ser EXATAMENTE a mesma constante usada em
    // SensorServer/crypto_utils.py (SHARED_KEY_B64).
    //
    // NOTA (nivel academico): chave estatica e hardcoded de proposito, apenas
    // para viabilizar a demonstracao do Marco 2 dentro do prazo da disciplina.
    // Em producao, ela NAO deveria ser fixa nem compartilhada por todos os
    // dispositivos - o correto seria cada celular gerar/receber sua propria
    // chave a partir de um pareamento inicial com o servidor (ex.: troca de
    // chaves via Diffie-Hellman/ECDH no primeiro cadastro, ou uma chave por
    // dispositivo armazenada de forma segura no Android Keystore), permitindo
    // revogar/trocar a chave de um idoso especifico sem afetar os demais.
    private const val SHARED_KEY_B64 = "nL+mtDjw5+64sqP0berkvBun36w9EFNxkyPjR+UHSik="

    private const val NONCE_SIZE_BYTES = 12
    private const val GCM_TAG_BITS = 128

    private val secretKey: SecretKeySpec by lazy {
        val keyBytes = Base64.decode(SHARED_KEY_B64, Base64.NO_WRAP)
        require(keyBytes.size == 32) { "Chave precisa ter 32 bytes (AES-256)." }
        SecretKeySpec(keyBytes, "AES")
    }

    /**
     * Cifra [plainText] e retorna um par (nonce em base64, ciphertext em base64),
     * pronto para ser colocado no envelope {"nonce": ..., "ciphertext": ...}
     * enviado ao servidor.
     */
    fun encrypt(plainText: String): Pair<String, String> {
        val nonce = ByteArray(NONCE_SIZE_BYTES)
        SecureRandom().nextBytes(nonce)

        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        val spec = GCMParameterSpec(GCM_TAG_BITS, nonce)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey, spec)

        val cipherBytes = cipher.doFinal(plainText.toByteArray(Charsets.UTF_8))

        val nonceB64 = Base64.encodeToString(nonce, Base64.NO_WRAP)
        val cipherB64 = Base64.encodeToString(cipherBytes, Base64.NO_WRAP)
        return Pair(nonceB64, cipherB64)
    }
}
