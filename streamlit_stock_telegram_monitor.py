import json
import streamlit as st
import altair as alt
import yfinance as yf
import pandas as pd
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

st.set_page_config(page_title="交易計劃｜每分鐘 Telegram 監控", layout="wide")

# 新版 Streamlit 用 width="stretch"；舊版沿用 use_container_width
try:
    _v = tuple(int(x) for x in st.__version__.split(".")[:2])
except Exception:
    _v = (0, 0)
STRETCH = {"width": "stretch"} if _v >= (1, 52) else {"use_container_width": True}

# ============================================================
# 預設交易計劃（首次載入用，之後可在頁面上直接編輯）
# ============================================================
DEFAULT_PLANS = {
    "META": {"direction": "LONG", "entry": (710, 725), "stop": 665, "targets": [739, 793], "rr": "0.4～1.8"},
    "AMD":  {"direction": "LONG", "entry": (600, 612), "stop": 554, "targets": [639, 652], "rr": "0.6～1.0"},
    "NVDA": {"direction": "LONG", "entry": (225, 229), "stop": 204, "targets": [236], "rr": "0.3～0.5"},
    "AAPL": {"direction": "LONG", "entry": (330, 334), "stop": 279, "targets": [344], "rr": "0.2～0.3"},
    "MSFT": {"direction": "LONG", "entry": (505, 513), "stop": 475, "targets": [537, 550], "rr": "0.8～1.4"},
    "XOM":  {"direction": "LONG", "entry": (162.7, 165), "stop": 158.4, "targets": [169.5, 174.1], "rr": "0.9～2.0"},
    "TSLA": {"direction": "LONG", "entry": (345, 355), "stop": (322, 328), "targets": [367.7, 382.8], "rr": "0.7～1.5"},
    "INTC": {"direction": "LONG", "entry": (116, 121), "stop": (74, 75), "targets": [128.7, 142.4], "rr": "低"},
}

COLS = ["啟用", "代碼", "方向", "入場下限", "入場上限", "止損", "止損上限(可空)",
        "目標1", "目標2", "目標3", "R:R 備註"]
MAX_TARGETS = 3

# ============================================================
# Telegram
# Streamlit Cloud: Settings -> Secrets
#
# [telegram]
# bot_token = "123456:ABC..."
# chat_id = "123456789"
# ============================================================
def get_telegram_config():
    try:
        token = st.secrets["telegram"]["bot_token"]
        chat_id = st.secrets["telegram"]["chat_id"]
        return token, str(chat_id)
    except Exception:
        return None, None

def send_telegram(message: str) -> tuple[bool, str]:
    token, chat_id = get_telegram_config()
    if not token or not chat_id:
        return False, "未設定 Telegram Secrets"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        if r.ok:
            return True, "Telegram 已發送"
        return False, f"Telegram HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"Telegram error: {e}"

# ============================================================
# 計劃 <-> 表格 轉換
# ============================================================
def plans_to_df(plans: dict) -> pd.DataFrame:
    rows = []
    for sym, p in plans.items():
        stop = p["stop"]
        if isinstance(stop, (tuple, list)):
            s_lo, s_hi = float(stop[0]), float(stop[1])
        else:
            s_lo, s_hi = float(stop), None
        tg = list(p["targets"]) + [None] * MAX_TARGETS
        rows.append({
            "啟用": True,
            "代碼": sym,
            "方向": "做多" if p["direction"] == "LONG" else "做空",
            "入場下限": float(p["entry"][0]),
            "入場上限": float(p["entry"][1]),
            "止損": s_lo,
            "止損上限(可空)": s_hi,
            "目標1": tg[0], "目標2": tg[1], "目標3": tg[2],
            "R:R 備註": p.get("rr", ""),
        })
    return pd.DataFrame(rows, columns=COLS)

def _num(x):
    return None if x is None or pd.isna(x) else float(x)

