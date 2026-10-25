#!/usr/bin/env python3
"""GePNIC's health checks for the hub: every portal in data/gepnic_portals.json as target "gepnic:<portal>", plus "eprocure" (the CPPP
search page, which the eprocure-* projects name as their probe). The hub checks each URL every minute: up = HTTP 200 within 20 s whose
body contains `expect` (GePNIC: the search page's form, since the static front page keeps answering when the application is stuck;
GePNIC 2.0: the API's own health answer). Projects name these in settings.probe (pool_settings.py); while one fails the hub holds the work.

Apply with the family owner's rights: POST /api/families/gepnic/probes with this JSON. Usage: python probe_settings.py > probes.json
"""
import json

reg = json.load(open("data/gepnic_portals.json"))["portals"]
targets = [{"name": f"gepnic:{p['portal']}", "url": p["check_url"], "expect": "Server is up" if p.get("ui") == "gepnic2" else "<form"} for p in reg]
targets.append({"name": "eprocure", "url": "https://eprocure.gov.in/eprocure/app?page=WebTenderStatusLists&service=page", "expect": "<form"})
print(json.dumps({"targets": targets}, indent=1))
