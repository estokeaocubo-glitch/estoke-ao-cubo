"""
Serviço de Autenticação, RBAC e Autorização de Supervisor (Estoke ao Cubo).

Contrato de serviço:
- Python puro, sem dependências de UI (tkinter/customtkinter).
- Dependências injetadas no construtor.
- Retorna dados de domínio e lança exceções de domínio (sem envelope {ok, data, error}).
- Nunca registra senha ou token completo em logs.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger("EstokeAoCubo.auth_service")


# Mapeamento centralizado botão -> chave existente em Database.PERFIS_PERMISSOES
# Inclui todos os botões base originais e os 25 botões condicionais de nicho aprovados.
PERMISSAO_MAP: dict[str, str] = {
    # Botões base (sidebar + topbar)
    "SALDO": "gastos",
    "GASTOS": "gastos",
    "CAIXA": "caixa",
    "KITS": "kits",
    "CATEGORIAS": "categorias",
    "FORNECEDORES": "fornecedores",
    "CLIENTES": "clientes",
    "ANOTA TUDO": "vender",
    "VENDEDORES": "vendedores",
    "CONFIG": "ver_config",
    "CHAT IA": "chat_ia",
    "INSUMOS": "insumos",
    "PRODUÇÃO": "producao",
    "TROCAS": "vender",
    "REPOSIÇÃO": "adicionar",
    "INVENTÁRIO": "editar",
    "COMPRAS": "adicionar",
    "MESAS": "mesas",
    "COMANDAS": "mesas",
    "COZINHA": "mesas",
    "CONTAS PAGAR": "contas_pagar",
    "ETIQUETAS": "etiquetas",
    "ADICIONAR": "adicionar",
    "VENDER": "vender",
    "SERVIÇO": "vender",
    "LISTA": "editar",
    "RELATORIO": "ver_relatorio",
    "DRE": "ver_relatorio",
    # Botões condicionais de nicho (Tabela C aprovada — apenas chaves já existentes)
    "ORDEM SERVIÇO": "vender",
    "AJUSTES / ATELIÊ": "vender",
    "BANHO & TOSA": "vender",
    "HARDWARE": "insumos",
    "RAÇÃO A GRANEL": "insumos",
    "MEDICAMENTOS": "insumos",
    "GARANTIAS": "vender",
    "Nº SÉRIE": "vender",
    "AGENDAMENTOS": "vender",
    "RESERVAS": "vender",
    "LOCAÇÃO": "vender",
    "ORÇAMENTOS": "vender",
    "LOTES & VALIDADE": "adicionar",
    "VENCIMENTOS": "adicionar",
    "VEÍCULOS": "clientes",
    "CONDOMÍNIOS": "clientes",
    "PETS & VACINAS": "clientes",
    "RECEITAS & GRAUS": "clientes",
    "ALUNOS & TURMAS": "clientes",
    "MENSALIDADES": "clientes",
    "PROFISSIONAIS": "vendedores",
    "MECÂNICOS": "vendedores",
    "PEDIDOS / MESAS": "mesas",
    "COMBOS": "mesas",
    "LEITOR RÁPIDO": "vender",
}


class AuthService:
    """Centraliza autenticação, controle de acesso RBAC e autorização de supervisor."""

    PERMISSAO_MAP = PERMISSAO_MAP

    def __init__(self, db: Any, config_module: Any = None):
        self.db = db
        self._config = config_module

    def _get_config(self) -> Any:
        if self._config is None:
            import config as _cfg
            self._config = _cfg
        return self._config

    @staticmethod
    def _extrair_perfil(usuario_ou_perfil: Any) -> str:
        if isinstance(usuario_ou_perfil, dict):
            return str(usuario_ou_perfil.get("perfil") or "").strip()
        if isinstance(usuario_ou_perfil, str):
            return usuario_ou_perfil.strip()
        return ""

    def tem_permissao(self, usuario_ou_perfil: Any, botao: str) -> bool:
        """
        Verifica se o usuário ou perfil possui permissão para o botão/ação (fail-closed).
        Botão não mapeado é restrito exclusivamente ao perfil 'admin'.
        """
        perfil = self._extrair_perfil(usuario_ou_perfil)
        if not perfil:
            return False
        chave = self.PERMISSAO_MAP.get(botao, "")
        if not chave:
            return perfil == "admin"
        return bool(self.db.usuario_tem_permissao(perfil, chave))

    def listar_permissoes_usuario(
        self,
        usuario_ou_perfil: Any,
        botoes: Optional[Iterable[str]] = None,
    ) -> list[str]:
        """Retorna a lista de botões permitidos para o usuário ou perfil informado."""
        alvo = list(botoes) if botoes is not None else list(self.PERMISSAO_MAP.keys())
        return [btn for btn in alvo if self.tem_permissao(usuario_ou_perfil, btn)]

    def autenticar(self, login: str, senha: str) -> Optional[dict]:
        """Autentica o operador delegando ao Database (sem alterar hash nem fluxo)."""
        login_limpo = str(login or "").strip()
        senha_val = str(senha or "")
        if not login_limpo or not senha_val:
            raise ValueError("Preencha login e senha.")
        return self.db.autenticar_usuario(login_limpo, senha_val)

    def precisa_trocar_senha(self, usuario: Optional[dict] = None) -> bool:
        """
        Indica se o usuário autenticado precisa trocar a senha padrão ou,
        quando chamado sem usuário, se o admin inicial ainda usa senha padrão.
        """
        if usuario is not None:
            return bool(isinstance(usuario, dict) and usuario.get("trocar_senha"))
        try:
            with self.db._lock:
                if hasattr(self.db, "_ensure_open"):
                    self.db._ensure_open()
                cur = self.db.conn.cursor()
                cur.execute("SELECT trocar_senha FROM usuarios WHERE login = 'admincentral'")
                row = cur.fetchone()
                return bool(row and row[0])
        except Exception:
            return False

    def verificar_gate_admin(self, login: str, senha: str) -> bool:
        """Valida acesso a áreas administrativas (credencial secundária admin ou perfil admin/gerente)."""
        login_limpo = str(login or "").strip()
        senha_val = str(senha or "")
        if not login_limpo or not senha_val:
            return False

        cfg = self._get_config()
        try:
            if cfg.verify_admin(login_limpo, senha_val):
                return True
        except Exception:
            pass

        try:
            user_db = self.db.autenticar_usuario(login_limpo, senha_val)
            if user_db and user_db.get("perfil") in ("admin", "gerente"):
                return True
        except Exception:
            pass

        return False

    def autorizar_supervisor(
        self,
        login: str,
        senha: str,
        acao: str,
        motivo: str = "",
        exigir_motivo: bool = False,
    ) -> dict:
        """
        Autoriza ação sensível por Admin ou Gerente e grava trilha de auditoria.
        Lança ValueError para campos ausentes e PermissionError para credenciais não autorizadas.
        """
        login_limpo = str(login or "").strip()
        senha_val = str(senha or "")
        motivo_limpo = str(motivo or "").strip()

        if not login_limpo or not senha_val:
            raise ValueError("Preencha login e senha do supervisor.")
        if exigir_motivo and not motivo_limpo:
            raise ValueError("Informe o motivo obrigatório da operação.")

        sup_user = None
        try:
            u = self.db.autenticar_usuario(login_limpo, senha_val)
            if u and u.get("perfil") in ("admin", "gerente"):
                sup_user = u
        except Exception:
            sup_user = None

        if sup_user is None:
            cfg = self._get_config()
            try:
                if cfg.verify_admin(login_limpo, senha_val):
                    sup_user = {"id": None, "nome": f"Admin ({login_limpo})", "perfil": "admin"}
            except Exception:
                sup_user = None

        if sup_user is None:
            raise PermissionError("Credenciais inválidas ou sem perfil Admin/Gerente.")

        sup_id = sup_user.get("id")
        sup_nome = sup_user.get("nome") or login_limpo
        sup_perfil = sup_user.get("perfil") or "admin"

        try:
            self.db.registrar_alteracao(
                "Autorização Supervisor",
                f"Ação '{acao}' autorizada por {sup_nome}" + (f" | Motivo: {motivo_limpo}" if motivo_limpo else ""),
                usuario_id=sup_id,
                entidade="autorizacao",
                depois={"acao": acao, "supervisor": sup_nome, "motivo": motivo_limpo},
            )
        except Exception:
            logger.warning("Falha ao gravar auditoria de autorização de supervisor para ação '%s'.", acao)

        return {
            "approved": True,
            "supervisor_id": sup_id,
            "supervisor_nome": sup_nome,
            "supervisor_perfil": sup_perfil,
            "motivo": motivo_limpo,
        }