def df_to_plans(df: pd.DataFrame):
    """回傳 (plans, errors, warnings)。無效列會被略過並提示。"""
    plans, errors, warnings = {}, [], []
    for i, r in df.iterrows():
        sym = str(r.get("代碼") or "").strip().upper()
        if not sym or sym == "NAN":
            continue
        if not bool(r.get("啟用", True)):
            continue
        if sym in plans:
            errors.append(f"{sym}：代碼重複，已略過後面一列")
            continue

        lo, hi = _num(r["入場下限"]), _num(r["入場上限"])
        s1, s2 = _num(r["止損"]), _num(r["止損上限(可空)"])
        if lo is None or hi is None:
            errors.append(f"{sym}：入場區間未填完整，已略過")
            continue
        if s1 is None:
            errors.append(f"{sym}：未填止損，已略過")
            continue
        lo, hi = sorted((lo, hi))
        stop = s1 if (s2 is None or s2 == s1) else tuple(sorted((s1, s2)))

        targets = [t for t in (_num(r[f"目標{k}"]) for k in range(1, MAX_TARGETS + 1)) if t is not None]
        if not targets:
            errors.append(f"{sym}：至少需要一個目標價，已略過")
            continue

        direction = "LONG" if r["方向"] == "做多" else "SHORT"
        stop_lo, stop_hi = (stop if isinstance(stop, tuple) else (stop, stop))
        if direction == "LONG" and stop_hi >= lo:
            warnings.append(f"{sym}：做多止損 ({stop_hi:.2f}) 不低於入場下限 ({lo:.2f})，請確認")
        if direction == "SHORT" and stop_lo <= hi:
            warnings.append(f"{sym}：做空止損 ({stop_lo:.2f}) 不高於入場上限 ({hi:.2f})，請確認")

        plans[sym] = {
            "direction": direction,
            "entry": (lo, hi),
            "stop": stop,
            "targets": targets,
            "rr": str(r.get("R:R 備註") or ""),
        }
    return plans, errors, warnings

# ============================================================
# Session state
# ============================================================
if "triggered" not in st.session_state:
    st.session_state.triggered = set()
if "positions" not in st.session_state:
    st.session_state.positions = {}
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = None
if "plan_df" not in st.session_state:
    st.session_state.plan_df = plans_to_df(DEFAULT_PLANS)
if "editor_ver" not in st.session_state:
    st.session_state.editor_ver = 0
if "plans" not in st.session_state:
    st.session_state.plans = dict(DEFAULT_PLANS)

ET = ZoneInfo("America/New_York")
SESSIONS = ["盤前", "盤中", "盤后", "夜盤"]
FRESH_MINUTES = 5  # 超過此分鐘數沒有新成交，視為資料過期

def now_et():
    return datetime.now(ET)

def et_date():
    return now_et().strftime("%Y-%m-%d")

def get_session(t=None):
    """依美東時間判斷目前時段（未考慮美國假期）。
    盤前 04:00-09:30｜盤中 09:30-16:00｜盤后 16:00-20:00｜
    夜盤 20:00-04:00（週日晚 20:00 起至週五 04:00 止）｜其餘為休市。
    """
    t = t or now_et()
    wd, hm = t.weekday(), t.hour * 60 + t.minute  # Mon=0 ... Sun=6
    if 4 * 60 <= hm < 9 * 60 + 30 and wd < 5:
        return "盤前"
    if 9 * 60 + 30 <= hm < 16 * 60 and wd < 5:
        return "盤中"
    if 16 * 60 <= hm < 20 * 60 and wd < 5:
        return "盤后"
    # 夜盤：週日~週四 20:00 之後，或週一~週五 04:00 之前
    if hm >= 20 * 60 and wd in (6, 0, 1, 2, 3):
        return "夜盤"
    if hm < 4 * 60 and wd in (0, 1, 2, 3, 4):
        return "夜盤"
    return "休市"

def data_age_minutes(data_time):
    """資料時間距今幾分鐘。"""
    if data_time is None:
        return None
    ts = pd.Timestamp(data_time)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return (pd.Timestamp.now(tz="UTC") - ts.tz_convert("UTC")).total_seconds() / 60

