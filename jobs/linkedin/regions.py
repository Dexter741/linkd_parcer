"""Regions used to split a country-wide search that exceeds LinkedIn's result cap.

Each value is a location string LinkedIn's search box understands. Any other
country works too: list its regions in SearchQuery.regions.
"""

US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
    "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]  # fmt: skip

CANADA_PROVINCES = [
    "Alberta", "British Columbia", "Manitoba", "New Brunswick", "Newfoundland and Labrador", "Nova Scotia",
    "Ontario", "Prince Edward Island", "Quebec", "Saskatchewan", "Northwest Territories", "Nunavut", "Yukon",
]  # fmt: skip

UK_NATIONS = ["England", "Scotland", "Wales", "Northern Ireland"]

GERMANY_STATES = [
    "Baden-Württemberg", "Bavaria", "Berlin", "Brandenburg", "Bremen", "Hamburg", "Hesse", "Lower Saxony",
    "Mecklenburg-Vorpommern", "North Rhine-Westphalia", "Rhineland-Palatinate", "Saarland", "Saxony",
    "Saxony-Anhalt", "Schleswig-Holstein", "Thuringia",
]  # fmt: skip

_COUNTRIES: dict[str, tuple[str, list[str]]] = {
    "united states": ("United States", US_STATES),
    "canada": ("Canada", CANADA_PROVINCES),
    "united kingdom": ("United Kingdom", UK_NATIONS),
    "germany": ("Germany", GERMANY_STATES),
}

_ALIASES = {
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "united states of america": "united states",
    "america": "united states",
    "uk": "united kingdom",
    "great britain": "united kingdom",
    "deutschland": "germany",
}


def regions_for(country: str) -> list[str]:
    """Built-in regions of a country as LinkedIn locations, or [] if we have none."""
    key = country.strip().lower()
    key = _ALIASES.get(key, key)
    if key not in _COUNTRIES:
        return []
    name, regions = _COUNTRIES[key]
    return [f"{region}, {name}" for region in regions]
