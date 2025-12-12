# -*- coding: utf-8 -*-
import re
import csv
import io
import sqlite3
import calendar

from flask import (
    Blueprint, redirect, render_template, request, session,
    url_for, flash, jsonify
)
from functools import wraps
from datetime import datetime, date, timedelta
from werkzeug.security import check_password_hash, generate_password_hash

from databaser import (
    conectar, criar_tabelas, horarios_disponiveis, get_busy_slots,
    is_slot_available, sugerir_proximo_horario, auto_close_past_appointments
)

STATUS_AGENDAMENTO = [
    ("agendado", "Agendado"),
    ("em atendimento", "Em atendimento"),
    ("concluido", "Concluído"),
    ("cancelado", "Cancelado"),
]
STATUS_LABELS = {valor: rotulo for valor, rotulo in STATUS_AGENDAMENTO}
CONFLICT_TOKENS = ("<<<<<<<", "=======", ">>>>>>>")


def _parse_datetime(data_str: str, hora_str: str):
    return datetime.strptime(f"{data_str} {hora_str}", "%Y-%m-%d %H:%M")


def _validar_data_hora_futura(data_str: str, hora_str: str):
    try:
        dt_alvo = _parse_datetime(_normalizar_data(data_str), _normalizar_hora(hora_str))
    except Exception:
        return False, "Data ou hora inválidas."
    if dt_alvo < datetime.now():
        return False, "Data ou horário no passado não são permitidos."
    return True, ""


def _aplicar_intervalo_mes(filtros):
    inicio = filtros.get("inicio") or ""
    fim = filtros.get("fim") or ""
    if filtros.get("mes"):
        try:
            ano_str, mes_str = filtros["mes"].split("-")
            ano_i = int(ano_str)
            mes_i = int(mes_str)
            inicio_mes = date(ano_i, mes_i, 1)
            fim_mes = date(ano_i, mes_i, calendar.monthrange(ano_i, mes_i)[1])
            if not inicio:
                inicio = inicio_mes.isoformat()
            if not fim:
                fim = fim_mes.isoformat()
        except ValueError:
            pass
    filtros["inicio"] = inicio
    filtros["fim"] = fim
    return filtros


def _buscar_agendamentos_filtrados(filtros):
    filtros = _aplicar_intervalo_mes(dict(filtros))
    condicoes = []
    params = []
    if filtros.get("inicio"):
        condicoes.append("a.data >= ?")
        params.append(filtros["inicio"])
    if filtros.get("fim"):
        condicoes.append("a.data <= ?")
        params.append(filtros["fim"])
    if filtros.get("medico"):
        condicoes.append("a.medico_id = ?")
        params.append(filtros["medico"])
    if filtros.get("paciente"):
        condicoes.append("a.paciente_id = ?")
        params.append(filtros["paciente"])
    if filtros.get("procedimento"):
        condicoes.append("a.procedimento_id = ?")
        params.append(filtros["procedimento"])
    if filtros.get("convenio"):
        condicoes.append("COALESCE(a.convenio, '') LIKE ?")
        params.append(f"%{filtros['convenio']}%")

    base_query = """
        SELECT a.id, a.data, a.hora, a.status, a.convenio,
               pac.nome AS paciente, med.nome AS medico, pr.nome AS procedimento
        FROM agendamentos a
        JOIN usuarios pac ON pac.id = a.paciente_id
        JOIN usuarios med ON med.id = a.medico_id
        JOIN procedimentos pr ON pr.id = a.procedimento_id
    """
    if condicoes:
        base_query += " WHERE " + " AND ".join(condicoes)
    base_query += " ORDER BY a.data, a.hora"

    conn = conectar()
    cur = conn.cursor()
    cur.execute(base_query, params)
    linhas = cur.fetchall()
    conn.close()

    status_validos = {valor for valor, _rotulo in STATUS_AGENDAMENTO}
    agendamentos_filtrados = []
    for row in linhas:
        registro = dict(row)
        status_normalizado = _normalizar_status(registro.get("status", ""), status_validos)
        registro["status"] = status_normalizado
        if isinstance(status_normalizado, str):
            registro["status_label"] = STATUS_LABELS.get(status_normalizado, status_normalizado.title())
        else:
            registro["status_label"] = status_normalizado
        registro["data_display"] = _formatar_data_display(registro.get("data"))
        registro["convenio"] = registro.get("convenio") or "—"
        agendamentos_filtrados.append(registro)

    totais_filtrados = {
        "total": len(agendamentos_filtrados),
        "concluidos": 0,
        "cancelados": 0,
        "agendados": 0,
    }
    for item in agendamentos_filtrados:
        status_val = (item.get("status") or "").lower()
        if status_val == "concluido":
            totais_filtrados["concluidos"] += 1
        elif status_val == "cancelado":
            totais_filtrados["cancelados"] += 1
        elif status_val:
            totais_filtrados["agendados"] += 1
    totais_filtrados["realizados"] = totais_filtrados["concluidos"]

    return agendamentos_filtrados, totais_filtrados, filtros


