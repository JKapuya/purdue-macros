#!/usr/bin/env python3
"""
Purdue dining macro scout.

Pulls every dining court's menu for a given day from Purdue HFS's public
endpoints (no key, no signup, no cost), pulls per-item nutrition, scores each
item on macro quality, builds the best attainable plate per hall per meal, and
writes a self-contained HTML dashboard.

    python3 purdue_macros.py                 # today, default profile
    python3 purdue_macros.py --profile bulk
    python3 purdue_macros.py --date 2026-08-24 --open

Stdlib only. Item nutrition is cached on disk by item ID, so day two onward
costs only a handful of requests.
"""

import argparse
import datetime as dt
import html
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Your settings. Edit these.
# ---------------------------------------------------------------------------

PROFILE = "lean"          # "lean" | "bulk" | "maintain"
DIET = "none"             # "none" | "vegetarian" | "vegan"
EXCLUDE_ALLERGENS = []    # e.g. ["Peanuts", "Shellfish"], items testing true are dropped
MIN_ITEM_CALORIES = 20    # filters out condiments, black coffee, broth

# Optional hard ceiling on tray sodium, in mg. 0 turns it off, which is the
# default: capping it at a third of the 2300mg daily limit cut the best trays
# from roughly 60g of protein down to 30g, because the highest protein items in
# a dining court are cured and processed meats. Skipping sauces and dressings
# gets most of the sodium back without giving up the protein. Set this to 800
# if you would rather have the tray enforce it for you.
SODIUM_CAP_PER_MEAL = 0    # mg
DAILY_SODIUM_LIMIT = 2300  # mg, for the "share of your day" readout

PROFILES = {
    # plate_kcal: calorie ceiling for the recommended plate
    # protein_goal: grams of protein the plate is aiming at
    "lean":     {"plate_kcal": 650,  "protein_goal": 45, "sodium_weight": 1.0, "density_weight": 1.0},
    "maintain": {"plate_kcal": 850,  "protein_goal": 55, "sodium_weight": 0.8, "density_weight": 0.85},
    "bulk":     {"plate_kcal": 1150, "protein_goal": 70, "sodium_weight": 0.5, "density_weight": 0.6},
}

BASE = "https://api.hfs.purdue.edu/menus/v2"
HERE = pathlib.Path(__file__).resolve().parent
CACHE_PATH = HERE / "cache" / "nutrition.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

MEAL_ORDER = {"Breakfast": 0, "Brunch": 1, "Lunch": 2, "Late Lunch": 3, "Dinner": 4}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def get_json(url, timeout=30, retries=6):
    """Retries with backoff totalling about a minute. The Mac wakes at 6:30 and
    the refresh fires at 6:30, so the first request of the day often lands
    before wifi has reassociated. Retrying without a delay just burns through
    every attempt in under a second and fails the whole morning run.
    """
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - network flake or no wifi yet
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to fetch {url}: {last}")


def dining_courts():
    data = get_json(f"{BASE}/locations")
    return [loc["Name"] for loc in data["Location"] if loc.get("Type") == "Dining Courts"]


def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache))


NUTRIENT_KEYS = {
    "Calories": "calories",
    "Total fat": "fat",
    "Saturated fat": "sat_fat",
    "Cholesterol": "cholesterol",
    "Sodium": "sodium",
    "Total Carbohydrate": "carbs",
    "Sugar": "sugar",
    "Added Sugar": "added_sugar",
    "Dietary Fiber": "fiber",
    "Protein": "protein",
}


def parse_nutrition(payload):
    out = {k: 0.0 for k in NUTRIENT_KEYS.values()}
    out["serving"] = ""
    for row in payload.get("Nutrition", []):
        name = row.get("Name")
        if name == "Serving Size":
            out["serving"] = (row.get("LabelValue") or "").strip()
        elif name in NUTRIENT_KEYS:
            val = row.get("Value")
            out[NUTRIENT_KEYS[name]] = float(val) if val is not None else 0.0
    return out


