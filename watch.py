"""Wise FX Watch - reads Wise rates, updates the online dashboard, emails buy/sell signals.

GitHub Actions runs this every hour (see .github/workflows/watch.yml).
Local use:  python watch.py            one check
            python watch.py --loop     check every hour until you stop it
            python watch.py --demo --dry-run   simulated rates, emails saved to out/ instead of sent
"""

# =============================== SETTINGS ====================================
CURRENCIES = ["USD", "EUR", "GBP", "AUD", "JPY", "MYR", "INR", "LKR"]
SEND_ONLY = ["INR", "LKR"]          # Wise can't hold these, so only "good day to send" alerts
AMOUNT_SGD = 20
LOOKBACK_DAYS = 90
BUY_RANK = 0.85                     # buy signal when today's rate beats 85% of the last LOOKBACK_DAYS
TAKE_PROFIT_PCT = 0.5               # sell signal when converting back gains this much after both fees
STOP_LOSS_PCT = None                # e.g. 2.0 -> warning when a holding is down 2% after fees
MAX_HOLD_DAYS = 60                  # reminder to review a holding after this many days
TARGETS = {}                        # rate alerts (units per 1 SGD); also managed from the dashboard
HOLDINGS = []                       # normally managed from the dashboard (saved in wallet.json); used only if wallet.json is missing
FEE_PCT = {"USD": .45, "EUR": .45, "GBP": .45, "AUD": .45, "JPY": .5, "MYR": .6, "INR": .7, "LKR": 1.0}
COOLDOWN_HOURS = 24                 # don't repeat the same signal more often than this
SUMMARY_HOUR_SGT = 8                # daily summary email around 8am Singapore time (None = off)
# =============================================================================

import argparse, csv, json, math, os, random, re, smtplib, sys, time, urllib.error, urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent
DATA, DOCS, OUT = ROOT / "data", ROOT / "site", ROOT / "out"
BASE, DAY_MS, SGT = "SGD", 86400000, timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
DEMO = DRY = False


# ------------------------------------------------------------------ fetching
def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            err = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {err}")


def to_ms(t):
    if isinstance(t, (int, float)):
        return int(t * 1000 if t < 1e12 else t)
    try:
        d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp() * 1000)
    except ValueError:
        return None


def by_day(pts):
    d = {}
    for t, v in sorted(pts):
        d[t // DAY_MS] = [t, v]
    return [d[k] for k in sorted(d)]


def wise_history(c):
    raw = get(f"https://wise.com/rates/history+live?source={BASE}&target={c}&length=1&resolution=daily&unit=year")
    return by_day([[to_ms(p["time"]), float(p["value"])] for p in raw if p.get("value")])


def wise_live(c):
    raw = get(f"https://wise.com/rates/live?source={BASE}&target={c}")
    raw = raw[-1] if isinstance(raw, list) else raw
    return [to_ms(raw.get("time")) or int(time.time() * 1000), float(raw["value"])]


def wise_fee(s, t, amount):
    for v in ("v3", "v1"):
        try:
            raw = get(f"https://wise.com/gateway/{v}/price?sourceAmount={amount}&sourceCurrency={s}&targetCurrency={t}", 1)
        except RuntimeError:
            continue
        for o in (raw if isinstance(raw, list) else raw.get("prices", [raw])):
            if str(o.get("payInMethod")).upper() == "BALANCE" and o.get("total") is not None:
                return float(o["total"]) / float(o.get("sourceAmount") or amount) * 100
    return None


def ecb_history(c):  # fallback only: ECB reference rates, close to Wise but not Wise
    end, start = date.today(), date.today() - timedelta(days=366)
    try:
        raw = get(f"https://api.frankfurter.dev/v1/{start}..{end}?from={BASE}&to={c}", 2)
    except RuntimeError:
        raw = get(f"https://api.frankfurter.app/{start}..{end}?from={BASE}&to={c}", 2)
    return by_day([[to_ms(d), float(r[c])] for d, r in raw["rates"].items() if c in r])


DEMO_LEVEL = {"USD": .776, "EUR": .668, "GBP": .579, "AUD": 1.18, "JPY": 115.2, "MYR": 3.29, "INR": 67.4, "LKR": 232.5}


def demo_history(c):
    rnd, x, xs = random.Random(c + str(date.today())), 0.0, []
    for _ in range(366):
        x = x * 0.992 + 0.0035 * rnd.gauss(0, 1)
        xs.append(x)
    today = int(time.time() * 1000) // DAY_MS * DAY_MS
    return [[today - (365 - i) * DAY_MS, DEMO_LEVEL.get(c, 1) * math.exp(v - xs[-1])] for i, v in enumerate(xs)]


def load(c):
    if DEMO:
        h = demo_history(c)
        return {"hist": h, "live": [int(time.time() * 1000), h[-1][1]], "src": "demo"}
    try:
        h = wise_history(c)
        try:
            live = wise_live(c)
        except RuntimeError:
            live = h[-1]
        return {"hist": h, "live": live, "src": "wise"}
    except RuntimeError as e:
        print(f"  {c}: Wise failed ({e}), trying ECB fallback")
        h = ecb_history(c)
        return {"hist": h, "live": h[-1], "src": "ecb"}


# ------------------------------------------------------------------ analysis
def mean(a):
    return sum(a) / len(a)


def std(a):
    m = mean(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / (len(a) - 1))


def phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def p_touch(up, sigma, n):
    return 1.0 if up <= 0 else min(1.0, 2 * (1 - phi(math.log(1 + up) / (sigma * math.sqrt(n))))) if sigma > 0 else 0.0


def slope(v):
    n = len(v)
    xs, ys = range(n), [math.log(x) for x in v]
    mx, my = (n - 1) / 2, mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)


