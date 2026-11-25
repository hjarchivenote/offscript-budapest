from sqlalchemy import select

from app.catalogue_expanded import ACTIVITIES as EXTRA_ACTIVITIES
from app.catalogue_expanded import VENUES as EXTRA_VENUES
from app.catalogue_open import ACTIVITIES as OPEN_ACTIVITIES
from app.catalogue_open import VENUES as OPEN_VENUES
from app.models import Activity, Venue
from app.opening_hours import SCHEDULES
from app.visit_time import visit_timing

CATEGORIES = {
    "coffee": "Coffee",
    "food": "Food",
    "culture": "Art & exhibitions",
    "classes": "One-day classes",
    "fitness": "Move & train",
    "nails": "Nails",
    "massage": "Massage",
    "beauty": "Beauty & aesthetics",
    "wellness": "Wellness",
    "study": "Study spots",
    "outdoors": "Outdoors & picnics",
    "shopping": "Shopping",
    "events": "Events",
    "nightlife": "Parties & nightlife",
}

# Short original summaries of provider information; no third-party reviews or photos.
# Price and schedule uncertainty are preserved rather than replaced with invented values.
VENUES = [
    (
        "kommunity",
        "Kastner Kommunity",
        "District VIII · II. János Pál pápa tér",
        "https://kommunity.kastner.hu/english/",
    ),
    ("she", "SHE Nail & Beauty", "District V · József nádor tér 10", "https://shenailbeauty.hu/"),
    ("mecca", "Mecca Gym", "District XIII · Béke utca 23", "https://meccagym.hu/nyitvatartas/"),
    (
        "madebyyou",
        "MadeByYou",
        "Budapest · confirm studio address when booking",
        "https://madebyyou.hu/paint-ceramics/",
    ),
    (
        "fszek",
        "FSZEK libraries",
        "Budapest · choose your branch",
        "https://www.fszek.hu/en/use/registration",
    ),
    (
        "ludwig",
        "Ludwig Museum",
        "District IX · Komor Marcell utca 1",
        "https://www.ludwigmuseum.hu/en/opening-hours-and-ticket-prices",
    ),
    (
        "szechenyi",
        "Széchenyi Thermal Bath",
        "City Park · Állatkerti körút 9–11",
        "https://www.szechenyibath.hu/en/prices",
    ),
    (
        "margaret",
        "Margaret Island",
        "Margitsziget · Budapest",
        "https://www.budapestinfo.hu/en/margaret-island-1",
    ),
    (
        "kanchana",
        "Kanchana Thai Massage",
        "District VIII · confirm branch when booking",
        "https://thai-massage.hu/index.php/en/prices.html",
    ),
]
VENUES.extend(EXTRA_VENUES)
VENUES.extend(OPEN_VENUES)


def entry(id, venue, title, category, *, kind="service", **details):
    if "duration_kind" not in details or not details.get("duration_note", "").strip():
        raise ValueError(f"Activity {id} needs an explicit duration kind and an explanation.")
    provider = next(v for v in VENUES if v[0] == venue)
    return dict(
        id=id,
        venue_id=venue,
        title=title,
        category=category,
        kind=kind,
        details={
            "description": "",
            "highlight": "",
            "price_low": None,
            "price_high": None,
            "price_note": "Ask the venue for the current total price.",
            "moods": [],
            "company": ["solo", "friends", "date"],
            "features": [],
            "categories": [category, "events"]
            if kind == "event" and category != "events"
            else [category],
            "weekdays": None,
            "hours": "Check current opening hours with the venue."
            if kind != "event"
            else "Check the dated event page for any schedule changes.",
            "language": "Ask the venue about English-language service.",
            "student_discount": False,
            "student_note": "No student offer confirmed for this activity.",
            "booking_url": None,
            "whatsapp_url": None,
            "instagram_url": None,
            "source_url": provider[3],
            "source_urls": [],
            "source_kind": "provider_page",
            "source_label": "Official provider page",
            "source_checked_on": "2026-11-22",
            "search_terms": f"{provider[1]} {provider[2]}",
            "availability_note": "",
            "setting": "indoor",
            "requirements": [],
            "effort": {"fitness": "high", "classes": "medium", "outdoors": "medium"}.get(
                category, "low"
            ),
            **details,
            **visit_timing(category, kind, details),
            "opening_hours": SCHEDULES.get(id),
        },
    )


