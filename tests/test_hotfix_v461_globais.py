# -*- coding: utf-8 -*-
"""Hotfix v4.6.1: nenhum nome global usado nos módulos de boot pode estar indefinido.

Na v4.6.0 o main.py usava ``threading`` sem importar. O NameError aparecia só em tempo de
execução: 5 s após abrir (``_check_updates_async``, auto-update), aos 60 s
(``_start_routine_scheduler._tick_loop``, agendador de rotinas) e ao clicar em
``open_chat_ai``, ``open_report_dialog`` e ``open_importar_nfe_xml``.

A verificação é estática (``symtable`` da stdlib): nada é importado nem executado, então o
teste não abre janela, não toca banco nem faz chamada externa.
"""
import builtins
import os
import symtable

import pytest

RAIZ = os.environ.get("ESTOKE_REPO_RAIZ") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODULOS_BOOT = [
    "main.py",
    "crash_handler.py",
    "database.py",
    "api_server.py",
    "services/updater.py",
    "services/scheduler.py",
]

IMPLICITOS = {"__file__", "__name__", "__doc__", "__spec__", "__builtins__", "__loader__", "__package__"}

# Pontos do main.py que quebravam na v4.6.0 por falta de ``import threading``
# (caminho qualificado do escopo, como o symtable enxerga).
PONTOS_THREADING_MAIN = {
    "App._check_updates_async",                        # auto-update (dispara 5 s após abrir)
    "App._check_updates_async._worker._prompt_user",   # inicia o download do auto-update
    "App._start_routine_scheduler._tick_loop",         # agendador de rotinas (dispara aos 60 s)
    "App.open_chat_ai.send_message",                   # UI: chat de IA
    "App.open_report_dialog._exportar_pacote_zip",     # UI: relatórios
    "App.open_importar_nfe_xml",                       # UI: importar XML de NF-e
}


def _caminho(modulo):
    return os.path.join(RAIZ, *modulo.split("/"))


def _analisa(caminho):
    """Retorna (nomes definidos no módulo, {nome global usado: {caminhos dos escopos que o usam}})."""
    with open(caminho, encoding="utf-8") as f:
        topo = symtable.symtable(f.read(), caminho, "exec")
    definidos = {s.get_name() for s in topo.get_symbols() if s.is_assigned() or s.is_imported()}
    definidos |= set(dir(builtins)) | IMPLICITOS
    usados = {}

    def visita(tabela, caminho_escopo):
        for s in tabela.get_symbols():
            global_ = (tabela is topo) or s.is_global() or s.is_declared_global()
            if global_ and s.is_referenced():
                usados.setdefault(s.get_name(), set()).add(caminho_escopo or "<módulo>")
        for filho in tabela.get_children():
            visita(filho, f"{caminho_escopo}.{filho.get_name()}" if caminho_escopo else filho.get_name())

    visita(topo, "")
    return definidos, usados


@pytest.mark.parametrize("modulo", MODULOS_BOOT)
def test_modulo_boot_sem_global_indefinido(modulo):
    caminho = _caminho(modulo)
    if not os.path.exists(caminho):
        pytest.skip(f"{modulo} não existe nesta versão")
    definidos, usados = _analisa(caminho)
    indefinidos = {nome: sorted(escopos) for nome, escopos in usados.items() if nome not in definidos}
    assert indefinidos == {}


def test_threading_resolvivel_nos_pontos_do_hotfix():
    """Os 6 pontos (auto-update, agendador e 3 da UI) usam ``threading`` e ele é importado."""
    definidos, usados = _analisa(_caminho("main.py"))
    escopos = usados.get("threading", set())
    faltando = PONTOS_THREADING_MAIN - escopos
    assert not faltando, f"pontos que deixaram de usar threading (atualize o teste): {sorted(faltando)}"
    assert "threading" in definidos, f"threading usado sem import em: {sorted(escopos)}"
