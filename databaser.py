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
            FOREIGN KEY (agendamento_id) REFERENCES agendamentos (id)
        )
    ''')

    # dedup seguro
    cur.execute("""DELETE FROM procedimentos WHERE rowid NOT IN (SELECT MIN(rowid) FROM procedimentos GROUP BY nome)""")
    cur.execute("""DELETE FROM salas         WHERE rowid NOT IN (SELECT MIN(rowid) FROM salas         GROUP BY nome)""")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_procedimentos_nome ON procedimentos(nome)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_salas_nome         ON salas(nome)")

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
def horarios_disponiveis(medico_id:int, sala_id:int, dia_str:str, passo_min=30):
    """
    Gera timeslots entre 08:00-17:00 para a data dada,
    removendo horários já ocupados (sala OU médico ocupados).
    Retorna lista de strings 'HH:MM'.
    """
    conn = conectar()
    c = conn.cursor()
    # horários ocupados por sala OU por médico na mesma data
    c.execute("""SELECT hora FROM agendamentos WHERE data=? AND (sala_id=? OR medico_id=?)""",
              (dia_str, sala_id, medico_id))
    ocupados = {row["hora"] for row in c.fetchall()}
    conn.close()

    inicio = time(8, 0); fim = time(17, 0)
    t = datetime.strptime(f"{dia_str} {inicio.hour:02d}:{inicio.minute:02d}", "%Y-%m-%d %H:%M")
    end = datetime.strptime(f"{dia_str} {fim.hour:02d}:{fim.minute:02d}", "%Y-%m-%d %H:%M")

    livres = []
    while t <= end:
        hhmm = t.strftime("%H:%M")
        if hhmm not in ocupados:
            livres.append(hhmm)
        t += timedelta(minutes=passo_min)
    return livres
