#!/usr/bin/env python3
"""Stage 2 (deterministic variant): exhaustive hotel sourcing via the
OpenStreetMap Overpass API — zero LLM tokens, geographic enumeration instead
of ranked search results.

WHY THIS EXISTS
DuckDuckGo text search ranks by SEO and returns the same top-~10 per query no
matter how many hotels a city actually has, so DDG sourcing hits a hard
ceiling and re-fires collapse into duplicates (the 2026-07-16 eu-hotels run:
108 merged candidates from 141 Haiku agents). Overpass queries the map data
itself — "every element tagged tourism=hotel inside this box" — which is
exhaustive per location and gives a real completeness signal: once a city is
sourced, it is DONE, and the next fire moves to fresh cities (ledgered in
vault/lead-outreach/overpass-cities-fired.txt). This implements the
region-by-region outreach model: exhaust one area, move to the next.

VALIDATED 2026-07-17 against the live public endpoint:
  - 6 sample cities (Athens, Barcelona, Dubrovnik, Istanbul, Prague, Vienna):
    2,356 hotel nodes, 727 with a usable website tag — more usable candidates
    from 6 cities than the entire prior 19-country DDG campaign produced
    (279 merged, ~200 Haiku dispatches).
  - Nodes alone MISS large hotels: big properties are usually drawn as
    building outlines (ways). Barcelona same-bbox: nodes-only 349 (162
    usable) vs nwr 515 (275 usable, +70%), and 71% of starred ways are
    4-5 star — exactly the big-hotel ICP. Hence `nwr` + `out center` below.
  - The public endpoint intermittently 429/504s (hit repeatedly during
    validation); retry + mirror failover below is mandatory, not paranoia.

OUTPUT CONTRACT
Writes candidates-batch-overpass-NNN.txt (one per city) in the run dir, in
the exact source-agent format `domain|BusinessName|COUNTRY_ISO2|vertical|0`,
so merge_candidates.py (glob: candidates-batch-*.txt) and every later stage
work unchanged. Only elements with BOTH a name and a website/contact:website
tag are emitted (downstream needs a domain); dedup + sent-log/sourced-log
nets still run at merge as usual.

RUN SELECTION (region-by-region, self-pacing)
  --cities "A,B,C"   explicit list
  --region "Spain"   one region group from REGIONS
  --auto             next cities not yet in the city ledger, in REGIONS
                     order, until --max-candidates (default 350) usable
                     candidates are collected. Completed cities are ledgered;
                     the city where the cap landed is NOT ledgered unless it
                     finished. This keeps each run's fetch/enrich stages the
                     same size as a normal run while every fire covers fresh
                     ground — no rank ceiling, no duplicate ground.
The city ledger can be bypassed for a re-sweep with --ignore-ledger (domain
dedup at merge still protects against re-contacting anyone).

GENERIC ACROSS CAMPAIGNS (the chain is ALWAYS the same)
This script is not hotel-specific. A new campaign opts in with, in its
templates/<base>/ fixture:
  source_agent.txt      overpass
  overpass_selector.txt one OSM tag selector, e.g. "office"="lawyer" or
                        "amenity"~"clinic|dentist"   (default: "tourism"="hotel")
  overpass_vertical.txt the vertical string for the candidate lines, e.g.
                        law / clinic / salon          (default: hotel)
  overpass_places.txt   optional own place list, one per line:
                        City Name|ISO2|lat|lon|half_width_deg
                        (default: the built-in EU/adjacent CITIES table below)
The fired-city ledger is keyed per-vertical, so a us-law-firms sweep of
Chicago never blocks a future us-clinics sweep of Chicago.

Coverage caveat (probed live 2026-07-17): OSM website-tag coverage varies
hard by vertical — US law firms 44/45 elements had a website (better than
hotels); US clinics 20/201, Beirut clinics 4/35, Dubai salons 25/347. For
weak-coverage verticals the DDG source-agent path stays the better Stage 2
method — and that's a one-line choice in source_agent.txt, with the rest of
the chain identical either way.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
CITY_LEDGER = PROJECT / "vault" / "lead-outreach" / "overpass-cities-fired.txt"
SENT_LOG = PROJECT / "vault" / "lead-outreach" / "sent-log.md"

# Shared dedup nets (sent-log + sourced-log), same code Stage 3 uses. Applied
# HERE so --max-candidates counts ONLY never-seen domains (user directive
# 2026-07-17): sourcing keeps opening cities until a full FRESH batch is
# collected, instead of burning cap slots on domains merge would drop anyway.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_candidates as _mc

# EVERY MIRROR HERE MUST CARRY THE WHOLE PLANET.
#
# A REGIONAL instance is worse than a dead one. A dead mirror fails, the city is
# not ledgered, and the next fire retries it. A regional mirror answers "200 OK,
# 0 elements" for anywhere outside its region, which reads as "this city has no
# businesses" and — before the guard added below — permanently ledgered the city
# as swept. Ground destroyed on the strength of a mirror's blind spot.
#
# overpass.osm.ch is the trap: it responds fast and looks healthy. Measured
# 2026-08-03 — Zurich: 490 offices, Dubai: 0. It is Switzerland-only. Do not add
# it, or any other national instance.
#
# Verify a candidate before adding it, against a city this system actually
# targets, and require a NON-ZERO count:
#   curl -s --max-time 50 -d '[out:json][timeout:40];nwr["office"](25.15,55.20,25.30,55.35);out count;' <URL>
#
# Status 2026-08-03: overpass-api.de, private.coffee and kumi.systems were all
# failing; the two added below returned 654 Dubai offices each. Ordering is
# deliberate — the ones that were actually up go first.
OVERPASS_MIRRORS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "lead-outreach-system-source-overpass/1.0 (internal tool, low volume)"

# city -> (ISO2, lat, lon, half-width of bbox in degrees).
# Approximate city centers; boxes cover the hotel core, not the metro sprawl.
CITIES: dict[str, tuple[str, float, float, float]] = {
    # Spain
    "Barcelona": ("ES", 41.385, 2.170, 0.07), "Madrid": ("ES", 40.417, -3.703, 0.07),
    "Seville": ("ES", 37.389, -5.984, 0.05), "Marbella": ("ES", 36.510, -4.886, 0.04),
    "Malaga": ("ES", 36.721, -4.421, 0.05), "Palma de Mallorca": ("ES", 39.570, 2.650, 0.05),
    "Tenerife (Santa Cruz)": ("ES", 28.469, -16.254, 0.06), "Las Palmas Gran Canaria": ("ES", 28.123, -15.436, 0.05),
    "Ibiza Town": ("ES", 38.907, 1.432, 0.04), "Mahon Menorca": ("ES", 39.889, 4.264, 0.04),
    "Bilbao": ("ES", 43.263, -2.935, 0.05), "San Sebastian": ("ES", 43.318, -1.981, 0.04),
    "Valencia": ("ES", 39.470, -0.377, 0.06), "Granada": ("ES", 37.177, -3.599, 0.04),
    "Toledo": ("ES", 39.858, -4.027, 0.03), "Salamanca": ("ES", 40.965, -5.664, 0.03),
    "Santander": ("ES", 43.462, -3.810, 0.04), "Arrecife Lanzarote": ("ES", 28.964, -13.548, 0.05),
    "Puerto del Rosario Fuerteventura": ("ES", 28.500, -13.862, 0.05), "Girona": ("ES", 41.983, 2.825, 0.04),
    "Murcia": ("ES", 37.987, -1.130, 0.04), "Zaragoza": ("ES", 41.649, -0.889, 0.05),
    # Italy
    "Rome": ("IT", 41.903, 12.496, 0.08), "Florence": ("IT", 43.769, 11.256, 0.05),
    "Venice": ("IT", 45.440, 12.316, 0.05), "Milan": ("IT", 45.464, 9.190, 0.07),
    "Positano": ("IT", 40.628, 14.485, 0.03), "Sorrento": ("IT", 40.627, 14.376, 0.03),
    "Capri": ("IT", 40.550, 14.243, 0.03), "Taormina": ("IT", 37.852, 15.286, 0.03),
    "Palermo": ("IT", 38.116, 13.361, 0.05), "Catania": ("IT", 37.502, 15.087, 0.05),
    "Siena": ("IT", 43.319, 11.331, 0.03), "Lucca": ("IT", 43.844, 10.503, 0.03),
    "Como": ("IT", 45.810, 9.086, 0.04), "Garda": ("IT", 45.578, 10.640, 0.06),
    "Cagliari Sardinia": ("IT", 39.223, 9.122, 0.05), "Lecce": ("IT", 40.353, 18.172, 0.04),
    "Bari": ("IT", 41.125, 16.867, 0.05), "Matera": ("IT", 40.667, 16.606, 0.03),
    "Bologna": ("IT", 44.494, 11.343, 0.05), "Verona": ("IT", 45.438, 10.993, 0.05),
    "Turin": ("IT", 45.070, 7.687, 0.06), "Genoa": ("IT", 44.407, 8.934, 0.05),
    "Portofino": ("IT", 44.303, 9.210, 0.02), "Rimini": ("IT", 44.059, 12.568, 0.05),
    "Cortina d'Ampezzo": ("IT", 46.540, 12.136, 0.03), "Courmayeur": ("IT", 45.793, 6.968, 0.03),
    "Trieste": ("IT", 45.649, 13.777, 0.04), "Ravenna": ("IT", 44.417, 12.199, 0.04),
    "Grosseto (Maremma)": ("IT", 42.760, 11.114, 0.05), "Naples": ("IT", 40.852, 14.268, 0.06),
    "Ischia": ("IT", 40.734, 13.947, 0.03),
    # France
    "Paris": ("FR", 48.857, 2.352, 0.09), "Nice": ("FR", 43.710, 7.262, 0.05),
    "Cannes": ("FR", 43.552, 7.017, 0.03), "Monaco": ("MC", 43.738, 7.425, 0.02),
    "Saint-Tropez": ("FR", 43.267, 6.639, 0.02), "Marseille": ("FR", 43.296, 5.370, 0.06),
    "Lyon": ("FR", 45.764, 4.836, 0.06), "Bordeaux": ("FR", 44.838, -0.579, 0.05),
    "Toulouse": ("FR", 43.605, 1.444, 0.05), "Chamonix": ("FR", 45.923, 6.869, 0.03),
    "Courchevel": ("FR", 45.415, 6.635, 0.02), "Megeve": ("FR", 45.857, 6.617, 0.02),
    "Val d'Isere": ("FR", 45.448, 6.980, 0.02), "Strasbourg": ("FR", 48.583, 7.745, 0.05),
    "Colmar": ("FR", 48.080, 7.359, 0.03), "Reims": ("FR", 49.258, 4.032, 0.04),
    "Biarritz": ("FR", 43.483, -1.559, 0.03), "Tours (Loire Valley)": ("FR", 47.394, 0.689, 0.05),
    "Deauville": ("FR", 49.359, 0.075, 0.02), "Ajaccio": ("FR", 41.919, 8.739, 0.04),
    "Porto-Vecchio": ("FR", 41.591, 9.280, 0.03), "Saint-Malo": ("FR", 48.649, -2.026, 0.03),
    "Aix-en-Provence": ("FR", 43.529, 5.447, 0.04), "Lille": ("FR", 50.629, 3.058, 0.05),
    "Rennes": ("FR", 48.117, -1.678, 0.05),
    # Portugal
    "Lisbon": ("PT", 38.722, -9.139, 0.07), "Porto": ("PT", 41.149, -8.611, 0.06),
    "Albufeira": ("PT", 37.089, -8.250, 0.03), "Lagos": ("PT", 37.102, -8.673, 0.03),
    "Funchal Madeira": ("PT", 32.650, -16.909, 0.04), "Ponta Delgada Azores": ("PT", 37.741, -25.668, 0.03),
    "Sintra": ("PT", 38.797, -9.390, 0.03), "Cascais": ("PT", 38.697, -9.422, 0.03),
    "Coimbra": ("PT", 40.203, -8.410, 0.04), "Braga": ("PT", 41.545, -8.427, 0.04),
    "Evora": ("PT", 38.571, -7.909, 0.03), "Peso da Regua (Douro)": ("PT", 41.163, -7.789, 0.04),
    "Guimaraes": ("PT", 41.444, -8.296, 0.03),
    # Greece
    "Athens": ("GR", 37.984, 23.728, 0.06), "Santorini (Thira)": ("GR", 36.417, 25.432, 0.03),
    "Mykonos": ("GR", 37.445, 25.328, 0.03), "Rhodes Town": ("GR", 36.451, 28.227, 0.03),
    "Kos Town": ("GR", 36.894, 27.288, 0.03), "Heraklion Crete": ("GR", 35.339, 25.132, 0.04),
    "Chania Crete": ("GR", 35.516, 24.018, 0.03), "Elounda Crete": ("GR", 35.257, 25.727, 0.02),
    "Corfu Town": ("GR", 39.625, 19.922, 0.03), "Zakynthos Town": ("GR", 37.787, 20.897, 0.03),
    "Nafplio": ("GR", 37.567, 22.803, 0.02), "Pylos (Costa Navarino)": ("GR", 36.914, 21.696, 0.04),
    "Thessaloniki": ("GR", 40.640, 22.944, 0.06), "Halkidiki": ("GR", 40.150, 23.500, 0.08),
    # Germany
    "Munich": ("DE", 48.137, 11.576, 0.07), "Berlin": ("DE", 52.520, 13.405, 0.08),
    "Frankfurt": ("DE", 50.111, 8.682, 0.05), "Hamburg": ("DE", 53.551, 9.994, 0.06),
    "Baden-Baden": ("DE", 48.760, 8.240, 0.03), "Garmisch-Partenkirchen": ("DE", 47.492, 11.096, 0.03),
    "Cologne": ("DE", 50.938, 6.960, 0.06), "Dusseldorf": ("DE", 51.228, 6.773, 0.05),
    "Heidelberg": ("DE", 49.398, 8.673, 0.03), "Freiburg": ("DE", 47.999, 7.842, 0.04),
    "Dresden": ("DE", 51.051, 13.738, 0.05), "Nuremberg": ("DE", 49.453, 11.077, 0.05),
    "Stuttgart": ("DE", 48.776, 9.182, 0.05), "Leipzig": ("DE", 51.340, 12.375, 0.05),
    # Austria
    "Vienna": ("AT", 48.208, 16.373, 0.06), "Salzburg": ("AT", 47.810, 13.045, 0.04),
    "Kitzbuhel": ("AT", 47.446, 12.392, 0.02), "Innsbruck": ("AT", 47.269, 11.404, 0.04),
    "Zell am See": ("AT", 47.325, 12.795, 0.02), "Graz": ("AT", 47.071, 15.439, 0.05),
    "Linz": ("AT", 48.306, 14.286, 0.04),
    # Switzerland
    "Zurich": ("CH", 47.377, 8.541, 0.05), "Geneva": ("CH", 46.204, 6.143, 0.05),
    "Zermatt": ("CH", 46.020, 7.749, 0.02), "St Moritz": ("CH", 46.498, 9.838, 0.02),
    "Interlaken": ("CH", 46.686, 7.864, 0.02), "Lucerne": ("CH", 47.050, 8.309, 0.04),
    "Montreux": ("CH", 46.434, 6.911, 0.03),
    # Benelux
    "Amsterdam": ("NL", 52.370, 4.895, 0.06), "Rotterdam": ("NL", 51.924, 4.478, 0.05),
    "Brussels": ("BE", 50.850, 4.352, 0.06), "Bruges": ("BE", 51.209, 3.225, 0.03),
    "Ghent": ("BE", 51.054, 3.717, 0.04), "Antwerp": ("BE", 51.221, 4.400, 0.05),
    "Luxembourg City": ("LU", 49.611, 6.132, 0.03),
    # Croatia / Balkans / Slovenia
    "Dubrovnik": ("HR", 42.640, 18.108, 0.04), "Split": ("HR", 43.508, 16.440, 0.05),
    "Hvar Town": ("HR", 43.172, 16.442, 0.02), "Zadar": ("HR", 44.117, 15.232, 0.04),
    "Pula": ("HR", 44.869, 13.848, 0.04), "Rovinj": ("HR", 45.081, 13.638, 0.03),
    "Makarska": ("HR", 43.297, 17.017, 0.03), "Kotor": ("ME", 42.424, 18.771, 0.03),
    "Budva": ("ME", 42.286, 18.840, 0.03), "Ljubljana": ("SI", 46.056, 14.505, 0.05),
    "Bled": ("SI", 46.369, 14.114, 0.02),
    # UK / Ireland
    "London": ("GB", 51.507, -0.128, 0.10), "Edinburgh": ("GB", 55.953, -3.189, 0.05),
    "Glasgow": ("GB", 55.861, -4.251, 0.05), "Manchester": ("GB", 53.480, -2.242, 0.05),
    "Birmingham": ("GB", 52.487, -1.891, 0.05), "Bath": ("GB", 51.381, -2.360, 0.03),
    "Dublin": ("IE", 53.350, -6.260, 0.06), "Cork": ("IE", 51.898, -8.475, 0.04),
    "Galway": ("IE", 53.271, -9.056, 0.03), "Killarney": ("IE", 52.059, -9.508, 0.03),
    "Belfast": ("GB", 54.597, -5.930, 0.05),
    # Nordics
    "Copenhagen": ("DK", 55.676, 12.568, 0.06), "Stockholm": ("SE", 59.329, 18.069, 0.06),
    "Gothenburg": ("SE", 57.709, 11.974, 0.05), "Malmo": ("SE", 55.605, 13.000, 0.05),
    "Oslo": ("NO", 59.913, 10.752, 0.06), "Bergen": ("NO", 60.393, 5.324, 0.04),
    "Helsinki": ("FI", 60.169, 24.938, 0.06), "Tampere": ("FI", 61.498, 23.761, 0.04),
    "Reykjavik": ("IS", 64.146, -21.943, 0.05),
    # Eastern Europe
    "Prague": ("CZ", 50.088, 14.421, 0.06), "Budapest": ("HU", 47.498, 19.040, 0.06),
    "Warsaw": ("PL", 52.230, 21.011, 0.06), "Krakow": ("PL", 50.062, 19.938, 0.05),
    "Gdansk": ("PL", 54.352, 18.646, 0.04), "Wroclaw": ("PL", 51.108, 17.038, 0.05),
    "Bucharest": ("RO", 44.427, 26.104, 0.06), "Brasov": ("RO", 45.643, 25.588, 0.03),
    "Sinaia": ("RO", 45.351, 25.550, 0.02), "Cluj-Napoca": ("RO", 46.771, 23.623, 0.04),
    "Sibiu": ("RO", 45.797, 24.152, 0.03), "Bratislava": ("SK", 48.148, 17.107, 0.05),
    "Sofia": ("BG", 42.698, 23.319, 0.05),
    # Turkey
    "Istanbul": ("TR", 41.010, 28.960, 0.09), "Bodrum": ("TR", 37.034, 27.430, 0.03),
    "Antalya": ("TR", 36.884, 30.704, 0.05), "Cappadocia (Goreme)": ("TR", 38.643, 34.828, 0.04),
    "Izmir": ("TR", 38.423, 27.142, 0.05), "Ankara": ("TR", 39.925, 32.837, 0.05),
    # Mediterranean islands
    "Valletta Malta": ("MT", 35.898, 14.514, 0.04), "Gozo Malta": ("MT", 36.044, 14.246, 0.04),
    "Limassol Cyprus": ("CY", 34.684, 33.037, 0.04), "Paphos Cyprus": ("CY", 34.776, 32.424, 0.03),
    "Nicosia Cyprus": ("CY", 35.185, 33.382, 0.04),
}

# Region groups for --region / --auto ordering. Order = outreach priority:
# the original ES/IT/FR core first, then outward. --auto walks these in order,
# skipping ledgered cities, so consecutive fires naturally sweep region by
# region ("finish one area, move to the next").
REGIONS: dict[str, list[str]] = {
    "Spain": ["Barcelona", "Madrid", "Seville", "Marbella", "Malaga", "Palma de Mallorca",
              "Tenerife (Santa Cruz)", "Las Palmas Gran Canaria", "Ibiza Town", "Mahon Menorca",
              "Bilbao", "San Sebastian", "Valencia", "Granada", "Toledo", "Salamanca",
              "Santander", "Arrecife Lanzarote", "Puerto del Rosario Fuerteventura",
              "Girona", "Murcia", "Zaragoza"],
    "Italy": ["Rome", "Florence", "Venice", "Milan", "Positano", "Sorrento", "Capri",
              "Taormina", "Palermo", "Catania", "Siena", "Lucca", "Como", "Garda",
              "Cagliari Sardinia", "Lecce", "Bari", "Matera", "Bologna", "Verona",
              "Turin", "Genoa", "Portofino", "Rimini", "Cortina d'Ampezzo", "Courmayeur",
              "Trieste", "Ravenna", "Grosseto (Maremma)", "Naples", "Ischia"],
    "France": ["Paris", "Nice", "Cannes", "Monaco", "Saint-Tropez", "Marseille", "Lyon",
               "Bordeaux", "Toulouse", "Chamonix", "Courchevel", "Megeve", "Val d'Isere",
               "Strasbourg", "Colmar", "Reims", "Biarritz", "Tours (Loire Valley)",
               "Deauville", "Ajaccio", "Porto-Vecchio", "Saint-Malo", "Aix-en-Provence",
               "Lille", "Rennes"],
    "Portugal": ["Lisbon", "Porto", "Albufeira", "Lagos", "Funchal Madeira",
                 "Ponta Delgada Azores", "Sintra", "Cascais", "Coimbra", "Braga",
                 "Evora", "Peso da Regua (Douro)", "Guimaraes"],
    "Greece": ["Athens", "Santorini (Thira)", "Mykonos", "Rhodes Town", "Kos Town",
               "Heraklion Crete", "Chania Crete", "Elounda Crete", "Corfu Town",
               "Zakynthos Town", "Nafplio", "Pylos (Costa Navarino)", "Thessaloniki", "Halkidiki"],
    "Germany": ["Munich", "Berlin", "Frankfurt", "Hamburg", "Baden-Baden",
                "Garmisch-Partenkirchen", "Cologne", "Dusseldorf", "Heidelberg",
                "Freiburg", "Dresden", "Nuremberg", "Stuttgart", "Leipzig"],
    "Austria": ["Vienna", "Salzburg", "Kitzbuhel", "Innsbruck", "Zell am See", "Graz", "Linz"],
    "Switzerland": ["Zurich", "Geneva", "Zermatt", "St Moritz", "Interlaken", "Lucerne", "Montreux"],
    "Benelux": ["Amsterdam", "Rotterdam", "Brussels", "Bruges", "Ghent", "Antwerp", "Luxembourg City"],
    "Croatia-Balkans": ["Dubrovnik", "Split", "Hvar Town", "Zadar", "Pula", "Rovinj",
                        "Makarska", "Kotor", "Budva", "Ljubljana", "Bled"],
    "UK-Ireland": ["London", "Edinburgh", "Glasgow", "Manchester", "Birmingham", "Bath",
                   "Dublin", "Cork", "Galway", "Killarney", "Belfast"],
    "Nordics": ["Copenhagen", "Stockholm", "Gothenburg", "Malmo", "Oslo", "Bergen",
                "Helsinki", "Tampere", "Reykjavik"],
    "Eastern-Europe": ["Prague", "Budapest", "Warsaw", "Krakow", "Gdansk", "Wroclaw",
                       "Bucharest", "Brasov", "Sinaia", "Cluj-Napoca", "Sibiu",
                       "Bratislava", "Sofia"],
    "Turkey": ["Istanbul", "Bodrum", "Antalya", "Cappadocia (Goreme)", "Izmir", "Ankara"],
    "Mediterranean-Islands": ["Valletta Malta", "Gozo Malta", "Limassol Cyprus",
                              "Paphos Cyprus", "Nicosia Cyprus"],
}

WEBSITE_TAGS = ("website", "contact:website")
DOMAIN_STRIP_RE = re.compile(r"^(https?://)?(www\.)?")
# Aggregator/OTA domains sometimes stuffed into a hotel's website tag — a
# lead whose only site is a booking platform can't be fetched/enriched as
# its own business.
AGGREGATOR_DOMAINS = ("booking.com", "expedia.", "tripadvisor.", "hotels.com",
                      "airbnb.", "agoda.", "trivago.", "facebook.com", "instagram.com")

# Hosts that are never a business's OWN domain, applied to every website field a
# source hands us — Maps' `web_site` above all. Google Maps listings routinely
# point at a social profile, a link-in-bio page, a menu/booking SaaS tenant or
# a plain wrong site: on the 2026-08-31 and 2026-09-01 gcc-receptionist fires
# 16% of QUALIFIED leads carried a domain that could not be the business
# (flynas.com for a men's salon, google.com and tiktok.com for barbers,
# qrcodechimp.com for a car rental, 6lb.menu / easymenu.site / taker.io /
# rekaz.io for restaurants and salons). Each cost a fetch and a name-finder
# and then retired the WRONG domain to disqualified-log forever. Mirrors
# resolve_domains.DIRECTORY (which guards the search path) plus the platform
# hosts that only show up through Maps.
NOT_OWN_DOMAIN_RE = re.compile(
    r"^(facebook|fb|instagram|twitter|x|linkedin|youtube|tiktok|pinterest|"
    r"threads|telegram|snapchat|reddit|whatsapp|google|bing|yandex|apple|"
    r"wikipedia|tripadvisor|yelp|foursquare|booking|agoda|expedia|airbnb|"
    r"justdial|yellowpages|saudiayp|eyeofriyadh|sehaguide|daleli|dalilnet|"
    r"vymaps|near-place|mapcarta|openstreetmap|numbeo|olx|amazon|noon|alibaba|"
    r"indeed|glassdoor|bayt|altibbi|vezeeta|okadoc|practo|zocdoc|healthgrades|"
    r"fresha|booksy|treatwell|vagaro|calendly|setmore|simplybook|zenoti|"
    r"linktr|linktree|heylink|lnk|linkfly|beacons|carrd|taplink|"
    r"6lb|easymenu|taker|rekaz|qrcodechimp|foodics|talabat|hungerstation|"
    r"jahez|deliveroo|ubereats|zomato|careem|mrsool|toyou|"
    r"blogspot|wordpress|wix|wixsite|weebly|godaddysites|squarespace|"
    r"webflow|notion|canva|behance|t|wa|goo)\.[a-z]{2,}(\.[a-z]{2})?$"
    r"|\.gov(\.|$)|\.edu(\.|$)", re.I)


def _registrable(domain: str) -> str:
    """`menu.raclette.ae` -> `raclette.ae`, `kokoro.my.taker.io` -> `taker.io`,
    `clinic.com.sa` -> `clinic.com.sa`. The platform net above is matched on
    THIS, so a business's own subdomain is never mistaken for a platform and a
    platform tenant's subdomain never hides the platform."""
    parts = [p for p in domain.lower().split(".") if p]
    if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "co", "gov", "edu", "med"} \
            and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain.lower()


