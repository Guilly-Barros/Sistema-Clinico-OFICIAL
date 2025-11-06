from flask import (
    Blueprint, redirect, render_template, request, session,
    url_for, flash, jsonify
)
from functools import wraps
from datetime import datetime
from werkzeug.security import check_password_hash

from databaser import (
    conectar, criar_tabelas, horarios_disponiveis
)

# NOME DO BLUEPRINT *deve* ser "user" para os endpoints ficarem "user.*"
user_bp = Blueprint('user', __name__, template_folder='templates')


# ------------------ Guard de autenticação ------------------
def login_required(role=None):
    def wrapper(fn):
        @wraps(fn)
        def decorated_view(*args, **kwargs):
            if 'usuario_id' not in session:
                return redirect(url_for('user.user'))
            if role:
                tipo = (session.get('usuario_tipo') or '').lower()
                if role == 'recepcionista':
                    if tipo not in ('recepcionista', 'recepcionista master'):
                        return redirect(url_for('user.user'))
                else:
                    if tipo != role:
                        return redirect(url_for('user.user'))
            return fn(*args, **kwargs)
        return decorated_view
    return wrapper


# ------------------ Login ------------------
@user_bp.route("/", methods=["GET", "POST"], endpoint="user")
def user():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        senha = request.form.get("senha", "")

        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT id, nome, tipo_usuario, senha FROM usuarios WHERE email = ?", (email,))
        usuario = cur.fetchone()

        if usuario:
            senha_db = usuario["senha"]
            ok = False
            try:
                ok = check_password_hash(senha_db, senha)
            except Exception:
                ok = False
            if not ok:
                # compatibilidade com alguma senha antiga salva sem hash
                ok = (senha_db == senha)

            if ok:
                session["usuario_id"] = usuario["id"]
                session["usuario_nome"] = usuario["nome"]
                session["usuario_tipo"] = (usuario["tipo_usuario"] or "").lower()

                tipo = session["usuario_tipo"]
                conn.close()
                if   tipo == "medico":                               return redirect(url_for("user.visao_medico"))
                elif tipo == "paciente":                              return redirect(url_for("user.visao_paciente"))
                elif tipo in ("recepcionista", "recepcionista master"): return redirect(url_for("user.visao_recepcionista"))
                else:
                    flash("Tipo de usuário desconhecido!", "danger")
                    return redirect(url_for("user.user"))
            else:
                flash("E-mail ou senha incorretos!", "danger")
        else:
            flash("E-mail ou senha incorretos!", "danger")

        conn.close()

    return render_template("login.html")


# ------------------ Cadastro de paciente ------------------
@user_bp.route("/register", methods=["GET", "POST"], endpoint="register")
def register():
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")

        from werkzeug.security import generate_password_hash
        senha_hash = generate_password_hash(senha)

        conn = conectar()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usuarios (nome, email, senha, tipo_usuario) VALUES (?, ?, ?, ?)",
            (nome, email, senha_hash, "paciente")
        )
        conn.commit()
        conn.close()

        flash("Cadastro realizado! Faça login para continuar.", "success")
        return redirect(url_for("user.user"))

    return render_template("register.html")


