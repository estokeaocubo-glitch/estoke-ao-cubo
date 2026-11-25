# CLAUDE.md — Estoke ao Cubo

Sistema de gestão/PDV para varejo, food service e serviços (desktop Windows).
**Responda sempre em português do Brasil. Comentários no código também em português.**

## Stack
- **Desktop:** Python 3 + CustomTkinter (`main.py` + `views/`).
- **API local:** Flask (`api_server.py`), sessões thread-safe.
- **Banco:** SQLite criptografado (Fernet + DPAPI) em `database.py`; schema versionado em
  `migrations.py` (`LATEST_SCHEMA_VERSION`). Acesso protegido por `RLock`.
- **Regras de negócio:** `services/` (Python puro, **sem import de UI** — contratos AST nos testes).
- **Integrações:** Mercado Pago (PIX + Point TEF), Focus NFe (NFC-e mod. 65 via Outbox),
  impressora térmica/gaveta/balança, Gemini (`ai_module.py`), auto-update Ed25519 (`services/updater.py`).
- **Auto-update:** manifesto em `https://estoke-updates.pages.dev/version.json` e instaladores em
  `https://estoke-updates.pages.dev/releases/vX.Y.Z/` (Pages `estoke-updates`, proxy do bucket R2
  `estoke-trial-downloads`). O domínio `estokeaocubo.com.br` **não** está registrado: não usar.
- **Duas chaves Ed25519 separadas** em `services/updater.py`:
  - `DEFAULT_ED25519_PUBLIC_KEY_B64` (`D1csAK…`, desde a v4.6.1) valida **só o manifesto do auto-update**.
    A privada fica fora do repo (`C:\dev\_segredos\updater_private.pem`); nunca exibir nem commitar.
  - `DEFAULT_LICENSE_PUBLIC_KEY_B64` (`fO2QB…`) valida **tokens de licença**. Não trocar: as licenças
    já emitidas deixariam de valer.
- **Build:** PyInstaller (`EstokeAoCubo.spec`, `--onedir`) + Inno Setup (`EstokeSetup.iss`), edições Full/Trial.

## Estrutura
```
main.py            App CustomTkinter (ainda com regras de negócio a extrair) + --self-test (8.485 linhas)
database.py        Monolito de acesso a dados (SQLite + RLock) (6.833 linhas)
migrations.py      Migrações de schema
api_server.py      API REST Flask local (1.044 linhas, 41 rotas)
services/          Camada de domínio (alvo da extração F1b; Lote 7: estoque_service com kits/reposição,
                   producao_service, troca_service)
views/             Telas já modularizadas (pdv_view, financeiro_view, admin_view...)
paths.py           Caminhos de dados/recursos (respeita ESTOKE_DATA_DIR) e get_web_path()
crash_handler.py   Hooks de exceção + diálogo de erro
printer.py, barcode_scanner.py, invoice_generator.py, label_generator.py, notifications.py
tests/             Suíte pytest (pytest.ini: testpaths = tests, pythonpath = .)
build_local.py     Pipeline único de build/release (APP_VERSION)
version_info.txt   Metadados PE da versão
```
Linhas e rotas conferidas em 25/11/2026 na `feat/pywebview-f1b`.
Repo de trabalho: `C:\dev\EstokeAoCubo`. Fora do repo (pasta antiga `G:\Meu Drive\100`, somente leitura): site Next.js (`app/`, `components/`, `dist_site/`),
geradores de nicho (`gerar_bancos_nichos.py`, `bancos_nichos/`), `projeto_trial/`, `auditoria/`.

## Comandos
Use o venv do projeto `C:\dev\EstokeAoCubo\.venv` (Python 3.14, `requirements.txt` + pytest).
**Nunca** rode nada contra os dados reais (`%LOCALAPPDATA%\EstokeAoCubo`): sempre `ESTOKE_DATA_DIR` isolado.

```powershell
$py = "C:\dev\EstokeAoCubo\.venv\Scripts\python.exe"
$env:PYTHONUTF8 = "0"

# Testes (baseline atual: 257 passam / 1 falha preexistente em
# test_mercadopago_integration::test_01_carregamento_credenciais, que exige token real no .env)
$env:ESTOKE_DATA_DIR = (New-Item -ItemType Directory -Force "$env:TEMP\estoke_pytest").FullName
& $py -m pytest -q

# Self-test de boot (esperado: exit 0, selftest_report.json com "status": "ok")
$env:ESTOKE_DATA_DIR = (New-Item -ItemType Directory -Force "$env:TEMP\estoke_selftest").FullName
& $py main.py --self-test

# Smoke test da UI: script em ESTOKE_DATA_DIR temporário, com MP/Focus/impressora mockados
# (referência: smoke_pdv_lote6.py do agente anterior; ESTOKE_LOAD_DOTENV=0).
# Padrão do Lote 7: patch de App._show_first_access_screen/_show_login_screen para no-op,
# messagebox mockado, abrir cada diálogo e destruir as janelas novas.

# Build/release (só quando pedido)
& $py build_local.py
```

