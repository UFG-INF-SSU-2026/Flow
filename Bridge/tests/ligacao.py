"""Apoio dos testes de integracao: sobe o bridge real em thread (porta 0) e uma conexao TLS crua."""
import socket
import ssl
import threading

import api
import bridge
from apoio import HOST, Pki
from config import Config

PRAZO_S = 5


def subir_bridge(pki: Pki, servidor_url: str, **ajustes):
    """Sobe o bridge em thread (porta 0). Devolve (servidor, porta, cfg). Quem chama deve fazer servidor.stop()."""
    cfg = Config(servidor=servidor_url, ca=str(pki.ca_raiz.arquivo), host=HOST, porta=0, api_cert=pki.servidor_cert,
                 api_chave=pki.servidor_chave, ca_dispositivos=str(pki.ca_disp.arquivo), max_simulacoes=2)
    servidor = bridge.criar_servidor(cfg, bridge.acesso_logado(api.criar_app(cfg)))
    servidor.prepare()
    threading.Thread(target=servidor.serve, daemon=True).start()
    return servidor, servidor.bind_addr[1], cfg


class Conexao:
    """Uma conexao TLS crua (a requests.Session esconderia se a conexao foi fechada). Le a resposta pelo Content-Length."""

    def __init__(self, porta: int, ca: str, cliente: tuple[str, str] | None = None):
        ctx = ssl.create_default_context(cafile=ca)
        if cliente:
            ctx.load_cert_chain(*cliente)
        self.sock = ctx.wrap_socket(socket.create_connection((HOST, porta), timeout=PRAZO_S), server_hostname=HOST)
        self.sock.settimeout(PRAZO_S)

    def pedir(self, metodo: str, rota: str, corpo: bytes = b"", cabecalhos: dict | None = None):
        extra = "".join(f"{k}: {v}\r\n" for k, v in (cabecalhos or {}).items())
        cab = (f"{metodo} {rota} HTTP/1.1\r\nHost: {HOST}\r\nContent-Type: application/json\r\n{extra}"
               f"Content-Length: {len(corpo)}\r\n\r\n")
        self.sock.sendall(cab.encode() + corpo)
        return self.ler()

    def ler(self):
        buf = b""
        while b"\r\n\r\n" not in buf:
            parte = self.sock.recv(4096)
            assert parte, "a conexao foi fechada antes de a resposta chegar"
            buf += parte
        cab, _, resto = buf.partition(b"\r\n\r\n")
        linhas = cab.decode("latin-1").split("\r\n")
        status = int(linhas[0].split()[1])
        cabs = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in linhas[1:])}
        tamanho = int(cabs.get("content-length", "0"))
        while len(resto) < tamanho:
            parte = self.sock.recv(4096)
            assert parte, "a conexao foi fechada no meio do corpo"
            resto += parte
        return status, cabs, resto[:tamanho]

    def segue_aberta(self) -> bool:
        self.sock.settimeout(0.3)
        try:
            return self.sock.recv(1) != b""
        except (TimeoutError, ssl.SSLWantReadError):
            return True
        except OSError:
            return False
        finally:
            self.sock.settimeout(PRAZO_S)

    def fechar(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
