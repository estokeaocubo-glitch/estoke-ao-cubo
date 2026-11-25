"""
API REST para integração com o app mobile EstokeAoCubo Android.
Usa Flask como servidor local leve com rate-limiting.
"""
import os
import sys
import json
import logging
import datetime
import threading
import hashlib
import time
import secrets
from collections import defaultdict

logger = logging.getLogger('EstokeAoCubo.api')

FLASK_AVAILABLE = False
try:
    from flask import Flask, jsonify, request, Response, render_template_string
    FLASK_AVAILABLE = True
except ImportError:
    logger.info("Flask não instalado. API REST indisponível. Instale com: pip install flask")


def api_disponivel():
    return FLASK_AVAILABLE


class RateLimiter:
    """Rate limiter simples por IP — sem dependências externas."""

    def __init__(self, max_requests=60, window_secs=60):
        self.max_requests = max_requests
        self.window_secs = window_secs
        self._hits = defaultdict(list)

    def is_allowed(self, key):
        now = time.monotonic()
        hits = self._hits[key]
        # Remover hits fora da janela
        self._hits[key] = [t for t in hits if now - t < self.window_secs]
        if len(self._hits[key]) >= self.max_requests:
            return False
        self._hits[key].append(now)
        return True


class EstokeAPI:
    """Servidor REST API local para comunicação com o app mobile."""

    def __init__(self, db, port=5050):
        self.db = db
        self.port = port
        self._thread = None
        self._server = None
        self._last_error = None
        self._running = False
        self.app = None
        self._rate_limiter = RateLimiter(max_requests=60, window_secs=60)
        self._login_limiter = RateLimiter(max_requests=5, window_secs=60)
        self._sessions = {}  # token -> {"user": {...}, "expires": timestamp}
        self._sessions_lock = threading.RLock()
        self._SESSION_TTL = 8 * 3600  # 8 horas

        if FLASK_AVAILABLE:
            self.app = Flask(__name__)
            self.app.config['JSON_AS_ASCII'] = False
            self._register_routes()

    def _cleanup_sessions(self):
        """Remove sessões expiradas de forma thread-safe."""
        now = time.time()
        with self._sessions_lock:
            expired = [t for t, s in list(self._sessions.items()) if s["expires"] < now]
            for t in expired:
                self._sessions.pop(t, None)

    def criar_sessao_pareamento(self, usuario: dict, ip_local: str | None = None) -> dict:
        """
        Cria sessão de pareamento mobile para um usuário já autenticado na sessão desktop.
        Não recebe nem armazena senha; retorna payload JSON pronto para QR Code.
        """
        if not isinstance(usuario, dict) or not usuario.get("id"):
            raise ValueError("Usuário autenticado inválido para pareamento mobile.")

        # Sanitizar cópia do usuário para nunca reter campos sensíveis na sessão
        user_safe = {
            k: v for k, v in usuario.items()
            if k not in ("senha", "senha_hash", "password", "password_hash")
        }
        login_val = str(user_safe.get("login") or "").strip()
        if not login_val:
            # db.autenticar_usuario não inclui 'login' no dict retornado; resolve pelo id
            with self.db._lock:
                if hasattr(self.db, "_ensure_open"):
                    self.db._ensure_open()
                cur = self.db.conn.cursor()
                cur.execute("SELECT login FROM usuarios WHERE id = ?", (user_safe["id"],))
                row_login = cur.fetchone()
            login_val = str(row_login[0]).strip() if (row_login and row_login[0]) else str(user_safe.get("nome") or "")
        if not login_val:
            raise ValueError("Login do usuário não encontrado para pareamento mobile.")
        user_safe["login"] = login_val

        ip_resolvido = str(ip_local or "").strip()
        if not ip_resolvido:
            try:
                import socket as _sock
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                try:
                    s.connect(("8.8.8.8", 80))
                    ip_resolvido = s.getsockname()[0]
                finally:
                    s.close()
            except Exception:
                ip_resolvido = "127.0.0.1"

        token = secrets.token_hex(32)
        with self._sessions_lock:
            self._sessions[token] = {
                "user": user_safe,
                "expires": time.time() + self._SESSION_TTL,
            }

        qr_payload = json.dumps(
            {"ip": ip_resolvido, "port": self.port, "login": login_val, "token": token},
            ensure_ascii=False,
        )
        return {
            "ip": ip_resolvido,
            "port": self.port,
            "login": login_val,
            "token": token,
            "expires_in": self._SESSION_TTL,
            "qr_payload": qr_payload,
        }

    def _require_auth(self, acao_necessaria=None):
        """Verifica token de autenticação, rate limit, atividade do usuário e permissões RBAC."""
        client_ip = request.remote_addr or '0.0.0.0'
        if not self._rate_limiter.is_allowed(client_ip):
            return None, (jsonify({"error": "Rate limit excedido. Aguarde."}), 429)

        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return None, (jsonify({"error": "Token não fornecido"}), 401)

        # Verificar sessão ativa sob lock próprio (sem aninhar com db._lock)
        self._cleanup_sessions()
        with self._sessions_lock:
            session = self._sessions.get(token)
            if not session:
                return None, (jsonify({"error": "Token inválido ou expirado"}), 401)
            user_id = session["user"]["id"]

        # Prevenção de Sessões Órfãs: Verificar status ativo no banco em tempo real
        with self.db._lock:
            if hasattr(self.db, "_ensure_open"):
                self.db._ensure_open()
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT ativo, perfil, loja_id FROM usuarios WHERE id=?", (user_id,))
            row = cursor.fetchone()
        if not row or row[0] != 1:
            with self._sessions_lock:
                self._sessions.pop(token, None)
            return None, (jsonify({"error": "Acesso revogado. Usuário inativo ou excluído."}), 403)

        # Atualizar os dados de perfil e loja na sessão e renovar TTL sob lock
        with self._sessions_lock:
            session = self._sessions.get(token)
            if not session:
                return None, (jsonify({"error": "Token inválido ou expirado"}), 401)
            session["user"]["perfil"] = row[1]
            session["user"]["loja_id"] = row[2]
            perfil_atual = session["user"]["perfil"]
            user_snapshot = dict(session["user"])
            session["expires"] = time.time() + self._SESSION_TTL

        # Verificar permissões do perfil (RBAC)
        if acao_necessaria:
            if not self.db.usuario_tem_permissao(perfil_atual, acao_necessaria):
                return None, (jsonify({"error": f"Acesso negado. Requer permissão para '{acao_necessaria}'."}), 403)

        return user_snapshot, None

    def _register_routes(self):
        app = self.app

        @app.route('/api/health', methods=['GET'])
        def health():
            return jsonify({"status": "ok", "version": "2.4"})

        @app.route('/api/login', methods=['POST'])
        def login():
            try:
                client_ip = request.remote_addr or '0.0.0.0'
                if not self._login_limiter.is_allowed(client_ip):
                    return jsonify({"error": "Muitas tentativas. Aguarde 1 minuto."}), 429

                data = request.get_json(silent=True) or {}
                login_val = data.get('login', '').strip()
                senha = data.get('senha', '').strip()
                if not login_val or not senha:
                    return jsonify({"error": "Login e senha obrigatórios"}), 400

                # Usar autenticação segura do banco (PBKDF2)
                user = self.db.autenticar_usuario(login_val, senha)

                if user:
                    # Gerar token de sessão seguro sob lock de sessões
                    token = secrets.token_hex(32)
                    with self._sessions_lock:
                        self._sessions[token] = {
                            "user": user,
                            "expires": time.time() + self._SESSION_TTL
                        }
                    return jsonify({"user": user, "token": token})
                return jsonify({"error": "Credenciais inválidas"}), 401
            except Exception as e:
                import traceback
                err_msg = traceback.format_exc()
                logger.error("Erro no login API: %s\n%s", e, err_msg)
                return jsonify({"error": str(e)}), 500

        # ---- PRODUTOS ----
        @app.route('/api/produtos', methods=['GET'])
        def listar_produtos():
            user, err = self._require_auth()
            if err:
                return err
            df = self.db.listar_produtos()
            if user['perfil'] != 'admin' and user['loja_id'] is not None and 'loja_id' in df.columns:
                df = df[df['loja_id'] == user['loja_id']]
                
            # Paginação opcional
            limit_val = request.args.get('limit')
            offset_val = request.args.get('offset')
            if limit_val is not None:
                try:
                    limit = int(limit_val)
                    offset = int(offset_val) if offset_val is not None else 0
                    total = len(df)
                    df = df.iloc[offset:offset+limit]
                    return jsonify({
                        "total": total,
                        "limit": limit,
                        "offset": offset,
                        "records": df.to_dict(orient='records')
                    })
                except (ValueError, TypeError):
                    pass
            return jsonify(df.to_dict(orient='records'))

        @app.route('/api/produtos/<int:prod_id>', methods=['GET'])
        def get_produto(prod_id):
            user, err = self._require_auth()
            if err:
                return err
            df = self.db.buscar_por_id('produtos', prod_id)
            if df is not None and not df.empty:
                prod = df.to_dict(orient='records')[0]
                if user['perfil'] != 'admin' and user['loja_id'] is not None and prod.get('loja_id') is not None and prod.get('loja_id') != user['loja_id']:
                    return jsonify({"error": "Acesso negado. Produto pertence a outra loja."}), 403
                return jsonify(prod)
            return jsonify({"error": "Produto não encontrado"}), 404

        @app.route('/api/produtos/barcode/<codigo>', methods=['GET'])
        def get_produto_barcode(codigo):
            user, err = self._require_auth()
            if err:
                return err
            df = self.db.buscar_produto_por_codigo(codigo)
            if not df.empty:
                prod = df.to_dict(orient='records')[0]
                if user['perfil'] != 'admin' and user['loja_id'] is not None and prod.get('loja_id') is not None and prod.get('loja_id') != user['loja_id']:
                    return jsonify({"error": "Acesso negado. Produto pertence a outra loja."}), 403
                return jsonify(prod)
            return jsonify({"error": "Produto não encontrado"}), 404

        # ---- VENDAS ----
        @app.route('/api/vendas', methods=['GET'])
        def listar_vendas():
            user, err = self._require_auth()
            if err:
                return err
            periodo = request.args.get('periodo', 'mes')
            if periodo not in ('dia', 'semana', 'mes', 'ano', 'todos'):
                periodo = 'mes'
            df = self.db.listar_vendas(periodo)
            if user['perfil'] != 'admin' and user['loja_id'] is not None and 'loja_id' in df.columns:
                df = df[df['loja_id'] == user['loja_id']]
                
            # Paginação opcional
            limit_val = request.args.get('limit')
            offset_val = request.args.get('offset')
            if limit_val is not None:
                try:
                    limit = int(limit_val)
                    offset = int(offset_val) if offset_val is not None else 0
                    total = len(df)
                    df = df.iloc[offset:offset+limit]
                    return jsonify({
                        "total": total,
                        "limit": limit,
                        "offset": offset,
                        "records": df.to_dict(orient='records')
                    })
                except (ValueError, TypeError):
                    pass
            return jsonify(df.to_dict(orient='records'))

        @app.route('/api/vendas', methods=['POST'])
        def registrar_venda():
            user, err = self._require_auth()
            if err:
                return err
            if not self.db.usuario_tem_permissao(user['perfil'], 'vender'):
                return jsonify({"error": "Sem permissão"}), 403
            data = request.get_json(silent=True) or {}
            itens = data.get('itens', [])
            total = data.get('total', 0)
            desconto = data.get('desconto', 0)
            cliente = data.get('cliente', 'Consumidor Final')
            forma = data.get('forma_pagamento', 'Dinheiro')
            vendedor = data.get('vendedor', user['nome'])
            try:
                resultado = self.db.registrar_venda_detalhada(itens, total, desconto, cliente, forma, vendedor)
                if isinstance(resultado, tuple) and not resultado[0]:
                    return jsonify({"error": resultado[1]}), 400
                if resultado:
                    return jsonify({"success": True})
                return jsonify({"error": "Falha ao registrar venda"}), 400
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        # ---- ESTOQUE ----
        @app.route('/api/estoque/baixo', methods=['GET'])
        def estoque_baixo():
            user, err = self._require_auth()
            if err:
                return err
            df = self.db.produtos_abaixo_estoque_minimo()
            return jsonify(df.to_dict(orient='records'))

        # ---- CLIENTES ----
        @app.route('/api/clientes', methods=['GET'])
        def listar_clientes():
            user, err = self._require_auth()
            if err:
                return err
            df = self.db.listar_clientes()
            return jsonify(df.to_dict(orient='records'))

        # ---- CAIXA ----
        @app.route('/api/caixa/status', methods=['GET'])
        def caixa_status():
            user, err = self._require_auth()
            if err:
                return err
            caixa = self.db.caixa_aberto()
            if caixa:
                return jsonify({"aberto": True, "caixa": dict(caixa)})
            return jsonify({"aberto": False})

        @app.route('/api/caixa/abrir', methods=['POST'])
        def abrir_caixa():
            user, err = self._require_auth()
            if err:
                return err
            if not self.db.usuario_tem_permissao(user['perfil'], 'caixa'):
                return jsonify({"error": "Sem permissão"}), 403
            data = request.get_json(silent=True) or {}
            valor = data.get('valor_abertura', 0)
            obs = data.get('observacao', 'Abertura automática via Bot')
            ok, msg = self.db.abrir_caixa(valor, obs)
            if ok:
                return jsonify({"success": True, "message": msg})
            return jsonify({"error": msg}), 400

        # ---- DASHBOARD ----
        @app.route('/api/dashboard', methods=['GET'])
        def dashboard():
            user, err = self._require_auth()
            if err:
                return err
            saldo = self.db.get_saldo_atual()
            lucro, faturamento = self.db.lucro_por_periodo("mes")
            alertas = self.db.coletar_alertas()
            return jsonify({
                "saldo": saldo,
                "lucro_mes": lucro,
                "faturamento_mes": faturamento,
                "alertas_count": len(alertas),
            })

        # ---- ALERTAS ----
        @app.route('/api/alertas', methods=['GET'])
        def alertas():
            user, err = self._require_auth()
            if err:
                return err
            alerts = self.db.coletar_alertas()
            # Converter sem dados DataFrame para serialização
            result = []
            for a in alerts:
                item = {k: v for k, v in a.items() if k != 'dados'}
                result.append(item)
            return jsonify(result)

        # ---- DRE ----
        @app.route('/api/dre', methods=['GET'])
        def dre():
            user, err = self._require_auth()
            if err:
                return err
            if not self.db.usuario_tem_permissao(user['perfil'], 'ver_relatorio'):
                return jsonify({"error": "Sem permissão"}), 403
            periodo = request.args.get('periodo', 'mes')
            if periodo not in ('dia', 'semana', 'mes', 'ano', 'todos'):
                periodo = 'mes'
            return jsonify(self.db.gerar_dre(periodo))

        # ---- LOJAS ----
        @app.route('/api/lojas', methods=['GET'])
        def listar_lojas():
            user, err = self._require_auth()
            if err:
                return err
            df = self.db.listar_lojas()
            return jsonify(df.to_dict(orient='records'))

        # ====================================================
        # ENDPOINTS DE MANUTENÇÃO (CRUD) - App Mobile v4.1
        # ====================================================

        # ---- PRODUTOS CRUD ----
        @app.route('/api/produtos', methods=['POST'])
        def criar_produto():
            user, err = self._require_auth(acao_necessaria='adicionar')
            if err:
                return err
            data = request.get_json(silent=True) or {}
            
            import html
            nome = html.escape(str(data.get('nome', '')).strip())
            categoria = html.escape(str(data.get('categoria', 'Geral')).strip()) if data.get('categoria') else 'Geral'
            codigo_barras = html.escape(str(data.get('codigo_barras', '')).strip()) if data.get('codigo_barras') else None
            
            if not nome:
                return jsonify({"error": "Nome obrigatório"}), 400
            try:
                self.db.adicionar_produto(
                    nome=nome,
                    quantidade=int(data.get('quantidade', 0)),
                    preco_venda=float(data.get('preco_venda', 0)),
                    categoria=categoria,
                    preco_compra=float(data.get('preco_compra', 0)) if data.get('preco_compra') else None,
                    codigo_barras=codigo_barras,
                    data_validade=data.get('data_validade'),
                    loja_id=user['loja_id'] if user['perfil'] != 'admin' else data.get('loja_id')
                )
                return jsonify({"success": True}), 201
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        @app.route('/api/produtos/<int:prod_id>', methods=['PUT'])
        def atualizar_produto(prod_id):
            user, err = self._require_auth(acao_necessaria='editar')
            if err:
                return err
            
            # Tenancy Isolation (BOLA) Check
            with self.db._lock:
                if hasattr(self.db, '_ensure_open'):
                    self.db._ensure_open()
                cursor = self.db.conn.cursor()
                cursor.execute("SELECT loja_id FROM produtos WHERE id=?", (prod_id,))
                p_row = cursor.fetchone()
            if not p_row:
                return jsonify({"error": "Produto não encontrado"}), 404
            if user['perfil'] != 'admin' and user['loja_id'] is not None and p_row[0] is not None and p_row[0] != user['loja_id']:
                return jsonify({"error": "Acesso negado. Produto pertence a outra loja."}), 403

            data = request.get_json(silent=True) or {}
            
            import html
            nome = html.escape(str(data.get('nome', '')).strip())
            categoria = html.escape(str(data.get('categoria', 'Geral')).strip()) if data.get('categoria') else 'Geral'
            codigo_barras = html.escape(str(data.get('codigo_barras', '')).strip()) if data.get('codigo_barras') else None
            
            if not nome:
                return jsonify({"error": "Nome obrigatório"}), 400
            try:
                self.db.atualizar_produto(
                    id_produto=prod_id,
                    nome=nome,
                    quantidade=int(data.get('quantidade', 0)),
                    preco=float(data.get('preco_venda', 0)),
                    categoria=categoria,
                    codigo_barras=codigo_barras,
                )
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        @app.route('/api/produtos/<int:prod_id>', methods=['DELETE'])
        def deletar_produto(prod_id):
            user, err = self._require_auth(acao_necessaria='remover')
            if err:
                return err
            
            # Tenancy Isolation (BOLA) Check
            with self.db._lock:
                if hasattr(self.db, '_ensure_open'):
                    self.db._ensure_open()
                cursor = self.db.conn.cursor()
                cursor.execute("SELECT loja_id FROM produtos WHERE id=?", (prod_id,))
                p_row = cursor.fetchone()
            if not p_row:
                return jsonify({"error": "Produto não encontrado"}), 404
            if user['perfil'] != 'admin' and user['loja_id'] is not None and p_row[0] is not None and p_row[0] != user['loja_id']:
                return jsonify({"error": "Acesso negado. Produto pertence a outra loja."}), 403

            try:
                self.db.remover_produto(prod_id)
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        @app.route('/api/produtos/<int:prod_id>/estoque', methods=['PUT'])
        def ajustar_estoque(prod_id):
            user, err = self._require_auth(acao_necessaria='editar')
            if err:
                return err
            
            # Tenancy Isolation (BOLA) Check
            with self.db._lock:
                if hasattr(self.db, '_ensure_open'):
                    self.db._ensure_open()
                cursor = self.db.conn.cursor()
                cursor.execute("SELECT loja_id FROM produtos WHERE id=?", (prod_id,))
                p_row = cursor.fetchone()
            if not p_row:
                return jsonify({"error": "Produto não encontrado"}), 404
            if user['perfil'] != 'admin' and user['loja_id'] is not None and p_row[0] is not None and p_row[0] != user['loja_id']:
                return jsonify({"error": "Acesso negado. Produto pertence a outra loja."}), 403

            data = request.get_json(silent=True) or {}
            quantidade_raw = data.get('quantidade')
            if quantidade_raw is None:
                return jsonify({"error": "Quantidade obrigatória"}), 400
            try:
                quantidade = int(quantidade_raw)
            except (ValueError, TypeError):
                return jsonify({"error": "Quantidade deve ser um inteiro válido"}), 400
                
            if quantidade < 0:
                return jsonify({"error": "Quantidade não pode ser negativa"}), 400
            try:
                self.db.reabastecer_produto(prod_id, quantidade)
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        # ---- CLIENTES CRUD ----
        @app.route('/api/clientes', methods=['POST'])
        def criar_cliente():
            user, err = self._require_auth()
            if err:
                return err
            data = request.get_json(silent=True) or {}
            nome = data.get('nome', '').strip()
            if not nome:
                return jsonify({"error": "Nome obrigatório"}), 400
            try:
                self.db.adicionar_cliente(nome, data.get('contato', ''), data.get('observacoes', ''))
                return jsonify({"success": True}), 201
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        @app.route('/api/clientes/<int:cli_id>', methods=['PUT'])
        def atualizar_cliente(cli_id):
            user, err = self._require_auth()
            if err:
                return err
            data = request.get_json(silent=True) or {}
            nome = data.get('nome', '').strip()
            if not nome:
                return jsonify({"error": "Nome obrigatório"}), 400
            try:
                self.db.atualizar_cliente(cli_id, nome, data.get('contato', ''), data.get('observacoes', ''))
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        @app.route('/api/clientes/<int:cli_id>', methods=['DELETE'])
        def deletar_cliente(cli_id):
            user, err = self._require_auth()
            if err:
                return err
            try:
                self.db.remover_cliente(cli_id)
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        # ---- TAREFAS (Estabelecimento) CRUD ----
        @app.route('/api/tarefas', methods=['GET'])
        def listar_tarefas():
            user, err = self._require_auth()
            if err:
                return err
            df = self.db.listar_tarefas_estabelecimento()
            return jsonify(df.to_dict(orient='records'))

        @app.route('/api/tarefas', methods=['POST'])
        def criar_tarefa():
            user, err = self._require_auth()
            if err:
                return err
            data = request.get_json(silent=True) or {}
            tarefa = data.get('tarefa', '').strip()
            if not tarefa:
                return jsonify({"error": "Tarefa obrigatória"}), 400
            try:
                self.db.adicionar_tarefa_estabelecimento(
                    area_local=data.get('area_local', ''),
                    tarefa=tarefa,
                    prioridade=data.get('prioridade', 'Média'),
                    status=data.get('status', 'Pendente'),
                )
                return jsonify({"success": True}), 201
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        @app.route('/api/tarefas/<int:tarefa_id>', methods=['PUT'])
        def atualizar_tarefa(tarefa_id):
            user, err = self._require_auth()
            if err:
                return err
            data = request.get_json(silent=True) or {}
            try:
                if 'status' in data and len(data) == 1:
                    self.db.atualizar_status_estabelecimento(tarefa_id, data['status'])
                else:
                    self.db.atualizar_tarefa_estabelecimento(
                        tarefa_id,
                        area_local=data.get('area_local', ''),
                        tarefa=data.get('tarefa', ''),
                        prioridade=data.get('prioridade', 'Média'),
                        status=data.get('status', 'Pendente'),
                    )
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        @app.route('/api/tarefas/<int:tarefa_id>', methods=['DELETE'])
        def deletar_tarefa(tarefa_id):
            user, err = self._require_auth()
            if err:
                return err
            try:
                self.db.remover_tarefa_estabelecimento(tarefa_id)
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        # ---- CONTAS A PAGAR ----
        @app.route('/api/contas-pagar', methods=['GET'])
        def listar_contas_pagar():
            user, err = self._require_auth()
            if err:
                return err
            status = request.args.get('status')
            # Lote H2: não-admin vê só a própria loja (+ legadas sem loja); admin pode filtrar por ?loja_id=
            if user['perfil'] != 'admin' and user['loja_id'] is not None:
                loja_id = user['loja_id']
            else:
                loja_id = request.args.get('loja_id', type=int)
            df = self.db.listar_contas_pagar(status=status, loja_id=loja_id)
            return jsonify(df.to_dict(orient='records'))

        @app.route('/api/contas-pagar/<int:conta_id>/pagar', methods=['PUT'])
        def pagar_conta(conta_id):
            user, err = self._require_auth()
            if err:
                return err
            # Tenancy Isolation (BOLA) Check (Lote H2)
            with self.db._lock:
                if hasattr(self.db, '_ensure_open'):
                    self.db._ensure_open()
                cursor = self.db.conn.cursor()
                cursor.execute("SELECT loja_id FROM contas_pagar WHERE id=?", (conta_id,))
                c_row = cursor.fetchone()
            if not c_row:
                return jsonify({"error": "Conta não encontrada"}), 404
            if user['perfil'] != 'admin' and user['loja_id'] is not None and c_row[0] is not None and c_row[0] != user['loja_id']:
                return jsonify({"error": "Acesso negado. Conta pertence a outra loja."}), 403
            try:
                self.db.pagar_conta(conta_id, loja_id=user.get('loja_id'))
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        # ---- CATEGORIAS ----
        @app.route('/api/categorias', methods=['GET'])
        def listar_categorias():
            user, err = self._require_auth()
            if err:
                return err
            tipo = request.args.get('tipo', 'produto')
            cats = self.db.listar_categorias(tipo=tipo)
            return jsonify(cats)

        # ---- FIADOS ----
        @app.route('/api/fiados', methods=['GET'])
        def listar_fiados():
            user, err = self._require_auth()
            if err:
                return err
            status = request.args.get('status')
            df = self.db.listar_fiados(status=status)
            return jsonify(df.to_dict(orient='records'))

        # ---- METAS ----
        @app.route('/api/metas', methods=['GET'])
        def listar_metas():
            user, err = self._require_auth()
            if err:
                return err
            df = self.db.listar_metas()
            return jsonify(df.to_dict(orient='records'))

        # ====================================================
        # MERCADO PAGO PAGAMENTOS & NOTA FISCAL WEB
        # ====================================================
        @app.route('/api/pagamento/config', methods=['GET'])
        def pagamento_config():
            from services.mercadopago_service import get_mercadopago_service
            mp = get_mercadopago_service()
            return jsonify(mp.get_public_config())

        @app.route('/api/pagamento/pix', methods=['POST'])
        def pagamento_pix():
            client_ip = request.remote_addr or '0.0.0.0'
            if not self._rate_limiter.is_allowed(client_ip):
                return jsonify({"error": "Rate limit excedido. Aguarde."}), 429
            data = request.get_json(silent=True) or {}
            try:
                valor = float(data.get('valor', 0))
            except (ValueError, TypeError):
                valor = 0
            if valor <= 0:
                return jsonify({"error": "Valor inválido"}), 400
            
            descricao = data.get('descricao', 'Venda Estoke ao Cubo')
            email = data.get('email')
            nome = data.get('nome')
            id_venda = data.get('id_venda')

            from services.mercadopago_service import get_mercadopago_service
            mp = get_mercadopago_service()
            res = mp.criar_cobranca_pix(
                valor=valor,
                descricao=descricao,
                cliente_email=email,
                cliente_nome=nome,
                id_venda=id_venda
            )
            if res.get('sucesso'):
                return jsonify(res), 200
            return jsonify(res), 400

        @app.route('/api/pagamento/status/<payment_id>', methods=['GET'])
        def pagamento_status(payment_id):
            client_ip = request.remote_addr or '0.0.0.0'
            if not self._rate_limiter.is_allowed(client_ip):
                return jsonify({"error": "Rate limit excedido. Aguarde."}), 429
            from services.mercadopago_service import get_mercadopago_service
            mp = get_mercadopago_service()
            res = mp.consultar_pagamento(payment_id)
            if res.get("sucesso") and hasattr(self.db, "registrar_pagamento_mp"):
                try:
                    metodo = str(res.get("metodo_pagamento") or "").lower()
                    tipo_mp = "pix" if metodo == "pix" else "cartao"
                    self.db.registrar_pagamento_mp(
                        payment_id=str(res.get("payment_id") or payment_id),
                        tipo=tipo_mp,
                        external_reference=str(res.get("external_reference") or ""),
                        status=str(res.get("status") or "pending"),
                        status_detail=str(res.get("status_detail") or ""),
                        valor=float(res.get("valor") or 0.0),
                        payload=res,
                        data_aprovacao=str(res.get("data_aprovacao") or ""),
                    )
                except Exception as exc:
                    logger.warning("Erro ao persistir consulta MP %s: %s", payment_id, exc)
            return jsonify(res)

        @app.route('/api/pagamento/cartao', methods=['POST'])
        def pagamento_cartao():
            client_ip = request.remote_addr or '0.0.0.0'
            if not self._rate_limiter.is_allowed(client_ip):
                return jsonify({"error": "Rate limit excedido. Aguarde."}), 429
            data = request.get_json(silent=True) or {}
            token = data.get('token')
            try:
                valor = float(data.get('valor', 0))
            except (ValueError, TypeError):
                valor = 0
            parcelas = int(data.get('parcelas', 1))
            method_id = data.get('payment_method_id', 'master')
            email = data.get('email')
            id_venda = data.get('id_venda')

            if not token or valor <= 0:
                return jsonify({"error": "Token e valor válidos são obrigatórios"}), 400

            from services.mercadopago_service import get_mercadopago_service
            mp = get_mercadopago_service()
            res = mp.processar_pagamento_cartao(
                token=token,
                valor=valor,
                parcelas=parcelas,
                payment_method_id=method_id,
                cliente_email=email,
                id_venda=id_venda
            )
            return jsonify(res)

        @app.route('/api/pagamento/webhook', methods=['POST'])
        def pagamento_webhook():
            data = request.get_json(silent=True) or {}
            logger.info("Webhook Mercado Pago recebido: %s", data)
            from services.mercadopago_service import get_mercadopago_service
            mp = get_mercadopago_service()

            data_id = str((data.get("data") or {}).get("id") or data.get("id") or request.args.get("data.id") or "")
            x_sig = request.headers.get("x-signature", "")
            x_req_id = request.headers.get("x-request-id", "")

            if not mp.validar_assinatura_webhook(x_sig, x_req_id, data_id):
                logger.warning("Assinatura de webhook Mercado Pago inválida.")
                return jsonify({"error": "Assinatura inválida"}), 401

            if data_id and mp.esta_configurado():
                try:
                    info = mp.consultar_pagamento(data_id)
                    if info.get("sucesso") and hasattr(self.db, "registrar_pagamento_mp"):
                        metodo = str(info.get("metodo_pagamento") or "").lower()
                        tipo_mp = "pix" if metodo == "pix" else ("point" if metodo in ("debit_card", "credit_card") and info.get("point_intent_id") else "cartao")
                        self.db.registrar_pagamento_mp(
                            payment_id=str(info.get("payment_id") or data_id),
                            tipo=tipo_mp,
                            external_reference=str(info.get("external_reference") or data.get("external_reference") or ""),
                            status=str(info.get("status") or "pending"),
                            status_detail=str(info.get("status_detail") or ""),
                            valor=float(info.get("valor") or 0.0),
                            payload=info,
                            data_aprovacao=str(info.get("data_aprovacao") or ""),
                        )
                    if info.get("aprovado"):
                        self.db.registrar_alteracao(
                            "Webhook Mercado Pago",
                            f"Pagamento #{data_id} aprovado (R$ {info.get('valor', 0):.2f})",
                            entidade="pagamentos_mp",
                            entidade_id=str(data_id),
                            depois={"status": "approved", "valor": info.get("valor", 0), "external_reference": info.get("external_reference", "")},
                        )
                except Exception as we:
                    logger.warning("Erro ao conciliar webhook MP %s: %s", data_id, we)

            return jsonify({"status": "received"}), 200

        @app.route('/recibo/<venda_id>', methods=['GET'])
        def visualizar_recibo(venda_id):
            """Renderiza a Nota Fiscal no Padrão de Design do Drive diretamente pelo navegador."""
            client_ip = request.remote_addr or '0.0.0.0'
            if not self._rate_limiter.is_allowed(client_ip):
                return "Rate limit excedido. Aguarde.", 429
            try:
                import invoice_generator
                with self.db._lock:
                    config_nota = self.db.get_config_nota()
                    cursor = self.db.conn.cursor()
                    cursor.execute(
                        "SELECT id, produto_id, quantidade, valor_total, data_venda, cliente, desconto, forma_pagamento, transacao_id FROM vendas WHERE id=?",
                        (venda_id,)
                    )
                    rows = cursor.fetchall()

                    if rows and rows[0][8]:
                        # Se pertencer a uma transação com múltiplos itens, carregar todos os itens da transação
                        cursor.execute(
                            "SELECT id, produto_id, quantidade, valor_total, data_venda, cliente, desconto, forma_pagamento FROM vendas WHERE transacao_id=?",
                            (rows[0][8],)
                        )
                        rows = cursor.fetchall()

                    if rows:
                        itens = []
                        total_bruto = 0.0
                        desconto_total = 0.0
                        for row in rows:
                            cursor.execute("SELECT nome, preco FROM produtos WHERE id=?", (row[1],))
                            p_row = cursor.fetchone()
                            qtd = row[2] or 1
                            val_item = float(row[3] or 0.0)
                            desc_item = float(row[6] or 0.0)
                            prod_nome = p_row[0] if p_row else f"Produto #{row[1] or row[0]}"
                            prod_preco = float(p_row[1]) if (p_row and p_row[1] is not None) else (val_item / max(qtd, 1))
                            itens.append({
                                "nome": prod_nome,
                                "qtd": qtd,
                                "preco": prod_preco,
                                "desconto_item": desc_item
                            })
                            total_bruto += val_item
                            desconto_total += desc_item
                        cliente = {"nome": rows[0][5] or "Consumidor Final"}
                        total_venda = round(total_bruto - desconto_total, 2)
                        desconto = round(desconto_total, 2)
                        forma_pgto = rows[0][7] or "PIX"
                    else:
                        itens = [{"nome": "Venda #" + str(venda_id), "qtd": 1, "preco": 100.0}]
                        cliente = {"nome": "Cliente"}
                        total_venda = 100.0
                        desconto = 0.0
                        forma_pgto = "PIX"

                html = invoice_generator.gerar_nota_html(
                    itens=itens,
                    cliente=cliente,
                    total_venda=total_venda,
                    id_venda=str(venda_id),
                    desconto=desconto,
                    forma_pagamento=forma_pgto,
                    config_nota=config_nota
                )
                return Response(html, mimetype='text/html')
            except Exception as e:
                logger.error("Erro ao gerar recibo web: %s", e)
                return "Erro interno ao gerar recibo.", 500

        @app.route('/', methods=['GET'])
        @app.route('/loja', methods=['GET'])
        def index_page():
            """Serve o site oficial do Estoke ao Cubo com catálogo e checkout Mercado Pago."""
            import paths
            caminhos = [
                paths.get_resource_path("index.html"),
                os.path.join(paths.get_install_dir(), "index.html"),
                os.path.join(os.path.dirname(__file__), "..", "index.html"),
                os.path.join(os.path.dirname(__file__), "index.html"),
            ]
            for c in caminhos:
                if os.path.exists(c):
                    with open(c, 'r', encoding='utf-8') as f:
                        return Response(f.read(), mimetype='text/html')
            return "Site Estoke ao Cubo ativo.", 200

        @app.route('/scanner', methods=['GET'])
        @app.route('/pdv-web', methods=['GET'])
        def scanner_page():
            """Serve o PDV Web e leitor ao vivo com checkout Mercado Pago."""
            import paths
            caminhos = [
                paths.get_resource_path("live_scanner.html"),
                os.path.join(paths.get_install_dir(), "live_scanner.html"),
                os.path.join(os.path.dirname(__file__), "..", "live_scanner.html"),
                os.path.join(os.path.dirname(__file__), "live_scanner.html"),
            ]
            for c in caminhos:
                if os.path.exists(c):
                    with open(c, 'r', encoding='utf-8') as f:
                        return Response(f.read(), mimetype='text/html')
            return "PDV Web ativo.", 200

    def start(self):
        """Inicia o servidor API em background usando WSGI make_server com shutdown controlado."""
        if not FLASK_AVAILABLE or self._running:
            return False
        self._last_error = None
        try:
            from werkzeug.serving import make_server
            if os.name == 'nt' and self.port != 0:
                # No Windows, SO_REUSEADDR do WSGI permite bind duplicado; valida porta livre antes
                import socket as _sock
                _probe = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                try:
                    if hasattr(_sock, 'SO_EXCLUSIVEADDRUSE'):
                        _probe.setsockopt(_sock.SOL_SOCKET, _sock.SO_EXCLUSIVEADDRUSE, 1)
                    _probe.bind(('0.0.0.0', self.port))
                finally:
                    _probe.close()
            self._server = make_server('0.0.0.0', self.port, self.app, threaded=True)
            # Atualiza a porta real alocada pelo SO quando iniciada em porta efêmera (0)
            if self.port == 0:
                self.port = self._server.server_port
        except OSError as e:
            self._server = None
            self._running = False
            self._last_error = (
                f"Não foi possível iniciar a API na porta {self.port}: "
                f"porta já em uso ou indisponível ({e})."
            )
            logger.error("%s", self._last_error)
            return False
        except Exception as e:
            self._server = None
            self._running = False
            self._last_error = f"Erro ao preparar servidor API na porta {self.port}: {e}"
            logger.error("%s", self._last_error)
            return False

        self._running = True
        self._thread = threading.Thread(target=self._run_server, name=f"EstokeAPI-{self.port}", daemon=True)
        self._thread.start()
        logger.info("API REST iniciada na porta %d", self.port)
        return True

    def _run_server(self):
        """Loop do servidor WSGI encerrável via stop()."""
        srv = self._server
        if srv is None:
            self._running = False
            return
        try:
            srv.serve_forever()
        except Exception as e:
            if self._running:
                self._last_error = str(e)
                logger.error("Erro na execução da API: %s", e)
        finally:
            self._running = False

    def stop(self):
        """Encerra o servidor WSGI de forma limpa e libera imediatamente a porta TCP."""
        self._running = False
        srv = self._server
        self._server = None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception as e:
                logger.warning("Aviso ao encerrar loop WSGI da API: %s", e)
            try:
                srv.server_close()
            except Exception as e:
                logger.warning("Aviso ao fechar socket da API: %s", e)
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5.0)
        with self._sessions_lock:
            self._sessions.clear()

    def is_running(self):
        return self._running
