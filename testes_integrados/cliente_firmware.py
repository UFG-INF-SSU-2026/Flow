"""Cliente Python que faz o papel do firmware: envelope AES, login, CSR, emissao e rotas com certificado (pelo bridge)."""
import json
import os
import tempfile

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from borda.crypto_utils import decrypt_payload, encrypt_payload


def envelope(obj) -> dict:
    n, c = encrypt_payload(json.dumps(obj))
    return {"nonce": n, "ciphertext": c}


def abrir(resp) -> dict:
    j = resp.json()
    return json.loads(decrypt_payload(j["nonce"], j["ciphertext"]))


class ClienteFirmware:
    def __init__(self, base: str, ca: str, identificador: str, segredo: str, device_id: str, pasta):
        self.base, self.ca, self.pasta = base, ca, pasta
        self.identificador, self.segredo, self.device_id = identificador, segredo, device_id
        self.access = self.refresh = self.user_id = self.certificado = None
        self.chave = ec.generate_private_key(ec.SECP256R1())

    def _req(self, metodo, rota, corpo=None, com_certificado=True, headers=None, **kw):
        cab = {"Authorization": f"Bearer {self.access}"} if self.access else {}
        cab.update(headers or {})
        cert = self.arquivos_certificado() if (com_certificado and self.certificado) else None
        return requests.request(metodo, self.base + rota, json=None if corpo is None else envelope(corpo), headers=cab,
                                verify=self.ca, cert=cert, timeout=10, **kw)

    def arquivos_certificado(self):
        crt = os.path.join(self.pasta, f"{self.device_id}.crt")
        chv = os.path.join(self.pasta, f"{self.device_id}.key")
        with open(crt, "w") as f:
            f.write(self.certificado)
        with open(chv, "wb") as f:
            f.write(self.chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        return crt, chv

    def login(self):
        r = self._req("POST", "/auth/login", {"schemaVersion": 3, "identificador": self.identificador, "segredo": self.segredo,
                                              "fonte": "wokwi", "device_id": self.device_id}, com_certificado=False)
        assert r.status_code == 200, r.text
        t = abrir(r)
        self.access, self.refresh, self.user_id = t["access_token"], t["refresh_token"], t["user_ids"][0]
        return t

    def emitir(self):
        csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.device_id)]))
               .sign(self.chave, hashes.SHA256()))
        r = self._req("POST", "/auth/certificado", {"schemaVersion": 1, "csr": csr.public_bytes(serialization.Encoding.PEM).decode()},
                      com_certificado=False)
        assert r.status_code == 200, r.text
        self.certificado = abrir(r)["certificado"]

    def telemetria(self, **extra):
        ev = {"schemaVersion": 3, "eventType": "imobilidade.leitura", "deviceId": self.device_id, "entityId": "idoso",
              "eventTimeMs": 1000, "sequence": 1, "value": 1.0, "valueMax": 1.5, "unit": "m/s2", "state": "NORMAL",
              "user_id": self.user_id, "source": "wokwi", **extra}
        return self._req("POST", "/dados", ev)
