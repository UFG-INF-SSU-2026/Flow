"""CLI: python -m flow_db {migrar|status}. Carrega SensorServer/.env (via comum/const.py)."""
import argparse
import sys

from comum import const  # noqa: F401  (efeito colateral: carrega o .env; variavel de ambiente do terminal vence)

from . import config, migracoes


def _status() -> int:
    import psycopg
    from .conexao import versao_aplicada
    with psycopg.connect(config.dsn(), autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        atual = versao_aplicada(conn)
    print(f"migracao aplicada: {atual} (disponivel: {migracoes.ultima_disponivel()})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m flow_db")
    ap.add_argument("comando", choices=["migrar", "status"])
    a = ap.parse_args(argv)
    try:
        if a.comando == "migrar":
            feitas = migracoes.aplicar()
            print("migracoes aplicadas: " + (", ".join(f"{v:04d}" for v in feitas) if feitas else "nenhuma (em dia)"))
        else:
            return _status()
    except (config.ConfigBanco, migracoes.MigracaoInvalida) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001  (erro de conexao etc.: mensagem curta, sem senha)
        print(f"erro: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
