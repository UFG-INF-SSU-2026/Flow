"""
Configuracao do bridge: Bridge/.env (modelo: .env.example) ou variaveis de ambiente. Variavel ja definida no terminal VENCE o
arquivo. Caminho relativo vale a partir de Bridge/. Este modulo nao abre rede nem arquivo de certificado: so le e valida texto.

  FLOW_SERVIDOR            URL base do servidor, https:// (padrao https://127.0.0.1:5000). Use o IP e nao "localhost": no
                           Windows o "localhost" tenta IPv6 primeiro e cada conexao nova espera 1-2 s.
  FLOW_TLS_CA              certificado da CA do projeto (padrao ../SensorServer/certs/ca.crt): a unica confianca do bridge
                           para falar com o servidor. A verificacao de certificado e de nome nunca e desligada.
  FLOW_BRIDGE_HOST         onde a API do bridge escuta (padrao 127.0.0.1)
  FLOW_BRIDGE_PORT         porta da API do bridge (padrao 5443)
  FLOW_BRIDGE_API_CERT     certificado da API do bridge, emitido pela CA (padrao ../SensorServer/certs/bridge.crt)
  FLOW_BRIDGE_API_KEY      chave privada desse certificado (padrao ../SensorServer/certs/bridge.key)
  FLOW_BRIDGE_MAX_SIMULACOES  teto de dispositivos logados ao mesmo tempo (padrao 8)

O bridge nao tem conta nem senha: o segredo de cada dispositivo mora no firmware e so passa por aqui no login.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PASTA = Path(__file__).resolve().parent
DOTENV_ARQUIVO = PASTA / ".env"
_CERTS = PASTA.parent / "SensorServer" / "certs"


def carregar_dotenv(caminho: Path) -> bool:
    """Carrega `caminho` (um .env) para os.environ SEM sobrescrever o que ja esta definido. Retorna True se o arquivo foi
    lido; arquivo ausente nao e erro. utf-8-sig tolera BOM (Bloco de Notas); interpolate=False deixa `$` literal;
    FLOW_SEM_DOTENV=1 desliga a leitura (usado pelos testes)."""
    if os.environ.get("FLOW_SEM_DOTENV") == "1" or not caminho.is_file():
        return False
    return load_dotenv(caminho, override=False, interpolate=False, encoding="utf-8-sig")


@dataclass(frozen=True)
class Config:
    servidor: str                       # https://..., sem "/" no fim
    ca: str                             # caminho do ca.crt
    host: str = "127.0.0.1"
    porta: int = 5443
    api_cert: str = ""
    api_chave: str = ""
    max_simulacoes: int = 8


def _caminho(valor: str | None, padrao: Path) -> str:
    p = Path(valor).expanduser() if valor else padrao
    return str(p if p.is_absolute() else PASTA / p)


def _inteiro(nome: str, padrao: int, minimo: int, maximo: int) -> int:
    texto = os.environ.get(nome, "").strip()
    if not texto:
        return padrao
    try:
        n = int(texto)
    except ValueError:
        raise SystemExit(f"{nome} precisa ser um numero inteiro; recebido: {texto!r}") from None
    if not minimo <= n <= maximo:
        raise SystemExit(f"{nome} precisa estar entre {minimo} e {maximo}; recebido: {n}")
    return n


def ler_config() -> Config:
    """Le o ambiente na hora do uso (depois de carregar_dotenv)."""
    return Config(
        servidor=os.environ.get("FLOW_SERVIDOR", "https://127.0.0.1:5000").rstrip("/"),
        ca=_caminho(os.environ.get("FLOW_TLS_CA"), _CERTS / "ca.crt"),
        host=os.environ.get("FLOW_BRIDGE_HOST", "127.0.0.1"),
        porta=_inteiro("FLOW_BRIDGE_PORT", 5443, 1, 65535),
        api_cert=_caminho(os.environ.get("FLOW_BRIDGE_API_CERT"), _CERTS / "bridge.crt"),
        api_chave=_caminho(os.environ.get("FLOW_BRIDGE_API_KEY"), _CERTS / "bridge.key"),
        max_simulacoes=_inteiro("FLOW_BRIDGE_MAX_SIMULACOES", 8, 1, 64),
    )
