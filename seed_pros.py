"""
Seed a Solaryien Connect database with verified pros that mirror the public
directory on the homeowner site (home/find-contractors.html).

Run:  python seed_pros.py            # writes solaryien_connect.db
      python seed_pros.py --reset    # wipe + reseed
"""

import sys

import database
import accounts

# (name, company, email, trades, regions)
SEED_PROS = [
    ("Marcus Reyes",    "Reyes Tile & Stone, LLC",        "marcus@reyestile.com",
     ["Flooring"],               ["R5", "R4"]),
    ("Dana Whitfield",  "Coastline Painting Co.",         "dana@coastlinepaint.com",
     ["Painting"],               ["R4", "R5"]),
    ("Elena Ortiz",     "Summit Remodeling Group",        "elena@summitremodel.com",
     ["General Contracting"],    ["R5"]),
    ("Travis Boone",    "Gulf Coast Roofing & Exteriors", "travis@gulfcoastroof.com",
     ["Roofing"],                ["R7"]),
    ("Priya Nair",      "Beacon Electric Services",       "priya@beaconelectric.com",
     ["Electrical"],             ["R6", "R5"]),
    ("Caleb Foster",    "Foster Plumbing & Mechanical",   "caleb@fosterplumbing.com",
     ["Plumbing"],               ["R2"]),
    ("Sofia Mendez",    "Heritage Cabinetry & Millwork",  "sofia@heritagecab.com",
     ["Cabinets & Millwork"],    ["R5"]),
    ("Anthony Delgado", "Delgado Air Conditioning",       "anthony@delgadoair.com",
     ["HVAC"],                   ["R4"]),
    ("Grace Holloway",  "Holloway Concrete & Masonry",    "grace@hollowayconcrete.com",
     ["Concrete & Masonry"],     ["R1"]),
    # extra flooring pros in R5 so fair rotation is visible in a demo
    ("Devin Park",      "Park Hardwood Floors",           "devin@parkfloors.com",
     ["Flooring"],               ["R5"]),
    ("Maria Castillo",  "Castillo Surfaces",              "maria@castillosurfaces.com",
     ["Flooring"],               ["R5", "R3"]),
]


def seed(conn):
    created = 0
    for name, company, email, trades, regions in SEED_PROS:
        exists = conn.execute("SELECT 1 FROM pros WHERE email = ?", (email,)).fetchone()
        if exists:
            continue
        pid = accounts.create_pro_account(
            conn, name=name, company=company, email=email, phone="239-555-0100",
            password="demo-password", trades=trades, regions=regions,
            plan="pro", coverage_type="bundled")
        # verified + subscribed -> active and eligible
        accounts.approve_and_activate(conn, pid, plan="pro", coverage_type="bundled")
        created += 1
    print(f"Seeded {created} verified pro(s). Total pros: "
          f"{conn.execute('SELECT COUNT(*) c FROM pros').fetchone()['c']}")


if __name__ == "__main__":
    path = "solaryien_connect.db"
    conn = database.connect(path)
    if "--reset" in sys.argv:
        for t in ("lead_distributions", "pro_lead_count", "leads",
                  "pro_trades", "pro_regions", "pros"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
    database.init_db(conn)
    seed(conn)
    conn.close()