def today_key(symbol, plan):
    # 每天（美東日期）重新允許一次通知；計劃入場區改了也視為新計劃，可再次通知
    lo, hi = plan["entry"]
    return f"{et_date()}_{symbol}_{lo}_{hi}_{plan['direction']}"

def price_in_entry(price, entry):
    lo, hi = entry
    return lo <= price <= hi

def format_stop(stop):
    if isinstance(stop, tuple):
        return f"{stop[0]:.2f}–{stop[1]:.2f}"
    return f"{stop:.2f}"

def calculate_rr(entry_price, stop, targets):
    stop_price = sum(stop) / 2 if isinstance(stop, tuple) else float(stop)
    risk = abs(entry_price - stop_price)
    if risk <= 0:
        return []
    return [round(abs(t - entry_price) / risk, 2) for t in targets]

# ============================================================
# 取得 1 分鐘價格
# ============================================================
@st.cache_data(ttl=45, show_spinner=False)
def get_history(symbol):
    """回傳 1 分鐘 K 線 (Close, Volume)，含盤前盤后；失敗回傳 None。"""
    # prepost=True：包含盤前 (04:00–09:30 ET) 與盤后 (16:00–20:00 ET)
    # period="5d"：週末／夜間也能取得最後一筆成交價
    df = None
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="1m",
                                       prepost=True, auto_adjust=False)
    except Exception:
        df = None
    if df is None or df.empty:
        try:
            df = yf.download(symbol, period="5d", interval="1m",
                             auto_adjust=False, progress=False, prepost=True)
        except Exception:
            return None
    if df is None or df.empty:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        close = df["Close"].iloc[:, 0]
        volume = df["Volume"].iloc[:, 0]
    else:
        close = df["Close"]
        volume = df["Volume"]

    out = pd.DataFrame({
        "Close": pd.to_numeric(close, errors="coerce"),
        "Volume": pd.to_numeric(volume, errors="coerce"),
    }).dropna(subset=["Close"])
    out["Volume"] = out["Volume"].fillna(0)
    return out if not out.empty else None

# ============================================================
# Telegram 訊息
# ============================================================
def build_alert(symbol, price, plan, timestamp, session=""):
    entry_lo, entry_hi = plan["entry"]
    stop = format_stop(plan["stop"])
    targets = " / ".join(f"{x:.2f}" for x in plan["targets"])
    rrs = calculate_rr(price, plan["stop"], plan["targets"])
    rr_text = " / ".join(f"{x:.2f}R" for x in rrs) if rrs else plan["rr"]
    dir_text = "🟢 做多" if plan["direction"] == "LONG" else "🔴 做空"

    return (
        f"🚨【入場區觸發】{symbol}\n\n"
        f"方向：{dir_text}\n"
        f"現價：${price:.2f}（{session}）\n"
        f"入場區：${entry_lo:.2f}–${entry_hi:.2f}\n"
        f"止損：${stop}\n"
        f"目標：${targets}\n"
        f"即時計算 R:R：{rr_text}\n\n"
        f"⏰ {timestamp}\n"
        f"⚠️ 價格已進入預設入場區，請自行確認盤勢後交易。"
    )

# ============================================================
# UI
# ============================================================
st.title("📡 股票交易計劃｜每分鐘價格監控 + Telegram")
st.caption(
    "監控邏輯：當股價進入你設定的參考入場區間，就發送一次 Telegram。"
    " 每日每隻股票最多通知一次，避免價格在區間內來回造成洗版。"
)

