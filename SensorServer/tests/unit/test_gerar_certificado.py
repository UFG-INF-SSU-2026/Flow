"""scripts/gerar_certificado.py: a CA de dispositivos nasce independente da raiz e sem tocar nela; o cabecalho da CA para o sketch.
Cada teste roda numa pasta temporaria (as constantes de caminho do script apontam para ela): nada e escrito no projeto."""
import os
import shutil
import stat
import sys

import pytest
from cryptography import x509

from _apoio import gerador_de_certificado


@pytest.fixture
def gc(tmp_path, monkeypatch):
    """O script com todos os caminhos dentro de tmp_path: SensorServer/certs, SensorApp (so a pasta) e sketch/."""
    g = gerador_de_certificado()
    raiz = tmp_path / "SensorServer"
    pasta = raiz / "certs"
    (tmp_path / "SensorApp" / "app").mkdir(parents=True)
    (tmp_path / "sketch").mkdir()
    for nome, valor in {
        "RAIZ": raiz, "PASTA": pasta, "CA_CERT": pasta / "ca.crt", "CA_CHAVE": pasta / "ca.key",
        "CA_DISPOSITIVOS_CERT": pasta / "ca-dispositivos.crt", "CA_DISPOSITIVOS_CHAVE": pasta / "ca-dispositivos.key",
        "CERT_NO_APP": tmp_path / "SensorApp" / "app" / "src" / "main" / "res" / "raw" / "flow_cert.pem",
        "CABECALHO_NO_SKETCH": tmp_path / "sketch" / "ca_flow.h",
    }.items():
        monkeypatch.setattr(g, nome, valor)
    monkeypatch.setattr(g, "_ip_da_rede", lambda: None)
    return g


def _rodar(g, *args) -> int:
    return g.main(list(args))


def _bytes(*arquivos) -> list[bytes]:
    return [a.read_bytes() for a in arquivos]


def _certificado(caminho) -> x509.Certificate:
    return x509.load_pem_x509_certificate(caminho.read_bytes())


def _marcas(*arquivos) -> list[int]:
    return [a.stat().st_mtime_ns for a in arquivos]


class TestCaDeDispositivos:
    def test_c1_pasta_vazia_cria_a_raiz_a_de_dispositivos_e_os_servicos(self, gc):
        assert _rodar(gc) == 0
        for nome in ("ca", "ca-dispositivos", "borda", "analise", "banco"):
            assert (gc.PASTA / f"{nome}.crt").is_file() and (gc.PASTA / f"{nome}.key").is_file(), nome

    def test_c2_com_a_raiz_ja_existente_so_a_de_dispositivos_e_criada(self, gc):
        _rodar(gc)
        for arq in (gc.CA_DISPOSITIVOS_CERT, gc.CA_DISPOSITIVOS_CHAVE):
            arq.unlink()
        intocados = [gc.CA_CERT, gc.CA_CHAVE, *(gc.PASTA / f"{n}.{e}" for n in ("borda", "analise", "banco") for e in ("crt", "key")),
                     gc.CERT_NO_APP, gc.CABECALHO_NO_SKETCH]
        antes, marcas = _bytes(*intocados), _marcas(*intocados)
        assert _rodar(gc) == 0
        assert gc.CA_DISPOSITIVOS_CERT.is_file() and gc.CA_DISPOSITIVOS_CHAVE.is_file()
        assert _bytes(*intocados) == antes                 # ca.crt e ca.key byte a byte iguais; nada mais reescrito
        assert _marcas(*intocados) == marcas

    def test_c3_segunda_execucao_nao_recria_nada(self, gc, capsys):
        _rodar(gc)
        arquivos = sorted(gc.PASTA.iterdir())
        antes, marcas = _bytes(*arquivos), _marcas(*arquivos)
        capsys.readouterr()
        assert _rodar(gc) == 0
        assert _bytes(*arquivos) == antes and _marcas(*arquivos) == marcas
        assert "nada a fazer" in capsys.readouterr().out

    def test_c4_forcar_e_forcar_ca_nao_recriam_a_de_dispositivos(self, gc):
        _rodar(gc)
        disp = [gc.CA_DISPOSITIVOS_CERT, gc.CA_DISPOSITIVOS_CHAVE]
        antes, raiz = _bytes(*disp), gc.CA_CERT.read_bytes()
        assert _rodar(gc, "--forcar") == 0
        assert _bytes(*disp) == antes and gc.CA_CERT.read_bytes() == raiz       # --forcar: nem a raiz muda
        assert _rodar(gc, "--forcar-ca") == 0
        assert _bytes(*disp) == antes and gc.CA_CERT.read_bytes() != raiz       # --forcar-ca: so a raiz muda

    def test_c5_autoassinada_independente_da_raiz(self, gc):
        _rodar(gc)
        disp, raiz = _certificado(gc.CA_DISPOSITIVOS_CERT), _certificado(gc.CA_CERT)
        assert disp.subject == disp.issuer
        disp.verify_directly_issued_by(disp)
        bc = disp.extensions.get_extension_for_class(x509.BasicConstraints).value
        assert bc.ca is True and bc.path_length == 0
        assert disp.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
        assert disp.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value == "flow-ca-dispositivos"
        assert disp.public_key().public_numbers() != raiz.public_key().public_numbers()
        with pytest.raises(Exception):
            disp.verify_directly_issued_by(raiz)                                # a raiz nao a emitiu

    @pytest.mark.skipif(sys.platform == "win32", reason="permissao 0600 nao existe no Windows")
    def test_c5_chave_com_permissao_0600(self, gc):
        _rodar(gc)
        assert stat.S_IMODE(os.stat(gc.CA_DISPOSITIVOS_CHAVE).st_mode) == 0o600

    @pytest.mark.parametrize("sobra", ["CA_DISPOSITIVOS_CERT", "CA_DISPOSITIVOS_CHAVE"])
    def test_c6_so_um_dos_dois_arquivos_da_a_erro_claro_sem_criar_nada(self, gc, capsys, sobra):
        gc.PASTA.mkdir(parents=True)
        getattr(gc, sobra).write_text("x", encoding="ascii")
        assert _rodar(gc) == 1
        assert "incompleta" in capsys.readouterr().err
        assert [f.name for f in gc.PASTA.iterdir()] == [getattr(gc, sobra).name]

    def test_criar_ca_sem_nome_segue_com_o_nome_da_raiz(self, tmp_path):
        g = gerador_de_certificado()
        g.criar_ca(tmp_path / "x.crt", tmp_path / "x.key", dias=2)               # como tests/_apoio.py a chama
        assert _certificado(tmp_path / "x.crt").subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value == "flow-ca"


