"""
Serviço de Precificação, Margem e Markup (Estoke ao Cubo).

Contrato de serviço:
- Python puro, sem dependências de UI (tkinter/customtkinter).
- Reutiliza ai_module.PricingEngine sem duplicar a fórmula.
- Retorna dados de domínio e lança exceções de domínio (sem envelope {ok, data, error}).
"""
from __future__ import annotations

from typing import Any


class PrecificacaoService:
    """Centraliza cálculos de lucro, margem, markup e sugestão de preço."""

    def __init__(self, pricing_engine: Any = None):
        self._pricing_engine = pricing_engine

    def _get_engine(self) -> Any:
        if self._pricing_engine is None:
            import ai_module
            self._pricing_engine = ai_module.PricingEngine()
        return self._pricing_engine

    @staticmethod
    def _to_float(valor: Any, nome: str) -> float:
        try:
            if isinstance(valor, str):
                valor = valor.strip().replace(",", ".")
            return float(valor)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Valor inválido para '{nome}'.") from exc

    def calcular_margem(self, custo: float, preco: float) -> dict[str, float]:
        """
        Calcula lucro, margem sobre venda, markup sobre custo e retorno por real investido.
        Lança ValueError se custo <= 0.
        """
        custo_f = self._to_float(custo, "custo")
        preco_f = self._to_float(preco, "preco")
        if custo_f <= 0:
            raise ValueError("Custo deve ser > 0")

        lucro = preco_f - custo_f
        margem_pct = (lucro / preco_f) * 100.0 if preco_f > 0 else 0.0
        markup_pct = (lucro / custo_f) * 100.0
        retorno_por_real = preco_f / custo_f

        return {
            "custo": custo_f,
            "preco": preco_f,
            "lucro": lucro,
            "margem_pct": margem_pct,
            "markup_pct": markup_pct,
            "retorno_por_real": retorno_por_real,
        }

    def calcular_markup(self, custo: float, margem_desejada: float) -> dict[str, float]:
        """
        Calcula o preço de venda e o markup necessários para atingir uma margem desejada (%).
        margem_desejada é informada em percentual (ex.: 30.0 para 30%).
        """
        custo_f = self._to_float(custo, "custo")
        margem_f = self._to_float(margem_desejada, "margem_desejada")
        if custo_f <= 0:
            raise ValueError("Custo deve ser > 0")
        if margem_f < 0 or margem_f >= 100:
            raise ValueError("Margem desejada deve estar entre 0 e 99.9%.")

        fator = 1.0 - (margem_f / 100.0)
        preco_venda = round(custo_f / fator, 2)
        lucro = preco_venda - custo_f
        markup_pct = (lucro / custo_f) * 100.0

        return {
            "custo": custo_f,
            "margem_desejada_pct": margem_f,
            "preco_venda": preco_venda,
            "lucro": lucro,
            "markup_pct": markup_pct,
        }

    def sugerir_preco(
        self,
        custo_material: float,
        horas_trabalho: float = 0.0,
        valor_hora: float = 0.0,
        margem_lucro: float = 0.30,
        fator_risco: float = 0.0,
        fator_raridade: float = 1.0,
    ) -> dict[str, Any]:
        """
        Sugere preço justo delegando integralmente ao ai_module.PricingEngine.
        margem_lucro e fator_risco seguem o contrato original (fração 0.0 a 1.0).
        """
        custo_f = self._to_float(custo_material, "custo_material")
        horas_f = self._to_float(horas_trabalho, "horas_trabalho")
        valor_hora_f = self._to_float(valor_hora, "valor_hora")
        margem_f = self._to_float(margem_lucro, "margem_lucro")
        risco_f = self._to_float(fator_risco, "fator_risco")
        raridade_f = self._to_float(fator_raridade, "fator_raridade")

        engine = self._get_engine()
        return engine.calcular_preco_etico(
            custo_f,
            horas_f,
            valor_hora_f,
            margem_f,
            risco_f,
            raridade_f,
        )
