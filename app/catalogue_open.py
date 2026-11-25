"""Selected Budapest place records with their open-data provenance preserved."""

import json
from pathlib import Path

SNAPSHOT = json.loads((Path(__file__).parent / "data" / "open_places.json").read_text())
DATA_SOURCE = {key: value for key, value in SNAPSHOT.items() if key != "places"}
PROVIDER_DETAILS = json.loads(
    (Path(__file__).parent / "data" / "provider_details.json").read_text()
)["places"]

VENUES = [
    (
        f"overture-{p['id']}",
        PROVIDER_DETAILS.get(p["id"], {}).get("venue_name", p["name"]),
        PROVIDER_DETAILS.get(p["id"], {}).get("venue_location", p["location"]),
        p["website"],
    )
    for p in SNAPSHOT["places"]
]

VISIT_NOTES = {
    "coffee": "Check the current menu, seating and laptop policy before visiting.",
    "food": "Check the current menu, prices and table availability with the restaurant.",
    "culture": "Check the current programme, visitor access and admission prices.",
    "fitness": "Ask about visitor access, single-entry tickets and any required registration.",
    "beauty": "Confirm the treatment, total price, language and appointment with the salon.",
    "nails": "Confirm the nail service, removal costs and appointment with the salon.",
    "massage": "Confirm the treatment, duration and total price before booking.",
    "wellness": "Check the available treatments, visitor access and any booking requirements.",
    "shopping": "Check current shop hours and stock on the retailer's website.",
    "study": "Check reader registration, public access and study-space rules before travelling.",
    "outdoors": "Check visitor access, seasonal opening and the weather before setting out.",
    "nightlife": "Check the current programme, age restrictions and any entry or table charges.",
}


def activity_record(place):
    """Directory data supplies identity and location, not prices or suitability claims."""
    enrichment = PROVIDER_DETAILS.get(place["id"], {})
    if enrichment and (
        "duration_kind" not in enrichment or not enrichment.get("duration_note", "").strip()
    ):
        raise ValueError(f"Reviewed provider {place['id']} needs an explicit timing classification.")
    return {
        "id": f"overture-{place['id']}-place",
        "venue": f"overture-{place['id']}",
        "title": place["name"],
        "category": place["category"],
        "kind": "place",
        "duration_kind": "unknown",
        "duration_note": "Duration and visitor access have not been confirmed with the venue. Check whether your chosen activity is an independent visit, a booked appointment or a scheduled programme before making a timed plan.",
        "description": f"{place['type_label']} in {place['location']}. {VISIT_NOTES[place['category']]}",
        "highlight": VISIT_NOTES[place["category"]],
        "availability_note": "Basic listing: current hours, prices and visitor access need confirmation.",
        "moods": [],
        "company": [],
        "features": [],
        "effort": None,
        "setting": None,
        "source_kind": "open_data",
        "source_label": "Overture Maps place data",
        "source_url": DATA_SOURCE["url"],
        "source_checked_on": None,
        "data_imported_on": DATA_SOURCE["imported_on"],
        "data_release": DATA_SOURCE["release"],
        "data_license": DATA_SOURCE["license"],
        "data_license_url": DATA_SOURCE["license_url"],
        "overture_id": place["id"],
        "source_records": place["sources"],
        "coordinates": place["coordinates"],
        "website_checked_on": place["website_checked_on"],
        **{
            field: value
            for field, value in enrichment.items()
            if field not in {"venue_name", "venue_location"}
        },
    }


ACTIVITIES = [activity_record(place) for place in SNAPSHOT["places"]]
