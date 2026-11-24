import os
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

def gerar_pdf_fechamento(resultado, filepath):
    """Gera um relatório PDF do fechamento de caixa usando ReportLab."""
    doc = SimpleDocTemplate(
        filepath,
        pagesize=A4,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40
    )
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'TitleStyle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=20,
        textColor=colors.HexColor('#0A4174'),
        spaceAfter=15
    )
    
    section_style = ParagraphStyle(
        'SectionStyle',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=12,
        textColor=colors.HexColor('#0A4174'),
        spaceBefore=15,
        spaceAfter=10
    )
    
    normal_style = ParagraphStyle(
        'NormalStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        textColor=colors.HexColor('#333333'),
        spaceAfter=6
    )
    
    bold_style = ParagraphStyle(
        'BoldStyle',
        parent=normal_style,
        fontName='Helvetica-Bold'
    )
    
    white_bold_style = ParagraphStyle(
        'WhiteBoldStyle',
        parent=bold_style,
        textColor=colors.white
    )
    
    elements = []
    
    # Title
    elements.append(Paragraph("Relatório de Fechamento de Caixa", title_style))
    elements.append(Paragraph(f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}", normal_style))
    elements.append(Spacer(1, 15))
    
    # General Info Table
    data_geral = [
        [Paragraph("<b>ID do Caixa:</b>", normal_style), Paragraph(str(resultado.get('id', '')), normal_style),
         Paragraph("<b>Status:</b>", normal_style), Paragraph("Fechado", normal_style)],
        [Paragraph("<b>Abertura:</b>", normal_style), Paragraph(str(resultado.get('data_abertura', ''))[:16].replace('T', ' '), normal_style),
         Paragraph("<b>Fechamento:</b>", normal_style), Paragraph(datetime.now().strftime('%Y-%m-%d %H:%M'), normal_style)],
        [Paragraph("<b>Operador ID:</b>", normal_style), Paragraph(str(resultado.get('usuario_id', '1')), normal_style),
         Paragraph("<b>Loja ID:</b>", normal_style), Paragraph(str(resultado.get('loja_id', '1')), normal_style)]
    ]
    t_geral = Table(data_geral, colWidths=[100, 160, 100, 160])
    t_geral.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('LINEBELOW', (0,-1), (-1,-1), 1, colors.HexColor('#CCCCCC'))
    ]))
    elements.append(t_geral)
    elements.append(Spacer(1, 15))
    
    # Financial breakdown
    elements.append(Paragraph("Resumo Financeiro", section_style))
    
    total_vendas = resultado.get('total_vendas', 0.0)
    valor_abertura = resultado.get('valor_abertura', 0.0)
    total_dinheiro = resultado.get('total_dinheiro', 0.0)
    total_pix = resultado.get('total_pix', 0.0)
    total_cartao = resultado.get('total_cartao', 0.0)
    total_fiado = resultado.get('total_fiado', 0.0)
    total_sangrias = resultado.get('total_sangrias', 0.0)
    esperado = resultado.get('esperado', 0.0)
    informado = resultado.get('informado', 0.0)
    diferenca = resultado.get('diferenca', 0.0)
    
    data_financeira = [
        [Paragraph("Descrição", white_bold_style), Paragraph("Valor (R$)", white_bold_style)],
        [Paragraph("Saldo Inicial (Troco de Abertura)", normal_style), Paragraph(f"{valor_abertura:.2f}", normal_style)],
        [Paragraph("Vendas em Dinheiro (+)", normal_style), Paragraph(f"{total_dinheiro:.2f}", normal_style)],
        [Paragraph("Vendas em PIX (+)", normal_style), Paragraph(f"{total_pix:.2f}", normal_style)],
        [Paragraph("Vendas em Cartão (+)", normal_style), Paragraph(f"{total_cartao:.2f}", normal_style)],
        [Paragraph("Vendas em Fiado (+)", normal_style), Paragraph(f"{total_fiado:.2f}", normal_style)],
        [Paragraph("Total de Sangrias (-)", normal_style), Paragraph(f"{total_sangrias:.2f}", normal_style)],
        [Paragraph("<b>Saldo Final Esperado (Dinheiro)</b>", bold_style), Paragraph(f"<b>{esperado:.2f}</b>", bold_style)],
        [Paragraph("<b>Saldo Final Declarado (Dinheiro)</b>", bold_style), Paragraph(f"<b>{informado:.2f}</b>", bold_style)],
        [Paragraph("<b>Diferença (Quebra/Sobra)</b>", bold_style), Paragraph(f"<b>{diferenca:.2f}</b>", bold_style)]
    ]
    
    t_fin = Table(data_financeira, colWidths=[350, 170])
    t_fin.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0A4174')),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BACKGROUND', (0,-3), (-1,-1), colors.HexColor('#F5F5F5')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E0E0E0')),
    ]))
    
    elements.append(t_fin)
    elements.append(Spacer(1, 15))

    # Conciliação Automática PIX (Mercado Pago)
    conc_pix = resultado.get("conciliacao_pix")
    if isinstance(conc_pix, dict):
        elements.append(Paragraph("Conciliação Automática PIX (Mercado Pago)", section_style))
        total_pix_pdv = float(conc_pix.get("total_pix_pdv", 0.0) or 0.0)
        total_pix_mp = float(conc_pix.get("total_pix_mp", 0.0) or 0.0)
        dif_pix = float(conc_pix.get("diferenca_total", 0.0) or 0.0)
        exatos = conc_pix.get("conciliados_exatos") or []
        aprox = conc_pix.get("conciliados_aproximados") or []
        sem_wh = conc_pix.get("vendas_sem_webhook") or []
        sem_venda = conc_pix.get("webhooks_sem_venda") or []
        div_val = conc_pix.get("divergencia_valor") or []

        data_conc = [
            [Paragraph("Indicador de Conciliação PIX", white_bold_style), Paragraph("Resultado", white_bold_style)],
            [Paragraph("Total Vendas PIX no PDV", normal_style), Paragraph(f"R$ {total_pix_pdv:.2f} ({conc_pix.get('qtd_vendas_pix', 0)} venda(s))", normal_style)],
            [Paragraph("Total PIX Aprovado (Webhooks MP)", normal_style), Paragraph(f"R$ {total_pix_mp:.2f} ({conc_pix.get('qtd_pagamentos_mp', 0)} pagamento(s))", normal_style)],
            [Paragraph("Diferença PIX (MP - PDV)", bold_style), Paragraph(f"<b>R$ {dif_pix:+.2f}</b>", bold_style)],
            [Paragraph("Conciliados Exatos (external_reference)", normal_style), Paragraph(f"{len(exatos)} transação(ões)", normal_style)],
            [Paragraph("Correspondência Aproximada (Valor/Horário)", normal_style), Paragraph(f"{len(aprox)} transação(ões)", normal_style)],
        ]
        t_conc = Table(data_conc, colWidths=[350, 170])
        t_conc.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#00695C')),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
        ]))
        elements.append(t_conc)
        elements.append(Spacer(1, 8))

        if sem_wh or sem_venda or div_val:
            div_rows = [
                [
                    Paragraph("Tipo de Divergência", white_bold_style),
                    Paragraph("Referência / Payment ID", white_bold_style),
                    Paragraph("Detalhe", white_bold_style),
                ]
            ]
            for v in sem_wh:
                div_rows.append([
                    Paragraph("Venda PIX sem webhook MP", normal_style),
                    Paragraph(str(v.get("transacao_id", "")), normal_style),
                    Paragraph(f"PDV R$ {float(v.get('valor_pix', 0.0)):.2f}", normal_style),
                ])
            for m in sem_venda:
                div_rows.append([
                    Paragraph("Webhook MP sem venda PDV", normal_style),
                    Paragraph(str(m.get("payment_id", "")), normal_style),
                    Paragraph(f"MP R$ {float(m.get('valor', 0.0)):.2f} (ref: {m.get('external_reference') or '-'})", normal_style),
                ])
            for dv in div_val:
                div_rows.append([
                    Paragraph("Divergência de valor", normal_style),
                    Paragraph(f"{dv.get('transacao_id')} / {dv.get('payment_id')}", normal_style),
                    Paragraph(f"PDV R$ {float(dv.get('valor_pdv', 0.0)):.2f} vs MP R$ {float(dv.get('valor_mp', 0.0)):.2f}", normal_style),
                ])
            t_div = Table(div_rows, colWidths=[175, 175, 170])
            t_div.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#C0392B')),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 5),
            ]))
            elements.append(t_div)
            elements.append(Spacer(1, 12))
        else:
            elements.append(Paragraph("<i>Nenhuma divergência detectada na conciliação PIX deste turno.</i>", normal_style))
            elements.append(Spacer(1, 12))
    
    # Observações
    obs = resultado.get('observacao', '')
    if obs:
        elements.append(Paragraph("Observações", section_style))
        elements.append(Paragraph(obs, normal_style))
        elements.append(Spacer(1, 25))
    else:
        elements.append(Spacer(1, 15))
        
    # Signatures
    elements.append(Spacer(1, 40))
    sig_data = [
        [Paragraph("________________________________________<br/>Assinatura do Operador", normal_style),
         Paragraph("________________________________________<br/>Assinatura do Gerente", normal_style)]
    ]
    t_sig = Table(sig_data, colWidths=[260, 260])
    t_sig.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    elements.append(t_sig)
    
    doc.build(elements)