with st.sidebar:
    st.header("⚙️ 監控設定")
    auto_refresh = st.checkbox("自動每 60 秒更新（全天候）", value=True)

    alert_sessions = st.multiselect(
        "允許發送 Telegram 的時段",
        SESSIONS,
        default=SESSIONS,
        help="只有在選中的時段、且價格資料是新鮮的情況下才會發送通知。",
    )
    allow_stale = st.checkbox(
        f"允許用過期價格觸發（> {FRESH_MINUTES} 分鐘無新成交）",
        value=False,
        help="預設關閉，避免夜間／休市時用舊價格誤發通知。",
    )
    show_charts = st.checkbox("顯示當日走勢圖", value=True)
    st.caption(f"🕒 目前美東時間：{now_et().strftime('%Y-%m-%d %H:%M')}｜時段：**{get_session()}**")

    if st.button("🧪 發送 Telegram 測試"):
        ok, msg = send_telegram("✅ Streamlit 股票監控 Telegram 測試成功")
        (st.success if ok else st.error)(msg)

    if st.button("🔄 重置今日通知與持倉"):
        today = et_date()
        st.session_state.triggered = {x for x in st.session_state.triggered if not x.startswith(today)}
        st.session_state.positions = {k: v for k, v in st.session_state.positions.items()
                                      if not k.startswith(today)}
        st.success("今日通知與持倉追蹤已重置。")

# ------------------------------------------------------------
# 可編輯交易計劃
# ------------------------------------------------------------
st.subheader("✏️ 編輯交易計劃")
st.caption(
    "直接在表格內修改；底部可新增／刪除列。取消勾選「啟用」即暫停該股票監控。"
    " 「止損上限」留空代表單一止損價；填寫則為止損區間。"
)

edited_df = st.data_editor(
    st.session_state.plan_df,
    key=f"plan_editor_{st.session_state.editor_ver}",
    num_rows="dynamic",
    **STRETCH,
    hide_index=True,
    column_config={
        "啟用": st.column_config.CheckboxColumn("啟用", default=True),
        "代碼": st.column_config.TextColumn("代碼", help="美股代碼，例如 TSLA", required=True),
        "方向": st.column_config.SelectboxColumn("方向", options=["做多", "做空"], default="做多", required=True),
        "入場下限": st.column_config.NumberColumn("入場下限", format="%.2f", min_value=0.0, step=0.1),
        "入場上限": st.column_config.NumberColumn("入場上限", format="%.2f", min_value=0.0, step=0.1),
        "止損": st.column_config.NumberColumn("止損", format="%.2f", min_value=0.0, step=0.1),
        "止損上限(可空)": st.column_config.NumberColumn("止損上限(可空)", format="%.2f", min_value=0.0, step=0.1),
        "目標1": st.column_config.NumberColumn("目標1", format="%.2f", min_value=0.0, step=0.1),
        "目標2": st.column_config.NumberColumn("目標2", format="%.2f", min_value=0.0, step=0.1),
        "目標3": st.column_config.NumberColumn("目標3", format="%.2f", min_value=0.0, step=0.1),
        "R:R 備註": st.column_config.TextColumn("R:R 備註"),
    },
)

plans, plan_errors, plan_warnings = df_to_plans(edited_df)
st.session_state.plans = plans  # 監控一律讀取最新編輯結果

for e in plan_errors:
    st.error(e)
for w in plan_warnings:
    st.warning(w)

c1, c2, c3 = st.columns([1, 1, 2])
with c1:
    st.download_button(
        "💾 匯出計劃 (JSON)",
        data=json.dumps(edited_df.where(edited_df.notna(), None).to_dict(orient="records"),
                        ensure_ascii=False, indent=2),
        file_name="trading_plans.json",
        mime="application/json",
    )
with c2:
    if st.button("♻️ 還原預設計劃"):
        st.session_state.plan_df = plans_to_df(DEFAULT_PLANS)
        st.session_state.editor_ver += 1
        st.rerun()
with c3:
    uploaded = st.file_uploader("匯入計劃 (JSON)", type="json", label_visibility="collapsed")
    if uploaded is not None and st.button("📥 套用匯入"):
        try:
            data = json.load(uploaded)
            new_df = pd.DataFrame(data)
            for c in COLS:
                if c not in new_df.columns:
                    new_df[c] = None
            st.session_state.plan_df = new_df[COLS]
            st.session_state.editor_ver += 1
            st.rerun()
        except Exception as e:
            st.error(f"匯入失敗：{e}")

