"""
Serviço de caixa — abertura, sangria (com autorização de supervisor),
suprimento, resumo parcial, conferência cega de fechamento e histórico.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("EstokeAoCubo.services.caixa")

PERFIS_ISENTOS_SUPERVISOR = ("admin", "gerente")


class CaixaService:
    """Lógica de negócio pura para controle de caixa."""

    def __init__(self, db: Any, operador: dict[str, Any] | None = None):
        self.db = db
        self.operador = operador

    def caixa_aberto(self, usuario_id: int | None = None) -> dict[str, Any] | None:
        """Retorna dados do caixa aberto ou None."""
        return self.db.caixa_aberto(usuario_id)

    def resumo_caixa(
        self, usuario_id: int | None = None, loja_id: int | None = None
    ) -> dict[str, Any] | None:
        """Retorna o resumo detalhado do caixa atualmente aberto."""
        return self.db.resumo_caixa_atual(usuario_id=usuario_id, loja_id=loja_id)

    def abrir_caixa(
        self,
        valor_inicial: float,
        observacao: str = "",
        usuario_id: int = 1,
        loja_id: int = 1,
    ) -> tuple[bool, str]:
        """Abre o caixa com valor de troco inicial delegando ao Database."""
        try:
            return self.db.abrir_caixa(valor_inicial, observacao, usuario_id, loja_id)
        except Exception as e:
            logger.error("Erro ao abrir caixa: %s", e)
            return False, str(e)

    def _validar_autorizacao_supervisor(
        self,
        autorizacao: dict[str, Any] | None,
        operador: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Valida a regra atual de supervisor para operações sensíveis de caixa:
        - Operador logado com perfil 'admin' ou 'gerente' possui isenção automática
          quando autorizacao não foi fornecida explicitamente;
        - Caso contrário, exige dict de autorização com approved=True emitido por
          supervisor válido ('admin' ou 'gerente'). Sem isso, levanta PermissionError.
        """
        op_ctx = operador if operador is not None else self.operador
        if (
            autorizacao is None
            and isinstance(op_ctx, dict)
            and op_ctx.get("perfil") in PERFIS_ISENTOS_SUPERVISOR
        ):
            return {
                "approved": True,
                "supervisor_id": op_ctx.get("id"),
                "supervisor_nome": op_ctx.get("nome", "Supervisor"),
                "supervisor_perfil": op_ctx.get("perfil"),
                "motivo": "",
            }

        if not isinstance(autorizacao, dict) or autorizacao.get("approved") is not True:
            raise PermissionError(
                "Operação negada: sangria exige autorização válida de supervisor (admin ou gerente)."
            )

        perfil_sup = autorizacao.get("supervisor_perfil")
        if perfil_sup is not None and perfil_sup not in PERFIS_ISENTOS_SUPERVISOR:
            raise PermissionError(
                f"Perfil '{perfil_sup}' não possui permissão de supervisor para autorizar sangria."
            )

        sup_id = autorizacao.get("supervisor_id")
        sup_nome = str(autorizacao.get("supervisor_nome") or "").strip()
        if sup_id is None and not sup_nome:
            raise PermissionError(
                "Autorização de supervisor incompleta: identificação do autorizador ausente."
            )

        return autorizacao

    def registrar_sangria(
        self,
        valor: float,
        motivo: str,
        autorizacao: dict[str, Any] | None = None,
        *,
        caixa_id: int | None = None,
        usuario_id: int | None = None,
        observacao: str = "",
        operador: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """
        Registra sangria (retirada em espécie) exigindo autorização de supervisor
        conforme a regra vigente, delegando a persistência ao Database e gravando
        na auditoria quem autorizou.
        """
        auth_info = self._validar_autorizacao_supervisor(autorizacao, operador=operador)

        op_ctx = operador if operador is not None else self.operador
        uid = usuario_id
        if uid is None:
            uid = op_ctx.get("id") if isinstance(op_ctx, dict) and op_ctx.get("id") else None

        cid = caixa_id
        if cid is None:
            cx = self.caixa_aberto(uid)
            if not cx:
                return False, "Nenhum caixa aberto."
            cid = cx["id"]
            if uid is None:
                uid = cx.get("usuario_id") or 1

        if uid is None:
            uid = 1

        ok, msg = self.db.registrar_sangria(cid, uid, valor, motivo, observacao)
        if ok:
            sup_id = auth_info.get("supervisor_id")
            sup_nome = auth_info.get("supervisor_nome") or "Supervisor"
            self.db.registrar_alteracao(
                "Autorização Sangria",
                f"Sangria R$ {float(valor):.2f} ({motivo}) no Caixa #{cid} autorizada por {sup_nome} (ID: {sup_id})",
                usuario_id=sup_id if sup_id is not None else uid,
                entidade="caixa",
                entidade_id=str(cid),
            )
        return ok, msg

    def registrar_suprimento(
        self,
        valor: float,
        motivo: str = "Reforço de troco",
        *,
        caixa_id: int | None = None,
        usuario_id: int | None = None,
        observacao: str = "",
    ) -> tuple[bool, str]:
        """Registra suprimento (reforço de troco) delegando ao Database."""
        op_ctx = self.operador
        uid = usuario_id
        if uid is None:
            uid = op_ctx.get("id") if isinstance(op_ctx, dict) and op_ctx.get("id") else None

        cid = caixa_id
        if cid is None:
            cx = self.caixa_aberto(uid)
            if not cx:
                return False, "Nenhum caixa aberto."
            cid = cx["id"]
            if uid is None:
                uid = cx.get("usuario_id") or 1

        if uid is None:
            uid = 1

        return self.db.registrar_suprimento(cid, uid, valor, motivo, observacao)

    def fechar_caixa(
        self,
        valor_informado: float,
        observacao: str = "",
        usuario_id: int | None = None,
        conferido_pix: float | None = None,
        conferido_cartao: float | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Fecha o caixa com conferência cega (Dinheiro, PIX e Cartão) delegando ao Database."""
        try:
            return self.db.fechar_caixa(
                valor_informado,
                observacao,
                usuario_id=usuario_id,
                conferido_pix=conferido_pix,
                conferido_cartao=conferido_cartao,
            )
        except Exception as e:
            logger.error("Erro ao fechar caixa: %s", e)
            return False, str(e)

    @staticmethod
    def formatar_resumo_fechamento(resultado: dict[str, Any]) -> str:
        """Formata o texto padrão da conferência cega de fechamento de caixa."""
        return (
            f"Caixa fechado com sucesso!\n\n"
            f"Vendas no período: R$ {resultado['total_vendas']:.2f}\n"
            f"Abertura: R$ {resultado['valor_abertura']:.2f} | "
            f"Suprimentos: R$ {resultado.get('total_suprimentos', 0.0):.2f} | "
            f"Sangrias: R$ {resultado['total_sangrias']:.2f}\n\n"
            f"• Dinheiro — Esperado: R$ {resultado['esperado']:.2f} | "
            f"Informado: R$ {resultado['informado']:.2f} | Dif.: R$ {resultado['diferenca']:.2f}\n"
            f"• PIX      — Esperado: R$ {resultado['total_pix']:.2f} | "
            f"Informado: R$ {resultado.get('conferido_pix', 0.0):.2f} | Dif.: R$ {resultado.get('diferenca_pix', 0.0):.2f}\n"
            f"• Cartão   — Esperado: R$ {resultado['total_cartao']:.2f} | "
            f"Informado: R$ {resultado.get('conferido_cartao', 0.0):.2f} | Dif.: R$ {resultado.get('diferenca_cartao', 0.0):.2f}"
        )

    def obter_total_fiado_caixa(self, caixa_id: int) -> float:
        """Retorna o total de vendas em fiado concluídas vinculadas a um caixa fechado/aberto."""
        with self.db._lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "SELECT COALESCE(SUM(valor_fiado), 0) FROM vendas WHERE caixa_id = ? AND status = 'Concluída'",
                (caixa_id,),
            )
            row = cursor.fetchone()
            return float(row[0] or 0.0) if row else 0.0

    def listar_movimentacoes_turno(self, caixa_id: int) -> dict[str, list[dict[str, Any]]]:
        """Retorna sangrias e suprimentos do turno como listas de dicts."""
        df_s = self.db.listar_sangrias(caixa_id)
        df_sup = (
            self.db.listar_suprimentos(caixa_id)
            if hasattr(self.db, "listar_suprimentos")
            else None
        )
        sangrias = df_s.to_dict(orient="records") if df_s is not None and not df_s.empty else []
        suprimentos = (
            df_sup.to_dict(orient="records") if df_sup is not None and not df_sup.empty else []
        )
        return {"sangrias": sangrias, "suprimentos": suprimentos}

    def historico_caixa(self, dias: int = 60) -> list[dict[str, Any]]:
        """Retorna o histórico recente de turnos de caixa como lista de dicts."""
        df = self.db.historico_caixa(dias)
        if df is None or df.empty:
            return []
        return df.to_dict(orient="records")
