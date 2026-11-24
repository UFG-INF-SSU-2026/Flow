"""borda/emissao.py: CSR valido vira certificado de cliente assinado pela CA de dispositivos; o que fugir do esperado e recusado."""
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from _apoio import criar_ca_dispositivos, csr_com_assinatura_adulterada, csr_rsa, montar_csr
from borda import const as const_borda
from borda import emissao
from comum import const as const_comum

CONTA, DEVICE = "bridge1", "bridge-dev"


@pytest.fixture
def agora():
    return datetime.now(timezone.utc).replace(microsecond=0)      # o certificado guarda segundos


def _emitir(agora, csr=...):
    return emissao.emitir_certificado(montar_csr() if csr is ... else csr, CONTA, DEVICE, agora)


def _cert(emitido) -> x509.Certificate:
    return x509.load_pem_x509_certificate(emitido.certificado_pem.encode("ascii"))


def _atributo(cert, oid):
    return cert.subject.get_attributes_for_oid(oid)[0].value


class TestCertificadoEmitido:
    def test_assinado_pela_ca_de_dispositivos_com_a_chave_do_csr(self, ca_dispositivos, agora):
        chave = ec.generate_private_key(ec.SECP256R1())
        cert = _cert(_emitir(agora, montar_csr(chave)))
        cert.verify_directly_issued_by(ca_dispositivos.certificado())
        assert cert.issuer == ca_dispositivos.certificado().subject
        assert cert.public_key().public_numbers() == chave.public_key().public_numbers()

    def test_identificacao_e_usos(self, ca_dispositivos, agora):
        cert = _cert(_emitir(agora))
        assert _atributo(cert, NameOID.COMMON_NAME) == DEVICE and _atributo(cert, NameOID.ORGANIZATIONAL_UNIT_NAME) == CONTA
        usos = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert list(usos) == [ExtendedKeyUsageOID.CLIENT_AUTH]
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        assert ku.digital_signature and not any([ku.key_cert_sign, ku.crl_sign, ku.key_encipherment, ku.key_agreement,
                                                 ku.content_commitment, ku.data_encipherment])
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is False and bc.critical
        with pytest.raises(x509.ExtensionNotFound):
            cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)

    def test_validade_curta_e_serial_diferente(self, ca_dispositivos, agora):
        a, b = _emitir(agora), _emitir(agora)
        cert = _cert(a)
        assert cert.not_valid_after_utc - agora == timedelta(days=const_borda.CERT_DISPOSITIVO_DIAS)
        assert agora - cert.not_valid_before_utc == timedelta(minutes=5)
        assert a.valido_ate == cert.not_valid_after_utc
        assert a.serial != b.serial and int(a.serial, 16) == cert.serial_number

    def test_validade_nunca_passa_da_ca(self, tmp_path, monkeypatch, agora):
        ca = criar_ca_dispositivos(tmp_path, dias=2)               # vence antes de CERT_DISPOSITIVO_DIAS
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_CERT", str(ca.cert))
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_KEY", str(ca.chave))
        assert _cert(_emitir(agora)).not_valid_after_utc == ca.certificado().not_valid_after_utc

    def test_o_dia_vem_de_const(self, ca_dispositivos, agora, monkeypatch):
        monkeypatch.setattr(const_borda, "CERT_DISPOSITIVO_DIAS", 3)
        assert _cert(_emitir(agora)).not_valid_after_utc - agora == timedelta(days=3)


class TestCsrRecusado:
    @pytest.mark.parametrize("nome,csr", [
        ("rsa", csr_rsa),
        ("p384", lambda: montar_csr(ec.generate_private_key(ec.SECP384R1()))),
        ("secp256k1", lambda: montar_csr(ec.generate_private_key(ec.SECP256K1()))),
        ("sha384", lambda: montar_csr(algoritmo=hashes.SHA384())),
        ("assinatura_adulterada", csr_com_assinatura_adulterada),
    ])
    def test_chave_algoritmo_ou_assinatura_fora_do_esperado(self, ca_dispositivos, agora, nome, csr):
        with pytest.raises(emissao.CsrInvalido):
            _emitir(agora, csr())

    @pytest.mark.parametrize("texto", [
        "isto nao e um pedido", "", "   ", None, 123,
        "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",            # tipo de PEM errado
        "-----BEGIN CERTIFICATE REQUEST-----\nnao-e-base64\n-----END CERTIFICATE REQUEST-----\n",
    ], ids=repr)
    def test_texto_que_nao_e_um_pedido_pem(self, ca_dispositivos, agora, texto):
        with pytest.raises(emissao.CsrInvalido):
            _emitir(agora, texto)

    def test_dois_pedidos_no_mesmo_texto(self, ca_dispositivos, agora):
        csr = montar_csr()
        with pytest.raises(emissao.CsrInvalido):
            _emitir(agora, csr + csr)

    def test_mensagens_nao_trazem_o_conteudo_recebido(self, ca_dispositivos, agora):
        marcador = "MARCADOR-QUE-NAO-PODE-VAZAR"
        for ruim in (marcador, f"-----BEGIN CERTIFICATE REQUEST-----\n{marcador}\n-----END CERTIFICATE REQUEST-----\n",
                     csr_rsa(), csr_com_assinatura_adulterada()):
            with pytest.raises(emissao.CsrInvalido) as erro:
                _emitir(agora, ruim)
            assert marcador not in str(erro.value)
            assert not any(trecho in str(erro.value) for trecho in ruim.splitlines()[1:2] if len(trecho) > 20)