# ------------------ Recepção: Agendar consulta ------------------
@user_bp.route("/agendar_consulta", methods=["GET", "POST"], endpoint="agendar_consulta")
@login_required(role='recepcionista')
def agendar_consulta():
    criar_tabelas()  # garante estrutura + seeds (idempotente)

    conn = conectar()
    cur = conn.cursor()

    # Carrega dados para o GET (render) e também para recarregar após POST clássico
    cur.execute("SELECT id, nome FROM usuarios WHERE tipo_usuario = 'paciente'")
    pacientes = cur.fetchall()
    cur.execute("SELECT id, nome FROM usuarios WHERE tipo_usuario IN ('medico','médico')")
    medicos = cur.fetchall()
    cur.execute("SELECT id, nome FROM procedimentos")
    procedimentos = cur.fetchall()
    cur.execute("SELECT id, nome FROM salas")
    salas = cur.fetchall()

    # Se for GET, só renderiza
    if request.method == "GET":
        conn.close()
        return render_template(
            "agendamentoConsulta.html",
            pacientes=pacientes, medicos=medicos, procedimentos=procedimentos, salas=salas
        )

    # POST: aceita tanto formulário normal quanto JSON/AJAX
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json
    data_in = request.get_json(silent=True) if request.is_json else request.form

    try:
        paciente_id     = (data_in.get("paciente_id") or "").strip()
        medico_id       = (data_in.get("medico_id") or "").strip()
        procedimento_id = (data_in.get("procedimento_id") or "").strip()
        sala_id         = (data_in.get("sala_id") or "").strip()
        data_           = (data_in.get("data") or "").strip()
        hora_           = (data_in.get("hora") or "").strip()

        # validações rápidas
        if not (paciente_id and medico_id and sala_id and data_ and hora_ and procedimento_id):
            if is_ajax:
                conn.close()
                return jsonify({"ok": False, "msg": "Preencha todos os campos."}), 400
            flash("Preencha todos os campos.", "danger")
            conn.close()
            return redirect(url_for("user.agendar_consulta"))

        # procedimento fallback textual → converte para ID real
        if not procedimento_id.isdigit():
            nome_map = {
                "__particular__": "Consulta Particular",
                "__convenio__":  "Consulta Convênio",
                "__receita__":   "Solicitação de Receita",
            }
            nome = nome_map.get(procedimento_id, procedimento_id)
            cur.execute("SELECT id FROM procedimentos WHERE nome = ?", (nome,))
            row = cur.fetchone()
            if row:
                procedimento_id = str(row["id"])
            else:
                cur.execute("INSERT INTO procedimentos (nome, descricao) VALUES (?, ?)", (nome, ""))
                conn.commit()
                procedimento_id = str(cur.lastrowid)

        # conflito sala+data+hora
        cur.execute("SELECT 1 FROM agendamentos WHERE data=? AND hora=? AND sala_id=?", (data_, hora_, sala_id))
        if cur.fetchone():
            if is_ajax:
                conn.close()
                return jsonify({"ok": False, "msg": "Já existe uma consulta para essa sala nesse horário."}), 409
            flash("Já existe uma consulta marcada para essa sala nesse horário!", "danger")
            conn.close()
            return redirect(url_for("user.agendar_consulta"))

        # insere
        cur.execute(
            """INSERT INTO agendamentos
               (paciente_id, medico_id, procedimento_id, sala_id, data, hora)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (paciente_id, medico_id, procedimento_id, sala_id, data_, hora_)
        )
        conn.commit()
        conn.close()

        if is_ajax:
            return jsonify({"ok": True, "msg": "Consulta agendada com sucesso!"})
        flash("Consulta agendada com sucesso!", "success")
        return redirect(url_for("user.agendar_consulta"))

    except Exception as e:
        conn.close()
        if is_ajax:
            return jsonify({"ok": False, "msg": f"Erro ao agendar: {e}"}), 500
        flash("Erro ao agendar.", "danger")
        return redirect(url_for("user.agendar_consulta"))



# ------------------ Painéis ------------------
@user_bp.route("/recepcionista", endpoint="visao_recepcionista")
@login_required(role='recepcionista')
def visao_recepcionista():
    conn = conectar()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(1) AS q FROM agendamento_ajustes WHERE status='pendente'")
    pend = cur.fetchone()["q"]
    conn.close()
    return render_template("recepcionista.html", pendentes=pend)

@user_bp.route("/medico", endpoint="visao_medico")
@login_required(role='medico')
def visao_medico():
    return render_template("medico.html")

@user_bp.route("/paciente", endpoint="visao_paciente")
@login_required(role='paciente')
def visao_paciente():
    pid = session["usuario_id"]
    conn = conectar()
    cur = conn.cursor()

    cur.execute("""
        SELECT a.id, a.data, a.hora, a.medico_id, a.sala_id,
               s.nome AS sala, u.nome AS medico, p.nome AS procedimento
        FROM agendamentos a
        JOIN salas s ON s.id=a.sala_id
        JOIN usuarios u ON u.id=a.medico_id
        JOIN procedimentos p ON p.id=a.procedimento_id
        WHERE a.paciente_id=?
        ORDER BY a.data, a.hora
    """, (pid,))
    ags = cur.fetchall()

    cur.execute("""
        SELECT j.*, a.data AS data_atual, a.hora AS hora_atual
        FROM agendamento_ajustes j
        JOIN agendamentos a ON a.id=j.agendamento_id
        WHERE a.paciente_id=?
        ORDER BY j.id DESC
    """, (pid,))
    ajustes = cur.fetchall()

    conn.close()
    return render_template("paciente.html", agendamentos=ags, ajustes=ajustes)


# ------------------ Paciente: solicitar ajuste ------------------
@user_bp.route("/paciente/solicitar_ajuste/<int:agendamento_id>", methods=["POST"], endpoint="solicitar_ajuste")
@login_required(role='paciente')
def solicitar_ajuste(agendamento_id):
    novo_dia  = request.form.get("novo_dia")
    nova_hora = request.form.get("nova_hora")
    motivo    = request.form.get("motivo", "").strip()[:240]
    now = datetime.utcnow().isoformat()

    conn = conectar()
    cur = conn.cursor()

    # valida: agendamento pertence ao paciente autenticado
    cur.execute(
        "SELECT id, medico_id, sala_id, data, hora FROM agendamentos WHERE id=? AND paciente_id=?",
        (agendamento_id, session["usuario_id"])
    )
    agendamento = cur.fetchone()

    if not agendamento:
        conn.close()
        flash("Agendamento inválido.", "danger")
        return redirect(url_for("user.visao_paciente"))

    if not novo_dia or not nova_hora:
        conn.close()
        flash("Informe novo dia e horário.", "danger")
        return redirect(url_for("user.visao_paciente"))

    livres = horarios_disponiveis(agendamento["medico_id"], agendamento["sala_id"], novo_dia)
    if not (novo_dia == agendamento["data"] and nova_hora == agendamento["hora"]):
        if nova_hora not in livres:
            conn.close()
            flash("Horário indisponível. Escolha outra opção.", "danger")
            return redirect(url_for("user.visao_paciente"))

    cur.execute("""
        INSERT INTO agendamento_ajustes (agendamento_id, novo_dia, nova_hora, motivo, status, criado_em)
        VALUES (?, ?, ?, ?, 'pendente', ?)
    """, (agendamento_id, novo_dia, nova_hora, motivo, now))
    conn.commit()
    conn.close()

    flash("Solicitação enviada à recepção.", "success")
    return redirect(url_for("user.visao_paciente"))


# ------------------ Recepção: lista & decisão de ajustes ------------------
@user_bp.route("/recepcionista/ajustes", endpoint="lista_ajustes")
@login_required(role='recepcionista')
def lista_ajustes():
    conn = conectar()
    cur = conn.cursor()
    cur.execute("""
        SELECT j.*, a.paciente_id, a.medico_id, a.sala_id, a.data AS data_atual, a.hora AS hora_atual,
               p.nome AS paciente, m.nome AS medico, s.nome AS sala
        FROM agendamento_ajustes j
        JOIN agendamentos a ON a.id=j.agendamento_id
        JOIN usuarios p ON p.id=a.paciente_id
        JOIN usuarios m ON m.id=a.medico_id
        JOIN salas s ON s.id=a.sala_id
        WHERE j.status='pendente'
        ORDER BY j.id ASC
    """)
    pendentes = cur.fetchall()
    conn.close()
    return render_template("recep_ajustes.html", pendentes=pendentes)

@user_bp.route("/recepcionista/ajustes/<int:ajuste_id>/decidir", methods=["POST"], endpoint="decidir_ajuste")
@login_required(role='recepcionista')
def decidir_ajuste(ajuste_id):
    acao = request.form.get("acao")  # 'aceitar' ou 'negar'

    conn = conectar()
    cur = conn.cursor()
    cur.execute("""
        SELECT j.*, a.medico_id, a.sala_id
        FROM agendamento_ajustes j
        JOIN agendamentos a ON a.id=j.agendamento_id
        WHERE j.id=? AND j.status='pendente'
    """, (ajuste_id,))
    row = cur.fetchone()

    if not row:
        conn.close()
        flash("Solicitação não encontrada.", "danger")
        return redirect(url_for("user.lista_ajustes"))

    if acao == "negar":
        cur.execute("UPDATE agendamento_ajustes SET status='negado' WHERE id=?", (ajuste_id,))
        conn.commit()
        conn.close()
        flash("Solicitação negada.", "warning")
        return redirect(url_for("user.lista_ajustes"))

    # aceitar → checa disponibilidade
    livres = horarios_disponiveis(row["medico_id"], row["sala_id"], row["novo_dia"])
    if row["nova_hora"] not in livres:
        conn.close()
        flash("Horário indisponível. Escolha outro horário.", "danger")
        return redirect(url_for("user.lista_ajustes"))

    # aplica ajuste
    cur.execute("UPDATE agendamentos SET data=?, hora=? WHERE id=?", (row["novo_dia"], row["nova_hora"], row["agendamento_id"]))
    cur.execute("UPDATE agendamento_ajustes SET status='aceito' WHERE id=?", (ajuste_id,))
    conn.commit()
    conn.close()
    flash("Solicitação aceita e agendamento atualizado.", "success")
    return redirect(url_for("user.lista_ajustes"))


# ------------------ Auxiliar: horários disponíveis (AJAX) ------------------
@user_bp.route("/recepcionista/horarios_disponiveis", endpoint="horarios_api")
@login_required(role='recepcionista')
def horarios_api():
    medico_id = int(request.args.get("medico_id"))
    sala_id   = int(request.args.get("sala_id"))
    dia       = request.args.get("dia")  # YYYY-MM-DD
    return jsonify(horarios_disponiveis(medico_id, sala_id, dia))


@user_bp.route("/paciente/horarios_disponiveis", endpoint="paciente_horarios_api")
@login_required(role='paciente')
def paciente_horarios_api():
    try:
        agendamento_id = int(request.args.get("agendamento_id", "0"))
    except ValueError:
        return jsonify({"ok": False, "msg": "Agendamento inválido."}), 400

    dia = (request.args.get("dia") or "").strip()
    if not dia:
        return jsonify({"ok": False, "msg": "Informe o dia."}), 400

    conn = conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT medico_id, sala_id, data, hora FROM agendamentos WHERE id=? AND paciente_id=?",
        (agendamento_id, session["usuario_id"])
    )
    agendamento = cur.fetchone()
    conn.close()

    if not agendamento:
        return jsonify({"ok": False, "msg": "Agendamento não encontrado."}), 404

    livres = horarios_disponiveis(agendamento["medico_id"], agendamento["sala_id"], dia)
    return jsonify(livres)


# ------------------ Recepção: criar usuários ------------------
@user_bp.route("/cadastrar_usuarios", methods=["GET", "POST"], endpoint="cadastrar_usuarios")
@login_required(role='recepcionista')
def cadastrar_usuarios():
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        tipo_usuario = request.form.get("tipo_usuario", "").lower()

        from werkzeug.security import generate_password_hash
        senha_hash = generate_password_hash(senha)

        conn = conectar()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usuarios (nome, email, senha, tipo_usuario) VALUES (?, ?, ?, ?)",
            (nome, email, senha_hash, tipo_usuario)
        )
        conn.commit()
        conn.close()

        flash(f"Usuário cadastrado: {nome} ({tipo_usuario})", "success")
        return redirect(url_for("user.visao_recepcionista"))

    return render_template("cadastrarUsuarios.html")
