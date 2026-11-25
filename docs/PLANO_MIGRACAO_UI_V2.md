# Plano mestre de migração da interface — Estoke ao Cubo v2.0 (PyWebView)

Status: **Marco 1 em execução** · Branch: `feat/pywebview-v2` (nasce de `feat/pywebview-f1b` em `52dc929`)

## Objetivo

Substituir a interface CustomTkinter por uma interface web moderna rodando **100% offline** dentro de
uma janela nativa (PyWebView + Edge WebView2), sem mudar nenhuma regra de negócio. O Python continua
sendo o dono dos dados e das regras (`services/*`, `database.py`); o frontend só apresenta e chama a
camada `bridge/`.

```
web/ (HTML + CSS + ES Modules, sem CDN)
   │  window.pywebview.api.<metodo>()  → Promise<{ok, data, error}>
   ▼
bridge/ (Python puro, só delega)  ──►  services/*  ──►  database.py (SQLite criptografado)
   ▲
   │  window.evaluate_js("window.__estokeEmit(evento, payload)")   (eventos Python → JS)
app_desktop.py (janela PyWebView + workers em background)
```

## Princípios invioláveis

1. **Zero regra de negócio nova** na bridge: ela valida parâmetros, chama `services/*` / `database.py`
   e serializa a resposta. Nada de SQL novo para regra de negócio.
2. **Envelope único**: todo método público da bridge devolve
   `{"ok": bool, "data": ..., "error": {"code": str, "message": str} | null}`. Exceções nunca vazam
   stacktrace para o JS; o traceback vai para o logger (`logs/app.log`).
3. **Serialização determinística**: `Decimal` → string (dinheiro nunca vira float no transporte),
   `date`/`datetime` → ISO-8601, `DataFrame` → lista de dicts, `NaN` → `null`.
4. **Offline total**: `web/` não referencia `http://` nem `https://`. Fontes, ícones e libs ficam em
   `web/vendor/` / `web/assets/`. Coberto por teste automatizado.
5. **Isolamento de camadas**: `services/` não importa UI (`customtkinter`, `tkinter`, `webview`).
   Coberto por teste AST.
6. **Convivência**: `python main.py` continua abrindo a UI legada (CTk). A nova UI abre com
   `python main.py --ui=web` (ou `python app_desktop.py`). `--self-test` continua intocado.
7. **Multi-loja**: toda chamada que envolve dados respeita `loja_id` (loja ativa da sessão da bridge;
   perfis não-admin/gerente ficam presos à própria loja).

## Marcos

### Marco 1 — Fundação (setup, bridge, shell)

- Instalar `pywebview` no `.venv` e fixar a versão em `requirements.txt`.
- `bridge/`:
  - `__init__.py`: `EstokeBridge` (agregadora exposta como `js_api`), envelope, serializador,
    decorador de captura de erros.
  - `events.py`: dispatcher thread-safe Python → JS (`window.__estokeEmit`).
  - `app_bridge.py`: bootstrap, tema, lojas/loja ativa, tela cheia, menu lateral por nicho, status de
    atualização.
  - `auth_bridge.py`: catálogo de primeiro acesso (18 nichos), conclusão do primeiro acesso, login,
    logout, sessão atual, autorização de supervisor.
- `app_desktop.py`: launcher PyWebView com servidor HTTP interno em 127.0.0.1 servindo
  `paths.get_web_path("index.html")`; respeita `ESTOKE_DATA_DIR`; sobe outbox, agendador de rotinas,
  backup diário e API local em background, sem Tk.
- `main.py --ui=web`: despacha para `app_desktop.main()`; padrão continua `--ui=legacy`.
- `web/`: design system dual-theme (tokens, base, componentes), cliente da bridge, estado, roteador
  por hash, hotkeys globais, páginas de login, primeiro acesso (18 nichos + aceite dos termos) e shell
  (sidebar colapsável por seções + topbar de ações + bento grid placeholder + rotas navegáveis).
- `tests/test_bridge_v2.py`.
- **Gate**: aprovação visual do Brian antes do Marco 2.

### Marco 2 — Dashboard Bento

- `bridge/dashboard_bridge.py` delegando para `RelatorioService.dados_dashboard(loja_id)` e afins.
- Cards: faturamento do dia/mês, ticket médio, caixa aberto, estoque crítico, contas a pagar, top
  produtos, gráfico de vendas (lib de gráfico vendorizada, offline).
- Eventos em tempo real (venda concluída, caixa aberto/fechado) via `events.py`.

### Marco 3 — PDV v2.0

- `bridge/pdv_bridge.py` delegando ao fluxo de venda existente (`finalizar_venda`, TEF simulado nos
  testes, NFC-e via `FiscalService`, antifraude e autorização de supervisor).