def accuracy(v, h=7):
    mom = mr = n = 0
    for t in range(20, len(v) - h):
        act = math.copysign(1, v[t + h] - v[t]) if v[t + h] != v[t] else 0
        trend = slope(v[t - 13:t + 1])
        rev = mean(v[t - 19:t + 1]) - v[t]
        if act and trend and rev:
            n += 1
            mom += math.copysign(1, trend) == act
            mr += math.copysign(1, rev) == act
    return (mom / n, mr / n) if n > 30 else (None, None)


def analyze(c, d, f, fb):
    v = [p[1] for p in d["hist"]]
    cur = d["live"][1]
    win = v[-LOOKBACK_DAYS:]
    rank = sum(x < cur for x in win) / len(win)
    sigma = std([math.log(v[i] / v[i - 1]) for i in range(max(1, len(v) - 180), len(v))])
    s14 = slope(v[-14:] + [cur])
    mom, mr = accuracy(v)
    model = "trend" if mom and mom >= 0.55 else "average" if mr and mr >= 0.55 else "random walk"
    fc7 = cur * math.exp(7 * s14) if model == "trend" else (cur + (mean(v[-20:]) - cur) * 0.5 if model == "average" else cur)
    sd7, sdn = sigma * math.sqrt(7), sigma * math.sqrt(14)
    return {"ccy": c, "cur": cur, "rank": rank, "lo": min(win), "hi": max(win), "ma30": mean(v[-30:]), "sigma": sigma,
            "trend_wk": math.exp(7 * s14) - 1, "model": model, "mom_acc": mom, "mr_acc": mr, "fc7": fc7,
            "r7": [fc7 * math.exp(-sd7), fc7 * math.exp(sd7)], "p_up": p_touch(0.005, sigma, 14),
            "f": f, "fb": fb, "be": 1 / ((1 - f / 100) * (1 - fb / 100)) - 1 if fb else None,
            "get": AMOUNT_SGD * (1 - f / 100) * cur, "send_only": c in SEND_ONLY, "src": d["src"],
            "verdict": "good" if rank >= BUY_RANK else "weak" if rank <= 1 - BUY_RANK else "average"}


def fmt(v):
    return f"{v:,.{4 if v < 10 else 3 if v < 100 else 2}f}"


# ------------------------------------------------------------------ wallet (holdings + alerts, edited from the dashboard)
WALLET = ROOT / "wallet.json"


def load_wallet():
    global HOLDINGS, TARGETS
    w = jload(WALLET, None)
    if isinstance(w, dict):
        HOLDINGS, TARGETS = list(w.get("holdings", [])), dict(w.get("targets", {}))


