# -*- coding: utf-8 -*-
"""
Bridge de aplicação: bootstrap, tema, lojas, tela cheia, menu lateral e status de atualização.

Só delega para ``config``, ``services.nicho_service``, ``services.auth_service``,
``services.updater`` e ``database``.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import TYPE_CHECKING, Any

from bridge import PERFIS_MULTI_LOJA, BridgeError

if TYPE_CHECKING:  # pragma: no cover
    from bridge import EstokeBridge

logger = logging.getLogger("EstokeAoCubo.bridge.app")

TEMAS_VALIDOS = ("light", "dark")
CHAVE_TEMA = "ui_theme"

TOPBAR_BOTOES = ("ADICIONAR", "VENDER", "SERVIÇO", "LISTA", "RELATORIO", "DRE")

# Agrupamento visual da sidebar (apresentação apenas; a lista e a ordem vêm do NichoService).
SECOES_SIDEBAR = (
    ("operacao", "Operação"),
    ("catalogo", "Catálogo"),
    ("financeiro", "Financeiro"),
    ("sistema", "Sistema & IA"),
)
_SECAO_POR_BOTAO = {
    # Operação
    "CAIXA": "operacao", "ANOTA TUDO": "operacao", "ETIQUETAS": "operacao",
    "ORDEM SERVIÇO": "operacao", "AJUSTES / ATELIÊ": "operacao", "BANHO & TOSA": "operacao",
    "MESAS": "operacao", "COMANDAS": "operacao", "COZINHA": "operacao",
    "PEDIDOS / MESAS": "operacao", "AGENDAMENTOS": "operacao", "RESERVAS": "operacao",
    "LOCAÇÃO": "operacao", "ORÇAMENTOS": "operacao", "TROCAS": "operacao",
    "GARANTIAS": "operacao", "Nº SÉRIE": "operacao", "LEITOR RÁPIDO": "operacao",
    "PRODUÇÃO": "operacao",
    # Catálogo
    "KITS": "catalogo", "INVENTÁRIO": "catalogo", "COMPRAS": "catalogo",
    "CATEGORIAS": "catalogo", "FORNECEDORES": "catalogo", "CLIENTES": "catalogo",
    "VENDEDORES": "catalogo", "INSUMOS": "catalogo", "REPOSIÇÃO": "catalogo",
    "HARDWARE": "catalogo", "RAÇÃO A GRANEL": "catalogo", "MEDICAMENTOS": "catalogo",
    "LOTES & VALIDADE": "catalogo", "VENCIMENTOS": "catalogo", "VEÍCULOS": "catalogo",
    "CONDOMÍNIOS": "catalogo", "PROFISSIONAIS": "catalogo", "PETS & VACINAS": "catalogo",
    "MECÂNICOS": "catalogo", "RECEITAS & GRAUS": "catalogo", "ALUNOS & TURMAS": "catalogo",
    "COMBOS": "catalogo",
    # Financeiro
    "SALDO": "financeiro", "GASTOS": "financeiro", "CONTAS PAGAR": "financeiro",
    "MENSALIDADES": "financeiro",
    # Sistema & IA
    "CONFIG": "sistema", "CHAT IA": "sistema",
}


def slugify(texto: str) -> str:
    """'CONTAS PAGAR' -> 'contas-pagar'; 'Nº SÉRIE' -> 'no-serie' (ASCII, estável para rotas)."""
    base = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base or "item"


class AppBridge:
    def __init__(self, root: "EstokeBridge") -> None:
        self._root = root

    # -- serviços (instanciados por chamada: são leves e sem estado) --
    def _nicho_service(self):
        from services.nicho_service import NichoService
        return NichoService(self._root._config)

    def _auth_service(self):
        from services.auth_service import AuthService
        return AuthService(self._root._db, self._root._config)

    # ------------------------------------------------------------------
    def get_bootstrap(self) -> dict:
        from services.updater import APP_VERSION

        cfg_mod = self._root._config
        nicho = cfg_mod.get_business_type()
        nicho_info = None
        if nicho:
            for item in self._nicho_service().listar_nichos():
                if item.get("key") == nicho:
                    nicho_info = {k: item.get(k) for k in ("key", "title", "icon", "color")}
                    break
        try:
            senha_padrao = bool(self._auth_service().precisa_trocar_senha())
        except Exception:
            logger.debug("Não foi possível verificar senha padrão do admin.", exc_info=True)
            senha_padrao = False
        try:
            trial = bool(self._root._db.is_trial_mode())
        except Exception:
            trial = False
        return {
            "app": "Estoke ao Cubo",
            "version": APP_VERSION,
            "ui": "web",
            "theme": self.get_theme(),
            "nicho": nicho,
            "nicho_info": nicho_info,
            "first_access_required": nicho is None,
            "senha_padrao_ativa": senha_padrao,
            "trial": trial,
            "session": self._root._auth.get_current_session(),
        }

    # ------------------------------------------------------------------
    def get_theme(self) -> str:
        cfg = self._root._config.load_config() or {}
        tema = cfg.get(CHAVE_TEMA)
        return tema if tema in TEMAS_VALIDOS else "light"

    def set_theme(self, theme: str) -> str:
        tema = str(theme or "").strip().lower()
        if tema not in TEMAS_VALIDOS:
            raise ValueError("Tema inválido. Use 'light' ou 'dark'.")
        cfg_mod = self._root._config
        cfg = cfg_mod.load_config() or {}
        cfg[CHAVE_TEMA] = tema
        cfg_mod.save_config(cfg)
        self._root._events.emit("theme_changed", {"theme": tema})
        return tema

    # ------------------------------------------------------------------
    def _lojas_visiveis(self, user: dict) -> list[dict]:
        lojas = self._root._db.listar_lojas()
        registros = lojas.to_dict(orient="records") if hasattr(lojas, "to_dict") else list(lojas or [])
        lista = [
            {"id": int(r["id"]), "nome": r.get("nome") or f"Loja {r['id']}",
             "cnpj": r.get("cnpj") or "", "endereco": r.get("endereco") or ""}
            for r in registros
        ]
        perfil = str(user.get("perfil") or "")
        loja_user = user.get("loja_id")
        if perfil not in PERFIS_MULTI_LOJA and loja_user:
            lista = [l for l in lista if l["id"] == int(loja_user)]
        return lista

    def list_lojas(self) -> dict:
        session = self._root._session
        user = session.require_user()
        perfil = str(user.get("perfil") or "")
        return {
            "lojas": self._lojas_visiveis(user),
            "loja_ativa_id": session.loja_ativa_id,
            "pode_trocar": perfil in PERFIS_MULTI_LOJA,
        }

    def set_loja_ativa(self, loja_id: Any) -> dict:
        session = self._root._session
        user = session.require_user()
        try:
            alvo = int(loja_id)
        except (TypeError, ValueError):
            raise ValueError("Loja inválida.") from None
        perfil = str(user.get("perfil") or "")
        loja_user = user.get("loja_id")
        if perfil not in PERFIS_MULTI_LOJA and loja_user and alvo != int(loja_user):
            raise PermissionError("Seu perfil só pode operar a própria loja.")
        if alvo not in {l["id"] for l in self._lojas_visiveis(user)}:
            raise ValueError("Loja não encontrada ou inativa.")
        session.set_loja_ativa(alvo)
        self._root._events.emit("loja_changed", {"loja_ativa_id": alvo})
        return {"loja_ativa_id": alvo}

    # ------------------------------------------------------------------
    def toggle_fullscreen(self) -> dict:
        window = self._root._window
        if window is None:
            raise BridgeError("NO_WINDOW", "Janela indisponível.")
        window.toggle_fullscreen()
        return {"fullscreen": self._root._session.toggle_fullscreen()}

    # ------------------------------------------------------------------
    def get_sidebar_menu(self) -> dict:
        nicho = self._root._config.get_business_type()
        user = self._root._session.user
        auth = self._auth_service()

        def _item(texto: str, acao: str | None = None) -> dict:
            return {
                "id": slugify(texto),
                "texto": texto,
                "acao": acao,
                "permitido": bool(user) and auth.tem_permissao(user, texto),
            }

        secoes = {sid: {"id": sid, "titulo": titulo, "itens": []} for sid, titulo in SECOES_SIDEBAR}
        for botao in self._nicho_service().get_sidebar_buttons(nicho):
            texto = botao["texto"]
            secoes[_SECAO_POR_BOTAO.get(texto, "operacao")]["itens"].append(_item(texto, botao["acao"]))

        return {
            "nicho": nicho or "variados",
            "secoes": [secoes[sid] for sid, _ in SECOES_SIDEBAR if secoes[sid]["itens"]],
            "topbar": [_item(t) for t in TOPBAR_BOTOES],
        }

    # ------------------------------------------------------------------
    def check_updates_status(self) -> dict:
        from services.updater import APP_VERSION, check_for_updates

        kwargs = {}
        if self._root._update_fetcher is not None:
            kwargs["fetcher"] = self._root._update_fetcher
        info = check_for_updates(**kwargs) or {}
        return {
            "available": bool(info.get("available")),
            "version": info.get("version"),
            "current_version": info.get("current_version") or APP_VERSION,
            "notes": info.get("notes"),
            "error": info.get("error"),
        }