- Teclas: F2 (buscar produto), F4 (quantidade), F6 (desconto item), Shift+F6 (desconto total),
  F8 (cliente), F9 (forma de pagamento), F10 (finalizar), F11 (tela cheia), F12 (cancelar/supervisor).
- Leitor de código de barras (keyboard wedge) e balança via serviços existentes.

### Marco 4 — Módulos (sublotes)

Migrar tela a tela, cada sublote com bridge + página + testes e aprovação do Brian:
4a Estoque/Produtos/Categorias/Kits · 4b Clientes/Fornecedores/Vendedores · 4c Financeiro
(Gastos, Caixa, Contas a Pagar, DRE, Saldo) · 4d Relatórios/Etiquetas/Inventário/Compras ·
4e Serviços/OS por nicho · 4f Configurações, usuários, licença, Chat IA, Anota Tudo.

### Marco 5 — Empacotamento e cutover

- `EstokeAoCubo.spec`: incluir `web/` e `bridge/` como dados/módulos; dependências do pywebview
  (WebView2 runtime já presente no Windows 11; fallback documentado para o Windows 10).
- `EstokeSetup.iss`: checagem do WebView2 Runtime.
- `--self-test` estendido: importa `bridge`, valida `web/index.html` e o contrato offline.
- Cutover: `--ui=web` vira padrão; `--ui=legacy` fica uma versão como fallback; depois remoção do
  CustomTkinter de `views/*` e `main.py`.

## Contrato da bridge (Marco 1)

| Método | Delega para | Retorno (`data`) |
|---|---|---|
| `get_bootstrap()` | config, NichoService, AuthService, updater | versão, tema, nicho, primeiro acesso?, sessão, dica de senha inicial |
| `get_theme()` / `set_theme(t)` | `config.load_config/save_config` (`ui_theme`) | `"light"`/`"dark"` |
| `list_lojas()` | `Database.listar_lojas()` | lista de lojas + loja ativa |
| `set_loja_ativa(id)` | sessão da bridge (respeita perfil) | loja ativa |
| `toggle_fullscreen()` | `window.toggle_fullscreen()` | estado |
| `get_sidebar_menu()` | `NichoService.get_sidebar_buttons(nicho)` + `AuthService.tem_permissao` | seções com botões |
| `check_updates_status()` | `services.updater.check_for_updates` (fetcher injetável) | status da atualização |
| `get_first_access_catalog()` | `NichoService.listar_nichos()` + `TERMOS_DE_USO.txt` | 18 nichos + termos |
| `complete_first_access(nicho, aceite)` | `config.set_business_type` | nicho gravado |
| `login(login, senha)` | `AuthService.autenticar` + `Database.set_current_user` | usuário + `trocar_senha` |
| `logout()` | `Database.set_current_user(None)` | — |
| `get_current_session()` | sessão da bridge | usuário, loja ativa |
| `autorizar_supervisor(login, senha, acao, motivo)` | `AuthService.autorizar_supervisor` | aprovação |

## Diretrizes visuais

- **Light**: canvas `#F0F0F5`, glass `rgba(255,255,255,0.84)`, sólido `#FFFFFF`, texto `#001D39` /
  `#4A607A`, CTA `#0A4174` (hover `#062F56`), borda `1px rgba(0,29,57,0.09)`.
- **Dark** (`[data-theme="dark"]`): canvas `#12151C`, glass `rgba(28,33,44,0.78)`, sólido `#1B202C`,
  texto `#F1F5F9` / `#94A3B8`, destaque `#5DADE2` / `#7BBDE8`, borda `rgba(174,214,241,0.12)`.
- **Topbar**: ADICIONAR `#AED6F1`, VENDER `#7BBDE8`, SERVIÇO `#6AADDA`, LISTA `#5DADE2`,
  RELATÓRIO `#2874A6`, DRE `#1A5276`, F11 `#154360` (texto escuro/claro conforme contraste AA).
- Grain SVG inline no canvas (0.025 light / 0.035 dark, `pointer-events: none`).
- Glass com `backdrop-filter: blur(12px)` em shell, cards e modais; inputs com alta opacidade;
  contraste WCAG AA ≥ 4.5:1.
- Responsivo de 1280×720 a 2560×1440. Sidebar colapsável com seções Operação, Catálogo, Financeiro e
  Sistema & IA. Troca de tema instantânea e persistida.
- Hotkeys globais: Ctrl+N, Ctrl+Shift+V, Ctrl+E, Ctrl+R, Ctrl+G, Ctrl+K, F5, F11 — isoladas quando um
  input ou modal está ativo; gancho de escopo pronto para o PDV (Marco 3).

## Testes e critérios de aceite por marco

- `pytest -q` com `ESTOKE_DATA_DIR` isolado: baseline + testes novos.
- `python main.py --self-test` com exit 0.
- `git status` limpo; nenhum `.env`/`*.pem` rastreado; bundle de backup atualizado e verificado.
- Aprovação visual do Brian antes de abrir o marco seguinte.