def b36(n):
    d = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = d[r] + out
    return out or "0"


def wallet_request():
    """Apply a dashboard request (a GitHub issue opened by the repo owner) to wallet.json."""
    f = {}
    for line in os.environ.get("ISSUE_BODY", "").splitlines():
        if ":" in line:
            k, v = line.strip().strip("`").split(":", 1)
            f[k.strip().lower()] = v.strip().strip("`")
    w = jload(WALLET, {})
    w.setdefault("holdings", [])
    w.setdefault("targets", {})
    act, ccy = f.get("action", "").lower(), f.get("ccy", "").upper()
    try:
        if act == "add":
            try:
                amount, sgd = float(f["amount"]), float(f["sgd"])
                d = f.get("date") or datetime.now(SGT).date().isoformat()
                date.fromisoformat(d)
            except (KeyError, ValueError):
                raise ValueError("amount, SGD paid and date must be filled in correctly")
            if not (re.fullmatch("[A-Z]{3}", ccy) and amount > 0 and sgd > 0):
                raise ValueError("currency, amount and SGD paid must be valid")
            n = int(time.time())
            while any(x.get("id") == b36(n) for x in w["holdings"]):
                n += 1
            h = {"id": b36(n), "ccy": ccy, "amount": round(amount, 4), "sgd": round(sgd, 2), "date": d}
            if f.get("note"):
                h["note"] = f["note"][:60]
            w["holdings"].append(h)
            msg = f"Added {amount:g} {ccy} (paid S${sgd:g}, {d})."
        elif act == "remove":
            keep = [h for h in w["holdings"] if not (h.get("id") == f["id"] if f.get("id") else h.get("ccy") == ccy)]
            if len(keep) == len(w["holdings"]):
                raise ValueError("no matching holding (already removed?)")
            msg = f"Removed {len(w['holdings']) - len(keep)} holding(s). Nice trade."
            w["holdings"] = keep
        elif act == "target":
            if not re.fullmatch("[A-Z]{3}", ccy):
                raise ValueError("currency must be a 3-letter code")
            if f.get("rate"):
                if float(f["rate"]) <= 0:
                    raise ValueError("rate must be positive")
                w["targets"][ccy] = float(f["rate"])
                msg = f"Alert set: email when 1 SGD buys at least {float(f['rate']):g} {ccy}."
            else:
                w["targets"].pop(ccy, None)
                msg = f"Alert for {ccy} removed."
        else:
            raise ValueError("unknown action")
        WALLET.write_text(json.dumps(w, indent=1), encoding="utf-8")
        ok = True
    except (KeyError, ValueError) as e:
        msg, ok = f"Could not apply this request: {e}. Please try again from the dashboard.", False
    OUT.mkdir(exist_ok=True)
    (OUT / "wallet_result.md").write_text(("Done. " if ok else "") + msg + (" The dashboard will update in a minute or two." if ok else ""), encoding="utf-8")
    print(("OK: " if ok else "FAILED: ") + msg)


# ------------------------------------------------------------------ signals
def signals(an):
    out, now = [], datetime.now(SGT)
    held = {h["ccy"] for h in HOLDINGS}
    for c, a in an.items():
        why = (f"1 SGD = {fmt(a['cur'])} {c}, better than {a['rank']:.0%} of the last {LOOKBACK_DAYS} days "
               f"(range {fmt(a['lo'])}-{fmt(a['hi'])}). Fee {a['f']:.2f}%.")
        if a["verdict"] == "good" and c not in held:
            if a["send_only"]:
                out.append((f"SEND:{c}", f"Good day to send SGD to {c}", why))
            else:
                out.append((f"BUY:{c}", f"Buy {c} with SGD", why + f" To profit when converting back, the rate must drop {a['be']:.2%} "
                            f"({c} strengthening), after both fees. Next 7 days likely {fmt(a['r7'][0])}-{fmt(a['r7'][1])}."))
        if c in TARGETS and a["cur"] >= TARGETS[c]:
            out.append((f"TARGET:{c}", f"{c} hit your target {fmt(TARGETS[c])}", why))
    for i, h in enumerate(HOLDINGS):
        a, hid = an.get(h["ccy"]), h.get("id", i)
        if not a:
            continue
        cost = h.get("sgd") or h["amount"] / (h.get("rate") or a["cur"]) / (1 - a["f"] / 100)
        back = h["amount"] * (1 - a["fb"] / 100) / a["cur"]
        pnl = back / cost - 1
        days = (now.date() - date.fromisoformat(h["date"])).days if h.get("date") else 0
        txt = f"{h['amount']:,.2f} {h['ccy']} converts back to S${back:,.2f} now (paid S${cost:,.2f}, {pnl:+.2%} after fees)."
        if pnl * 100 >= TAKE_PROFIT_PCT:
            out.append((f"SELL:{h['ccy']}:{hid}", f"Sell {h['ccy']} back to SGD: {pnl:+.2%}", txt))
        elif STOP_LOSS_PCT and pnl * 100 <= -STOP_LOSS_PCT:
            out.append((f"STOP:{h['ccy']}:{hid}", f"{h['ccy']} holding down {pnl:.2%}", txt))
        elif MAX_HOLD_DAYS and days >= MAX_HOLD_DAYS:
            out.append((f"OLD:{h['ccy']}:{hid}", f"Review {h['ccy']} held {days} days", txt))
    return out


