# Purdue Macro Scout

Ranks Purdue's dining courts every morning by the best plate you can actually
build at each one, and names the items worth walking there for.

Open **`~/purdue-macros/today.html`**. Bookmark it, or set it as your browser homepage.

## How it refreshes

A launchd agent regenerates the page at **6:30 AM daily**. If the Mac is asleep,
launchd runs it on wake. If the page is ever stale, it says so in red at the top.

```
launchctl list | grep purduemacros            # is it scheduled?
launchctl start com.jkapuya.purduemacros      # refresh right now
cat ~/purdue-macros/refresh.log               # what happened last run
```

Remove it entirely:

```
launchctl unload ~/Library/LaunchAgents/com.jkapuya.purduemacros.plist
rm ~/Library/LaunchAgents/com.jkapuya.purduemacros.plist
```

## Running it by hand

```
python3 purdue_macros.py                      # today, your default profile
python3 purdue_macros.py --profile bulk       # lean | maintain | bulk
python3 purdue_macros.py --date 2026-08-24    # any published day, including ahead
python3 purdue_macros.py --open               # write it and open it
```

## Tuning it

Edit the settings block at the top of `purdue_macros.py`:

| Setting | Does what |
| --- | --- |
| `PROFILE` | `lean` (650 cal / 45g protein per meal), `maintain` (850 / 55g), `bulk` (1150 / 70g) |
| `DIET` | `none`, `vegetarian`, or `vegan`, filters every item |
| `EXCLUDE_ALLERGENS` | e.g. `["Peanuts", "Shellfish"]`; items testing true are dropped |
| `SODIUM_CAP_PER_MEAL` | 0 by default, meaning off. Set it to 800 to force trays under a third of the daily limit |

The per-profile calorie ceilings and protein goals live in `PROFILES` if you
want different numbers.

## How items are scored

Grade is out of 100, built around **protein per 100 calories**. 12 g/100 cal is
the practical ceiling for dining-hall food. Grilled chicken and white fish land
there, eggs around 8, a bean burger around 6. Fiber adds up to 14 points.
Sodium above 250 mg/100 cal, saturated fat above 2 g/100 cal, and any added
sugar subtract.

The tray is built in two passes: **anchors** first (items with 8g+ protein,
ranked by grade), then **sides** to fill the calorie budget with the best of
whatever else is out. No item twice, at most two picks per station, and if the
tray falls short of the protein goal with calories to spare it goes back for a
second serving of its best anchor instead of adding another side.

Sauces, dressings, and shredded or grated cheeses are excluded from trays.
Grated parmesan is 11g protein per ounce, so it would otherwise dominate every
ranking.
They still appear in each hall's full item list.

## A note on sodium

Every tray shows its sodium as a share of the 2300mg daily limit, and some of
the best protein trays run over 100% of it in one meal. That is real, not a bug.
Carved ham, smoked pork chop, and boneless wings are the highest protein per
calorie items in a dining court, and they are cured or brined.

Forcing trays under a third of the daily limit cut the best plates from about
60g of protein down to 30g, so the cap ships off. Most of the sodium in a tray
sits in sauces, dressings, gravy, and queso, which are counted here only when
you take them. Skip those and the listed number overstates what you actually
eat. If you want the tray to enforce a limit anyway, set `SODIUM_CAP_PER_MEAL`
to 800.

## Data

Purdue HFS's public menu feed at `api.hfs.purdue.edu/menus/v2`, the same source
Purdue's own dining site uses. No key, no account, no rate limit. Per-item
nutrition is cached in `cache/nutrition.json` by item ID, so a normal day only
fetches the handful of dishes it hasn't seen before.

Hillenbrand and Windsor publish no menu outside the semester; they appear
automatically once they do.

**Numbers are one serving as Purdue lists it.** Serving size is shown under each
item, things like "1/2 Cup", "Ounce", "Each". Portion by eye and adjust.