def fetch_nutrition(item_ids, cache):
    missing = [i for i in item_ids if i not in cache]
    if missing:
        def one(item_id):
            try:
                return item_id, parse_nutrition(get_json(f"{BASE}/items/{item_id}"))
            except Exception:  # noqa: BLE001 - a dead item shouldn't kill the run
                return item_id, None

        with ThreadPoolExecutor(max_workers=12) as pool:
            for item_id, nut in pool.map(one, missing):
                if nut is not None:
                    cache[item_id] = nut
        save_cache(cache)
    return cache


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def score_item(nut, cfg):
    """0-100 macro quality score for one serving.

    The spine is protein density (g protein per 100 kcal). Fiber adds a little;
    sodium, saturated fat and added sugar subtract. Weights shift by profile.
    A bulk profile cares less about sodium and density, more about volume.
    """
    cal = nut["calories"]
    if cal < MIN_ITEM_CALORIES:
        return None
    per100 = cal / 100.0

    protein_density = nut["protein"] / per100          # g/100kcal
    fiber_density = nut["fiber"] / per100
    sodium_density = nut["sodium"] / per100            # mg/100kcal
    sat_density = nut["sat_fat"] / per100
    sugar_density = nut["added_sugar"] / per100

    # 12 g protein per 100 kcal is the practical ceiling for dining-hall food:
    # grilled chicken and white fish land there, eggs around 8, a bean burger
    # around 6. Normalizing against a leaner ideal compressed every real item
    # into the bottom third of the scale and made the grades meaningless.
    base = clamp(protein_density / 12.0) * 62 * cfg["density_weight"]
    # bulk cares about absolute protein on the plate, not just the ratio
    base += clamp(nut["protein"] / 25.0) * 62 * (1 - cfg["density_weight"])
    base += clamp(fiber_density / 4.0) * 14

    base -= clamp((sodium_density - 250) / 450) * 18 * cfg["sodium_weight"]
    base -= clamp((sat_density - 2.0) / 5.0) * 12
    base -= clamp(sugar_density / 8.0) * 14

    return round(clamp(base, 0, 100), 1)


def tier(score):
    if score >= 45:
        return "excellent", "Excellent"
    if score >= 32:
        return "strong", "Strong"
    if score >= 18:
        return "decent", "Decent"
    return "weak", "Skip"


def diet_ok(item):
    flags = {a["Name"]: a["Value"] for a in item.get("Allergens", [])}
    if DIET == "vegan" and not flags.get("Vegan"):
        return False
    if DIET == "vegetarian" and not (flags.get("Vegetarian") or item.get("IsVegetarian")):
        return False
    for allergen in EXCLUDE_ALLERGENS:
        if flags.get(allergen):
            return False
    return True


# Toppings and sauces can post absurd protein-per-calorie numbers (grated
# parmesan is 11g protein per ounce) but nobody builds a tray out of them.
# They stay in the full listing and out of the recommended plate.
CONDIMENT_WORDS = (
    "sauce", "dressing", "syrup", "grated", "butter", "mayo", "jelly", "jam",
    "gravy", "seasoning", "ketchup", "mustard", "whipped", "topping", "glaze",
    "aioli", "vinaigrette", "sour cream", "cream cheese", "queso", "dip",
)
# Shredded and blended cheeses are toppings by the ounce, but "Cheese Pizza"
# and "Cheeseburger" are meals, so cheese only disqualifies in combination.
CHEESE_QUALIFIERS = ("shredded", "grated", "blend", "crumbles", "sliced")


ANCHOR_PROTEIN = 8  # grams that qualify an item as a protein anchor


def plate_eligible(it):
    if it["calories"] < 50:
        return False
    name = it["name"].lower()
    if any(word in name for word in CONDIMENT_WORDS):
        return False
    if "cheese" in name and any(q in name for q in CHEESE_QUALIFIERS):
        return False
    return True