def _remover_marcadores_conflito(valor):
    if not isinstance(valor, str):
        return valor
    texto = valor.strip()
    if not texto:
        return texto
    if not any(token in texto for token in CONFLICT_TOKENS):
        return texto

    blocos = []
    trecho_atual = []
    for linha in texto.splitlines():
        if linha.startswith("<<<<<<<"):
            trecho_atual = []
            continue
        if linha.startswith("======="):
            blocos.append("\n".join(trecho_atual).strip())
            trecho_atual = []
            continue
        if linha.startswith(">>>>>>>"):
            blocos.append("\n".join(trecho_atual).strip())
            trecho_atual = []
            continue
        trecho_atual.append(linha)

    if trecho_atual:
        blocos.append("\n".join(trecho_atual).strip())

    for bloco in blocos:
        if bloco:
            return bloco

    return texto.replace("<<<<<<<", "").replace("=======", "").replace(">>>>>>>", "").strip()


def _normalizar_status(valor, validos):
    texto = (_remover_marcadores_conflito(valor) or "").strip().lower()
    if not texto:
        return "agendado" if "agendado" in validos else (next(iter(validos)) if validos else texto)

    if texto in validos:
        return texto

    for candidato in validos:
        if candidato in texto:
            return candidato

    return texto


def _normalizar_data(valor):
    texto = (_remover_marcadores_conflito(valor) or "").strip()
    if not texto:
        return texto

    match_iso = re.search(r"\b\d{4}-\d{2}-\d{2}\b", texto)
    if match_iso:
        return match_iso.group(0)

    match_br = re.search(r"\b\d{2}/\d{2}/\d{4}\b", texto)
    if match_br:
        dia, mes, ano = match_br.group(0).split("/")
        return f"{ano}-{mes}-{dia}"

    return texto


def _normalizar_hora(valor):
    texto = (_remover_marcadores_conflito(valor) or "").strip()
    if not texto:
        return texto

    match_hora = re.search(r"\b\d{2}:\d{2}\b", texto)
    if match_hora:
        return match_hora.group(0)

    return texto


