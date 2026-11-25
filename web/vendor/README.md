# web/vendor

Bibliotecas de terceiros usadas pelo frontend v2.0 ficam **copiadas aqui**, nunca
carregadas de CDN (`unpkg`, `jsdelivr`, `cdnjs`, Google Fonts remoto etc.).

O Marco 1 não usa nenhuma biblioteca externa: o shell é HTML + CSS + ES modules puros.
Ao adicionar uma lib em marcos futuros, registre aqui nome, versão, licença e origem,
e confirme que `tests/test_bridge_v2.py` (contrato offline) continua passando.