ACTIVITIES = [
    entry(
        "kommunity-coffee",
        "kommunity",
        "Coffee, with a creative side",
        "coffee",
        duration_kind="flexible",
        duration_note="Independent café visit: you choose your visit length within opening hours. Seating, laptop rules and any table time limit still apply.",
        description="A café and creative space with its own coffee blend and a food selection.",
        highlight="Coffee blend developed with Brostery; vegetarian and vegan options listed.",
        categories=["coffee", "food"],
        moods=["social", "curious"],
        features=["indoor"],
        requirements=[
            "The English page directs laptop users to the separate coworking area. Check current house rules."
        ],
        booking_url="https://kommunity.kastner.hu/english/",
    ),
    entry(
        "kommunity-desk",
        "kommunity",
        "A desk, a coffee break, a fresh start",
        "study",
        duration_kind="flexible",
        duration_note="Independent coworking within the purchased half-day or full-day access window: you choose how long to stay during opening hours. A shorter visit still requires the applicable ticket; this is not a meeting-room reservation.",
        description="Choose a coworking ticket for a laptop session away from home.",
        highlight="Listed facilities include high-speed internet and a silent corner.",
        price_low=3990,
        price_high=5490,
        price_note="Published half-day to full-day ticket range. Weekend ticket: 4,250 Ft. Confirm the applicable session.",
        moods=["focus", "reset"],
        company=["solo", "friends"],
        features=["quiet", "study", "indoor"],
        hours="Published sessions: weekdays 08:00–18:00; weekend ticket 10:00–16:00. Confirm current operation.",
        booking_url="https://kommunity.kastner.hu/english/",
        requirements=["Coworking has its own ticket; a café purchase is not a desk pass."],
    ),
    entry(
        "she-manicure",
        "she",
        "A fresh manicure, just for you",
        "nails",
        duration_kind="fixed",
        description="An appointment for the salon's hygienic manicure without polish.",
        highlight="English and Hungarian service listed by the salon.",
        price_low=8000,
        price_high=8000,
        duration=45,
        duration_note="Provider lists 40–45 minutes.",
        price_note="Hygienic manicure without polish. Gel, removal and nail art are separate services.",
        moods=["reset", "treat"],
        company=["solo"],
        features=["indoor"],
        weekdays=[0, 1, 2, 3, 4],
        hours="Mon–Fri 09:30–18:00; appointment required.",
        language="English and Hungarian service listed.",
        booking_url="https://www.fresha.com/a/she-nail-beauty-budapest-jozsef-nador-ter-10-s4chwxuv?pId=622291",
        instagram_url="https://www.instagram.com/shenail__beauty/",
        whatsapp_url="https://wa.me/36707422995",
    ),
    entry(
        "she-brows",
        "she",
        "A little brow refresh",
        "beauty",
        duration_kind="unknown",
        duration_note="Select the brow treatment and confirm its full appointment length with the salon. The salon's opening hours do not establish a treatment duration.",
        description="Browse the salon's brow shaping and tint services, then choose a treatment with the provider.",
        highlight="Brow services alongside nail appointments in District V.",
        moods=["reset", "treat"],
        company=["solo"],
        features=["indoor"],
        weekdays=[0, 1, 2, 3, 4],
        hours="Mon–Fri 09:30–18:00; appointment required.",
        language="English and Hungarian service listed.",
        booking_url="https://www.fresha.com/a/she-nail-beauty-budapest-jozsef-nador-ter-10-s4chwxuv?pId=622291",
        instagram_url="https://www.instagram.com/shenail__beauty/",
        whatsapp_url="https://wa.me/36707422995",
    ),
    entry(
        "mecca-day",
        "mecca",
        "One workout. No monthly commitment.",
        "fitness",
        duration_note="Independent gym visit on a day ticket: you choose your workout length within opening hours and ticket conditions. Coached classes and personal training have separate session lengths.",
        description="A single-entry gym visit at Mecca Gym.",
        duration_kind="flexible",
        highlight="A daily ticket is listed separately from monthly memberships.",
        price_low=5500,
        price_high=5500,
        price_note="Standard day ticket 4,500 Ft + mandatory first entry card 1,000 Ft. Existing cardholders pay 4,500 Ft; a separate first-time offer is also listed.",
        moods=["active", "reset"],
        company=["solo", "friends"],
        features=["indoor"],
        weekdays=list(range(7)),
        hours="Mon–Fri 06:00–22:00; Sat–Sun 08:00–20:00.",
        booking_url="https://meccagym.hu/nyitvatartas/",
        requirements=[
            "Bring or purchase the required entry card. Confirm registration and any first-time offer at reception."
        ],
    ),
    entry(
        "madebyyou-paint",
        "madebyyou",
        "Paint something you'll actually keep",
        "classes",
        duration_kind="unknown",
        duration_note="The open studio describes a few hours of self-directed painting, but a guaranteed completion time and booked table length are not confirmed. Agree the time needed for your chosen piece with the studio; firing and collection happen later.",
        timing_source_url="https://madebyyou.hu/paint-ceramics/",
        timing_checked_on="2026-11-24",
        description="Choose a ceramic piece and paint it in the studio. Staff provide an introduction.",
        highlight="The provider says its staff can help in English; finished work needs firing and later collection.",
        moods=["curious", "reset", "social"],
        features=["indoor"],
        language="English-speaking guidance listed.",
        booking_url="https://madebyyou.hu/paint-ceramics/",
        requirements=[
            "Reserve a table with the studio before visiting.",
            "Confirm the piece price and collection date; this is not a same-day take-home activity.",
        ],
    ),
    entry(
        "fszek-study",
        "fszek",
        "Make room for a proper study session",
        "study",
        duration_kind="flexible",
        duration_note="Independent reading or study visit: you choose your visit length within opening hours and your registration's access conditions. This does not cover guided visits or booked group rooms.",
        kind="place",
        description="Explore library registration for reading, studying and local Wi-Fi access.",
        highlight="The registration page distinguishes local-use access from borrowing membership.",
        price_low=400,
        price_high=400,
        price_note="Published administration fee for a local-use registration card. Confirm branch and room access; sightseeing admission is separate.",
        moods=["focus"],
        company=["solo"],
        features=["quiet", "study", "indoor"],
        booking_url="https://www.fszek.hu/en/use/registration",
        requirements=[
            "Choose a branch and check its opening hours.",
            "Ask reception about registration documents and access to the rooms you want to use.",
        ],
    ),
    entry(
        "ludwig-art",
        "ludwig",
        "An afternoon with contemporary art",
        "culture",
        duration_kind="flexible",
        duration_note="Independent exhibition visit: you choose your visit length within opening hours, admission conditions and any ticket limit. Guided tours, workshops and scheduled shows need their own confirmed duration.",
        description="Visit a single exhibition at Ludwig Museum and choose what to see on the provider's programme.",
        highlight="The provider lists separate single-exhibition and combined tickets.",
        price_low=3300,
        price_high=3300,
        price_note="Published full-price single-exhibition ticket. Combined tickets and guided tours cost more.",
        moods=["curious", "reset"],
        features=["indoor"],
        weekdays=[1, 2, 3, 4, 5, 6],
        hours="Tue–Sun from 10:00; floors close at 18:00 or 20:00. Monday closed. Check holiday changes.",
        student_discount=True,
        student_note="Published student-group ticket: 1,600 Ft per person for groups of more than 10. This is not a confirmed individual student offer.",
        booking_url="https://www.ludwigmuseum.hu/en/opening-hours-and-ticket-prices",
    ),
    entry(
        "ludwig-shop",
        "ludwig",
        "A browse through the museum shop",
        "shopping",
        duration_kind="flexible",
        duration_note="Independent shop browsing: you choose your visit length within opening hours. This listing does not cover a workshop or booked personal-shopping appointment.",
        kind="place",
        description="Add a museum-shop browse to a culture day; check the current range with the museum.",
        highlight="The shop has its own published opening hours.",
        moods=["curious", "social"],
        features=["indoor"],
        weekdays=[1, 2, 3, 4, 5, 6],
        hours="Tue–Sun 10:00–19:30; Monday closed.",
        booking_url="https://www.ludwigmuseum.hu/en/opening-hours-and-ticket-prices",
    ),
    entry(
        "szechenyi-soak",
        "szechenyi",
        "Slow down at Széchenyi",
        "wellness",
        duration_note="Independent bath entry: you choose your stay within opening hours and the selected ticket's time and zone limits. Massage, sauna programmes and treatment packages have separate session lengths; shorter stays still require the full ticket.",
        description="Plan a bath visit using the official ticket options and date-specific prices.",
        duration_kind="flexible",
        highlight="Locker entry and extra services are priced separately.",
        price_low=13200,
        price_high=15800,
        price_note="Published daily locker entry: 13,200 Ft Mon–Thu; 14,800 Ft Fri–Sun; 15,800 Ft peak dates. Extras not included.",
        moods=["reset", "treat", "social"],
        features=[],
        setting="mixed",
        booking_url="https://tickets.szechenyibath.hu/",
        requirements=[
            "Check age rules, swimwear requirements and the exact date's ticket conditions on the official site."
        ],
    ),
    entry(
        "margaret-picnic",
        "margaret",
        "A little island time",
        "outdoors",
        duration_kind="flexible",
        duration_note="Independent outdoor visit: you choose your visit length within visitor opening hours and access rules. Guided walks, exhibitions and paid attractions have separate schedules.",
        kind="place",
        description="Plan your own walk or picnic on Margaret Island, a public recreational area in Budapest.",
        highlight="Our idea: bring your own picnic and leave time for an unhurried walk.",
        moods=["reset", "social", "active"],
        features=["outdoor"],
        setting="outdoor",
        price_note="Your spend depends on picnic supplies, transport and any paid attractions you choose.",
        booking_url="https://www.budapestinfo.hu/en/margaret-island-1",
        requirements=[
            "Check the weather and current access before setting out. Individual attractions have their own schedules."
        ],
    ),
    entry(
        "kanchana-massage",
        "kanchana",
        "Make time for a massage",
        "massage",
        duration_kind="unknown",
        duration_note="Choose the massage treatment and confirm its full appointment length. A service menu or an open salon does not establish how long your selected treatment takes.",
        description="Browse Kanchana's listed massage options and ask the provider about the treatment you want.",
        highlight="Confirm treatment, duration and total price before booking.",
        moods=["reset", "treat"],
        company=["solo", "date"],
        features=["indoor"],
        booking_url="https://thai-massage.hu/index.php/en/prices.html",
    ),
]
ACTIVITIES.extend(entry(**record) for record in EXTRA_ACTIVITIES)
ACTIVITIES.extend(entry(**record) for record in OPEN_ACTIVITIES)


def seed_catalogue(session):
    for id, name, location, website in VENUES:
        venue = session.get(Venue, id)
        if venue is None:
            session.add(Venue(id=id, name=name, location=location, website=website))
        else:
            venue.name, venue.location, venue.website = name, location, website
    session.flush()
    for activity in ACTIVITIES:
        existing = session.get(Activity, activity["id"])
        if existing is None:
            session.add(Activity(**activity))
        else:
            for field, value in activity.items():
                setattr(existing, field, value)
    current_ids = {activity["id"] for activity in ACTIVITIES}
    for existing in session.scalars(select(Activity)):
        if existing.id not in current_ids:
            existing.details = {**existing.details, "catalogue_active": False}
    # Keep retired IDs for historical plans and reviews; never recommend them.
    # The bootstrap transaction owns the schema, catalogue and state marker.
    session.flush()