class TestCabecalhoDoSketch:
    def test_c7_tem_a_ca_publica_na_variavel_do_firmware(self, gc):
        _rodar(gc)
        texto = gc.CABECALHO_NO_SKETCH.read_text(encoding="utf-8")
        assert texto.startswith("#pragma once") and "static const char FLOW_CA_PEM[] = R\"PEM(\n" in texto
        assert gc.CA_CERT.read_text(encoding="ascii").strip() in texto          # o PEM identico a ca.crt
        assert "PRIVATE" not in texto
        assert gc.CA_DISPOSITIVOS_CERT.read_text(encoding="ascii").strip() not in texto
        assert texto.rstrip().endswith(")PEM\";")

    def test_c8_segunda_execucao_nao_reescreve(self, gc):
        _rodar(gc)
        antes = gc.CABECALHO_NO_SKETCH.stat().st_mtime_ns
        _rodar(gc)
        assert gc.CABECALHO_NO_SKETCH.stat().st_mtime_ns == antes

    def test_c9_forcar_ca_atualiza_para_a_ca_nova(self, gc):
        _rodar(gc)
        antigo = gc.CABECALHO_NO_SKETCH.read_text(encoding="utf-8")
        _rodar(gc, "--forcar-ca")
        novo = gc.CABECALHO_NO_SKETCH.read_text(encoding="utf-8")
        assert novo != antigo and gc.CA_CERT.read_text(encoding="ascii").strip() in novo

    def test_c9_cabecalho_apagado_e_regerado_sem_recriar_a_ca(self, gc):
        _rodar(gc)
        ca = gc.CA_CERT.read_bytes()
        gc.CABECALHO_NO_SKETCH.unlink()
        _rodar(gc)
        assert gc.CABECALHO_NO_SKETCH.is_file() and gc.CA_CERT.read_bytes() == ca

    def test_c10_sem_a_pasta_do_sketch_nao_ha_erro_nem_arquivo(self, gc):
        shutil.rmtree(gc.CABECALHO_NO_SKETCH.parent)
        assert _rodar(gc) == 0
        assert not gc.CABECALHO_NO_SKETCH.exists() and not gc.CABECALHO_NO_SKETCH.parent.exists()
