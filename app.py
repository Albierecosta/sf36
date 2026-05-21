import os
import csv
import contextlib
from io import BytesIO, StringIO
from datetime import date
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sf36-dev-secret-mude-em-producao")

# ---------------------------------------------------------------------------
# Database — PostgreSQL em produção (DATABASE_URL), SQLite local
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
    PH = "%s"
    SERIAL = "SERIAL PRIMARY KEY"
else:
    import sqlite3
    DB_PATH = os.environ.get("DB_PATH", "sf36.db")
    PH = "?"
    SERIAL = "INTEGER PRIMARY KEY AUTOINCREMENT"


@contextlib.contextmanager
def get_db():
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _col_exists(cur, table, column):
    if USE_PG:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
            (table, column),
        )
        return bool(cur.fetchone())
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def init_db():
    with get_db() as conn:
        cur = conn.cursor()

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                id {SERIAL},
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'student',
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS groups (
                id {SERIAL},
                name TEXT NOT NULL,
                description TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # role: 'admin' = admin do grupo, 'member' = estudante comum
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS user_groups (
                user_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                PRIMARY KEY (user_id, group_id)
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS assessments (
                id {SERIAL},
                patient_name TEXT NOT NULL,
                patient_code TEXT,
                assessment_date TEXT NOT NULL,
                notes TEXT,
                q1 INTEGER, q2 INTEGER,
                q3a INTEGER, q3b INTEGER, q3c INTEGER, q3d INTEGER, q3e INTEGER,
                q3f INTEGER, q3g INTEGER, q3h INTEGER, q3i INTEGER, q3j INTEGER,
                q4a INTEGER, q4b INTEGER, q4c INTEGER, q4d INTEGER,
                q5a INTEGER, q5b INTEGER, q5c INTEGER,
                q6 INTEGER, q7 INTEGER, q8 INTEGER,
                q9a INTEGER, q9b INTEGER, q9c INTEGER, q9d INTEGER, q9e INTEGER,
                q9f INTEGER, q9g INTEGER, q9h INTEGER, q9i INTEGER,
                q10 INTEGER,
                q11a INTEGER, q11b INTEGER, q11c INTEGER, q11d INTEGER,
                capacidade_funcional REAL,
                limitacao_fisica REAL,
                dor REAL,
                estado_geral_saude REAL,
                vitalidade REAL,
                aspectos_sociais REAL,
                limitacao_emocional REAL,
                saude_mental REAL,
                group_id INTEGER,
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Default admin master on first run
        cur.execute("SELECT COUNT(*) FROM users")
        if cur.fetchone()[0] == 0:
            cur.execute(
                f"INSERT INTO users (username, password_hash, name, role) VALUES ({PH},{PH},{PH},{PH})",
                ("admin", generate_password_hash("admin123"), "Administrador", "admin"),
            )

    # Migrations: colunas adicionadas em versões posteriores
    for table, col, ctype in [
        ("assessments", "group_id", "INTEGER"),
        ("assessments", "created_by", "INTEGER"),
        ("user_groups", "role", "TEXT NOT NULL DEFAULT 'member'"),
        ("users", "must_change_password", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                if not _col_exists(cur, table, col):
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SF-36 scoring
# ---------------------------------------------------------------------------

Q_FIELDS = (
    ["q1", "q2"]
    + [f"q3{x}" for x in "abcdefghij"]
    + [f"q4{x}" for x in "abcd"]
    + [f"q5{x}" for x in "abc"]
    + ["q6", "q7", "q8"]
    + [f"q9{x}" for x in "abcdefghi"]
    + ["q10"]
    + [f"q11{x}" for x in "abcd"]
)

DOMAIN_LABELS = {
    "capacidade_funcional": "Capacidade Funcional",
    "limitacao_fisica": "Limitação por Aspectos Físicos",
    "dor": "Dor",
    "estado_geral_saude": "Estado Geral de Saúde",
    "vitalidade": "Vitalidade",
    "aspectos_sociais": "Aspectos Sociais",
    "limitacao_emocional": "Limitação por Aspectos Emocionais",
    "saude_mental": "Saúde Mental",
}

DOMAIN_FIELDS = list(DOMAIN_LABELS.keys())

INSERT_FIELDS = (
    ["patient_name", "patient_code", "assessment_date", "notes"]
    + Q_FIELDS
    + DOMAIN_FIELDS
    + ["group_id", "created_by"]
)


def calculate_sf36(data: dict) -> dict:
    q1_map = {1: 5.0, 2: 4.4, 3: 3.4, 4: 2.0, 5: 1.0}
    q6_map = {1: 5, 2: 4, 3: 3, 4: 2, 5: 1}
    q7_map = {1: 6.0, 2: 5.4, 3: 4.2, 4: 3.1, 5: 2.0, 6: 1.0}
    inv6 = {1: 6, 2: 5, 3: 4, 4: 3, 5: 2, 6: 1}
    inv5 = {1: 5, 2: 4, 3: 3, 4: 2, 5: 1}

    q1 = q1_map[data["q1"]]
    q3 = sum(data[f"q3{x}"] for x in "abcdefghij")
    q4 = sum(data[f"q4{x}"] for x in "abcd")
    q5 = sum(data[f"q5{x}"] for x in "abc")
    q6 = q6_map[data["q6"]]
    q7 = q7_map[data["q7"]]
    q8 = 6 if data["q7"] == 1 else {1: 5, 2: 4, 3: 3, 4: 2, 5: 1}[data["q8"]]

    invert_q9 = set("adeh")
    q9 = {x: (inv6[data[f"q9{x}"]] if x in invert_q9 else data[f"q9{x}"]) for x in "abcdefghi"}

    q10 = data["q10"]

    invert_q11 = set("bd")
    q11 = {x: (inv5[data[f"q11{x}"]] if x in invert_q11 else data[f"q11{x}"]) for x in "abcd"}
    q11_sum = sum(q11.values())

    def raw(val, lower, rng):
        return round((val - lower) / rng * 100, 1)

    vitality = q9["a"] + q9["e"] + q9["g"] + q9["i"]
    mental = q9["b"] + q9["c"] + q9["d"] + q9["f"] + q9["h"]

    return {
        "capacidade_funcional": raw(q3, 10, 20),
        "limitacao_fisica": raw(q4, 4, 4),
        "dor": raw(q7 + q8, 2, 10),
        "estado_geral_saude": raw(q1 + q11_sum, 5, 20),
        "vitalidade": raw(vitality, 4, 20),
        "aspectos_sociais": raw(q6 + q10, 2, 8),
        "limitacao_emocional": raw(q5, 3, 3),
        "saude_mental": raw(mental, 5, 25),
    }


# ---------------------------------------------------------------------------
# Auth / permission helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Apenas admin master."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            flash("Acesso restrito ao administrador master.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


def get_user_groups(user_id):
    """Todos os grupos em que o usuário é membro (qualquer papel)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT g.*, ug.role as ug_role
                FROM groups g
                JOIN user_groups ug ON g.id = ug.group_id
                WHERE ug.user_id = {PH}
                ORDER BY g.name""",
            (user_id,),
        )
        return cur.fetchall()


def get_managed_groups(user_id):
    """Grupos onde o usuário é admin de grupo."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT g.*
                FROM groups g
                JOIN user_groups ug ON g.id = ug.group_id
                WHERE ug.user_id = {PH} AND ug.role = 'admin'
                ORDER BY g.name""",
            (user_id,),
        )
        return cur.fetchall()


def can_manage_group(user_id, group_id):
    """True se o usuário é admin master OU admin do grupo especificado."""
    if session.get("role") == "admin":
        return True
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT 1 FROM user_groups WHERE user_id = {PH} AND group_id = {PH} AND role = 'admin'",
            (user_id, group_id),
        )
        return bool(cur.fetchone())


@app.before_request
def force_password_change():
    if session.get("must_change_password") and request.endpoint not in ("alterar_senha", "logout", "static"):
        return redirect(url_for("alterar_senha"))


@app.context_processor
def inject_user():
    if "user_id" not in session:
        return dict(current_user=None, managed_groups=[])
    user = {
        "id": session["user_id"],
        "username": session["username"],
        "name": session["name"],
        "role": session["role"],
    }
    # Para estudantes, busca grupos que gerenciam (admin de grupo)
    managed_groups = []
    if session["role"] != "admin":
        managed_groups = get_managed_groups(session["user_id"])
    return dict(current_user=user, managed_groups=managed_groups)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM users WHERE username = {PH}", (username,))
            user = cur.fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["name"] = user["name"]
            session["role"] = user["role"]
            session["must_change_password"] = bool(user["must_change_password"])
            return redirect(url_for("index"))

        flash("Usuário ou senha inválidos.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/alterar-senha", methods=["GET", "POST"])
@login_required
def alterar_senha():
    if request.method == "POST":
        nova = request.form.get("nova_senha", "")
        confirma = request.form.get("confirma_senha", "")

        if len(nova) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.", "error")
        elif nova != confirma:
            flash("As senhas não coincidem.", "error")
        else:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"UPDATE users SET password_hash = {PH}, must_change_password = 0 WHERE id = {PH}",
                    (generate_password_hash(nova), session["user_id"]),
                )
            session["must_change_password"] = False
            flash("Senha alterada com sucesso!", "info")
            return redirect(url_for("index"))

    return render_template("alterar_senha.html")


# ---------------------------------------------------------------------------
# Main routes
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    if session["role"] == "admin":
        return redirect(url_for("admin_grupos"))
    groups = get_user_groups(session["user_id"])
    return render_template("index.html", today=date.today().isoformat(), groups=groups)


@app.route("/avaliar", methods=["POST"])
@login_required
def avaliar():
    if session["role"] == "admin":
        return redirect(url_for("admin_grupos"))

    form = request.form

    patient_name = form.get("patient_name", "").strip()
    if not patient_name:
        flash("O nome do paciente é obrigatório.", "error")
        return redirect(url_for("index"))

    responses = {}
    missing = []
    for field in Q_FIELDS:
        val = form.get(field)
        if val is None:
            missing.append(field)
        else:
            responses[field] = int(val)

    if missing:
        flash(f"Por favor, responda todas as questões. Faltando {len(missing)} resposta(s).", "error")
        return redirect(url_for("index"))

    group_id_raw = form.get("group_id", "").strip()
    group_id = int(group_id_raw) if group_id_raw else None

    if session["role"] != "admin":
        if not group_id:
            flash("Selecione um grupo para esta avaliação.", "error")
            return redirect(url_for("index"))
        user_group_ids = [g["id"] for g in get_user_groups(session["user_id"])]
        if group_id not in user_group_ids:
            flash("Grupo inválido.", "error")
            return redirect(url_for("index"))

    scores = calculate_sf36(responses)

    values = (
        [
            patient_name,
            form.get("patient_code", "").strip() or None,
            form.get("assessment_date", date.today().isoformat()),
            form.get("notes", "").strip() or None,
        ]
        + [responses[f] for f in Q_FIELDS]
        + [scores[f] for f in DOMAIN_FIELDS]
        + [group_id, session["user_id"]]
    )

    phs = ", ".join([PH] * len(INSERT_FIELDS))
    query = f"INSERT INTO assessments ({', '.join(INSERT_FIELDS)}) VALUES ({phs})"

    with get_db() as conn:
        cur = conn.cursor()
        if USE_PG:
            cur.execute(query + " RETURNING id", values)
            assessment_id = cur.fetchone()["id"]
        else:
            cur.execute(query, values)
            assessment_id = cur.lastrowid

    return redirect(url_for("resultado", id=assessment_id))


@app.route("/resultado/<int:id>")
@login_required
def resultado(id):
    if session["role"] == "admin":
        return redirect(url_for("admin_grupos"))

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM assessments WHERE id = {PH}", (id,))
        row = cur.fetchone()

    if row is None:
        flash("Avaliação não encontrada.", "error")
        return redirect(url_for("pacientes"))

    if session["role"] != "admin" and row["group_id"] is not None:
        user_group_ids = [g["id"] for g in get_user_groups(session["user_id"])]
        if row["group_id"] not in user_group_ids:
            flash("Acesso não autorizado.", "error")
            return redirect(url_for("pacientes"))

    domains = {key: {"label": label, "score": row[key]} for key, label in DOMAIN_LABELS.items()}
    return render_template("result.html", row=row, domains=domains)


@app.route("/pacientes")
@login_required
def pacientes():
    if session["role"] == "admin":
        return redirect(url_for("admin_grupos"))

    search = request.args.get("q", "").strip()
    group_filter = request.args.get("grupo", "").strip()

    with get_db() as conn:
        cur = conn.cursor()
        all_groups = get_user_groups(session["user_id"])

        conditions = []
        params = []

        if session["role"] != "admin":
            user_group_ids = [g["id"] for g in all_groups]
            if not user_group_ids:
                return render_template(
                    "patients.html", rows=[], search=search,
                    domain_labels=DOMAIN_LABELS, domains_order=DOMAIN_FIELDS,
                    all_groups=[], group_filter="",
                )
            placeholders = ", ".join([PH] * len(user_group_ids))
            conditions.append(f"a.group_id IN ({placeholders})")
            params.extend(user_group_ids)

        if group_filter:
            conditions.append(f"a.group_id = {PH}")
            params.append(int(group_filter))

        if search:
            conditions.append(f"(a.patient_name LIKE {PH} OR a.patient_code LIKE {PH})")
            like = f"%{search}%"
            params.extend([like, like])

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute(
            f"""SELECT a.*, g.name as group_name
                FROM assessments a
                LEFT JOIN groups g ON a.group_id = g.id
                {where}
                ORDER BY a.created_at DESC LIMIT 500""",
            params,
        )
        rows = cur.fetchall()

    return render_template(
        "patients.html", rows=rows, search=search,
        domain_labels=DOMAIN_LABELS, domains_order=DOMAIN_FIELDS,
        all_groups=all_groups, group_filter=group_filter,
    )


@app.route("/resultado/<int:id>/excluir", methods=["POST"])
@admin_required
def excluir(id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM assessments WHERE id = {PH}", (id,))
    flash("Avaliação excluída.", "info")
    return redirect(url_for("pacientes"))


# ---------------------------------------------------------------------------
# Admin master — Usuários
# ---------------------------------------------------------------------------

@app.route("/admin/usuarios", methods=["GET", "POST"])
@admin_required
def admin_usuarios():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "create":
            username = request.form.get("username", "").strip()
            name = request.form.get("name", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "student")
            if not username or not name or not password:
                flash("Preencha todos os campos obrigatórios.", "error")
            else:
                try:
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            f"INSERT INTO users (username, password_hash, name, role) VALUES ({PH},{PH},{PH},{PH})",
                            (username, generate_password_hash(password), name, role),
                        )
                    flash(f"Usuário '{username}' criado.", "info")
                except Exception:
                    flash("Nome de usuário já existe.", "error")

        elif action == "delete":
            user_id = int(request.form.get("user_id"))
            if user_id == session["user_id"]:
                flash("Você não pode excluir sua própria conta.", "error")
            else:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(f"DELETE FROM user_groups WHERE user_id = {PH}", (user_id,))
                    cur.execute(f"DELETE FROM users WHERE id = {PH}", (user_id,))
                flash("Usuário excluído.", "info")

        elif action == "reset_password":
            user_id = int(request.form.get("user_id"))
            new_password = request.form.get("new_password", "")
            if len(new_password) < 6:
                flash("A nova senha deve ter pelo menos 6 caracteres.", "error")
            else:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        f"UPDATE users SET password_hash = {PH} WHERE id = {PH}",
                        (generate_password_hash(new_password), user_id),
                    )
                flash("Senha redefinida.", "info")

        return redirect(url_for("admin_usuarios"))

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users ORDER BY name")
        users = cur.fetchall()

    return render_template("admin/usuarios.html", users=users)