# ------------------------------------------------------------------ email
def table(an):
    rows = "".join(
        f"<tr><td><b>{c}</b>{' (send)' if a['send_only'] else ''}</td><td>{fmt(a['cur'])}</td><td>{a['rank']:.0%}</td>"
        f"<td style='color:{'#16774a' if a['verdict'] == 'good' else '#b1372b' if a['verdict'] == 'weak' else '#56665f'}'>{a['verdict']}</td>"
        f"<td>{a['trend_wk']:+.2%}/wk</td><td>{fmt(a['r7'][0])}-{fmt(a['r7'][1])}</td><td>{a['f']:.2f}%</td></tr>"
        for c, a in an.items())
    return ("<table cellpadding='6' style='border-collapse:collapse;font:14px Arial'><tr style='background:#eef2f0'>"
            "<th>Currency</th><th>1 SGD =</th><th>Beats</th><th>Verdict</th><th>Recent trend</th><th>Likely in 7d</th><th>Fee</th></tr>"
            f"{rows}</table>")


def send(subject, html, attach=None):
    """Returns True if emailed (or saved in dry-run), False if it failed. Never raises."""
    clean = lambda v: "".join((v or "").split())   # drops spaces/newlines/non-breaking spaces from pasted secrets
    user, pw = clean(os.environ.get("EMAIL_USER")), clean(os.environ.get("EMAIL_PASS"))
    to = clean(os.environ.get("EMAIL_TO")) or user
    url = os.environ.get("DASHBOARD_URL") or (f"https://{os.environ['GITHUB_REPOSITORY'].split('/')[0]}.github.io/"
                                               f"{os.environ['GITHUB_REPOSITORY'].split('/')[1]}/" if os.environ.get("GITHUB_REPOSITORY") else "")
    body = (f"<div style='font:14px Arial;max-width:680px'>{html}"
            + (f"<p><a href='{url}'>Open the live dashboard</a></p>" if url else "")
            + "<p style='color:#777;font-size:12px'>Wise FX Watch. Signals are probabilities from past rates, not advice.</p></div>")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "FX Watch: " + subject, user or "fx-watch", to or "you"
    msg.set_content("Open this email in HTML view.")
    msg.add_alternative(body, subtype="html")
    if attach:
        msg.add_attachment(Path(attach).read_bytes(), maintype="text", subtype="html", filename="fx_dashboard.html")
    if DRY:
        OUT.mkdir(exist_ok=True)
        p = OUT / f"email_{int(time.time() * 1000)}.html"
        p.write_text(f"<h3>{msg['Subject']}</h3>{body}", encoding="utf-8")
        print(f"  dry run, email saved: {p.name}")
        return True
    if user and pw and smtp_send(msg, user, pw, subject):
        return True
    if github_issue(msg["Subject"], body):
        return True
    OUT.mkdir(exist_ok=True)
    p = OUT / f"email_{int(time.time() * 1000)}.html"
    p.write_text(f"<h3>{msg['Subject']}</h3>{body}", encoding="utf-8")
    print(f"  NOT DELIVERED (saved as {p.name}): set EMAIL_USER/EMAIL_PASS, or run inside GitHub Actions for GitHub notifications.")
    return False