def build_plate(items, cfg):
    """Two-phase tray. Anchors are the real protein sources, ranked by score,
    the highlights worth walking to a specific hall for. Sides then
    fill the remaining calories with the best of whatever else is out, with no
    score floor, because a tray needs carbs and volume and a hall's best
    available side is still the answer even when it is mediocre.

    No item appears twice (the same dish is often listed under two stations),
    and no station supplies more than two picks. If the tray lands short of the
    protein goal with calories to spare, it goes back for another serving of
    its best anchor rather than padding with a fifth different side.
    """
    pool = sorted((i for i in items if plate_eligible(i)),
                  key=lambda x: (-x["score"], -x["protein"]))
    picks, kcal, salt, per_station, seen = [], 0.0, 0.0, {}, set()
    cap = SODIUM_CAP_PER_MEAL or float("inf")

    def take(it, limit):
        nonlocal kcal, salt
        if len(picks) >= limit or it["name"] in seen:
            return
        if kcal + it["calories"] > cfg["plate_kcal"]:
            return
        if salt + it["sodium"] > cap:
            return
        if per_station.get(it["station"], 0) >= 2:
            return
        picks.append(dict(it, qty=1))
        seen.add(it["name"])
        kcal += it["calories"]
        salt += it["sodium"]
        per_station[it["station"]] = per_station.get(it["station"], 0) + 1

    # Anchors go in by protein per mg of sodium, not by grade. Under a sodium
    # ceiling the binding resource is salt, not calories, and grade order spends
    # the whole budget on one cured meat. Grilled chicken delivers three times
    # the protein per mg that carved ham does.
    anchors = [i for i in pool if i["protein"] >= ANCHOR_PROTEIN]
    if SODIUM_CAP_PER_MEAL:
        anchors.sort(key=lambda i: -(i["protein"] / max(i["sodium"], 1.0)))
    for it in anchors:
        take(it, 3)

    # Sides fill what is left, but a side has to earn the slot. Without this a
    # tray squeezed by the sodium ceiling fills up with cookies and margarine,
    # which are low sodium and nothing else.
    for it in pool:
        if it["score"] >= 12 or it["fiber"] >= 2:
            take(it, 5)

    protein = sum(p["protein"] for p in picks)
    for _ in range(2):
        if protein >= cfg["protein_goal"]:
            break
        candidates = [p for p in picks if p["protein"] >= ANCHOR_PROTEIN and p["qty"] < 3
                      and kcal + p["calories"] <= cfg["plate_kcal"]
                      and salt + p["sodium"] <= cap]
        if not candidates:
            break
        best = max(candidates, key=lambda p: p["protein"] / max(p["calories"], 1))
        best["qty"] += 1
        salt += best["sodium"]
        kcal += best["calories"]
        protein += best["protein"]

    totals = {k: round(sum(p[k] * p["qty"] for p in picks))
              for k in ("calories", "protein", "carbs", "fat", "fiber", "sodium")}
    totals["density"] = round(totals["protein"] / (totals["calories"] / 100.0), 1) if totals["calories"] else 0.0
    totals["hits_goal"] = totals["protein"] >= cfg["protein_goal"]
    totals["sodium_pct"] = round(100 * totals["sodium"] / DAILY_SODIUM_LIMIT)
    totals["salty"] = bool(SODIUM_CAP_PER_MEAL) and totals["sodium"] > SODIUM_CAP_PER_MEAL
    # The best protein on the menu that the sodium ceiling kept off the tray,
    # so the page can name what it is turning down rather than hiding it.
    served = {p["name"] for p in picks}
    blocked = [i for i in items
               if plate_eligible(i) and i["name"] not in served
               and i["protein"] >= ANCHOR_PROTEIN and i["sodium"] > cap]
    totals["blocked"] = max(blocked, key=lambda i: i["protein"], default=None)
    return picks, totals


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def fmt_time(t):
    if not t:
        return ""
    try:
        h, m, _ = t.split(":")
        h, m = int(h), int(m)
        suffix = "AM" if h < 12 else "PM"
        hh = h % 12 or 12
        return f"{hh}:{m:02d} {suffix}"
    except ValueError:
        return t


