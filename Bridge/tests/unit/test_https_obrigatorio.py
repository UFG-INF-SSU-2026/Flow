"""Toda ponta do bridge fala HTTPS: a configuracao recusa texto claro, e a API nao tem rota nem listener sem TLS."""
import pytest

import bridge
from config import Config, ler_config


def test_config_recusa_servidor_em_texto_claro():
    with pytest.raises(ValueError):
        Config(servidor="http://127.0.0.1:5000", ca="ca.crt")


def test_ler_config_encerra_com_mensagem_se_o_servidor_nao_e_https(monkeypatch):
    monkeypatch.setenv("FLOW_SERVIDOR", "http://127.0.0.1:5000")
    with pytest.raises(SystemExit) as e:
        ler_config()
    assert "https://" in str(e.value)


def test_ler_config_aceita_https(monkeypatch):
    monkeypatch.setenv("FLOW_SERVIDOR", "https://127.0.0.1:5000/")
    assert ler_config().servidor == "https://127.0.0.1:5000"


def test_contexto_tls_da_api_exige_tls_1_2_ou_superior_e_certificado_opcional_so_da_ca_de_dispositivos(tmp_path):
    import ssl
    from apoio import criar_pki
    pki = criar_pki(tmp_path)
    cfg = Config(servidor="https://127.0.0.1:5000", ca=str(pki.ca_raiz.arquivo), api_cert=pki.servidor_cert,
                 api_chave=pki.servidor_chave, ca_dispositivos=str(pki.ca_disp.arquivo))
    ctx = bridge.contexto_tls(cfg)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode == ssl.CERT_OPTIONAL
    assert len(ctx.get_ca_certs()) == 1                     # so a CA de dispositivos (nao a raiz)


def test_verificar_exige_a_ca_de_dispositivos(tmp_path):
    from apoio import criar_pki
    pki = criar_pki(tmp_path)
    cfg = Config(servidor="https://127.0.0.1:5000", ca=str(pki.ca_raiz.arquivo), api_cert=pki.servidor_cert,
                 api_chave=pki.servidor_chave, ca_dispositivos=str(tmp_path / "falta.crt"))
    assert "FLOW_CA_DISPOSITIVOS_CERT" in bridge.verificar(cfg)
    assert bridge.verificar(Config(**{**cfg.__dict__, "ca_dispositivos": str(pki.ca_disp.arquivo)})) is None


def test_verificar_recusa_servidor_em_texto_claro_mesmo_fora_da_config(tmp_path):
    from types import SimpleNamespace
    assert "https://" in bridge.verificar(SimpleNamespace(servidor="http://x"))


class TestCertificadoProprioDoBridge:
    """O bridge.crt tambem e a identidade do bridge na borda: sem clientAuth ou com outro CN a borda o recusa no handshake. Melhor
    falhar na subida, com o remedio, do que numa mensagem obscura de TLS."""

    def _cfg(self, tmp_path, **kw):
        from apoio import certificado_de_servidor, criar_pki
        pki = criar_pki(tmp_path)
        cert, chave = certificado_de_servidor(tmp_path, pki.ca_raiz, **kw)
        return Config(servidor="https://127.0.0.1:5000", ca=str(pki.ca_raiz.arquivo), api_cert=cert, api_chave=chave,
                      ca_dispositivos=str(pki.ca_disp.arquivo))

    def test_certificado_emitido_para_o_bridge_passa(self, tmp_path):
        assert bridge.verificar(self._cfg(tmp_path)) is None

    def test_so_server_auth_e_recusado_com_o_remedio(self, tmp_path):
        motivo = bridge.verificar(self._cfg(tmp_path, cliente=False))
        assert motivo and "clientAuth" in motivo and "--forcar --nome bridge" in motivo

    def test_outro_nome_e_recusado(self, tmp_path):
        motivo = bridge.verificar(self._cfg(tmp_path, nome="outro-servico"))
        assert motivo and "CN=bridge" in motivo and "--forcar --nome bridge" in motivo
