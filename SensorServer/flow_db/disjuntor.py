"""Disjuntor simples: apos uma falha do banco, pausa as tentativas por alguns segundos.
Evita que cada requisicao espere o tempo limite do pool durante uma queda (principio: a borda segue aceitando dados)."""
import time


class Disjuntor:
    def __init__(self, pausa_s: float, relogio=time.monotonic):
        self._pausa = pausa_s
        self._relogio = relogio
        self._ate = 0.0

    def permitido(self) -> bool:
        return self._relogio() >= self._ate

    def falhou(self) -> None:
        self._ate = self._relogio() + self._pausa

    def sucesso(self) -> None:
        self._ate = 0.0
