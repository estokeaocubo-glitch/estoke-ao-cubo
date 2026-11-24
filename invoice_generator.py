import os
import datetime
import subprocess
import sys
import logging
import sqlite3
import re

logger = logging.getLogger('EstokeAoCubo.invoice')

# Importação do gerador de Pix e caminhos centrais do banco de dados
try:
    from paths import get_app_data_path
except ImportError:
    from database import get_app_data_path

try:
    from printer import gerar_payload_pix
except ImportError:
    def gerar_payload_pix(chave, valor, nome, cidade, txid="***"):
        return ""


# ============================================================
# INICIALIZAÇÃO DE TABELAS E COLUNAS PIX (AUTO-HEALING)
# ============================================================

def _garantir_migracao_pix():
    """Garante que as colunas de Pix existam na tabela config_nota no SQLite."""
    # 1 linha para explicar o motivo: Auto-healing garante que o banco de dados se adapte sem intervenção manual do usuário.
    try:
        db_path = os.path.join(get_app_data_path(), "estoque.db")
        if os.path.exists(db_path):
            with sqlite3.connect(db_path, timeout=5.0) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(config_nota)")
                colunas = [c[1] for c in cursor.fetchall()]
                if 'pix_key' not in colunas:
                    cursor.execute("ALTER TABLE config_nota ADD COLUMN pix_key TEXT DEFAULT ''")
                if 'pix_recebedor' not in colunas:
                    cursor.execute("ALTER TABLE config_nota ADD COLUMN pix_recebedor TEXT DEFAULT ''")
                if 'pix_cidade' not in colunas:
                    cursor.execute("ALTER TABLE config_nota ADD COLUMN pix_cidade TEXT DEFAULT ''")
                conn.commit()
    except Exception as e:
        logger.debug("Falha na migração automática de colunas Pix: %s", e)

# Rodar migração ao carregar o módulo
_garantir_migracao_pix()


# ============================================================
# CONSULTA DE PROGRAMA DE PONTOS (FIDELIDADE)
# ============================================================

