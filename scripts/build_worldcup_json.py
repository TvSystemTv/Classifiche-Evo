#!/usr/bin/env python3
"""
Converte i dati pubblici openfootball/worldcup.json (2026) nel formato
worldcup.json richiesto dalla nostra WebView (data/worldcup.html).

Fonte dati (pubblico dominio, nessuna API key):
  https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json

IMPORTANTE - formato codici fase a eliminazione:
  Il file usa codici compatti stile FIFA, non nomi descrittivi:
    "1A"          -> vincitrice Gruppo A
    "2B"          -> seconda classificata Gruppo B
    "3A/B/C/D/F"  -> migliore terza tra i gruppi elencati
    "W74"         -> vincente Match 74
    "L101"        -> perdente Match 101
  Li risolviamo qui usando le classifiche reali che calcoliamo dai
  risultati. Per le terze classificate ("3X/Y/.../"), la regola FIFA
  ufficiale usa una tabella di assegnazione a 495 combinazioni (Annex C)
  che non è replicata qui per intero: usiamo un'euristica che incrocia
  i gruppi candidati elencati nel codice con le 8 migliori terze reali.
  Funziona nella stragrande maggioranza dei casi, ma in caso di dubbio
  verifica sempre il risultato reale prima di un turno decisivo.

Uso:
  python3 build_worldcup_json.py > data/worldcup.json
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone

MATCHES_URL = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"

# Mappa nome squadra (come scritto nel file openfootball) -> codice ISO 3166-1 alpha-2
TEAM_TO_ISO = {
    "Mexico": "mx", "South Africa": "za", "South Korea": "kr", "Czech Republic": "cz",
    "Canada": "ca", "Bosnia & Herzegovina": "ba", "Qatar": "qa", "Switzerland": "ch",
    "Brazil": "br", "Morocco": "ma", "Haiti": "ht", "Scotland": "gb-sct",
    "USA": "us", "Paraguay": "py", "Australia": "au", "Turkey": "tr",
    "Germany": "de", "Curaçao": "cw", "Ivory Coast": "ci", "Ecuador": "ec",
    "Netherlands": "nl", "Japan": "jp", "Sweden": "se", "Tunisia": "tn",
    "Belgium": "be", "Egypt": "eg", "Iran": "ir", "New Zealand": "nz",
    "Spain": "es", "Cape Verde": "cv", "Saudi Arabia": "sa", "Uruguay": "uy",
    "France": "fr", "Senegal": "sn", "Iraq": "iq", "Norway": "no",
    "Argentina": "ar", "Algeria": "dz", "Austria": "at", "Jordan": "jo",
    "Portugal": "pt", "DR Congo": "cd", "Uzbekistan": "uz", "Colombia": "co",
    "England": "gb-eng", "Croatia": "hr", "Ghana": "gh", "Panama": "pa",
}

# Round (testo openfootball, ATTENZIONE: singolare) -> chiave bracket usata da worldcup.html
KNOCKOUT_ROUND_MAP = {
    "Round of 32": "round_of_32",
    "Round of 16": "round_of_16",
    "Quarter-final": "quarter_finals",
    "Quarter-finals": "quarter_finals",
    "Semi-final": "semi_finals",
    "Semi-finals": "semi_finals",
    "Match for third place": "third_place",
    "Final": "final",
}

GROUP_CODE_RE = re.compile(r'^([12])([A-L])$')
THIRD_CODE_RE = re.compile(r'^3([A-L](?:/[A-L])*)$')
WL_CODE_RE = re.compile(r'^([WL])(\d+)$')


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def team_obj(name):
    """Costruisce l'oggetto team, gestendo i casi non ancora risolvibili
    (placeholder testuale al posto di nome/bandiera)."""
    if not name:
        return {"name": None, "code": None, "flag": None, "placeholder": "TBD"}
    iso = TEAM_TO_ISO.get(name)
    if iso:
        return {"name": name, "code": iso.upper(), "flag": f"https://flagcdn.com/w40/{iso}.png", "placeholder": None}
    # Nome non in mappa: trattalo come placeholder leggibile (es. una squadra
    # nuova non ancora aggiunta a TEAM_TO_ISO) finché non aggiorni la mappa.
    return {"name": None, "code": None, "flag": None, "placeholder": name}


def compute_group_standings(matches_raw):
    """Calcola la classifica di ogni girone dai risultati disponibili.
    Ritorna dict {"A": [ {name, played, won, draw, lost, gf, ga, points}, ... ordinata ]}."""
    groups = {}
    for m in matches_raw["matches"]:
        g = m.get("group")
        if not g:
            continue
        letter = g.replace("Group ", "").strip()
        groups.setdefault(letter, {})
        for t in (m["team1"], m["team2"]):
            groups[letter].setdefault(t, {
                "name": t, "played": 0, "won": 0, "draw": 0, "lost": 0,
                "gf": 0, "ga": 0, "points": 0,
            })
        score = m.get("score")
        if not score or "ft" not in score:
            continue
        s1, s2 = score["ft"]
        t1, t2 = groups[letter][m["team1"]], groups[letter][m["team2"]]
        t1["played"] += 1; t2["played"] += 1
        t1["gf"] += s1; t1["ga"] += s2
        t2["gf"] += s2; t2["ga"] += s1
        if s1 > s2:
            t1["won"] += 1; t1["points"] += 3; t2["lost"] += 1
        elif s2 > s1:
            t2["won"] += 1; t2["points"] += 3; t1["lost"] += 1
        else:
            t1["draw"] += 1; t2["draw"] += 1; t1["points"] += 1; t2["points"] += 1

    result = {}
    for letter, teams in groups.items():
        ordered = sorted(teams.values(), key=lambda x: (-x["points"], -(x["gf"] - x["ga"]), -x["gf"]))
        result[letter] = ordered
    return result


def best_thirds_letters(standings):
    """Calcola le lettere dei gruppi le cui terze classificate rientrano
    nelle 8 migliori terze del torneo (criterio: punti, diff. reti, gol fatti)."""
    thirds = []
    for letter, teams in standings.items():
        if len(teams) >= 3:
            t = teams[2]
            if t["played"] > 0 or t["points"] > 0:
                thirds.append((letter, t))
    thirds.sort(key=lambda x: (-x[1]["points"], -(x[1]["gf"] - x[1]["ga"]), -x[1]["gf"]))
    return {letter for letter, _ in thirds[:8]}


def resolve_code(code, standings, qualified_third_letters, resolved_by_num):
    """Decodifica un singolo codice openfootball in un team_obj()."""
    m = GROUP_CODE_RE.match(code)
    if m:
        pos, letter = int(m.group(1)), m.group(2)
        teams = standings.get(letter, [])
        if len(teams) >= pos and (teams[pos - 1]["played"] > 0):
            return team_obj(teams[pos - 1]["name"])
        label = "Vincitrice" if pos == 1 else "Seconda"
        return team_obj(None) | {"placeholder": f"{label} Gruppo {letter}"}

    m = THIRD_CODE_RE.match(code)
    if m:
        candidate_letters = m.group(1).split("/")
        matches_qualified = [l for l in candidate_letters if l in qualified_third_letters]
        if len(matches_qualified) == 1:
            letter = matches_qualified[0]
            teams = standings.get(letter, [])
            if len(teams) >= 3:
                return team_obj(teams[2]["name"])
        # Ambiguo o non ancora risolvibile: lascia un placeholder leggibile.
        return team_obj(None) | {"placeholder": f"Migliore 3ª: {'/'.join(candidate_letters)}"}

    m = WL_CODE_RE.match(code)
    if m:
        kind, num = m.group(1), int(m.group(2))
        ref = resolved_by_num.get(num)
        if ref and ref.get("winner"):
            target_name = ref["winner"] if kind == "W" else (
                ref["home"]["name"] if ref["winner"] == ref["away"]["name"] else ref["away"]["name"]
            )
            return team_obj(target_name)
        label = "Vincente" if kind == "W" else "Perdente"
        return team_obj(None) | {"placeholder": f"{label} Match {num}"}

    # Non è un codice: è già un nome squadra reale.
    return team_obj(code)


def build_bracket(matches_raw, standings):
    qualified_thirds = best_thirds_letters(standings)

    knockout = [m for m in matches_raw["matches"] if m.get("round") in KNOCKOUT_ROUND_MAP]
    knockout.sort(key=lambda m: m.get("num") or 0)

    bracket = {"round_of_32": [], "round_of_16": [], "quarter_finals": [], "semi_finals": [],
               "third_place": None, "final": None}
    resolved_by_num = {}

    for m in knockout:
        home = resolve_code(m["team1"], standings, qualified_thirds, resolved_by_num)
        away = resolve_code(m["team2"], standings, qualified_thirds, resolved_by_num)

        score = m.get("score") or {}
        ft = score.get("ft")
        winner = None
        if ft and home.get("name") and away.get("name"):
            if ft[0] > ft[1]:
                winner = home["name"]
            elif ft[1] > ft[0]:
                winner = away["name"]

        entry = {
            "matchNumber": m.get("num"),
            "home": home,
            "away": away,
            "homeScore": ft[0] if ft else None,
            "awayScore": ft[1] if ft else None,
            "winner": winner,
            "datetime": f"{m.get('date','')}T{m.get('time','00:00').split(' ')[0]}:00",
        }

        num = m.get("num")
        if num is not None:
            resolved_by_num[num] = entry

        key = KNOCKOUT_ROUND_MAP[m["round"]]
        if key in ("final", "third_place"):
            bracket[key] = entry
        else:
            bracket[key].append(entry)

    return bracket


def build_groups_output(standings):
    result = []
    for letter in sorted(standings.keys()):
        teams = standings[letter]
        result.append({
            "name": f"Group {letter}",
            "teams": [
                {**team_obj(t["name"]), "played": t["played"], "won": t["won"],
                 "draw": t["draw"], "lost": t["lost"], "points": t["points"]}
                for t in teams
            ],
        })
    return result


def main():
    try:
        matches_raw = fetch_json(MATCHES_URL)
    except Exception as e:
        print(f"Errore nel download dei dati Mondiale: {e}", file=sys.stderr)
        sys.exit(1)

    standings = compute_group_standings(matches_raw)
    output = {
        "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "groups": build_groups_output(standings),
        "bracket": build_bracket(matches_raw, standings),
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