# ---------------------------------------------------------------------------
# Admin master — Grupos
# ---------------------------------------------------------------------------

@app.route("/admin/grupos", methods=["GET", "POST"])
@admin_required
def admin_grupos():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        if not name:
            flash("O nome do grupo é obrigatório.", "error")
        else:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"INSERT INTO groups (name, description) VALUES ({PH},{PH})",
                    (name, description),
                )
            flash(f"Grupo '{name}' criado.", "info")
        return redirect(url_for("admin_grupos"))

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT g.*, COUNT(ug.user_id) as member_count
            FROM groups g
            LEFT JOIN user_groups ug ON g.id = ug.group_id
            GROUP BY g.id
            ORDER BY g.name
        """)
        groups = cur.fetchall()

    return render_template("admin/grupos.html", groups=groups)


@app.route("/admin/grupos/<int:group_id>", methods=["GET", "POST"])
@admin_required
def admin_grupo_detail(group_id):
    if request.method == "POST":
        action = request.form.get("action")

        if action == "create_group_admin":
            username = request.form.get("username", "").strip()
            name = request.form.get("name", "").strip()
            password = request.form.get("password", "")
            if not username or not name or not password:
                flash("Preencha todos os campos.", "error")
            elif len(password) < 6:
                flash("A senha deve ter pelo menos 6 caracteres.", "error")
            else:
                try:
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            f"INSERT INTO users (username, password_hash, name, role) VALUES ({PH},{PH},{PH},'student')",
                            (username, generate_password_hash(password), name),
                        )
                        if USE_PG:
                            cur.execute(f"SELECT id FROM users WHERE username = {PH}", (username,))
                            new_user_id = cur.fetchone()["id"]
                        else:
                            new_user_id = cur.lastrowid
                        cur.execute(
                            f"INSERT INTO user_groups (user_id, group_id, role) VALUES ({PH},{PH},'admin')",
                            (new_user_id, group_id),
                        )
                    flash(f"Admin do grupo '{username}' criado com sucesso.", "info")
                except Exception:
                    flash("Nome de usuário já existe.", "error")

        elif action == "remove_group_admin":
            user_id = int(request.form.get("user_id"))
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"DELETE FROM user_groups WHERE user_id = {PH} AND group_id = {PH}",
                    (user_id, group_id),
                )
            flash("Admin do grupo removido.", "info")

        elif action == "delete_group":
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(f"DELETE FROM user_groups WHERE group_id = {PH}", (group_id,))
                cur.execute(f"UPDATE assessments SET group_id = NULL WHERE group_id = {PH}", (group_id,))
                cur.execute(f"DELETE FROM groups WHERE id = {PH}", (group_id,))
            flash("Grupo excluído.", "info")
            return redirect(url_for("admin_grupos"))

        elif action == "edit_group":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip() or None
            if name:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        f"UPDATE groups SET name = {PH}, description = {PH} WHERE id = {PH}",
                        (name, description, group_id),
                    )
                flash("Grupo atualizado.", "info")

        return redirect(url_for("admin_grupo_detail", group_id=group_id))

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM groups WHERE id = {PH}", (group_id,))
        group = cur.fetchone()
        if not group:
            flash("Grupo não encontrado.", "error")
            return redirect(url_for("admin_grupos"))

        # Admins do grupo
        cur.execute(
            f"""SELECT u.*
                FROM users u
                JOIN user_groups ug ON u.id = ug.user_id
                WHERE ug.group_id = {PH} AND ug.role = 'admin'
                ORDER BY u.name""",
            (group_id,),
        )
        group_admins = cur.fetchall()

        # Estudantes do grupo (membros comuns)
        cur.execute(
            f"""SELECT u.*
                FROM users u
                JOIN user_groups ug ON u.id = ug.user_id
                WHERE ug.group_id = {PH} AND ug.role = 'member'
                ORDER BY u.name""",
            (group_id,),
        )
        members = cur.fetchall()

        cur.execute(f"SELECT COUNT(*) FROM assessments WHERE group_id = {PH}", (group_id,))
        assessment_count = cur.fetchone()[0]

    return render_template(
        "admin/grupo_detail.html",
        group=group, group_admins=group_admins, members=members,
        assessment_count=assessment_count,
    )


# ---------------------------------------------------------------------------
# Admin de Grupo — gerenciar membros do próprio grupo
# ---------------------------------------------------------------------------

@app.route("/grupo/<int:group_id>/gerenciar", methods=["GET", "POST"])
@login_required
def grupo_gerenciar(group_id):
    if not can_manage_group(session["user_id"], group_id):
        flash("Acesso não autorizado.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        action = request.form.get("action")

        if action == "create_user":
            username = request.form.get("username", "").strip()
            name = request.form.get("name", "").strip()
            if not username or not name:
                flash("Nome e usuário são obrigatórios.", "error")
            else:
                DEFAULT_PASSWORD = "123456"
                try:
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            f"INSERT INTO users (username, password_hash, name, role, must_change_password) VALUES ({PH},{PH},{PH},'student',1)",
                            (username, generate_password_hash(DEFAULT_PASSWORD), name),
                        )
                        if USE_PG:
                            new_user_id = cur.fetchone()["id"] if False else None
                            cur.execute(f"SELECT id FROM users WHERE username = {PH}", (username,))
                            new_user_id = cur.fetchone()["id"]
                        else:
                            new_user_id = cur.lastrowid
                        cur.execute(
                            f"INSERT INTO user_groups (user_id, group_id, role) VALUES ({PH},{PH},'member')",
                            (new_user_id, group_id),
                        )
                    flash(f"Usuário '{username}' criado com senha padrão 123456. O estudante deverá trocar no primeiro acesso.", "info")
                except Exception:
                    flash("Nome de usuário já existe.", "error")

        elif action == "add_member":
            user_id = int(request.form.get("user_id"))
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    # Admin de grupo só pode adicionar como membro comum
                    cur.execute(
                        f"INSERT INTO user_groups (user_id, group_id, role) VALUES ({PH},{PH},'member')",
                        (user_id, group_id),
                    )
                flash("Estudante adicionado ao grupo.", "info")
            except Exception:
                flash("Usuário já é membro deste grupo.", "error")

        elif action == "remove_member":
            user_id = int(request.form.get("user_id"))
            # Admin de grupo não pode remover outro admin de grupo (apenas master admin pode)
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT role FROM user_groups WHERE user_id = {PH} AND group_id = {PH}",
                    (user_id, group_id),
                )
                row = cur.fetchone()
            if row and row["role"] == "admin" and session["role"] != "admin":
                flash("Apenas o admin master pode remover outro admin de grupo.", "error")
            else:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        f"DELETE FROM user_groups WHERE user_id = {PH} AND group_id = {PH}",
                        (user_id, group_id),
                    )
                flash("Membro removido.", "info")

        return redirect(url_for("grupo_gerenciar", group_id=group_id))

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM groups WHERE id = {PH}", (group_id,))
        group = cur.fetchone()
        if not group:
            flash("Grupo não encontrado.", "error")
            return redirect(url_for("index"))

        cur.execute(
            f"""SELECT u.*, ug.role as ug_role
                FROM users u
                JOIN user_groups ug ON u.id = ug.user_id
                WHERE ug.group_id = {PH}
                ORDER BY ug.role DESC, u.name""",
            (group_id,),
        )
        members = cur.fetchall()

        member_ids = [m["id"] for m in members]
        if member_ids:
            placeholders = ", ".join([PH] * len(member_ids))
            cur.execute(
                f"SELECT * FROM users WHERE id NOT IN ({placeholders}) AND role = 'student' ORDER BY name",
                member_ids,
            )
        else:
            cur.execute("SELECT * FROM users WHERE role = 'student' ORDER BY name")
        non_members = cur.fetchall()

        cur.execute(f"SELECT COUNT(*) FROM assessments WHERE group_id = {PH}", (group_id,))
        assessment_count = cur.fetchone()[0]

    return render_template(
        "grupo_gerenciar.html",
        group=group, members=members, non_members=non_members,
        assessment_count=assessment_count,
    )


# ---------------------------------------------------------------------------
# Exportação — helpers PDF
# ---------------------------------------------------------------------------

def _score_color_rgb(score):
    if score >= 67:
        return (0.086, 0.639, 0.290)   # #16a34a verde
    if score >= 34:
        return (0.792, 0.541, 0.024)   # #ca8a04 amarelo
    return (0.863, 0.149, 0.149)       # #dc2626 vermelho


def _score_label(score):
    if score >= 67:
        return "Alto"
    if score >= 34:
        return "Médio"
    return "Baixo"


def build_assessment_pdf(row, domains, group_name=None):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    )
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.graphics import renderPDF

    W, H = A4
    PRIMARY   = colors.HexColor("#2563eb")
    GRAY_700  = colors.HexColor("#374151")
    GRAY_500  = colors.HexColor("#6b7280")
    GRAY_100  = colors.HexColor("#f3f4f6")
    GRAY_200  = colors.HexColor("#e5e7eb")
    WHITE     = colors.white

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2.5*cm,
    )

    def _style(name, **kw):
        base = dict(fontName="Helvetica", fontSize=10, textColor=GRAY_700, leading=14)
        base.update(kw)
        return ParagraphStyle(name, **base)

    s_title    = _style("title",    fontName="Helvetica-Bold", fontSize=22, textColor=PRIMARY, leading=28)
    s_sub      = _style("sub",      fontSize=10, textColor=GRAY_500, spaceAfter=6)
    s_label    = _style("label",    fontName="Helvetica-Bold", fontSize=8,  textColor=GRAY_500,
                        leading=10, spaceBefore=10)
    s_value    = _style("value",    fontName="Helvetica-Bold", fontSize=12, textColor=GRAY_700, leading=16)
    s_domain   = _style("domain",   fontName="Helvetica-Bold", fontSize=9,  textColor=GRAY_700, leading=12)
    s_footer   = _style("footer",   fontSize=8,  textColor=GRAY_500)

    story = []

    # ── Cabeçalho ──────────────────────────────────────────────────────────
    story.append(Paragraph("SF-36", s_title))
    story.append(Paragraph("Questionário Brasileiro de Qualidade de Vida", s_sub))
    story.append(HRFlowable(width="100%", thickness=2, color=PRIMARY, spaceAfter=14))

    # ── Dados do paciente ──────────────────────────────────────────────────
    info_data = [
        [
            Paragraph("<b>Paciente</b>", _style("lbl", fontSize=8, textColor=GRAY_500)),
            Paragraph("<b>Prontuário</b>", _style("lbl", fontSize=8, textColor=GRAY_500)),
            Paragraph("<b>Data</b>", _style("lbl", fontSize=8, textColor=GRAY_500)),
            Paragraph("<b>Grupo</b>", _style("lbl", fontSize=8, textColor=GRAY_500)),
        ],
        [
            Paragraph(str(row["patient_name"]), s_value),
            Paragraph(str(row["patient_code"] or "—"), s_value),
            Paragraph(str(row["assessment_date"]), s_value),
            Paragraph(str(group_name or "—"), s_value),
        ],
    ]
    info_table = Table(info_data, colWidths=[(W - 4*cm) * f for f in [0.35, 0.2, 0.2, 0.25]])
    info_table.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), GRAY_100),
        ("BACKGROUND",   (0, 1), (-1, 1), WHITE),
        ("BOX",          (0, 0), (-1, -1), 0.5, GRAY_200),
        ("INNERGRID",    (0, 0), (-1, -1), 0.3, GRAY_200),
        ("TOPPADDING",   (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(info_table)

    if row["notes"]:
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"<i>Obs: {row['notes']}</i>",
                                _style("notes", fontSize=9, textColor=GRAY_500)))

    story.append(Spacer(1, 18))
    story.append(Paragraph("DOMÍNIOS DE QUALIDADE DE VIDA",
                            _style("sec", fontName="Helvetica-Bold", fontSize=9,
                                   textColor=GRAY_500, spaceAfter=10)))

    # ── Domínios ───────────────────────────────────────────────────────────
    BAR_W   = (W - 4*cm) * 0.52
    SCORE_W = (W - 4*cm) * 0.10
    LABEL_W = (W - 4*cm) * 0.10
    NAME_W  = (W - 4*cm) - BAR_W - SCORE_W - LABEL_W

    for key, info in domains.items():
        score = info["score"]
        r, g, b = _score_color_rgb(score)
        fill_c  = colors.Color(r, g, b)
        empty_c = colors.HexColor("#e5e7eb")
        badge_c = colors.Color(r, g, b, alpha=0.15)

        fill_px = int(BAR_W * score / 100)

        bar_draw = Drawing(BAR_W, 14)
        bar_draw.add(Rect(0, 3, BAR_W, 8,   fillColor=empty_c, strokeColor=None))
        bar_draw.add(Rect(0, 3, max(fill_px, 2), 8, fillColor=fill_c, strokeColor=None))

        row_data = [[
            Paragraph(info["label"], s_domain),
            bar_draw,
            Paragraph(f"<b>{score:.1f}</b>", _style("sc", fontName="Helvetica-Bold",
                                                      fontSize=10, textColor=fill_c)),
            Paragraph(_score_label(score), _style("badge", fontSize=8,
                                                   textColor=fill_c, alignment=1)),
        ]]
        t = Table(row_data, colWidths=[NAME_W, BAR_W, SCORE_W, LABEL_W])
        t.setStyle(TableStyle([
            ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",  (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING",   (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
            ("LINEBELOW",    (0, 0), (-1, 0), 0.3, GRAY_200),
        ]))
        story.append(t)

    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GRAY_200, spaceAfter=6))
    story.append(Paragraph(
        f"Relatório gerado em {date.today().strftime('%d/%m/%Y')} — SF-36 Qualidade de Vida",
        s_footer,
    ))

    doc.build(story)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Exportação — rotas
# ---------------------------------------------------------------------------

@app.route("/resultado/<int:id>/pdf")
@login_required
def resultado_pdf(id):
    if session["role"] == "admin":
        return redirect(url_for("admin_grupos"))

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT a.*, g.name as group_name FROM assessments a LEFT JOIN groups g ON a.group_id = g.id WHERE a.id = {PH}",
            (id,),
        )
        row = cur.fetchone()

    if row is None:
        flash("Avaliação não encontrada.", "error")
        return redirect(url_for("pacientes"))

    if session["role"] != "admin" and row["group_id"] is not None:
        user_group_ids = [g["id"] for g in get_user_groups(session["user_id"])]
        if row["group_id"] not in user_group_ids:
            flash("Acesso não autorizado.", "error")
            return redirect(url_for("pacientes"))

    domains = {key: {"label": label, "score": row[key]} for key, label in DOMAIN_LABELS.items()}
    pdf_buf = build_assessment_pdf(row, domains, group_name=row["group_name"])

    safe_name = "".join(c if c.isalnum() else "_" for c in str(row["patient_name"]))
    filename = f"SF36_{safe_name}_{row['assessment_date']}.pdf"

    return Response(
        pdf_buf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/pacientes/exportar")
@login_required
def pacientes_exportar():
    if session["role"] == "admin":
        return redirect(url_for("admin_grupos"))

    fmt         = request.args.get("fmt", "csv")
    search      = request.args.get("q", "").strip()
    group_filter = request.args.get("grupo", "").strip()

    with get_db() as conn:
        cur = conn.cursor()
        all_groups = get_user_groups(session["user_id"])

        conditions, params = [], []
        user_group_ids = [g["id"] for g in all_groups]
        if not user_group_ids:
            rows = []
        else:
            placeholders = ", ".join([PH] * len(user_group_ids))
            conditions.append(f"a.group_id IN ({placeholders})")
            params.extend(user_group_ids)
            if group_filter:
                conditions.append(f"a.group_id = {PH}")
                params.append(int(group_filter))
            if search:
                conditions.append(f"(a.patient_name LIKE {PH} OR a.patient_code LIKE {PH})")
                like = f"%{search}%"
                params.extend([like, like])
            where = "WHERE " + " AND ".join(conditions)
            cur.execute(
                f"""SELECT a.*, g.name as group_name
                    FROM assessments a
                    LEFT JOIN groups g ON a.group_id = g.id
                    {where}
                    ORDER BY a.assessment_date DESC""",
                params,
            )
            rows = cur.fetchall()

    if fmt == "csv":
        return _export_csv(rows)
    return _export_list_pdf(rows)


def _export_csv(rows):
    si = StringIO()
    w = csv.writer(si)
    w.writerow([
        "Paciente", "Prontuário", "Grupo", "Data",
        "Cap. Funcional", "Lim. Física", "Dor", "Est. Geral Saúde",
        "Vitalidade", "Asp. Sociais", "Lim. Emocional", "Saúde Mental",
    ])
    for r in rows:
        w.writerow([
            r["patient_name"], r["patient_code"] or "", r["group_name"] or "",
            r["assessment_date"],
            r["capacidade_funcional"], r["limitacao_fisica"], r["dor"],
            r["estado_geral_saude"], r["vitalidade"], r["aspectos_sociais"],
            r["limitacao_emocional"], r["saude_mental"],
        ])
    output = si.getvalue().encode("utf-8-sig")  # BOM para Excel
    filename = f"SF36_prontuarios_{date.today()}.csv"
    return Response(
        output,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8-sig",
        },
    )


def _export_list_pdf(rows):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable

    W, H = landscape(A4)
    PRIMARY  = colors.HexColor("#2563eb")
    GRAY_100 = colors.HexColor("#f3f4f6")
    GRAY_200 = colors.HexColor("#e5e7eb")
    GRAY_500 = colors.HexColor("#6b7280")
    GRAY_700 = colors.HexColor("#374151")

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)

    def _p(txt, bold=False, size=9, color=GRAY_700, align=1):
        fn = "Helvetica-Bold" if bold else "Helvetica"
        return Paragraph(f"<font name='{fn}' size='{size}' color='{color.hexval()}'>{txt}</font>",
                         ParagraphStyle("x", alignment=align, leading=12))

    story = []
    story.append(Paragraph(
        "<font name='Helvetica-Bold' size='16' color='#2563eb'>SF-36</font>"
        "&nbsp;&nbsp;<font name='Helvetica' size='10' color='#6b7280'>Relatório de Prontuários</font>",
        ParagraphStyle("hd", leading=20),
    ))
    story.append(Paragraph(
        f"<font name='Helvetica' size='8' color='#6b7280'>Gerado em {date.today().strftime('%d/%m/%Y')} — {len(rows)} avaliação(ões)</font>",
        ParagraphStyle("dt", leading=12, spaceAfter=12),
    ))
    story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY, spaceAfter=12))

    if not rows:
        story.append(Paragraph("Nenhuma avaliação encontrada.",
                                ParagraphStyle("e", textColor=GRAY_500)))
    else:
        SHORT = {
            "capacidade_funcional": "Cap.\nFunc.",
            "limitacao_fisica":     "Lim.\nFísica",
            "dor":                  "Dor",
            "estado_geral_saude":   "Est.\nSaúde",
            "vitalidade":           "Vital.",
            "aspectos_sociais":     "Asp.\nSociais",
            "limitacao_emocional":  "Lim.\nEmoc.",
            "saude_mental":         "Saúde\nMental",
        }
        header = [
            _p("Paciente",   bold=True, size=8, align=0),
            _p("Prontuário", bold=True, size=8),
            _p("Grupo",      bold=True, size=8, align=0),
            _p("Data",       bold=True, size=8),
        ] + [_p(v, bold=True, size=7) for v in SHORT.values()]

        table_data = [header]
        ts = TableStyle([
            ("BACKGROUND",   (0, 0), (-1, 0), PRIMARY),
            ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, GRAY_100]),
            ("BOX",          (0, 0), (-1, -1), 0.3, GRAY_200),
            ("INNERGRID",    (0, 0), (-1, -1), 0.2, GRAY_200),
            ("TOPPADDING",   (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
            ("LEFTPADDING",  (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ])

        for i, r in enumerate(rows):
            score_cells = []
            for key in SHORT:
                s = r[key]
                rc, gc, bc = _score_color_rgb(s)
                sc = colors.Color(rc, gc, bc)
                score_cells.append(
                    Paragraph(f"<font name='Helvetica-Bold' size='8' color='{sc.hexval()}'>{s:.0f}</font>",
                               ParagraphStyle("sc", alignment=1, leading=10))
                )
            table_data.append([
                _p(r["patient_name"][:24], size=8, align=0),
                _p(r["patient_code"] or "—", size=8),
                _p((r["group_name"] or "—")[:18], size=8, align=0),
                _p(r["assessment_date"], size=8),
            ] + score_cells)

        avail_w = W - 3*cm
        col_w = [avail_w * f for f in [0.18, 0.10, 0.14, 0.09] + [0.0612] * 8]
        t = Table(table_data, colWidths=col_w, repeatRows=1)
        t.setStyle(ts)
        story.append(t)

    doc.build(story)
    buf.seek(0)
    filename = f"SF36_prontuarios_{date.today()}.pdf"
    return Response(buf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
