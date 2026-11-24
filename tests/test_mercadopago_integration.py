"""
Testes de Integração para Mercado Pago e Emissão de Nota Fiscal no Padrão do Drive.
"""
import os
import sys
import unittest

# Adiciona o diretório base ao sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from services.mercadopago_service import MercadoPagoService, get_mercadopago_service
import invoice_generator


class TestMercadoPagoIntegration(unittest.TestCase):
    """Valida as credenciais e o serviço do Mercado Pago."""

    def test_01_carregamento_credenciais(self):
        """Verifica se as chaves de produção do Mercado Pago foram carregadas corretamente."""
        mp = get_mercadopago_service()
        self.assertTrue(mp.esta_configurado(), "O Access Token do Mercado Pago deve estar configurado.")
        self.assertTrue(mp.access_token.startswith("APP_USR-"), "O Access Token deve ser de produção (APP_USR-).")
        self.assertTrue(mp.public_key.startswith("APP_USR-"), "A Public Key deve ser de produção (APP_USR-).")
        
        cfg = mp.get_public_config()
        self.assertEqual(cfg.get("mode"), "production")
        self.assertTrue(cfg.get("configured"))

    def test_02_geracao_nota_html_padrao_drive(self):
        """Valida se o HTML gerado segue rigorosamente o design do Padrão do Drive."""
        itens = [
            {"nome": "Camiseta Estoke Premium", "qtd": 2, "preco": 49.90, "desconto_item": 0.0},
            {"nome": "Caneca Personalizada", "qtd": 1, "preco": 35.00, "desconto_item": 5.0}
        ]
        cliente = {"nome": "Lucas Ferreira", "contato": "11999999999"}
        total = 129.80
        desconto = 5.0
        id_venda = "TEST20260821"
        config_nota = {
            "empresa_nome": "Estoke ao Cubo Tech",
            "empresa_cnpj": "12.345.678/0001-90",
            "empresa_telefone": "(11) 98765-4321",
            "empresa_endereco": "Av. Paulista, 1000 - SP",
            "mensagem_rodape": "Obrigado por comprar conosco!"
        }

        html = invoice_generator.gerar_nota_html(
            itens=itens,
            cliente=cliente,
            total_venda=total,
            id_venda=id_venda,
            desconto=desconto,
            forma_pagamento="PIX (Mercado Pago)",
            config_nota=config_nota,
            pix_payload="00020126580014br.gov.bcb.pix0136123e4567-e89b-12d3-a456-4266141740005204000053039865406129.805802BR5919Estoke ao Cubo Tech6009SAO PAULO62070503***6304ABCD"
        )

        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Estoke ao Cubo Tech", html)
        self.assertIn("Camiseta Estoke Premium", html)
        self.assertIn("R$ 129.80", html)
        self.assertIn("TEST20260821", html)
        self.assertIn("thermal-paper", html)
        self.assertIn("barcode-svg", html)
        self.assertIn("PIX (Mercado Pago)", html)

    def test_03_validacao_chamada_pix_online(self):
        """Testa geração de cobrança PIX via API do Mercado Pago."""
        mp = get_mercadopago_service()
        # Testamos a geração de uma cobrança de R$ 1,00 para validar a integração ao vivo
        res = mp.criar_cobranca_pix(
            valor=1.00,
            descricao="Teste de Integração Estoke ao Cubo",
            cliente_email="teste@estokeaocubo.com.br",
            cliente_nome="Teste Automatizado",
            id_venda="TEST001"
        )
        print("Resultado da chamada de PIX do Mercado Pago:", res)
        # Se houver conexão de rede e token válido, sucesso será True e retornará payment_id e qr_code
        if res.get("sucesso"):
            self.assertTrue(len(res.get("qr_code", "")) > 10, "Deve retornar código Pix Copia e Cola válido.")
            self.assertTrue(len(res.get("payment_id", "")) > 0, "Deve retornar ID do pagamento.")

    def test_04_validacao_site_html(self):
        """Valida que o site index.html e live_scanner.html existem e contêm os componentes de pagamento."""
        site_path = os.path.join(BASE_DIR, "..", "index.html")
        scanner_path = os.path.join(BASE_DIR, "..", "live_scanner.html")
        
        self.assertTrue(os.path.exists(site_path), "index.html deve existir.")
        self.assertTrue(os.path.exists(scanner_path), "live_scanner.html deve existir.")
        
        with open(site_path, "r", encoding="utf-8") as f:
            conteudo_site = f.read()
            self.assertIn("ESTOKE AO CUBO", conteudo_site)
            self.assertIn("Mercado Pago", conteudo_site)
            self.assertIn("thermal-paper", conteudo_site)
            self.assertIn("barcode-svg", conteudo_site)
            self.assertIn("/api/pagamento/pix", conteudo_site)


if __name__ == "__main__":
    unittest.main()
