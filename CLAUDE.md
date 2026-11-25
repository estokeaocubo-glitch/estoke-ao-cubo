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
- **Build:** PyInstaller (`EstokeAoCubo.spec`, `--onedir`) + Inno Setup (`EstokeSetup.iss`), edições Full/Trial.

## Estrutura
```
main.py            App CustomTkinter (ainda com regras de negócio a extrair) + --self-test
database.py        Monolito de acesso a dados (SQLite + RLock)
migrations.py      Migrações de schema
api_server.py      API REST Flask local
services/          Camada de domínio (alvo da extração F1b)
views/             Telas já modularizadas (pdv_view, financeiro_view, admin_view...)
paths.py           Caminhos de dados/recursos (respeita ESTOKE_DATA_DIR) e get_web_path()
crash_handler.py   Hooks de exceção + diálogo de erro
printer.py, barcode_scanner.py, invoice_generator.py, label_generator.py, notifications.py
tests/             Suíte pytest (pytest.ini: testpaths = tests, pythonpath = .)
build_local.py     Pipeline único de build/release (APP_VERSION)
version_info.txt   Metadados PE da versão
```
Fora do repo (pasta pai `G:\Meu Drive\100`): site Next.js (`app/`, `components/`, `dist_site/`),
geradores de nicho (`gerar_bancos_nichos.py`, `bancos_nichos/`), `projeto_trial/`, `auditoria/`.

## Comandos
Use o venv `C:\auditoria_estoke\.venv-auditoria` (Python 3.14). O `.venv` do projeto está quebrado.
**Nunca** rode nada contra os dados reais (`%LOCALAPPDATA%\EstokeAoCubo`): sempre `ESTOKE_DATA_DIR` isolado.

```powershell
$py = "C:\auditoria_estoke\.venv-auditoria\Scripts\python.exe"
$env:PYTHONUTF8 = "0"

# Testes (baseline atual: 209 passam / 1 falha preexistente em
# test_mercadopago_integration::test_01_carregamento_credenciais, que exige token real no .env)
$env:ESTOKE_DATA_DIR = (New-Item -ItemType Directory -Force "$env:TEMP\estoke_pytest").FullName
& $py -m pytest -q

# Self-test de boot (esperado: exit 0, selftest_report.json com "status": "ok")
$env:ESTOKE_DATA_DIR = (New-Item -ItemType Directory -Force "$env:TEMP\estoke_selftest").FullName
& $py main.py --self-test

# Smoke test da UI: script em ESTOKE_DATA_DIR temporário, com MP/Focus/impressora mockados
# (referência: smoke_pdv_lote6.py do agente anterior; ESTOKE_LOAD_DOTENV=0)

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
12. O repo está no Google Drive: timeout de I/O em teste pode ser do Drive. Rode o teste de novo
    isolado e informe; não mascare.

## Convenções de commit
- Formato: `tipo(escopo): descrição em português, minúscula, sem ponto final`.
- Tipos: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`.
- Escopo por fase/lote: `f1a`, `f1b-loteN`, `f1b-loteNb-r1`, `hotfix-vX.Y.Z`.

## Branches
- `main` (= `origin/main`), `chore/checkpoint-v4.6.0`, `feat/pywebview-f1a`, `feat/pywebview-f1b` (ativa).
- `feat/pywebview-f1a` e `feat/pywebview-f1b` **não estão no origin**.
- Release em produção: v4.6.0 (instaladores em `release/`, gerados antes do checkpoint `5619b61`).

## Roadmap
O roadmap vivo e as anotações fora de escopo ficam em `implementation_plan.md` (na raiz do repo;
enquanto não existir aqui, a última versão está na pasta do agente anterior, em
`%USERPROFILE%\.gemini\antigravity\brain\7f1adfc0-...\implementation_plan.md`).
Ordem atual: hotfix v4.6.1 → higiene (venv, requirements, teste MP `live`, `__version__`)
→ F1b Lote 7 (estoque/kits/produção/trocas) → F1b Lote 8 (restaurante, cadastros, migração v10, API)
→ isolamento por loja restante → F2 (ponte PyWebView) → F3 (UI em `web/`) → pré-release.

## Armadilhas conhecidas
- `database.__version__` ("4.3.0") diverge de `version_info.txt`/`build_local.APP_VERSION` ("4.6.0").
- `contas_pagar` não tem `loja_id`; `pagar_conta()` gera gasto sem loja.
- `servicos` não tem `data_conclusao`; `gerar_dre`/`calcular_comissoes` usam `servicos.data`.
- `resumo_caixa_atual()` recebe `loja_id` mas não filtra por ele.
- Arquivos sensíveis fora do Git: `.env` (credenciais MP/Focus), `../key.pem` (provável chave de assinatura).
  Nunca commitar, nunca imprimir o conteúdo.
