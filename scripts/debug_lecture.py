"""
Run: python debug_lecture.py <lecture_id>
Prints raw lectureUnits JSON for the given lecture ID so you can see
exactly what the API returns and why files aren't downloading.
"""
import json
import sys
import os
from pathlib import Path
from dotenv import load_dotenv
import connectors.artemis as artemis

load_dotenv()

lecture_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1951

session = artemis._make_session()
if not artemis._ensure_authenticated(session):
    sys.exit("Auth failed")

url = f"{artemis.BASE_URL}/api/lecture/lectures/{lecture_id}/details"
resp = session.get(url)
print(f"Status: {resp.status_code}  URL: {url}\n")

if resp.status_code != 200:
    print(resp.text[:500])
    sys.exit(1)

data = resp.json()
units = data.get("lectureUnits", [])
print(f"lectureUnits count: {len(units)}\n")

for i, unit in enumerate(units):
    utype = unit.get("type")
    uname = unit.get("name")
    uid   = unit.get("id")
    attachment = unit.get("attachment") or {}
    link = attachment.get("link") or unit.get("link")
    print(f"[{i}] type={utype!r}  id={uid}  name={uname!r}  link={link!r}")

print("\n--- Full JSON ---")
print(json.dumps(data, indent=2)[:4000])