def collect(date_str, cfg):
    courts = dining_courts()
    raw = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        def one(court):
            try:
                return court, get_json(f"{BASE}/locations/{court}/{date_str}")
            except Exception:  # noqa: BLE001
                return court, None
        for court, payload in pool.map(one, courts):
            if payload:
                raw[court] = payload

    ids = set()
    for payload in raw.values():
        for meal in payload.get("Meals", []):
            for station in meal.get("Stations", []):
                for item in station.get("Items", []):
                    ids.add(item["ID"])

    cache = fetch_nutrition(sorted(ids), load_cache())

    meals = {}
    closed = []
    for court, payload in raw.items():
        served = 0
        for meal in payload.get("Meals", []):
            scored = []
            for station in meal.get("Stations", []):
                for item in station.get("Items", []):
                    nut = cache.get(item["ID"])
                    if not nut or not diet_ok(item):
                        continue
                    s = score_item(nut, cfg)
                    if s is None:
                        continue
                    scored.append({
                        "name": item["Name"],
                        "station": station.get("Name", "n/a"),
                        "score": s,
                        "veg": bool(item.get("IsVegetarian")),
                        **{k: round(nut[k], 1) for k in NUTRIENT_KEYS.values()},
                        "serving": nut["serving"],
                    })
            served += len(scored)
            if not scored:
                continue
            picks, totals = build_plate(scored, cfg)
            hours = meal.get("Hours") or {}
            entry = {
                "hall": court,
                "hours": f"{fmt_time(hours.get('StartTime'))} to {fmt_time(hours.get('EndTime'))}".strip(" to"),
                "start": hours.get("StartTime") or "00:00:00",
                "end": hours.get("EndTime") or "23:59:59",
                "plate": picks,
                "totals": totals,
                "items": sorted(scored, key=lambda x: -x["score"]),
            }
            meals.setdefault(meal["Name"], []).append(entry)
        if served == 0:
            closed.append(court)

    for name in meals:
        meals[name].sort(key=lambda e: (-e["totals"]["protein"], -e["totals"]["density"]))

    ordered = sorted(meals.items(), key=lambda kv: MEAL_ORDER.get(kv[0], 9))
    return ordered, closed, len(ids)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

