#!/usr/bin/env python3
"""Patreon -> supporters.txt for the AtlasVRFX Photo Studio world (read by SupporterList.cs).

Runs in a GitHub Action every 15 minutes (see workflow/supporters.yml and README.md). It asks the Patreon API for the
campaign's members, keeps the active patrons, maps each one's pledge to a world tier, and writes one line per supporter:

    <tier>\t<VRChat display name>

The VRChat name comes, in order, from: (1) the patron's own /verify in Discord (the Cloudflare Worker in discord_worker/
keeps Discord id -> VRChat name; Patreon says which Discord account each pledge belongs to, so no one types anything into
Patreon), (2) the creator's private *note* on the member in Patreon (Audience > member > note), the old manual way. Active
patrons with neither are skipped (listed in the Action log so you can chase them). manual.txt adds people by hand
(testers, friends, gifted tiers), same format.

Reliability: on any API error the script exits non-zero WITHOUT touching the file, so the world keeps the last good list.
It also refuses to publish an empty list when the previous one had names (a Patreon outage returning nothing).

Environment: PATREON_TOKEN (creator access token), PATREON_CAMPAIGN_ID (optional; the first campaign is used if unset),
VERIFY_URL (the Worker's /sync address) and VERIFY_SECRET (its SYNC_SECRET), both optional: without them only notes and
manual.txt are used. If the Worker is set but unreachable the run fails and the last good list stays online.
Standard library only.  Self-test:  python3 sync_supporters.py --selftest
"""
import json
import os
import sys
import urllib.parse
import urllib.request

API = "https://www.patreon.com/api/oauth2/v2"
# pledge (in the campaign's currency, pence for GBP) -> world tier: 1 Core (1), 2 Plus (2), 3 Elite (6). Free members get
# nothing. Core is a name on the supporter wall; Plus and Elite open every VIP area (Elite is a tip tier).
TIER_CENTS = [(600, 3), (200, 2), (100, 1)]


def get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "User-Agent": "atlasvrfx-supporters/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def campaign_id(token):
    cid = os.environ.get("PATREON_CAMPAIGN_ID", "").strip()
    if cid:
        return cid
    data = get(API + "/campaigns", token)["data"]
    if not data:
        raise RuntimeError("no campaign on this token")
    return data[0]["id"]


def fetch_members(token, cid):
    """All members with patron_status, note, pledge and entitled tiers (follows pagination)."""
    q = {"include": "currently_entitled_tiers,user",
         "fields[member]": "patron_status,note,full_name,currently_entitled_amount_cents",
         "fields[tier]": "amount_cents,title",
         "fields[user]": "social_connections",
         "page[count]": "500"}
    url = f"{API}/campaigns/{cid}/members?" + urllib.parse.urlencode(q)
    members, tiers, discord = [], {}, {}
    while url:
        page = get(url, token)
        members += page.get("data", [])
        for inc in page.get("included", []):
            if inc.get("type") == "tier":
                tiers[inc["id"]] = inc["attributes"].get("amount_cents") or 0
            elif inc.get("type") == "user":                       # the patron's Discord account, if they connected it
                d = ((inc.get("attributes") or {}).get("social_connections") or {}).get("discord") or {}
                if d.get("user_id"):
                    discord[inc["id"]] = str(d["user_id"])
        url = page.get("links", {}).get("next")
    for m in members:                                             # member -> their Discord id, kept on the member
        u = ((m.get("relationships") or {}).get("user") or {}).get("data") or {}
        m["discord_id"] = discord.get(u.get("id"))
    return members, tiers


def tier_for(cents):
    for need, tier in TIER_CENTS:
        if cents >= need:
            return tier
    return 0


def clean_name(note):
    """First line of the creator note is the VRChat name (tabs and newlines can't be in it)."""
    return (note or "").strip().splitlines()[0].replace("\t", " ").strip() if (note or "").strip() else ""


def active_tiers(members, tiers):
    """-> {member index: tier} for active patrons with a tier above 0 (shared by build() and the Discord sync)."""
    out = {}
    for i, m in enumerate(members):
        a = m.get("attributes", {})
        if a.get("patron_status") != "active_patron":
            continue
        cents = a.get("currently_entitled_amount_cents") or 0
        for t in m.get("relationships", {}).get("currently_entitled_tiers", {}).get("data", []):
            cents = max(cents, tiers.get(t["id"], 0))
        if tier_for(cents):
            out[i] = tier_for(cents)
    return out


def discord_patrons(members, tiers):
    """{discord user id: tier} for the active patrons who connected Discord (what the Worker answers /status from)."""
    return {members[i]["discord_id"]: t for i, t in active_tiers(members, tiers).items() if members[i].get("discord_id")}


