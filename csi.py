"""CSI MasterFormat divisions for commercial trade selection + trade matching."""

# code -> display name (display as "Div. 9 — Finishes")
CSI_DIVISIONS = {
    "01": "General Requirements",
    "02": "Existing Conditions / Demolition",
    "03": "Concrete",
    "04": "Masonry",
    "05": "Metals / Structural Steel / Architectural Metals",
    "06": "Wood, Plastics & Composites / Carpentry / Millwork",
    "07": "Thermal & Moisture Protection / Roofing / Waterproofing",
    "08": "Openings / Doors, Frames & Hardware / Windows / Glazing",
    "09": "Finishes / Drywall / Flooring / Tile / Painting / Ceilings / Carpet",
    "10": "Specialties / Bath Accessories / Signage / Lockers",
    "11": "Equipment",
    "12": "Furnishings",
    "13": "Special Construction",
    "14": "Conveying Equipment / Elevators",
    "15": "Mechanical / Plumbing / HVAC / Fire Protection",
    "16": "Electrical / Low Voltage / Data / Communications",
    "17": "Integrated Automation",
    "31": "Earthwork / Site Preparation / Grading",
    "32": "Exterior Improvements / Paving / Landscaping / Fencing",
    "33": "Utilities / Site Utilities",
}

# Map a CSI division -> residential trade names a pro might be registered under,
# so we can match subscribed contractors to commercial projects.
DIVISION_TRADES = {
    "03": ["Concrete", "Concrete & Masonry"],
    "04": ["Masonry", "Concrete & Masonry"],
    "06": ["Cabinets & Millwork", "Carpentry", "General Contracting"],
    "07": ["Roofing"],
    "08": ["Windows", "Doors", "Glazing"],
    "09": ["Flooring", "Tile", "Painting", "Drywall & Plastering", "Cabinets & Millwork"],
    "15": ["Plumbing", "HVAC"],
    "16": ["Electrical"],
    "31": ["Concrete & Masonry", "General Contracting"],
    "32": ["Landscaping", "Concrete & Masonry"],
    "33": ["Plumbing", "General Contracting"],
}


def label(code):
    """'09' -> 'Div. 9 — Finishes / ...'"""
    name = CSI_DIVISIONS.get(code, "")
    try:
        n = str(int(code))
    except ValueError:
        n = code
    return f"Div. {n} — {name}"


def trades_for_divisions(codes):
    """Residential trade names implied by a set of CSI division codes."""
    out = set()
    for c in codes:
        out.update(DIVISION_TRADES.get(c, []))
    return out


def pro_matches_divisions(pro_trades, division_codes):
    """
    True if a pro's trades qualify them for any of the needed divisions.
    General Contractors match any division; otherwise trade overlap is required.
    Divisions with no residential-trade mapping fall back to any commercial pro.
    """
    pro_trades = set(pro_trades or [])
    if "General Contracting" in pro_trades:
        return True
    wanted = trades_for_divisions(division_codes)
    if not wanted:
        return True  # no specific residential trade maps -> open to all commercial pros
    return bool(pro_trades & wanted)
