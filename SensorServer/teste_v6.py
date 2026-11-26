"""
teste_v6_captura.py - V6 (canal) SEM Wireshark: captura os bytes do trafego e prova que so passa TLS.

Como funciona: o script sobe um "espiao" local (um repasse TCP) entre ele mesmo e o servidor. Faz uma requisicao HTTPS normal
atraves do espiao, que GUARDA os bytes crus que trafegam, exatamente o que o Wireshark veria na rede. Depois confere:
  1. o fluxo e uma sequencia valida de REGISTROS TLS (tipos handshake=22, dados=23...), sem nada fora do protocolo;
  2. textos que existiriam em texto claro ("HTTP/1.1", "GET /health", cabecalhos, a senha) NAO aparecem nos bytes.

Nao precisa instalar nada (so Python), nem de administrador. Roda de qualquer pasta.

Uso:
    python teste_v6_captura.py                               # borda local, https://127.0.0.1:5000
    python teste_v6_captura.py 127.0.0.1:5443 --host host.wokwi.internal   # bridge (nome do certificado)
    python teste_v6_captura.py 192.168.0.10:5000             # borda pelo IP da rede
    --ca caminho\\para\\ca.crt   (padrao: certs/ca.crt, a CA do projeto)

Limite: captura a conexao DESTE script (que usa o mesmo TLS do app e do firmware); nao ve o trafego do celular. Para
provar o do celular, use o Wireshark/tcpdump (ou repita com o IP da rede e use o app ao mesmo tempo, sem este script).
"""
import argparse
import http.client
import socket
import ssl
import threading

SEGREDO = "SEGREDO-DE-TESTE-123"                    # texto que NAO pode aparecer nos bytes capturados
PROCURADOS = [b"HTTP/1.", b"GET /health", b"POST /auth", b"Authorization", b"Bearer", b"Content-Type",
              SEGREDO.encode(), b"identificador", b"segredo"]
TIPOS = {20: "ChangeCipherSpec", 21: "Alert", 22: "Handshake", 23: "ApplicationData"}


class Espiao:
    """Repasse TCP: escuta numa porta local, conecta ao destino e copia os bytes nos dois sentidos, guardando-os."""

    def __init__(self, destino: tuple[str, int]):
        self.destino = destino
        self.cliente_para_servidor = bytearray()
        self.servidor_para_cliente = bytearray()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.porta = self.sock.getsockname()[1]
        threading.Thread(target=self._aceitar, daemon=True).start()

    def _aceitar(self) -> None:
        cli, _ = self.sock.accept()
        srv = socket.create_connection(self.destino, timeout=10)
        t = threading.Thread(target=self._copiar, args=(srv, cli, self.servidor_para_cliente), daemon=True)
        t.start()
        self._copiar(cli, srv, self.cliente_para_servidor)
        t.join(timeout=5)

    @staticmethod
    def _copiar(origem: socket.socket, destino: socket.socket, guardar: bytearray) -> None:
        try:
            while dados := origem.recv(65536):
                guardar.extend(dados)
                destino.sendall(dados)
        except OSError:
            pass
        finally:
            for s in (origem, destino):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def registros_tls(fluxo: bytes) -> tuple[list[tuple[int, int, int]], bool]:
    """Le o fluxo como registros TLS: cabecalho de 5 bytes (tipo, versao, tamanho) + corpo. Devolve os registros e se o
    fluxo inteiro foi consumido sem sobras fora do protocolo."""
    achados, i = [], 0
    while i + 5 <= len(fluxo):
        tipo, versao, tamanho = fluxo[i], int.from_bytes(fluxo[i + 1:i + 3], "big"), int.from_bytes(fluxo[i + 3:i + 5], "big")
        if tipo not in TIPOS or (versao >> 8) != 3 or i + 5 + tamanho > len(fluxo):
            return achados, False
        achados.append((tipo, versao, tamanho))
        i += 5 + tamanho
    return achados, i == len(fluxo)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("alvo", nargs="?", default="127.0.0.1:5000", help="host:porta do servidor (borda 5000, bridge 5443)")
    ap.add_argument("--ca", default="certs/ca.crt")
    ap.add_argument("--host", default=None, help="nome para o cabecalho Host/SNI (ex.: host.wokwi.internal no bridge)")
    a = ap.parse_args()
    host, porta = a.alvo.rsplit(":", 1)
    espiao = Espiao((host, int(porta)))

    ctx = ssl.create_default_context(cafile=a.ca)       # a cadeia e VERIFICADA contra a CA do projeto
    ctx.check_hostname = False                          # so o nome (o certificado do bridge lista host.wokwi.internal)
    nome = a.host or host
    print(f"Requisicao HTTPS para {host}:{porta} passando pelo espiao local (127.0.0.1:{espiao.porta})...")
    conexao = http.client.HTTPSConnection("127.0.0.1", espiao.porta, context=ctx, timeout=10)
    conexao.sock = ctx.wrap_socket(socket.create_connection(("127.0.0.1", espiao.porta), timeout=10), server_hostname=nome)
    versao_tls = conexao.sock.version()
    conexao.request("POST", "/auth/login", body=('{"identificador":"x","segredo":"%s"}' % SEGREDO),
                    headers={"Host": nome, "Content-Type": "application/json", "Authorization": "Bearer abc"})
    status = conexao.getresponse().status
    conexao.close()
    print(f"A requisicao foi respondida normalmente (HTTP {status}) e a versao negociada foi {versao_tls}.\n")

    tudo_ok = True
    for titulo, fluxo in (("cliente -> servidor", bytes(espiao.cliente_para_servidor)),
                          ("servidor -> cliente", bytes(espiao.servidor_para_cliente))):
        regs, valido = registros_tls(fluxo)
        contagem = {}
        for tipo, _, _ in regs:
            contagem[TIPOS[tipo]] = contagem.get(TIPOS[tipo], 0) + 1
        encontrados = [p.decode(errors="replace") for p in PROCURADOS if p in fluxo]
        ok = valido and not encontrados and bool(regs)
        tudo_ok &= ok
        print(f"[{titulo}] {len(fluxo)} bytes capturados")
        print(f"  registros TLS: {contagem}   fluxo e TLS puro: {'sim' if valido else 'NAO'}")
        print(f"  texto claro encontrado: {encontrados if encontrados else 'nenhum'}")
        amostra = next((fluxo[i:i + 24] for i in range(len(fluxo)) if fluxo[i] == 23 and fluxo[i + 1] == 3), None)
        if amostra:
            print(f"  amostra de um registro de dados (ilegivel): {amostra.hex(' ')}")
        print(f"  -> {'OK' if ok else 'DIFERENTE'}")
    print("\nResultado: " + ("OK, so TLS no trafego capturado; nada legivel em texto claro (inclusive a senha)."
                             if tudo_ok else "DIFERENTE, ha texto claro ou bytes fora do TLS."))


if __name__ == "__main__":
    main()