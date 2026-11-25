import hashlib
import os
import re
import secrets
from collections import Counter, defaultdict
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import __version__
from app.bootstrap import initialize_database, require_initialized_database
from app.catalogue import CATEGORIES
from app.catalogue_open import DATA_SOURCE
from app.database import make_database
from app.discovery import BUDAPEST, discover, plan_day_problem
from app.models import Activity, Candidate, Participant, Plan, Review, Venue, Vote
from app.opening_hours import opening_status
from app.provider_freshness import commercial_view
from app.ranking import order_description, rank_matches
from app.visit_window import current_visit

STATIC = Path(__file__).parent / "static"
Choice = Literal["yes", "maybe", "no"]


class Input(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class PlanInput(Input):
    title: str = Field(min_length=2, max_length=100)
    day: date
    nickname: str = Field(min_length=2, max_length=40)
    use_today: bool = False
    activity_id: str | None = Field(default=None, min_length=1, max_length=80)


class JoinInput(Input):
    nickname: str = Field(min_length=2, max_length=40)


class CandidateInput(Input):
    activity_id: str = Field(min_length=1, max_length=80)


class VoteInput(Input):
    choice: Choice


class PickInput(Input):
    candidate_id: int = Field(gt=0)


class ReviewInput(Input):
    nickname: str = Field(min_length=2, max_length=40)
    rating: int = Field(ge=1, le=5)
    text: str = Field(min_length=10, max_length=1500)
    visited_on: date


def create_app(database_url: str | None = None, clock: Callable | None = None):
    engine, sessions = make_database(database_url)

    @asynccontextmanager
    async def lifespan(application):
        try:
            if os.environ.get("APP_ENV") == "production":
                require_initialized_database(engine)
            else:
                initialize_database(engine)
            yield
        finally:
            engine.dispose()

    api = FastAPI(title="Offscript Budapest", version=__version__, lifespan=lifespan)
    api.state.sessions = sessions
    api.state.clock = clock or (lambda: datetime.now(UTC))
    api.state.engine = engine

    @api.middleware("http")
    async def headers_and_origin(request, call_next):
        response = None
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            allowed = os.environ.get("PUBLIC_ORIGIN", str(request.base_url).rstrip("/"))
            if origin and origin != allowed:
                response = JSONResponse({"detail": "Please submit from this app."}, status_code=403)
        if response is None:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @api.exception_handler(SQLAlchemyError)
    async def database_unavailable(request, error):
        return JSONResponse(
            {"detail": "Storage is temporarily unavailable. Please try again shortly."},
            status_code=503, headers={"Retry-After": "5"},
        )

    def get_db():
        with sessions() as db:
            yield db

    DB = Annotated[Session, Depends(get_db)]

    def visitor(request: Request, response: Response):
        token = request.cookies.get("offscript_guest", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            token = secrets.token_urlsafe(32)
            response.set_cookie(
                "offscript_guest",
                token,
                httponly=True,
                samesite="lax",
                secure=os.environ.get("COOKIE_SECURE") == "1",
                max_age=2592000,
            )
        return hashlib.sha256(token.encode()).hexdigest()

    Visitor = Annotated[str, Depends(visitor)]

    def commit(db):
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                409, "This changed in another request. Refresh and try again."
            ) from None

    def activity_or_404(db, activity_id):
        activity = db.get(Activity, activity_id)
        if not activity:
            raise HTTPException(404, "Activity not found.")
        return activity

    def eligible_activity(db, activity_id, day):
        activity = activity_or_404(db, activity_id)
        if problem := plan_day_problem(activity, day, api.state.clock()):
            raise HTTPException(422, problem)
        return activity

    def activity_json(db, activity, now=None, max_minutes=None, venue=None):
        venue = venue if venue is not None else db.get(Venue, activity.venue_id)
        now = now or api.state.clock()
        opening = opening_status(activity, now)
        return {
            "id": activity.id,
            "title": activity.title,
            "kind": activity.kind,
            "category": activity.category,
            "venue": {"name": venue.name, "location": venue.location, "website": venue.website},
            **activity.details,
            **commercial_view(activity.details, now),
            "catalogue_note": (
                "No longer in our current collection. This saved listing is historical; confirm current details with the venue."
                if activity.details.get("catalogue_active") is False else ""
            ),
            "opening": opening,
            "visit_now": current_visit(activity, now, opening, max_minutes),
        }

    def plan_or_404(db, plan_id):
        plan = db.get(Plan, plan_id)
        if not plan:
            raise HTTPException(404, "Plan not found. Check your invitation link.")
        return plan

    def writable_plan(db, plan_id):
        # Acquire the plan lock before checking its state. Every plan mutation
        # uses this same lock, including joins, suggestions and changed votes.
        if db.bind.dialect.name == "sqlite":
            # SQLite has no SELECT FOR UPDATE; a no-op write locks its writer
            # transaction, including across separate application processes.
            found = db.execute(update(Plan).where(Plan.id == plan_id).values(selected_candidate=Plan.selected_candidate))
            if not found.rowcount:
                raise HTTPException(404, "Plan not found. Check your invitation link.")
            plan = db.get(Plan, plan_id, populate_existing=True)
        else:
            plan = db.scalar(select(Plan).where(Plan.id == plan_id).with_for_update())
            if plan is None:
                raise HTTPException(404, "Plan not found. Check your invitation link.")
        open_plan(plan)
        return plan

    def participant(db, plan_id, guest, required=True):
        person = db.scalar(
            select(Participant).where(
                Participant.plan_id == plan_id, Participant.visitor_hash == guest
            )
        )
        if required and not person:
            raise HTTPException(403, "Join this plan with a nickname first.")
        return person

    def open_plan(plan):
        if plan.selected_candidate is not None:
            raise HTTPException(409, "This plan is decided. Voting and suggestions are closed.")

    def plan_json(db, plan, guest):
        now = api.state.clock()
        people = list(
            db.scalars(
                select(Participant).where(Participant.plan_id == plan.id).order_by(Participant.id)
            )
        )
        me = next((p for p in people if p.visitor_hash == guest), None)
        candidates = list(
            db.scalars(select(Candidate).where(Candidate.plan_id == plan.id).order_by(Candidate.id))
        )
        activities = {a.id: a for a in db.scalars(select(Activity).where(Activity.id.in_([c.activity_id for c in candidates])))}
        venues = {v.id: v for v in db.scalars(select(Venue).where(Venue.id.in_({a.venue_id for a in activities.values()})))}
        votes_by_candidate = defaultdict(list)
        for vote in db.scalars(select(Vote).where(Vote.candidate_id.in_([c.id for c in candidates]))):
            votes_by_candidate[vote.candidate_id].append(vote)
        result = []
        for candidate in candidates:
            activity = activities[candidate.activity_id]
            votes = votes_by_candidate[candidate.id]
            counts = Counter(v.choice for v in votes)
            mine = next((v.choice for v in votes if me and v.participant_id == me.id), None)
            proposer = next(p.nickname for p in people if p.id == candidate.participant_id)
            result.append(
                {
                    "id": candidate.id,
                    "activity": activity_json(db, activity, now, venue=venues[activity.venue_id]),
                    "day_problem": plan_day_problem(activity, plan.day, now),
                    "proposed_by": proposer,
                    "votes": {c: counts[c] for c in ("yes", "maybe", "no")},
                    "my_vote": mine,
                }
            )
        return {
            "id": plan.id,
            "title": plan.title,
            "day": plan.day.isoformat(),
            "invite_path": f"/plan/{plan.id}",
            "selected_candidate": plan.selected_candidate,
            "participants": [p.nickname for p in people],
            "me": {"nickname": me.nickname, "is_host": me.is_host} if me else None,
            "candidates": result,
        }

    @api.get("/health")
    def health():
        return {"status": "ok"}

    @api.get("/ready")
    def ready():
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    @api.get("/version")
    def version():
        return {"version": __version__, "commit": os.environ.get("APP_COMMIT", "local")}

    def calendar_snapshot():
        now = api.state.clock().astimezone(BUDAPEST)
        return {"as_of": now.isoformat(), "today": now.date().isoformat(), "timezone": "Europe/Budapest"}

    @api.get("/api/clock")
    def read_clock():
        return calendar_snapshot()

    @api.get("/api/config")
    def config(db: DB):
        catalogue = [a for a in db.scalars(select(Activity)) if a.details.get("catalogue_active") is not False]
        basic_places = {
            a.venue_id
            for a in catalogue
            if a.details.get("source_kind") == "open_data"
        }
        enriched_places = {
            a.venue_id for a in catalogue if a.details.get("source_kind") == "provider_enriched"
        }
        return {
            "categories": CATEGORIES,
            **calendar_snapshot(),
            "version": __version__,
            "catalogue_stats": {
                "places": len({a.venue_id for a in catalogue}),
                "activities": len(catalogue),
                "basic_places": len(basic_places),
                "enriched_places": len(enriched_places),
            },
            "data_sources": [DATA_SOURCE],
            "event_sources": [
                {"name": "A38 programme", "url": "https://www.a38.hu/en/programs"},
                {"name": "Akvárium programme", "url": "https://akvariumklub.hu/en/"},
                {"name": "Mai Manó House programme", "url": "https://maimano.hu/"},
            ],
            "catalogue_note": "Provider summaries and clearly marked basic place listings. Hours, prices and bookings can change.",
        }

    @api.get("/api/activities")
    def activities(
        db: DB,
        when: Literal["today", "weekend", "anytime"] = "today",
        category: str | None = None,
        mood: Literal["reset", "focus", "curious", "active", "social", "treat"] | None = None,
        company: Literal["solo", "friends", "date"] | None = None,
        energy: Literal["low", "medium", "high"] | None = None,
        max_price: Annotated[int | None, Query(ge=0, le=1000000)] = None,
        max_minutes: Annotated[int | None, Query(ge=1, le=1440)] = None,
        feature: Annotated[
            list[Literal["quiet", "study", "indoor", "outdoor"]] | None, Query()
        ] = None,
        q: Annotated[str | None, Query(max_length=120)] = None,
        student: bool = False,
        open_now: bool = False,
    ):
        if category and category not in CATEGORIES:
            raise HTTPException(422, "Unknown activity category.")
        if open_now and when != "today":
            raise HTTPException(422, "Open now is available with Today; use a future window without it.")
        now = api.state.clock()
        pairs = discover(
            list(db.scalars(select(Activity))),
            now=now,
            when=when,
            category=category,
            mood=mood,
            company=company,
            max_price=max_price,
            max_minutes=max_minutes,
            feature=feature,
            q=q,
            student=student,
            energy=energy,
            open_now=open_now,
        )
        ranked = rank_matches(pairs, now=now, when=when, query=q, max_minutes=max_minutes, open_now=open_now)
        venues = {v.id: v for v in db.scalars(select(Venue).where(Venue.id.in_({a.venue_id for a, _ in pairs})))}
        result = [
            {**activity_json(db, item.activity, now, max_minutes, venue=venues[item.activity.venue_id]), "reasons": item.reasons, "recommendation": item.recommendation}
            for item in ranked
        ]
        return {
            "items": result,
            "total": len(result),
            "places": len({a.venue_id for a, _ in pairs}),
            "basic_places": len(
                {a.venue_id for a, _ in pairs if a.details.get("source_kind") == "open_data"}
            ),
            "when": when,
            "open_now": open_now,
            "as_of": now.astimezone(BUDAPEST).isoformat(),
            "ordering": {"id": "suggested", "description": order_description(when, q)},
            "note": "Published dated exceptions and opening days are used where confirmed; current hours and reservation availability must be checked with the provider.",
        }

    @api.get("/api/activities/{activity_id}")
    def activity_detail(
        activity_id: str,
        db: DB,
        max_minutes: Annotated[int | None, Query(ge=1, le=1440)] = None,
    ):
        return activity_json(db, activity_or_404(db, activity_id), max_minutes=max_minutes)

    @api.get("/api/activities/{activity_id}/reviews")
    def reviews(activity_id: str, db: DB):
        activity_or_404(db, activity_id)
        rows = db.scalars(
            select(Review)
            .where(Review.activity_id == activity_id)
            .order_by(Review.id.desc())
            .limit(100)
        )
        return [
            {
                "nickname": r.nickname,
                "rating": r.rating,
                "text": r.text,
                "visited_on": r.visited_on.isoformat(),
            }
            for r in rows
        ]

    @api.post("/api/activities/{activity_id}/reviews")
    def write_review(activity_id: str, body: ReviewInput, db: DB, guest: Visitor):
        activity_or_404(db, activity_id)
        if body.visited_on > api.state.clock().astimezone(BUDAPEST).date():
            raise HTTPException(422, "A visit date cannot be in the future.")
        review = db.scalar(
            select(Review).where(Review.activity_id == activity_id, Review.visitor_hash == guest)
        )
        if review:
            for key, value in body.model_dump().items():
                setattr(review, key, value)
        else:
            db.add(Review(activity_id=activity_id, visitor_hash=guest, **body.model_dump()))
        commit(db)
        return {"status": "published"}

    @api.post("/api/plans", status_code=201)
    def create_plan(body: PlanInput, db: DB, guest: Visitor):
        today = api.state.clock().astimezone(BUDAPEST).date()
        day = today if body.use_today else body.day
        if day < today:
            raise HTTPException(422, "Choose today or a future date.")
        if body.activity_id:
            eligible_activity(db, body.activity_id, day)
        plan = Plan(id=secrets.token_urlsafe(18), title=body.title, day=day)
        db.add(plan)
        db.flush()
        host = Participant(plan_id=plan.id, visitor_hash=guest, nickname=body.nickname, is_host=True)
        db.add(host)
        if body.activity_id:
            db.flush()
            db.add(Candidate(plan_id=plan.id, activity_id=body.activity_id, participant_id=host.id))
        commit(db)
        return plan_json(db, plan, guest)

    @api.get("/api/plans/{plan_id}")
    def read_plan(plan_id: str, db: DB, guest: Visitor):
        return plan_json(db, plan_or_404(db, plan_id), guest)

    @api.post("/api/plans/{plan_id}/join")
    def join_plan(plan_id: str, body: JoinInput, db: DB, guest: Visitor):
        plan = writable_plan(db, plan_id)
        me = participant(db, plan_id, guest, required=False)
        if me:
            me.nickname = body.nickname
        else:
            db.add(Participant(plan_id=plan_id, visitor_hash=guest, nickname=body.nickname))
        commit(db)
        return plan_json(db, plan, guest)

    @api.post("/api/plans/{plan_id}/candidates", status_code=201)
    def suggest(plan_id: str, body: CandidateInput, db: DB, guest: Visitor):
        plan = writable_plan(db, plan_id)
        me = participant(db, plan_id, guest)
        eligible_activity(db, body.activity_id, plan.day)
        existing = db.scalar(
            select(Candidate).where(
                Candidate.plan_id == plan_id, Candidate.activity_id == body.activity_id
            )
        )
        if existing:
            raise HTTPException(409, "This activity is already a candidate.")
        if (
            db.scalar(
                select(func.count()).select_from(Candidate).where(Candidate.plan_id == plan_id)
            )
            >= 30
        ):
            raise HTTPException(422, "A plan can have up to 30 candidates.")
        db.add(Candidate(plan_id=plan_id, activity_id=body.activity_id, participant_id=me.id))
        commit(db)
        return plan_json(db, plan, guest)

    @api.put("/api/plans/{plan_id}/candidates/{candidate_id}/vote")
    def vote(plan_id: str, candidate_id: int, body: VoteInput, db: DB, guest: Visitor):
        plan = writable_plan(db, plan_id)
        me = participant(db, plan_id, guest)
        candidate = db.get(Candidate, candidate_id)
        if not candidate or candidate.plan_id != plan_id:
            raise HTTPException(404, "Candidate not found in this plan.")
        row = db.scalar(
            select(Vote).where(Vote.candidate_id == candidate_id, Vote.participant_id == me.id)
        )
        if row:
            row.choice = body.choice
        else:
            db.add(Vote(candidate_id=candidate_id, participant_id=me.id, choice=body.choice))
        commit(db)
        return plan_json(db, plan, guest)

    @api.post("/api/plans/{plan_id}/pick")
    def pick(plan_id: str, body: PickInput, db: DB, guest: Visitor):
        plan = writable_plan(db, plan_id)
        me = participant(db, plan_id, guest)
        if not me.is_host:
            raise HTTPException(403, "Only the plan creator can confirm the final choice.")
        candidate = db.get(Candidate, body.candidate_id)
        if not candidate or candidate.plan_id != plan_id:
            raise HTTPException(404, "Candidate not found in this plan.")
        eligible_activity(db, candidate.activity_id, plan.day)
        plan.selected_candidate = candidate.id
        commit(db)
        return plan_json(db, plan, guest)

    api.mount("/static", StaticFiles(directory=STATIC), name="static")

    @api.get("/", include_in_schema=False)
    @api.get("/plan/{plan_id}", include_in_schema=False)
    def frontend():
        return FileResponse(STATIC / "index.html")

    return api


app = create_app()
