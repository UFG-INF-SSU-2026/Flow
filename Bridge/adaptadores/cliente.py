"""
Cliente da API do bridge, usado pelos adaptadores. Fala so HTTPS, confiando so na CA do projeto (verify=<ca.crt>, nunca False).

O token de sessao local vem do login e fica so em memoria. O adaptador nao tem credencial do servidor: o login que o
dispositivo mandou e entregue ao bridge e esquecido.
"""
import os
from pathlib import Path

import requests

PASTA = Path(__file__).resolve().parent.parent
URL_PADRAO = "https://127.0.0.1:5443"
CA_PADRAO = PASTA.parent / "SensorServer" / "certs" / "ca.crt"
TIMEOUT_S = 5


class ErroApi(Exception):
    def __init__(self, status: int, codigo: str | None = None):
        super().__init__(f"API respondeu {status}" + (f" ({codigo})" if codigo else ""))
        self.status, self.codigo = status, codigo


def url_e_ca() -> tuple[str, str]:
    """URL da API e CA a partir do ambiente (FLOW_BRIDGE_URL, FLOW_TLS_CA); caminho relativo vale a partir de Bridge/."""
    url = os.environ.get("FLOW_BRIDGE_URL", URL_PADRAO).rstrip("/")
    ca = Path(os.environ.get("FLOW_TLS_CA") or CA_PADRAO).expanduser()
    return url, str(ca if ca.is_absolute() else PASTA / ca)


class ApiBridge:
    def __init__(self, base: str, ca: str):
        if not base.lower().startswith("https://"):
            raise ValueError(f"a API do bridge so fala HTTPS; recebido: {base}")
        self.base, self.ca = base, ca
        self.token: str | None = None
        self.device_id: str | None = None

    def _chamar(self, metodo: str, caminho: str, espera_extra: float = 0, **kw):
        cab = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        r = requests.request(metodo, f"{self.base}{caminho}", headers=cab, timeout=TIMEOUT_S + espera_extra,
                             verify=self.ca, **kw)
        if r.status_code >= 300:
            try:
                codigo = r.json().get("codigo")
            except ValueError:
                codigo = None
            raise ErroApi(r.status_code, codigo)
        return r

    def login(self, linha: dict) -> None:
        """Entrega ao bridge o login que o dispositivo mandou (identificador, segredo, deviceId)."""
        corpo = {k: linha.get(k) for k in ("identificador", "segredo", "deviceId")}
        r = self._chamar("POST", "/v1/login", json=corpo)
        self.token, self.device_id = r.json()["token"], r.json()["deviceId"]

    def esquecer(self) -> None:
        self.token = None

    def evento(self, evento: dict) -> None:
        self._chamar("POST", f"/v1/dispositivos/{self.device_id}/eventos", json=evento)

    def comandos(self, espera: float = 0) -> list[str]:
        r = self._chamar("GET", f"/v1/dispositivos/{self.device_id}/comandos", espera_extra=espera,
                         params={"espera": espera})
        return r.json()["comandos"]
