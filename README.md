# EstokeAoCubo — Open-Source Smart ERP & POS for Small Businesses

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CI](https://github.com/estokeaocubo-glitch/estoke-ao-cubo/actions/workflows/ci.yml/badge.svg)](https://github.com/estokeaocubo-glitch/estoke-ao-cubo/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/Version-4.3.0-0ea5e9.svg)](#)
[![Website](https://img.shields.io/badge/Website-estoke--ao--cubo.pages.dev-10367D?logo=google-chrome&logoColor=white)](https://estoke-ao-cubo.pages.dev)

> **Português:** [Documentação completa em Português abaixo](#estokeaocubo---sistema-de-gestão-inteligente).

## About & Social Purpose

**EstokeAoCubo** is a free, open-source, offline-first desktop ERP and Point of Sale (POS) built to empower **micro-entrepreneurs, local shops, restaurants, repair workshops, and independent artisans** in Brazil and Latin America who cannot afford expensive SaaS subscriptions or rely on unstable internet connections.

It runs locally with **zero mandatory cloud fees**, encrypts local SQLite data at rest (Fernet AES-128 + Windows DPAPI), supports ESC/POS thermal receipt printers, and includes an optional embedded Flask REST API for mobile companion apps.

## Core Features

- **Inventory & Virtual Kits:** Product tracking, batch/serial/IMEI traceability, expiry alerts, CSV/PDF bulk import, and atomic stock deduction for virtual bundles.
- **Point of Sale (POS) & Restaurant Mode:** Cash/PIX/card/store-credit checkout, change calculator, table map, kitchen Kanban board, bill splitting, and direct ESC/POS thermal printing.
- **Financial Management & Analytics:** Real profit calculation, simplified income statement (DRE), 90-day cash flow projection, ABC (Pareto) analysis, accounts payable/receivable, and sales commissions.
- **Work Orders (OS) & Multi-Store RBAC:** 7-stage service order workflow, multi-branch support, and role-based access control (`Admin`, `Manager`, `Seller`, `Cashier`).
- **Hardware & AI Integrations:** Webcam barcode/QR scanner (`OpenCV` + `pyzbar`), barcode label generator, local linear-regression expense forecasting, optional Gemini AI assistant, and embedded Flask REST API (port `5050`).

## Quick Start (Run Locally)

### Prerequisites
- Python `3.10+`
- `pip` (virtual environment recommended)

### Installation & Run
```bash
git clone https://github.com/estokeaocubo-glitch/estoke-ao-cubo.git
cd estoke-ao-cubo

python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux / macOS:
# source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # Optional: configure Gemini / Mercado Pago keys
python main.py
```

### Running Tests
```bash
pytest
```

## Contributing & License

Contributions, bug reports, and translations are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
Licensed under the **MIT License** — see [LICENSE](LICENSE) for details.

---

# EstokeAoCubo - Sistema de Gestão Inteligente

[![Website](https://img.shields.io/badge/Website-EstokeAoCubo-blue?style=for-the-badge&logo=google-chrome)](https://estoke-ao-cubo.pages.dev)

Este é um sistema completo de organização e gestão para pequenas empresas e artesãos, com Inteligência Artificial integrada.

## 🌐 Website Oficial
Acesse nosso site oficial para download e mais informações: **[estoke-ao-cubo.pages.dev](https://estoke-ao-cubo.pages.dev)**

## 🚀 Funcionalidades

### 📊 Gestão & Dashboard
-   **Dashboard Interativo**: Gráficos em tempo real mostrando a distribuição do estoque e gastos por categoria.
-   **Gestão Completa**:
    -   **Produtos**: Controle de estoque (Adicionar, Editar, Remover).
    -   **Kits Virtuais**: Criação de combos de produtos. O estoque do kit é virtual, calculado com base na disponibilidade dos itens que o compõem. A venda baixa os componentes automaticamente.
    -   **Categorias Customizáveis**: Crie suas próprias categorias de produto (ex: "Shot Linho", "Shot Jeans") e de gasto. Acesse pelo botão **CATEGORIAS** na sidebar.
    -   **Financeiro**: Controle de Saldo (Entradas e Saídas) e Gastos.
    -   **Metas**: Definição e acompanhamento de objetivos.
    -   **Fornecedores**: Cadastro de contatos e produtos fornecidos.
    -   **Estabelecimento**: Gerenciamento de tarefas de organização do espaço físico com priorização (Baixa/Média/Alta) e rastreamento de status (Pendente/Em Progresso/Concluído).
    -   **Gestão de Tempo**: Controle de tarefas com prazos.
-   **Relatórios Avançados**: Histórico completo de ações, relatórios de vendas, lucro real, ranking de produtos, comparativo mensal e gráficos temporais.
    -   **Clientes**: Gestão completa com histórico de compras, programa de fidelidade e contas a receber (fiado).

### 💰 PDV (Ponto de Venda)
-   **Formas de Pagamento**: Selecione entre **Dinheiro**, **PIX**, **Cartão Débito**, **Cartão Crédito** ou **Fiado** para cada venda.
-   **Calculadora de Troco**: Ao pagar em dinheiro, informe o valor recebido e o sistema calcula o troco automaticamente.
-   **Sistema de Fiado**: Vendas no fiado exigem cliente identificado e geram uma conta a receber com vencimento automático (30 dias).
-   **Desconto no PDV**: Aplique descontos por **percentual (%)** ou **valor fixo (R$)** diretamente no carrinho. O desconto é exibido em tempo real e consta no recibo.
-   **Alterar Preço do Item** [NOVO v4.1]: Botão laranja no carrinho para alterar o preço de um item específico durante a venda, sem removê-lo. O preço original do estoque não é afetado — a mudança vale apenas para aquela venda.
-   **Cancelamento de Vendas**: Cancele vendas com restauração automática do estoque e estorno financeiro.
-   **Foto do Produto**: Visualize a foto do produto no PDV ao selecionar e ao passar o mouse sobre o carrinho (tooltip único sem duplicação).
-   **Impressão Direta**: Após finalizar a venda, o sistema pergunta se deseja imprimir o recibo. A impressão é enviada direto para a impressora configurada, sem abrir Bloco de Notas.
-   **Fallback Inteligente**: Se a impressão falhar, o recibo é salvo em arquivo e aberto automaticamente.

### 📈 Relatórios & Análises
-   **Histórico de Vendas**: Visualize todas as vendas com filtros por período (Hoje/Semana/Mês/Ano/Todos), forma de pagamento e status.
-   **Lucro Real por Período**: Cards com lucro calculado para Hoje, Semana, Mês e Ano, baseado no custo real dos produtos.
-   **Ranking de Produtos**: Top 10 produtos mais vendidos com quantidade total e faturamento.
-   **Curva ABC (Pareto)** [NOVO v4.2]: Classificação de produtos em A (80% receita), B (15%) e C (5%) para análise estratégica de portfólio.
-   **Gráfico Temporal**: Gráfico de linhas com faturamento e lucro dos últimos 30 dias (matplotlib).
-   **Comparativo Mensal**: Compare faturamento, lucro e quantidade de vendas do mês atual vs mês anterior, com variação percentual.
-   **Vendas por Forma de Pagamento**: Resumo de quanto foi vendido em cada forma de pagamento.

### 👥 Clientes & CRM
-   **Gestão de Clientes**: Cadastro completo com 3 abas: lista de clientes, histórico de compras e programa de fidelidade.
-   **Histórico de Compras por Cliente**: Veja tudo que cada cliente comprou, com datas e valores.
-   **Programa de Fidelidade**: Clientes acumulam **1 ponto a cada R$10 gastos**. Pontos podem ser consultados e utilizados.
-   **Contas a Receber (Fiado)**: Aba dedicada para gerenciar todas as vendas fiadas, com filtro por status (Pendente/Pago) e botão para quitar.

### 📦 Estoque Avançado
-   **Alerta de Validade**: Ao abrir o sistema, é exibido um alerta automático se houver produtos vencendo nos próximos 7 dias. A informação também aparece no Dashboard.
-   **Importação CSV/PDF**: Importe produtos em massa via arquivo CSV ou PDF. Disponível no cadastro de produto ("📂 Importar Produtos (CSV/PDF)") com preview antes de confirmar.
-   **Arquivo Modelo CSV**: Incluso na pasta `dist` o arquivo `PADRÃO DE IMPORTAÇÃO.csv` com cabeçalhos prontos para o cliente preencher e importar.
-   **Importação de Imagens**: Coluna `imagem` no CSV/PDF permite associar fotos aos produtos na importação. O sistema copia automaticamente para `product_images/`.
-   **Lote/Nº de Série** [NOVO v4.2]: Campo de rastreabilidade para IMEI, SIF, lote do fornecedor ou qualquer referência de origem.
-   **Adicionar Estoque a Produto Existente**: Ao cadastrar produto, a primeira seção (destaque azul) permite reabastecer com filtros de categoria, subcategoria e busca por nome/código de barras, com indicadores de nível de estoque.
-   **Calculadora de Lucro na Edição**: Ao editar um produto, exibe lucro unitário, margem (%) e valor total de estoque em tempo real. Alterações geram registros automáticos no histórico financeiro.

### 🤖 Inteligência Artificial (IA)
-   **Previsão de Gastos**: Utiliza Regressão Linear para prever custos do próximo mês.
-   **Chatbot Assistente Executável**: Converse com a IA e execute ações direto do chat:
    
    **Perguntas (Consultas):**
    -   *"Qual meu gasto total?"*
    -   *"Qual a previsão para o futuro?"*
    -   *"Quais foram meus últimos gastos?"*
    -   *"Quantos [produto] eu tenho?"*
    
    **Comandos (Ações Executáveis):**
    -   *"Registre um gasto de 50 em Material para compra de tintas"*
    -   *"Adicione um produto Pincel com 10 unidades a 25 reais"*
    -   *"Adicione 1000 reais ao saldo"*
    -   *"Crie uma meta Aumentar vendas com descrição Crescer 20%"*

### 🎨 Design Moderno
-   **Interface "Clean"**: Visual profissional com paleta de cores personalizada (Azul Escuro `#10367D`, Branco e Cinza).
-   **Elementos Visuais**: Botões arredondados ("Pill shape"), Cards flutuantes e layout intuitivo.
### 🍽️ Restaurante (PDV Gastronômico)
-   **Mapa de Mesas**: Visualização por área (Salão, Varanda, Área Externa, VIP, Balcão) com status colorido (🟢 Livre / 🔴 Ocupada / 🟡 Reservada). Criação em lote com capacidade configurável.
-   **Comandas**: Abertura por mesa, balcão, delivery ou retirada. Cardápio com busca e filtro por categoria. Observações por item ("sem cebola", "bem passado").
-   **Fechamento de Comanda**: Taxa de serviço 10% (opcional), desconto em R$, formas de pagamento (Dinheiro, PIX, Cartão Débito, Crédito, Misto). Baixa automática de estoque e registro de venda.
-   **Divisão de Conta**: Informe o número de pessoas e o sistema calcula o valor por pessoa.
-   **Painel da Cozinha**: Kanban com 3 colunas (Recebido → Preparando → Pronto). Auto-refresh a cada 15s. Ideal para monitor dedicado na cozinha.
-   **Fichas Técnicas**: Integração com módulos INSUMOS e PRODUÇÃO para controle de ingredientes por receita.

### 🔧 Ordem de Serviço (OS) [NOVO v4.2]
-   **Workflow de 7 Etapas**: Recebido → Em Análise → Aguardando Peça → Em Execução → Pronto → Entregue → Concluído.
-   **Campo Equipamento/Item**: Identifique o que está sendo consertado (ex: "iPhone 14", "Notebook Dell").
-   **Histórico Colorido**: Cards com badges de status e bordas coloridas por etapa.
-   **Avanço Rápido**: Botão "Avançar Status →" para progredir a OS com um clique.

### 🧾 Impressão Térmica Otimizada [ATUALIZADO v4.2]
-   **Layout Compacto**: Margens reduzidas (12px), fontes otimizadas (14/20), separadores com traços.
-   **Recorte Automático**: Espaço branco extra no rodapé do cupom é removido automaticamente.
-   **Logo Otimizado**: Altura máxima 80px (antes 130px), largura máxima 300px para ESC/POS.
-   **Pré-visualização**: Botão "👁 Pré-visualizar Nota" nas Configurações > Nota/Recibo gera exemplo visual antes de imprimir.

### 👤 Multi-Usuário & Permissões [NOVO v4.0]
-   **Perfis de Acesso**: 4 perfis pré-definidos — Administrador (acesso total), Gerente (sem gestão de usuários), Vendedor (vendas e cadastros), Caixa (vendas e caixa).
-   **Gestão de Usuários**: Cadastro com login, senha (SHA-256), perfil e vinculação a loja. Ativar/desativar sem excluir.
-   **Controle de Permissões**: Cada funcionalidade verifica o perfil do usuário logado antes de permitir acesso.

### 🔔 Notificações Toast [NOVO v4.0]
-   **Alertas Visuais**: Cards flutuantes no canto superior direito com cores por severidade (info/success/warning/error).
-   **Alertas Automáticos**: Estoque baixo, validade próxima, fiado vencido, contas a pagar vencidas/vencendo — exibidos ao iniciar o sistema.
-   **Auto-dismiss**: Notificações fecham automaticamente. Máximo 4 visíveis simultâneas com empilhamento.

### 📊 Dashboard em Tempo Real [NOVO v4.0]
-   **Auto-refresh**: Dashboard atualizado automaticamente a cada 60 segundos.

### 📷 Scanner por Webcam [NOVO v4.0]
-   **Leitura por Câmera**: Botão 📷 no PDV abre preview ao vivo da webcam (640×480, ~30fps).
-   **Detecção Automática**: Lê EAN-13, EAN-8, UPC-A, Code 128, Code 39, QR Code via pyzbar.
-   **Integração PDV**: Código detectado é inserido automaticamente no campo de barras e o produto é adicionado ao carrinho.

### 🏷️ Gerador de Etiquetas [NOVO v4.0]
-   **Etiquetas com Código de Barras**: Gera imagens PNG com nome, preço, código de barras e QR Code do produto.
-   **Seleção em Lote**: Checkboxes para selecionar múltiplos produtos e gerar etiquetas de uma vez.
-   **Preview**: Janela de preview das etiquetas geradas antes de imprimir.

### 📋 Contas a Pagar [ATUALIZADO v4.1]
-   **CRUD Completo**: Cadastro com fornecedor, descrição, valor, vencimento, categoria e observação.
-   **Navegação por Botões**: Interface com dois painéis alternáveis ("Contas" e "+ Nova Conta") em vez de abas, para maior estabilidade.
-   **Calendário Interativo** [NOVO v4.1]: Seleção de data de vencimento via calendário popup (DateEntry/tkcalendar) em português.
-   **Parcelamento Automático**: Ao informar número de parcelas > 1, o sistema cria parcelas com vencimentos mensais e valores proporcionais.
-   **Filtro por Status**: Pendente, Pago ou Todos, com total pendente em tempo real.
-   **Categorias**: Aluguel, Energia, Internet, Fornecedor, Impostos, Salários, Outros.

### 📈 DRE Simplificado [NOVO v4.0]
-   **Demonstração de Resultado**: Receita Bruta → Devoluções → Receita Líquida → CMV → Lucro Bruto → Despesas → Contas Pagar → Resultado Operacional → Impostos → Resultado Líquido.
-   **Períodos**: Mês Atual, Mês Anterior, Trimestre, Semestre, Ano.
-   **Links Rápidos**: Acesso direto ao Fluxo de Caixa e Comissões no rodapé.

### 💰 Fluxo de Caixa Futuro [NOVO v4.0]
-   **Projeção 90 dias**: Baseada na média de vendas e gastos dos últimos 30 dias + contas a pagar/receber pendentes.
-   **Tabela Detalhada**: Colunas Data, Entradas, Saídas e Saldo Projetado.
-   **Alerta de Déficit**: Valores negativos destacados em vermelho.

### 💵 Controle de Comissões [NOVO v4.0]
-   **Cálculo Automático**: Vendas × percentual de comissão (definido no cadastro do vendedor).
-   **Relatório por Período**: Mês Atual, Mês Anterior, Trimestre, Semestre, Ano.
-   **Resumo Financeiro**: Total de comissões a pagar no período selecionado.

### 🏪 Múltiplas Lojas [NOVO v4.0]
-   **Cadastro de Filiais**: Nome, endereço, telefone e CNPJ por loja.
-   **Vinculação de Usuários**: Cada usuário pode ser vinculado a uma loja específica.
-   **Configuração**: Gerenciamento na aba "Lojas" das Configurações.

### 🌐 REST API para App Mobile [NOVO v4.0]
-   **Servidor Flask Embutido**: Roda na porta 5050 em background ao iniciar o sistema.
-   **Autenticação por Token**: Login via `/api/login` retorna token JWT-like para autenticar requisições.
-   **Endpoints**: `/api/produtos`, `/api/vendas`, `/api/clientes`, `/api/estoque/baixo`, `/api/produtos/barcode/<codigo>`.
-   **Permissões**: Respeita o perfil do usuário logado na API.
-   **App Android**: Compatível com o app EstokeAoCuboAndroid para gestão remota via celular.
## 🛠 Instalação

1.  Certifique-se de ter o Python instalado.
2.  Instale as dependências:

    ```bash
    pip install -r requirements.txt
    ```

> ⚙️ **Novo em 2.2**: agora o aplicativo pode enviar recibos diretamente para uma impressora fiscal/ESC-POS. Configure a impressora em **Configurações → Impressora** no menu lateral. O sistema ainda gera um arquivo de texto como backup.

## ▶️ Como Executar

Para rodar o aplicativo diretamente:

```bash
py main.py
```
*(Ou `python main.py` dependendo da sua configuração)*

## 📦 Como Criar o Executável (.exe)

Para transformar o projeto em um arquivo executável para Windows:

1.  Instale o pyinstaller:
    ```bash
    pip install pyinstaller
    ```

2.  Gere o executável (ou use `build.bat` que já inclui todos os dados e, em
    especial, o Guia do Usuário):
    ```bash
    pyinstaller --noconfirm --onefile --windowed --name "EstokeAoCubo" \
        --add-data "icon;icon" \
        --add-data "dist_package/GUIA_DO_USUARIO.txt;." \
        --collect-all customtkinter main.py
    ```

3.  O arquivo `EstokeAoCubo.exe`, o `GUIA_DO_USUARIO.txt` e o `PADRÃO DE IMPORTAÇÃO.csv` estarão na pasta
    `dist`. Envie todos para o cliente; o guia e o modelo CSV são copiados automaticamente
    pelo script `build.bat`.

## 📜 Histórico de Alterações

### v4.2 - Rastreabilidade, OS Workflow & Notas Otimizadas
**Novas Funcionalidades:**
- 📊 **Curva ABC (Análise Pareto)**: Classifica produtos em A (80% da receita), B (15%) e C (5%) com base no faturamento. Disponível em Relatórios > Lucro & Ranking.
- 🔖 **Lote/Nº de Série**: Novo campo de rastreabilidade no cadastro de produtos. Aceita IMEI, SIF, lote do fornecedor ou qualquer referência.
- 🔧 **Ordem de Serviço (OS)**: Workflow completo com 7 etapas (Recebido → Em Análise → Aguardando Peça → Em Execução → Pronto → Entregue → Concluído). Campo de equipamento/item, badges coloridos no histórico e botão de avanço rápido.

**Melhorias:**
- 🧾 **Notas Fiscais Otimizadas**: Layout mais compacto para impressoras térmicas — margens 12px, fontes 14/20, separadores com traços (---), valores abreviados (R$49.90), recorte automático de espaço branco.
- 👁 **Pré-visualização de Nota**: Botão nas Configurações > Nota/Recibo para gerar preview visual da nota com dados de exemplo antes de imprimir.
- 📐 **Logo Otimizado**: Altura máxima reduzida para 80px na imagem e 100px em ESC/POS RAW. Largura máxima 300px.

**Alterações no Banco de Dados:**
- `produtos`: Nova coluna `lote_serie TEXT DEFAULT ''`.
- `servicos`: Novas colunas `status_os`, `contato_cliente`, `equipamento`, `data_previsao`.

### v4.1 - PDV Aprimorado & Contas a Pagar Redesenhadas (26/03/2026)
**Novas Funcionalidades:**
- 💲 **Alterar Preço do Item no PDV**: Botão laranja no carrinho permite mudar o preço de um item específico durante a venda. O preço original do estoque não é alterado — a mudança vale apenas para aquela venda. Suporte a ENTER para confirmação rápida.
- 📅 **Calendário Interativo nas Contas a Pagar**: Campo de vencimento agora usa DateEntry (tkcalendar) com calendário popup em português, substituindo a digitação manual.

**Melhorias:**
- 📋 **Contas a Pagar Redesenhadas**: Interface reescrita com sistema de navegação por botões ("Contas" / "+ Nova Conta") substituindo CTkTabview, corrigindo problemas de interatividade. Janela modal com prioridade sobre a tela principal.
- 🎯 **Filtro de Contas Unificado**: Lista de contas agora usa radio buttons (Pendente/Pago/Todos) com total pendente exibido em tempo real.

**Novas Dependências:**
- `tkcalendar` (+ `babel`): Widget de calendário para seleção de datas.

### v4.0 - Gestão Empresarial Completa (16/03/2026)
**Novas Funcionalidades:**
- 👤 **Multi-Usuário com Permissões**: 4 perfis (Admin, Gerente, Vendedor, Caixa) com controle granular de acesso. Senhas com hash SHA-256. Gestão completa na aba "Usuários" das Configurações.
- 🔔 **Notificações Toast**: Alertas visuais automáticos (estoque baixo, validade, fiado vencido, contas vencidas/vencendo). Cards empilháveis com cores por severidade. Módulo `notifications.py`.
- 📊 **Dashboard em Tempo Real**: Auto-refresh a cada 60 segundos.
- 📷 **Scanner de Código de Barras por Câmera**: Botão 📷 no PDV abre webcam com detecção em tempo real. Suporte a EAN-13, UPC-A, Code 128, QR Code. Módulo `barcode_scanner.py` com OpenCV + pyzbar.
- 🏷️ **Gerador de Etiquetas**: Gera etiquetas com nome, preço, código de barras e QR Code. Seleção em lote com checkboxes. Módulo `label_generator.py`.
- 📋 **Contas a Pagar**: CRUD completo com fornecedor, valor, vencimento, categoria, parcelas automáticas. Abas Pendentes/Pagas/Nova Conta. Alertas integrados.
- 📈 **DRE Simplificado**: Demonstração de Resultado do Exercício com Receita → CMV → Lucro Bruto → Despesas → Resultado Líquido. 5 períodos disponíveis.
- 💰 **Fluxo de Caixa Futuro**: Projeção de 90 dias baseada em médias históricas + contas pendentes. Tabela com entradas, saídas e saldo projetado.
- 💵 **Controle de Comissões**: Cálculo automático por vendedor (vendas × % comissão). Relatório por período com total de comissões.
- 🏪 **Múltiplas Lojas**: Cadastro de filiais com nome, endereço, telefone e CNPJ. Vinculação de usuários a lojas específicas. Aba "Lojas" nas Configurações.
- 🌐 **REST API para App Mobile**: Servidor Flask embutido (porta 5050) com autenticação por token. Endpoints: produtos, vendas, clientes, estoque baixo, busca por código de barras. Módulo `api_server.py`. Compatível com app Android.

**Novos Módulos:**
- `notifications.py`: Sistema de notificações toast com `ToastNotification` e `NotificationManager`.
- `barcode_scanner.py`: Scanner de código de barras via webcam com `CameraScanner` (threading).
- `label_generator.py`: Gerador de etiquetas com código de barras e QR Code.
- `api_server.py`: Servidor REST API Flask com autenticação e permissões.

**Novas Tabelas no Banco de Dados:**
- `usuarios` (id, nome, login, senha_hash, perfil, ativo, loja_id, data_criacao)
- `lojas` (id, nome, endereco, telefone, cnpj, ativa, data_criacao)
- `contas_pagar` (id, fornecedor_id, descricao, valor, data_emissao, data_vencimento, data_pagamento, status, categoria, recorrente, parcela_atual, total_parcelas, observacao)

**Novas Dependências:**
- `opencv-python`, `pyzbar`, `python-barcode`, `qrcode`, `flask`, `tkcalendar`

### v3.1 - Modo Restaurante Completo (13/03/2026)
**Novas Funcionalidades:**
- 🍽️ **Modalidade Restaurante**: Novo tipo de negócio "🍽️ Restaurante" na tela de primeiro acesso com cor temática #E07B56.
- 🧊 **Mapa de Mesas**: Criação em lote com capacidade e área. Visualização colorida por status. Agrupamento automático por área (Salão, Varanda, Área Externa, VIP, Balcão).
- 📋 **Comandas**: Suporte a Mesa, Balcão, Delivery e Retirada. Editor com cardápio integrado, busca, filtro, observações por item e acompanhamento de status (Recebido → Preparando → Pronto → Entregue).
- 💰 **Fechamento de Comanda**: Taxa de serviço 10% (toggle), desconto R$, pagamento (Dinheiro/PIX/Cartão Débito/Crédito/Misto). Baixa de estoque e registro de venda automáticos.
- ✂️ **Divisão de Conta**: Cálculo automático por número de pessoas.
- 🍳 **Painel da Cozinha**: Kanban 3 colunas com auto-refresh (15s). Cards com mesa/tipo, item, quantidade, observação e horário.
- 🗃️ **Novas Tabelas**: `mesas` (id, numero, capacidade, area, status), `comandas` (id, mesa_id, tipo, status, total, timestamps), `comanda_itens` (id, comanda_id, produto_id, quantidade, observacao, status, timestamps). Índices otimizados.
- 🔧 **Sidebar Dinâmica**: Botões MESAS, COMANDAS, COZINHA, INSUMOS e PRODUÇÃO aparecem exclusivamente no modo Restaurante.

### v2.8 - Gestão de Produtos Avançada & Importação com Imagens (13/03/2026)
**Novas Funcionalidades:**
- 📦 **Adicionar Estoque a Produto Existente (Redesign)**: Seção em destaque azul no topo do diálogo de cadastro, com filtros de categoria/subcategoria, busca por nome/código de barras e indicadores visuais de nível de estoque (🟢/🟠/🔴).
- 📊 **Calculadora de Lucro na Edição**: Ao editar um produto, exibe lucro unitário (R$), margem (%) e valor total de estoque em tempo real. Alterações de custo ou quantidade geram registros automáticos de gasto no histórico financeiro.
- 🖼️ **Importação de Imagens via CSV/PDF**: Nova coluna `imagem` reconhecida na importação. Aceita caminhos relativos/absolutos. Formatos: PNG, JPG, JPEG, GIF, BMP, WEBP.
- 📄 **Arquivo Modelo CSV**: Incluso `PADRÃO DE IMPORTAÇÃO.csv` na pasta `dist` como modelo pronto para o cliente preencher com seus produtos.
- 🔐 **Acesso Administrativo**: Relatórios e Configurações protegidos por login/senha (padrão: admincentral/admin9438). Alterável em CONFIG > Administração.
- 👥 **Vendedores/Funcionários**: Cadastro de equipe com nome, horário, comissão (%). Auto-detecção no PDV por horário. Aba de desempenho com métricas.

### v2.6 - Gestão Avançada, Backup & Relatórios (02/03/2026)
**Correções Críticas:**
- 🐛 **Venda detalhada**: Corrigido bug onde vendas não deduziam estoque nem registravam entrada financeira. Desconto agora é proporcional por item.
- 🐛 **Venda de Kit**: Agora registra corretamente na tabela de vendas e entradas financeiras (antes só baixava estoque).
- 🐛 **Editar produto na venda**: Corrigido sobrescrita de margem_lucro, fator_risco e fator_raridade com valores padrão ao processar vendas.
- 🐛 **Custo Material zero**: Campo "Custo Material" na edição de produto agora mostra o valor real (preco_compra) ao invés de sempre "0.00".
- 🐛 **Memory leak matplotlib**: Figuras de gráficos agora são fechadas antes de recriar (evita vazamento de memória).
- 🐛 **dialog.destroy() crash**: Corrigidas 4 instâncias onde messagebox.showinfo era chamado com parent=dialog após dialog.destroy().
- 🐛 **Ctrl+V override**: Atalho de vender mudado para Ctrl+Shift+V para não sobrescrever colar texto.
- 🐛 **Prioridade de tarefas**: Ordenação corrigida (Alta > Média > Baixa) usando CASE expression.
- 🐛 **Foreign keys**: PRAGMA foreign_keys habilitado no SQLite.

**Novas Funcionalidades:**
- 💾 **Backup/Restore**: Nova aba "Backup" nas Configurações com criação manual, listagem e restauração de backups.
- 📊 **Exportação de Relatórios HTML**: Nova aba "Exportar" nas Configurações para gerar relatórios estilizados (vendas, estoque, financeiro) por período.
- 📦 **Estoque Mínimo Configurável**: Campo "Estoque Mínimo" no cadastro de produtos. Alertas no dashboard agora usam limites individuais por produto.
- 🔧 **Configurações com Abas**: Janela de configurações reorganizada com abas (Impressora, Backup, Exportar).

### v2.5 - Importação PDF & Correções de UX (02/03/2026)
**Mudanças:**
- 📄 **Importação de PDF**: Agora é possível importar produtos de arquivos PDF com tabelas (além de CSV). O sistema usa `pdfplumber` para extrair tabelas automaticamente.
- 🛒 **Correção Tooltip do Carrinho**: Corrigido bug que causava múltiplos tooltips duplicados ao passar o mouse sobre itens no carrinho de compras. Agora exibe apenas um tooltip por vez, reposicionando ao mover o mouse dentro da mesma linha.
- 📦 **Nova dependência**: `pdfplumber` adicionado ao `requirements.txt`.

### v2.4 - Vendas Completas, CRM & Relatórios Avançados (28/02/2026)
**Mudanças:**
- 💳 **Formas de Pagamento no PDV**: Seleção entre Dinheiro, PIX, Cartão Débito, Cartão Crédito e Fiado. Cada venda registra a forma utilizada.
- 💵 **Calculadora de Troco**: Ao pagar em dinheiro, campo para informar valor recebido com cálculo automático do troco.
- 📝 **Sistema de Fiado**: Vendas no fiado geram contas a receber com vencimento de 30 dias. Exige cliente identificado.
- ❌ **Cancelamento de Vendas**: Cancele vendas com restauração completa do estoque e estorno financeiro automático.
- 📊 **Histórico de Vendas**: Nova aba "Vendas" nos relatórios com Treeview completo, filtros por período e resumo por forma de pagamento.
- 💰 **Lucro Real**: Cards de lucro por período (Hoje/Semana/Mês/Ano) calculados com base no custo real dos produtos.
- 🏆 **Ranking de Produtos**: Top 10 produtos mais vendidos com quantidade e faturamento total.
- 📈 **Gráfico Temporal**: Gráfico de linhas (30 dias) com faturamento e lucro usando matplotlib.
- 📅 **Comparativo Mensal**: Mês atual vs anterior com variação percentual de faturamento, lucro e quantidade.
- 👥 **Gestão de Clientes Expandida**: Nova janela com 3 abas — Lista de Clientes, Histórico de Compras e Programa de Fidelidade.
- ⭐ **Programa de Fidelidade**: Clientes acumulam 1 ponto a cada R$10 gastos. Pontos registrados automaticamente.
- ⏰ **Alerta de Validade**: Aviso automático na inicialização + seção no Dashboard para produtos próximos do vencimento (7 dias).
- 📂 **Importação via CSV**: Importe produtos em massa via arquivo CSV com preview e validação de colunas.
- 🗃️ **Novas Tabelas**: `contas_receber` (fiado) e `fidelidade` (pontos). Novos índices para performance.
- 🔧 **Migração Automática**: Colunas `forma_pagamento`, `cliente`, `status` e `desconto` adicionadas à tabela `vendas`.

### v2.3 - Categorias, Desconto e Impressão Direta (28/02/2026)
**Mudanças:**
- 🏷️ **Categorias Customizáveis**: Botão CATEGORIAS na sidebar permite criar/remover categorias de produto (ex: "Shot Linho", "Shot Jeans") e de gasto. Todos os combos do sistema carregam do banco de dados.
- 💰 **Desconto no PDV**: Campo de desconto no carrinho com opção de % ou R$. Valor atualiza em tempo real, consta na confirmação e no recibo gerado.
- 🖨️ **Impressão Direta do Recibo**: Após finalizar venda, sistema pergunta se deseja imprimir. Envia direto para impressora configurada sem abrir Bloco de Notas. Fallback automático para arquivo.
- 📄 Recibo agora exibe SUBTOTAL, DESCONTO e TOTAL A PAGAR quando há desconto aplicado.
- 🔧 Nova tabela `categorias` no banco de dados com categorias padrão inseridas automaticamente.
- 🐞 Correção de prioridade de janelas: todas as messageboxes aparecem à frente da janela correta.
- ⚡ Melhorias de performance: lazy-load matplotlib, índices SQLite, otimização de queries.
- 🛡️ Segurança: validação de API key, whitelist de tabelas em buscar_por_id.
- 📝 Sistema de logging substituíndo todos os print().

### v2.2 - Impressão Fiscal e Configurações (24/02/2026)
**Mudanças:**
- 🖨️ Nova tela de configurações com suporte a impressora
- 🔧 Adicionado módulo `printer` e `config` para gerenciamento de dispositivos e preferências
- 📄 Recibos continuam sendo salvos em `.txt`, mas agora são enviados automaticamente para a impressora configurada (ESC/POS USB, rede ou serial) se habilitada
- 🐞 Correções de pequenos bugs de geração de nota

### v2.1 - Kits Virtuais (20/01/2026)
**Mudanças:**
- 📦 **Módulo de Kits**: Nova funcionalidade para criar "Combos" ou "Bundles".
- 🔄 **Estoque Virtual**: Kits não ocupam espaço físico; a quantidade disponível é calculada em tempo real com base nos componentes.
- 📉 **Venda com Baixa Atômica**: Ao vender um kit, o sistema desconta automaticamente as quantidades unitárias de cada item composto.
- 🖱️ **Botão Dedicado**: Acesso rápido via sidebar.

### v2.0 - Rebranding e Website Oficial (01/01/2025)
**Mudanças:**
- 🚀 **Rebranding Completo**: Projeto renomeado de "EstoqueAI" para **"EstokeAoCubo"**.
- 🌐 **Novo Website**: Lançamento do site oficial [estoke-ao-cubo.pages.dev](https://estoke-ao-cubo.pages.dev).
  - Landing page moderna com animações e vídeo demonstrativo.
  - Sistema de checkout integrado com Cloudflare D1.
  - Design responsivo para mobile e desktop.
- 🎨 **Identidade Visual**:
  - Novos ícones de alta resolução.
  - Ajustes na interface gráfica para refletir a nova marca.
- 📄 **Documentação**: Atualização completa dos guias de instalação e manuais técnicos.
### v1.3 - Chat Executável e Ações via IA (29/12/2025)
**Mudanças:**
- ✨ Chat integrado agora pode executar funções do sistema
  - Novo método `execute_action()` na classe ExpensePredictor
  - Suporte para registrar gastos via chat: "Registre um gasto de 50 em Material para compra"
  - Suporte para adicionar produtos: "Adicione um produto Pincel com 10 unidades a 25 reais"
  - Suporte para adicionar saldo: "Adicione 1000 reais ao saldo"
  - Suporte para criar metas: "Crie uma meta Aumentar vendas com descrição Crescer 20%"
  - Chat atualiza o dashboard automaticamente após executar ações
  - Interface melhorada com instruções de uso e tecla Enter para enviar
  - Feedback visual (✅/❌) para sucesso ou erro nas operações
### v1.2 - Refatoração do Módulo Estabelecimento (29/12/2025)
**Mudanças:**
- ✨ Refatoração completa do módulo ESTABELECIMENTO
  - Transformado de sistema genérico para **gerenciador de tarefas de organização do espaço**
  - Atualizado esquema do banco de dados: de `(item, detalhe)` para `(area_local, tarefa, status, prioridade)`
  - Criada interface especializada `open_estabelecimento_manager()` em `main.py`
  - Implementado sistema de priorização com cores visuais (Baixa=Verde, Média=Amarelo, Alta=Vermelho)
  - Adicionado rastreamento de status com transições automáticas (Pendente → Em Progresso → Concluído)
  - Métodos do banco atualizados:
    - `adicionar_tarefa_estabelecimento(area_local, tarefa, prioridade, status)`
    - `remover_tarefa_estabelecimento(id_tarefa)`
    - `atualizar_status_estabelecimento(id_tarefa, novo_status)`
    - `listar_tarefas_estabelecimento()` - retorna ordenadas por prioridade

### v1.1 - Funcionalidades Principais
- Dashboard interativo com gráficos em tempo real
- IA com previsão de gastos e chatbot assistente
- 7 módulos de gestão completos (SALDO, GASTOS, METAS, FORNECEDORES, ESTABELECIMENTO, GESTÃO DE TEMPO, RELATÓRIOS)
- Design moderno com paleta de cores profissional
- Validação de formulários e tratamento de erros

## 📋 Módulos Detalhados

### Chat IA - Assistente Inteligente Executável

O **CHAT IA** é um assistente inteligente que não apenas responde perguntas, mas também **executa ações no sistema** diretamente pela linguagem natural.

**Funcionalidades:**
- 💬 **Consultas Inteligentes**: Obtenha informações sobre gastos, estoque, previsões
- ⚡ **Comandos Executáveis**: Execute ações como adicionar produtos, registrar gastos, criar metas
- 🔄 **Atualização Automática**: Dashboard atualiza em tempo real após cada ação
- 📝 **Histórico de Conversas**: Visualize todas as interações no chat

**Exemplos de Uso:**

| Tipo | Comando | Resultado |
|------|---------|-----------|
| **Pergunta** | "Qual é o gasto total?" | Retorna: "O total de gastos é R$ XXX" |
| **Pergunta** | "Qual a previsão para o próximo mês?" | Retorna: "A previsão é de R$ XXX" |
| **Comando** | "Registre um gasto de 150 em Material para compra de tintas" | ✅ Adiciona gasto ao banco |
| **Comando** | "Adicione um produto Pincel com 10 unidades a 25 reais" | ✅ Adiciona produto ao estoque |
| **Comando** | "Adicione 1000 reais ao saldo" | ✅ Aumenta saldo da empresa |
| **Comando** | "Crie uma meta Aumentar vendas com descrição Crescer 20%" | ✅ Cria nova meta |

**Padrões de Linguagem Natural Suportados:**
- Gastos: "registre/adicione um gasto de [valor] em [categoria] para [descrição]"
- Produtos: "adicione um produto [nome] com [qtd] unidades a [preço] reais"
- Saldo: "adicione [valor] reais ao saldo"
- Metas: "crie uma meta [nome] com descrição [descrição]"

### Estabelecimento - Gestão de Tarefas de Organização

O módulo **ESTABELECIMENTO** foi refatorado para funcionar como um sistema de **gestão de tarefas e organização do espaço físico**. Permite que você organize e acompanhe as atividades de manutenção, limpeza e reorganização do seu estabelecimento.

**Funcionalidades:**
-   ✅ **Adicionar Tarefas**: Defina área/local, descrição da tarefa e nível de prioridade
-   ✅ **Priorização**: Marque tarefas como Baixa, Média ou Alta prioridade (com indicadores coloridos)
-   ✅ **Rastreamento de Status**: Acompanhe o progresso com transições automáticas:
    -   **Pendente** (Azul) → **Em Progresso** (Amarelo) → **Concluído** (Verde)
-   ✅ **Visualização Inteligente**: Tarefas organizadas e coloridas por prioridade
-   ✅ **Remoção**: Apague tarefas concluídas ou canceladas

**Exemplo de Uso:**
- Área: "Sala de Produção"
- Tarefa: "Organizar prateleiras de insumos"
- Prioridade: "Alta"
- Status: Transição de Pendente → Em Progresso → Concluído

**Estrutura no Banco de Dados:**
A tabela `estabelecimento` armazena:
- `id`: Identificador único da tarefa
- `area_local`: Área ou local do estabelecimento
- `tarefa`: Descrição da tarefa
- `status`: Pendente, Em Progresso ou Concluído
- `prioridade`: Baixa, Média ou Alta
- `data_criacao`: Timestamp da criação