st.info(
    "提示：Streamlit Cloud 重新整理頁面或重新部署後，編輯內容會回到預設值。"
    " 修改完請按「匯出計劃」保存，下次再用「匯入計劃」載入。"
)

# ------------------------------------------------------------
# 計劃總覽
# ------------------------------------------------------------
st.subheader("📋 交易計劃")

plan_rows = []
for symbol, p in plans.items():
    plan_rows.append({
        "代碼": symbol,
        "方向": "🟢 做多" if p["direction"] == "LONG" else "🔴 做空",
        "參考入場": f"{p['entry'][0]:.2f}–{p['entry'][1]:.2f}",
        "止損": format_stop(p["stop"]),
        "目標": " / ".join(f"{x:.2f}" for x in p["targets"]),
        "R:R 約": p["rr"],
    })
st.dataframe(pd.DataFrame(plan_rows), **STRETCH, hide_index=True)

st.divider()
st.subheader("📡 即時監控")

# ============================================================
# 持倉追蹤（入場後追蹤止損／目標與 R 倍數）
# ============================================================
def sort_targets(plan):
    # 由近到遠：多單由小到大，空單由大到小
    return sorted(plan["targets"], reverse=(plan["direction"] == "SHORT"))

def stop_trigger_level(plan):
    stop = plan["stop"]
    if isinstance(stop, tuple):
        return max(stop) if plan["direction"] == "LONG" else min(stop)
    return float(stop)

def r_multiple(plan, entry_price, price):
    stop = plan["stop"]
    mid = sum(stop) / 2 if isinstance(stop, tuple) else float(stop)
    risk = abs(entry_price - mid)
    if risk <= 0:
        return 0.0
    move = price - entry_price if plan["direction"] == "LONG" else entry_price - price
    return move / risk

def update_position(plan, pos, price):
    """依最新價更新持倉狀態；新事件（止損／目標）寫入 pos['events']。"""
    if pos["closed"]:
        return
    r = r_multiple(plan, pos["entry"], price)
    pos["last_r"] = r
    pos["max_r"] = max(pos["max_r"], r)

    level = stop_trigger_level(plan)
    stopped = price <= level if plan["direction"] == "LONG" else price >= level
    if stopped:
        pos.update(stopped=True, closed=True, final_r=r)
        pos["events"].append({"kind": "STOP", "price": price, "r": r})
        return

    for i, t in enumerate(sort_targets(plan)):
        kind = f"T{i + 1}"
        reached = price >= t if plan["direction"] == "LONG" else price <= t
        if reached and kind not in pos["hit"]:
            pos["hit"].append(kind)
            pos["events"].append({"kind": kind, "price": price, "r": r})
    if len(pos["hit"]) >= len(plan["targets"]):
        pos.update(closed=True, final_r=r)

def position_text(plan, pos):
    if not pos:
        return "—"
    n, m = len(pos["hit"]), len(plan["targets"])
    if pos["stopped"]:
        return f"🛑 {pos['final_r']:+.2f}R"
    if pos["closed"]:
        return f"🏁 {pos['final_r']:+.2f}R（目標全達）"
    return f"{pos['last_r']:+.2f}R（目標 {n}/{m}｜最高 {pos['max_r']:+.2f}R）"

def build_event_alert(symbol, ev, plan, pos, timestamp, session):
    dir_text = "🟢 做多" if plan["direction"] == "LONG" else "🔴 做空"
    if ev["kind"] == "STOP":
        head = f"🛑【止損觸發】{symbol}"
    else:
        head = f"🎯【目標 {ev['kind'][1:]} 達成】{symbol}"
    targets = " / ".join(f"{x:.2f}" for x in sort_targets(plan))
    return (
        f"{head}\n\n"
        f"方向：{dir_text}\n"
        f"現價：${ev['price']:.2f}（{session}）\n"
        f"入場參考價：${pos['entry']:.2f}\n"
        f"止損：${format_stop(plan['stop'])}\n"
        f"目標：${targets}\n"
        f"目前損益：{ev['r']:+.2f}R\n\n"
        f"⏰ {timestamp}\n"
        f"⚠️ 價格提醒，不會自動下單。"
    )

