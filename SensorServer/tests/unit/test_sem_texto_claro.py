"""
Texto claro: nenhum literal `http://` nem `ws://` no codigo de producao, fora de uma lista explicita de excecoes.

A varredura cobre o repositorio inteiro (a pasta acima de SensorServer/): servidor, bridge, firmware e app. Entram os arquivos de
texto com as extensoes de codigo e de configuracao de EXTENSOES. Ficam de fora os .md, as pastas de testes, de docs e de build, as
pastas ocultas e os ambientes virtuais, e o `local.properties` e o `sketch/segredos.h` (configuracao da maquina). `https://` e `wss://` nao contam.

Toda ocorrencia aceita tem uma entrada em EXCECOES (arquivo, trecho exato na mesma linha, motivo) e esta descrita em
docs/arquitetura.md. Entrada sem ocorrencia correspondente tambem falha: a lista nao guarda excecao que deixou de existir.
A busca e textual: nao pega endereco montado por concatenacao.
"""
import os
import re

import pytest

RAIZ_SERVIDOR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # SensorServer/
RAIZ_REPO = os.path.dirname(RAIZ_SERVIDOR)

PASTAS_DO_REPO = ("SensorServer", "Bridge", "sketch", "SensorApp")
EXTENSOES = (".py", ".ino", ".h", ".cpp", ".kt", ".kts", ".xml", ".toml", ".yml", ".yaml", ".json", ".html", ".sh", ".ps1",
             ".properties", ".txt", ".example")
PASTAS_FORA = {"tests", "test", "host_test", "docs", "atividades", "build", "certs", "backups", "__pycache__"}
# Configuracao da maquina, fora do Git: o resultado nao pode depender de quem roda. segredos.h e a copia local de
# sketch/segredos.h.example, que e varrido; uma URL http:// nele nem compila (static_assert em transporte_wifi.h).
ARQUIVOS_FORA = {"local.properties", "segredos.h"}

TEXTO_CLARO = re.compile(r"(?:http|ws)://", re.IGNORECASE)

# (caminho relativo com "/", trecho exato que a linha deve conter, motivo)
EXCECOES = [
    ("SensorApp/app/src/main/AndroidManifest.xml", 'xmlns:android="http://schemas.android.com/apk/res/android"',
     "espaco de nomes XML: identificador, nao um endereco acessado"),
    ("SensorApp/app/src/main/res/layout/activity_main.xml", 'xmlns:android="http://schemas.android.com/apk/res/android"',
     "espaco de nomes XML: identificador, nao um endereco acessado"),
    ("SensorApp/app/src/main/res/layout/dialog_socket.xml", 'xmlns:android="http://schemas.android.com/apk/res/android"',
     "espaco de nomes XML: identificador, nao um endereco acessado"),
    ("sketch/wokwi.toml", "ws://localhost:9011",
     "gateway do wokwigw: WebSocket em texto claro so na propria maquina, entre o simulador e o gateway; os bytes do TLS "
     "firmware <-> bridge que passam por ele continuam cifrados"),
    ("sketch/transporte_wifi.h", "Uma URL http:// nem compila",
     "comentario que descreve a recusa de URL em texto claro; nao e uma conexao"),
    ("sketch/segredos.h.example", "uma URL http:// nao compila",
     "comentario que descreve a recusa de URL em texto claro; nao e uma conexao"),
]


def _pasta_ignorada(nome: str) -> bool:
    return nome in PASTAS_FORA or nome.startswith(".") or "venv" in nome.lower()


@pytest.fixture(scope="module")
def varredura():
    """(arquivos lidos por pasta de primeiro nivel, ocorrencias como (caminho relativo, numero da linha, linha))."""
    lidos: dict[str, int] = {}
    ocorrencias: list[tuple[str, int, str]] = []
    for pasta, subpastas, nomes in os.walk(RAIZ_REPO):
        subpastas[:] = [p for p in subpastas if not _pasta_ignorada(p)]
        for nome in nomes:
            if nome in ARQUIVOS_FORA or not nome.lower().endswith(EXTENSOES):
                continue
            caminho = os.path.join(pasta, nome)
            rel = os.path.relpath(caminho, RAIZ_REPO).replace(os.sep, "/")
            primeira = rel.split("/")[0]
            lidos[primeira] = lidos.get(primeira, 0) + 1
            with open(caminho, encoding="utf-8", errors="replace") as f:     # o repositorio mistura plataformas
                for numero, linha in enumerate(f, 1):
                    if TEXTO_CLARO.search(linha):
                        ocorrencias.append((rel, numero, linha.rstrip("\r\n")))
    return lidos, ocorrencias


def _permitida(rel: str, linha: str) -> bool:
    return any(rel == caminho and trecho in linha for caminho, trecho, _ in EXCECOES)


def test_nenhum_texto_claro_no_codigo_de_producao_fora_da_lista(varredura):
    _, ocorrencias = varredura
    proibidas = [f"{rel}:{numero}: {linha.strip()[:200]}" for rel, numero, linha in ocorrencias if not _permitida(rel, linha)]
    assert not proibidas, ("endereco em texto claro (http:// ou ws://) no codigo de producao:\n  " + "\n  ".join(proibidas)
                           + "\nRemova o endereco ou, se for inevitavel, acrescente uma entrada com motivo em EXCECOES "
                             "e documente a excecao em docs/arquitetura.md.")


@pytest.mark.parametrize("caminho, trecho, motivo", EXCECOES, ids=[e[0] for e in EXCECOES])
def test_toda_excecao_da_lista_ainda_ocorre(varredura, caminho, trecho, motivo):
    _, ocorrencias = varredura
    assert any(rel == caminho and trecho in linha for rel, _, linha in ocorrencias), (
        f"{caminho} nao tem mais uma linha com {trecho!r} ({motivo}): tire a entrada de EXCECOES")


def test_a_varredura_leu_servidor_bridge_firmware_e_app(varredura):
    lidos, _ = varredura
    for pasta in PASTAS_DO_REPO:
        assert os.path.isdir(os.path.join(RAIZ_REPO, pasta)), f"{pasta}/ nao existe ao lado de SensorServer/"
        assert lidos.get(pasta, 0) > 0, f"a varredura nao leu nenhum arquivo em {pasta}/"


@pytest.mark.parametrize("texto", ["http://x", "HTTP://x", "ws://x", 'gateway = "ws://localhost:9011"'])
def test_detector_reconhece_texto_claro(texto):
    assert TEXTO_CLARO.search(texto)


@pytest.mark.parametrize("texto", ["https://x", "wss://x", "HTTPS://x"])
def test_detector_nao_reconhece_https_nem_wss(texto):
    assert not TEXTO_CLARO.search(texto)
