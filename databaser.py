import sqlite3, os
from werkzeug.security import generate_password_hash
from datetime import datetime, time, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'databaser.db')

def conectar():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def criar_tabelas():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # --- tabelas base ---
    cur.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            senha TEXT NOT NULL,
            tipo_usuario TEXT NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS procedimentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            descricao TEXT,
            UNIQUE(nome)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS salas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            capacidade INTEGER,
            UNIQUE(nome)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS agendamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paciente_id INTEGER NOT NULL,
            medico_id INTEGER NOT NULL,
            procedimento_id INTEGER NOT NULL,
            sala_id INTEGER NOT NULL,
            data TEXT NOT NULL, -- YYYY-MM-DD
            hora TEXT NOT NULL, -- HH:MM
            status TEXT NOT NULL DEFAULT 'agendado',
            convenio TEXT,
            notas TEXT,
            motivo_negacao TEXT,
            data_sugerida TEXT,
            hora_sugerida TEXT,
            updated_at TEXT,
            FOREIGN KEY (paciente_id) REFERENCES usuarios (id),
            FOREIGN KEY (medico_id) REFERENCES usuarios (id),
            FOREIGN KEY (procedimento_id) REFERENCES procedimentos (id),
            FOREIGN KEY (sala_id) REFERENCES salas (id)
        )
    ''')

    # garante que a coluna status exista mesmo em bases antigas
    cur.execute("PRAGMA table_info(agendamentos)")
    cols = [row[1] for row in cur.fetchall()]
    if 'status' not in cols:
        cur.execute("ALTER TABLE agendamentos ADD COLUMN status TEXT NOT NULL DEFAULT 'agendado'")
    if 'convenio' not in cols:
        cur.execute("ALTER TABLE agendamentos ADD COLUMN convenio TEXT")
    if 'notas' not in cols:
        cur.execute("ALTER TABLE agendamentos ADD COLUMN notas TEXT")
    if 'motivo_negacao' not in cols:
        cur.execute("ALTER TABLE agendamentos ADD COLUMN motivo_negacao TEXT")
    if 'data_sugerida' not in cols:
        cur.execute("ALTER TABLE agendamentos ADD COLUMN data_sugerida TEXT")
    if 'hora_sugerida' not in cols:
        cur.execute("ALTER TABLE agendamentos ADD COLUMN hora_sugerida TEXT")
    if 'updated_at' not in cols:
        cur.execute("ALTER TABLE agendamentos ADD COLUMN updated_at TEXT")

    # chamadas de pacientes da agenda médica
    cur.execute('''
        CREATE TABLE IF NOT EXISTS chamadas_pacientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agendamento_id INTEGER NOT NULL,
            medico_id INTEGER NOT NULL,
            paciente_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pendente', -- pendente|encaminhado|encerrado
            criado_em TEXT NOT NULL,
            encaminhado_em TEXT,
            FOREIGN KEY (agendamento_id) REFERENCES agendamentos (id),
            FOREIGN KEY (medico_id) REFERENCES usuarios (id),
            FOREIGN KEY (paciente_id) REFERENCES usuarios (id)
        )
    ''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chamadas_status ON chamadas_pacientes(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chamadas_agendamento ON chamadas_pacientes(agendamento_id)")

    # --- solicitações de ajuste de agendamento ---
    cur.execute('''
        CREATE TABLE IF NOT EXISTS agendamento_ajustes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agendamento_id INTEGER NOT NULL,
            novo_dia TEXT NOT NULL,  -- YYYY-MM-DD
            nova_hora TEXT NOT NULL, -- HH:MM
            motivo TEXT,
            status TEXT NOT NULL DEFAULT 'pendente', -- pendente|aceito|negado
            criado_em TEXT NOT NULL, -- ISO
            motivo_negativa TEXT,
            data_sugerida TEXT,
            hora_sugerida TEXT,
            updated_at TEXT,
            FOREIGN KEY (agendamento_id) REFERENCES agendamentos (id)
        )
    ''')

    cur.execute("PRAGMA table_info(agendamento_ajustes)")
    cols_ajuste = [row[1] for row in cur.fetchall()]
    if 'motivo_negativa' not in cols_ajuste:
        cur.execute("ALTER TABLE agendamento_ajustes ADD COLUMN motivo_negativa TEXT")
    if 'data_sugerida' not in cols_ajuste:
        cur.execute("ALTER TABLE agendamento_ajustes ADD COLUMN data_sugerida TEXT")
    if 'hora_sugerida' not in cols_ajuste:
        cur.execute("ALTER TABLE agendamento_ajustes ADD COLUMN hora_sugerida TEXT")
    if 'updated_at' not in cols_ajuste:
        cur.execute("ALTER TABLE agendamento_ajustes ADD COLUMN updated_at TEXT")

    # dedup seguro
    cur.execute("""DELETE FROM procedimentos WHERE rowid NOT IN (SELECT MIN(rowid) FROM procedimentos GROUP BY nome)""")
    cur.execute("""DELETE FROM salas         WHERE rowid NOT IN (SELECT MIN(rowid) FROM salas         GROUP BY nome)""")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_procedimentos_nome ON procedimentos(nome)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_salas_nome         ON salas(nome)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agendamentos_data ON agendamentos(data)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agendamentos_data_hora ON agendamentos(data, hora)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agendamentos_medico ON agendamentos(medico_id)")

    # seeds
    for nome, desc in [
        ("Consulta Particular", "Atendimento particular"),
        ("Consulta Convênio", "Atendimento via convênios cadastrados"),
        ("Solicitação de Receita", "Solicitação/renovação de receita"),
    ]:
        cur.execute("INSERT OR IGNORE INTO procedimentos (nome, descricao) VALUES (?, ?)", (nome, desc))
    for nome, cap in [("Sala 1", 1), ("Sala 2", 1), ("Sala 3", 1)]:
        cur.execute("INSERT OR IGNORE INTO salas (nome, capacidade) VALUES (?, ?)", (nome, cap))
    cur.execute("SELECT 1 FROM usuarios WHERE email = ?", ("recepcionistamaster@gmail.com",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO usuarios (nome, email, senha, tipo_usuario) VALUES (?, ?, ?, ?)",
            ("Recepcionista Master", "recepcionistamaster@gmail.com", generate_password_hash("12345"), "recepcionista master")
        )

    conn.commit()
    conn.close()

# ---------- util: calcular horários disponíveis ----------
def _status_ocupado(status: str) -> bool:
    texto = (status or "").strip().lower()
    return texto not in {"negado", "cancelado", "negada", "cancelada"}


def get_busy_slots(dia_str: str, medico_id: int = None, sala_id: int = None, ignorar_agendamento_id=None):
    params = [dia_str]
    condicoes = ["data=?"]
    condicao_alocacao = None
    if medico_id and sala_id:
        condicao_alocacao = "(medico_id=? OR sala_id=?)"
        params.extend([medico_id, sala_id])
    elif medico_id:
        condicao_alocacao = "medico_id=?"
        params.append(medico_id)
    elif sala_id:
        condicao_alocacao = "sala_id=?"
        params.append(sala_id)

    if condicao_alocacao:
        condicoes.append(condicao_alocacao)
    if ignorar_agendamento_id is not None:
        condicoes.append("id<>?")
        params.append(ignorar_agendamento_id)

    conn = conectar()
    cur = conn.cursor()
    cur.execute(
        f"SELECT hora, status FROM agendamentos WHERE {' AND '.join(condicoes)}",
        params,
    )
    ocupados = {row["hora"] for row in cur.fetchall() if _status_ocupado(row["status"])}
    conn.close()
    return sorted(ocupados)


def is_slot_available(dia_str: str, hora_str: str, medico_id: int = None, sala_id: int = None, ignorar_agendamento_id=None) -> bool:
    ocupados = set(get_busy_slots(dia_str, medico_id, sala_id, ignorar_agendamento_id))
    return hora_str not in ocupados


def horarios_disponiveis(medico_id:int, sala_id:int, dia_str:str, passo_min=30, ignorar_agendamento_id=None):
    """
    Gera timeslots entre 08:00-17:00 para a data dada,
    removendo horários já ocupados (sala OU médico ocupados)
    considerando status que não sejam cancelados/negados.
    """
    inicio = time(8, 0); fim = time(17, 0)
    t = datetime.strptime(f"{dia_str} {inicio.hour:02d}:{inicio.minute:02d}", "%Y-%m-%d %H:%M")
    end = datetime.strptime(f"{dia_str} {fim.hour:02d}:{fim.minute:02d}", "%Y-%m-%d %H:%M")

    ocupados = set(get_busy_slots(dia_str, medico_id, sala_id, ignorar_agendamento_id))
    livres = []
    while t <= end:
        hhmm = t.strftime("%H:%M")
        if hhmm not in ocupados:
            livres.append(hhmm)
        t += timedelta(minutes=passo_min)
    return livres


def sugerir_proximo_horario(data_str: str, hora_str: str, medico_id: int, sala_id: int, passo_min=30):
    try:
        base_dt = datetime.strptime(f"{data_str} {hora_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None, None

    for _ in range(0, 14 * 24 * 60 // passo_min):
        dia = base_dt.strftime("%Y-%m-%d")
        hora = base_dt.strftime("%H:%M")
        if is_slot_available(dia, hora, medico_id, sala_id):
            return dia, hora
        base_dt += timedelta(minutes=passo_min)
        if base_dt.time() > time(17, 0):
            base_dt = datetime.combine(base_dt.date() + timedelta(days=1), time(8, 0))
    return None, None


def auto_close_past_appointments(now: datetime = None):
    ref = now or datetime.now()
    conn = conectar()
    cur = conn.cursor()
    cur.execute("SELECT id, data, hora, status FROM agendamentos")
    rows = cur.fetchall()
    to_close = []
    for row in rows:
        try:
            dt_row = datetime.strptime(f"{row['data']} {row['hora']}", "%Y-%m-%d %H:%M")
        except Exception:
            continue
        status_raw = (row["status"] or "").lower()
        if dt_row < ref and status_raw in {"agendado", "em atendimento", "confirmado", "pendente"}:
            to_close.append(row["id"])
    if to_close:
        now_iso = ref.isoformat()
        cur.execute(
            f"UPDATE agendamentos SET status='concluido', updated_at=? WHERE id IN ({','.join('?' for _ in to_close)})",
            [now_iso, *to_close],
        )
        conn.commit()
    conn.close()
