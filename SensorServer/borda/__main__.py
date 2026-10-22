"""python -m borda  ->  a BORDA como processo proprio (S5). Ver borda/servico.py.

As duas variaveis abaixo precisam existir ANTES de qualquer outro import do projeto: comum/const.py escolhe o arquivo de
ambiente (.env.borda) por FLOW_SERVICO e borda/const.py le FLOW_BORDA_ISOLADA ao ser importado."""
import os
import sys

os.environ["FLOW_SERVICO"] = "borda"
os.environ["FLOW_BORDA_ISOLADA"] = "1"

from borda import servico  # noqa: E402

if __name__ == "__main__":
    sys.exit(servico.main())
