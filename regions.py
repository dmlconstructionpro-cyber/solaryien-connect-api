"""
Approximate Florida ZIP -> region (R1..R7) mapping.

Used as a fallback when a lead arrives with a ZIP but no explicit region
(e.g. from the homeowner Post-a-Project form). Apex webhooks send region_code
directly and do not need this.

NOTE: this is a coarse ZIP3-prefix map for getting leads routed. The canonical
region definition is by COUNTY (see the regions section of the homeowner site).
For production accuracy, replace this with a ZIP->county->region lookup table.
"""

# region -> set of 3-digit ZIP prefixes (approximate)
_REGION_ZIP3 = {
    "R1": {"320", "323", "324", "325", "326"},          # Panhandle / Big Bend
    "R2": {"322", "320", "321"},                          # First Coast (Duval/St Johns/Flagler)
    "R3": {"344", "346", "335"},                          # Nature Coast / Central West
    "R4": {"336", "337", "338", "342", "335"},            # Tampa Bay / Sun Coast
    "R5": {"327", "328", "329", "347", "348"},            # Central Florida (Orlando/Polk/Brevard)
    "R6": {"330", "331", "332", "333", "334", "349"},     # Gold/Treasure Coast (Miami/Broward/PB)
    "R7": {"339", "341", "342"},                          # Southwest (Lee/Collier)
}

# Build a prefix -> region lookup. When a prefix appears in more than one
# region above, the first match by region order wins (documented approximation).
_PREFIX_TO_REGION = {}
for _region, _prefixes in _REGION_ZIP3.items():
    for _p in _prefixes:
        _PREFIX_TO_REGION.setdefault(_p, _region)


def region_for_zip(zip_code):
    """Return 'R1'..'R7' for a Florida ZIP, or None if it can't be mapped."""
    if not zip_code:
        return None
    digits = "".join(ch for ch in str(zip_code) if ch.isdigit())
    if len(digits) < 3:
        return None
    return _PREFIX_TO_REGION.get(digits[:3])