class TestIdentidadeVemDoToken:
    def test_assunto_e_extensoes_do_csr_sao_ignorados(self, ca_dispositivos, agora):
        pedido = montar_csr(assunto="outro-dispositivo", extensoes=[
            (x509.SubjectAlternativeName([x509.DNSName("evil.example")]), False),
            (x509.BasicConstraints(ca=True, path_length=None), True),
            (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False),
        ])
        cert = _cert(_emitir(agora, pedido))
        assert _atributo(cert, NameOID.COMMON_NAME) == DEVICE and _atributo(cert, NameOID.ORGANIZATIONAL_UNIT_NAME) == CONTA
        assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
        assert list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [ExtendedKeyUsageOID.CLIENT_AUTH]
        with pytest.raises(x509.ExtensionNotFound):
            cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)

    @pytest.mark.parametrize("conta,device", [("", DEVICE), (CONTA, ""), (None, DEVICE), (CONTA, None)])
    def test_sem_identificacao_nao_emite(self, ca_dispositivos, agora, conta, device):
        with pytest.raises(emissao.CsrInvalido):
            emissao.emitir_certificado(montar_csr(), conta, device, agora)

    def test_identificacao_longa_demais_para_o_assunto_e_recusada(self, ca_dispositivos, agora):
        with pytest.raises(emissao.CsrInvalido):                           # CN de X.509 vai ate 64 caracteres
            emissao.emitir_certificado(montar_csr(), CONTA, "d" * 65, agora)


class TestCaIndisponivel:
    def _apontar(self, monkeypatch, cert, chave):
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_CERT", str(cert))
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_KEY", str(chave))

    def test_ausente(self, tmp_path, monkeypatch, agora):
        self._apontar(monkeypatch, tmp_path / "nada.crt", tmp_path / "nada.key")
        with pytest.raises(emissao.EmissaoIndisponivel, match="gerar_certificado"):
            _emitir(agora)

    def test_so_o_certificado_sem_a_chave(self, ca_dispositivos, monkeypatch, tmp_path, agora):
        self._apontar(monkeypatch, ca_dispositivos.cert, tmp_path / "nada.key")
        with pytest.raises(emissao.EmissaoIndisponivel):
            _emitir(agora)

    def test_chave_que_nao_corresponde_ao_certificado(self, tmp_path, monkeypatch, agora):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a, b = criar_ca_dispositivos(tmp_path / "a"), criar_ca_dispositivos(tmp_path / "b")
        self._apontar(monkeypatch, a.cert, b.chave)
        with pytest.raises(emissao.EmissaoIndisponivel):
            _emitir(agora)

    def test_vencida(self, ca_dispositivos, agora):
        with pytest.raises(emissao.EmissaoIndisponivel):
            _emitir(agora + timedelta(days=365))               # a CA de teste vale 30 dias

    def test_arquivo_ilegivel_nao_vaza_conteudo(self, ca_dispositivos, agora):
        ca_dispositivos.chave.write_text("SEGREDO-NO-ARQUIVO", encoding="utf-8")
        with pytest.raises(emissao.EmissaoIndisponivel) as erro:
            _emitir(agora)
        assert "SEGREDO-NO-ARQUIVO" not in str(erro.value)


