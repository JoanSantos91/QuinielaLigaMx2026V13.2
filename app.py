import base64
import hashlib
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from schedule_data import PLAYER_PINS, SCHEDULE

APP_NAME = "Quiniela Joan Santos"
DB_PATH = Path(__file__).with_name("quiniela.db")
ASSETS = Path(__file__).with_name("assets")
TZ = ZoneInfo("America/Mexico_City")

# El organizador aparece primero, como se solicitó.
PLAYERS = [
    ("CAZ", "Joan Santos", "Cruz Azul"),
    ("TOL", "Diego", "Toluca"),
    ("TIG", "Lupe", "Tigres UANL"),
    ("AME", "Oscar", "América"),
    ("CHI", "Pity", "Guadalajara"),
    ("ATL", "Sholko", "Atlante"),
    ("MTY", "José Luis", "Monterrey"),
    ("ATS", "Lugo", "Atlas"),
    ("JUA", "Jorge Ceballos", "Juárez"),
    ("PUE", "Giovanni Román", "Puebla"),
    ("SAN", "José Juan", "Santos Laguna"),
    ("PUM", "Ricky Zazueta", "Pumas UNAM"),
    ("NEC", "Sebastián", "Necaxa"),
    ("QRO", "Juan Antonio", "Querétaro"),
    ("LEO", "Roger", "León"),
    ("TIJ", "Laura", "Tijuana"),
    ("SLP", "Chino Terrazas", "Atlético de San Luis"),
    ("PAC", "Rodolfo Félix", "Pachuca"),
]

TEAM_SHORT = {
    "Atlético de San Luis": "San Luis", "Guadalajara": "Chivas",
    "Pumas UNAM": "Pumas", "Santos Laguna": "Santos", "Tigres UANL": "Tigres",
}
TEAM_SLUG = {
    "Toluca":"toluca", "Tigres UANL":"tigres-uanl", "América":"america", "Guadalajara":"guadalajara",
    "Atlante":"atlante", "Monterrey":"monterrey", "Atlas":"atlas", "Juárez":"juarez", "Puebla":"puebla",
    "Cruz Azul":"cruz-azul", "Santos Laguna":"santos-laguna", "Pumas UNAM":"pumas-unam", "Necaxa":"necaxa",
    "Querétaro":"queretaro", "León":"leon", "Tijuana":"tijuana", "Atlético de San Luis":"atletico-de-san-luis",
    "Pachuca":"pachuca",
}
ALL_TEAMS = sorted({team for games in SCHEDULE.values() for _, home, away in games for team in (home, away)})


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def conn():
    """Conexión corta y segura. No cambia el modo WAL en cada recarga."""
    connection = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def run_write(operation, retries=4):
    """Ejecuta una escritura atómica con reintentos breves si SQLite está ocupado."""
    delay = 0.15
    for attempt in range(retries):
        c = conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            operation(c)
            c.commit()
            return
        except sqlite3.OperationalError as exc:
            c.rollback()
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def team_logo(team: str) -> Path:
    return ASSETS / "team_logos" / f"{TEAM_SLUG.get(team, 'generic')}.png"


