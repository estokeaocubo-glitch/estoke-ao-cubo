"""
Serviço de Cadastros Base (clientes, fornecedores, categorias, vendedores,
tarefas do estabelecimento, lojas e usuários).

Contrato de serviço (F1b Lote 8A):
- Python puro, sem dependências de UI (tkinter/customtkinter).
- Dependência injetada no construtor (db).
- Retorna dados de domínio e lança ValueError com a mensagem exibida pela UI.
- Hash de senha continua no Database; nunca registra senha em logs ou exceções.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

logger = logging.getLogger("EstokeAoCubo.services.cadastro")

_CENTAVOS = Decimal("0.01")


def _df_to_records(df: Any) -> list[dict[str, Any]]:
    """Converte DataFrame (ou lista) para list[dict] preservando vazio como []."""
    if df is None:
        return []
    if isinstance(df, list):
        return list(df)
    if getattr(df, "empty", False):
        return []
    return df.to_dict(orient="records")


def _parse_decimal_opcional(valor: Any, padrao: Decimal = Decimal("0.00")) -> Decimal:
    """Converte entrada numérica/texto (vírgula ou ponto) para Decimal com 2 casas."""
    if valor in (None, ""):
        return padrao
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError):
        return padrao
    if not d.is_finite():
        return padrao
    return d.quantize(_CENTAVOS, rounding=ROUND_HALF_UP)


class CadastroService:
    """Centraliza CRUD e consultas de cadastros administrativos e comerciais."""

    def __init__(self, db: Any):
        self.db = db

    # ------------------------------------------------------------------
    # Clientes e Fidelidade
    # ------------------------------------------------------------------

    def listar_clientes(self) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_clientes())

    def adicionar_cliente(
        self,
        nome: str,
        contato: str = "",
        observacoes: str = "",
        cpf_cnpj: str = "",
    ) -> int:
        nome_limpo = str(nome or "").strip()
        if not nome_limpo:
            raise ValueError("Nome é obrigatório.")
        return int(
            self.db.adicionar_cliente(
                nome_limpo,
                str(contato or "").strip(),
                str(observacoes or "").strip(),
                cpf_cnpj=str(cpf_cnpj or "").strip(),
            )
        )

    def atualizar_cliente(
        self,
        cliente_id: int,
        nome: str,
        contato: str = "",
        observacoes: str = "",
        cpf_cnpj: str = "",
    ) -> None:
        nome_limpo = str(nome or "").strip()
        if not nome_limpo:
            raise ValueError("Nome é obrigatório.")
        self.db.atualizar_cliente(
            int(cliente_id),
            nome_limpo,
            str(contato or "").strip(),
            str(observacoes or "").strip(),
            cpf_cnpj=str(cpf_cnpj or "").strip(),
        )

    def remover_cliente(self, cliente_id: int) -> None:
        self.db.remover_cliente(int(cliente_id))

    def vendas_por_cliente(self, cliente_nome: str) -> list[dict[str, Any]]:
        nome_limpo = str(cliente_nome or "").strip()
        if not nome_limpo:
            return []
        return _df_to_records(self.db.vendas_por_cliente(nome_limpo))

    def listar_fidelidade_clientes(self) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_fidelidade_clientes())

    def get_pontos_cliente(self, cliente_id: int) -> dict[str, Any]:
        pontos, total_acumulado = self.db.get_pontos_cliente(int(cliente_id))
        return {"pontos": int(pontos or 0), "total_acumulado": float(total_acumulado or 0.0)}

    def usar_pontos_fidelidade(self, cliente_id: int, pontos: int) -> tuple[bool, Any]:
        return self.db.usar_pontos_fidelidade(int(cliente_id), int(pontos))

    # ------------------------------------------------------------------
    # Fornecedores
    # ------------------------------------------------------------------

    def listar_fornecedores(self) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_fornecedores())

    def adicionar_fornecedor(
        self,
        nome: str,
        contato: str = "",
        produto_fornecido: str = "",
    ) -> None:
        nome_limpo = str(nome or "").strip()
        if not nome_limpo:
            raise ValueError("Nome do fornecedor é obrigatório.")
        self.db.adicionar_fornecedor(
            nome_limpo,
            str(contato or "").strip(),
            str(produto_fornecido or "").strip(),
        )

    def remover_fornecedor(self, fornecedor_id: int) -> None:
        self.db.remover_fornecedor(int(fornecedor_id))

    # ------------------------------------------------------------------
    # Categorias, Subcategorias, Tamanhos e Sub-subcategorias
    # ------------------------------------------------------------------

    def listar_categorias(self, tipo: str = "produto") -> list[str]:
        return list(self.db.listar_categorias(tipo=tipo) or [])

    def adicionar_categoria(self, nome: str, tipo: str = "produto") -> bool:
        nome_limpo = str(nome or "").strip()
        if not nome_limpo:
            raise ValueError("Digite um nome para a categoria.")
        return bool(self.db.adicionar_categoria(nome_limpo, tipo=tipo))

    def remover_categoria(self, nome: str, tipo: str = "produto") -> None:
        self.db.remover_categoria(str(nome or "").strip(), tipo=tipo)

    def listar_subcategorias(self, categoria_nome: str, tipo: str = "produto") -> list[str]:
        return list(self.db.listar_subcategorias(categoria_nome, tipo=tipo) or [])

    def adicionar_subcategoria(self, categoria_nome: str, subcategoria_nome: str, tipo: str = "produto") -> bool:
        sub_limpa = str(subcategoria_nome or "").strip()
        if not sub_limpa:
            raise ValueError("Informe o nome da subcategoria.")
        return bool(self.db.adicionar_subcategoria(categoria_nome, sub_limpa, tipo=tipo))

    def remover_subcategoria(self, categoria_nome: str, subcategoria_nome: str, tipo: str = "produto") -> None:
        self.db.remover_subcategoria(categoria_nome, subcategoria_nome, tipo=tipo)

    def listar_tamanhos(self, categoria_nome: str, subcategoria_nome: str, tipo: str = "produto") -> list[str]:
        return list(self.db.listar_tamanhos(categoria_nome, subcategoria_nome, tipo=tipo) or [])

    def adicionar_tamanho(self, categoria_nome: str, subcategoria_nome: str, tamanho_nome: str, tipo: str = "produto") -> bool:
        tam_limpo = str(tamanho_nome or "").strip()
        if not tam_limpo:
            raise ValueError("Informe o tamanho.")
        return bool(self.db.adicionar_tamanho(categoria_nome, subcategoria_nome, tam_limpo, tipo=tipo))

    def remover_tamanho(self, categoria_nome: str, subcategoria_nome: str, tamanho_nome: str, tipo: str = "produto") -> None:
        self.db.remover_tamanho(categoria_nome, subcategoria_nome, tamanho_nome, tipo=tipo)

    def listar_sub_subcategorias(self, categoria_nome: str, subcategoria_nome: str, tipo: str = "produto") -> list[str]:
        return list(self.db.listar_sub_subcategorias(categoria_nome, subcategoria_nome, tipo=tipo) or [])

    def adicionar_sub_subcategoria(self, categoria_nome: str, subcategoria_nome: str, sub_sub_nome: str, tipo: str = "produto") -> bool:
        ss_limpa = str(sub_sub_nome or "").strip()
        if not ss_limpa:
            raise ValueError("Informe a sub-subcategoria.")
        return bool(self.db.adicionar_sub_subcategoria(categoria_nome, subcategoria_nome, ss_limpa, tipo=tipo))

    def remover_sub_subcategoria(self, categoria_nome: str, subcategoria_nome: str, sub_sub_nome: str, tipo: str = "produto") -> None:
        self.db.remover_sub_subcategoria(categoria_nome, subcategoria_nome, sub_sub_nome, tipo=tipo)

    # ------------------------------------------------------------------
    # Vendedores / Funcionários
    # ------------------------------------------------------------------

    def listar_vendedores(self, apenas_ativos: bool = True) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_vendedores(apenas_ativos=apenas_ativos))

    def adicionar_vendedor(
        self,
        nome: str,
        horario_inicio: str = "08:00",
        horario_fim: str = "18:00",
        comissao: Any = 0.0,
    ) -> None:
        nome_limpo = str(nome or "").strip()
        if not nome_limpo:
            raise ValueError("Informe o nome.")
        com_dec = _parse_decimal_opcional(comissao, padrao=Decimal("0.00"))
        if com_dec < Decimal("0.00"):
            com_dec = Decimal("0.00")
        self.db.adicionar_vendedor(
            nome_limpo,
            str(horario_inicio or "08:00").strip() or "08:00",
            str(horario_fim or "18:00").strip() or "18:00",
            float(com_dec),
        )

    def atualizar_vendedor(
        self,
        vendedor_id: int,
        nome: str,
        horario_inicio: str,
        horario_fim: str,
        comissao: Any,
    ) -> None:
        nome_limpo = str(nome or "").strip()
        if not nome_limpo:
            raise ValueError("Informe o nome.")
        com_dec = _parse_decimal_opcional(comissao, padrao=Decimal("0.00"))
        self.db.atualizar_vendedor(
            int(vendedor_id),
            nome_limpo,
            str(horario_inicio or "08:00").strip(),
            str(horario_fim or "18:00").strip(),
            float(com_dec),
        )

    def remover_vendedor(self, vendedor_id: int) -> None:
        self.db.remover_vendedor(int(vendedor_id))

    def vendedor_no_turno(self) -> str:
        return str(self.db.vendedor_no_turno() or "")

    def vendas_por_vendedor(self, dias: int = 30) -> list[dict[str, Any]]:
        return _df_to_records(self.db.vendas_por_vendedor(dias=int(dias)))

    # ------------------------------------------------------------------
    # Estabelecimento (Tarefas Operacionais)
    # ------------------------------------------------------------------

    def listar_tarefas_estabelecimento(self) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_tarefas_estabelecimento())

    def adicionar_tarefa_estabelecimento(
        self,
        area_local: str,
        tarefa: str,
        prioridade: str = "Média",
        status: str = "Pendente",
    ) -> None:
        area_limpa = str(area_local or "").strip()
        tarefa_limpa = str(tarefa or "").strip()
        if not area_limpa or not tarefa_limpa:
            raise ValueError("Preencha Área/Local e Tarefa.")
        self.db.adicionar_tarefa_estabelecimento(
            area_limpa,
            tarefa_limpa,
            prioridade=str(prioridade or "Média").strip(),
            status=str(status or "Pendente").strip(),
        )

    def atualizar_status_estabelecimento(self, tarefa_id: int, novo_status: str) -> None:
        self.db.atualizar_status_estabelecimento(int(tarefa_id), str(novo_status).strip())

    def atualizar_tarefa_estabelecimento(
        self,
        tarefa_id: int,
        area_local: str,
        tarefa: str,
        prioridade: str,
        status: str,
    ) -> None:
        area_limpa = str(area_local or "").strip()
        tarefa_limpa = str(tarefa or "").strip()
        if not area_limpa or not tarefa_limpa:
            raise ValueError("Preencha Área e Tarefa.")
        self.db.atualizar_tarefa_estabelecimento(
            int(tarefa_id),
            area_limpa,
            tarefa_limpa,
            str(prioridade or "Média").strip(),
            str(status or "Pendente").strip(),
        )

    def remover_tarefa_estabelecimento(self, tarefa_id: int) -> None:
        self.db.remover_tarefa_estabelecimento(int(tarefa_id))

    def criar_tarefas_estoque_baixo(self) -> int:
        return int(self.db.criar_tarefas_estoque_baixo() or 0)

    # ------------------------------------------------------------------
    # Lojas / Filiais
    # ------------------------------------------------------------------

    def listar_lojas(self) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_lojas())

    def adicionar_loja(
        self,
        nome: str,
        endereco: str = "",
        telefone: str = "",
        cnpj: str = "",
    ) -> int:
        nome_limpo = str(nome or "").strip()
        if not nome_limpo:
            raise ValueError("Nome obrigatório.")
        return int(
            self.db.adicionar_loja(
                nome_limpo,
                str(endereco or "").strip(),
                str(telefone or "").strip(),
                str(cnpj or "").strip(),
            )
        )

    def atualizar_loja(
        self,
        loja_id: int,
        nome: str,
        endereco: str = "",
        telefone: str = "",
        cnpj: str = "",
    ) -> None:
        nome_limpo = str(nome or "").strip()
        if not nome_limpo:
            raise ValueError("Nome obrigatório.")
        self.db.atualizar_loja(
            int(loja_id),
            nome_limpo,
            str(endereco or "").strip(),
            str(telefone or "").strip(),
            str(cnpj or "").strip(),
        )

    def remover_loja(self, loja_id: int) -> None:
        self.db.remover_loja(int(loja_id))

    # ------------------------------------------------------------------
    # Usuários (hash de senha permanece em db; nunca logar senha)
    # ------------------------------------------------------------------

    def listar_usuarios(self) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_usuarios())

    def adicionar_usuario(
        self,
        nome: str,
        login: str,
        senha: str,
        perfil: str = "vendedor",
        loja_id: int | None = None,
    ) -> tuple[bool, str]:
        nome_limpo = str(nome or "").strip()
        login_limpo = str(login or "").strip()
        senha_val = str(senha or "").strip()
        if not nome_limpo or not login_limpo or not senha_val:
            raise ValueError("Preencha todos os campos.")
        if len(senha_val) < 4:
            raise ValueError("Senha deve ter 4+ caracteres.")
        return self.db.adicionar_usuario(
            nome_limpo,
            login_limpo,
            senha_val,
            perfil=str(perfil or "vendedor").strip(),
            loja_id=loja_id,
        )

    def atualizar_usuario(
        self,
        user_id: int,
        nome: str,
        perfil: str,
        ativo: int | bool,
        loja_id: int | None = None,
    ) -> None:
        nome_limpo = str(nome or "").strip()
        if not nome_limpo:
            raise ValueError("Nome é obrigatório.")
        self.db.atualizar_usuario(
            int(user_id),
            nome_limpo,
            str(perfil or "vendedor").strip(),
            1 if ativo else 0,
            loja_id=loja_id,
        )

    def alterar_senha_usuario(self, user_id: int, nova_senha: str) -> None:
        senha_val = str(nova_senha or "").strip()
        if len(senha_val) < 4:
            raise ValueError("Senha deve ter 4+ caracteres.")
        self.db.alterar_senha_usuario(int(user_id), senha_val)

    def remover_usuario(self, user_id: int) -> None:
        self.db.remover_usuario(int(user_id))