class TestCaminhos:
    def test_padrao_relativo_a_pasta_do_servidor(self, monkeypatch):
        monkeypatch.delenv("FLOW_CA_DISPOSITIVOS_CERT", raising=False)
        esperado = const_comum.DOTENV_ARQUIVO.parent / const_borda.CA_DISPOSITIVOS_CERT_PADRAO
        assert emissao._caminho("FLOW_CA_DISPOSITIVOS_CERT", const_borda.CA_DISPOSITIVOS_CERT_PADRAO) == esperado

    def test_variavel_vence_o_padrao_e_relativo_tambem_vale_a_partir_do_servidor(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_KEY", str(tmp_path / "x.key"))
        assert emissao._caminho("FLOW_CA_DISPOSITIVOS_KEY", "padrao") == tmp_path / "x.key"
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_KEY", "outra/pasta/x.key")
        assert emissao._caminho("FLOW_CA_DISPOSITIVOS_KEY", "padrao") == const_comum.DOTENV_ARQUIVO.parent / "outra/pasta/x.key"


class _SocketFalso:
    """O `werkzeug.socket` de uma conexao TLS: getpeercert(binary_form=True) devolve o DER que o TLS ja validou."""

    def __init__(self, der=None, erro=None):
        self._der, self._erro = der, erro

    def getpeercert(self, binary_form=False):
        if self._erro:
            raise self._erro
        return self._der


def _der(emitido) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return _cert(emitido).public_bytes(serialization.Encoding.DER)


class TestIdentidadeDoCliente:
    """Caminho direto (o app). O caminho do bridge, com TLS de verdade, esta em tests/integration/test_certificado_de_cliente.py."""

    def test_le_conta_e_device_do_certificado_da_ca_de_dispositivos(self, ca_dispositivos, agora):
        environ = {"werkzeug.socket": _SocketFalso(_der(_emitir(agora)))}
        assert emissao.identidade_do_cliente(environ, agora) == (CONTA, DEVICE)

    def test_cabecalho_de_repasse_numa_conexao_que_nao_e_do_bridge_e_ignorado(self, ca_dispositivos, agora):
        import base64
        outro = base64.b64encode(_der(emissao.emitir_certificado(montar_csr(), "outra", "outro", agora))).decode()
        environ = {"werkzeug.socket": _SocketFalso(_der(_emitir(agora))),
                   "HTTP_" + const_borda.CABECALHO_CERTIFICADO_REPASSADO.upper().replace("-", "_"): outro}
        assert emissao.identidade_do_cliente(environ, agora) == (CONTA, DEVICE)

    @pytest.mark.parametrize("environ", [
        {},                                                                    # sem TLS (teste, desenvolvimento)
        {"werkzeug.socket": object()},                                         # socket sem getpeercert
        {"werkzeug.socket": _SocketFalso(None)},                               # TLS sem certificado de cliente
        {"werkzeug.socket": _SocketFalso(b"")},
        {"werkzeug.socket": _SocketFalso(erro=ValueError("handshake"))},
    ], ids=range(5))
    def test_sem_certificado_devolve_none(self, ca_dispositivos, agora, environ):
        assert emissao.identidade_do_cliente(environ, agora) is None

    def test_certificado_sem_uso_de_cliente_devolve_none(self, ca_dispositivos, agora):
        """O handshake aceita certificado sem a extensao de uso; a borda confere o clientAuth."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        ca = ca_dispositivos.certificado()
        nome = x509.Name([x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, CONTA), x509.NameAttribute(NameOID.COMMON_NAME, DEVICE)])
        cert = (x509.CertificateBuilder().subject_name(nome).issuer_name(ca.subject)
                .public_key(ec.generate_private_key(ec.SECP256R1()).public_key()).serial_number(1)
                .not_valid_before(agora).not_valid_after(agora + timedelta(days=1))
                .sign(serialization.load_pem_private_key(ca_dispositivos.chave.read_bytes(), None), hashes.SHA256()))
        der = cert.public_bytes(serialization.Encoding.DER)
        assert emissao.identidade_do_cliente({"werkzeug.socket": _SocketFalso(der)}, agora) is None

    def test_certificado_de_outro_emissor_devolve_none(self, ca_dispositivos, agora, tmp_path, monkeypatch):
        """Na conexao direta vale so o certificado emitido pela CA de dispositivos (a raiz tambem e aceita no TLS, pelo bridge)."""
        from _apoio import gerador_de_certificado
        outra_cert, outra_chave = tmp_path / "outra.crt", tmp_path / "outra.key"
        gerador_de_certificado().criar_ca(outra_cert, outra_chave, dias=2, nome="flow-ca")
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_CERT", str(outra_cert))
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_KEY", str(outra_chave))
        der = _der(_emitir(agora))                                              # emitido pela "flow-ca"
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_CERT", str(ca_dispositivos.cert))
        assert emissao.identidade_do_cliente({"werkzeug.socket": _SocketFalso(der)}, agora) is None


class TestConfigurarTlsDeCliente:
    def test_pede_opcional_e_confia_na_de_dispositivos_e_na_raiz(self, ca_dispositivos, monkeypatch):
        import ssl
        from _apoio import ambiente_tls
        monkeypatch.setenv("FLOW_TLS_CA", ambiente_tls()["FLOW_TLS_CA"])
        contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        emissao.configurar_tls_de_cliente(contexto)
        assert contexto.verify_mode == ssl.CERT_OPTIONAL
        assert sorted(c["subject"][0][0][1] for c in contexto.get_ca_certs()) == ["flow-ca", "flow-ca-dispositivos"]

    def test_sem_a_ca_de_dispositivos_nao_sobe(self, tmp_path, monkeypatch):
        import ssl
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_CERT", str(tmp_path / "nada.crt"))
        contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with pytest.raises(emissao.EmissaoIndisponivel):
            emissao.configurar_tls_de_cliente(contexto)
        assert contexto.verify_mode == ssl.CERT_NONE

    def test_ca_ilegivel_nao_sobe(self, ca_dispositivos):
        import ssl
        ca_dispositivos.cert.write_text("SEGREDO-NO-ARQUIVO", encoding="utf-8")
        with pytest.raises(emissao.EmissaoIndisponivel) as erro:
            emissao.configurar_tls_de_cliente(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
        assert "SEGREDO-NO-ARQUIVO" not in str(erro.value)