def smtp_send(msg, user, pw, subject):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    tries = [("ssl", int(os.environ["SMTP_PORT"]))] if os.environ.get("SMTP_PORT") else [("ssl", 465), ("starttls", 587)]
    for mode, port in tries:
        try:
            if mode == "ssl":
                c = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                c = smtplib.SMTP(host, port, timeout=30)
                c.starttls()
            with c:
                c.login(user, pw)
                c.send_message(msg)
            print(f"  emailed: {subject}")
            return True
        except smtplib.SMTPAuthenticationError as e:
            print(f"  EMAIL FAILED ({mode} {port}): {host} rejected the login ({e.smtp_code}). EMAIL_PASS must be a 16-character "
                  "Google App Password, and EMAIL_USER the Gmail address it was created for.")
            return False
        except Exception as e:
            print(f"  EMAIL FAILED ({mode} {port}): {type(e).__name__}: {e}")
    print("  -> mail server refused the connection (Google often blocks logins from GitHub's servers).")
    return False


def github_issue(title, body):
    """Fallback: open a GitHub issue assigned to (and mentioning) the repo owner. GitHub then emails the owner."""
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (tok and repo):
        return False
    owner = repo.split("/")[0]
    data = json.dumps({"title": title, "body": body + f"<p>@{owner}</p>", "assignees": [owner], "labels": ["fx-watch"]}).encode()
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues", data=data, method="POST", headers={
        "Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json", "User-Agent": "fx-watch"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  notified via GitHub issue #{json.loads(r.read())['number']}: {title}")
        return True
    except urllib.error.HTTPError as e:
        print(f"  GITHUB NOTIFY FAILED: HTTP {e.code} {e.read()[:200]!r} (workflow needs 'issues: write' permission)")
    except Exception as e:
        print(f"  GITHUB NOTIFY FAILED: {e}")
    return False


# ------------------------------------------------------------------ outputs
def jload(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def dashboard(data, an, sigs, label):
    note = ("Signals now: " + "; ".join(s[1] for s in sigs) + ".") if sigs else "No buy or sell signals right now."
    snap = {"app": "wise-fx-watch", "base": BASE, "savedAt": int(time.time() * 1000), "demo": DEMO, "label": label,
            "repo": os.environ.get("GITHUB_REPOSITORY", ""), "holdings": HOLDINGS, "targets": TARGETS,
            "takeProfit": TAKE_PROFIT_PCT, "maxHold": MAX_HOLD_DAYS,
            "note": note + f" Checked {datetime.now(SGT):%d %b %H:%M} SGT; updates hourly.",
            "currencies": {c: {"hist": d["hist"], "live": {"t": d["live"][0], "v": d["live"][1]}, "hold": c not in SEND_ONLY,
                               "fee": an[c]["f"], "feeBack": an[c]["fb"] or None} for c, d in data.items()}}
    js = json.dumps(snap).replace("</", "<\\/")
    head = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="refresh" content="1800"><style>body{margin:0}[hidden]{display:none!important}</style>'
            f'<script>window.FX_SNAPSHOT={js}</script></head><body>')
    DOCS.mkdir(exist_ok=True)
    p = DOCS / "index.html"
    p.write_text(head + (ROOT / "dashboard_template.html").read_text(encoding="utf-8") + "</body></html>", encoding="utf-8")
    return p