# ============================================================
# 時段倒數
# ============================================================
def fmt_delta(td):
    mins = max(int(td.total_seconds() // 60), 0)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    return (f"{d}天 " if d else "") + f"{h}小時 {m:02d}分"

def next_session_change(t):
    cur = get_session(t)
    for off in range(0, 9):
        day = (t + timedelta(days=off)).date()
        for hh, mm in [(4, 0), (9, 30), (16, 0), (20, 0)]:
            cand = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)
            if cand > t and get_session(cand) != cur:
                return get_session(cand), cand
    return None, None

def next_regular_open(t):
    for off in range(0, 9):
        day = (t + timedelta(days=off)).date()
        cand = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
        if cand > t and cand.weekday() < 5:
            return cand
    return None

def render_clock():
    t = now_et()
    cur = get_session(t)
    nxt_name, nxt_t = next_session_change(t)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("美東時間", t.strftime("%m/%d %H:%M"))
    c2.metric("目前時段", cur)
    if nxt_t:
        c3.metric(f"距離「{nxt_name}」", fmt_delta(nxt_t - t))
    if cur == "盤中":
        close_t = datetime(t.year, t.month, t.day, 16, 0, tzinfo=ET)
        c4.metric("距離收盤", fmt_delta(close_t - t))
    else:
        op = next_regular_open(t)
        if op:
            c4.metric("距離開盤", fmt_delta(op - t))

# ============================================================
# 當日走勢圖（含入場區／止損／目標）
# ============================================================
def intraday_series(hist):
    idx = hist.index
    idx_et = idx.tz_convert(ET) if idx.tz is not None else idx
    last_day = idx_et[-1].date()
    mask = idx_et.date == last_day
    s = hist["Close"][mask].copy()
    s.index = idx_et[mask].tz_localize(None)
    return s

def render_chart(plan, s, price):
    lo, hi = plan["entry"]
    levels = [("止損", stop_trigger_level(plan))] + [("目標", t) for t in plan["targets"]]
    visible = [(k, v) for k, v in levels if abs(v - price) / price <= 0.12]
    hidden = [f"{k} {v:.2f}" for k, v in levels if abs(v - price) / price > 0.12]

    vals = [float(s.min()), float(s.max()), lo, hi] + [v for _, v in visible]
    pad = (max(vals) - min(vals)) * 0.05 or 1.0
    scale = alt.Scale(domain=[min(vals) - pad, max(vals) + pad], zero=False)

    line_df = s.rename("v").rename_axis("t").reset_index()
    line = alt.Chart(line_df).mark_line(color="#4c78a8").encode(
        x=alt.X("t:T", title=None), y=alt.Y("v:Q", scale=scale, title=None))
    last = alt.Chart(line_df.tail(1)).mark_point(filled=True, size=70, color="#ff7f0e").encode(
        x="t:T", y=alt.Y("v:Q", scale=scale))
    band = alt.Chart(pd.DataFrame({"a": [lo], "b": [hi]})).mark_rect(
        opacity=0.18, color="#2ca02c").encode(y=alt.Y("a:Q", scale=scale, title=None), y2="b:Q")

    layers = [band, line, last]
    if visible:
        rules_df = pd.DataFrame(visible, columns=["kind", "v"])
        rules = alt.Chart(rules_df).mark_rule(strokeDash=[5, 4]).encode(
            y=alt.Y("v:Q", scale=scale),
            color=alt.Color("kind:N", legend=None,
                            scale=alt.Scale(domain=["止損", "目標"], range=["#d62728", "#1f77b4"])))
        layers.append(rules)
    return alt.layer(*layers).properties(height=170), hidden

# ============================================================
# 監控主流程
# ============================================================
def monitor():
    rows, chart_items = [], []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current_plans = st.session_state.plans
    session = get_session()

    for symbol, plan in current_plans.items():
        entry_txt = f"${plan['entry'][0]:.2f}–${plan['entry'][1]:.2f}"
        row = {
            "代碼": symbol, "現價": "—", "狀態": "❌ 無法取得價格",
            "距入場區": "—", "接近度": None, "持倉 R": "—",
            "入場區": entry_txt, "止損": format_stop(plan["stop"]),
            "目標": " / ".join(f"${x:.2f}" for x in plan["targets"]),
            "Telegram": "—", "時段": session, "資料新鮮度": "—", "資料時間": "—",
        }

        hist = get_history(symbol)
        if hist is None or hist.empty:
            rows.append(row)
            continue

        price = float(hist["Close"].iloc[-1])
        data_time = hist.index[-1]
        lo, hi = plan["entry"]
        in_zone = price_in_entry(price, plan["entry"])
        key = today_key(symbol, plan)

        # --- 資料新鮮度 ---
        age = data_age_minutes(data_time)
        is_fresh = age is not None and age <= FRESH_MINUTES
        if is_fresh:
            data_note = f"✅ {age:.0f} 分鐘前"
        elif age is not None:
            data_note = f"⚠️ 過期 {age / 60:.1f} 小時" if age >= 60 else f"⚠️ 過期 {age:.0f} 分鐘"
        else:
            data_note = "—"
        can_track = is_fresh or allow_stale

        # --- 距入場區 / 接近度 ---
        if in_zone:
            dist_pct, dist_txt = 0.0, "🟢 在入場區內"
        elif price < lo:
            dist_pct = (lo - price) / price * 100
            dist_txt = f"{'🟡' if dist_pct < 2 else '⚪'} 差 {dist_pct:.1f}%（需上漲）"
        else:
            dist_pct = (price - hi) / price * 100
            dist_txt = f"{'🟡' if dist_pct < 2 else '⚪'} 差 {dist_pct:.1f}%（需回落）"
        closeness = max(0.0, 100.0 - dist_pct * 10)  # 距離 10% 以上為 0

        # --- 入場通知 ---
        status_parts = []
        if in_zone and key not in st.session_state.triggered:
            if session not in alert_sessions:
                status_parts.append(f"⏸ {session}未啟用通知")
            elif not can_track:
                status_parts.append("⏸ 價格資料過期，暫不通知")
            else:
                ok, msg = send_telegram(build_alert(symbol, price, plan, now, session))
                if ok:
                    st.session_state.triggered.add(key)
                    status_parts.append("✅ 入場已通知")
                else:
                    status_parts.append(f"❌ {msg}")
        elif in_zone:
            status_parts.append("入場已通知（今日不重複）")

        # --- 持倉追蹤 ---
        pos = st.session_state.positions.get(key)
        if pos is None and in_zone and can_track:
            pos = {"entry": price, "time": now, "hit": [], "stopped": False,
                   "closed": False, "max_r": 0.0, "last_r": 0.0, "final_r": None,
                   "events": [], "notified": []}
            st.session_state.positions[key] = pos
        if pos is not None and can_track:
            update_position(plan, pos, price)
        if pos is not None:
            for ev in pos["events"]:
                if ev["kind"] in pos["notified"]:
                    continue
                label = "止損" if ev["kind"] == "STOP" else f"目標{ev['kind'][1:]}"
                if session not in alert_sessions:
                    status_parts.append(f"⏸ {label}待發（{session}未啟用通知）")
                    continue
                ok, msg = send_telegram(build_event_alert(symbol, ev, plan, pos, now, session))
                if ok:
                    pos["notified"].append(ev["kind"])
                    status_parts.append(f"✅ {label}已通知")
                else:
                    status_parts.append(f"❌ {msg}")

        # --- 狀態文字 ---
        if pos and pos["stopped"]:
            state = "🛑 已止損"
        elif pos and pos["closed"]:
            state = "🏁 目標全達"
        elif pos:
            state = "📈 持倉追蹤中"
        elif in_zone:
            state = "🟢 到達入場區"
        elif plan["direction"] == "LONG":
            state = "⏳ 等待價格上來" if price < lo else "⏳ 等待回踩"
        else:
            state = "⏳ 等待價格回落" if price > hi else "⏳ 等待反彈"

        row.update({
            "現價": f"${price:.2f}", "狀態": state,
            "距入場區": dist_txt, "接近度": closeness,
            "持倉 R": position_text(plan, pos),
            "Telegram": "；".join(status_parts) if status_parts else "—",
            "資料新鮮度": data_note, "資料時間": str(data_time),
        })
        rows.append(row)
        chart_items.append((symbol, plan, intraday_series(hist), price))

    st.session_state.last_refresh = now
    st.dataframe(
        pd.DataFrame(rows),
        **STRETCH,
        hide_index=True,
        column_config={
            "接近度": st.column_config.ProgressColumn(
                "接近度", min_value=0, max_value=100, format="%d%%",
                help="100% = 已在入場區；距離每增加 1% 減少 10%，10% 以上為 0"),
        },
    )

    today = et_date()
    todays = [v for k, v in st.session_state.positions.items() if k.startswith(today)]
    if todays:
        n_open = sum(1 for v in todays if not v["closed"])
        n_stop = sum(1 for v in todays if v["stopped"])
        n_done = sum(1 for v in todays if v["closed"] and not v["stopped"])
        st.caption(f"📊 今日持倉追蹤：進行中 {n_open}｜已止損 {n_stop}｜目標全達 {n_done}")
    return chart_items

def live_panel():
    render_clock()
    chart_items = monitor()
    st.caption(
        f"最後更新：{st.session_state.last_refresh}｜美東 {now_et().strftime('%H:%M:%S')}"
        f"｜時段：{get_session()}"
    )
    if show_charts and chart_items:
        st.subheader("📈 當日走勢與關鍵價位")
        st.caption("綠帶＝入場區｜紅虛線＝止損｜藍虛線＝目標｜橘點＝最新價")
        for i in range(0, len(chart_items), 2):
            cols = st.columns(2)
            for col, item in zip(cols, chart_items[i:i + 2]):
                symbol, plan, s, price = item
                with col:
                    st.markdown(f"**{symbol}**　${price:.2f}")
                    chart, hidden = render_chart(plan, s, price)
                    st.altair_chart(chart, **STRETCH)
                    if hidden:
                        st.caption("圖外價位：" + "、".join(hidden))

if st.button("⚡ 立即更新價格"):
    get_history.clear()  # 清除快取，強制重新抓取

# 表格與刷新都放在同一個 fragment 內：
# 自動更新時每 60 秒只重跑這一塊，且只畫一次（不再從 fragment 外寫入）
st.fragment(run_every="60s" if auto_refresh else None)(live_panel)()

st.divider()
st.subheader("🧠 計算原則")
st.markdown(
    """
- **多單止損**：最近支撐 − 0.5 ATR
- **空單止損**：最近阻力 + 0.5 ATR
- **目標**：優先採用最近阻力／支撐
- **R:R** = 潛在獲利 ÷ 潛在風險
- 明顯不適合追價時，使用「回踩／突破確認」入場，而不是直接追現價。
- 本版本的 Telegram 只負責**提醒價格進入你設定的交易區**，不會自動下單。
"""
)

st.warning(
    "重要：Streamlit Cloud 的程式只有在 App 實際運行／頁面活躍時才能可靠地執行前端每分鐘刷新。"
    " 如果你要求 24/7 即使手機關閉 App 仍持續監控，應把監控程式部署到 VPS／雲端背景服務，而不是只依靠 Streamlit。"
)