CSS = """
:root{
  --ground:#FAF7F0; --surface:#FFFFFF; --surface-2:#F4F0E6; --line:#E2DACA;
  --ink:#1A1712; --ink-2:#544C3E; --ink-3:#857B69;
  --gold:#8A6A17; --gold-fill:#CEB888; --gold-wash:#F6EFDC;
  --good:#2E6F52; --good-wash:#E2F0E7;
  --mid:#8A6317; --mid-wash:#F7EDD8;
  --low:#9B4436; --low-wash:#F8E6E2;
  --shadow:0 1px 2px rgba(26,23,18,.05), 0 8px 24px -12px rgba(26,23,18,.18);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ground:#131108; --surface:#1C1912; --surface-2:#252118; --line:#38321F;
    --ink:#F3EEE0; --ink-2:#B8AF98; --ink-3:#8B8270;
    --gold:#D9BC72; --gold-fill:#CEB888; --gold-wash:#2A2415;
    --good:#7FC49E; --good-wash:#1B2C23;
    --mid:#D3A74E; --mid-wash:#2C2415;
    --low:#DE8B7C; --low-wash:#2E1D19;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.7);
  }
}
:root[data-theme="dark"]{
  --ground:#131108; --surface:#1C1912; --surface-2:#252118; --line:#38321F;
  --ink:#F3EEE0; --ink-2:#B8AF98; --ink-3:#8B8270;
  --gold:#D9BC72; --gold-fill:#CEB888; --gold-wash:#2A2415;
  --good:#7FC49E; --good-wash:#1B2C23;
  --mid:#D3A74E; --mid-wash:#2C2415;
  --low:#DE8B7C; --low-wash:#2E1D19;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.7);
}

*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:ui-sans-serif,-apple-system,"SF Pro Text","Helvetica Neue",Arial,sans-serif;
  font-size:15px; line-height:1.5; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1080px; margin:0 auto; padding:32px 20px 72px; display:flex; flex-direction:column; gap:28px}
.num{font-variant-numeric:tabular-nums}

.masthead{display:flex; flex-wrap:wrap; align-items:baseline; gap:10px 16px; border-bottom:2px solid var(--ink); padding-bottom:12px}
.masthead h1{
  font-family:"Iowan Old Style",Georgia,ui-serif,"Times New Roman",serif;
  font-weight:600; font-size:30px; margin:0; letter-spacing:-.01em;
}
.masthead .date{color:var(--ink-2); font-size:14px}
.masthead .profile{
  margin-left:auto; font-size:11px; letter-spacing:.09em; text-transform:uppercase;
  color:var(--gold); border:1px solid var(--gold-fill); border-radius:2px; padding:3px 9px;
}

.tabs{display:flex; gap:4px; flex-wrap:wrap}
.tab{
  appearance:none; cursor:pointer; font:inherit; font-size:13px; font-weight:600;
  background:transparent; color:var(--ink-3); border:1px solid transparent;
  border-radius:3px; padding:7px 14px;
}
.tab:hover{color:var(--ink); background:var(--surface-2)}
.tab[aria-selected="true"]{background:var(--ink); color:var(--ground); border-color:var(--ink)}
.tab:focus-visible{outline:2px solid var(--gold); outline-offset:2px}
.tab .now{font-size:10px; letter-spacing:.08em; opacity:.75; margin-left:6px}

.panel[hidden]{display:none}
.panel{display:flex; flex-direction:column; gap:20px}

.verdict{
  background:var(--surface); border:1px solid var(--line); border-left:4px solid var(--gold-fill);
  border-radius:4px; padding:20px 22px; box-shadow:var(--shadow);
  display:flex; flex-wrap:wrap; gap:20px 36px; align-items:flex-start;
}
.verdict .lede{flex:1 1 260px; min-width:0}
.verdict .eyebrow{font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--ink-3)}
.verdict h2{
  font-family:"Iowan Old Style",Georgia,ui-serif,serif; font-weight:600;
  font-size:26px; margin:4px 0 2px; text-wrap:balance;
}
.verdict .sub{color:var(--ink-2); font-size:14px}
.stats{display:flex; gap:26px; flex-wrap:wrap}
.stat .k{font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--ink-3)}
.stat .v{font-size:26px; font-weight:600; font-family:ui-monospace,"SF Mono",Menlo,monospace; letter-spacing:-.02em}
.stat .u{font-size:13px; font-weight:400; color:var(--ink-2)}

.halls{display:flex; flex-direction:column; gap:14px}
.hall{background:var(--surface); border:1px solid var(--line); border-radius:4px; box-shadow:var(--shadow); overflow:hidden}
.hall.top{border-color:var(--gold-fill)}
.hall-head{display:flex; align-items:center; gap:14px; padding:14px 18px; border-bottom:1px solid var(--line); flex-wrap:wrap}
.rank{
  font-family:ui-monospace,"SF Mono",Menlo,monospace; font-size:12px; font-weight:600;
  width:24px; height:24px; display:grid; place-items:center; border-radius:2px;
  background:var(--surface-2); color:var(--ink-2); flex:none;
}
.hall.top .rank{background:var(--gold-fill); color:#1A1712}
.hall-head h3{margin:0; font-size:17px; font-weight:600; font-family:"Iowan Old Style",Georgia,ui-serif,serif}
.hall-head .hours{font-size:12px; color:var(--ink-3)}
.hall-head .macro{margin-left:auto; display:flex; gap:16px; font-size:13px; color:var(--ink-2)}
.hall-head .macro b{color:var(--ink); font-family:ui-monospace,"SF Mono",Menlo,monospace; font-weight:600}

.plate{padding:6px 18px 16px}
.plate-label{font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--ink-3); padding:12px 0 8px}
table{width:100%; border-collapse:collapse; font-size:13.5px}
.scroll{overflow-x:auto}
th{
  text-align:right; font-size:10.5px; letter-spacing:.07em; text-transform:uppercase;
  color:var(--ink-3); font-weight:600; padding:0 0 6px; border-bottom:1px solid var(--line); white-space:nowrap;
}
th.l,td.l{text-align:left}
td{padding:8px 0; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap;
   font-family:ui-monospace,"SF Mono",Menlo,monospace; font-variant-numeric:tabular-nums}
td.l{font-family:inherit; white-space:normal}
tr:last-child td{border-bottom:none}
th+th,td+td{padding-left:14px}
.item-name{font-weight:500}
.item-meta{font-size:11.5px; color:var(--ink-3); margin-top:1px}
.leaf{color:var(--good); font-size:11px}

.chip{
  display:inline-block; font-size:10.5px; font-weight:600; letter-spacing:.05em;
  text-transform:uppercase; padding:2px 7px; border-radius:2px; white-space:nowrap;
}
.chip.excellent{background:var(--good-wash); color:var(--good)}
.chip.strong{background:var(--gold-wash); color:var(--gold)}
.chip.decent{background:var(--surface-2); color:var(--ink-2)}
.chip.weak{background:var(--low-wash); color:var(--low)}

.goal{font-size:12px; padding:10px 18px; border-top:1px solid var(--line); background:var(--surface-2); color:var(--ink-2)}
.goal.hit{color:var(--good)}
.goal.salty{background:var(--low-wash); color:var(--low)}
.qty{
  display:inline-block; background:var(--ink); color:var(--ground); font-size:10.5px;
  font-weight:700; padding:1px 5px; border-radius:2px; margin-right:2px; vertical-align:1px;
}

details{border-top:1px solid var(--line)}
summary{
  cursor:pointer; padding:11px 18px; font-size:12px; font-weight:600; color:var(--ink-2);
  letter-spacing:.03em; list-style:none;
}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸ "; color:var(--ink-3)}
details[open] summary::before{content:"▾ "}
summary:hover{color:var(--ink)}
summary:focus-visible{outline:2px solid var(--gold); outline-offset:-2px}
details .scroll{padding:0 18px 16px}

.note{font-size:12.5px; color:var(--ink-3); line-height:1.6}
.note code{font-family:ui-monospace,"SF Mono",Menlo,monospace; font-size:11.5px; background:var(--surface-2); padding:1px 5px; border-radius:2px}
.empty{background:var(--surface); border:1px dashed var(--line); border-radius:4px; padding:28px; text-align:center; color:var(--ink-3)}
.stale{background:var(--low-wash); color:var(--low); border:1px solid var(--low); border-radius:4px;
       padding:11px 16px; font-size:13px; font-weight:500}
.stale[hidden]{display:none}
@media (prefers-reduced-motion:reduce){*{animation:none !important; transition:none !important}}
"""