def run():
    DATA.mkdir(exist_ok=True)
    load_wallet()
    state = jload(DATA / "state.json", {"sent": {}, "active": {}, "fails": 0, "summary": ""})
    data, failed = {}, []
    for c in CURRENCIES:
        try:
            data[c] = load(c)
        except Exception as e:
            failed.append(c)
            print(f"  {c}: no data ({e})")
    if not data:
        state["fails"] += 1
        if state["fails"] == 3:
            send("can't reach any rate source", "<p>The last 3 checks couldn't load rates from Wise or the ECB fallback.</p>")
        (DATA / "state.json").write_text(json.dumps(state, indent=1))
        sys.exit(1)
    an = {}
    for c, d in data.items():
        f = fb = None
        if d["src"] == "wise":
            f = wise_fee(BASE, c, AMOUNT_SGD)
            fb = None if c in SEND_ONLY else wise_fee(c, BASE, round(AMOUNT_SGD * d["live"][1], 2))
        an[c] = analyze(c, d, f or FEE_PCT.get(c, .6), 0 if c in SEND_ONLY else (fb or FEE_PCT.get(c, .6)))
        a = an[c]
        print(f"  {c}: {fmt(a['cur'])} [{d['src']}] beats {a['rank']:.0%}, {a['verdict']}, model {a['model']}")
    srcs = {d["src"] for d in data.values()}
    label = "Demo (simulated)" if DEMO else "Live · Wise rates" if srcs == {"wise"} else "Partly ECB fallback"
    sigs = signals(an)
    dash = dashboard(data, an, sigs, label)

    now, nowts = datetime.now(SGT), time.time()
    with open(DATA / "rates.csv", "a", newline="") as fh:
        w = csv.writer(fh)
        for c, d in data.items():
            w.writerow([now.isoformat(timespec="minutes"), c, d["live"][1], d["src"]])
    new = [s for s in sigs if not state["active"].get(s[0]) or nowts - state["sent"].get(s[0], 0) >= COOLDOWN_HOURS * 3600]
    if new:
        ccys = {k.split(":")[1] for k, _, _ in new}
        ok = send(" | ".join(t for _, t, _ in new), "".join(f"<h2 style='font:600 18px Arial'>{t}</h2><p>{x}</p>" for _, t, x in new)
                  + table({c: an[c] for c in an if c in ccys}))
        if ok:
            with open(DATA / "signals.csv", "a", newline="") as fh:
                for k, t, _ in new:
                    state["sent"][k] = nowts
                    csv.writer(fh).writerow([now.isoformat(timespec="minutes"), k, t])
    state["active"] = {k: True for k, _, _ in sigs if k in state["sent"] or k not in {n[0] for n in new}}
    if srcs != {"wise"} and not DEMO and state.get("warned_src") != str(now.date()):
        if send("Wise unreachable, using ECB fallback", "<p>Wise didn't answer, so these rates are ECB reference rates (close to Wise, "
                "not identical). If this repeats, run watch.py on your own PC instead.</p>"):
            state["warned_src"] = str(now.date())
    if SUMMARY_HOUR_SGT is not None and now.hour >= SUMMARY_HOUR_SGT and state.get("summary") != str(now.date()):
        lines = "".join(f"<li>{t}: {x}</li>" for _, t, x in sigs) or "<li>No buy or sell signals today.</li>"
        if send(f"daily summary {now:%d %b}", f"<h2 style='font:600 18px Arial'>Rates at {now:%H:%M} SGT</h2><ul>{lines}</ul>{table(an)}"
                f"<p style='color:#555'>Beats = share of the last {LOOKBACK_DAYS} days with a worse rate. Likely in 7d = 68% range.</p>", dash):
            state["summary"] = str(now.date())
    state["fails"] = 0
    (DATA / "state.json").write_text(json.dumps(state, indent=1))
    (DATA / "latest.json").write_text(json.dumps({c: {k: v for k, v in a.items()} for c, a in an.items()}, indent=1))
    print(f"Done: {len(sigs)} signal(s), dashboard {dash}")


def main():
    global DEMO, DRY
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true", help="repeat every hour")
    ap.add_argument("--test-email", action="store_true", help="send one test email and stop")
    ap.add_argument("--wallet-issue", action="store_true", help="apply a wallet request from env ISSUE_BODY (used by GitHub Actions)")
    a = ap.parse_args()
    DEMO, DRY = a.demo, a.dry_run
    if a.wallet_issue:
        wallet_request()
        return
    if a.test_email:
        ok = send("test email", "<h2 style='font:600 18px Arial'>It works</h2><p>Your FX Watch email settings are correct.</p>")
        sys.exit(0 if ok else 1)
    while True:
        print(f"Check at {datetime.now(SGT):%Y-%m-%d %H:%M} SGT")
        try:
            run()
        except SystemExit:
            if not a.loop:
                raise
        if not a.loop:
            break
        time.sleep(3600)


if __name__ == "__main__":
    main()