def _obter_pontos_fidelidade(cliente_nome: str, total_venda: float) -> dict:
    """Consulta transacional no SQLite para obter pontuação do cliente e cálculo da venda atual."""
    # 1 linha para explicar o motivo: Consulta em tempo real garante a fidelidade de pontos impressa idêntica ao banco de dados.
    if not cliente_nome or cliente_nome == "Consumidor Final":
        return None
    
    try:
        db_path = os.path.join(get_app_data_path(), "estoque.db")
        if not os.path.exists(db_path):
            return None

        with sqlite3.connect(db_path, timeout=5.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM clientes WHERE nome = ?", (cliente_nome,))
            c_row = cursor.fetchone()
            if c_row:
                cliente_id = c_row[0]
                cursor.execute("SELECT pontos FROM fidelidade WHERE cliente_id = ?", (cliente_id,))
                f_row = cursor.fetchone()
                saldo_atual = f_row[0] if f_row else 0
                
                # Regra de negócio: 1 ponto a cada R$10,00 gastos
                pontos_ganhos = int(total_venda // 10)
                saldo_final = saldo_atual + pontos_ganhos
                return {
                    "ganhos": pontos_ganhos,
                    "saldo": saldo_final
                }
    except Exception as e:
        logger.warning("Falha ao consultar dados de fidelidade no SQLite: %s", e)
    return None


# ============================================================
# GERADOR DE CUPOM DINÂMICO
# ============================================================

def _gerar_cupom_desconto_dinamico(total_venda: float) -> str:
    """Retorna um cupom de desconto promocional se a venda atingir valor mínimo."""
    # 1 linha para explicar o motivo: Técnica de marketing integrado para estimular o retorno e fidelizar o cliente no PDV.
    if total_venda >= 150.0:
        return "VOLTEJA15"  # R$ 15 reais de desconto na próxima compra
    elif total_venda >= 70.0:
        return "CUPOM10"    # 10% de desconto
    return None


# ============================================================
# MÓDULOS DE GERAÇÃO DE COMPROVANTES
# ============================================================

def gerar_nota_txt(itens, cliente, total_venda, id_venda=None, desconto=0.0, abrir_arquivo=True, config_nota=None):
    """
    Gera recibo de venda térmico (formato vertical responsivo 58mm/80mm) com PIX e Fidelidade.
    """
    # 1 linha para explicar o motivo: Carregamos as propriedades da empresa configuradas ou aplicamos defaults elegantes.
    if config_nota:
        empresa_nome = config_nota.get('empresa_nome', 'Estoke Ao Cubo') or 'Estoke Ao Cubo'
        empresa_email = config_nota.get('empresa_email', '')
        empresa_telefone = config_nota.get('empresa_telefone', '')
        empresa_endereco = config_nota.get('empresa_endereco', '')
        empresa_cnpj = config_nota.get('empresa_cnpj', '')
        mensagem_rodape = config_nota.get('mensagem_rodape', 'Obrigado pela preferência!') or 'Obrigado pela preferência!'
        
        # Novas variáveis de Pix do estabelecimento
        pix_key = config_nota.get('pix_key', '')
        pix_recebedor = config_nota.get('pix_recebedor', empresa_nome)
        pix_cidade = config_nota.get('pix_cidade', 'SAO PAULO')
    else:
        empresa_nome = "Estoke Ao Cubo"
        empresa_email = ""
        empresa_telefone = ""
        empresa_endereco = ""
        empresa_cnpj = ""
        mensagem_rodape = "Obrigado pela preferência!"
        pix_key = ""
        pix_recebedor = "Estoke Ao Cubo"
        pix_cidade = "SAO PAULO"

    if not id_venda:
        id_venda = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        
    data_emissao = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    cliente_nome = cliente.get('nome', 'Consumidor Final') if cliente else 'Consumidor Final'
    cliente_contato = cliente.get('contato', '') if cliente else ''
    # 1 linha para explicar o motivo: Largura dinâmica baseada na impressora configurada (48 colunas para 80mm/i8, 32 para 58mm).
    try:
        from config import get_printer_config
        printer_cfg = get_printer_config()
        # Se page_width estiver definido nas configurações da impressora, usamos.
        # Caso contrário, se for escpos ou windows, o padrão para Elgin i8 (80mm) é 48 colunas, senão 32.
        width = int(printer_cfg.get('settings', {}).get('page_width', 48 if printer_cfg.get('type') in ('escpos', 'windows') else 32))
    except Exception:
        width = 32  
        
    line = "-" * width
    double_line = "=" * width
    
    content = []
    content.append(empresa_nome.upper().center(width))
    if empresa_cnpj:
        content.append(f"CNPJ: {empresa_cnpj}".center(width))
    if empresa_endereco:
        content.append(empresa_endereco.center(width))
    if empresa_telefone:
        content.append(f"Tel: {empresa_telefone}".center(width))
    if empresa_email:
        content.append(empresa_email.center(width))
    content.append(line)
    content.append(f"RECIBO #{id_venda}".center(width))
    content.append(f"Data: {data_emissao}".center(width))
    content.append(line)
    content.append(f"Cliente: {cliente_nome}")
    if cliente_contato:
        content.append(f"Contato: {cliente_contato}")
    content.append(line)
    content.append("ITENS ADQUIRIDOS".center(width))
    
    # Renderização formatada dos itens da compra
    for item in itens:
        nome = item['nome'][:28]
        qtd = item['qtd']
        preco = item['preco']
        subtotal = qtd * preco
        desc_item = item.get('desconto_item', 0.0)
        
        content.append(f" {nome}")
        content.append(f"  {qtd}x R${preco:.2f} = R${subtotal:.2f}")
        if desc_item > 0:
            content.append(f"  Desc: -R${desc_item:.2f}")
    
    content.append(line)
    
    # Subtotal bruto dos itens
    subtotal_itens = sum(item['qtd'] * item['preco'] for item in itens)
    
    if desconto > 0:
        content.append(f" Subtotal:  R${subtotal_itens:.2f}")
        content.append(f" Desconto: -R${desconto:.2f}")
        content.append(line)
    
    content.append(f" TOTAL: R${total_venda:.2f}".center(width))
    content.append(double_line)

    # Inclusão dos pontos de fidelidade
    fidelidade = _obter_pontos_fidelidade(cliente_nome, total_venda)
    if fidelidade:
        content.append("PROGRAMA DE FIDELIDADE".center(width))
        content.append(f" Pontos Ganhos: {fidelidade['ganhos']}")
        content.append(f" Saldo Atual: {fidelidade['saldo']} pts")
        content.append(double_line)

    # Inclusão de cupom promocional no rodapé da nota
    cupom = _gerar_cupom_desconto_dinamico(total_venda)
    if cupom:
        content.append("PARABENS! VOCE GANHOU UM CUPOM".center(width))
        content.append(f" Use: {cupom}".center(width))
        content.append(" na sua proxima compra!".center(width))
        content.append(line)

    content.append(mensagem_rodape.center(width))
    
    # Adicionar metadados do PIX caso a chave esteja configurada
    # 1 linha para explicar o motivo: O metadado é interpretado pelo driver de forma transparente sem poluir o visual do arquivo.
    pix_payload = ""
    if pix_key:
        pix_payload = gerar_payload_pix(pix_key, total_venda, pix_recebedor, pix_cidade, id_venda)
        if pix_payload:
            content.append(f"[PIX_PAYLOAD: {pix_payload}]")
    
    text_content = "\n".join(content)
    
    # Versão WhatsApp do Recibo
    wpp_content = _gerar_nota_whatsapp(
        itens, cliente_nome, cliente_contato, total_venda, id_venda,
        data_emissao, desconto, subtotal_itens,
        empresa_nome, empresa_email, empresa_telefone, empresa_endereco,
        mensagem_rodape, fidelidade, cupom, pix_payload
    )
    
    # Gravação do arquivo físico local de auditoria/reimpressão (TXT e HTML no Padrão do Drive)
    try:
        docs_path = os.path.join(os.path.expanduser("~"), "Documents", "EstokeAoCubo_Recibos")
        os.makedirs(docs_path, exist_ok=True)
        
        filename = f"Recibo_{id_venda}.txt"
        file_path = os.path.join(docs_path, filename)
        
        with open(file_path, "w", encoding="utf-8") as f:
            # Gravamos sem o metadado do payload no arquivo físico nítido
            f.write(re.sub(r'\[PIX_PAYLOAD:\s*[^\]]+\s*\]', '', text_content).strip())

        # Gerar também a versão HTML estilizada no Padrão de Design da Nota Fiscal
        html_content = gerar_nota_html(
            itens=itens,
            cliente=cliente,
            total_venda=total_venda,
            id_venda=id_venda,
            desconto=desconto,
            forma_pagamento=config_nota.get('forma_pagamento', 'PIX / Dinheiro') if config_nota else 'PIX / Dinheiro',
            config_nota=config_nota,
            pix_payload=pix_payload
        )
        html_file_path = os.path.join(docs_path, f"Recibo_{id_venda}.html")
        with open(html_file_path, "w", encoding="utf-8") as f_html:
            f_html.write(html_content)
            
        if abrir_arquivo:
            try:
                if os.name == 'nt':
                    os.startfile(html_file_path)
                else:
                    opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
                    subprocess.call([opener, html_file_path])
            except Exception:
                pass

        return file_path, text_content, wpp_content
    
    except Exception as e:
        logger.error("Erro ao gerar arquivo físico de nota: %s", e)
        return None, None, None


def _gerar_nota_whatsapp(itens, cliente_nome, cliente_contato, total_venda, id_venda,
                         data_emissao, desconto, subtotal_itens,
                         empresa_nome, empresa_email, empresa_telefone,
                         empresa_endereco, mensagem_rodape, fidelidade=None, cupom=None, pix_payload=None):
    """Gera texto de recibo com excelente formatação de fontes markdown para o WhatsApp."""
    partes = data_emissao.split(' ')
    data_str = partes[0] if len(partes) >= 1 else data_emissao
    hora_str = partes[1] if len(partes) >= 2 else ''
    hora_display = f" às {hora_str}" if hora_str else ''

    lines = []
    lines.append(f"🧾 *COMPROVANTE DE COMPRA | {empresa_nome.upper()}*")
    lines.append("Olá! Segue o detalhamento de seu pedido:")
    lines.append("")
    lines.append(f"Pedido: *#{id_venda}*")
    lines.append(f"Data: {data_str}{hora_display}")
    lines.append(f"Cliente: *{cliente_nome}*")
    lines.append("")

    if len(itens) == 1:
        lines.append('🛒 *ITEM ADQUIRIDO:*')
    else:
        lines.append('🛒 *ITENS ADQUIRIDOS:*')

    for item in itens:
        nome = item['nome']
        qtd = item['qtd']
        preco = item['preco']
        subtotal = qtd * preco
        desc_item = item.get('desconto_item', 0.0)
        lines.append(f"• {qtd}x *{nome}*")
        if desc_item > 0:
            lines.append(f"  Preço: R$ {subtotal:.2f} _(desc: -R$ {desc_item:.2f})_")
        else:
            lines.append(f"  Preço: R$ {subtotal:.2f}")

    lines.append("")

    if desconto > 0:
        lines.append(f"Subtotal bruto: R$ {subtotal_itens:.2f}")
        lines.append(f"Desconto concedido: *-R$ {desconto:.2f}*")

    lines.append(f"💰 *VALOR TOTAL PAGO: R$ {total_venda:.2f}*")
    lines.append("")

    # Fidelidade
    if fidelidade:
        lines.append("⭐ *FIDELIDADE ESTOKE:*")
        lines.append(f" Pontos Ganhos nesta Compra: *{fidelidade['ganhos']}*")
        lines.append(f" Saldo Total Acumulado: *{fidelidade['saldo']} pontos*")
        lines.append("")

    # Cupom de desconto
    if cupom:
        lines.append("🎁 *PRESENTE PARA VOCÊ:*")
        lines.append(f" Ganhe desconto na próxima compra com o cupom: *{cupom}*")
        lines.append("")

    # Link do Pix para cópia
    if pix_payload:
        lines.append("⚡ *PIX COPIA E COLA:*")
        lines.append(f"`{pix_payload}`")
        lines.append("")

    if empresa_endereco:
        lines.append(f"📍 {empresa_endereco}")
    if empresa_telefone:
        lines.append(f"📞 Contato/Suporte: {empresa_telefone}")
    if empresa_email:
        lines.append(f"✉️ E-mail: {empresa_email}")

    lines.append("")
    lines.append(f"_{mensagem_rodape}_")

    return '\n'.join(lines)


def gerar_nota_servico(descricao, valor, cliente, id_servico=None, forma_pagamento='Dinheiro',
                       observacao='', abrir_arquivo=True, config_nota=None):
    """
    Gera recibo de nota de serviço com suporte a PIX, Fidelidade e QR Code.
    """
    if config_nota:
        empresa_nome = config_nota.get('empresa_nome', 'Estoke Ao Cubo') or 'Estoke Ao Cubo'
        empresa_email = config_nota.get('empresa_email', '')
        empresa_telefone = config_nota.get('empresa_telefone', '')
        empresa_endereco = config_nota.get('empresa_endereco', '')
        empresa_cnpj = config_nota.get('empresa_cnpj', '')
        mensagem_rodape = config_nota.get('mensagem_rodape', 'Obrigado pela preferência!') or 'Obrigado pela preferência!'
        
        pix_key = config_nota.get('pix_key', '')
        pix_recebedor = config_nota.get('pix_recebedor', empresa_nome)
        pix_cidade = config_nota.get('pix_cidade', 'SAO PAULO')
    else:
        empresa_nome = "Estoke Ao Cubo"
        empresa_email = ""
        empresa_telefone = ""
        empresa_endereco = ""
        empresa_cnpj = ""
        mensagem_rodape = "Obrigado pela preferência!"
        pix_key = ""
        pix_recebedor = "Estoke Ao Cubo"
        pix_cidade = "SAO PAULO"

    if not id_servico:
        id_servico = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

    data_emissao = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    cliente_nome = cliente.get('nome', 'Consumidor Final') if cliente else 'Consumidor Final'
    cliente_contato = cliente.get('contato', '') if cliente else ''

    # 1 linha para explicar o motivo: Largura dinâmica baseada na impressora configurada (48 colunas para 80mm/i8, 32 para 58mm).
    try:
        from config import get_printer_config
        printer_cfg = get_printer_config()
        # Se page_width estiver definido nas configurações da impressora, usamos.
        # Caso contrário, se for escpos ou windows, o padrão para Elgin i8 (80mm) é 48 colunas, senão 32.
        width = int(printer_cfg.get('settings', {}).get('page_width', 48 if printer_cfg.get('type') in ('escpos', 'windows') else 32))
    except Exception:
        width = 32  
        
    line = "-" * width
    double_line = "=" * width

    content = []
    content.append(empresa_nome.upper().center(width))
    if empresa_cnpj:
        content.append(f"CNPJ: {empresa_cnpj}".center(width))
    if empresa_endereco:
        content.append(empresa_endereco.center(width))
    if empresa_telefone:
        content.append(f"Tel: {empresa_telefone}".center(width))
    if empresa_email:
        content.append(empresa_email.center(width))
    content.append(line)
    content.append("COMPROVANTE DE SERVIÇO".center(width))
    content.append(f"Nº {id_servico}".center(width))
    content.append(f"Data: {data_emissao}".center(width))
    content.append(line)
    content.append(f"Cliente: {cliente_nome}")
    if cliente_contato:
        content.append(f"Contato: {cliente_contato}")
    content.append(line)
    content.append("SERVIÇO PRESTADO".center(width))
    
    desc_lines = [descricao[i:i+30] for i in range(0, len(descricao), 30)]
    for dl in desc_lines:
        content.append(f" {dl}")
    content.append("")
    if observacao:
        content.append(f" Obs: {observacao[:28]}")
        if len(observacao) > 28:
            content.append(f"      {observacao[28:56]}")
    content.append(line)
    content.append(f" Pgto: {forma_pagamento}")
    content.append(f" TOTAL: R${valor:.2f}".center(width))
    content.append(double_line)

    # Inclusão dos pontos de fidelidade
    fidelidade = _obter_pontos_fidelidade(cliente_nome, valor)
    if fidelidade:
        content.append("PROGRAMA DE FIDELIDADE".center(width))
        content.append(f" Pontos Ganhos: {fidelidade['ganhos']}")
        content.append(f" Saldo Atual: {fidelidade['saldo']} pts")
        content.append(double_line)

    content.append(mensagem_rodape.center(width))

    # Anexar metadados Pix de nota de serviço
    pix_payload = ""
    if pix_key:
        pix_payload = gerar_payload_pix(pix_key, valor, pix_recebedor, pix_cidade, id_servico)
        if pix_payload:
            content.append(f"[PIX_PAYLOAD: {pix_payload}]")

    text_content = "\n".join(content)

    # Versão WhatsApp da Nota de Serviço
    partes = data_emissao.split(' ')
    data_str = partes[0] if len(partes) >= 1 else data_emissao
    hora_str = partes[1] if len(partes) >= 2 else ''
    hora_display = f" às {hora_str}" if hora_str else ''

    wpp = []
    wpp.append(f"🛠️ *NOTA DE SERVIÇO | {empresa_nome.upper()}*")
    wpp.append("Olá! Segue o detalhamento de seu serviço:")
    wpp.append("")
    wpp.append(f"Código OS: *#{id_servico}*")
    wpp.append(f"Data: {data_str}{hora_display}")
    wpp.append(f"Cliente: *{cliente_nome}*")
    wpp.append("")
    wpp.append("*SERVIÇO PRESTADO:*")
    wpp.append(f"_{descricao}_")
    if observacao:
        wpp.append(f"Observações: `{observacao}`")
    wpp.append("")
    wpp.append(f"Forma de Pagamento: {forma_pagamento}")
    wpp.append(f"💰 *VALOR TOTAL: R$ {valor:.2f}*")
    wpp.append("")

    if fidelidade:
        wpp.append("⭐ *FIDELIDADE ESTOKE:*")
        wpp.append(f" Pontos Ganhos: *{fidelidade['ganhos']}*")
        wpp.append(f" Saldo Atualizado: *{fidelidade['saldo']} pontos*")
        wpp.append("")

    if pix_payload:
        wpp.append("⚡ *PIX COPIA E COLA:*")
        wpp.append(f"`{pix_payload}`")
        wpp.append("")

    if empresa_telefone:
        wpp.append(f"📞 Telefone/Suporte: {empresa_telefone}")
    if empresa_email:
        wpp.append(f"✉️ E-mail: {empresa_email}")
    wpp.append("")
    wpp.append(f"_{mensagem_rodape}_")

    wpp_content = '\n'.join(wpp)

    # Gravação física local
    try:
        docs_path = os.path.join(os.path.expanduser("~"), "Documents", "EstokeAoCubo_Recibos")
        os.makedirs(docs_path, exist_ok=True)

        filename = f"Servico_{id_servico}.txt"
        file_path = os.path.join(docs_path, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(re.sub(r'\[PIX_PAYLOAD:\s*[^\]]+\s*\]', '', text_content).strip())

        if abrir_arquivo:
            try:
                if os.name == 'nt':
                    os.startfile(file_path)
                else:
                    opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
                    subprocess.call([opener, file_path])
            except Exception:
                pass

        return file_path, text_content, wpp_content

    except Exception as e:
        logger.error("Erro ao gravar nota de serviço física: %s", e)
        return None, None, None


# ============================================================
# PADRÃO DE DESIGN NOTA FISCAL (MODERNO / INTERATIVO / WEB)
# ============================================================

def gerar_nota_html(itens, cliente, total_venda, id_venda=None, desconto=0.0,
                    forma_pagamento="PIX / Dinheiro", config_nota=None,
                    pix_payload="", qr_code_base64=""):
    """
    Gera o documento HTML interativo e responsivo no Padrão de Design da Nota Fiscal (Drive).
    Inclui animações suaves, textura de papel térmico, código de barras SVG,
    QR Code Pix com Copia e Cola, e suporte a impressão direta em PDF.
    """
    if config_nota:
        empresa_nome = config_nota.get('empresa_nome', 'Estoke Ao Cubo') or 'Estoke Ao Cubo'
        empresa_email = config_nota.get('empresa_email', '')
        empresa_telefone = config_nota.get('empresa_telefone', '')
        empresa_endereco = config_nota.get('empresa_endereco', '')
        empresa_cnpj = config_nota.get('empresa_cnpj', '')
        mensagem_rodape = config_nota.get('mensagem_rodape', 'Obrigado pela preferência!') or 'Obrigado pela preferência!'
    else:
        empresa_nome = "Estoke Ao Cubo"
        empresa_email = ""
        empresa_telefone = ""
        empresa_endereco = ""
        empresa_cnpj = ""
        mensagem_rodape = "Obrigado pela preferência!"

    if not id_venda:
        id_venda = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

    data_emissao = datetime.datetime.now().strftime("%d/%m/%Y às %H:%M:%S")
    cliente_nome_raw = cliente.get('nome', 'Consumidor Final') if isinstance(cliente, dict) else (cliente or 'Consumidor Final')
    cliente_contato_raw = cliente.get('contato', '') if isinstance(cliente, dict) else ''

    subtotal_bruto = sum(item.get('qtd', 1) * item.get('preco', 0.0) for item in itens)
    fidelidade = _obter_pontos_fidelidade(cliente_nome_raw, total_venda)
    cupom = _gerar_cupom_desconto_dinamico(total_venda)

    # Sanitização HTML contra XSS em todos os campos interpolados
    import html as _html
    empresa_nome = _html.escape(str(empresa_nome))
    empresa_email = _html.escape(str(empresa_email))
    empresa_telefone = _html.escape(str(empresa_telefone))
    empresa_endereco = _html.escape(str(empresa_endereco))
    empresa_cnpj = _html.escape(str(empresa_cnpj))
    mensagem_rodape = _html.escape(str(mensagem_rodape))
    cliente_nome = _html.escape(str(cliente_nome_raw))
    cliente_contato = _html.escape(str(cliente_contato_raw))
    forma_pagamento = _html.escape(str(forma_pagamento or "PIX / Dinheiro"))
    id_venda = _html.escape(str(id_venda))
    pix_payload = _html.escape(str(pix_payload or ""))

    # Renderizar itens em HTML
    itens_html = []
    for item in itens:
        nome = _html.escape(str(item.get('nome', 'Produto')))
        qtd = item.get('qtd', 1)
        preco = item.get('preco', 0.0)
        sub = qtd * preco
        desc_item = item.get('desconto_item', 0.0)
        
        item_block = f"""
        <div class="line-item">
            <div class="item-header">
                <span class="item-name"><strong>{qtd}x</strong> {nome}</span>
                <span class="item-price">R$ {sub:.2f}</span>
            </div>
            <div class="item-detail">R$ {preco:.2f} cada {f'(Desc: -R$ {desc_item:.2f})' if desc_item > 0 else ''}</div>
        </div>
        """
        itens_html.append(item_block)
    
    itens_rendered = "\n".join(itens_html)

    # QR Code e Pix
    pix_section = ""
    if qr_code_base64:
        pix_section = f"""
        <div class="pix-card-section">
            <div class="pix-badge"><i class="fas fa-qrcode"></i> PAGAMENTO VIA PIX</div>
            <img src="data:image/png;base64,{qr_code_base64}" alt="QR Code Pix" class="pix-qr-img" />
            <div class="pix-copy-box">
                <input type="text" id="pix-emv-input" readonly value="{pix_payload}" />
                <button type="button" class="btn-copy-pix" onclick="copiarPix()">
                    <i class="fas fa-copy"></i> Copiar Código PIX
                </button>
            </div>
        </div>
        """
    elif pix_payload:
        pix_section = f"""
        <div class="pix-card-section">
            <div class="pix-badge"><i class="fas fa-bolt"></i> PIX COPIA E COLA</div>
            <div class="pix-copy-box">
                <input type="text" id="pix-emv-input" readonly value="{pix_payload}" />
                <button type="button" class="btn-copy-pix" onclick="copiarPix()">
                    <i class="fas fa-copy"></i> Copiar Código PIX
                </button>
            </div>
        </div>
        """

    # Fidelidade & Cupom
    fidelidade_section = ""
    if fidelidade:
        fidelidade_section = f"""
        <div class="loyalty-box">
            <div class="loyalty-title"><i class="fas fa-star"></i> PROGRAMA DE FIDELIDADE</div>
            <div class="loyalty-row"><span>Pontos nesta compra:</span><strong>+{fidelidade['ganhos']}</strong></div>
            <div class="loyalty-row"><span>Saldo acumulado:</span><strong>{fidelidade['saldo']} pts</strong></div>
        </div>
        """

    cupom_section = ""
    if cupom:
        cupom_section = f"""
        <div class="coupon-box">
            <div class="coupon-title">🎁 CUPOM PARA PRÓXIMA COMPRA</div>
            <div class="coupon-code">{cupom}</div>
        </div>
        """

    # Documento HTML Completo
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Recibo #{id_venda} — {empresa_nome}</title>
    <!-- Google Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
    <!-- FontAwesome Icons -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <style>
        :root {{
            --bg-body: #09090b;
            --card-bg: #18181b;
            --card-border: rgba(255, 255, 255, 0.1);
            --paper-bg: #fafaf9;
            --paper-text: #18181b;
            --accent-green: #10b981;
            --accent-amber: #f59e0b;
            --accent-blue: #3b82f6;
            --text-muted: #a1a1aa;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-body);
            color: #ffffff;
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-start;
            padding: 40px 15px;
            background-image: radial-gradient(circle at 50% 10%, rgba(16, 185, 129, 0.05), transparent 40%),
                              radial-gradient(circle at 80% 80%, rgba(59, 130, 246, 0.04), transparent 40%);
        }}

        .receipt-container {{
            width: 100%;
            max-width: 420px;
            display: flex;
            flex-direction: column;
            align-items: center;
            position: relative;
        }}

        /* Terminal Top Card */
        .printer-terminal-card {{
            width: 100%;
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 24px;
            position: relative;
            z-index: 20;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.85);
            backdrop-filter: blur(12px);
        }}

        .terminal-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
        }}

        .brand-badge {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .brand-icon-box {{
            width: 38px;
            height: 38px;
            border-radius: 12px;
            background: #27272a;
            border: 1px solid rgba(255, 255, 255, 0.08);
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--accent-green);
            font-size: 16px;
            box-shadow: inset 0 1px 2px rgba(255,255,255,0.1);
        }}

        .brand-text {{
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            color: #e4e4e7;
        }}

        .live-status-pill {{
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 5px 12px;
            border-radius: 99px;
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid rgba(16, 185, 129, 0.3);
            font-size: 11px;
            font-weight: 600;
            color: var(--accent-green);
        }}

        .live-dot {{
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: var(--accent-green);
            animation: pulseDot 1.8s infinite;
        }}

        @keyframes pulseDot {{
            0%, 100% {{ opacity: 1; transform: scale(1); }}
            50% {{ opacity: 0.4; transform: scale(1.3); }}
        }}

        .order-summary-row {{
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
            margin-bottom: 20px;
        }}

        .order-id-label {{
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-muted);
            margin-bottom: 2px;
        }}

        .order-id-val {{
            font-family: 'Space Grotesk', sans-serif;
            font-size: 18px;
            font-weight: 700;
            color: #ffffff;
        }}

        .total-amount-box {{
            text-align: right;
        }}

        .total-val {{
            font-family: 'Space Grotesk', sans-serif;
            font-size: 22px;
            font-weight: 800;
            color: var(--accent-green);
            text-shadow: 0 0 20px rgba(16, 185, 129, 0.3);
        }}

        /* Printer Slit Depth */
        .printer-slit {{
            height: 8px;
            width: 100%;
            background: #000000;
            border-radius: 99px;
            border: 1px solid #27272a;
            margin-top: 15px;
            margin-bottom: -28px;
            box-shadow: inset 0 2px 4px rgba(0,0,0,0.8);
            position: relative;
        }}

        /* Thermal Receipt Paper */
        .thermal-paper {{
            width: 92%;
            background: var(--paper-bg);
            color: var(--paper-text);
            font-family: 'Space Mono', 'Consolas', monospace;
            font-size: 11px;
            line-height: 1.5;
            padding: 24px 22px;
            position: relative;
            z-index: 10;
            margin-top: 0;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
            border-radius: 0 0 6px 6px;
            animation: printSlideDown 1.2s cubic-bezier(0.16, 1, 0.3, 1) forwards;
            transform-origin: top center;
        }}

        @keyframes printSlideDown {{
            0% {{
                opacity: 0;
                transform: translateY(-20px) scaleY(0.6);
            }}
            100% {{
                opacity: 1;
                transform: translateY(0) scaleY(1);
            }}
        }}

        .paper-header {{
            text-align: center;
            margin-bottom: 12px;
        }}

        .paper-store-name {{
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }}

        .paper-subtitle {{
            font-size: 9px;
            color: #52525b;
            margin-top: 2px;
        }}

        .paper-divider {{
            border-bottom: 1px dashed #d4d4d8;
            margin: 10px 0;
        }}

        .line-item {{
            margin-bottom: 6px;
        }}

        .item-header {{
            display: flex;
            justify-content: space-between;
            font-weight: 700;
        }}

        .item-detail {{
            font-size: 10px;
            color: #71717a;
        }}

        .totals-breakdown {{
            font-size: 11px;
            margin: 8px 0;
        }}

        .totals-row {{
            display: flex;
            justify-content: space-between;
            margin: 3px 0;
            color: #52525b;
        }}

        .totals-row.final {{
            font-size: 13px;
            font-weight: 700;
            color: #000000;
            padding-top: 6px;
            border-top: 1px solid #e4e4e7;
            margin-top: 6px;
        }}

        .meta-info {{
            font-size: 10px;
            color: #71717a;
            margin: 8px 0;
        }}

        .meta-row {{
            display: flex;
            justify-content: space-between;
            margin: 2px 0;
        }}

        /* Barcode SVG */
        .barcode-wrap {{
            text-align: center;
            margin: 14px 0 8px 0;
            display: flex;
            flex-direction: column;
            align-items: center;
        }}

        .barcode-svg {{
            width: 200px;
            height: 35px;
        }}

        .barcode-number {{
            font-size: 9px;
            letter-spacing: 3px;
            color: #71717a;
            margin-top: 2px;
        }}

        /* PIX Section */
        .pix-card-section {{
            background: #ffffff;
            border: 1px solid #e4e4e7;
            border-radius: 8px;
            padding: 12px;
            text-align: center;
            margin: 10px 0;
        }}

        .pix-badge {{
            font-size: 10px;
            font-weight: 700;
            color: #059669;
            margin-bottom: 8px;
        }}

        .pix-qr-img {{
            max-width: 140px;
            height: auto;
            margin: 0 auto 8px auto;
            display: block;
            border-radius: 4px;
        }}

        .pix-copy-box {{
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}

        .pix-copy-box input {{
            width: 100%;
            padding: 6px;
            font-size: 9px;
            background: #f4f4f5;
            border: 1px solid #d4d4d8;
            border-radius: 4px;
            font-family: monospace;
            color: #3f3f46;
        }}

        .btn-copy-pix {{
            background: #059669;
            color: #ffffff;
            border: none;
            padding: 8px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 700;
            cursor: pointer;
            transition: background 0.2s;
        }}

        .btn-copy-pix:hover {{
            background: #047857;
        }}

        /* Fidelidade & Cupom */
        .loyalty-box {{
            background: #fef3c7;
            border: 1px solid #fde68a;
            border-radius: 6px;
            padding: 8px;
            margin: 8px 0;
            color: #92400e;
        }}

        .loyalty-title {{
            font-weight: 700;
            font-size: 10px;
            margin-bottom: 4px;
        }}

        .loyalty-row {{
            display: flex;
            justify-content: space-between;
            font-size: 10px;
        }}

        .coupon-box {{
            background: #f0fdf4;
            border: 1px dashed #86efac;
            border-radius: 6px;
            padding: 8px;
            text-align: center;
            margin: 8px 0;
            color: #166534;
        }}

        .coupon-code {{
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 2px;
            margin-top: 2px;
        }}

        .footer-note {{
            text-align: center;
            font-size: 9px;
            color: #a1a1aa;
            margin-top: 8px;
            text-transform: uppercase;
        }}

        /* Action Buttons */
        .actions-panel {{
            display: flex;
            gap: 10px;
            margin-top: 20px;
            width: 100%;
            justify-content: center;
            flex-wrap: wrap;
        }}

        .action-btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 10px 18px;
            border-radius: 99px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            text-decoration: none;
            border: 1px solid transparent;
        }}

        .action-btn-primary {{
            background: #27272a;
            color: #ffffff;
            border-color: rgba(255, 255, 255, 0.15);
        }}

        .action-btn-primary:hover {{
            background: #3f3f46;
            transform: translateY(-2px);
        }}

        .action-btn-accent {{
            background: var(--accent-green);
            color: #000000;
            font-weight: 700;
        }}

        .action-btn-accent:hover {{
            background: #34d399;
            transform: translateY(-2px);
        }}

        /* Toast notification */
        .toast {{
            position: fixed;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%) translateY(100px);
            background: #18181b;
            color: #fff;
            padding: 12px 24px;
            border-radius: 99px;
            border: 1px solid rgba(255,255,255,0.2);
            font-size: 13px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
            z-index: 1000;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .toast.show {{
            transform: translateX(-50%) translateY(0);
        }}

        /* Print Media Styles */
        @media print {{
            body {{
                background: #ffffff !important;
                color: #000000 !important;
                padding: 0 !important;
            }}
            .printer-terminal-card, .actions-panel, .toast {{
                display: none !important;
            }}
            .receipt-container {{
                max-width: 100% !important;
                width: 100% !important;
            }}
            .thermal-paper {{
                width: 100% !important;
                box-shadow: none !important;
                padding: 10px !important;
                animation: none !important;
            }}
        }}
    </style>
</head>
<body>

    <div class="receipt-container">
        <!-- Terminal Top Card -->
        <div class="printer-terminal-card">
            <div class="terminal-header">
                <div class="brand-badge">
                    <div class="brand-icon-box">
                        <i class="fas fa-cube"></i>
                    </div>
                    <span class="brand-text">{empresa_nome}</span>
                </div>
                <div class="live-status-pill">
                    <div class="live-dot"></div>
                    <span>Concluído</span>
                </div>
            </div>

            <div class="order-summary-row">
                <div>
                    <div class="order-id-label">Recibo da Venda</div>
                    <div class="order-id-val">#{id_venda}</div>
                </div>
                <div class="total-amount-box">
                    <div class="order-id-label">Total</div>
                    <div class="total-val">R$ {total_venda:.2f}</div>
                </div>
            </div>

            <div class="printer-slit"></div>
        </div>

        <!-- Thermal Receipt Paper Component -->
        <div class="thermal-paper" id="thermal-paper">
            <div class="paper-header">
                <div class="paper-store-name">{empresa_nome}</div>
                {f'<div class="paper-subtitle">CNPJ: {empresa_cnpj}</div>' if empresa_cnpj else ''}
                {f'<div class="paper-subtitle">{empresa_endereco}</div>' if empresa_endereco else ''}
                {f'<div class="paper-subtitle">Tel: {empresa_telefone}</div>' if empresa_telefone else ''}
                <div class="paper-subtitle">{data_emissao}</div>
            </div>

            <div class="paper-divider"></div>

            <div class="meta-info">
                <div class="meta-row"><span>Cliente:</span><strong>{cliente_nome}</strong></div>
                {f'<div class="meta-row"><span>Contato:</span><span>{cliente_contato}</span></div>' if cliente_contato else ''}
                <div class="meta-row"><span>Pagamento:</span><span>{forma_pagamento}</span></div>
            </div>

            <div class="paper-divider"></div>

            <!-- Itens -->
            <div class="items-list">
                {itens_rendered}
            </div>

            <div class="paper-divider"></div>

            <!-- Totais -->
            <div class="totals-breakdown">
                <div class="totals-row">
                    <span>Subtotal bruto:</span>
                    <span>R$ {subtotal_bruto:.2f}</span>
                </div>
                {f'<div class="totals-row" style="color:#dc2626;"><span>Desconto aplicado:</span><span>-R$ {desconto:.2f}</span></div>' if desconto > 0 else ''}
                <div class="totals-row final">
                    <span>TOTAL PAGO:</span>
                    <span>R$ {total_venda:.2f}</span>
                </div>
            </div>

            {fidelidade_section}
            {cupom_section}
            {pix_section}

            <!-- Barcode SVG -->
            <div class="barcode-wrap">
                <svg class="barcode-svg" viewBox="0 0 160 30" preserveAspectRatio="none">
                    <rect x="0" y="0" width="3" height="30" fill="#000" />
                    <rect x="5" y="0" width="1.5" height="30" fill="#000" />
                    <rect x="9" y="0" width="4" height="30" fill="#000" />
                    <rect x="15" y="0" width="2" height="30" fill="#000" />
                    <rect x="19" y="0" width="1" height="30" fill="#000" />
                    <rect x="23" y="0" width="5" height="30" fill="#000" />
                    <rect x="31" y="0" width="2" height="30" fill="#000" />
                    <rect x="36" y="0" width="3" height="30" fill="#000" />
                    <rect x="42" y="0" width="1.5" height="30" fill="#000" />
                    <rect x="46" y="0" width="4.5" height="30" fill="#000" />
                    <rect x="53" y="0" width="2" height="30" fill="#000" />
                    <rect x="58" y="0" width="1" height="30" fill="#000" />
                    <rect x="62" y="0" width="3.5" height="30" fill="#000" />
                    <rect x="68" y="0" width="2" height="30" fill="#000" />
                    <rect x="73" y="0" width="4" height="30" fill="#000" />
                    <rect x="79" y="0" width="1.5" height="30" fill="#000" />
                    <rect x="83" y="0" width="5" height="30" fill="#000" />
                    <rect x="91" y="0" width="2" height="30" fill="#000" />
                    <rect x="96" y="0" width="3" height="30" fill="#000" />
                    <rect x="102" y="0" width="1" height="30" fill="#000" />
                    <rect x="106" y="0" width="4" height="30" fill="#000" />
                    <rect x="113" y="0" width="2.5" height="30" fill="#000" />
                    <rect x="118" y="0" width="1.5" height="30" fill="#000" />
                    <rect x="122" y="0" width="5" height="30" fill="#000" />
                    <rect x="130" y="0" width="2" height="30" fill="#000" />
                    <rect x="135" y="0" width="3" height="30" fill="#000" />
                    <rect x="141" y="0" width="1" height="30" fill="#000" />
                    <rect x="145" y="0" width="4" height="30" fill="#000" />
                    <rect x="152" y="0" width="2" height="30" fill="#000" />
                    <rect x="157" y="0" width="3" height="30" fill="#000" />
                </svg>
                <span class="barcode-number">{id_venda}</span>
            </div>

            <div class="footer-note">{mensagem_rodape}</div>
        </div>

        <!-- Action Buttons -->
        <div class="actions-panel">
            <button class="action-btn action-btn-accent" onclick="window.print()">
                <i class="fas fa-print"></i> Imprimir / Salvar PDF
            </button>
            <button class="action-btn action-btn-primary" onclick="destacarRecibo()">
                <i class="fas fa-cut"></i> Destacar Recibo
            </button>
        </div>
    </div>

    <div class="toast" id="toast-msg">
        <i class="fas fa-check-circle" style="color:var(--accent-green);"></i>
        <span id="toast-text">Código copiado!</span>
    </div>

    <script>
        function copiarPix() {{
            const input = document.getElementById('pix-emv-input');
            if (!input) return;
            input.select();
            navigator.clipboard.writeText(input.value).then(() => {{
                showToast("Código PIX copiado com sucesso!");
            }}).catch(() => {{
                document.execCommand('copy');
                showToast("Código PIX copiado!");
            }});
        }}

        function destacarRecibo() {{
            const paper = document.getElementById('thermal-paper');
            if (!paper) return;
            paper.style.transform = 'translateY(16px) rotate(1.2deg)';
            paper.style.boxShadow = '0 30px 60px rgba(0, 0, 0, 0.6)';
            tocarSomRasgo();
            showToast("Recibo destacado!");
        }}

        function showToast(text) {{
            const toast = document.getElementById('toast-msg');
            const toastText = document.getElementById('toast-text');
            toastText.textContent = text;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 2500);
        }}

        // Som de rasgo com Web Audio API
        function tocarSomRasgo() {{
            try {{
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const bufferSize = ctx.sampleRate * 0.15;
                const buffer = ctx.createBuffer(1, bufferSize, ctx.sampleRate);
                const data = buffer.getChannelData(0);
                for (let i = 0; i < bufferSize; i++) {{
                    data[i] = (Math.random() * 2 - 1) * Math.exp(-i / (bufferSize * 0.3));
                }}
                const noise = ctx.createBufferSource();
                noise.buffer = buffer;
                const filter = ctx.createBiquadFilter();
                filter.type = 'bandpass';
                filter.frequency.value = 1200;
                noise.connect(filter);
                filter.connect(ctx.destination);
                noise.start();
            }} catch(e) {{}}
        }}
    </script>
</body>
</html>
"""
    return html