def read_run_cfg(run: Path, name: str, default: str) -> str:
    p = run / name
    return p.read_text().strip() if p.exists() and p.read_text().strip() else default


def _domain_from_url(url: str) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    url = DOMAIN_STRIP_RE.sub("", url)
    domain = url.split("/", 1)[0].split("?", 1)[0].strip().lower()
    if not domain or "." not in domain or " " in domain:
        return None
    if any(agg in domain for agg in AGGREGATOR_DOMAINS):
        return None
    if NOT_OWN_DOMAIN_RE.search(_registrable(domain)):
        return None
    return domain


def query_overpass(bbox: tuple[float, float, float, float], selector: str,
                   timeout_s: int = 30) -> list[dict] | None:
    """bbox = (south, west, north, east); selector = one OSM tag filter like
    '"tourism"="hotel"' or '"amenity"~"clinic|dentist"'. Returns elements, or
    None if ALL attempts failed (caller must NOT ledger the city —
    distinguishable from a legitimately empty box). `nwr` covers nodes + ways
    + relations; big venues are usually mapped as building ways, so node-only
    misses them (validated: Barcelona hotels 162 -> 275 usable, +70%).
    Retries with backoff across two mirrors — the public endpoint 429/504s
    routinely."""
    south, west, north, east = bbox
    query = (f"[out:json][timeout:{timeout_s - 5}];"
             f"nwr[{selector}]({south},{west},{north},{east});out center;")
    last_err = "unknown"
    for mirror in OVERPASS_MIRRORS:
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    mirror, data=f"data={query}".encode(),
                    headers={"User-Agent": USER_AGENT,
                             "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    return json.loads(resp.read()).get("elements", [])
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
            except Exception as e:
                last_err = str(e)[:120]
            time.sleep(4 + attempt * 5)
    print(f"    WARN: all mirrors/attempts failed ({last_err})", file=sys.stderr)
    return None


def load_city_ledger(vertical: str) -> set[str]:
    """Fired cities for THIS vertical. Rows are `vertical|city|date|run` —
    keyed per-vertical so e.g. a law-firms sweep of a city never blocks a
    clinics sweep of the same city. (Legacy 3-field rows, if any, are treated
    as vertical `hotel`.)"""
    if not CITY_LEDGER.exists():
        return set()
    fired = set()
    for line in CITY_LEDGER.read_text().splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 4 and parts[0] == vertical:
            fired.add(parts[1])
        elif len(parts) == 3 and vertical == "hotel":
            fired.add(parts[0])
    return fired


def ledger_city(vertical: str, city: str, run_slug: str) -> None:
    CITY_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with CITY_LEDGER.open("a") as f:
        f.write(f"{vertical}|{city}|{date.today().isoformat()}|{run_slug}\n")


def load_places_file(path: Path) -> dict[str, tuple[str, float, float, float]]:
    """Per-campaign place list: `City Name|ISO2|lat|lon|half_width_deg`."""
    places: dict[str, tuple[str, float, float, float]] = {}
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 5:
            sys.exit(f"ABORT: {path}:{n} — expected `City|ISO2|lat|lon|half_width`, got: {line}")
        try:
            places[parts[0].strip()] = (parts[1].strip().upper(),
                                        float(parts[2]), float(parts[3]), float(parts[4]))
        except ValueError:
            sys.exit(f"ABORT: {path}:{n} — lat/lon/half_width must be numeric: {line}")
    if not places:
        sys.exit(f"ABORT: {path} contains no places.")
    return places


def candidates_for_city(city: str, places: dict, selector: str, vertical: str,
                        blocked_domains: set[str], blocked_slugs: set[str],
                        capture_unresolved: bool = False,
                        ) -> tuple[list[str], int, list[str]] | None:
    """None = query failed (do not ledger); (lines, n_already_seen, unresolved) otherwise.

    Already-seen domains (sent-log or sourced-log) are skipped at the source so
    they never count against the fresh-candidate cap.

    `capture_unresolved`: OSM knows the ICP far better than it knows websites —
    in Arabic-speaking markets most elements carry a correct name and no website
    tag at all (Riyadh clinics 2026-07-27: 61 named, 7 with a website). Those
    name-only rows are returned as `Name|ISO2|City|vertical` for Stage 2.5
    (resolve_domains.py) to resolve and VERIFY, instead of being silently
    dropped here. Off by default, so a campaign that does not opt in behaves
    exactly as before."""
    country, lat, lon, r = places[city]
    elements = query_overpass((lat - r, lon - r, lat + r, lon + r), selector)
    if elements is None:
        return None
    lines, seen = [], set()
    unresolved: list[str] = []
    already_seen = 0
    for el in elements:
        tags = el.get("tags", {}) or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        domain = None
        for tag in WEBSITE_TAGS:
            domain = _domain_from_url(tags.get(tag, ""))
            if domain:
                break
        if not domain:
            if capture_unresolved:
                unresolved.append(f"{name.replace('|', ' ')}|{country}|{city}|{vertical}")
            continue
        if domain in seen:
            continue
        seen.add(domain)
        norm = _mc._norm_domain(domain)
        if norm in blocked_domains or _mc._slugify(norm) in blocked_slugs:
            already_seen += 1
            continue
        lines.append(f"{domain}|{name.replace('|', ' ')}|{country}|{vertical}|0")
    return lines, already_seen, unresolved


def sweep_outcome(total: int, total_unresolved: int, cities_completed: int,
                  skipped_seen: int, empty_cities: int = 0) -> tuple[int, str]:
    """Decide how a finished Overpass sweep should exit. -> (exit_code, message).

    exit 0 = produced yield; exit 8 = no fresh ground (an expected end state, so
    a run's OTHER sources still carry it).

    This mirrors `sweep_outcome` in source_places.py, and exists because it
    previously did not. The old code was `if not total: sys.exit("ABORT: ...")`,
    and `sys.exit(<str>)` exits **1**, which run_fire.py does not tolerate
    (it tolerates only 8). Two consequences, both real:

      * `total` counts ONLY website-tagged rows. The name-only `unresolved`
        rows this sweep just wrote for Stage 2.5 to resolve were ignored, so a
        target returning 300 correctly-named businesses with no `website` tag
        killed the entire fire and threw away the file it had just produced.
        Measured on 2026-08-09-au-trades: 50 website-tagged rows against 1,217
        unresolved — that run was carried almost entirely by name-only rows.
      * "every city already swept" is the textbook no-fresh-ground state that
        exit 8 exists for, and it was also being reported as a hard failure.

    NO DRY SWEEP EXITS 1 (2026-08-11). A sweep that returns nothing used to halt
    the fire on the theory that an all-empty result proves a malformed selector.
    It does not — it is equally what a sparse vertical or an out-of-region mirror
    returns, and the caller cannot tell those apart. Meanwhile the cost of being
    wrong was total: on 2026-08-11-au-trades, source [2/4] exited 1 over five
    empty AU cities and killed the run before the Google Places source at [4/4]
    — the one source that had ground — ever executed. The suspicion is still
    worth printing, so it survives as a WARNING on an exit-8 path. A run where
    EVERY source is dry still halts, at run_fire.py's Stage 3 "0 candidates after
    dedup" gate, which is the gate that can actually see the whole picture.
    """
    if total or total_unresolved:
        return 0, ""
    if empty_cities and cities_completed == 0:
        return 8, (f"NO FRESH GROUND: attempted {empty_cities} city/cities and Overpass "
                   f"returned nothing for each. Not ledgered, so they are retried next "
                   f"fire. If this repeats for the same selector, check it is a valid "
                   f"OSM tag expression and that the mirrors are up.")
    if cities_completed == 0:
        return 8, ("NO FRESH GROUND: every requested city is already swept for this "
                   "vertical. Add rows to places.txt, widen the selector, or pass "
                   "--ignore-ledger.")
    return 8, (f"NO FRESH GROUND: swept {cities_completed} city/cities; all "
               f"{skipped_seen} businesses found were already sourced or contacted.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--cities", default="", help="comma-separated explicit city list")
    ap.add_argument("--region", default="", help="one REGIONS group name (built-in places only)")
    ap.add_argument("--auto", action="store_true",
                    help="next un-ledgered cities in order, up to --max-candidates")
    ap.add_argument("--max-candidates", type=int, default=350,
                    help="--auto stops opening new cities once this many usable candidates are collected")
    ap.add_argument("--ignore-ledger", action="store_true")
    ap.add_argument("--selector", default="",
                    help='OSM tag filter, e.g. \'"office"="lawyer"\' (normally passed by run_fire.py from sourcing.json)')
    ap.add_argument("--vertical", default="",
                    help="vertical string for the candidate lines (normally from sourcing.json)")
    ap.add_argument("--capture-unresolved", action="store_true",
                    help="also write name-only businesses (no website tag) to "
                         "unresolved-<prefix>-NNN.txt for Stage 2.5 domain resolution")
    ap.add_argument("--batch-prefix", default="overpass",
                    help="filename tag for this sweep's candidates-batch-<prefix>-NNN.txt. "
                         "Distinct per selector so a multi-vertical campaign (run_fire.py "
                         "calls this once per selector) never overwrites an earlier sweep.")
    a = ap.parse_args()
    run = Path(a.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    run_slug = run.resolve().name

    # Config precedence: CLI (run_fire.py passes sourcing.json values) ->
    # legacy per-file config in the run dir -> hotel defaults.
    selector = a.selector or read_run_cfg(run, "overpass_selector.txt", '"tourism"="hotel"')
    vertical = a.vertical or read_run_cfg(run, "overpass_vertical.txt", "hotel")
    places_file = run / "places.txt"
    if not places_file.exists():
        places_file = run / "overpass_places.txt"   # legacy name
    if places_file.exists():
        places = load_places_file(places_file)
        regions = {"ALL": list(places.keys())}
    else:
        places, regions = CITIES, REGIONS
        # GENERALITY GUARD: the built-in table is EU/adjacent only. When the
        # run declares its geography (countries.txt), restrict the sweep to it
        # — and if nothing overlaps, ABORT with instructions instead of
        # silently sweeping the wrong continent and having merge discard
        # everything (the run must work the same for ANY campaign, or fail
        # loudly saying exactly what to add).
        cfile = run / "countries.txt"
        if cfile.exists():
            wanted_cc = {c.strip().upper() for c in cfile.read_text().splitlines()
                         if c.strip() and not c.startswith("#")}
            if wanted_cc and "*" not in wanted_cc and "ALL" not in wanted_cc:
                places = {city: v for city, v in places.items() if v[0] in wanted_cc}
                regions = {r: [c for c in cs if c in places] for r, cs in regions.items()}
                regions = {r: cs for r, cs in regions.items() if cs}
                if not places:
                    sys.exit("ABORT: map sourcing has no places for this campaign's "
                             "countries.txt — the built-in place table covers EU/adjacent "
                             "only. Add a places.txt to the fixture "
                             "(`City|ISO2|lat|lon|half_width_deg`, one per line).")
    print(f"Overpass config: selector={selector} vertical={vertical} "
          f"places={'custom:' + places_file.name if places_file.exists() else 'built-in EU table'} "
          f"({len(places)} places)")

    if a.cities:
        wanted = [c.strip() for c in a.cities.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in places]
        if unknown:
            sys.exit(f"ABORT: unknown cities (not in the places table): {unknown}")
    elif a.region:
        if a.region not in regions:
            sys.exit(f"ABORT: unknown region '{a.region}'. Known: {', '.join(regions)}")
        wanted = regions[a.region]
    else:
        a.auto = True
        wanted = [c for cities in regions.values() for c in cities]

    fired = set() if a.ignore_ledger else load_city_ledger(vertical)
    todo = [c for c in wanted if c not in fired]
    if not todo:
        # EXIT 8 = "this source has no fresh ground left". Deliberately NOT the
        # generic failure code 1: an exhausted source is a known, expected end
        # state, and on a multi-source campaign the other sources must still run.
        # run_fire.py tolerates 8 per source and only halts if EVERY source is dry.
        #
        # Read this message literally before re-sweeping. Measured 2026-07-31,
        # `--ignore-ledger` does NOT create new leads here: OSM's supply for a
        # vertical is national, not per-city. The whole Australian continent holds
        # 450 elements matching craft~plumber|electrician|hvac, 159 with a website,
        # and au-trades had already contacted its way through them — which is why
        # its last fires decayed 51 -> 4 -> 20 -> 5 candidates. Re-sweeping the
        # same ground returns domains merge will discard.
        print(f"NO FRESH GROUND: every requested city is already swept for vertical "
              f"'{vertical}' ({CITY_LEDGER.name}).\n"
              f"  What actually adds leads, in order of effect:\n"
              f"   1. Add a `places` source (Google Places API) to this fixture's "
              f"sourcing.json — it indexes the businesses OSM has no premises for:\n"
              f'        {{"sources": [{{"type": "places", "targets": '
              f'[{{"included_type": "plumber", "vertical": "{vertical}"}}]}}]}}\n'
              f"   2. Add rows to places.txt to open genuinely new geography.\n"
              f"   3. --ignore-ledger only helps if OSM has been re-surveyed since; "
              f"domain dedup at merge still applies, so expect near-zero.\n"
              f"  If this is the run's only source, it HALTS at the Stage 3 gate — "
              f"there is no backlog to fall back on (removed 2026-08-05).",
              file=sys.stderr)
        sys.exit(8)

    # Fresh-only cap: load the sent-log + sourced-log nets once, so every
    # candidate counted against --max-candidates is a never-seen domain.
    blocked_domains = _mc.load_sent_domains(SENT_LOG) | \
        _mc.load_sourced_domains(_mc.sourced_log_path(SENT_LOG), run_slug)
    blocked_slugs = _mc.load_sent_slugs(SENT_LOG)
    print(f"Fresh-only cap: {len(blocked_domains)} previously-seen domains excluded at source.")

    total = 0
    total_unresolved = 0
    batch_n = 0
    skipped_seen = 0
    completed: list[str] = []
    failed: list[str] = []
    empty: list[str] = []      # queried fine, returned nothing -> not ledgered, not "completed"
    for city in todo:
        # Yield is website-tagged rows PLUS name-only rows, exactly as
        # sweep_outcome() judges it — source_places.py:478 already caps this way.
        # Counting only `total` meant the cap never tripped on a weakly-mapped
        # market: 2026-08-09-au-trades produced 50 website-tagged rows against
        # 1,217 name-only, so --max-candidates silently stopped capping and the
        # sweep walked every remaining city (a network round-trip plus a 1.5s
        # courtesy sleep each) while handing Stage 2.5 an unbounded resolve
        # backlog to pay for.
        if a.auto and (total + total_unresolved) >= a.max_candidates:
            break
        res = candidates_for_city(city, places, selector, vertical, blocked_domains,
                                  blocked_slugs, capture_unresolved=a.capture_unresolved)
        if res is None:
            failed.append(city)          # query failed -> NOT ledgered, retried next fire
            continue
        lines, n_seen, unresolved = res
        if unresolved:
            # Name-only businesses -> Stage 2.5 resolves + VERIFIES their domain.
            # These COUNT as yield (see sweep_outcome): on a weakly-mapped market
            # they are often the entire harvest, so the exit decision must see them.
            total_unresolved += len(unresolved)
            (run / f"unresolved-{a.batch_prefix}-{batch_n + 1:03d}.txt").write_text(
                "\n".join(unresolved) + "\n")
        skipped_seen += n_seen
        batch_n += 1
        out = run / f"candidates-batch-{a.batch_prefix}-{batch_n:03d}.txt"
        out.write_text("\n".join(lines) + ("\n" if lines else ""))
        total += len(lines)
        # LEDGER ONLY WHAT WAS GENUINELY SEARCHED.
        #
        # This ledger is permanent: a city written here is never swept again, so
        # a wrong entry destroys that ground for good. A city counts as done
        # only if OSM actually returned SOMETHING — fresh candidates,
        # already-seen domains, or name-only businesses for Stage 2.5.
        #
        # Zero of all three is not evidence of an empty city. It is what a
        # regional mirror returns for anywhere outside its region (see the
        # OVERPASS_MIRRORS note above), and what a partial or truncated
        # response looks like. Being wrong the safe way costs one repeated
        # query next fire; being wrong the other way costs the ground forever.
        #
        # `completed` uses the SAME definition, because sweep_outcome() reads it
        # to choose exit 8 ("no fresh ground", other sources carry the run) over
        # exit 1 ("selector is broken", halt the whole fire). Counting a city
        # that returned nothing as "searched" made those two disagree: the
        # ledger called the response meaningless while the exit code called it
        # proof of a bad selector. Measured on 2026-08-11-au-trades — five AU
        # cities returned nothing for craft~locksmith|glaziery|roofer, source
        # [2/4] exited 1, and the Google Places source at [4/4] never ran.
        if lines or n_seen or unresolved:
            completed.append(city)
            ledger_city(vertical, city, run_slug)
        else:
            empty.append(city)
            print(f"  [{batch_n}] {city}: OSM returned NOTHING — NOT ledgered "
                  f"(an empty response is not an empty city; retried next fire)")
            time.sleep(1.5)
            continue
        print(f"  [{batch_n}] {city} ({places[city][0]}): {len(lines)} fresh candidates "
              f"(+{n_seen} already-seen skipped; running total {total})")
        time.sleep(1.5)   # be a reasonable citizen of shared public infrastructure

    print(f"\nOverpass sourcing: {len(completed)} cities completed, {total} FRESH usable candidates -> {run}")
    if skipped_seen:
        print(f"  {skipped_seen} already-seen domains skipped at source (never counted against the cap).")
    if failed:
        print(f"  {len(failed)} cities failed all retries (NOT ledgered, will retry next fire): {failed}")
    if empty:
        print(f"  {len(empty)} cities returned an empty response (NOT ledgered, will retry next fire): {empty}")
    if total_unresolved:
        print(f"  {total_unresolved} name-only businesses captured for Stage 2.5 to resolve.")
    remaining = len([c for c in wanted if c not in fired and c not in completed and c not in failed])
    print(f"  {remaining} requested cities left un-sourced (future fires walk them in order).")
    code, msg = sweep_outcome(total, total_unresolved, len(completed), skipped_seen, len(empty))
    if code:
        print(msg)
        sys.exit(code)


if __name__ == "__main__":
    main()
