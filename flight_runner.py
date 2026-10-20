"""
Flight Monitor runner (public repo, runs on GitHub Actions).

1. Asks your Google Sheet which Google Flights lookups to make.
2. Sends them to Google Flights and reads the fare options.
3. Sends the fare options back; the sheet compares prices and sends any alerts.

Needs two repository secrets: SHEET_URL (the sheet's web app /exec address) and
SHEET_KEY (from the sheet's menu: Flight Monitor > Show runner key).

This repo is public, so the log prints only counts: never flight numbers, prices,
the sheet address, or the key.
"""
import json
import os
import re
import sys
import time

import requests

SHEET_URL = os.environ.get("SHEET_URL", "").strip()
SHEET_KEY = os.environ.get("SHEET_KEY", "").strip()
GOOGLE_URL = ("https://www.google.com/_/FlightsFrontendUi/data/"
              "travel.frontend.flights.FlightsFrontendService/GetBookingResults?hl=en&gl=US&curr=USD")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
REDIRECTS = (301, 302, 303, 307, 308)
REFUSED_RE = re.compile(r'"wrb\.fr",null,null,null,null,\[(\d+)')


class SheetError(Exception):
    pass


# ---------- talking to the sheet ----------
def sheet_json(r):
    if r.status_code != 200:
        raise SheetError("the sheet answered HTTP %d" % r.status_code)
    try:
        return r.json()
    except ValueError:
        raise SheetError("the sheet didn't answer with data (check SHEET_URL ends in /exec and the "
                         "web app's access is 'Anyone')")


def call_sheet(payload):
    """Apps Script runs the script on the first request, then redirects to a one-time result
    page. The key travels in the body (never the URL), and only the result page is retried."""
    try:
        r = requests.post(SHEET_URL, data=json.dumps(payload), headers={"Content-Type": "text/plain"},
                          timeout=(15, 90), allow_redirects=False)
    except requests.RequestException as e:
        raise SheetError("couldn't reach the sheet (%s)" % type(e).__name__)
    if r.status_code not in REDIRECTS or not r.headers.get("Location"):
        return sheet_json(r)
    url = r.headers["Location"]
    for attempt in range(4):
        try:
            return sheet_json(requests.get(url, timeout=(10, 25)))
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
    raise SheetError("the sheet ran but its answer couldn't be downloaded after 4 tries")


# ---------- talking to Google Flights ----------
def norm_fare(s):
    s = re.sub(r"[\u00ae\u2122]", "", str(s or "").lower())
    s = re.sub(r"^delta\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_booking(text):
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("[") or "wrb.fr" not in line:
            continue
        try:
            outer = json.loads(line)
        except ValueError:
            continue
        for item in outer:
            if not isinstance(item, list) or len(item) < 3 or item[0] != "wrb.fr" or not isinstance(item[2], str):
                continue
            try:
                opts = json.loads(item[2])[1][0]
            except (ValueError, TypeError, IndexError, KeyError):
                continue
            for o in opts or []:
                try:
                    price = o[7][0][1]
                    if not isinstance(price, (int, float)):
                        continue
                    brand = (o[21][3] if o[21] else "") or ""
                    flights = " ".join(str(f[0]) + str(f[1]) for f in (o[3] or [])).upper()
                    seller = (o[1][0][0] if o[1] and o[1][0] else "") or ""
                    seller_name = (o[1][0][1] if o[1] and o[1][0] and len(o[1][0]) > 1 else seller) or seller
                    out.append({"brand": brand, "price": price, "flights": flights,
                                "seller": str(seller).upper()[:3], "sellerName": str(seller_name)[:60]})
                except (TypeError, IndexError, KeyError):
                    continue
    return out


def google_lookup(body):
    try:
        r = requests.post(GOOGLE_URL, data=body, timeout=(15, 45), allow_redirects=False, headers={
            "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    except requests.RequestException:
        return {"status": "neterror", "code": 0}
    text = r.text
    loc = r.headers.get("Location", "")
    if r.status_code == 429 or "/sorry/" in loc or "/sorry/" in text or "unusual traffic" in text.lower():
        return {"status": "blocked", "code": r.status_code}
    m = REFUSED_RE.search(text)
    if m and len(text) < 2000:
        return {"status": "refused", "code": int(m.group(1))}
    if r.status_code != 200:
        return {"status": "http", "code": r.status_code}
    return {"status": "ok", "code": 200, "options": parse_booking(text), "rawLength": len(text)}


def main():
    if not SHEET_URL or not SHEET_KEY:
        raise SheetError("missing the SHEET_URL or SHEET_KEY secret")
    data = call_sheet({"key": SHEET_KEY, "action": "tasks"})
    if not data.get("ok"):
        raise SheetError("the sheet refused the request: %s" % data.get("error"))
    tasks, token = data.get("tasks", []), data.get("runToken", "")
    results, counts, google_refusing = [], {}, False
    for t in tasks:
        res = {"row": t["row"], "sig": t["sig"], "lookups": []}
        if google_refusing:
            continue  # Google refuses in batches: don't send the rest this run
        for lk in t.get("lookups", []):
            got = google_lookup(lk["body"])
            res["lookups"].append(dict(got, seat=lk["seat"]))
            counts[got["status"]] = counts.get(got["status"], 0) + 1
            if got["status"] in ("refused", "blocked"):
                google_refusing = True
                break
            if got["status"] != "ok":
                continue
            if t.get("codeshare") or any(norm_fare(o["brand"]) == t.get("fare") and o["flights"] == t.get("wanted")
                                         for o in got["options"]):
                break  # found what the sheet needs; skip the other cabins
            time.sleep(1)
        results.append(res)
        time.sleep(2)
    out = call_sheet({"key": SHEET_KEY, "action": "results", "runToken": token, "results": results})
    if not out.get("ok"):
        raise SheetError("the sheet refused the results: %s" % out.get("error"))
    summary = ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items())) or "none"
    print("Trips: %d. Google lookups: %s. Sheet updated %s rows." % (len(tasks), summary, out.get("rows")))


if __name__ == "__main__":
    try:
        main()
    except SheetError as e:
        print("ERROR: %s" % e)
        sys.exit(1)
    except Exception as e:  # never print details that could include addresses
        print("ERROR: unexpected %s" % type(e).__name__)
        sys.exit(1)
