"""python -m borda  ->  a BORDA como processo proprio. Ver borda/servico.py.

A variavel abaixo precisa existir ANTES de qualquer outro import do projeto: comum/const.py precisa saber que servico e
este, por FLOW_SERVICO."""
import os
import sys

os.environ["FLOW_SERVICO"] = "borda"

from borda import servico  # noqa: E402

if __name__ == "__main__":
    sys.exit(servico.main())