def esc(s):
    return html.escape(str(s))


def item_rows(items, limit=None):
    rows = []
    for it in (items[:limit] if limit else items):
        cls, label = tier(it["score"])
        qty = it.get("qty", 1)
        leaf = ' <span class="leaf" title="Vegetarian">◆</span>' if it["veg"] else ""
        serving = f' · {esc(it["serving"])}' if it["serving"] else ""
        mult = f'<span class="qty num">{qty}×</span> ' if qty > 1 else ""
        val = lambda k: it[k] * qty  # noqa: E731 - local formatting shorthand
        rows.append(f"""<tr>
<td class="l"><div class="item-name">{mult}{esc(it['name'])}{leaf}</div>
<div class="item-meta">{esc(it['station'])}{serving}</div></td>
<td>{val('calories'):.0f}</td><td>{val('protein'):.0f}g</td><td>{val('carbs'):.0f}g</td>
<td>{val('fat'):.0f}g</td><td>{val('fiber'):.0f}g</td><td>{val('sodium'):.0f}</td>
<td><span class="chip {cls}">{label}</span></td></tr>""")
    return "\n".join(rows)


HEAD_ROW = ('<tr><th class="l">Item</th><th>Cal</th><th>Protein</th><th>Carb</th>'
            '<th>Fat</th><th>Fiber</th><th>Sodium</th><th>Grade</th></tr>')


