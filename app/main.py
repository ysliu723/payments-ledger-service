"""The web application.

    uvicorn app.main:app --reload --port 8001

Interactive API docs are then at http://localhost:8001/docs
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.db.schema import create_schema
from app.db.session import engine
from app.ledger.errors import LedgerError


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create missing tables and the database guards at startup. A bigger project would use migrations.
    create_schema(engine)
    yield


app = FastAPI(title="Payments Ledger Service", version="1.0.0", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(LedgerError)
def ledger_error(request: Request, error: LedgerError) -> JSONResponse:
    """Every refused request gets the same shape: an error code and a human-readable reason."""
    return JSONResponse(status_code=error.status, content={"error": error.code, "detail": str(error)})
