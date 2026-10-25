"""Download the official portable client and refresh numeric item IDs; no installers run."""
import gzip
import hashlib
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RELEASE = "0.1.61"
URL = f"https://github.com/ao-data/albiondata-client/releases/download/{RELEASE}/update-windows-amd64.exe.gz"
ITEMS = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.txt"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "AlbionLocal/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    vendor = ROOT / "vendor"
    vendor.mkdir(exist_ok=True)
    target = vendor / "albiondata-client.exe"
    if not target.exists():
        blob = fetch(URL)
        binary = gzip.decompress(blob)
        if not binary.startswith(b"MZ"):
            raise ValueError("Expected Windows executable")
        target.write_bytes(binary)
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    text = fetch(ITEMS).decode("utf-8-sig")
    mapping = {}
    for line in text.splitlines():
        match = re.match(r"\s*(\d+):\s*(\S+)\s*:", line)
        if match and match[2] in cfg["items"]:
            mapping[match[1]] = match[2]
    if set(mapping.values()) != set(cfg["items"]):
        raise ValueError("Could not map all four target items; config not changed")
    cfg["item_numeric_ids"] = mapping
    cfg["item_mapping_checked"] = datetime.now(timezone.utc).isoformat()
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (vendor / "provenance.json").write_text(json.dumps({"release": RELEASE, "url": URL, "downloaded_executable_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "item_metadata_url": ITEMS, "item_metadata_sha256": hashlib.sha256(text.encode()).hexdigest(), "fetched_at": datetime.now(timezone.utc).isoformat(), "verification": "Downloaded via HTTPS from official GitHub release. Hash is a local receipt, not an independently signed publisher checksum."}, indent=2), encoding="utf-8")
    license_url = "https://raw.githubusercontent.com/ao-data/albiondata-client/master/LICENSE"
    (vendor / "LICENSE-albiondata-client.txt").write_bytes(fetch(license_url))
    print("Official portable client and item mapping ready:", target)
    print("Npcap must be installed separately: https://npcap.com/#download")


if __name__ == "__main__":
    main()