def render_hall(entry, rank, cfg):
    t = entry["totals"]
    top = " top" if rank == 1 else ""
    goal_cls = "goal hit" if t["hits_goal"] else "goal"
    goal_txt = (f"Clears your {cfg['protein_goal']}g protein target for this meal."
                if t["hits_goal"] else
                f"{cfg['protein_goal'] - t['protein']}g short of your {cfg['protein_goal']}g target. "
                f"nothing else on the menu closes the gap inside {cfg['plate_kcal']} cal.")
    if t["salty"]:
        goal_cls = "goal salty"
        goal_txt += (f" Sodium is {t['sodium']}mg, over the {SODIUM_CAP_PER_MEAL}mg "
                     f"ceiling for one meal.")
    if t["blocked"]:
        b = t["blocked"]
        goal_txt += (f" Held back: {b['name']} has {b['protein']:.0f}g protein but "
                     f"{b['sodium']:.0f}mg sodium on its own.")
    return f"""<article class="hall{top}">
<div class="hall-head">
  <span class="rank num">{rank}</span>
  <h3>{esc(entry['hall'])}</h3>
  <span class="hours num">{esc(entry['hours'])}</span>
  <div class="macro">
    <span><b class="num">{t['protein']}g</b> protein</span>
    <span><b class="num">{t['calories']}</b> cal</span>
    <span><b class="num">{t['density']}</b> g/100cal</span>
  </div>
</div>
<div class="plate">
  <div class="plate-label">Build this tray</div>
  <div class="scroll"><table><thead>{HEAD_ROW}</thead><tbody>
{item_rows(entry['plate'])}
  </tbody></table></div>
</div>
<div class="{goal_cls}">{esc(goal_txt)}</div>
<details><summary>All {len(entry['items'])} scored items at {esc(entry['hall'])}</summary>
<div class="scroll"><table><thead>{HEAD_ROW}</thead><tbody>
{item_rows(entry['items'])}
</tbody></table></div></details>
</article>"""


def render_panel(meal, entries, cfg, idx):
    if not entries:
        return f'<section class="panel" id="p{idx}" hidden><div class="empty">No {esc(meal.lower())} service today.</div></section>'
    best = entries[0]
    t = best["totals"]
    runners = ", ".join(e["hall"] for e in entries[1:3])
    sub = f"Best plate beats {runners} on protein at this calorie ceiling." if runners else "Only court serving this meal today."
    halls = "\n".join(render_hall(e, i + 1, cfg) for i, e in enumerate(entries))
    return f"""<section class="panel" id="p{idx}" hidden>
<div class="verdict">
  <div class="lede">
    <div class="eyebrow">{esc(meal)} · go here</div>
    <h2>{esc(best['hall'])}</h2>
    <div class="sub">{esc(best['hours'])}. {esc(sub)}</div>
  </div>
  <div class="stats">
    <div class="stat"><div class="k">Protein</div><div class="v num">{t['protein']}<span class="u">g</span></div></div>
    <div class="stat"><div class="k">Calories</div><div class="v num">{t['calories']}</div></div>
    <div class="stat"><div class="k">Density</div><div class="v num">{t['density']}<span class="u">g/100cal</span></div></div>
    <div class="stat"><div class="k">Sodium</div><div class="v num">{t['sodium']}<span class="u">mg · {t['sodium_pct']}% of your day</span></div></div>
  </div>
</div>
<div class="halls">{halls}</div>
</section>"""