def _formatar_data_display(valor):
    if not valor:
        return valor
    try:
        return datetime.strptime(valor, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return valor

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
        paciente_id       = (data_in.get("paciente_id") or "").strip()
        medico_id         = (data_in.get("medico_id") or "").strip()
        procedimento_id   = (data_in.get("procedimento_id") or "").strip()
        procedimento_raw  = procedimento_id
        sala_id           = (data_in.get("sala_id") or "").strip()
        data_             = (data_in.get("data") or "").strip()
        hora_             = (data_in.get("hora") or "").strip()
        convenio_informado = (data_in.get("convenio") or data_in.get("convenio_subtipo") or "").strip()

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

        cur.execute("SELECT nome FROM procedimentos WHERE id = ?", (procedimento_id,))
        row_proc = cur.fetchone()
        procedimento_nome = (row_proc["nome"] if row_proc else "")
        nome_lower = (procedimento_nome or "").lower()
        convenio_valor = convenio_informado or None
        if "convênio" in nome_lower or "convenio" in nome_lower or procedimento_raw == "__convenio__":
            convenio_valor = convenio_informado or "Convênio"
        elif "particular" in nome_lower or procedimento_raw == "__particular__":
            convenio_valor = "Particular"
        elif "receita" in nome_lower or procedimento_raw == "__receita__":
            convenio_valor = "Receita"

        valido, msg = _validar_data_hora_futura(data_, hora_)
        if not valido:
            if is_ajax:
                conn.close()
                return jsonify({"ok": False, "msg": msg}), 400
            flash(msg, "danger")
            conn.close()
            return redirect(url_for("user.agendar_consulta"))

        if not is_slot_available(data_, hora_, int(medico_id), int(sala_id)):
            if is_ajax:
                conn.close()
                return jsonify({"ok": False, "msg": "Horário indisponível para este médico ou sala."}), 409
            flash("Horário indisponível para este médico ou sala.", "danger")
            conn.close()
            return redirect(url_for("user.agendar_consulta"))

        # insere
        cur.execute(
            """INSERT INTO agendamentos
               (paciente_id, medico_id, procedimento_id, sala_id, data, hora, convenio)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (paciente_id, medico_id, procedimento_id, sala_id, data_, hora_, convenio_valor)
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
    criar_tabelas()
    auto_close_past_appointments()
    conn = conectar()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(1) AS q FROM agendamento_ajustes WHERE status='pendente'")
    pend = cur.fetchone()["q"]
    hoje = date.today().isoformat()

    cur.execute("SELECT COUNT(1) AS total FROM agendamentos WHERE data=?", (hoje,))
    total_hoje = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(1) AS total FROM agendamentos WHERE data=? AND LOWER(status)='cancelado'", (hoje,))
    cancelados_hoje = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(1) AS total FROM agendamentos WHERE data=? AND LOWER(status)='concluido'", (hoje,))
    concluidos_hoje = cur.fetchone()["total"]

    dashboard_totais = {
        "agendados_hoje": total_hoje,
        "cancelados_hoje": cancelados_hoje,
        "realizados_hoje": concluidos_hoje,
    }

    cur.execute(
        """
        SELECT med.nome AS medico, COUNT(*) AS total
        FROM agendamentos a
        JOIN usuarios med ON med.id = a.medico_id
        WHERE LOWER(a.status) = 'concluido'
        GROUP BY med.id
        ORDER BY total DESC, med.nome ASC
        """
    )
    consultas_medico = [dict(row) for row in cur.fetchall()]

    cur.execute("SELECT id, nome FROM usuarios WHERE tipo_usuario IN ('medico','médico') ORDER BY nome")
    medicos = cur.fetchall()
    cur.execute("SELECT id, nome FROM usuarios WHERE tipo_usuario = 'paciente' ORDER BY nome")
    pacientes = cur.fetchall()
    cur.execute("SELECT id, nome FROM procedimentos ORDER BY nome")
    procedimentos = cur.fetchall()
    cur.execute(
        "SELECT DISTINCT convenio FROM agendamentos WHERE convenio IS NOT NULL AND TRIM(convenio)<>'' ORDER BY convenio"
    )
    convenios = [row["convenio"] for row in cur.fetchall()]

    cur.execute(
        """
        SELECT c.id, a.data, a.hora, pac.nome AS paciente, med.nome AS medico,
               pr.nome AS procedimento
        FROM chamadas_pacientes c
        JOIN agendamentos a ON a.id = c.agendamento_id
        JOIN usuarios pac ON pac.id = c.paciente_id
        JOIN usuarios med ON med.id = c.medico_id
        JOIN procedimentos pr ON pr.id = a.procedimento_id
        WHERE c.status = 'pendente'
        ORDER BY a.data, a.hora, c.id
        """
    )
    chamadas_rows = cur.fetchall()
    chamadas_pendentes = []
    for row in chamadas_rows:
        registro = dict(row)
        registro["data_display"] = _formatar_data_display(registro.get("data"))
        registro["hora_display"] = _normalizar_hora(registro.get("hora"))
        chamadas_pendentes.append(registro)

    filtros = {
        "inicio": (request.args.get("inicio") or "").strip(),
        "fim": (request.args.get("fim") or "").strip(),
        "mes": (request.args.get("mes") or "").strip(),
        "medico": (request.args.get("medico") or "").strip(),
        "paciente": (request.args.get("paciente") or "").strip(),
        "procedimento": (request.args.get("procedimento") or "").strip(),
        "convenio": (request.args.get("convenio") or "").strip(),
    }

    agendamentos_filtrados, totais_filtrados, filtros = _buscar_agendamentos_filtrados(filtros)

    conn.close()
    return render_template(
        "recepcionista.html",
        pendentes=pend,
        dashboard_totais=dashboard_totais,
        consultas_por_medico=consultas_medico,
        filtros=filtros,
        agendamentos_filtrados=agendamentos_filtrados,
        totais_filtrados=totais_filtrados,
        medicos=medicos,
        pacientes=pacientes,
        procedimentos=procedimentos,
        convenios=convenios,
        chamadas_pendentes=chamadas_pendentes,
    )


@user_bp.route("/recepcionista/relatorios/exportar", endpoint="exportar_relatorio")
@login_required(role='recepcionista')
def exportar_relatorio():
    hoje = date.today()
    escopo = (request.args.get("escopo") or "").lower()
    filtros = {
        "inicio": (request.args.get("inicio") or "").strip(),
        "fim": (request.args.get("fim") or "").strip(),
        "mes": (request.args.get("mes") or "").strip(),
        "medico": (request.args.get("medico") or "").strip(),
        "paciente": (request.args.get("paciente") or "").strip(),
        "procedimento": (request.args.get("procedimento") or "").strip(),
        "convenio": (request.args.get("convenio") or "").strip(),
    }

    if escopo == "diario":
        filtros["inicio"] = filtros["fim"] = hoje.isoformat()
        filtros["mes"] = ""
    elif escopo == "semanal":
        inicio_semana = hoje - timedelta(days=hoje.weekday())
        fim_semana = inicio_semana + timedelta(days=6)
        filtros["inicio"] = inicio_semana.isoformat()
        filtros["fim"] = fim_semana.isoformat()
        filtros["mes"] = ""
    elif escopo == "mensal" and not filtros.get("mes"):
        filtros["mes"] = hoje.strftime("%Y-%m")

    agendamentos, totais, filtros_norm = _buscar_agendamentos_filtrados(filtros)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Data",
        "Hora",
        "Paciente",
        "Médico",
        "Procedimento",
        "Convênio",
        "Status",
    ])
    for linha in agendamentos:
        writer.writerow([
            linha.get("data"),
            linha.get("hora"),
            linha.get("paciente"),
            linha.get("medico"),
            linha.get("procedimento"),
            linha.get("convenio"),
            linha.get("status_label"),
        ])
    writer.writerow([])
    writer.writerow(["Totais", totais.get("total"), "Realizados", totais.get("realizados"), "Cancelados", totais.get("cancelados"), "Em aberto", totais.get("agendados")])

    conteudo = output.getvalue()
    output.close()

    inicio_disp = filtros_norm.get("inicio") or ""
    fim_disp = filtros_norm.get("fim") or ""
    label_escopo = escopo or "personalizado"
    filename = f"relatorio_{label_escopo}_{inicio_disp.replace('-', '')}_{fim_disp.replace('-', '') or hoje.strftime('%Y%m%d')}".strip("_") + ".csv"

    return (
        conteudo,
        200,
        {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f"attachment; filename={filename}",
        },
    )


@user_bp.route("/recepcionista/chamadas/<int:chamada_id>/encaminhar", methods=["POST"], endpoint="encaminhar_chamada")
@login_required(role='recepcionista')
def encaminhar_chamada(chamada_id):
    criar_tabelas()
    conn = conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, status FROM chamadas_pacientes WHERE id=?",
        (chamada_id,),
    )
    chamada = cur.fetchone()

    if not chamada:
        conn.close()
        flash("Chamada não encontrada.", "danger")
        return redirect(url_for("user.visao_recepcionista"))

    if chamada["status"] != "pendente":
        conn.close()
        flash("Esta chamada já foi atualizada anteriormente.", "info")
        return redirect(url_for("user.visao_recepcionista"))

    agora = datetime.utcnow().isoformat()
    cur.execute(
        "UPDATE chamadas_pacientes SET status='encaminhado', encaminhado_em=? WHERE id=?",
        (agora, chamada_id),
    )
    conn.commit()
    conn.close()

    flash("Paciente liberado para atendimento.", "success")
    return redirect(url_for("user.visao_recepcionista"))


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
    agendamentos_brutos = cur.fetchall()

    colunas_agendamento = agendamentos_brutos[0].keys() if agendamentos_brutos else []
    possui_coluna_status = "status" in colunas_agendamento
    status_validos = {valor for valor, _ in STATUS_AGENDAMENTO}

    agendamentos = []
    atualizacoes = []

    for row in agendamentos_brutos:
        linha = dict(row)

        status_original = linha.get("status", "")
        data_original = linha.get("data", "")
        hora_original = linha.get("hora", "")

        status_normalizado = _normalizar_status(status_original, status_validos)
        data_normalizada = _normalizar_data(data_original)
        hora_normalizada = _normalizar_hora(hora_original)

        linha["status"] = status_normalizado
        linha["status_label"] = STATUS_LABELS.get(status_normalizado, status_normalizado.title() if isinstance(status_normalizado, str) else status_normalizado)
        linha["data"] = data_normalizada
        linha["data_display"] = _formatar_data_display(data_normalizada) if data_normalizada else data_original
        linha["hora"] = hora_normalizada

        agendamentos.append(linha)

        if linha.get("id") is not None and (
            status_normalizado != status_original
            or data_normalizada != data_original
            or hora_normalizada != hora_original
        ):
            if possui_coluna_status:
                atualizacoes.append((status_normalizado, data_normalizada, hora_normalizada, linha["id"]))
            else:
                atualizacoes.append((data_normalizada, hora_normalizada, linha["id"]))

    if atualizacoes:
        if possui_coluna_status:
            cur.executemany(
                "UPDATE agendamentos SET status=?, data=?, hora=? WHERE id=?",
                atualizacoes,
            )
        else:
            cur.executemany(
                "UPDATE agendamentos SET data=?, hora=? WHERE id=?",
                atualizacoes,
            )
        conn.commit()

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
    criar_tabelas()
    medico_id = session["usuario_id"]
    hoje = date.today().isoformat()

    conn = conectar()
    cur = conn.cursor()

    cur.execute(
        "SELECT COUNT(1) AS total FROM agendamentos WHERE medico_id=? AND data=?",
        (medico_id, hoje),
    )
    total_hoje = cur.fetchone()["total"]

    cur.execute(
        """SELECT COUNT(1) AS total FROM agendamentos
            WHERE medico_id=? AND data=? AND LOWER(status)='concluido'""",
        (medico_id, hoje),
    )
    concluidos_hoje = cur.fetchone()["total"]

    cur.execute(
        """SELECT COUNT(1) AS total FROM agendamentos
            WHERE medico_id=? AND data=? AND LOWER(status)='cancelado'""",
        (medico_id, hoje),
    )
    cancelados_hoje = cur.fetchone()["total"]

    cur.execute(
        "SELECT COUNT(1) AS total FROM agendamentos WHERE medico_id=? AND LOWER(status)='concluido'",
        (medico_id,),
    )
    concluidos_totais = cur.fetchone()["total"]

    cur.execute(
        """
        SELECT a.id, a.data, a.hora, a.status, a.notas, a.convenio,
               pac.nome AS paciente, pr.nome AS procedimento, s.nome AS sala
        FROM agendamentos a
        JOIN usuarios pac ON pac.id = a.paciente_id
        JOIN procedimentos pr ON pr.id = a.procedimento_id
        JOIN salas s ON s.id = a.sala_id
        WHERE a.medico_id=? AND a.data=?
        ORDER BY a.hora
        """,
        (medico_id, hoje),
    )
    consultas_brutas = cur.fetchall()

    cur.execute(
        """SELECT agendamento_id, status, criado_em, encaminhado_em
            FROM chamadas_pacientes
            WHERE medico_id=?
            ORDER BY id DESC""",
        (medico_id,),
    )
    chamadas = cur.fetchall()
    chamadas_por_agendamento = {}
    for row in chamadas:
        agendamento_id = row["agendamento_id"]
        if agendamento_id not in chamadas_por_agendamento:
            chamadas_por_agendamento[agendamento_id] = dict(row)

    status_validos = {valor for valor, _ in STATUS_AGENDAMENTO}
    consultas = []
    for row in consultas_brutas:
        registro = dict(row)
        status_normalizado = _normalizar_status(registro.get("status", ""), status_validos)
        registro["status"] = status_normalizado
        registro["status_label"] = STATUS_LABELS.get(
            status_normalizado,
            status_normalizado.title() if isinstance(status_normalizado, str) and status_normalizado else status_normalizado,
        )
        registro["data_display"] = _formatar_data_display(registro.get("data"))
        registro["hora_display"] = _normalizar_hora(registro.get("hora"))
        registro["notas"] = registro.get("notas") or ""
        registro["convenio"] = registro.get("convenio") or "—"
        chamada_info = chamadas_por_agendamento.get(registro["id"])
        if chamada_info:
            registro["chamada_status"] = chamada_info.get("status")
            registro["chamada_criada"] = chamada_info.get("criado_em")
            registro["chamada_encaminhada"] = chamada_info.get("encaminhado_em")
        else:
            registro["chamada_status"] = None
            registro["chamada_criada"] = None
            registro["chamada_encaminhada"] = None
        consultas.append(registro)

    conn.close()

    dashboard = {
        "total_hoje": total_hoje,
        "cancelados_hoje": cancelados_hoje,
        "concluidos_hoje": concluidos_hoje,
        "concluidos_totais": concluidos_totais,
    }

    return render_template(
        "medico.html",
        dashboard=dashboard,
        consultas=consultas,
        data_hoje=_formatar_data_display(hoje),
    )


@user_bp.route("/medico/agendamentos/<int:agendamento_id>/nota", methods=["POST"], endpoint="salvar_nota_medico")
@login_required(role='medico')
def salvar_nota_medico(agendamento_id):
    criar_tabelas()
    nota = request.form.get("nota", "")
    if nota is None:
        nota = ""
    nota = nota.strip()
    if len(nota) > 1000:
        nota = nota[:1000]

    conn = conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM agendamentos WHERE id=? AND medico_id=?",
        (agendamento_id, session["usuario_id"]),
    )
    agendamento = cur.fetchone()

    if not agendamento:
        conn.close()
        flash("Agendamento não encontrado.", "danger")
        return redirect(url_for("user.visao_medico"))

    cur.execute("UPDATE agendamentos SET notas=? WHERE id=?", (nota, agendamento_id))
    conn.commit()
    conn.close()

    flash("Notas do atendimento atualizadas.", "success")
    return redirect(url_for("user.visao_medico"))


@user_bp.route("/medico/agendamentos/<int:agendamento_id>/chamar", methods=["POST"], endpoint="chamar_paciente")
@login_required(role='medico')
def chamar_paciente(agendamento_id):
    criar_tabelas()
    medico_id = session["usuario_id"]
    conn = conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, paciente_id, data, status FROM agendamentos WHERE id=? AND medico_id=?",
        (agendamento_id, medico_id),
    )
    agendamento = cur.fetchone()

    if not agendamento:
        conn.close()
        flash("Agendamento não encontrado.", "danger")
        return redirect(url_for("user.visao_medico"))

    hoje = date.today().isoformat()
    if agendamento["data"] != hoje:
        conn.close()
        flash("Chamadas só podem ser feitas para atendimentos do dia.", "warning")
        return redirect(url_for("user.visao_medico"))

    cur.execute(
        "SELECT id FROM chamadas_pacientes WHERE agendamento_id=? AND status='pendente'",
        (agendamento_id,),
    )
    chamada_pendente = cur.fetchone()
    if chamada_pendente:
        conn.close()
        flash("Este paciente já foi chamado e aguarda na recepção.", "info")
        return redirect(url_for("user.visao_medico"))

    agora = datetime.utcnow().isoformat()
    cur.execute(
        """INSERT INTO chamadas_pacientes
               (agendamento_id, medico_id, paciente_id, status, criado_em)
               VALUES (?, ?, ?, 'pendente', ?)""",
        (agendamento_id, medico_id, agendamento["paciente_id"], agora),
    )
    cur.execute(
        "UPDATE agendamentos SET status='em atendimento' WHERE id=?",
        (agendamento_id,),
    )
    conn.commit()
    conn.close()

    flash("Chamada enviada à recepção.", "success")
    return redirect(url_for("user.visao_medico"))

@user_bp.route("/paciente", endpoint="visao_paciente")
@login_required(role='paciente')
def visao_paciente():
    pid = session["usuario_id"]
    auto_close_past_appointments()
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

    cur.execute("SELECT nome, email FROM usuarios WHERE id=?", (pid,))
    perfil_row = cur.fetchone()
    perfil = {"nome": perfil_row["nome"], "email": perfil_row["email"]} if perfil_row else {"nome": "", "email": ""}

    cur.execute("SELECT id, nome FROM usuarios WHERE tipo_usuario IN ('medico','médico') ORDER BY nome")
    medicos = cur.fetchall()
    cur.execute("SELECT id, nome FROM procedimentos ORDER BY nome")
    procedimentos = cur.fetchall()
    cur.execute("SELECT id, nome FROM salas ORDER BY nome")
    salas = cur.fetchall()
    cur.execute(
        "SELECT DISTINCT convenio FROM agendamentos WHERE convenio IS NOT NULL AND TRIM(convenio)<>'' ORDER BY convenio"
    )
    convenios = [row["convenio"] for row in cur.fetchall()]

    conn.close()
    return render_template(
        "paciente.html",
        agendamentos=ags,
        ajustes=ajustes,
        perfil=perfil,
        medicos=medicos,
        procedimentos=procedimentos,
        salas=salas,
        convenios=convenios,
    )


@user_bp.route("/paciente/perfil", methods=["POST"], endpoint="atualizar_perfil_paciente")
@login_required(role='paciente')
def atualizar_perfil_paciente():
    usuario_id = session["usuario_id"]
    nome = (request.form.get("nome") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    senha = request.form.get("senha") or ""
    confirmar = request.form.get("confirmar_senha") or ""

    if not nome or not email:
        flash("Informe nome e e-mail para atualizar seu perfil.", "danger")
        return redirect(url_for("user.visao_paciente"))

    if senha and senha != confirmar:
        flash("A confirmação de senha não confere.", "danger")
        return redirect(url_for("user.visao_paciente"))

    conn = conectar()
    cur = conn.cursor()

    try:
        if senha:
            senha_hash = generate_password_hash(senha)
            cur.execute(
                "UPDATE usuarios SET nome=?, email=?, senha=? WHERE id=?",
                (nome, email, senha_hash, usuario_id),
            )
        else:
            cur.execute(
                "UPDATE usuarios SET nome=?, email=? WHERE id=?",
                (nome, email, usuario_id),
            )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        flash("Este e-mail já está cadastrado para outro usuário.", "danger")
        return redirect(url_for("user.visao_paciente"))

    conn.close()
    session["usuario_nome"] = nome
    flash("Perfil atualizado com sucesso!", "success")
    return redirect(url_for("user.visao_paciente"))


@user_bp.route("/paciente/agendar", methods=["POST"], endpoint="agendar_consulta_paciente")
@login_required(role='paciente')
def agendar_consulta_paciente():
    criar_tabelas()
    paciente_id = session["usuario_id"]
    medico_id = (request.form.get("medico_id") or "").strip()
    procedimento_id = (request.form.get("procedimento_id") or "").strip()
    sala_id = (request.form.get("sala_id") or "").strip()
    data_ = (request.form.get("data") or "").strip()
    hora_ = (request.form.get("hora") or "").strip()
    convenio = (request.form.get("convenio") or "").strip() or None

    if not (medico_id and procedimento_id and sala_id and data_ and hora_):
        flash("Preencha todos os campos para agendar.", "danger")
        return redirect(url_for("user.visao_paciente"))

    valido, msg = _validar_data_hora_futura(data_, hora_)
    if not valido:
        flash(msg, "danger")
        return redirect(url_for("user.visao_paciente"))

    try:
        disponiveis = horarios_disponiveis(int(medico_id), int(sala_id), data_)
    except ValueError:
        flash("Dados inválidos para médico ou sala.", "danger")
        return redirect(url_for("user.visao_paciente"))

    if hora_ not in disponiveis or not is_slot_available(data_, hora_, int(medico_id), int(sala_id)):
        flash("Horário indisponível para o médico ou sala escolhidos.", "danger")
        return redirect(url_for("user.visao_paciente"))

    conn = conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM agendamentos WHERE paciente_id=? AND data=? AND hora=?",
        (paciente_id, data_, hora_),
    )
    if cur.fetchone():
        conn.close()
        flash("Você já possui uma consulta nesse horário.", "warning")
        return redirect(url_for("user.visao_paciente"))

    cur.execute(
        """INSERT INTO agendamentos (paciente_id, medico_id, procedimento_id, sala_id, data, hora, convenio)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (paciente_id, medico_id, procedimento_id, sala_id, data_, hora_, convenio),
    )
    conn.commit()
    conn.close()

    flash("Consulta agendada com sucesso!", "success")
    return redirect(url_for("user.visao_paciente"))


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

    valido, msg = _validar_data_hora_futura(novo_dia, nova_hora)
    if not valido:
        conn.close()
        flash(msg, "danger")
        return redirect(url_for("user.visao_paciente"))

    livres = horarios_disponiveis(agendamento["medico_id"], agendamento["sala_id"], novo_dia)
    if not (novo_dia == agendamento["data"] and nova_hora == agendamento["hora"]):
        if nova_hora not in livres or not is_slot_available(novo_dia, nova_hora, agendamento["medico_id"], agendamento["sala_id"], agendamento_id):
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
    auto_close_past_appointments()
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
        data_sugerida = (request.form.get("data_sugerida") or "").strip()
        hora_sugerida = (request.form.get("hora_sugerida") or "").strip()
        now_iso = datetime.utcnow().isoformat()
        if not (data_sugerida and hora_sugerida):
            data_sugerida, hora_sugerida = sugerir_proximo_horario(row["novo_dia"], row["nova_hora"], row["medico_id"], row["sala_id"]) or (None, None)
        if data_sugerida and hora_sugerida:
            valido, msg_val = _validar_data_hora_futura(data_sugerida, hora_sugerida)
            if not valido:
                conn.close()
                flash(msg_val, "danger")
                return redirect(url_for("user.lista_ajustes"))
        motivo_padrao = (
            f"CONSULTA PARA A DATA ({row['novo_dia']}) no horario ({row['nova_hora']}) "
            "NÃO TEMOS NENHUM HORARIO DISPONIVEL, SEGUE DATA E HORARIO PARA NOVA CONSULTA."
        )
        cur.execute(
            """UPDATE agendamento_ajustes
                   SET status='negada', motivo_negativa=?, data_sugerida=?, hora_sugerida=?, updated_at=?
                 WHERE id=?""",
            (motivo_padrao, data_sugerida, hora_sugerida, now_iso, ajuste_id),
        )
        cur.execute(
            """UPDATE agendamentos
                   SET status='negado', motivo_negacao=?, data_sugerida=?, hora_sugerida=?, updated_at=?
                 WHERE id=?""",
            (motivo_padrao, data_sugerida, hora_sugerida, now_iso, row["agendamento_id"]),
        )
        conn.commit()
        conn.close()
        flash("Solicitação negada e paciente receberá um novo horário sugerido.", "warning")
        return redirect(url_for("user.lista_ajustes"))

    valido, msg_val = _validar_data_hora_futura(row["novo_dia"], row["nova_hora"])
    if not valido:
        conn.close()
        flash(msg_val, "danger")
        return redirect(url_for("user.lista_ajustes"))

    livres = horarios_disponiveis(row["medico_id"], row["sala_id"], row["novo_dia"])
    if row["nova_hora"] not in livres or not is_slot_available(row["novo_dia"], row["nova_hora"], row["medico_id"], row["sala_id"], row["agendamento_id"]):
        conn.close()
        flash("Horário indisponível. Escolha outro horário.", "danger")
        return redirect(url_for("user.lista_ajustes"))

    # aplica ajuste
    now_iso = datetime.utcnow().isoformat()
    cur.execute("UPDATE agendamentos SET data=?, hora=?, updated_at=? WHERE id=?", (row["novo_dia"], row["nova_hora"], now_iso, row["agendamento_id"]))
    cur.execute("UPDATE agendamento_ajustes SET status='aceito', updated_at=? WHERE id=?", (now_iso, ajuste_id))
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


@user_bp.route("/api/disponibilidade", methods=["GET"], endpoint="api_disponibilidade")
@login_required()
def api_disponibilidade():
    data = (request.args.get("data") or "").strip()
    medico_raw = request.args.get("medico_id")
    sala_raw = request.args.get("sala_id")
    if not data:
        return jsonify({"ok": False, "msg": "Informe a data."}), 400
    try:
        medico_id = int(medico_raw) if medico_raw else None
        sala_id = int(sala_raw) if sala_raw else None
    except ValueError:
        return jsonify({"ok": False, "msg": "Parâmetros inválidos."}), 400

    ocupados = get_busy_slots(data, medico_id, sala_id)
    disponiveis = horarios_disponiveis(medico_id or 0, sala_id or 0, data)
    return jsonify({
        "data": data,
        "ocupados": ocupados,
        "disponiveis": disponiveis,
    })


@user_bp.route("/api/sugerir_horario", methods=["GET"], endpoint="api_sugerir_horario")
@login_required()
def api_sugerir_horario():
    data = (request.args.get("data") or "").strip()
    hora = (request.args.get("hora") or "").strip()
    medico_raw = request.args.get("medico_id")
    sala_raw = request.args.get("sala_id")
    try:
        medico_id = int(medico_raw) if medico_raw else None
        sala_id = int(sala_raw) if sala_raw else None
    except ValueError:
        return jsonify({"ok": False, "msg": "Parâmetros inválidos."}), 400

    if not (data and hora):
        return jsonify({"ok": False, "msg": "Informe data e hora."}), 400

    proxima_data, proxima_hora = sugerir_proximo_horario(data, hora, medico_id, sala_id)
    if not proxima_data:
        return jsonify({"ok": False, "msg": "Nenhum horário encontrado."}), 404
    return jsonify({"data": proxima_data, "hora": proxima_hora})


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


@user_bp.route("/paciente/horarios_novo", endpoint="paciente_horarios_novo")
@login_required(role='paciente')
def paciente_horarios_novo():
    try:
        medico_id = int(request.args.get("medico_id", "0"))
        sala_id = int(request.args.get("sala_id", "0"))
    except ValueError:
        return jsonify({"ok": False, "msg": "Parâmetros inválidos."}), 400

    dia = (request.args.get("dia") or "").strip()
    if not (medico_id and sala_id and dia):
        return jsonify([])

    return jsonify(horarios_disponiveis(medico_id, sala_id, dia))


# ------------------ Recepção: criar usuários ------------------
@user_bp.route("/cadastrar_usuarios", methods=["GET", "POST"], endpoint="cadastrar_usuarios")
@login_required(role='recepcionista')
def cadastrar_usuarios():
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        tipo_usuario = request.form.get("tipo_usuario", "").lower()

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


@user_bp.route("/recepcionista/usuarios", endpoint="gerenciar_usuarios")
@login_required(role='recepcionista')
def gerenciar_usuarios():
    conn = conectar()
    cur = conn.cursor()
    cur.execute("SELECT id, nome, email FROM usuarios WHERE LOWER(tipo_usuario)='paciente' ORDER BY nome")
    pacientes = cur.fetchall()
    cur.execute(
        "SELECT id, nome, email FROM usuarios WHERE LOWER(REPLACE(tipo_usuario, 'é', 'e'))='medico' ORDER BY nome"
    )
    medicos = cur.fetchall()
    conn.close()
    return render_template("gerenciar_usuarios.html", pacientes=pacientes, medicos=medicos)


@user_bp.route("/recepcionista/usuarios/<int:usuario_id>/editar", methods=["GET", "POST"], endpoint="editar_usuario")
@login_required(role='recepcionista')
def editar_usuario(usuario_id):
    conn = conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, nome, email, tipo_usuario FROM usuarios WHERE id=?",
        (usuario_id,),
    )
    usuario = cur.fetchone()

    if not usuario:
        conn.close()
        flash("Usuário não encontrado ou tipo não permitido para edição.", "danger")
        return redirect(url_for("user.gerenciar_usuarios"))

    tipo_usuario = (usuario["tipo_usuario"] or "").lower()
    tipo_normalizado = tipo_usuario.replace("é", "e")
    if tipo_normalizado not in {"paciente", "medico"}:
        conn.close()
        flash("Usuário não encontrado ou tipo não permitido para edição.", "danger")
        return redirect(url_for("user.gerenciar_usuarios"))

    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""
        confirmar = request.form.get("confirmar_senha") or ""

        if not nome or not email:
            conn.close()
            flash("Informe nome e e-mail para atualizar o cadastro.", "danger")
            return redirect(url_for("user.editar_usuario", usuario_id=usuario_id))

        if senha and senha != confirmar:
            conn.close()
            flash("A confirmação de senha não confere.", "danger")
            return redirect(url_for("user.editar_usuario", usuario_id=usuario_id))

        try:
            if senha:
                senha_hash = generate_password_hash(senha)
                cur.execute(
                    "UPDATE usuarios SET nome=?, email=?, senha=? WHERE id=?",
                    (nome, email, senha_hash, usuario_id),
                )
            else:
                cur.execute(
                    "UPDATE usuarios SET nome=?, email=? WHERE id=?",
                    (nome, email, usuario_id),
                )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            flash("Este e-mail já está cadastrado para outro usuário.", "danger")
            return redirect(url_for("user.editar_usuario", usuario_id=usuario_id))

        conn.close()
        flash("Cadastro atualizado com sucesso!", "success")
        return redirect(url_for("user.gerenciar_usuarios"))

    conn.close()
    return render_template("editar_usuario.html", usuario=usuario)

app = Flask(__name__)