## Regras de trabalho
1. **Um lote por vez.** Mapear (sem editar) → plano curto (arquivos/métodos com `arquivo:linha`,
   decisões do Brian, riscos) → **aguardar OK** → implementar → validar → commitar → relatório → **parar**.
2. **Arquivos autorizados:** só os aprovados no plano. Precisou de outro: pare e pergunte.
3. **Git:** um commit por lote, Conventional Commits em português (ex.: `fix(f1b-lote7): ...`).
   **Nunca** `push`, merge, rebase ou force sem pedido explícito. Sem `--no-verify`.
4. **Dinheiro:** `Decimal` ou inteiros em centavos; arredondar só na saída (`ROUND_HALF_UP`, 2 casas).
5. **Lojas:** toda consulta/gravação financeira ou de estoque respeita o `loja_id` da loja ativa.
6. **Refatoração não muda comportamento.** Mudança intencional: tabela `campo | antes | depois | motivo`.
   Proibido regenerar snapshot inteiro às cegas.
7. **Nenhuma chamada real** a TEF, SEFAZ/Focus NFe, Mercado Pago, impressora ou balança em testes
   e smoke tests. Use mock ou homologação.
8. **`services/` não importa UI.**
9. **Validação de todo lote:** `pytest` completo (baseline × pós), `main.py --self-test`, smoke test
   da UI em `ESTOKE_DATA_DIR` isolado, hash do commit.
10. **Trava anti-loop:** 2 falhas seguidas pelo mesmo erro → pare e explique a causa raiz.
11. Diffs enxutos. Melhorias fora do escopo: anotar no `implementation_plan.md`, não implementar.
12. Timeout de I/O em teste: rode o teste de novo isolado e informe; não mascare. O repo saiu do
    Google Drive (agora em `C:\dev`), mas o bundle de backup continua em `G:\Meu Drive\backup\`.

## Convenções de commit
- Formato: `tipo(escopo): descrição em português, minúscula, sem ponto final`.
- Tipos: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`.
- Escopo por fase/lote: `f1a`, `f1b-loteN`, `f1b-loteNb-r1`, `hotfix-vX.Y.Z`.

## Branches
- `main` (= `origin/main`), `chore/checkpoint-v4.6.0`, `feat/pywebview-f1a`, `feat/pywebview-f1b` (ativa).
- `hotfix/v4.6.1` está no origin, com a tag `v4.6.1` → `1caccee`. Os 4 commits dela foram portados
  para a f1b via `cherry-pick -x`.
- `feat/pywebview-f1a` e `feat/pywebview-f1b` **não estão no origin**.
- Release em produção: v4.6.1 (build da `hotfix/v4.6.1`, publicada em `estoke-updates.pages.dev`).

## Roadmap
O roadmap vivo e as anotações fora de escopo ficam em `implementation_plan.md` (na raiz do repo;
enquanto não existir aqui, a última versão está na pasta do agente anterior, em
`%USERPROFILE%\.gemini\antigravity\brain\7f1adfc0-...\implementation_plan.md`).
Ordem atual: hotfix v4.6.1 → higiene (venv, requirements, teste MP `live`, `__version__`;
lotes H1 e H2+H3 = migração v10) → F1b Lote 7 (estoque/kits/produção/trocas, concluído)
→ F1b Lote 8 (restaurante, cadastros, migração v11, API)
→ isolamento por loja restante → F2 (ponte PyWebView) → F3 (UI em `web/`) → pré-release.

## Armadilhas conhecidas
- Versão: a fonte única é `services/updater.py` (`APP_VERSION`); `database.__version__` importa de lá.
  Ao subir versão, ainda sincronizar `build_local.py`, `crash_handler.py`, `version_info.txt` e `EstokeSetup.iss`.
- Schema atual: `LATEST_SCHEMA_VERSION = 10`. Teste novo não fixa número de versão; use a constante.
- `servicos.data_conclusao` é gravada na 1ª entrada em status terminal e nunca é limpa (não há reabertura).
  DRE e comissões só contam OS terminal, pela data de conclusão.
- A UI (`views/`) ainda não passa `loja_id` para contas a pagar: conta nova vai para a loja padrão
  e a lista mostra todas as lojas.
- Lote 7: `registrar_baixa_insumo`, `adicionar_ficha_tecnica`, `remover_ficha_tecnica`,
  `registrar_producao` e `registrar_troca` rodam sob `_lock` com rollback e checam tudo antes de escrever.
  Estoque negativo continua bloqueado em baixa, produção e troca. `adicionar_kit` retorna o id.
- Dívidas anotadas para o Lote 8 (v11): `loja_id` em `trocas_devolucoes`, `producao_diaria`,
  `baixa_insumos`, `fichas_tecnicas` e `kits` (hoje só o lançamento financeiro da troca leva a loja).
- Dívidas fora de escopo: `db.vender_kit` é código morto (o PDV não chama); `kit_itens` liga o
  componente por `produto_nome` (renomear o produto quebra o kit).
- Arquivos sensíveis fora do Git: `.env` (credenciais MP/Focus), chaves privadas em `C:\dev\_segredos\`.
  Nunca commitar, nunca imprimir o conteúdo.
