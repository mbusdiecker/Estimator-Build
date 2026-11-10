"""ROMtool app entrypoint. Binds via uvicorn (see README / SPEC runtime):

  cd /root/assistant/romtool && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8807
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .routes import __version__, router

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
_INDEX = os.path.join(_STATIC_DIR, "index.html")
_HELP = os.path.join(_STATIC_DIR, "help.html")
_HOW_IT_WORKS = os.path.join(_STATIC_DIR, "how-it-works.html")


def _seed_example():
    from romtool.example_project import build_example_model

    if not db.list_projects():
        model = build_example_model()
        db.create_project(model["name"], model)


def build_app() -> FastAPI:
    app = FastAPI(title="ROMtool", version=__version__,
                  description=("In-building RF ROM estimator (phase 4 + E2 "
                      "governance + E3 commercial wrap + E4 intelligence: LTE "
                      "+ public safety + 5G NR, SS-RSRP presets, multiband "
                      "composite, Class 1 labor estimate engine with versioned "
                      "revisions, audit log, state machine, assumption "
                      "register, quote register + bid tab, documented "
                      "commercial wrap chain, crew schedule with blackout "
                      "windows, milestone cash curve, what-if scenarios, bid "
                      "form with alternates + unit prices, sanity gates with "
                      "venue-archetype checklists, actuals drift loop with "
                      "guarded refit proposals + company labor override layer, "
                      "RF-link draft estimates from RF projects, gateway-agnostic "
                      "AI endpoints (RFP scan, scenario suggestions, bid memo) "
                      "that degrade to 503 with documented schemas, and phase E5: "
                      "the Generic 60k-Seat Stadium objects with the morning bid "
                      "package and GET /reports/stadium)"))
    app.include_router(router)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.init_db()
        _seed_example()
        yield

    app.router.lifespan_context = lifespan

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(_INDEX)

    @app.get("/help", include_in_schema=False)
    def help_page():
        return FileResponse(_HELP, media_type="text/html")

    @app.get("/how-it-works", include_in_schema=False)
    def how_it_works_page():
        return FileResponse(_HOW_IT_WORKS, media_type="text/html")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    return app


app = build_app()
