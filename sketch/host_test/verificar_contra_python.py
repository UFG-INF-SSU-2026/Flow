"""Confere o firmware (harness) contra o servidor Python de verdade: formato do envelope nas DUAS direcoes e o CSR que o firmware gera.

Uso (em sketch/host_test, depois de compilar o harness; ver README.md):
    python verificar_contra_python.py

- envelope: o Python (borda/crypto_utils.py, com a chave de teste 0x00..0x1f) cifra um texto e o firmware o decifra; o firmware cifra
  com nonce fixo e o Python decifra, e o texto cifrado e IGUAL ao que o Python produz com o mesmo nonce;
- CSR: o CSR gerado no firmware passa por borda/emissao.py (a mesma validacao da rota POST /auth/certificado) e vira certificado.
Termina com codigo != 0 se algo divergir. Precisa de SensorServer/requirements.txt instalado.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent
SERVIDOR = AQUI.parents[1] / "SensorServer"
sys.path.insert(0, str(SERVIDOR))
sys.path.insert(0, str(SERVIDOR / "tests"))
os.environ.setdefault("FLOW_SEM_DOTENV", "1")

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from borda import crypto_utils  # noqa: E402

CHAVE = bytes(range(32))
NONCE = bytes(range(12))
falhas = 0


def confere(condicao, mensagem):
    global falhas
    print(("PASS " if condicao else "FAIL ") + mensagem)
    falhas += 0 if condicao else 1


def main() -> int:
    exe = AQUI / "harness"
    if not exe.is_file():
        print("compile o harness antes (ver README.md)")
        return 2
    crypto_utils._aesgcm = AESGCM(CHAVE)                   # a chave de teste do harness, no mesmo codigo do servidor
    with tempfile.TemporaryDirectory() as pasta:
        env = {**os.environ, "FLOW_VETORES_DIR": pasta}
        texto = '{"msg":"confirmação","n":1}'
        n, c = crypto_utils.encrypt_payload(texto)
        (Path(pasta) / "flow_py_env.json").write_text(json.dumps({"nonce": n, "ciphertext": c}))
        r = subprocess.run([str(exe)], cwd=AQUI, env=env, capture_output=True, text=True)
        confere(r.returncode == 0, "o harness do firmware passou (sem falhas)")
        confere((Path(pasta) / "flow_py_resultado.txt").read_text(encoding="utf-8") == texto, "firmware decifra o envelope que o servidor cifrou")

        do_firmware = json.loads((Path(pasta) / "flow_env.json").read_text())
        confere(crypto_utils.decrypt_payload(do_firmware["nonce"], do_firmware["ciphertext"]) == '{"x":1}', "servidor decifra o envelope que o firmware cifrou")
        esperado = AESGCM(CHAVE).encrypt(NONCE, b'{"x":1}', None)
        confere(base64.b64decode(do_firmware["ciphertext"]) == esperado and base64.b64decode(do_firmware["nonce"]) == NONCE,
                "mesmo nonce, mesmo ciphertext+tag que o Python (AES-256-GCM, sem AAD, tag no fim)")

        csr_pem = (Path(pasta) / "flow_csr.pem").read_text()
        from borda import emissao
        from _apoio import criar_ca_dispositivos
        with tempfile.TemporaryDirectory() as ca_dir:
            ca = criar_ca_dispositivos(Path(ca_dir))
            os.environ["FLOW_CA_DISPOSITIVOS_CERT"], os.environ["FLOW_CA_DISPOSITIVOS_KEY"] = str(ca.cert), str(ca.chave)
            try:
                emitido = emissao.emitir_certificado(csr_pem, "conta-1", "esp32-decisao-imobilidade-01", datetime.now(timezone.utc))
                confere("BEGIN CERTIFICATE" in emitido.certificado_pem, "o CSR do firmware (EC P-256, ECDSA-SHA256) e aceito por borda/emissao.py e vira certificado")
            except Exception as e:                           # noqa: BLE001
                confere(False, f"o CSR do firmware foi recusado pelo servidor: {e}")
    print("\nOK" if not falhas else f"\nFALHAS: {falhas}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
