#!/usr/bin/env python3
"""
Vérification uptime -> Discord, en une seule passe (pour GitHub Actions).
L'état précédent est lu/écrit dans state.json (conservé entre deux exécutions
grâce au cache GitHub Actions), pour n'alerter que sur les changements.

Variables d'environnement (Secrets GitHub) :
  UPTIME_URL, UPTIME_API_KEY, DISCORD_WEBHOOK
Optionnelles : LATENCY_THRESHOLD (ms, défaut 200), API_FAIL_LIMIT (défaut 2),
               DISCORD_MENTION (ex: "@here")
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

UPTIME_URL = os.environ.get("UPTIME_URL")
API_KEY = os.environ.get("UPTIME_API_KEY")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
LATENCY_MS = int(os.environ.get("LATENCY_THRESHOLD") or "200")
API_FAIL_LIMIT = int(os.environ.get("API_FAIL_LIMIT") or "2")
MENTION = os.environ.get("DISCORD_MENTION", "")
STATE_FILE = "state.json"
UA = "uptime-discord-bot/1.0"

GREEN, RED, ORANGE, GREY = 0x2ECC71, 0xE74C3C, 0xF39C12, 0x95A5A6


def http_json(url, headers=None, data=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else None


def discord(title, desc, color, ping=False, retry=True):
    payload = {
        "username": "Uptime Hôpital",
        "content": MENTION if (ping and MENTION) else "",
        "embeds": [{"title": title, "description": desc, "color": color,
                    "timestamp": datetime.now(timezone.utc).isoformat()}],
    }
    try:
        http_json(WEBHOOK, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    except urllib.error.HTTPError as e:
        if e.code == 429 and retry:
            time.sleep(float(json.loads(e.read() or b"{}").get("retry_after", 2)))
            discord(title, desc, color, ping, retry=False)
        else:
            print(f"Erreur webhook Discord : {e}")
    except Exception as e:
        print(f"Erreur webhook Discord : {e}")


def parse_services(data):
    out = {}
    for it in data.get("services", []):
        name = it.get("label") or it.get("service") or "?"
        lat = it.get("last_ms")
        out[name] = {
            "up": str(it.get("status", "")).lower() == "up",
            "latency": float(lat) if isinstance(lat, (int, float)) else None,
            "code": it.get("last_code"),
            "uptime": it.get("uptime"),
        }
    return out


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def summary(services):
    return "\n".join(
        f"{'🟢' if s['up'] else '🔴'} {n} — {s['uptime']} % — {s['latency']:.0f} ms"
        if s["latency"] is not None else f"{'🟢' if s['up'] else '🔴'} {n} — {s['uptime']} %"
        for n, s in services.items()
    )


def main():
    if not all([UPTIME_URL, API_KEY, WEBHOOK]):
        sys.exit("Secrets manquants : UPTIME_URL, UPTIME_API_KEY, DISCORD_WEBHOOK.")

    prev = load_state()
    first_run = prev is None
    state = prev or {"services": {}, "slow": [], "api_fails": 0, "api_down": False, "last_daily": ""}

    # 1. Interroger l'API
    try:
        services = parse_services(http_json(UPTIME_URL, {"X-API-Key": API_KEY}))
    except Exception as e:
        state["api_fails"] += 1
        print(f"Échec API ({state['api_fails']})")
        if state["api_fails"] >= API_FAIL_LIMIT and not state["api_down"]:
            state["api_down"] = True
            discord("🔴 API uptime injoignable",
                    f"{state['api_fails']} échecs consécutifs : `{e}`\n"
                    "Le serveur ou le bastion est peut-être tombé.", RED, ping=True)
        save_state(state)
        return

    if state["api_down"]:
        discord("✅ API uptime de nouveau joignable", "", GREEN)
    state["api_fails"], state["api_down"] = 0, False

    # 2. Premier lancement : état des lieux
    if first_run:
        discord("Surveillance GitHub Actions active",
                "Vérification toutes les ~5 min.\n\n" + summary(services), GREY)

    # 3. Changements d'état
    for name, s in services.items():
        was_up = state["services"].get(name)
        if was_up is not None and was_up != s["up"]:
            if s["up"]:
                discord(f"✅ {name} est de nouveau UP", "", GREEN, ping=True)
            else:
                discord(f"🔴 {name} est DOWN",
                        f"Dernier code : `{s['code']}`\nUptime : {s['uptime']} %", RED, ping=True)
        elif was_up is None and not first_run and not s["up"]:
            discord(f"🔴 {name} est DOWN", f"Dernier code : `{s['code']}`", RED, ping=True)
        state["services"][name] = s["up"]

        # Latence
        lat = s["latency"]
        if s["up"] and lat is not None:
            if lat > LATENCY_MS and name not in state["slow"]:
                state["slow"].append(name)
                discord(f"🟠 {name} lent", f"Latence : {lat:.0f} ms (seuil {LATENCY_MS} ms)", ORANGE)
            elif lat <= LATENCY_MS and name in state["slow"]:
                state["slow"].remove(name)
                discord(f"✅ {name} latence normale", f"{lat:.0f} ms", GREEN)

    for name in set(state["services"]) - set(services):
        discord(f"⚠️ {name} n'apparaît plus dans le feed", "", ORANGE)
        del state["services"][name]

    # 4. Récap quotidien (prouve que la surveillance tourne toujours)
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if not first_run and state.get("last_daily") != today and now.hour >= 7:
        discord("📋 Récap quotidien — surveillance toujours active", summary(services), GREY)
    if first_run or (state.get("last_daily") != today and now.hour >= 7):
        state["last_daily"] = today

    save_state(state)
    print("OK")  # rien de détaillé : les logs d'un dépôt public sont visibles par tous


if __name__ == "__main__":
    main()