def render_html(date_obj, ordered, closed, cfg, profile, item_count):
    tabs, panels, windows = [], [], []
    for idx, (meal, entries) in enumerate(ordered):
        tabs.append(f'<button class="tab" role="tab" aria-selected="false" aria-controls="p{idx}" '
                    f'data-i="{idx}">{esc(meal)}<span class="now"></span></button>')
        panels.append(render_panel(meal, entries, cfg, idx))
        start = entries[0]["start"] if entries else "00:00:00"
        end = entries[0]["end"] if entries else "00:00:00"
        windows.append({"i": idx, "s": start, "e": end})

    closed_note = (f" {esc(', '.join(closed))} published no menu for this date."
                   if closed else "")
    generated = dt.datetime.now().strftime("%a %b %-d, %-I:%M %p")

    return f"""<title>Purdue Macro Scout</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CSS}</style>
<div class="wrap">
  <header class="masthead">
    <h1>Purdue Macro Scout</h1>
    <span class="date num">{date_obj.strftime('%A, %B %-d, %Y')}</span>
    <span class="profile">{esc(profile)} profile</span>
  </header>

  <div class="stale" id="stale" hidden></div>
  <div class="tabs" role="tablist">{''.join(tabs)}</div>
  {''.join(panels)}

  <p class="note">
    Scored {item_count} menu items across Purdue's dining courts, straight from
    Purdue HFS's public menu feed.{closed_note}
    Grades weight protein per calorie first, credit fiber, and penalize sodium,
    saturated fat and added sugar. Trays cap at {cfg['plate_kcal']} cal and at most
    two picks per station. Numbers are one serving as listed.
    Refresh with <code>python3 purdue_macros.py</code> · generated {esc(generated)}.
  </p>
</div>
<script>
(function(){{
  // The page is generated once a day. If the Mac was asleep when the refresh
  // was due, say so instead of quietly serving a stale menu.
  var BAKED = "{date_obj.isoformat()}";
  var t = new Date(), today = t.getFullYear() + '-' +
    String(t.getMonth()+1).padStart(2,'0') + '-' + String(t.getDate()).padStart(2,'0');
  if (today !== BAKED) {{
    var el = document.getElementById('stale');
    el.textContent = 'This menu is from ' + BAKED + ', not today. Run ' +
      'python3 purdue_macros.py to refresh.';
    el.hidden = false;
  }}

  var W = {json.dumps(windows)};
  var tabs = [].slice.call(document.querySelectorAll('.tab'));
  function show(i){{
    tabs.forEach(function(t){{
      var on = +t.dataset.i === i;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      document.getElementById('p' + t.dataset.i).hidden = !on;
    }});
  }}
  var now = new Date(), mins = now.getHours()*60 + now.getMinutes(), pick = 0, best = null;
  function toMin(s){{ var p = s.split(':'); return +p[0]*60 + +p[1]; }}
  W.forEach(function(w){{
    var s = toMin(w.s), e = toMin(w.e);
    if (mins >= s && mins <= e) {{ pick = w.i; best = 'serving now'; }}
    else if (best === null && mins < s) {{ pick = w.i; best = 'up next'; }}
  }});
  if (best) {{
    var t = tabs.filter(function(x){{ return +x.dataset.i === pick; }})[0];
    if (t) t.querySelector('.now').textContent = best;
  }}
  show(pick);
  tabs.forEach(function(t){{ t.addEventListener('click', function(){{ show(+t.dataset.i); }}); }});
}})();
</script>"""


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Rank Purdue dining courts by macro quality.")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="YYYY-MM-DD (default: today)")
    ap.add_argument("--profile", default=PROFILE, choices=sorted(PROFILES), help=f"default: {PROFILE}")
    ap.add_argument("--out", default=str(HERE / "today.html"))
    ap.add_argument("--open", action="store_true", help="open the dashboard when done")
    args = ap.parse_args()

    date_obj = dt.date.fromisoformat(args.date)
    cfg = PROFILES[args.profile]

    ordered, closed, item_count = collect(args.date, cfg)
    if not ordered:
        print(f"No dining court published a menu for {args.date}.", file=sys.stderr)

    out = pathlib.Path(args.out)
    out.write_text(render_html(date_obj, ordered, closed, cfg, args.profile, item_count))

    (HERE / "latest.json").write_text(json.dumps({
        "date": args.date, "profile": args.profile,
        "meals": {m: [{"hall": e["hall"], "totals": e["totals"],
                       "plate": [f"{p['qty']}× {p['name']}" if p["qty"] > 1 else p["name"]
                                 for p in e["plate"]]} for e in es]
                  for m, es in ordered},
        "closed": closed,
    }, indent=2))

    for meal, entries in ordered:
        if entries:
            b = entries[0]
            print(f"{meal:10s} → {b['hall']:12s} {b['totals']['protein']}g protein / "
                  f"{b['totals']['calories']} cal")
    print(f"\nWrote {out}")

    if args.open:
        os.system(f'open "{out}"')


if __name__ == "__main__":
    main()
