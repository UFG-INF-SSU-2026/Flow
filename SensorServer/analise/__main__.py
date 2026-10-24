"""python -m analise  ->  a ANALISE como processo proprio. Ver analise/servico.py.

FLOW_SERVICO precisa existir ANTES de qualquer outro import do projeto: comum/const.py decide por ele que variaveis do .env
ler (so as FLOW_ANALISE_*)."""
import os
import sys

os.environ["FLOW_SERVICO"] = "analise"

from analise import servico  # noqa: E402

if __name__ == "__main__":
    sys.exit(servico.main())