def init_db():
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, team TEXT,
            pin_hash TEXT, is_admin INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS rounds(
            id INTEGER PRIMARY KEY, number INTEGER UNIQUE, name TEXT,
            deadline TEXT, is_open INTEGER DEFAULT 0, reveal_override INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS matches(
            id INTEGER PRIMARY KEY, round_id INTEGER, home_team TEXT, away_team TEXT,
            kickoff TEXT, home_score INTEGER, away_score INTEGER,
            UNIQUE(round_id, home_team, away_team)
        );
        CREATE TABLE IF NOT EXISTS predictions(
            id INTEGER PRIMARY KEY, user_id INTEGER, match_id INTEGER,
            home_score INTEGER, away_score INTEGER, submitted_at TEXT,
            UNIQUE(user_id, match_id)
        );
        CREATE TABLE IF NOT EXISTS survivor_picks(
            id INTEGER PRIMARY KEY, user_id INTEGER, round_id INTEGER,
            team TEXT, submitted_at TEXT,
            UNIQUE(user_id, round_id), UNIQUE(user_id, team)
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS champion_eligible(team TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS champion_picks(
            id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, team TEXT UNIQUE,
            pick_order INTEGER, submitted_at TEXT
        );
        """)
        # Migración compatible con bases creadas por versiones anteriores.
        round_columns = {row["name"] for row in c.execute("PRAGMA table_info(rounds)").fetchall()}
        if "reveal_override" not in round_columns:
            c.execute("ALTER TABLE rounds ADD COLUMN reveal_override INTEGER DEFAULT 0")
        c.execute(
            "INSERT OR IGNORE INTO users(code,name,team,pin_hash,is_admin) VALUES(?,?,?,?,1)",
            ("ADMIN", "Administrador", "", hash_pin("5866")),
        )
        c.execute("UPDATE users SET pin_hash=? WHERE code='ADMIN'", (hash_pin("5866"),))
        for code, name, team in PLAYERS:
            c.execute(
                "INSERT OR IGNORE INTO users(code,name,team,pin_hash,is_admin) VALUES(?,?,?,?,0)",
                (code, name, team, hash_pin(PLAYER_PINS[code])),
            )
            # Mantiene sincronizados nombre, equipo y nuevos PIN aun si existe una base previa.
            c.execute(
                "UPDATE users SET name=?, team=?, pin_hash=? WHERE code=?",
                (name, team, hash_pin(PLAYER_PINS[code]), code),
            )
        for number, games in SCHEDULE.items():
            deadline = min(datetime.fromisoformat(game[0]) for game in games).isoformat(timespec="minutes")
            c.execute(
                "INSERT OR IGNORE INTO rounds(number,name,deadline,is_open) VALUES(?,?,?,0)",
                (number, f"Jornada {number}", deadline),
            )
            round_id = c.execute("SELECT id FROM rounds WHERE number=?", (number,)).fetchone()[0]
            for kickoff, home, away in games:
                c.execute(
                    "INSERT OR IGNORE INTO matches(round_id,home_team,away_team,kickoff) VALUES(?,?,?,?)",
                    (round_id, home, away, kickoff),
                )
        c.execute("INSERT OR IGNORE INTO settings VALUES('champion_draft_active','0')")


def inject_style():
    st.markdown("""
    <style>
    :root{--mx-green:#00A94F;--mx-pink:#E6007E;--mx-navy:#071426;--mx-blue:#123B68;--mx-bg:#F2F6FB;--mx-card:#FFFFFF;--mx-text:#101828;--mx-muted:#667085;--mx-border:#D7E0EA}
    .stApp{background:linear-gradient(180deg,#eef4fa 0,#f8fafc 280px);color:var(--mx-text)}
    [data-testid="stHeader"]{background:rgba(242,246,251,.92);backdrop-filter:blur(10px)}
    .block-container{max-width:1160px;padding-top:.7rem;padding-bottom:4rem} h1,h2,h3,p,label,.stMarkdown,.stCaption{color:var(--mx-text)}
    .hero{position:relative;overflow:hidden;display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:16px;padding:18px 22px;margin-bottom:18px;background:linear-gradient(112deg,#061526 0%,#0b3156 60%,#0a4d60 100%);border-radius:24px;box-shadow:0 14px 34px rgba(7,20,38,.18)}
    .hero:before{content:"";position:absolute;inset:0;background:radial-gradient(circle at 88% 24%,rgba(230,0,126,.42),transparent 25%),radial-gradient(circle at 72% 92%,rgba(0,169,79,.35),transparent 28%)}
    .hero>*{position:relative;z-index:1}.hero .league-logo{width:76px;height:58px;object-fit:contain;background:#fff;border-radius:15px;padding:8px}.hero .ball{width:116px;height:116px;border-radius:50%;object-fit:cover;border:4px solid rgba(255,255,255,.82)}
    .hero h1{margin:0;color:#fff;font-size:clamp(1.45rem,4vw,2.35rem)}.hero p{margin:3px 0 0;color:#d8e8f7}.hero .tag{display:inline-flex;margin-top:8px;padding:5px 11px;border-radius:999px;background:linear-gradient(90deg,var(--mx-green),#0bc46a);color:#fff;font-size:.78rem;font-weight:850}
    .login-shell{max-width:560px;margin:0 auto}.profile-card{display:flex;align-items:center;gap:14px;background:#fff;border:1px solid var(--mx-border);border-left:6px solid var(--mx-green);border-radius:18px;padding:15px 17px;margin:10px 0 16px;box-shadow:0 8px 20px rgba(15,23,42,.06)}
    .profile-card img{width:96px;height:96px;object-fit:contain}.profile-card .name{font-size:1.15rem;font-weight:900}.profile-card .sub{color:var(--mx-muted)}
    .section-note{background:linear-gradient(90deg,#e9fbf2,#f4fffa);border:1px solid #aee9ca;border-radius:14px;padding:11px 13px;color:#075f43;font-weight:750}
    .match-title{text-align:center;font-size:.78rem;color:var(--mx-muted);font-weight:800;margin-bottom:6px;text-transform:uppercase}.team-name{text-align:center;font-weight:900;font-size:.92rem;line-height:1.1;margin-top:5px;color:var(--mx-text)}.score-sep{text-align:center;font-size:1.65rem;font-weight:900;color:var(--mx-pink)}
    .privacy-lock{background:#fff5fb;border:1px solid #efb9d7;border-radius:14px;padding:12px;color:#8b1455}.privacy-open{background:#edfdf4;border:1px solid #b6e9ca;border-radius:14px;padding:12px;color:#0b6b3e;font-weight:750}
    .table-title{display:flex;align-items:center;justify-content:space-between;margin:.3rem 0 .8rem}.table-title h3{margin:0}.table-pill{background:var(--mx-navy);color:#fff;padding:5px 10px;border-radius:999px;font-size:.75rem;font-weight:800}
    .rank-table{width:100%;border-collapse:separate;border-spacing:0 7px}.rank-table th{padding:7px 10px;color:#667085;font-size:.74rem;text-transform:uppercase;text-align:left}.rank-table td{padding:10px;background:#fff;border-top:1px solid #e2e8f0;border-bottom:1px solid #e2e8f0}.rank-table td:first-child{border-left:4px solid var(--mx-green);border-radius:12px 0 0 12px;text-align:center;font-weight:950;width:52px}.rank-table td:last-child{border-right:1px solid #e2e8f0;border-radius:0 12px 12px 0}.rank-table tr.top1 td{background:linear-gradient(90deg,#2f741e,#3f8b29);color:#fff;border-color:#28651a}.rank-table tr.top1 small{color:#e8f7df!important}.rank-table tr.top2 td{background:linear-gradient(90deg,#1763a4,#2776b9);color:#fff;border-color:#15568e}.rank-table tr.top2 small{color:#e2f2ff!important}.rank-table tr.top3 td{background:linear-gradient(90deg,#4a86df,#5a97ec);color:#fff;border-color:#3c76c8}.rank-table tr.top3 small{color:#edf5ff!important}.rank-table tr.alt td{background:#f0f3f6}.rank-table tr.base td{background:#fff}.rank-table tr.qualifier td:first-child{border-left:7px solid #11a8ff}.rank-table tr.top1 .pts,.rank-table tr.top2 .pts,.rank-table tr.top3 .pts{color:#fff}.club-cell{display:flex;align-items:center;gap:10px;font-weight:850}.club-cell img{width:52px;height:52px;object-fit:contain}.pts{font-size:1.05rem;font-weight:950;color:var(--mx-navy)}
    .rank-table{table-layout:fixed;min-width:570px}
    .rank-table .rank-pos{width:54px;text-align:center}
    .rank-table .rank-participant{width:250px;text-align:left}
    .rank-table .rank-number{width:66px;text-align:center!important;vertical-align:middle;font-variant-numeric:tabular-nums}
    .rank-table .club-cell{min-width:0;width:100%}
    .rank-table .participant-text{display:flex;flex-direction:column;min-width:0;line-height:1.15}
    .rank-table .participant-name{display:block;white-space:normal;overflow-wrap:anywhere;word-break:normal;line-height:1.15}
    .rank-table .participant-text small{display:block;margin-top:4px;white-space:normal;line-height:1.2}
    [data-testid="stVerticalBlockBorderWrapper"]{background:var(--mx-card);border-color:var(--mx-border)!important;border-radius:18px!important;box-shadow:0 5px 16px rgba(7,26,51,.06)} div[data-testid="stMetric"]{background:#fff;border:1px solid var(--mx-border);padding:12px;border-radius:15px}
    .stButton>button,.stDownloadButton>button{border-radius:12px;font-weight:850;min-height:44px}.stButton>button[kind="primary"]{background:linear-gradient(90deg,var(--mx-green),#08bf69);border:0;color:#fff}
    div[data-baseweb="select"]>div,input{background:#fff!important;color:var(--mx-text)!important;border-color:#b8c7d9!important}[data-testid="stNumberInput"] input{text-align:center;font-size:1.4rem;font-weight:950;color:var(--mx-navy)!important;min-height:52px}.score-stack+[data-testid="stNumberInput"],.score-stack~[data-testid="stNumberInput"]{max-width:480px;margin-left:auto;margin-right:auto}
    [data-baseweb="tab-list"]{gap:5px;background:#e7edf5;padding:5px;border-radius:14px;overflow-x:auto}[data-baseweb="tab"]{border-radius:10px;color:var(--mx-text);white-space:nowrap}[aria-selected="true"]{background:#fff!important;color:var(--mx-navy)!important}
    [data-testid="stDataFrame"]{background:#fff;border-radius:14px;overflow:hidden}.stAlert{border-radius:14px}
    .versus-badge{display:flex;align-items:center;justify-content:center;margin:auto;width:42px;height:42px;border-radius:50%;background:var(--mx-navy);color:#fff;font-weight:950;font-size:.82rem;letter-spacing:.04em}
    .score-label{text-align:center;color:#64748b;font-size:.68rem;font-weight:900;letter-spacing:.12em;margin:2px 0 4px}
    .score-sep-lower{padding-top:24px;font-size:1.8rem;color:var(--mx-pink)}
    .pro-table-wrap{width:100%;overflow-x:auto;padding-bottom:4px}.pro-table{width:100%;border-collapse:separate;border-spacing:0 6px;min-width:640px}.pro-table th{background:var(--mx-navy);color:#fff;padding:9px 10px;text-transform:uppercase;font-size:.72rem;letter-spacing:.04em;text-align:center}.pro-table th:first-child{border-radius:10px 0 0 10px}.pro-table th:last-child{border-radius:0 10px 10px 0}.pro-table td{padding:9px 10px;text-align:center;border-top:1px solid #dce4ed;border-bottom:1px solid #dce4ed;background:#fff}.pro-table tbody tr:nth-child(even) td{background:#f0f3f6}.pro-table td:first-child{border-left:4px solid var(--mx-green);border-radius:10px 0 0 10px}.pro-table td:last-child{border-right:1px solid #dce4ed;border-radius:0 10px 10px 0}.pro-table .podium-1 td{background:#2f741e!important;color:#fff}.pro-table .podium-2 td{background:#1763a4!important;color:#fff}.pro-table .podium-3 td{background:#4a86df!important;color:#fff}.pro-table .qualifier td:first-child{border-left:7px solid #11a8ff}.player-cell{display:flex;align-items:center;justify-content:flex-start;gap:10px;min-width:220px;text-align:left}.mini-logo{display:block;width:52px;height:52px;min-width:52px;object-fit:contain;object-position:center;margin:0}.player-cell b{display:block;text-align:left;white-space:nowrap}.table-legend{display:flex;gap:12px;flex-wrap:wrap;margin:4px 0 10px;color:#475467;font-size:.78rem}.legend-bar{display:inline-block;width:6px;height:15px;background:#11a8ff;border-radius:4px;vertical-align:middle;margin-right:5px}.logo-stage img{width:126px!important;height:126px!important;object-fit:contain;margin:auto}.match-card-note{text-align:center;color:#667085;font-size:.72rem;margin-top:5px}
    .pro-table td:nth-child(2){text-align:left}.pro-table th:nth-child(2){text-align:left}
    .match-teams-row{display:grid;grid-template-columns:minmax(0,1fr) 48px minmax(0,1fr);align-items:center;gap:8px;width:100%;margin:2px 0 8px}.match-team{display:flex;flex-direction:column;align-items:center;justify-content:flex-start;min-width:0}.match-team img{width:132px;height:132px;max-width:100%;object-fit:contain;object-position:center}.match-team-name{text-align:center;font-weight:900;font-size:.9rem;line-height:1.15;margin-top:5px;color:var(--mx-text);min-height:2.1em;display:flex;align-items:center;justify-content:center}.match-vs{display:flex;align-items:center;justify-content:center;width:42px;height:42px;margin:auto;border-radius:50%;background:var(--mx-navy);color:#fff;font-weight:950;font-size:.82rem}.match-score-row{display:grid;grid-template-columns:minmax(0,1fr) 48px minmax(0,1fr);align-items:end;gap:8px}.match-score-dash{text-align:center;padding-bottom:14px;font-size:1.8rem;font-weight:900;color:var(--mx-pink)}
    [class*="st-key-match_card_"] [data-testid="stHorizontalBlock"]{display:flex!important;flex-direction:row!important;flex-wrap:nowrap!important;align-items:flex-end!important;gap:.45rem!important}
    [class*="st-key-match_card_"] [data-testid="column"]{min-width:0!important;flex:1 1 0!important;width:auto!important}
    [class*="st-key-match_card_"] [data-testid="column"]:nth-child(2){flex:0 0 48px!important}
    .survivor-required{margin-top:16px;padding:14px;border:1px solid #b8e7ce;border-radius:16px;background:linear-gradient(90deg,#effcf5,#f8fffb)}
    .survivor-required h4{margin:0 0 3px;color:#075f43}.survivor-required p{margin:0;color:#476357;font-size:.82rem}

    .pred{display:inline-block;min-width:48px;padding:5px 8px;border-radius:9px;font-weight:900;white-space:nowrap}.pred.exact{background:#d9fbe8;color:#087443;border:1px solid #83ddb0}.pred.winner{background:#fff4c7;color:#785900;border:1px solid #e8cf62}.pred.wrong{background:#f3f4f6;color:#475467;border:1px solid #d0d5dd}.pred.pending{background:#eef2f6;color:#667085;border:1px solid #d7dee7}.official-score{font-size:.67rem;font-weight:700;color:#d9e5f2;white-space:nowrap}.prediction-legend{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0}.prediction-legend .pred{font-size:.74rem;min-width:0}.survivor-eliminated{margin-top:16px;padding:14px;border:1px solid #efb7b7;border-radius:16px;background:#fff3f3}.survivor-eliminated h4{margin:0 0 3px;color:#9b1c1c}.survivor-eliminated p{margin:0;color:#6b3131;font-size:.82rem}
    .pro-table{table-layout:auto;width:max-content;min-width:100%}
    .pro-table th,.pro-table td{white-space:nowrap}
    .pro-table .col-pos,.pro-table .col-pts,.pro-table .col-jg,.pro-table .col-je,.pro-table .col-jp,.pro-table .col-gf,.pro-table .col-gc,.pro-table .col-dif,.pro-table .col-vidas,.pro-table .col-elecciones{width:1%;min-width:48px;max-width:72px;padding-left:7px;padding-right:7px}
    .pro-table .col-pos{min-width:44px;max-width:55px}
    .pro-table .col-jugador,.pro-table .col-participante{min-width:220px}
    .pro-table .col-equipo{min-width:95px;max-width:135px}
    .pro-table .col-estado,.pro-table .col-survivor{min-width:90px;max-width:125px}
    .pro-table .col-capturados,.pro-table .col-total{min-width:78px;max-width:95px}
    @media(max-width:640px){.rank-table{min-width:530px}.rank-table .rank-participant{width:215px}.rank-table .rank-number{width:60px}.rank-table .club-cell img{width:46px;height:46px;min-width:46px}.player-cell{min-width:190px;gap:8px}.mini-logo{width:48px;height:48px;min-width:48px}.pro-table td{padding:8px 7px}.block-container{padding-left:.55rem;padding-right:.55rem}.hero{grid-template-columns:auto 1fr;padding:13px}.hero .league-logo{width:58px;height:46px}.hero .ball{display:none}.hero h1{font-size:1.35rem}.team-name{font-size:.75rem}.profile-card img{width:76px;height:76px}[data-testid="stNumberInput"] input{font-size:1.15rem}.stTabs [data-baseweb="tab"]{font-size:.73rem;padding-left:7px;padding-right:7px}}
    </style>
    """, unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def _data_uri(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def brand():
    league = _data_uri(ASSETS / "liga_mx_logo.png")
    ball = _data_uri(ASSETS / "liga_mx_balon.png")
    st.markdown(
        f'<div class="hero"><img class="league-logo" src="{league}" alt="Liga MX">'
        f'<div><h1>{APP_NAME}</h1><p>Apertura 2026 · La quiniela oficial del grupo</p><span class="tag">Quiniela · Survivor · Duelos</span></div>'
        f'<img class="ball" src="{ball}" alt="Balón Liga MX"></div>', unsafe_allow_html=True)


def result_type(home, away):
    if home is None or away is None:
        return None
    return "L" if home > away else "V" if home < away else "E"


def score_prediction(ph, pa, rh, ra):
    if rh is None or ra is None:
        return 0
    if (ph, pa) == (rh, ra):
        return 2
    return 1 if result_type(ph, pa) == result_type(rh, ra) else 0


def show_team(column, team, width=104):
    with column:
        st.image(str(team_logo(team)), width=width)
        st.markdown(f'<div class="team-name">{TEAM_SHORT.get(team, team)}</div>', unsafe_allow_html=True)


def match_score_card(match, previous=None, locked=False, prefix="p"):
    """Tarjeta horizontal: ambos escudos permanecen alineados en móvil y escritorio."""
    card_key = f"match_card_{prefix}_{match['id']}".replace(" ", "_").replace(".", "_")
    with st.container(border=True, key=card_key):
        kickoff = datetime.fromisoformat(match["kickoff"]).strftime("%d %b · %H:%M")
        home_logo = _data_uri(team_logo(match["home_team"]))
        away_logo = _data_uri(team_logo(match["away_team"]))
        home_name = TEAM_SHORT.get(match["home_team"], match["home_team"])
        away_name = TEAM_SHORT.get(match["away_team"], match["away_team"])
        st.markdown(
            f'<div class="match-title">{kickoff}</div>'
            f'<div class="match-teams-row">'
            f'<div class="match-team"><img src="{home_logo}" alt="{home_name}"><div class="match-team-name">{home_name}</div></div>'
            f'<div class="match-vs">VS</div>'
            f'<div class="match-team"><img src="{away_logo}" alt="{away_name}"><div class="match-team-name">{away_name}</div></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # En móvil los controles se apilan para evitar desplazamiento horizontal;
        # los escudos se mantienen alineados en la misma fila.
        st.markdown('<div class="score-stack">', unsafe_allow_html=True)
        st.markdown(f'<div class="score-team-label"><span>LOCAL</span><b>{home_name}</b></div>', unsafe_allow_html=True)
        home = st.number_input(
            f"Goles {match['home_team']}", min_value=0, max_value=20,
            value=int(previous["home_score"]) if previous else 0,
            key=f"{prefix}h{match['id']}", disabled=locked, label_visibility="collapsed",
        )
        st.markdown(f'<div class="score-team-label visitor"><span>VISITANTE</span><b>{away_name}</b></div>', unsafe_allow_html=True)
        away = st.number_input(
            f"Goles {match['away_team']}", min_value=0, max_value=20,
            value=int(previous["away_score"]) if previous else 0,
            key=f"{prefix}a{match['id']}", disabled=locked, label_visibility="collapsed",
        )
        st.markdown('</div>', unsafe_allow_html=True)
        return home, away


def standings():
    with conn() as c:
        users = c.execute("SELECT id,name,team FROM users WHERE is_admin=0").fetchall()
        rows = c.execute("""
            SELECT p.user_id,r.number jornada,p.home_score ph,p.away_score pa,
                   m.home_score rh,m.away_score ra
            FROM predictions p JOIN matches m ON m.id=p.match_id
            JOIN rounds r ON r.id=m.round_id
        """).fetchall()
    data = {u["id"]: {"USER_ID":u["id"], "JUGADOR":u["name"], "EQUIPO":TEAM_SHORT.get(u["team"],u["team"]), "TOTAL":0, "EXACTOS":0, "ACIERTOS":0} for u in users}
    for row in rows:
        points = score_prediction(row["ph"], row["pa"], row["rh"], row["ra"])
        column = f"J{row['jornada']}"
        data[row["user_id"]][column] = data[row["user_id"]].get(column, 0) + points
        data[row["user_id"]]["TOTAL"] += points
        if row["rh"] is not None and row["ra"] is not None:
            if (row["ph"], row["pa"]) == (row["rh"], row["ra"]): data[row["user_id"]]["EXACTOS"] += 1
            if result_type(row["ph"], row["pa"]) == result_type(row["rh"], row["ra"]): data[row["user_id"]]["ACIERTOS"] += 1
    df = pd.DataFrame(data.values()).fillna(0)
    # El Excel usa para Duelos: puntos, diferencia de goles y goles a favor.
    # Esos mismos valores sirven como desempate secundario en la tabla general.
    duel = duel_standings(add_position=False).set_index("JUGADOR") if len(data) else pd.DataFrame()
    df["DUELO_PTS"] = df["JUGADOR"].map(duel["PTS"] if not duel.empty else {}).fillna(0)
    df["DUELO_DIF"] = df["JUGADOR"].map(duel["DIF"] if not duel.empty else {}).fillna(0)
    df["DUELO_GF"] = df["JUGADOR"].map(duel["GF"] if not duel.empty else {}).fillna(0)
    df["DUELO_GC"] = df["JUGADOR"].map(duel["GC"] if not duel.empty else {}).fillna(0)
    journeys = sorted([x for x in df.columns if x.startswith("J") and x[1:].isdigit()], key=lambda x: int(x[1:]))
    df = df[["USER_ID","JUGADOR","EQUIPO","TOTAL","EXACTOS","ACIERTOS","DUELO_PTS","DUELO_DIF","DUELO_GF","DUELO_GC"] + journeys]
    df = df.sort_values(["TOTAL","DUELO_PTS","DUELO_DIF","DUELO_GF","EXACTOS","ACIERTOS","JUGADOR"], ascending=[False,False,False,False,False,False,True]).reset_index(drop=True)
    df.insert(0, "POS", range(1, len(df)+1))
    return df



def render_rank_table(df, title="Tabla general"):
    rows=[]
    for _,r in df.iterrows():
        team_full=next((team for _,name,team in PLAYERS if name==r["JUGADOR"]), r.get("EQUIPO",""))
        logo=_data_uri(team_logo(team_full)) if team_full in TEAM_SLUG else ""
        pos=int(r["POS"]); cls="top1" if pos==1 else "top2" if pos==2 else "top3" if pos==3 else ("alt" if pos%2==0 else "base")
        if pos <= 8: cls += " qualifier"
        points=int(r["TOTAL"] if "TOTAL" in r else r.get("PTS",0))
        gf=int(r.get("DUELO_GF", r.get("GF",0)))
        gc=int(r.get("DUELO_GC", r.get("GC",0)))
        dif=int(r.get("DUELO_DIF", r.get("DIF",gf-gc)))
        extra = f'<span class="tie-info">Exactos: {int(r.get("EXACTOS",0))}</span>' if "EXACTOS" in r else ""
        rows.append(f'<tr class="{cls}"><td class="rank-pos">{pos}</td><td class="rank-participant"><div class="club-cell"><img src="{logo}" alt="{team_full}"><span class="participant-text"><b class="participant-name">{r["JUGADOR"]}</b><small style="color:#667085;font-weight:600">{TEAM_SHORT.get(team_full,team_full)} {extra}</small></span></div></td><td class="pts rank-number">{points}</td><td class="rank-number">{gf}</td><td class="rank-number">{gc}</td><td class="rank-number">{dif:+d}</td></tr>')
    legend='<div class="table-legend"><span><i class="legend-bar"></i>Top 8: clasifica a elección de campeón</span><span>Desempate: puntos, DIF, GF y exactos</span></div>' if title.lower().startswith("tabla general") else ''
    html=f'<div class="table-title"><h3>{title}</h3><span class="table-pill">Actualizada en tiempo real</span></div>{legend}<div class="pro-table-wrap"><table class="rank-table"><thead><tr><th>Pos.</th><th>Participante</th><th>Puntos</th><th>GF</th><th>GC</th><th>DIF</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    st.markdown(html,unsafe_allow_html=True)


def render_pro_table(df, title, rank_col="POS", team_by_player=True, qualifier_top8=False):
    if df is None or df.empty:
        st.info("Todavía no hay información disponible."); return
    visible=df.copy()
    cols=list(visible.columns)
    def col_class(col):
        safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(col)).strip("-")
        return f"col-{safe}"
    headers=''.join(f'<th class="{col_class(c)}">{c}</th>' for c in cols)
    body=[]
    for idx,row in visible.iterrows():
        pos=int(row[rank_col]) if rank_col in row and str(row[rank_col]).replace('.0','').isdigit() else None
        cls = f'podium-{pos}' if pos in (1,2,3) else ''
        if qualifier_top8 and pos and pos<=8: cls += ' qualifier'
        cells=[]
        for col in cols:
            val=row[col]
            if col in ("JUGADOR","PARTICIPANTE") and team_by_player:
                team=next((t for _,n,t in PLAYERS if n==str(val)),None)
                if team:
                    logo=_data_uri(team_logo(team)); val=f'<div class="player-cell"><img class="mini-logo" src="{logo}" alt="{team}"><b>{val}</b></div>'
            if isinstance(val,float): val=f'{val:g}'
            cells.append(f'<td class="{col_class(col)}">{val}</td>')
        body.append(f'<tr class="{cls}">{"".join(cells)}</tr>')
    st.markdown(f'<div class="table-title"><h3>{title}</h3></div><div class="pro-table-wrap"><table class="pro-table"><thead><tr>{headers}</tr></thead><tbody>{"".join(body)}</tbody></table></div>',unsafe_allow_html=True)

def round_submission_status(round_id):
    with conn() as c:
        total_matches = c.execute("SELECT COUNT(*) FROM matches WHERE round_id=?", (round_id,)).fetchone()[0]
        round_info = c.execute("SELECT number,reveal_override FROM rounds WHERE id=?", (round_id,)).fetchone()
        round_number = round_info["number"]
        users = c.execute("SELECT id,name FROM users WHERE is_admin=0 ORDER BY name").fetchall()
        counts = {row["user_id"]: row["n"] for row in c.execute("""
            SELECT p.user_id, COUNT(*) n FROM predictions p
            JOIN matches m ON m.id=p.match_id WHERE m.round_id=? GROUP BY p.user_id
        """, (round_id,)).fetchall()}
        survivor_users = {row["user_id"] for row in c.execute(
            "SELECT user_id FROM survivor_picks WHERE round_id=?", (round_id,)
        ).fetchall()}
        override = bool(round_info and round_info["reveal_override"])
        alive_before = {u["id"]: _survivor_lives_before_round(c, u["id"], round_number) > 0 for u in users}
    rows = []
    for user in users:
        captured = counts.get(user["id"], 0)
        survivor_ok = user["id"] in survivor_users or not alive_before[user["id"]]
        survivor_label = "Eliminado" if not alive_before[user["id"]] else ("Listo" if user["id"] in survivor_users else "Pendiente")
        ready = captured == total_matches and survivor_ok
        rows.append({
            "JUGADOR": user["name"],
            "CAPTURADOS": captured,
            "TOTAL": total_matches,
            "SURVIVOR": survivor_label,
            "ESTADO": "Listo" if ready else "Pendiente",
        })
    complete = bool(total_matches and all(row["ESTADO"] == "Listo" for row in rows))
    return rows, complete, override


def _prediction_cell(prediction, match):
    if not prediction:
        return '<span class="pred pending">—</span>'
    ph, pa = prediction
    text = f"{ph}-{pa}"
    if match["home_score"] is None or match["away_score"] is None:
        return f'<span class="pred pending">{text}</span>'
    oh, oa = match["home_score"], match["away_score"]
    if (ph, pa) == (oh, oa):
        return f'<span class="pred exact" title="Marcador exacto">{text}</span>'
    pred_result = (ph > pa) - (ph < pa)
    official_result = (oh > oa) - (oh < oa)
    if pred_result == official_result:
        return f'<span class="pred winner" title="Acierto de resultado">{text}</span>'
    return f'<span class="pred wrong">{text}</span>'


def public_predictions(round_id):
    status, complete, override = round_submission_status(round_id)
    can_view = complete or override
    if not can_view:
        ready = sum(1 for row in status if row["ESTADO"] == "Listo")
        st.markdown(
            f'<div class="privacy-lock">🔒 Los pronósticos se mostrarán cuando los 18 participantes terminen, '
            f'o cuando el administrador autorice su publicación. Avance actual: <b>{ready}/18</b>.</div>',
            unsafe_allow_html=True,
        )
        return
    with conn() as c:
        matches = c.execute("SELECT * FROM matches WHERE round_id=? ORDER BY kickoff", (round_id,)).fetchall()
        round_row = c.execute("SELECT number FROM rounds WHERE id=?", (round_id,)).fetchone()
        users = c.execute("SELECT id,name FROM users WHERE is_admin=0 ORDER BY CASE WHEN name='Joan Santos' THEN 0 ELSE 1 END,name").fetchall()
        predictions = c.execute("""
            SELECT p.user_id,p.match_id,p.home_score,p.away_score FROM predictions p
            JOIN matches m ON m.id=p.match_id WHERE m.round_id=?
        """, (round_id,)).fetchall()
        survivor_round = {row["user_id"]: row["team"] for row in c.execute(
            "SELECT user_id,team FROM survivor_picks WHERE round_id=?", (round_id,)
        ).fetchall()}
        current_lives = {u["id"]: _survivor_lives_before_round(c, u["id"], None) for u in users}
    lookup = {(p["user_id"], p["match_id"]):(p["home_score"],p["away_score"]) for p in predictions}
    headers = []
    for match in matches:
        home = TEAM_SHORT.get(match["home_team"], match["home_team"])
        away = TEAM_SHORT.get(match["away_team"], match["away_team"])
        official = "Pendiente" if match["home_score"] is None else f'{match["home_score"]}-{match["away_score"]}'
        headers.append((match, f'{home} vs {away}<br><small class="official-score">Oficial: {official}</small>'))
    rows = []
    for user in users:
        row = {"PARTICIPANTE":user["name"]}
        for match, header in headers:
            row[header] = _prediction_cell(lookup.get((user["id"], match["id"])), match)
        pick = survivor_round.get(user["id"])
        lives = current_lives[user["id"]]
        if pick:
            state = "☠️ Eliminado" if lives <= 0 else f"❤️ {lives:g} vidas"
            row["SURVIVOR"] = f'<b>{TEAM_SHORT.get(pick,pick)}</b><br><small>{state}</small>'
        elif lives <= 0:
            row["SURVIVOR"] = '<b>Sin elección</b><br><small>☠️ Eliminado</small>'
        else:
            row["SURVIVOR"] = '<span class="pred pending">—</span>'
        rows.append(row)
    message = "✅ Todos terminaron. Los pronósticos ya son visibles para el grupo." if complete else "🔓 Publicación autorizada por el administrador. Los jugadores pendientes aparecen con guiones."
    st.markdown(f'<div class="privacy-open">{message}</div>', unsafe_allow_html=True)
    st.markdown('<div class="prediction-legend"><span class="pred exact">Exacto · 2 pts</span><span class="pred winner">Ganador/empate · 1 pt</span><span class="pred wrong">Sin puntos</span><span class="pred pending">Pendiente</span></div>', unsafe_allow_html=True)
    render_pro_table(pd.DataFrame(rows), f'Pronósticos del grupo · Jornada {round_row["number"]}', rank_col="", team_by_player=True)


def login():
    brand()
    st.subheader("Iniciar sesión")
    options = [(code, name, team) for code, name, team in PLAYERS] + [("ADMIN", "Administrador", "")]
    labels = [name for _, name, _ in options]

    # El selector queda fuera del formulario para que Streamlit actualice
    # inmediatamente el escudo y el equipo al cambiar de participante.
    selected_name = st.selectbox("Tu nombre", labels, key="login_selected_name")
    code, name, team = next(item for item in options if item[1] == selected_name)

    if code != "ADMIN":
        logo = team_logo(team)
        logo_data = base64.b64encode(logo.read_bytes()).decode("ascii")
        st.markdown(
            f'<div class="profile-card"><img src="data:image/png;base64,{logo_data}" alt="{team}">'
            f'<div><div class="name">{name}</div><div class="sub">Equipo de duelos: {TEAM_SHORT.get(team,team)}</div></div></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="profile-card"><div><div class="name">Administrador</div>'
            '<div class="sub">Panel de administración</div></div></div>',
            unsafe_allow_html=True,
        )

    with st.form("login_form", clear_on_submit=False):
        pin = st.text_input(
            "PIN personal",
            type="password",
            placeholder="Escribe tu PIN",
            key="login_pin",
        )
        submitted = st.form_submit_button("Entrar", type="primary", use_container_width=True)

    if submitted:
        if not pin:
            st.warning("Escribe tu PIN para continuar.")
        else:
            with conn() as c:
                user = c.execute(
                    "SELECT * FROM users WHERE code=? AND pin_hash=?",
                    (code, hash_pin(pin)),
                ).fetchone()
            if user:
                st.session_state.user = dict(user)
                st.session_state.pop("login_pin", None)
                st.rerun()
            else:
                st.error("El nombre o el PIN no son correctos.")

    st.caption("Cada participante tiene un PIN diferente. No lo compartas.")

def round_points(user_id, journey):
    with conn() as c:
        rows = c.execute("""
            SELECT p.home_score ph,p.away_score pa,m.home_score rh,m.away_score ra
            FROM predictions p JOIN matches m ON m.id=p.match_id JOIN rounds r ON r.id=m.round_id
            WHERE p.user_id=? AND r.number=?
        """, (user_id, journey)).fetchall()
    return sum(score_prediction(x["ph"],x["pa"],x["rh"],x["ra"]) for x in rows)


def round_complete(journey):
    with conn() as c:
        row = c.execute("""
            SELECT COUNT(*) n,SUM(CASE WHEN m.home_score IS NOT NULL AND m.away_score IS NOT NULL THEN 1 ELSE 0 END) d
            FROM matches m JOIN rounds r ON r.id=m.round_id WHERE r.number=?
        """, (journey,)).fetchone()
    return bool(row["n"] and row["n"] == row["d"])


def duels_round(journey):
    with conn() as c:
        users = {u["team"]:u for u in c.execute("SELECT id,name,team FROM users WHERE is_admin=0")}
        games = c.execute("""SELECT m.home_team,m.away_team FROM matches m JOIN rounds r ON r.id=m.round_id
                             WHERE r.number=? ORDER BY m.id""", (journey,)).fetchall()
    output = []
    complete = round_complete(journey)
    for game in games:
        home, away = users.get(game["home_team"]), users.get(game["away_team"])
        if not home or not away:
            continue
        hp, ap = round_points(home["id"],journey), round_points(away["id"],journey)
        if not complete: hd=ad=0; result="Pendiente"
        elif hp>ap: hd,ad,result=3,0,home["name"]
        elif hp<ap: hd,ad,result=0,3,away["name"]
        else: hd,ad,result=1,1,"Empate"
        output.append({"J":journey,"LOCAL":home["name"],"VISITANTE":away["name"],"QUINIELA":f"{hp}-{ap}","DUELO":f"{hd}-{ad}","RESULTADO":result})
    return output


def duel_standings(add_position=True):
    data = {name:{"JUGADOR":name,"EQUIPO":TEAM_SHORT.get(team,team),"PTS":0,"JG":0,"JE":0,"JP":0,"GF":0,"GC":0,"DIF":0} for _,name,team in PLAYERS}
    for journey in range(1,18):
        if not round_complete(journey): continue
        for row in duels_round(journey):
            a,b=row["LOCAL"],row["VISITANTE"]
            qa,qb=map(int,row["QUINIELA"].split("-")); pa,pb=map(int,row["DUELO"].split("-"))
            data[a]["PTS"]+=pa; data[b]["PTS"]+=pb
            data[a]["GF"]+=qa; data[a]["GC"]+=qb; data[b]["GF"]+=qb; data[b]["GC"]+=qa
            if pa==3: data[a]["JG"]+=1; data[b]["JP"]+=1
            elif pb==3: data[b]["JG"]+=1; data[a]["JP"]+=1
            else: data[a]["JE"]+=1; data[b]["JE"]+=1
    for row in data.values(): row["DIF"]=row["GF"]-row["GC"]
    df=pd.DataFrame(data.values()).sort_values(["PTS","DIF","GF","JG","JUGADOR"],ascending=[False,False,False,False,True]).reset_index(drop=True)
    if add_position: df.insert(0,"POS",range(1,len(df)+1))
    return df


def _survivor_lives_before_round(c, user_id, round_number=None):
    """Calcula las vidas usando únicamente jornadas anteriores a la indicada."""
    params = [user_id]
    round_filter = ""
    if round_number is not None:
        round_filter = " AND r.number < ?"
        params.append(round_number)
    rows = c.execute(f"""SELECT sp.team,m.home_team,m.away_team,m.home_score,m.away_score
                          FROM survivor_picks sp
                          JOIN rounds r ON r.id=sp.round_id
                          LEFT JOIN matches m ON m.round_id=r.id
                            AND (m.home_team=sp.team OR m.away_team=sp.team)
                          WHERE sp.user_id=? {round_filter}
                          ORDER BY r.number""", params).fetchall()
    lives = 3.0
    for row in rows:
        if row["home_score"] is None or row["away_score"] is None:
            continue
        gf = row["home_score"] if row["team"] == row["home_team"] else row["away_score"]
        ga = row["away_score"] if row["team"] == row["home_team"] else row["home_score"]
        if gf < ga:
            lives -= 1
        elif gf == ga:
            lives -= 0.5
    return max(0.0, lives)


def survivor_lives(user_id, round_number=None):
    with conn() as c:
        return _survivor_lives_before_round(c, user_id, round_number)


def survivor_status():
    with conn() as c:
        users=c.execute("SELECT id,name,team FROM users WHERE is_admin=0").fetchall()
        picks=c.execute("""SELECT sp.user_id,sp.team,m.home_team,m.away_team,m.home_score,m.away_score
                           FROM survivor_picks sp JOIN rounds r ON r.id=sp.round_id
                           LEFT JOIN matches m ON m.round_id=r.id AND (m.home_team=sp.team OR m.away_team=sp.team)""").fetchall()
    data={u["id"]:{"JUGADOR":u["name"],"EQUIPO":TEAM_SHORT.get(u["team"],u["team"]),"VIDAS":3.0,"ELECCIONES":0} for u in users}
    for row in picks:
        data[row["user_id"]]["ELECCIONES"]+=1
        if row["home_score"] is None: continue
        gf=row["home_score"] if row["team"]==row["home_team"] else row["away_score"]
        ga=row["away_score"] if row["team"]==row["home_team"] else row["home_score"]
        data[row["user_id"]]["VIDAS"]-=1 if gf<ga else (.5 if gf==ga else 0)
    df=pd.DataFrame(data.values()).sort_values(["VIDAS","ELECCIONES","JUGADOR"],ascending=[False,False,True]).reset_index(drop=True)
    df.insert(0,"POS",range(1,len(df)+1)); return df


def survivor_form_selection(user, round_row, locked, key_prefix="combined"):
    """Selector Survivor. Los eliminados pueden enviar pronósticos sin elegir equipo."""
    with conn() as c:
        used=[x["team"] for x in c.execute("SELECT team FROM survivor_picks WHERE user_id=?",(user["id"],))]
        old=c.execute("SELECT team FROM survivor_picks WHERE user_id=? AND round_id=?",(user["id"],round_row["id"])).fetchone()
        matches=c.execute("SELECT home_team,away_team FROM matches WHERE round_id=?",(round_row["id"],)).fetchall()
        lives_before = _survivor_lives_before_round(c, user["id"], round_row["number"])
    if lives_before <= 0 and not old:
        st.markdown('<div class="survivor-eliminated"><h4>☠️ Survivor finalizado</h4><p>Ya perdiste tus 3 vidas. Puedes seguir enviando tus pronósticos, pero ya no debes elegir equipo Survivor.</p></div>', unsafe_allow_html=True)
        return "__ELIMINATED__"
    teams=sorted({t for match in matches for t in (match["home_team"],match["away_team"])})
    available=[t for t in teams if t not in used or (old and t==old["team"])]
    st.markdown(f'<div class="survivor-required"><h4>Selección Survivor obligatoria</h4><p>Vidas disponibles antes de esta jornada: <b>{lives_before:g}</b>. No podrás repetir equipo.</p></div>', unsafe_allow_html=True)
    if not available:
        st.error("No tienes equipos disponibles para Survivor en esta jornada.")
        return None
    options=[None]+available
    default_index=options.index(old["team"]) if old and old["team"] in options else 0
    pick=st.selectbox(
        "Equipo Survivor", options, index=default_index,
        format_func=lambda x: "Selecciona un equipo" if x is None else TEAM_SHORT.get(x,x),
        disabled=locked, key=f"{key_prefix}_survivor_{round_row['id']}"
    )
    if pick:
        cols=st.columns([1,1,1])
        cols[1].image(str(team_logo(pick)),width=120)
        cols[1].markdown(f'<div class="team-name">{TEAM_SHORT.get(pick,pick)}</div>',unsafe_allow_html=True)
    return pick


def survivor_pick(user, round_row, locked):
    with conn() as c:
        used=[x["team"] for x in c.execute("SELECT team FROM survivor_picks WHERE user_id=?",(user["id"],))]
        old=c.execute("SELECT team FROM survivor_picks WHERE user_id=? AND round_id=?",(user["id"],round_row["id"])).fetchone()
        matches=c.execute("SELECT home_team,away_team FROM matches WHERE round_id=?",(round_row["id"],)).fetchall()
    teams=sorted({t for match in matches for t in (match["home_team"],match["away_team"])})
    available=[t for t in teams if t not in used or (old and t==old["team"])]
    st.caption("Gana: conserva vidas · Empata: pierde 0.5 · Pierde: pierde 1 · No se repiten equipos.")
    if not available:
        st.warning("No tienes equipos disponibles."); return
    pick=st.selectbox("Elige tu equipo Survivor",available,format_func=lambda x:TEAM_SHORT.get(x,x),disabled=locked)
    center=st.columns([1,1,1])[1]
    center.image(str(team_logo(pick)),width=132)
    center.markdown(f'<div class="team-name">{TEAM_SHORT.get(pick,pick)}</div>',unsafe_allow_html=True)
    if st.button("Guardar Survivor",type="primary",disabled=locked,use_container_width=True):
        try:
            with conn() as c:
                c.execute("""INSERT INTO survivor_picks(user_id,round_id,team,submitted_at) VALUES(?,?,?,?)
                             ON CONFLICT(user_id,round_id) DO UPDATE SET team=excluded.team,submitted_at=excluded.submitted_at""",
                          (user["id"],round_row["id"],pick,now_local().isoformat()))
            st.success("Elección guardada.")
        except sqlite3.IntegrityError:
            st.error("Ese equipo ya fue utilizado.")


def champion_order():
    return standings().head(8)[["POS","USER_ID","JUGADOR","TOTAL"]]


def champion_view(user=None, admin=False):
    with conn() as c:
        active=c.execute("SELECT value FROM settings WHERE key='champion_draft_active'").fetchone()["value"]=="1"
        eligible=[x["team"] for x in c.execute("SELECT team FROM champion_eligible")]
        picks=c.execute("""SELECT cp.pick_order,u.id user_id,u.name,cp.team FROM champion_picks cp
                           JOIN users u ON u.id=cp.user_id ORDER BY cp.pick_order""").fetchall()
    if picks:
        render_pro_table(pd.DataFrame([{"TURNO":x["pick_order"],"JUGADOR":x["name"],"CAMPEÓN":TEAM_SHORT.get(x["team"],x["team"])} for x in picks]), "Selecciones de campeón", rank_col="", team_by_player=True)
    if not active: st.info("La selección de campeón todavía no está activa."); return
    if len(picks)>=8: st.success("La selección terminó."); return
    order=champion_order(); picked_ids={x["user_id"] for x in picks}; picked_teams={x["team"] for x in picks}
    nxt=order[~order.USER_ID.isin(picked_ids)].iloc[0]
    st.write(f"Turno actual: **#{int(nxt.POS)} {nxt.JUGADOR}**")
    if admin: return
    if user["id"] not in set(order.USER_ID): st.warning("Solo participan los primeros 8."); return
    if user["id"] in picked_ids: st.success("Ya elegiste."); return
    if user["id"]!=int(nxt.USER_ID): st.warning("Todavía no es tu turno."); return
    available=[t for t in eligible if t not in picked_teams]
    if not available: st.error("El administrador debe cargar los equipos elegibles."); return
    team=st.selectbox("Equipo campeón",available,format_func=lambda x:TEAM_SHORT.get(x,x))
    if st.button("Confirmar campeón",type="primary",use_container_width=True):
        with conn() as c:
            c.execute("INSERT INTO champion_picks(user_id,team,pick_order,submitted_at) VALUES(?,?,?,?)",(user["id"],team,len(picks)+1,now_local().isoformat()))
        st.rerun()


def player_view(user):
    logo_data=_data_uri(team_logo(user["team"]))
    st.markdown(f'<div class="profile-card"><img src="{logo_data}" alt="{user["team"]}"><div><div class="name">{user["name"]}</div><div class="sub">Equipo de duelos: {TEAM_SHORT.get(user["team"],user["team"])}</div></div></div>',unsafe_allow_html=True)

    section = st.radio(
        "Sección",
        ["Pronósticos","Grupo","Survivor","Tabla","Duelos","Campeón"],
        horizontal=True,
        key="player_section",
        label_visibility="collapsed",
    )

    if section == "Pronósticos":
        with conn() as c: rounds=c.execute("SELECT * FROM rounds ORDER BY number").fetchall()
        choices={f"Jornada {r['number']}":r for r in rounds}
        round_row=choices[st.selectbox("Jornada",list(choices),key="player_round")]
        locked=not round_row["is_open"] or now_local()>datetime.fromisoformat(round_row["deadline"])
        st.markdown(f'<div class="section-note">{"🟢 Jornada abierta" if not locked else "🔒 Jornada cerrada"}</div>',unsafe_allow_html=True)
        with conn() as c:
            matches=c.execute("SELECT * FROM matches WHERE round_id=? ORDER BY kickoff",(round_row["id"],)).fetchall()
            previous={x["match_id"]:x for x in c.execute("SELECT * FROM predictions WHERE user_id=? AND match_id IN (SELECT id FROM matches WHERE round_id=?)",(user["id"],round_row["id"]))}
        with st.form(f"player_predictions_{user['id']}_{round_row['id']}", clear_on_submit=False):
            values=[]
            for match in matches:
                h,a=match_score_card(match,previous.get(match["id"]),locked,prefix=f"j{round_row['number']}_")
                values.append((match["id"],h,a))
            survivor_choice=survivor_form_selection(user,round_row,locked,key_prefix=f"j{round_row['number']}")
            submitted=st.form_submit_button("Enviar pronósticos y Survivor",type="primary",disabled=locked,use_container_width=True)
        if submitted:
            if survivor_choice is None:
                st.error("Debes seleccionar tu equipo Survivor antes de enviar la jornada.")
            else:
                try:
                    stamp=now_local().isoformat()
                    def save(c):
                        c.executemany("""INSERT INTO predictions(user_id,match_id,home_score,away_score,submitted_at) VALUES(?,?,?,?,?)
                            ON CONFLICT(user_id,match_id) DO UPDATE SET home_score=excluded.home_score,away_score=excluded.away_score,submitted_at=excluded.submitted_at""",
                            [(user["id"],mid,h,a,stamp) for mid,h,a in values])
                        if survivor_choice != "__ELIMINATED__":
                            c.execute("""INSERT INTO survivor_picks(user_id,round_id,team,submitted_at) VALUES(?,?,?,?)
                                ON CONFLICT(user_id,round_id) DO UPDATE SET team=excluded.team,submitted_at=excluded.submitted_at""",
                                (user["id"],round_row["id"],survivor_choice,stamp))
                    run_write(save)
                    st.success("Jornada enviada correctamente.")
                except sqlite3.IntegrityError:
                    st.error("Ese equipo Survivor ya fue utilizado en otra jornada.")
                except sqlite3.OperationalError:
                    st.error("La base estaba ocupada. Espera unos segundos y vuelve a presionar Guardar.")
    elif section == "Grupo":
        with conn() as c: rounds=c.execute("SELECT * FROM rounds ORDER BY number").fetchall()
        choices={f"Jornada {r['number']}":r for r in rounds}; round_row=choices[st.selectbox("Pronósticos del grupo",list(choices),key="group_round")]
        public_predictions(round_row["id"])
    elif section == "Survivor":
        st.info("La elección Survivor se envía junto con los pronósticos.")
        render_pro_table(survivor_status(), "Tabla Survivor")
    elif section == "Tabla":
        render_rank_table(standings(), "Tabla general de la quiniela")
    elif section == "Duelos":
        render_pro_table(duel_standings(), "Tabla general de duelos")
        journey=st.selectbox("Detalle de jornada",range(1,18),key="player_duel_round")
        render_pro_table(pd.DataFrame(duels_round(journey)), f"Duelos · Jornada {journey}", rank_col="", team_by_player=False)
    else:
        champion_view(user=user)


def admin_view():
    section = st.radio(
        "Panel",
        ["Resultados","Captura manual","Jornadas","Entregas","Participantes","Tabla","Duelos","Survivor","Campeón"],
        horizontal=True,
        key="admin_section",
        label_visibility="collapsed",
    )

    if section == "Resultados":
        st.subheader("Resultados oficiales")
        st.caption("Selecciona una jornada, captura todos sus partidos y guárdalos en una sola operación.")
        with conn() as c: rounds=c.execute("SELECT * FROM rounds ORDER BY number").fetchall()
        options={f"Jornada {r['number']}":r for r in rounds}
        label=st.selectbox("Jornada",list(options),key="admin_results_round")
        selected=options[label]
        with conn() as c:
            matches=c.execute("SELECT * FROM matches WHERE round_id=? ORDER BY kickoff",(selected["id"],)).fetchall()
        st.markdown(f'<div class="section-note"><b>Jornada {selected["number"]}</b> · {len(matches)} partidos</div>',unsafe_allow_html=True)
        with st.form(f"official_results_{selected['id']}",clear_on_submit=False):
            values=[]
            for m in matches:
                current={"home_score":m["home_score"] if m["home_score"] is not None else 0,"away_score":m["away_score"] if m["away_score"] is not None else 0}
                h,a=match_score_card(m,current,False,prefix=f"official_{selected['id']}_")
                values.append((h,a,m["id"]))
            save=st.form_submit_button(f"Guardar Jornada {selected['number']}",type="primary",use_container_width=True)
        if save:
            try:
                run_write(lambda c: c.executemany("UPDATE matches SET home_score=?,away_score=? WHERE id=?",values))
                st.success(f"Resultados de la Jornada {selected['number']} guardados correctamente.")
            except sqlite3.OperationalError:
                st.error("La base estaba ocupada. Espera unos segundos y vuelve a guardar.")

    elif section == "Captura manual":
        st.subheader("Captura manual de pronósticos")
        st.caption("Primero carga participante y jornada. Las casillas no recargarán la aplicación mientras escribes.")
        with conn() as c:
            rounds=c.execute("SELECT * FROM rounds ORDER BY number").fetchall()
            players=c.execute("SELECT * FROM users WHERE is_admin=0 ORDER BY CASE WHEN name='Joan Santos' THEN 0 ELSE 1 END,name").fetchall()
        ro={f"Jornada {r['number']}":r for r in rounds}
        po={f"{u['name']} · {TEAM_SHORT.get(u['team'],u['team'])}":u for u in players}
        with st.form("manual_loader",clear_on_submit=False):
            c1,c2=st.columns(2)
            rlabel=c1.selectbox("Jornada a capturar",list(ro),key="manual_round_picker")
            plabel=c2.selectbox("Participante",list(po),key="manual_player_picker")
            load=st.form_submit_button("Cargar captura",use_container_width=True)
        if load or "manual_loaded" not in st.session_state:
            st.session_state.manual_loaded=(rlabel,plabel)
        rlabel,plabel=st.session_state.manual_loaded
        round_row,selected_user=ro[rlabel],po[plabel]
        st.markdown(f'<div class="section-note">Capturando para <b>{selected_user["name"]}</b> · Jornada {round_row["number"]}</div>',unsafe_allow_html=True)
        with conn() as c:
            matches=c.execute("SELECT * FROM matches WHERE round_id=? ORDER BY kickoff",(round_row["id"],)).fetchall()
            previous={x["match_id"]:x for x in c.execute("SELECT * FROM predictions WHERE user_id=? AND match_id IN (SELECT id FROM matches WHERE round_id=?)",(selected_user["id"],round_row["id"]))}
        with st.form(f"manual_capture_{selected_user['id']}_{round_row['id']}",clear_on_submit=False):
            values=[]
            for m in matches:
                h,a=match_score_card(m,previous.get(m["id"]),False,prefix=f"manual_{selected_user['id']}_{round_row['id']}_")
                values.append((m["id"],h,a))
            survivor_choice=survivor_form_selection(selected_user,round_row,False,key_prefix=f"manual_{selected_user['id']}_{round_row['id']}")
            save=st.form_submit_button("Guardar captura manual",type="primary",use_container_width=True)
        if save:
            try:
                stamp=now_local().isoformat()
                def save_manual(c):
                    c.executemany("""INSERT INTO predictions(user_id,match_id,home_score,away_score,submitted_at) VALUES(?,?,?,?,?)
                        ON CONFLICT(user_id,match_id) DO UPDATE SET home_score=excluded.home_score,away_score=excluded.away_score,submitted_at=excluded.submitted_at""",
                        [(selected_user["id"],mid,h,a,stamp) for mid,h,a in values])
                    if survivor_choice and survivor_choice != "__ELIMINATED__":
                        c.execute("""INSERT INTO survivor_picks(user_id,round_id,team,submitted_at) VALUES(?,?,?,?)
                            ON CONFLICT(user_id,round_id) DO UPDATE SET team=excluded.team,submitted_at=excluded.submitted_at""",
                            (selected_user["id"],round_row["id"],survivor_choice,stamp))
                run_write(save_manual)
                st.success(f"Captura de {selected_user['name']} guardada correctamente.")
            except sqlite3.IntegrityError:
                st.error("Ese participante ya utilizó ese equipo Survivor.")
            except sqlite3.OperationalError:
                st.error("La base estaba ocupada. Espera unos segundos y vuelve a guardar.")

    elif section == "Jornadas":
        with conn() as c: rounds=c.execute("SELECT * FROM rounds ORDER BY number").fetchall()
        for r in rounds:
            a,b=st.columns([4,1]); a.write(f"**Jornada {r['number']}** · {'ABIERTA' if r['is_open'] else 'CERRADA'} · límite {r['deadline']}")
            if b.button("Cerrar" if r["is_open"] else "Activar",key=f"r{r['id']}"):
                run_write(lambda c,rid=r["id"]: c.execute("UPDATE rounds SET is_open=1-is_open WHERE id=?",(rid,)))
                st.rerun()
    elif section == "Entregas":
        with conn() as c: rounds=c.execute("SELECT * FROM rounds ORDER BY number").fetchall()
        choices={f"Jornada {r['number']}":r for r in rounds}; r=choices[st.selectbox("Revisar entregas",list(choices),key="delivery_round")]
        status,complete,override=round_submission_status(r["id"]); ready=sum(x["ESTADO"]=="Listo" for x in status)
        a,b=st.columns(2); a.metric("Entregaron",f"{ready}/18"); b.metric("Publicación","Visible" if complete or override else "Bloqueada")
        if not complete and st.button("Revocar publicación anticipada" if override else "Autorizar publicación aunque falten jugadores",use_container_width=True):
            run_write(lambda c: c.execute("UPDATE rounds SET reveal_override=? WHERE id=?",(0 if override else 1,r["id"])))
            st.rerun()
        render_pro_table(pd.DataFrame(status),"Control de entregas",rank_col="",team_by_player=True)
        public_predictions(r["id"])
    elif section == "Participantes":
        accesses=pd.DataFrame([(code,name,TEAM_SHORT.get(team,team),PLAYER_PINS[code]) for code,name,team in PLAYERS],columns=["CLAVE","PARTICIPANTE","EQUIPO","PIN"])
        st.warning("Comparte cada PIN de forma privada.")
        render_pro_table(accesses,"Accesos privados",rank_col="",team_by_player=True)
        st.download_button("Descargar accesos",accesses.to_csv(index=False).encode("utf-8-sig"),"accesos_privados.csv")
    elif section == "Tabla":
        render_rank_table(standings(),"Tabla general de la quiniela")
    elif section == "Duelos":
        render_pro_table(duel_standings(),"Tabla general de duelos")
        journey=st.selectbox("Jornada de duelos",range(1,18),key="admin_duel_round")
        render_pro_table(pd.DataFrame(duels_round(journey)),f"Duelos · Jornada {journey}",rank_col="",team_by_player=False)
    elif section == "Survivor":
        render_pro_table(survivor_status(),"Tabla Survivor")
    else:
        render_pro_table(champion_order().drop(columns=["USER_ID"]),"Orden de elección",qualifier_top8=True)
        with conn() as c:
            current=[x["team"] for x in c.execute("SELECT team FROM champion_eligible")]
            active=c.execute("SELECT value FROM settings WHERE key='champion_draft_active'").fetchone()["value"]=="1"
        eligible=st.multiselect("Equipos elegibles",ALL_TEAMS,default=current)
        if st.button("Guardar equipos elegibles"):
            def save_eligible(c):
                c.execute("DELETE FROM champion_eligible"); c.executemany("INSERT INTO champion_eligible VALUES(?)",[(x,) for x in eligible])
            run_write(save_eligible); st.success("Guardados.")
        if st.button("Desactivar selección" if active else "Activar selección",type="primary"):
            run_write(lambda c: c.execute("UPDATE settings SET value=? WHERE key='champion_draft_active'",("0" if active else "1",)))
            st.rerun()
        champion_view(admin=True)
        if st.button("Reiniciar selección de campeón"):
            run_write(lambda c: c.execute("DELETE FROM champion_picks")); st.rerun()


@st.cache_resource(show_spinner=False)
def ensure_database_ready():
    init_db()
    return True


def logout():
    selected = st.session_state.get("login_selected_name", "Joan Santos")
    st.session_state.clear()
    st.session_state["login_selected_name"] = selected
    st.rerun()


def main():
    st.set_page_config(page_title=APP_NAME,page_icon=str(ASSETS / "liga_mx_balon.png"),layout="wide",initial_sidebar_state="collapsed")
    inject_style()
    ensure_database_ready()
    if "user" not in st.session_state:
        login(); return
    brand(); user=st.session_state.user
    left,right=st.columns([5,1]); left.caption("Apertura 2026")
    if right.button("Salir",use_container_width=True,key="logout_button"):
        logout()
    admin_view() if user["is_admin"] else player_view(user)


if __name__ == "__main__":
    main()