def build(members, tiers, manual_lines, links=None):
    """-> ({name: tier}, [skipped descriptions]); links: {discord id: VRChat name} from the Discord /verify Worker"""
    links = links or {}
    out, skipped = {}, []
    for m in members:
        a = m.get("attributes", {})
        if a.get("patron_status") != "active_patron":
            continue
        cents = a.get("currently_entitled_amount_cents") or 0
        for t in m.get("relationships", {}).get("currently_entitled_tiers", {}).get("data", []):
            cents = max(cents, tiers.get(t["id"], 0))
        tier = tier_for(cents)
        if tier == 0:
            continue
        name = clean_name(links.get(m.get("discord_id") or "")) or clean_name(a.get("note"))
        if not name:
            why = "has not run /verify in Discord" if m.get("discord_id") else "no Discord connected on Patreon and no VRChat name in the member note"
            skipped.append(f"{a.get('full_name', '?')} ({cents / 100:.2f}): {why}")
            continue
        out[name] = max(tier, out.get(name, 0))
    for line in manual_lines:
        line = line.strip()
        if not line or line.startswith("#") or "\t" not in line:
            continue
        t, name = line.split("\t", 1)
        if t.strip().isdigit() and name.strip():
            out[name.strip()] = max(int(t), out.get(name.strip(), 0))
    return out, skipped


def render(names):
    lines = ["# AtlasVRFX Photo Studio supporters: tier<TAB>VRChat name. Generated; edit manual.txt instead."]
    for name, tier in sorted(names.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        lines.append(f"{tier}\t{name}")
    return "\n".join(lines) + "\n"


def main(out_path, manual_path):
    token = os.environ.get("PATREON_TOKEN", "").strip()
    if not token:
        sys.exit("PATREON_TOKEN is not set")
    try:
        members, tiers = fetch_members(token, campaign_id(token))
    except Exception as e:                               # keep the last good list online
        sys.exit(f"Patreon API failed, keeping the existing list: {e}")
    links = {}
    url, secret = os.environ.get("VERIFY_URL", "").strip(), os.environ.get("VERIFY_SECRET", "").strip()
    if url:
        try:                                             # tell the Worker who the patrons are, get the VRChat names back
            req = urllib.request.Request(url, data=json.dumps({"patrons": discord_patrons(members, tiers)}).encode(),
                                         headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json", "User-Agent": "atlasvrfx-supporters/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                links = json.load(r).get("links", {})
        except Exception as e:                           # (links missing would drop every verified patron: keep the last list)
            sys.exit(f"The verification Worker failed, keeping the existing list: {e}")
    manual = open(manual_path, encoding="utf-8").read().splitlines() if os.path.exists(manual_path) else []
    names, skipped = build(members, tiers, manual, links)
    for s in skipped:
        print("SKIPPED", s)
    old = open(out_path, encoding="utf-8").read() if os.path.exists(out_path) else ""
    old_count = sum(1 for l in old.splitlines() if l and not l.startswith("#"))
    if not names and old_count > 0:
        sys.exit("Patreon returned no supporters but the current list has some; not publishing an empty list")
    text = render(names)
    if text != old:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"UPDATED {len(names)} supporters")
    else:
        print(f"UNCHANGED {len(names)} supporters")


def selftest():
    tiers = {"t2": 100, "t5": 200, "t10": 600}
    rel = lambda t: {"currently_entitled_tiers": {"data": [{"id": t, "type": "tier"}]}}
    members = [
        {"attributes": {"patron_status": "active_patron", "note": "AtlasFan", "full_name": "A"}, "relationships": rel("t5")},
        {"attributes": {"patron_status": "active_patron", "note": "Tipper\nextra line", "full_name": "B"}, "relationships": rel("t10")},
        {"attributes": {"patron_status": "active_patron", "note": "Small", "full_name": "C"}, "relationships": rel("t2")},
        {"attributes": {"patron_status": "former_patron", "note": "Gone", "full_name": "D"}, "relationships": rel("t10")},
        {"attributes": {"patron_status": "active_patron", "note": "", "full_name": "E"}, "relationships": rel("t5")},
        {"attributes": {"patron_status": "active_patron", "note": "FreeFollower", "full_name": "F"}, "relationships": {}},
        {"attributes": {"patron_status": "active_patron", "note": "Legacy", "full_name": "G", "currently_entitled_amount_cents": 300}},
    ]
    names, skipped = build(members, tiers, ["# comment", "2\tTester Friend", "1\tAtlasFan"])
    assert names == {"AtlasFan": 2, "Tipper": 3, "Small": 1, "Tester Friend": 2, "Legacy": 2}, names
    assert len(skipped) == 1 and "E" in skipped[0], skipped
    # Discord verification: a linked VRChat name beats the note; a patron with only a Discord id is named from the links
    members[0]["discord_id"] = "111"; members[4]["discord_id"] = "555"
    assert discord_patrons(members, tiers) == {"111": 2, "555": 2}, discord_patrons(members, tiers)
    names, skipped = build(members, tiers, [], {"111": "Atlas Verified", "555": "Patron Five", "999": "Not A Patron"})
    assert names["Atlas Verified"] == 2 and "AtlasFan" not in names and names["Patron Five"] == 2 and "Not A Patron" not in names, names
    assert not skipped, skipped
    text = render(names)
    assert text.splitlines()[1] == "3\tTipper" and text.splitlines()[-1] == "1\tSmall", text
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main(os.environ.get("OUT", "docs/supporters.txt"), os.environ.get("MANUAL", "manual.txt"))
