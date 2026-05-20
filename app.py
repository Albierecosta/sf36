import os
import contextlib
from datetime import date
from flask import Flask, render_template, request, redirect, url_for, flash

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


def init_db():
    with get_db() as conn:
        cur = conn.cursor()
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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


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

INSERT_FIELDS = ["patient_name", "patient_code", "assessment_date", "notes"] + Q_FIELDS + DOMAIN_FIELDS


def calculate_sf36(data: dict) -> dict:
    q1_map  = {1: 5.0, 2: 4.4, 3: 3.4, 4: 2.0, 5: 1.0}
    q6_map  = {1: 5,   2: 4,   3: 3,   4: 2,   5: 1  }
    q7_map  = {1: 6.0, 2: 5.4, 3: 4.2, 4: 3.1, 5: 2.0, 6: 1.0}
    inv6    = {1: 6,   2: 5,   3: 4,   4: 3,   5: 2,   6: 1  }
    inv5    = {1: 5,   2: 4,   3: 3,   4: 2,   5: 1  }

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
    mental   = q9["b"] + q9["c"] + q9["d"] + q9["f"] + q9["h"]

    return {
        "capacidade_funcional": raw(q3,          10, 20),
        "limitacao_fisica":     raw(q4,           4,  4),
        "dor":                  raw(q7 + q8,      2, 10),
        "estado_geral_saude":   raw(q1 + q11_sum, 5, 20),
        "vitalidade":           raw(vitality,     4, 20),
        "aspectos_sociais":     raw(q6 + q10,     2,  8),
        "limitacao_emocional":  raw(q5,           3,  3),
        "saude_mental":         raw(mental,       5, 25),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", today=date.today().isoformat())


@app.route("/avaliar", methods=["POST"])
def avaliar():
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

    scores = calculate_sf36(responses)

    values = (
        [patient_name,
         form.get("patient_code", "").strip() or None,
         form.get("assessment_date", date.today().isoformat()),
         form.get("notes", "").strip() or None]
        + [responses[f] for f in Q_FIELDS]
        + [scores[f] for f in DOMAIN_FIELDS]
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
def resultado(id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM assessments WHERE id = {PH}", (id,))
        row = cur.fetchone()

    if row is None:
        flash("Avaliação não encontrada.", "error")
        return redirect(url_for("pacientes"))

    domains = {key: {"label": label, "score": row[key]} for key, label in DOMAIN_LABELS.items()}
    return render_template("result.html", row=row, domains=domains)


@app.route("/pacientes")
def pacientes():
    search = request.args.get("q", "").strip()
    with get_db() as conn:
        cur = conn.cursor()
        if search:
            like = f"%{search}%"
            cur.execute(
                f"SELECT * FROM assessments WHERE patient_name LIKE {PH} OR patient_code LIKE {PH} ORDER BY created_at DESC",
                (like, like),
            )
        else:
            cur.execute("SELECT * FROM assessments ORDER BY created_at DESC LIMIT 500")
        rows = cur.fetchall()

    return render_template("patients.html", rows=rows, search=search,
                           domain_labels=DOMAIN_LABELS, domains_order=DOMAIN_FIELDS)


@app.route("/resultado/<int:id>/excluir", methods=["POST"])
def excluir(id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM assessments WHERE id = {PH}", (id,))
    flash("Avaliação excluída.", "info")
    return redirect(url_for("pacientes"))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
