# -*- coding: utf-8 -*-
import sqlite3

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

STATUS_AGENDAMENTO = [
    ("agendado", "Agendado"),
    ("em atendimento", "Em atendimento"),
    ("concluido", "Concluído"),
    ("cancelado", "Cancelado"),
]

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
                if tipo == "medico":
                    return redirect(url_for("user.visao_medico"))
                if tipo == "paciente":
                    return redirect(url_for("user.visao_paciente"))
                if tipo in ("recepcionista", "recepcionista master"):
                    return redirect(url_for("user.visao_recepcionista"))
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

        # procedimento fallback textual -> converte para ID real
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


@user_bp.route("/recepcionista/procedimentos", methods=["GET"], endpoint="procedimentos")
@login_required(role='recepcionista')
def procedimentos():
    criar_tabelas()

    conn = conectar()
    cur = conn.cursor()

    cur.execute("SELECT id, nome, descricao FROM procedimentos ORDER BY nome")
    procedimentos = cur.fetchall()

    cur.execute(
        """
        SELECT a.id, a.data, a.hora, a.status,
               a.medico_id, a.sala_id, a.procedimento_id,
               pac.nome AS paciente, med.nome AS medico,
               pr.nome AS procedimento, s.nome AS sala
        FROM agendamentos a
        JOIN usuarios pac ON pac.id = a.paciente_id
        JOIN usuarios med ON med.id = a.medico_id
        JOIN procedimentos pr ON pr.id = a.procedimento_id
        JOIN salas s ON s.id = a.sala_id
        ORDER BY a.data, a.hora
        """
    )
    agendamentos = cur.fetchall()

    conn.close()

    return render_template(
        "recep_procedimentos.html",
        procedimentos=procedimentos,
        agendamentos=agendamentos,
        status_opcoes=STATUS_AGENDAMENTO,
    )


@user_bp.route("/recepcionista/procedimentos/novo", methods=["POST"], endpoint="criar_procedimento")
@login_required(role='recepcionista')
def criar_procedimento():
    nome = (request.form.get("nome") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()

    if not nome:
        flash("Informe o nome do procedimento.", "danger")
        return redirect(url_for("user.procedimentos"))

    conn = conectar()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO procedimentos (nome, descricao) VALUES (?, ?)",
            (nome, descricao)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        flash("Já existe um procedimento com esse nome.", "warning")
        return redirect(url_for("user.procedimentos"))

    conn.close()
    flash("Procedimento cadastrado com sucesso!", "success")
    return redirect(url_for("user.procedimentos"))


@user_bp.route(
    "/recepcionista/procedimentos/<int:procedimento_id>/editar",
    methods=["POST"],
    endpoint="editar_procedimento"
)
@login_required(role='recepcionista')
def editar_procedimento(procedimento_id):
    nome = (request.form.get("nome") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()

    if not nome:
        flash("Informe o nome do procedimento.", "danger")
        return redirect(url_for("user.procedimentos"))

    conn = conectar()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE procedimentos SET nome=?, descricao=? WHERE id=?",
            (nome, descricao, procedimento_id)
        )
        if cur.rowcount == 0:
            conn.close()
            flash("Procedimento não encontrado.", "danger")
            return redirect(url_for("user.procedimentos"))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        flash("Já existe um procedimento com esse nome.", "warning")
        return redirect(url_for("user.procedimentos"))

    conn.close()
    flash("Procedimento atualizado com sucesso!", "success")
    return redirect(url_for("user.procedimentos"))


@user_bp.route(
    "/recepcionista/procedimentos/agendamentos/<int:agendamento_id>",
    methods=["POST"],
    endpoint="atualizar_agendamento"
)
@login_required(role='recepcionista')
def atualizar_agendamento(agendamento_id):
    status = (request.form.get("status") or "").strip().lower()
    nova_data = (request.form.get("data") or "").strip()
    nova_hora = (request.form.get("hora") or "").strip()

    status_validos = {valor for valor, _ in STATUS_AGENDAMENTO}
    if status and status not in status_validos:
        flash("Status inválido.", "danger")
        return redirect(url_for("user.procedimentos"))

    if (nova_data and not nova_hora) or (nova_hora and not nova_data):
        flash("Informe data e horário para alterar o agendamento.", "warning")
        return redirect(url_for("user.procedimentos"))

    conn = conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT medico_id, sala_id, data, hora, status FROM agendamentos WHERE id=?",
        (agendamento_id,)
    )
    atual = cur.fetchone()

    if not atual:
        conn.close()
        flash("Agendamento não encontrado.", "danger")
        return redirect(url_for("user.procedimentos"))

    campos = []
    valores = []

    if status and status != atual["status"]:
        campos.append("status=?")
        valores.append(status)

    alterar_horario = False

    if nova_data and nova_hora:
        try:
            datetime.strptime(nova_data, "%Y-%m-%d")
            datetime.strptime(nova_hora, "%H:%M")
        except ValueError:
            conn.close()
            flash("Formato de data ou hora inválido.", "danger")
            return redirect(url_for("user.procedimentos"))

        if nova_data != atual["data"] or nova_hora != atual["hora"]:
            livres = horarios_disponiveis(
                atual["medico_id"],
                atual["sala_id"],
                nova_data,
                ignorar_agendamento_id=agendamento_id,
            )
            if nova_hora not in livres:
                conn.close()
                flash("Horário indisponível para este médico ou sala.", "danger")
                return redirect(url_for("user.procedimentos"))
            alterar_horario = True
        else:
            alterar_horario = False

        if alterar_horario:
            campos.extend(["data=?", "hora=?"])
            valores.extend([nova_data, nova_hora])

    if not campos:
        conn.close()
        flash("Nenhuma alteração informada.", "info")
        return redirect(url_for("user.procedimentos"))

    valores.append(agendamento_id)
    cur.execute(f"UPDATE agendamentos SET {', '.join(campos)} WHERE id=?", valores)
    conn.commit()
    conn.close()

    flash("Agendamento atualizado com sucesso!", "success")
    return redirect(url_for("user.procedimentos"))

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

    # aceitar -> checa disponibilidade
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
    try:
        medico_id = int(request.args.get("medico_id", "0"))
        sala_id = int(request.args.get("sala_id", "0"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "Parâmetros inválidos."}), 400

    dia = (request.args.get("dia") or "").strip()  # YYYY-MM-DD
    if not dia:
        return jsonify([])

    ignorar_id = request.args.get("ignorar_id")
    try:
        ignorar_id_int = int(ignorar_id) if ignorar_id is not None else None
    except ValueError:
        ignorar_id_int = None

    return jsonify(
        horarios_disponiveis(
            medico_id,
            sala_id,
            dia,
            ignorar_agendamento_id=ignorar_id_int,
        )
    )


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
