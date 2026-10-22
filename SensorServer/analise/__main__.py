"""python -m analise  ->  a ANALISE como processo proprio (S5). Ver analise/servico.py.

FLOW_SERVICO precisa existir ANTES de qualquer outro import do projeto: comum/const.py escolhe o arquivo de ambiente
(.env.analise) por ele."""
import os
import sys

os.environ["FLOW_SERVICO"] = "analise"

from analise import servico  # noqa: E402

if __name__ == "__main__":
    sys.exit(servico.main())
